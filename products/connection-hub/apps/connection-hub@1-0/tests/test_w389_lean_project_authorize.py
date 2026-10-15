"""W389: a project authorize exchange reads through the lean service and logs its build and decide times.

Live 2026-10-15: every board view's three-operation authorize rebuilt the Hub's whole request composition
(OAuth hosting, issuer registries, the Card-transaction binding), about 110 ms at the median before any
decision was read.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import load_dynamic_module_for_path


def _module():
    return load_dynamic_module_for_path(Path(__file__).resolve().parents[1] / "entrypoint.py")[1]


class _Service:
    def __init__(self, answer=None):
        self.calls = []
        self.answer = answer

    async def project_operation_authorize(self, user, **kwargs):
        self.calls.append(("one", user, kwargs))
        return self.answer or {"ok": True, "allowed": True, "reason": "project_operation_allowed"}

    async def project_operations_authorize(self, user, **kwargs):
        self.calls.append(("many", user, kwargs))
        return self.answer or {"ok": True, "decisions": [{"allowed": True}] * len(kwargs["operations"])}


@pytest.fixture()
def hub(monkeypatch):
    module = _module()
    service = _Service()
    built = []

    async def lean(entrypoint):
        built.append(entrypoint)
        return service

    async def request_wide(*_args, **_kwargs):
        raise AssertionError("an authorize exchange must not build the request-wide service")

    monkeypatch.setattr(module, "_project_authorization_service", lean)
    monkeypatch.setattr(module, "_automation_access_service", request_wide)
    monkeypatch.setattr(module, "_platform_user_payload", lambda *a, **kw: {"user_id": "person-one"})
    instance = module.ConnectionHubEntrypoint.__new__(module.ConnectionHubEntrypoint)
    return SimpleNamespace(module=module, instance=instance, service=service, built=built)


@pytest.mark.asyncio
async def test_batch_authorize_uses_the_lean_service_and_logs_build_and_decide(hub, caplog) -> None:
    operations = [{"operation": f"review.{name}", "required_grants": []} for name in ("accept", "return", "cancel")]
    with caplog.at_level(logging.INFO, logger="kdcube.playground.connection_hub"):
        answer = await hub.module.ConnectionHubEntrypoint.project_operations_authorize(
            hub.instance, data={"project_ref": "work:project:p", "resource": "mcp/board", "operations": operations},
            request=SimpleNamespace(state=SimpleNamespace(), scope={}))

    assert answer["ok"] is True and len(answer["decisions"]) == 3
    assert hub.built == [hub.instance]
    kind, user, kwargs = hub.service.calls[0]
    assert kind == "many" and user == {"user_id": "person-one"}
    assert kwargs["operations"] == operations and kwargs["project_ref"] == "work:project:p"
    lines = [r.getMessage() for r in caplog.records if "[connection-hub.project-authorize]" in r.getMessage()]
    assert len(lines) == 1
    assert lines[0].startswith("[connection-hub.project-authorize] operations=3 build_ms=")
    assert "decide_ms=" in lines[0] and lines[0].endswith("outcome=ok")


@pytest.mark.asyncio
async def test_single_authorize_uses_the_lean_service(hub, caplog) -> None:
    with caplog.at_level(logging.INFO, logger="kdcube.playground.connection_hub"):
        answer = await hub.module.ConnectionHubEntrypoint.project_operation_authorize(
            hub.instance, data={"project_ref": "work:project:p", "resource": "mcp/board", "operation": "work.status.set"})

    assert answer["allowed"] is True
    assert hub.service.calls[0][0] == "one" and hub.service.calls[0][2]["operation"] == "work.status.set"
    lines = [r.getMessage() for r in caplog.records if "[connection-hub.project-authorize]" in r.getMessage()]
    assert len(lines) == 1 and " operations=1 " in lines[0] and lines[0].endswith("outcome=ok")


@pytest.mark.asyncio
async def test_the_log_names_only_a_fixed_code(hub, caplog) -> None:
    hub.service.answer = {"ok": False, "error": "Project edge for person-one is gone"}
    with caplog.at_level(logging.INFO, logger="kdcube.playground.connection_hub"):
        await hub.module.ConnectionHubEntrypoint.project_operations_authorize(
            hub.instance, data={"project_ref": "work:project:p", "operations": [{"operation": "x"}]})
    hub.service.answer = {"ok": False, "error": "project_identity_edge_unavailable"}
    with caplog.at_level(logging.INFO, logger="kdcube.playground.connection_hub"):
        await hub.module.ConnectionHubEntrypoint.project_operations_authorize(
            hub.instance, data={"project_ref": "work:project:p", "operations": [{"operation": "x"}]})

    lines = [r.getMessage() for r in caplog.records if "[connection-hub.project-authorize]" in r.getMessage()]
    assert [line.rsplit("outcome=", 1)[1] for line in lines] == ["refused", "project_identity_edge_unavailable"]
    assert all("person-one" not in line for line in lines)


@pytest.mark.asyncio
async def test_the_lean_service_reads_cards_and_catalog_but_composes_no_write_path(monkeypatch) -> None:
    module = _module()
    marks = {}

    async def persistence(entrypoint, redis):
        marks["persistence"] = redis
        return "card-persistence"

    async def grants(entrypoint):
        return None

    async def config(entrypoint, request):
        marks["config_request"] = request
        return SimpleNamespace()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("the authorize path must not compose a write or OAuth path")

    monkeypatch.setattr(module, "_delegated_card_persistence", persistence)
    monkeypatch.setattr(module, "_oauth_grant_store", grants)
    monkeypatch.setattr(module, "_delegated_oauth_config_from_entrypoint", config)
    monkeypatch.setattr(module, "_delegated_catalog_resolver", lambda entrypoint, redis: "catalog-resolver")
    monkeypatch.setattr(module, "_delegated_authority_config", lambda entrypoint: SimpleNamespace(backend="redis"))
    monkeypatch.setattr(module, "_runtime_tenant_project", lambda entrypoint: ("t", "p"))
    for name in ("_bind_card_transactions", "_card_participant_callers", "_managed_card_edit_forwarders",
                 "_project_authorization_port", "_project_invitation_binding_resolver",
                 "_bind_delegated_client_request_config"):
        monkeypatch.setattr(module, name, forbidden)
    entrypoint = SimpleNamespace(redis="redis-client")

    service = await module._project_authorization_service(entrypoint)

    assert isinstance(service, module.AutomationAccessService)
    assert marks == {"persistence": "redis-client", "config_request": None}
    assert service._persistence == "card-persistence" and service._catalog_resolver == "catalog-resolver"

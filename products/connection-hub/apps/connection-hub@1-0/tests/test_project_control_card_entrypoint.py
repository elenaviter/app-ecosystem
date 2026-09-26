"""The project path to a project's Control Card, at the app boundary (W260).

Connection Hub asks the project host (Problem Board) "may this person read or
change this project's Control Card" under the person's session, and serves two
operations that act through that answer. The creator's own
``control_card_get``/``control_card_update`` are unchanged.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from connection_hub.delegated_credentials.project_control_card_access import (
    ControlCardAuthorizationError,
    RefusingProjectControlCardAuthorizationPort,
)
from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import (
    load_dynamic_module_for_path,
)

BUNDLE_ROOT = Path(__file__).resolve().parents[1]
OPERATIONS = ("project_control_card_get", "project_control_card_update")
REQUEST = {"control_id": "aut_control", "project_ref": "work:project:one", "action": "write"}


def _entrypoint_module():
    _name, module = load_dynamic_module_for_path(BUNDLE_ROOT / "entrypoint.py")
    return module


def _membership_module():
    _name, module = load_dynamic_module_for_path(BUNDLE_ROOT / "services" / "project_membership.py")
    return module


def test_the_control_card_question_goes_to_the_project_host_under_the_session():
    module = _membership_module()
    calls = []

    async def caller(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "decision": {
            "allowed": True, "via": "project_admin", "grantor_subject": "boris", **REQUEST,
            "evidence": {"role": "admin", "operation": "project.control.update"},
        }}

    port = module.BundleOperationControlCardAuthorizer(bundle_id="problem-board@1-0", caller=caller)
    decision = asyncio.run(port.authorize_project_control_card(**REQUEST))
    assert calls == [{"bundle_id": "problem-board@1-0", "operation": "project_control_card_authorize",
                      "data": REQUEST}]
    assert decision.allowed and decision.via == "project_admin" and decision.grantor_subject == "boris"
    assert decision.evidence == {"role": "admin", "operation": "project.control.update"}


def test_an_attach_question_names_the_agent_card():
    # The board answers the owner's linking vias only for a named agent Card.
    module = _membership_module()
    calls = []
    attach = {"control_id": "aut_control", "project_ref": "work:project:one", "action": "attach"}

    async def caller(**kwargs):
        calls.append(kwargs["data"])
        return {"ok": True, "decision": {"allowed": True, "via": "owner_linking", "grantor_subject": "boris", **attach}}

    port = module.BundleOperationControlCardAuthorizer(bundle_id="problem-board@1-0", caller=caller)
    asyncio.run(port.authorize_project_control_card(**attach, access_id=" aut_agent "))
    asyncio.run(port.authorize_project_control_card(**attach))
    assert calls == [{**attach, "access_id": "aut_agent"}, attach]


def test_host_refusals_and_failures():
    module = _membership_module()

    async def refusing(**_kwargs):
        return {"ok": False, "error": {"code": "work_identity_required"}}

    refused = asyncio.run(module.BundleOperationControlCardAuthorizer(bundle_id="pb", caller=refusing)
                          .authorize_project_control_card(**REQUEST))
    assert refused.allowed is False and refused.reason == "work_identity_required"

    async def denying(**_kwargs):
        return {"ok": True, "decision": {"allowed": False, "via": "", "grantor_subject": "", **REQUEST,
                                         "reason": "work_project_control_card_write_denied",
                                         "message": "Your Card for this project does not hold project.control.update."}}

    denied = asyncio.run(module.BundleOperationControlCardAuthorizer(bundle_id="pb", caller=denying)
                         .authorize_project_control_card(**REQUEST))
    assert denied.allowed is False and denied.reason == "work_project_control_card_write_denied"
    assert denied.message.startswith("Your Card")

    async def broken(**_kwargs):
        raise RuntimeError("down")

    with pytest.raises(ControlCardAuthorizationError) as down:
        asyncio.run(module.BundleOperationControlCardAuthorizer(bundle_id="pb", caller=broken)
                    .authorize_project_control_card(**REQUEST))
    assert down.value.reason == "project_control_card_provider_unavailable"

    async def bad(**_kwargs):
        return {"ok": True, "decision": {"via": "owner"}}

    with pytest.raises(ControlCardAuthorizationError):
        asyncio.run(module.BundleOperationControlCardAuthorizer(bundle_id="pb", caller=bad)
                    .authorize_project_control_card(**REQUEST))


def test_the_descriptor_names_the_operation_and_fails_closed_without_a_provider():
    module = _membership_module()
    configured = SimpleNamespace(bundle_props={"project_membership": {"provider": {
        "bundle_id": "problem-board@1-0", "operation": "project_membership_resolve"}}})
    port = module.descriptor_control_card_port(configured)
    assert isinstance(port, module.BundleOperationControlCardAuthorizer)
    assert port._operation == "project_control_card_authorize", "the default when the descriptor names none"
    refusing = module.descriptor_control_card_port(SimpleNamespace(bundle_props={}))
    assert isinstance(refusing, RefusingProjectControlCardAuthorizationPort)

    template = yaml.safe_load((BUNDLE_ROOT / "config" / "bundles.template.yaml").read_text())
    assert "control_card_operation: project_control_card_authorize" in yaml.safe_dump(template)


def test_the_two_operations_are_csrf_protected_posts_hidden_from_menus():
    module = _entrypoint_module()
    for alias in OPERATIONS:
        method = getattr(getattr(module.ConnectionHubEntrypoint, alias), "__bundle_api_method__")
        assert method.alias == alias and method.http_method == "POST"
        assert alias in module.CSRF_PROTECTED_OPERATION_ALIASES
    source = (BUNDLE_ROOT / "entrypoint.py").read_text()
    for alias in OPERATIONS:
        assert f'"{alias}": {{"visibility": {{"user_types": []}}}}' in source


def test_the_operations_pass_the_person_and_the_body_to_the_project_path(monkeypatch):
    module = _entrypoint_module()
    calls = []

    class Access:
        async def get(self, user, **kwargs):
            calls.append(("get", user, kwargs))
            return {"ok": True}

        async def update(self, user, **kwargs):
            calls.append(("update", user, kwargs))
            return {"ok": True}

    async def access(_self, _request):
        return Access()

    monkeypatch.setattr(module.ConnectionHubEntrypoint, "_project_control_card_access", access)
    monkeypatch.setattr(module, "_platform_user_payload", lambda *a, **kw: {"user_id": "second-admin"})
    monkeypatch.setattr(module, "_audit_request_id", lambda _request: "req-1")
    instance = module.ConnectionHubEntrypoint.__new__(module.ConnectionHubEntrypoint)
    base = {"control_id": " aut_control ", "project_ref": "work:project:one"}

    asyncio.run(module.ConnectionHubEntrypoint.project_control_card_get(instance, data=base))
    asyncio.run(module.ConnectionHubEntrypoint.project_control_card_update(
        instance, data={**base, "resource_grants": {"problem_board": ["work:relay"]},
                        "composition_mode": "and", "expected_card_revision": 4}))

    assert [name for name, *_ in calls] == ["get", "update"]
    assert all(user == {"user_id": "second-admin"} for _, user, _ in calls)
    assert calls[0][2] == {"control_id": "aut_control", "project_ref": "work:project:one"}
    update = calls[1][2]
    assert update["control_id"] == "aut_control" and update["request_id"] == "req-1"
    assert update["resource_grants"] == {"problem_board": ["work:relay"]}
    assert update["composition_mode"] == "and" and update["expected_card_revision"] == 4
    # A field the request did not send stays unchanged (None), as on the creator's path.
    assert update["resource_operations"] is None and update["properties"] is None


def test_the_creator_path_reads_the_same_change_fields(monkeypatch):
    module = _entrypoint_module()
    seen = {}

    class Service:
        async def control_card_update(self, user, **kwargs):
            seen.update(kwargs)
            return {"ok": True}

    async def service(_self, _request):
        return Service()

    monkeypatch.setattr(module, "_automation_access_service", service)
    monkeypatch.setattr(module, "_platform_user_payload", lambda *a, **kw: {"user_id": "boris"})
    instance = module.ConnectionHubEntrypoint.__new__(module.ConnectionHubEntrypoint)
    asyncio.run(module.ConnectionHubEntrypoint.control_card_update(
        instance, data={"control_id": "aut_control", "label": " Board ", "accepted_operations": {"r": ["x"]}}))
    assert seen["control_id"] == "aut_control" and seen["label"] == "Board"
    assert seen["accepted_operations"] == {"r": ["x"]} and seen["resource_grants"] is None
    assert "_delegable_grants" not in seen and "_record_transform" not in seen


BINDING_OPERATIONS = ("project_control_card_attach", "project_control_card_detach")


def test_attach_and_detach_are_csrf_protected_posts_hidden_from_menus():
    module = _entrypoint_module()
    source = (BUNDLE_ROOT / "entrypoint.py").read_text()
    for alias in BINDING_OPERATIONS:
        method = getattr(getattr(module.ConnectionHubEntrypoint, alias), "__bundle_api_method__")
        assert method.alias == alias and method.http_method == "POST"
        assert alias in module.CSRF_PROTECTED_OPERATION_ALIASES
        assert f'"{alias}": {{"visibility": {{"user_types": []}}}}' in source


def test_no_request_field_names_a_control_card_holder(monkeypatch):
    """Review condition (claude-main): the holder comes only from the project host's answer."""

    module = _entrypoint_module()
    calls = []

    class Access:
        async def attach(self, user, **kwargs):
            calls.append(("attach", kwargs))
            return {"ok": True}

        async def detach(self, user, **kwargs):
            calls.append(("detach", kwargs))
            return {"ok": True}

    class Service:
        async def attach_control_card(self, user, **kwargs):
            calls.append(("plain", kwargs))
            return {"ok": True}

    async def access(_self, _request):
        return Access()

    async def service(_self, _request):
        return Service()

    monkeypatch.setattr(module.ConnectionHubEntrypoint, "_project_control_card_access", access)
    monkeypatch.setattr(module, "_automation_access_service", service)
    monkeypatch.setattr(module, "_platform_user_payload", lambda *a, **kw: {"user_id": "admin-a"})
    monkeypatch.setattr(module, "_audit_request_id", lambda _request: "req-1")
    instance = module.ConnectionHubEntrypoint.__new__(module.ConnectionHubEntrypoint)
    forged = {"control_id": "c", "project_ref": "work:project:one", "access_id": "a",
              "holder_subject": "someone", "_control_holder": "someone", "grantor_subject": "someone"}

    asyncio.run(module.ConnectionHubEntrypoint.project_control_card_attach(instance, data=forged))
    asyncio.run(module.ConnectionHubEntrypoint.project_control_card_detach(instance, data=forged))
    asyncio.run(module.ConnectionHubEntrypoint.control_card_attach(instance, data=forged))

    assert [name for name, _ in calls] == ["attach", "detach", "plain"]
    for _, kwargs in calls:
        assert not {"holder_subject", "_control_holder", "grantor_subject"} & set(kwargs)
    assert calls[0][1] == {"control_id": "c", "project_ref": "work:project:one", "access_id": "a",
                           "expected_card_revision": None, "request_id": "req-1", "replace_control_id": ""}

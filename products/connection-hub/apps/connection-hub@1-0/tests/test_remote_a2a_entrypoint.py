"""W937 phase 1: the Hub's remote A2A connector operations."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import (
    load_dynamic_module_for_path,
)

PLANTED = "https://evil.example/approve"


def _entrypoint_module():
    bundle_root = Path(__file__).resolve().parents[1]
    _name, module = load_dynamic_module_for_path(bundle_root / "entrypoint.py")
    return module


class _Connector:
    def __init__(self, revision: int = 1) -> None:
        self.revision = revision

    def to_public_dict(self):
        return {"connector_id": "a2a_0123456789abcdef01234567", "revision": self.revision}


class _Service:
    def __init__(self, failure: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.failure = failure

    async def _record(self, method: str, **kwargs):
        self.calls.append({"method": method, **kwargs})
        if self.failure is not None:
            raise self.failure
        return _Connector(revision=int(kwargs.get("expected_revision") or 0) + 1)

    async def list(self, **kwargs):
        self.calls.append({"method": "list", **kwargs})
        return [_Connector()]

    async def create(self, **kwargs):
        return await self._record("create", **kwargs)

    async def refresh(self, **kwargs):
        return await self._record("refresh", **kwargs)

    async def accept_descriptor(self, **kwargs):
        return await self._record("accept_descriptor", **kwargs)

    async def set_enabled(self, **kwargs):
        return await self._record("set_enabled", **kwargs)

    async def replace_credential(self, **kwargs):
        return await self._record("replace_credential", **kwargs)

    async def delete(self, **kwargs):
        return await self._record("delete", **kwargs)


def _entrypoint(monkeypatch, service: _Service):
    module = _entrypoint_module()
    monkeypatch.setattr(module, "_remote_a2a_service", lambda *_args: service)
    monkeypatch.setattr(module, "_platform_user_id", lambda *_args, **_kwargs: "user-1")
    instance = module.ConnectionHubEntrypoint.__new__(module.ConnectionHubEntrypoint)
    return SimpleNamespace(module=module, instance=instance)


@pytest.mark.asyncio
async def test_create_is_owner_scoped_and_never_returns_the_credential(monkeypatch) -> None:
    service = _Service()
    hub = _entrypoint(monkeypatch, service)
    response = await hub.module.ConnectionHubEntrypoint.remote_a2a_connector_create(
        hub.instance,
        data={
            "label": "Outside agent",
            "card_url": "https://agents.example/a2a/agent-card.json",
            "credential_mode": "Bearer",
            "credential_value": "outbound-secret",
        },
    )
    assert response == {"ok": True, "connector": {"connector_id": "a2a_0123456789abcdef01234567", "revision": 1}}
    assert "outbound-secret" not in repr(response)
    assert service.calls == [
        {
            "method": "create",
            "owner_subject": "user-1",
            "label": "Outside agent",
            "card_url": "https://agents.example/a2a/agent-card.json",
            "credential_mode": "bearer",
            "credential_header": "",
            "credential_value": "outbound-secret",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("alias", "method"),
    [
        ("remote_a2a_connector_refresh", "refresh"),
        ("remote_a2a_connector_accept_descriptor", "accept_descriptor"),
        ("remote_a2a_connector_set_enabled", "set_enabled"),
        ("remote_a2a_connector_delete", "delete"),
    ],
)
async def test_mutations_forward_the_revision_precondition(monkeypatch, alias: str, method: str) -> None:
    service = _Service()
    hub = _entrypoint(monkeypatch, service)
    response = await getattr(hub.module.ConnectionHubEntrypoint, alias)(
        hub.instance, data={"connector_id": "a2a_0123456789abcdef01234567", "expected_revision": 4}
    )
    assert response["ok"] is True and response["connector"]["revision"] == 5
    assert service.calls[0]["method"] == method and service.calls[0]["expected_revision"] == 4
    assert service.calls[0]["owner_subject"] == "user-1"


@pytest.mark.asyncio
async def test_a_missing_revision_is_a_fixed_400(monkeypatch) -> None:
    hub = _entrypoint(monkeypatch, _Service())
    response = await hub.module.ConnectionHubEntrypoint.remote_a2a_connector_refresh(
        hub.instance, data={"connector_id": "a2a_0123456789abcdef01234567"}
    )
    assert response["ok"] is False and response["status"] == 400


@pytest.mark.asyncio
async def test_client_and_policy_failures_are_fixed_codes(monkeypatch) -> None:
    from app_foundation.a2a import A2AClientError
    from app_foundation.net import OutboundEndpointDenied
    from connection_hub.remote_a2a import RemoteA2ARecordError

    for failure, code in (
        (A2AClientError("remote_request_failed"), "remote_request_failed"),
        (OutboundEndpointDenied("endpoint_private_network_forbidden"), "endpoint_private_network_forbidden"),
        (RemoteA2ARecordError("card_endpoint_cross_origin"), "card_endpoint_cross_origin"),
    ):
        hub = _entrypoint(monkeypatch, _Service(failure=failure))
        response = await hub.module.ConnectionHubEntrypoint.remote_a2a_connector_create(
            hub.instance, data={"label": "x", "card_url": "https://agents.example/card.json"}
        )
        assert response == {"ok": False, "error": code, "status": 400}
        assert PLANTED not in repr(response)


@pytest.mark.asyncio
async def test_an_unexpected_failure_is_a_fixed_503(monkeypatch) -> None:
    hub = _entrypoint(monkeypatch, _Service(failure=RuntimeError(f"boom {PLANTED}")))
    response = await hub.module.ConnectionHubEntrypoint.remote_a2a_connector_create(
        hub.instance, data={"label": "x", "card_url": "https://agents.example/card.json"}
    )
    assert response["error"] == "remote_a2a_connector_unavailable" and response["status"] == 503
    assert PLANTED not in repr(response)


@pytest.mark.asyncio
async def test_an_unauthenticated_caller_is_refused(monkeypatch) -> None:
    service = _Service()
    hub = _entrypoint(monkeypatch, service)
    monkeypatch.setattr(hub.module, "_platform_user_id", lambda *_args, **_kwargs: "")
    response = await hub.module.ConnectionHubEntrypoint.remote_a2a_connectors_list(hub.instance)
    assert response == {"ok": False, "error": "remote_a2a_requires_authenticated_user"}
    assert service.calls == []

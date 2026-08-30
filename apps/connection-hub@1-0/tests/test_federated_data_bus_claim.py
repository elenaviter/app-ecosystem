from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import (
    load_dynamic_module_for_path,
)


def _entrypoint_module():
    bundle_root = Path(__file__).resolve().parents[1]
    _name, module = load_dynamic_module_for_path(bundle_root / "entrypoint.py")
    return module


def _entrypoint(module, *, identity_authority, user_type="registered"):
    instance = module.ConnectionHubEntrypoint.__new__(module.ConnectionHubEntrypoint)
    instance._comm = SimpleNamespace(
        user_id="user-1",
        roles=["kdcube:role:registered"],
        permissions=["connections:read"],
        service={},
    )
    instance._comm_context = SimpleNamespace(
        actor=SimpleNamespace(
            tenant_id="demo-tenant",
            project_id="demo-project",
        ),
        user=SimpleNamespace(
            user_id="user-1",
            user_type=user_type,
            roles=["kdcube:role:registered"],
            permissions=["connections:read"],
            identity_authority=identity_authority,
        )
    )
    instance.bundle_props = {}
    return instance


class _LiveSessions:
    def __init__(self) -> None:
        self.calls = []

    async def register_live_session(self, user_id, session_id, expires_at):
        self.calls.append((user_id, session_id, expires_at))


@pytest.mark.asyncio
async def test_direct_platform_session_claim_uses_host_authenticated_context(monkeypatch):
    module = _entrypoint_module()
    instance = _entrypoint(
        module,
        identity_authority={},
    )
    issued = []
    live_sessions = _LiveSessions()

    async def external_authenticator(*args, **kwargs):
        raise AssertionError("a direct platform session does not need an external authenticator")

    async def issue_token(**kwargs):
        issued.append(kwargs)
        return SimpleNamespace(
            token="federated-token",
            session=SimpleNamespace(session_id="session-1"),
            expires_at=123456,
        )

    monkeypatch.setattr(module, "_authenticate_request_context", external_authenticator)
    monkeypatch.setattr(module, "issue_federated_data_bus_token", issue_token)
    monkeypatch.setattr(module, "_automation_access_service", lambda *args, **kwargs: live_sessions)

    result = await module.ConnectionHubEntrypoint.federated_data_bus_claim(
        instance,
        data={},
        request=SimpleNamespace(),
    )

    assert result["ok"] is True
    assert result["session_id"] == "session-1"
    assert issued[0]["user_id"] == "user-1"
    assert issued[0]["user_type"] == "registered"
    authority = issued[0]["identity_authority"]
    assert authority["authority_id"] == "platform"
    assert authority["platform_user_id"] == "user-1"
    assert live_sessions.calls == [("user-1", "session-1", 123456)]


@pytest.mark.asyncio
async def test_projected_external_identity_stays_on_authenticator_path(monkeypatch):
    module = _entrypoint_module()
    instance = _entrypoint(
        module,
        identity_authority={
            "authority_id": "telegram",
            "actor_user_id": "telegram_100",
            "platform_user_id": "user-1",
        },
    )
    calls = []

    async def external_authenticator(*args, **kwargs):
        calls.append((args, kwargs))
        return {
            "ok": False,
            "authenticated": False,
            "error": "no_authenticator_accepted",
        }

    monkeypatch.setattr(module, "_authenticate_request_context", external_authenticator)

    result = await module.ConnectionHubEntrypoint.federated_data_bus_claim(
        instance,
        data={},
        request=SimpleNamespace(),
    )

    assert calls
    assert result == {
        "ok": False,
        "error": "no_authenticator_accepted",
        "message": "Connection Hub could not authenticate this request.",
    }


def test_anonymous_host_context_is_not_a_platform_authenticator():
    module = _entrypoint_module()
    instance = _entrypoint(
        module,
        identity_authority={},
        user_type="anonymous",
    )

    assert module._authenticated_platform_request_context(instance) == {}


def test_descriptor_owned_platform_authority_is_accepted():
    module = _entrypoint_module()
    instance = _entrypoint(
        module,
        identity_authority={
            "authority_id": "custom.platform",
            "actor_user_id": "user-1",
        },
    )
    instance.bundle_props = {
        "authority_registry": {
            "authorities": {
                "custom.platform": {
                    "platform": True,
                }
            }
        }
    }

    result = module._authenticated_platform_request_context(instance)

    assert result["authenticated"] is True
    assert result["platform_user_id"] == "user-1"
    assert result["selected_authenticator"] == "platform_session"

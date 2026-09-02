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


def _surface_module():
    bundle_root = Path(__file__).resolve().parents[1]
    _name, module = load_dynamic_module_for_path(
        bundle_root / "surfaces" / "remote_mcp.py"
    )
    return module


class _Connector:
    def __init__(self, revision: int = 1) -> None:
        self.revision = revision

    def to_public_dict(self):
        return {
            "connector_id": "mcp_0123456789abcdef01234567",
            "revision": self.revision,
            "credential_present": True,
        }


class _Service:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def list(self, **kwargs):
        self.calls.append({"method": "list", **kwargs})
        return [_Connector()]

    async def create(self, **kwargs):
        self.calls.append({"method": "create", **kwargs})
        return _Connector()

    async def refresh(self, **kwargs):
        self.calls.append({"method": "refresh", **kwargs})
        return _Connector(revision=kwargs["expected_revision"] + 1)


@pytest.fixture()
def entrypoint(monkeypatch):
    module = _entrypoint_module()
    service = _Service()
    monkeypatch.setattr(module, "_remote_mcp_service", lambda *_args: service)
    monkeypatch.setattr(
        module, "_platform_user_id", lambda *_args, **_kwargs: "user-1"
    )
    instance = module.ConnectionHubEntrypoint.__new__(module.ConnectionHubEntrypoint)
    return SimpleNamespace(module=module, instance=instance, service=service)


@pytest.mark.asyncio
async def test_connector_create_is_owner_scoped_and_never_returns_secret(entrypoint):
    response = await entrypoint.module.ConnectionHubEntrypoint.remote_mcp_connector_create(
        entrypoint.instance,
        data={
            "label": "Paid search",
            "endpoint": "https://mcp.example.test/mcp",
            "credential_mode": "bearer",
            "credential_value": "upstream-secret",
        },
    )

    assert response == {
        "ok": True,
        "connector": {
            "connector_id": "mcp_0123456789abcdef01234567",
            "revision": 1,
            "credential_present": True,
        },
    }
    assert "upstream-secret" not in repr(response)
    assert entrypoint.service.calls == [
        {
            "method": "create",
            "owner_subject": "user-1",
            "label": "Paid search",
            "endpoint": "https://mcp.example.test/mcp",
            "credential_mode": "bearer",
            "credential_header": "",
            "credential_value": "upstream-secret",
        }
    ]


@pytest.mark.asyncio
async def test_refresh_forwards_revision_precondition(entrypoint):
    response = await entrypoint.module.ConnectionHubEntrypoint.remote_mcp_connector_refresh(
        entrypoint.instance,
        data={
            "connector_id": "mcp_0123456789abcdef01234567",
            "expected_revision": 7,
        },
    )

    assert response["connector"]["revision"] == 8
    assert entrypoint.service.calls == [
        {
            "method": "refresh",
            "owner_subject": "user-1",
            "connector_id": "mcp_0123456789abcdef01234567",
            "expected_revision": 7,
        }
    ]


@pytest.mark.asyncio
async def test_connector_operations_require_an_authenticated_owner(
    entrypoint, monkeypatch
):
    monkeypatch.setattr(
        entrypoint.module, "_platform_user_id", lambda *_args, **_kwargs: ""
    )

    response = await entrypoint.module.ConnectionHubEntrypoint.remote_mcp_connectors_list(
        entrypoint.instance
    )

    assert response == {
        "ok": False,
        "error": "remote_mcp_requires_authenticated_user",
    }
    assert entrypoint.service.calls == []


def test_proxy_tool_does_not_return_upstream_exception_details():
    module = _surface_module()
    secret = "upstream-secret-that-must-not-reach-the-caller"
    failure = RuntimeError(
        f"request failed with Authorization: Bearer {secret}"
    )

    result = module._public_upstream_failure(failure)

    assert result == {
        "ok": False,
        "error": "remote_mcp_call_failed",
        "reason": "remote_mcp_call_failed",
        "failure_type": "RuntimeError",
        "retryable": True,
    }
    assert secret not in repr(result)

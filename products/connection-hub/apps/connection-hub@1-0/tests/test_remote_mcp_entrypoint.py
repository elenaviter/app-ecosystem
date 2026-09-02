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
        self.connector_id = "mcp_0123456789abcdef01234567"
        self.label = "Paid search"

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


class _OAuthService:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def start(self, **kwargs):
        self.calls.append({"method": "start", **kwargs})
        return {
            "authorize_url": "https://auth.example.test/authorize?state=opaque",
            "state_id": "state-digest",
            "expires_at": 1_788_200_900,
        }

    async def complete(self, **kwargs):
        self.calls.append({"method": "complete", **kwargs})
        return {
            "connector": _Connector(),
            "return_hint": "https://hub.example.test/settings",
        }

    def client_metadata(self, **kwargs):
        self.calls.append({"method": "client_metadata", **kwargs})
        return {
            "client_name": "KDCube Connection Hub",
            "redirect_uris": [kwargs["callback_url"]],
            "token_endpoint_auth_method": "none",
        }


@pytest.fixture()
def entrypoint(monkeypatch):
    module = _entrypoint_module()
    service = _Service()
    monkeypatch.setattr(module, "_remote_mcp_service", lambda *_args: service)
    oauth_service = _OAuthService()
    monkeypatch.setattr(
        module, "_remote_mcp_oauth_service", lambda *_args: oauth_service
    )
    monkeypatch.setattr(
        module,
        "_remote_mcp_oauth_callback_url",
        lambda *_args: "https://hub.example.test/oauth/callback",
    )
    monkeypatch.setattr(
        module,
        "_remote_mcp_oauth_client_metadata_url",
        lambda *_args: "https://hub.example.test/oauth/client-metadata",
    )
    monkeypatch.setattr(
        module, "_platform_user_id", lambda *_args, **_kwargs: "user-1"
    )
    instance = module.ConnectionHubEntrypoint.__new__(module.ConnectionHubEntrypoint)
    return SimpleNamespace(
        module=module,
        instance=instance,
        service=service,
        oauth_service=oauth_service,
    )


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
async def test_oauth_start_is_owner_scoped_and_returns_only_authorization_coordinates(
    entrypoint,
):
    response = await entrypoint.module.ConnectionHubEntrypoint.remote_mcp_connector_start_oauth(
        entrypoint.instance,
        data={
            "label": "OAuth records",
            "endpoint": "https://mcp.example.test/mcp",
            "return_hint": "https://hub.example.test/settings",
        },
        request=SimpleNamespace(),
    )

    assert response == {
        "ok": True,
        "authorize_url": "https://auth.example.test/authorize?state=opaque",
        "state_id": "state-digest",
        "expires_at": 1_788_200_900,
    }
    assert entrypoint.oauth_service.calls == [
        {
            "method": "start",
            "owner_subject": "user-1",
            "label": "OAuth records",
            "endpoint": "https://mcp.example.test/mcp",
            "callback_url": "https://hub.example.test/oauth/callback",
            "client_metadata_url": "https://hub.example.test/oauth/client-metadata",
            "return_hint": "https://hub.example.test/settings",
            "connector_id": "",
            "expected_revision": 0,
            "oauth_client_mode": "",
            "oauth_client": None,
        }
    ]


@pytest.mark.asyncio
async def test_oauth_start_forwards_provisioned_client_without_returning_secret(
    entrypoint,
):
    response = await entrypoint.module.ConnectionHubEntrypoint.remote_mcp_connector_start_oauth(
        entrypoint.instance,
        data={
            "label": "Provider console records",
            "endpoint": "https://mcp.example.test/mcp",
            "oauth_client_mode": "provisioned",
            "oauth_client": {
                "client_id": "provider-client",
                "client_secret": "provider-secret",
                "token_endpoint_auth_method": "client_secret_basic",
            },
        },
        request=SimpleNamespace(),
    )

    assert response["ok"] is True
    assert "provider-secret" not in repr(response)
    assert entrypoint.oauth_service.calls[-1] == {
        "method": "start",
        "owner_subject": "user-1",
        "label": "Provider console records",
        "endpoint": "https://mcp.example.test/mcp",
        "callback_url": "https://hub.example.test/oauth/callback",
        "client_metadata_url": "https://hub.example.test/oauth/client-metadata",
        "return_hint": "",
        "connector_id": "",
        "expected_revision": 0,
        "oauth_client_mode": "provisioned",
        "oauth_client": {
            "client_id": "provider-client",
            "client_secret": "provider-secret",
            "token_endpoint_auth_method": "client_secret_basic",
        },
    }


@pytest.mark.asyncio
async def test_oauth_client_metadata_publishes_the_registered_callback(entrypoint):
    response = await entrypoint.module.ConnectionHubEntrypoint.remote_mcp_oauth_client_metadata(
        entrypoint.instance,
        request=SimpleNamespace(),
    )

    assert response["redirect_uris"] == [
        "https://hub.example.test/oauth/callback"
    ]
    assert entrypoint.oauth_service.calls == [
        {
            "method": "client_metadata",
            "callback_url": "https://hub.example.test/oauth/callback",
        }
    ]


@pytest.mark.asyncio
async def test_oauth_reconnect_rejects_invalid_revision_before_start(entrypoint):
    response = await entrypoint.module.ConnectionHubEntrypoint.remote_mcp_connector_start_oauth(
        entrypoint.instance,
        data={
            "connector_id": "mcp_0123456789abcdef01234567",
            "expected_revision": "not-a-number",
        },
        request=SimpleNamespace(),
    )

    assert response == {
        "ok": False,
        "error": "expected_revision_invalid",
        "status": 400,
    }
    assert entrypoint.oauth_service.calls == []


@pytest.mark.asyncio
async def test_oauth_callback_completes_and_broadcasts_connector_refresh(
    entrypoint,
):
    request = SimpleNamespace(
        headers={"host": "hub.example.test", "x-forwarded-proto": "https"}
    )

    response = await entrypoint.module.ConnectionHubEntrypoint.remote_mcp_oauth_callback(
        entrypoint.instance,
        request=request,
        code="provider-code",
        state="opaque-state",
        iss="https://auth.example.test",
    )

    body = response.body.decode("utf-8")
    assert "Connection complete" in body
    assert "remote_mcp.connector.connected" in body
    assert "mcp_0123456789abcdef01234567" in body
    assert entrypoint.oauth_service.calls == [
        {
            "method": "complete",
            "state": "opaque-state",
            "code": "provider-code",
            "callback_url": "https://hub.example.test/oauth/callback",
            "issuer": "https://auth.example.test",
            "provider_error": "",
        }
    ]


def test_callback_return_link_requires_exact_origin(entrypoint):
    assert entrypoint.module._same_origin_return_link(
        origin="https://hub.example.test",
        candidate="https://hub.example.test.evil.test/settings",
    ) == "https://hub.example.test"
    assert entrypoint.module._same_origin_return_link(
        origin="https://hub.example.test",
        candidate="https://hub.example.test/settings",
    ) == "https://hub.example.test/settings"
    assert entrypoint.module._same_origin_return_link(
        origin="https://hub.example.test",
        candidate="https://hub.example.test:not-a-port/settings",
    ) == "https://hub.example.test"


@pytest.mark.asyncio
async def test_raw_oauth_bundle_is_rejected_by_ordinary_create(entrypoint):
    response = await entrypoint.module.ConnectionHubEntrypoint.remote_mcp_connector_create(
        entrypoint.instance,
        data={
            "label": "OAuth records",
            "endpoint": "https://mcp.example.test/mcp",
            "credential_mode": "oauth",
            "credential_value": "must-not-be-accepted",
        },
    )

    assert response == {
        "ok": False,
        "error": "remote_mcp_oauth_requires_browser_flow",
        "status": 400,
    }
    assert entrypoint.service.calls == []


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

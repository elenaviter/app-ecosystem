from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import (
    load_dynamic_module_for_path,
)
from kdcube_ai_app.infra.plugin.bundle_loader import discover_bundle_interface_manifest


def _load_entrypoint_module():
    bundle_root = Path(__file__).resolve().parents[1]
    _module_name, module = load_dynamic_module_for_path(bundle_root / "entrypoint.py")
    return module


def _request(*, method: str = "GET") -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": (
                "/api/integrations/bundles/tenant-a/project-a/"
                "connection-hub@1-0/public/oauth/"
                ".well-known/oauth-authorization-server"
            ),
            "raw_path": b"",
            "query_string": b"",
            "headers": [(b"host", b"runtime.example.test")],
            "client": ("127.0.0.1", 12345),
            "server": ("runtime.example.test", 443),
        }
    )


def _postgres_entrypoint(module, durable):
    entrypoint = module.ConnectionHubEntrypoint.__new__(module.ConnectionHubEntrypoint)
    entrypoint.bundle_props = {
        "connections": {
            "delegated_credentials": {
                "oauth": {"enabled": True},
                "authority": {
                    "backend": "postgresql",
                    "generation_id": "durable-authority-v1",
                },
            }
        }
    }
    entrypoint.redis = object()
    entrypoint.pg_pool = object()
    entrypoint._durable_authority = durable
    entrypoint.runtime_identity = lambda: {
        "tenant": "tenant-a",
        "project": "project-a",
    }
    return entrypoint


def test_connection_hub_post_surfaces_have_explicit_csrf_classification():
    module = _load_entrypoint_module()
    manifest = discover_bundle_interface_manifest(
        module.ConnectionHubEntrypoint,
        bundle_id="connection-hub@1-0",
    )

    operation_posts = {
        spec.alias
        for spec in manifest.api_endpoints
        if spec.route == "operations" and spec.http_method == "POST"
    }
    public_posts = {
        spec.alias
        for spec in manifest.api_endpoints
        if spec.route == "public" and spec.http_method == "POST"
    }
    protected = {
        spec.alias
        for spec in manifest.api_endpoints
        if spec.csrf
    }

    assert protected == set(module.CSRF_PROTECTED_OPERATION_ALIASES)
    assert protected.isdisjoint(module.CSRF_EXEMPT_POST_OPERATION_ALIASES)
    assert operation_posts == (
        protected | set(module.CSRF_EXEMPT_POST_OPERATION_ALIASES)
    )
    assert public_posts == set(module.CSRF_EXEMPT_PUBLIC_POST_ALIASES)


@pytest.mark.asyncio
async def test_connection_hub_discovery_advertises_enabled_client_registration_modes():
    module = _load_entrypoint_module()
    entrypoint = module.ConnectionHubEntrypoint.__new__(module.ConnectionHubEntrypoint)
    entrypoint.bundle_props = {
        "connections": {
            "delegated_credentials": {
                "oauth": {
                    "enabled": True,
                    "dynamic_client_registration": {"enabled": False},
                    "client_id_metadata_documents": {"enabled": True},
                }
            }
        }
    }
    entrypoint.runtime_identity = lambda: {"tenant": "tenant-a", "project": "project-a"}

    response = await entrypoint.oauth_get(
        request=_request(),
        path_tail=".well-known/oauth-authorization-server",
    )
    payload = json.loads(response.body)

    assert response.status_code == 200
    assert payload["issuer"] == (
        "https://runtime.example.test/api/integrations/bundles/tenant-a/project-a/"
        "connection-hub@1-0/public/oauth"
    )
    assert payload["client_id_metadata_document_supported"] is True
    assert "registration_endpoint" not in payload
    assert payload["revocation_endpoint"] == (
        "https://runtime.example.test/api/integrations/bundles/tenant-a/project-a/"
        "connection-hub@1-0/public/oauth/revoke"
    )
    assert payload["device_authorization_endpoint"] == (
        "https://runtime.example.test/api/integrations/bundles/tenant-a/project-a/"
        "connection-hub@1-0/public/oauth/device_authorization"
    )


@pytest.mark.asyncio
async def test_fresh_postgresql_entrypoint_serves_discovery_and_access_list(
    monkeypatch,
):
    module = _load_entrypoint_module()
    readiness_checks = []

    async def _ensure_ready():
        readiness_checks.append("checked")

    durable = SimpleNamespace(oauth=object(), ensure_ready=_ensure_ready)

    class _AccessService:
        async def list_access(self, user):
            assert user == {"user_id": "user-a"}
            return {"ok": True, "platform_user_id": "user-a", "items": []}

    service = _AccessService()

    async def _access_service_for(_entrypoint, _config):
        return service

    monkeypatch.setattr(module, "_automation_access_service_for", _access_service_for)
    monkeypatch.setattr(
        module,
        "_platform_user_payload",
        lambda *_args, **_kwargs: {"user_id": "user-a"},
    )
    monkeypatch.setattr(module, "_invocation_policy_service", lambda _entrypoint: object())

    discovery_entrypoint = _postgres_entrypoint(module, durable)
    response = await discovery_entrypoint.oauth_get(
        request=_request(),
        path_tail=".well-known/oauth-authorization-server",
    )
    assert response.status_code == 200

    access_entrypoint = _postgres_entrypoint(module, durable)
    listing = await access_entrypoint.delegated_access_list(
        request=_request(),
        user_id="user-a",
    )
    assert listing["ok"] is True
    assert readiness_checks == ["checked", "checked"]


@pytest.mark.asyncio
async def test_fresh_postgresql_entrypoint_refuses_without_activation_receipt(
    monkeypatch,
):
    module = _load_entrypoint_module()

    async def _ensure_ready():
        raise RuntimeError("authority_cutover_receipt_missing")

    durable = SimpleNamespace(oauth=object(), ensure_ready=_ensure_ready)

    async def _access_service_for(_entrypoint, _config):
        return object()

    monkeypatch.setattr(module, "_automation_access_service_for", _access_service_for)
    entrypoint = _postgres_entrypoint(module, durable)

    with pytest.raises(RuntimeError, match="authority_cutover_receipt_missing"):
        await entrypoint.oauth_get(
            request=_request(),
            path_tail=".well-known/oauth-authorization-server",
        )


@pytest.mark.asyncio
async def test_connection_hub_dispatches_device_authorization_routes(monkeypatch):
    module = _load_entrypoint_module()
    entrypoint = module.ConnectionHubEntrypoint.__new__(module.ConnectionHubEntrypoint)
    entrypoint.bundle_props = {
        "connections": {
            "delegated_credentials": {"oauth": {"enabled": True}}
        }
    }
    entrypoint.runtime_identity = lambda: {"tenant": "tenant-a", "project": "project-a"}
    calls = []

    async def _route(request):
        calls.append(request.state.oauth_delegated_issuer)
        return module.JSONResponse({"routed": True})

    monkeypatch.setattr(module, "oauth_device_authorization", _route)
    monkeypatch.setattr(module, "oauth_verify_device", _route)
    monkeypatch.setattr(module, "oauth_device_complete", _route)

    post_response = await entrypoint.oauth_post(
        request=_request(method="POST"),
        path_tail="device_authorization",
    )
    verify_response = await entrypoint.oauth_get(
        request=_request(),
        path_tail="device",
    )
    complete_response = await entrypoint.oauth_get(
        request=_request(),
        path_tail="device/complete",
    )

    assert json.loads(post_response.body) == {"routed": True}
    assert json.loads(verify_response.body) == {"routed": True}
    assert json.loads(complete_response.body) == {"routed": True}
    assert calls == [
        "https://runtime.example.test/api/integrations/bundles/tenant-a/project-a/"
        "connection-hub@1-0/public/oauth",
        "https://runtime.example.test/api/integrations/bundles/tenant-a/project-a/"
        "connection-hub@1-0/public/oauth",
        "https://runtime.example.test/api/integrations/bundles/tenant-a/project-a/"
        "connection-hub@1-0/public/oauth",
    ]


@pytest.mark.asyncio
async def test_connection_hub_dispatches_the_advertised_revocation_route(monkeypatch):
    module = _load_entrypoint_module()
    entrypoint = module.ConnectionHubEntrypoint.__new__(module.ConnectionHubEntrypoint)
    entrypoint.bundle_props = {
        "connections": {
            "delegated_credentials": {"oauth": {"enabled": True}}
        }
    }
    entrypoint.runtime_identity = lambda: {"tenant": "tenant-a", "project": "project-a"}

    async def _revoke(request):
        return module.JSONResponse({"routed": True})

    monkeypatch.setattr(module, "oauth_revoke", _revoke)
    response = await entrypoint.oauth_post(
        request=_request(method="POST"),
        path_tail="revoke",
    )

    assert response.status_code == 200
    assert json.loads(response.body) == {"routed": True}


@pytest.mark.asyncio
async def test_protected_resource_metadata_advertises_enabled_admission_endpoint():
    module = _load_entrypoint_module()
    entrypoint = module.ConnectionHubEntrypoint.__new__(module.ConnectionHubEntrypoint)
    entrypoint.bundle_props = {
        "connections": {
            "delegated_credentials": {
                "oauth": {"enabled": True},
                "admission": {"enabled": True},
            }
        }
    }
    entrypoint.runtime_identity = lambda: {"tenant": "tenant-a", "project": "project-a"}

    response = await entrypoint.oauth_get(
        request=_request(),
        path_tail=".well-known/oauth-protected-resource",
    )
    payload = json.loads(response.body)

    assert payload["connection_hub_delegated_admission_endpoint"] == (
        "https://runtime.example.test/api/integrations/bundles/tenant-a/project-a/"
        "connection-hub@1-0/public/delegated_admission"
    )
    assert payload["connection_hub_delegated_admission_schema"] == (
        "connection_hub.delegated_admission.v1"
    )

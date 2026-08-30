from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import (
    load_dynamic_module_for_path,
)
from connection_hub.authority_registry import CredentialEnvelope
from connection_hub.delegated_credentials.admission import (
    AdmissionRequest,
    sign_admission_request,
)
from connection_hub.delegated_credentials.oauth.surface_policy import SurfacePolicyDecision


SERVICE_SECRET = "service-secret-with-at-least-thirty-two-bytes"
PROJECTION_SECRET = "projection-secret-with-at-least-thirty-two-bytes"
RESOURCE = "https://service.example/customers"


def _load_entrypoint_module():
    bundle_root = Path(__file__).resolve().parents[1]
    _module_name, module = load_dynamic_module_for_path(bundle_root / "entrypoint.py")
    return module


class _Redis:
    def __init__(self) -> None:
        self.keys: set[str] = set()

    async def set(self, key, value, *, ex, nx):
        del value, ex
        assert nx is True
        if key in self.keys:
            return None
        self.keys.add(key)
        return True


def _connections() -> dict:
    return {
        "delegated_credentials": {
            "admission": {
                "enabled": True,
                "identity_projection_secret_ref": "admission.subject_secret",
                "services": {
                    "crm-api": {
                        "secret_ref": "admission.crm-api.secret",
                        "resources": [RESOURCE],
                    }
                },
            }
        }
    }


async def _secret(path: str, scope: str) -> str:
    del scope
    if path == "admission.crm-api.secret":
        return SERVICE_SECRET
    if path == "admission.subject_secret":
        return PROJECTION_SECRET
    return ""


def _request(
    payload: dict,
    *,
    nonce: str = "nonce-1234567890abcd",
    signing_secret: str = SERVICE_SECRET,
) -> Request:
    semantic = AdmissionRequest.from_mapping(payload)
    timestamp = str(int(time.time()))
    delegated_token = "kst1.delegated-token"
    signature = sign_admission_request(
        secret=signing_secret,
        service_id="crm-api",
        timestamp=timestamp,
        nonce=nonce,
        delegated_token=delegated_token,
        request=semantic,
    )
    headers = [
        (b"authorization", f"Bearer {delegated_token}".encode()),
        (b"x-connection-hub-service-id", b"crm-api"),
        (b"x-connection-hub-timestamp", timestamp.encode()),
        (b"x-connection-hub-nonce", nonce.encode()),
        (b"x-connection-hub-signature", signature.encode()),
    ]
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/public/delegated_admission",
            "raw_path": b"",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("runtime.example.test", 443),
        }
    )


def _allowed_result(*, account_scope: dict | None = None):
    envelope = CredentialEnvelope(
        credential_kind="delegated_client_access",
        issuer_authority_id="delegated_client",
        subject="integration:client:user-1",
        attrs={
            "client_id": "external-client",
            "grantor_subject": "user-1",
            "resource_grants": {RESOURCE: ["crm:read"]},
            "account_scope": account_scope or {},
        },
    )
    return SimpleNamespace(
        denial=None,
        envelope=envelope,
        grant_record={
            "registry_access_id": "access-1",
            "card_revision": 4,
            "catalog_version": "catalog-card",
            "expires_at": 2000000000,
            "account_scope": account_scope or {},
        },
        runtime={"grantor_user_id": "user-1"},
        decision=SurfacePolicyDecision.allow(
            matched_resource=RESOURCE,
            available_grants=("crm:read",),
            granted_operations=("customers.search",),
        ),
        catalog=SimpleNamespace(version="catalog-active"),
    )


@pytest.mark.asyncio
async def test_direct_admission_returns_bounded_pairwise_projection(monkeypatch):
    module = _load_entrypoint_module()
    surface = sys.modules[module.handle_delegated_admission.__module__]
    calls = []

    async def _evaluate(**kwargs):
        calls.append(kwargs)
        return _allowed_result()

    monkeypatch.setattr(surface, "evaluate_delegated_rest_admission", _evaluate)
    redis = _Redis()
    payload = {"resource": RESOURCE, "operation": "customers.search"}
    response = await module.handle_delegated_admission(
        context=module.AdmissionHostContext(
            connections=_connections(),
            redis=redis,
            tenant="tenant-a",
            project="project-a",
            resolve_secret=_secret,
            bind_delegated_request=lambda request: None,
        ),
        payload=payload,
        request=_request(payload),
    )
    body = json.loads(response.body)

    assert response.status_code == 200
    assert body["allowed"] is True
    assert body["principal"]["sub"].startswith("prk_sub_")
    assert body["principal"]["client_id"] == "external-client"
    assert "user-1" not in str(body)
    assert "access-1" not in str(body)
    assert body["authority"]["operation"] == "customers.search"
    assert body["provenance"] == {
        "card_revision": 4,
        "card_catalog_version": "catalog-card",
        "active_catalog_version": "catalog-active",
    }
    assert calls[0]["request_resource"] == RESOURCE
    assert calls[0]["operation"] == "customers.search"
    assert calls[0]["log_identity_details"] is False


@pytest.mark.asyncio
async def test_direct_admission_rejects_a_replayed_service_proof(monkeypatch):
    module = _load_entrypoint_module()
    surface = sys.modules[module.handle_delegated_admission.__module__]

    async def _evaluate(**kwargs):
        del kwargs
        return _allowed_result()

    monkeypatch.setattr(surface, "evaluate_delegated_rest_admission", _evaluate)
    redis = _Redis()
    context = module.AdmissionHostContext(
        connections=_connections(),
        redis=redis,
        tenant="tenant-a",
        project="project-a",
        resolve_secret=_secret,
        bind_delegated_request=lambda request: None,
    )
    payload = {"resource": RESOURCE, "operation": "customers.search"}
    request = _request(payload)

    first = await module.handle_delegated_admission(
        context=context, payload=payload, request=request
    )
    second = await module.handle_delegated_admission(
        context=context, payload=payload, request=request
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert json.loads(second.body)["error"]["code"] == "admission_request_replayed"


@pytest.mark.asyncio
async def test_service_registration_bounds_the_resource_before_token_policy(monkeypatch):
    module = _load_entrypoint_module()
    surface = sys.modules[module.handle_delegated_admission.__module__]

    async def _must_not_evaluate(**kwargs):
        raise AssertionError(f"bearer policy should not be queried: {kwargs}")

    monkeypatch.setattr(
        surface,
        "evaluate_delegated_rest_admission",
        _must_not_evaluate,
    )
    payload = {
        "resource": "https://other.example/customers",
        "operation": "customers.search",
    }
    response = await module.handle_delegated_admission(
        context=module.AdmissionHostContext(
            connections=_connections(),
            redis=_Redis(),
            tenant="tenant-a",
            project="project-a",
            resolve_secret=_secret,
            bind_delegated_request=lambda request: None,
        ),
        payload=payload,
        request=_request(payload),
    )

    assert response.status_code == 403
    assert json.loads(response.body)["error"]["code"] == (
        "service_resource_not_registered"
    )


@pytest.mark.asyncio
async def test_invalid_service_signature_is_rejected_before_bearer_policy(monkeypatch):
    module = _load_entrypoint_module()
    surface = sys.modules[module.handle_delegated_admission.__module__]

    async def _must_not_evaluate(**kwargs):
        raise AssertionError(f"bearer policy should not be queried: {kwargs}")

    monkeypatch.setattr(
        surface,
        "evaluate_delegated_rest_admission",
        _must_not_evaluate,
    )
    payload = {"resource": RESOURCE, "operation": "customers.search"}
    response = await module.handle_delegated_admission(
        context=module.AdmissionHostContext(
            connections=_connections(),
            redis=_Redis(),
            tenant="tenant-a",
            project="project-a",
            resolve_secret=_secret,
            bind_delegated_request=lambda request: None,
        ),
        payload=payload,
        request=_request(payload, signing_secret="different-secret-with-32-bytes-minimum"),
    )

    assert response.status_code == 401
    assert json.loads(response.body)["error"]["code"] == (
        "service_authentication_failed"
    )


@pytest.mark.asyncio
async def test_missing_service_secret_fails_closed_as_unavailable(monkeypatch):
    module = _load_entrypoint_module()
    surface = sys.modules[module.handle_delegated_admission.__module__]

    async def _must_not_evaluate(**kwargs):
        raise AssertionError(f"bearer policy should not be queried: {kwargs}")

    async def _missing_secret(path: str, scope: str) -> str:
        del path, scope
        return ""

    monkeypatch.setattr(
        surface,
        "evaluate_delegated_rest_admission",
        _must_not_evaluate,
    )
    payload = {"resource": RESOURCE, "operation": "customers.search"}
    response = await module.handle_delegated_admission(
        context=module.AdmissionHostContext(
            connections=_connections(),
            redis=_Redis(),
            tenant="tenant-a",
            project="project-a",
            resolve_secret=_missing_secret,
            bind_delegated_request=lambda request: None,
        ),
        payload=payload,
        request=_request(payload),
    )

    assert response.status_code == 503
    body = json.loads(response.body)
    assert body["error"]["code"] == "service_authentication_unavailable"
    assert body["error"]["retryable"] is True


@pytest.mark.asyncio
async def test_direct_admission_narrows_a_requested_connected_account(monkeypatch):
    module = _load_entrypoint_module()
    surface = sys.modules[module.handle_delegated_admission.__module__]

    async def _evaluate(**kwargs):
        del kwargs
        return _allowed_result(
            account_scope={
                "salesforce": {"account-17": ["contacts:read"]},
            }
        )

    monkeypatch.setattr(surface, "evaluate_delegated_rest_admission", _evaluate)
    payload = {
        "resource": RESOURCE,
        "operation": "customers.search",
        "account": {
            "provider_id": "salesforce",
            "account_id": "account-17",
            "claims": ["contacts:read"],
        },
    }
    response = await module.handle_delegated_admission(
        context=module.AdmissionHostContext(
            connections=_connections(),
            redis=_Redis(),
            tenant="tenant-a",
            project="project-a",
            resolve_secret=_secret,
            bind_delegated_request=lambda request: None,
        ),
        payload=payload,
        request=_request(payload),
    )

    assert response.status_code == 200
    assert json.loads(response.body)["authority"]["account_scope"] == {
        "provider_id": "salesforce",
        "account_id": "account-17",
        "claims": ["contacts:read"],
    }

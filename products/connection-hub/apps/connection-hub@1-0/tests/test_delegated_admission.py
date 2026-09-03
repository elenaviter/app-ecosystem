from __future__ import annotations

import json
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.responses import JSONResponse
from starlette.requests import Request

from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import (
    load_dynamic_module_for_path,
)
from connection_hub.authority_registry import CredentialEnvelope
from connection_hub.delegated_credentials.admission import (
    AdmissionRequest,
    sign_admission_request,
)
from connection_hub.delegated_credentials.oauth.surface_policy import (
    SurfacePolicyDecision,
    SurfacePolicyDenial,
)
from connection_hub.delegated_credentials.request_approval import (
    verify_request_approval_ticket,
)
from connection_hub.invocation_policy import (
    POLICY_ONCE,
    SURFACE_OUTER,
    BundleStorageInvocationPolicyStore,
    InvocationAuthority,
    InvocationPolicyService,
    canonical_request_digest,
)


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


@asynccontextmanager
async def _lock(**_kwargs):
    yield {}


def _connections(*, request_bound: bool = False) -> dict:
    service = {
        "secret_ref": "admission.crm-api.secret",
        "resources": [RESOURCE],
    }
    if request_bound:
        service.update(
            {
                "request_bound_operations": ["customers.search"],
                "request_permit_ttl_seconds": 600,
            }
        )
    return {
        "delegated_credentials": {
            "admission": {
                "enabled": True,
                "identity_projection_secret_ref": "admission.subject_secret",
                "services": {
                    "crm-api": service,
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
    assert body["principal"]["client_id"].startswith("prk_client_")
    assert "external-client" not in str(body)
    assert "user-1" not in str(body)
    assert body["authority"]["operation"] == "customers.search"
    assert body["provenance"] == {
        "access_id": "access-1",
        "card_revision": 4,
        "card_catalog_version": "catalog-card",
        "active_catalog_version": "catalog-active",
    }
    assert calls[0]["request_resource"] == RESOURCE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("denial_code", "include_requested_capability"),
    [
        ("delegated_capability_not_granted", True),
        ("operation_not_consented", False),
    ],
)
async def test_direct_ungranted_operation_returns_exact_once_or_always_recovery(
    monkeypatch,
    denial_code,
    include_requested_capability,
):
    module = _load_entrypoint_module()
    surface = sys.modules[module.handle_delegated_admission.__module__]
    base = _allowed_result()
    denial_ret = {"reason": denial_code}
    if include_requested_capability:
        denial_ret["requested_capability"] = {
            "kind": "outer_operation",
            "request_resource": RESOURCE,
            "outer_operation": "customers.delete",
        }
    base.denial = JSONResponse(
        status_code=403,
        content={
            "ok": False,
            "error": {
                "code": (
                    denial_code
                    if include_requested_capability
                    else "forbidden"
                ),
                "message": "This delegated card does not grant that operation.",
            },
            "ret": denial_ret,
        },
    )
    base.runtime = None
    if denial_code == "operation_not_consented":
        base.decision = SurfacePolicyDecision.deny(
            SurfacePolicyDenial(
                reason="operation_not_consented",
                description=(
                    "operation not consented for this connection: "
                    "customers.delete"
                ),
            ),
            matched_resource=RESOURCE,
        )

    async def _evaluate(**_kwargs):
        return base

    monkeypatch.setattr(surface, "evaluate_delegated_rest_admission", _evaluate)
    recovery_calls = []

    def recovery(view, admission, change_id):
        recovery_calls.append(
            (view.registry_access_id, admission.operation, change_id)
        )
        return "https://hub.example/grant?invocation_policy=choose"

    payload = {
        "resource": RESOURCE,
        "operation": "customers.delete",
        "invocation_id": "invoke-delete-1",
        "request_digest": canonical_request_digest(
            {"customer_id": "customer-17"}
        ),
    }
    response = await module.handle_delegated_admission(
        context=module.AdmissionHostContext(
            connections=_connections(),
            redis=_Redis(),
            tenant="tenant-a",
            project="project-a",
            resolve_secret=_secret,
            bind_delegated_request=lambda request: None,
            operation_grant_url_builder=recovery,
        ),
        payload=payload,
        request=_request(payload),
    )
    body = json.loads(response.body)

    assert response.status_code == 403
    assert body["error"]["code"] == denial_code
    consent = body["consent"]
    assert consent["agent_client_id"] == "external-client"
    assert consent["access_id"] == "access-1"
    assert consent["resource"] == RESOURCE
    assert consent["outer_operation"] == "customers.delete"
    assert consent["available_choices"] == ["allow_once", "allow_always"]
    assert consent["invocation_change_id"] == "invoke-delete-1"
    assert consent["grant"]["payload"] == {
        "client_id": "external-client",
        "access_id": "access-1",
        "resource": RESOURCE,
        "claims": [],
        "resource_operations": {RESOURCE: ["customers.delete"]},
        "invocation_change_id": "invoke-delete-1",
    }
    assert body["ret"]["details"]["recovery"] == consent
    assert recovery_calls == [
        ("access-1", "customers.delete", "invoke-delete-1")
    ]


@pytest.mark.asyncio
async def test_request_bound_ungranted_operation_returns_signed_exact_recovery(
    monkeypatch,
):
    module = _load_entrypoint_module()
    surface = sys.modules[module.handle_delegated_admission.__module__]
    denied_result = _allowed_result()
    denied_result.denial = JSONResponse(
        status_code=403,
        content={
            "ok": False,
            "error": {
                "code": "forbidden",
                "message": "This delegated card does not grant that operation.",
            },
            "ret": {"reason": "operation_not_consented"},
        },
    )
    denied_result.decision = SurfacePolicyDecision.deny(
        SurfacePolicyDenial(
            reason="operation_not_consented",
            description="operation not consented for this connection",
            missing_grants=frozenset({"crm:write"}),
        ),
        matched_resource=RESOURCE,
    )

    async def _evaluate(**_kwargs):
        return denied_result

    monkeypatch.setattr(surface, "evaluate_delegated_rest_admission", _evaluate)
    approval_tokens: list[str] = []

    def _recovery_url(
        _view,
        _request,
        _card_revision,
        _catalog_version,
        _ttl,
        approval_ticket,
    ):
        approval_tokens.append(approval_ticket)
        return (
            "https://hub.example/approve?request_approval_ticket="
            f"{approval_ticket}"
        )

    digest = canonical_request_digest({"application_id": "app-a@1-0"})
    payload = {
        "resource": RESOURCE,
        "operation": "customers.search",
        "invocation_id": "search-1",
        "request_digest": digest,
        "approval_context": {"application_id": "app-a@1-0"},
    }
    response = await module.handle_delegated_admission(
        context=module.AdmissionHostContext(
            connections=_connections(request_bound=True),
            redis=_Redis(),
            tenant="tenant-a",
            project="project-a",
            resolve_secret=_secret,
            bind_delegated_request=lambda request: None,
            request_permit_recovery_url_builder=_recovery_url,
        ),
        payload=payload,
        request=_request(payload, nonce="nonce-1234567890abc3"),
    )

    body = json.loads(response.body)
    ticket = verify_request_approval_ticket(
        approval_tokens[0],
        secret=SERVICE_SECRET,
    )
    assert response.status_code == 403
    assert body["consent"]["kind"] == "delegated_request_permit"
    assert body["consent"]["invocation_id"] == "search-1"
    assert body["consent"]["request_digest"] == digest
    assert body["consent"]["expires_at"] == ticket.expires_at
    assert body["consent"]["claims"] == ["crm:write"]
    assert body["consent"]["grant"]["payload"]["claims"] == ["crm:write"]
    assert ticket.approval_context == {"application_id": "app-a@1-0"}


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


@pytest.mark.asyncio
async def test_direct_admission_once_replays_the_recorded_allow(monkeypatch, tmp_path):
    module = _load_entrypoint_module()
    surface = sys.modules[module.handle_delegated_admission.__module__]
    evaluations = 0

    async def _evaluate(**kwargs):
        nonlocal evaluations
        del kwargs
        evaluations += 1
        return _allowed_result()

    monkeypatch.setattr(surface, "evaluate_delegated_rest_admission", _evaluate)
    policies = InvocationPolicyService(
        store=BundleStorageInvocationPolicyStore(tmp_path),
        mutation_lock=_lock,
    )
    authority = InvocationAuthority(
        access_id="access-1",
        resource=RESOURCE,
        surface=SURFACE_OUTER,
        operation="customers.search",
    )
    await policies.set_policy(
        owner_subject="user-1",
        authority=authority,
        mode=POLICY_ONCE,
        now=100,
    )
    context = module.AdmissionHostContext(
        connections=_connections(),
        redis=_Redis(),
        tenant="tenant-a",
        project="project-a",
        resolve_secret=_secret,
        bind_delegated_request=lambda request: None,
        invocation_policies=policies,
        invocation_recovery_url_builder=lambda view, request: (
            f"https://hub.example/cards/{view.registry_access_id}"
            f"?resource={request.resource}&operation={request.operation}"
        ),
    )
    digest = canonical_request_digest({"customer_id": "customer-7"})
    payload = {
        "resource": RESOURCE,
        "operation": "customers.search",
        "invocation_id": "invoke-1",
        "request_digest": digest,
    }

    first = await module.handle_delegated_admission(
        context=context,
        payload=payload,
        request=_request(payload, nonce="nonce-1234567890abc1"),
    )
    replay = await module.handle_delegated_admission(
        context=context,
        payload=payload,
        request=_request(payload, nonce="nonce-1234567890abc2"),
    )
    next_payload = {
        **payload,
        "invocation_id": "invoke-2",
        "request_digest": canonical_request_digest({"customer_id": "customer-8"}),
    }
    exhausted = await module.handle_delegated_admission(
        context=context,
        payload=next_payload,
        request=_request(next_payload, nonce="nonce-1234567890abc3"),
    )

    first_body = json.loads(first.body)
    replay_body = json.loads(replay.body)
    exhausted_body = json.loads(exhausted.body)
    assert first.status_code == 200
    assert first_body["invocation_id"] == "invoke-1"
    assert first_body["replay"] is False
    assert first_body["invocation_policy"]["remaining"] == 0
    assert set(first_body["invocation_policy"]) == {
        "mode",
        "revision",
        "state",
        "remaining",
        "updated_at",
    }
    assert first_body["provenance"]["access_id"] == "access-1"
    assert replay.status_code == 200
    assert replay_body["decision_id"] == first_body["decision_id"]
    assert replay_body["replay"] is True
    assert exhausted.status_code == 403
    assert exhausted_body["error"]["code"] == (
        "delegated_invocation_limit_exhausted"
    )
    assert exhausted_body["ret"]["details"]["recovery"] == {
        "kind": "delegated_invocation_policy",
        "connection_hub_url": (
            "https://hub.example/cards/access-1"
            f"?resource={RESOURCE}&operation=customers.search"
        ),
        "access_id": "access-1",
        "resource": RESOURCE,
        "outer_operation": "customers.search",
        "available_choices": ["allow_once", "allow_always"],
    }
    # Live authority is intentionally re-evaluated before replay is served.
    assert evaluations == 3


@pytest.mark.asyncio
async def test_request_bound_direct_admission_accepts_only_exact_browser_permit(
    monkeypatch,
    tmp_path,
):
    module = _load_entrypoint_module()
    surface = sys.modules[module.handle_delegated_admission.__module__]

    async def _evaluate(**kwargs):
        del kwargs
        return _allowed_result()

    monkeypatch.setattr(surface, "evaluate_delegated_rest_admission", _evaluate)
    policies = InvocationPolicyService(
        store=BundleStorageInvocationPolicyStore(tmp_path),
        mutation_lock=_lock,
    )
    authority = InvocationAuthority(
        access_id="access-1",
        resource=RESOURCE,
        surface=SURFACE_OUTER,
        operation="customers.search",
    )
    await policies.set_policy(
        owner_subject="user-1",
        authority=authority,
        mode=POLICY_ONCE,
    )
    approval_tickets: list[str] = []

    def _recovery_url(
        _view,
        _request,
        _card_revision,
        _catalog_version,
        _ttl,
        approval_ticket,
    ):
        approval_tickets.append(approval_ticket)
        return "https://hub.example/approve-exact-request"

    context = module.AdmissionHostContext(
        connections=_connections(request_bound=True),
        redis=_Redis(),
        tenant="tenant-a",
        project="project-a",
        resolve_secret=_secret,
        bind_delegated_request=lambda request: None,
        invocation_policies=policies,
        request_permit_recovery_url_builder=_recovery_url,
    )
    digest = canonical_request_digest({"application_id": "app-a@1-0"})
    payload = {
        "resource": RESOURCE,
        "operation": "customers.search",
        "invocation_id": "approved-request",
        "request_digest": digest,
        "approval_context": {"application_id": "app-a@1-0"},
    }
    denied = await module.handle_delegated_admission(
        context=context,
        payload=payload,
        request=_request(payload, nonce="nonce-1234567890abc4"),
    )
    competing_payload = {
        **payload,
        "invocation_id": "competing-request",
        "request_digest": canonical_request_digest(
            {"application_id": "app-b@1-0"}
        ),
        "approval_context": {"application_id": "app-b@1-0"},
    }
    competing = await module.handle_delegated_admission(
        context=context,
        payload=competing_payload,
        request=_request(competing_payload, nonce="nonce-1234567890abc5"),
    )
    await policies.issue_request_permit(
        owner_subject="user-1",
        authority=authority,
        invocation_id="approved-request",
        request_digest=digest,
        card_revision=4,
        authority_revision="catalog-active",
    )
    approved = await module.handle_delegated_admission(
        context=context,
        payload=payload,
        request=_request(payload, nonce="nonce-1234567890abc6"),
    )

    denied_body = json.loads(denied.body)
    competing_body = json.loads(competing.body)
    approved_body = json.loads(approved.body)
    assert denied.status_code == 403
    assert denied_body["error"]["code"] == "delegated_request_permit_required"
    assert denied_body["consent"]["invocation_id"] == "approved-request"
    assert denied_body["consent"]["request_digest"] == digest
    assert denied_body["consent"]["approval_context"] == {
        "application_id": "app-a@1-0"
    }
    ticket = verify_request_approval_ticket(
        approval_tickets[0],
        secret=SERVICE_SECRET,
    )
    assert ticket.invocation_id == "approved-request"
    assert ticket.request_digest == digest
    assert ticket.card_revision == 4
    assert ticket.authority_revision == "catalog-active"
    assert ticket.approval_context == {"application_id": "app-a@1-0"}
    assert denied_body["consent"]["expires_at"] == ticket.expires_at
    assert "permit_ttl_seconds" not in denied_body["consent"]
    assert competing.status_code == 403
    assert competing_body["error"]["code"] == "delegated_request_permit_required"
    assert approved.status_code == 200
    assert approved_body["request_permit"]["state"] == "consumed"
    assert approved_body["provenance"]["request_permit_revision"] == 1

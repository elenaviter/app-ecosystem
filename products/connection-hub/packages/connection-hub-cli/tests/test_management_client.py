from __future__ import annotations

import hashlib
import json

import pytest

from connection_hub_cli.errors import AuthorizationError
from connection_hub_cli.management import (
    APPLICATION_RELOAD,
    DEPLOYMENT_INSPECT,
    ConsentRecovery,
    HttpxManagementTransport,
    ManagementClient,
    ManagementDenial,
    ManagementRequest,
    ManagementResult,
    ManagementTarget,
)


def _target() -> ManagementTarget:
    return ManagementTarget.create(
        public_base_url="https://runtime.example.test",
        tenant="demo-tenant",
        project="demo-project",
    )


def _success(request: ManagementRequest, result: dict | None = None) -> dict:
    if result is None:
        if request.operation == DEPLOYMENT_INSPECT:
            result = {
                "platform_release": "2026.09.01.110",
                "readiness": "ready",
                "applications": [],
            }
        else:
            result = {}
    return {
        "schema": "kdcube.management.result.v1",
        "ok": True,
        "operation": request.operation,
        "resource": request.target.resource,
        "target": {
            "tenant": request.target.tenant,
            "project": request.target.project,
        },
        "invocation": {"id": request.invocation_id, "replay": False},
        "authority": {
            "decision_id": "decision-1",
            "access_id": "access-1",
            "card_revision": 4,
        },
        "result": result,
    }


def _denial(request: ManagementRequest, *, recovery: dict | None = None) -> dict:
    value = {
        "schema": "kdcube.management.error.v1",
        "ok": False,
        "operation": request.operation,
        "resource": request.target.resource,
        "target": {
            "tenant": request.target.tenant,
            "project": request.target.project,
        },
        "invocation_id": request.invocation_id,
        "error": {
            "code": "delegated_request_permit_required",
            "message": "Approval is required.",
            "retryable": False,
        },
    }
    if recovery is not None:
        value["recovery"] = recovery
    return value


def _recovery(request: ManagementRequest) -> dict:
    return {
        "type": "consent_required",
        "reason": "delegated_request_permit_required",
        "authorization_url": (
            "https://runtime.example.test/api/integrations/bundles/demo-tenant/"
            "demo-project/connection-hub@1-0/widgets/"
            "connections_settings?request=opaque"
        ),
        "access_id": "access-1",
        "resource": request.target.resource,
        "operation": request.operation,
        "application_id": request.application_id,
        "invocation_id": request.invocation_id,
        "request_digest": request.request_digest,
        "card_revision": 4,
        "catalog_version": "catalog-7",
        "expires_at": 1_788_380_000,
        "choices": ["allow_once", "allow_always"],
    }


def test_target_and_requests_match_the_frozen_protocol() -> None:
    target = _target()
    inspect = ManagementRequest.inspect(target, invocation_id="inspect-1")
    surfaces = ManagementRequest.surfaces(
        target,
        application_id="connection-hub@1-0",
        invocation_id="surfaces-1",
    )
    reload_request = ManagementRequest.reload(
        target,
        application_id="connection-hub@1-0",
        invocation_id="reload-1",
    )

    assert target.resource == (
        "urn:kdcube:management:deployment:demo-tenant:demo-project"
    )
    assert target.protected_resource_metadata_url.endswith(
        "/api/integrations/management/v1/.well-known/oauth-protected-resource"
    )
    assert inspect.operation == DEPLOYMENT_INSPECT
    assert surfaces.path.endswith("/applications/connection-hub%401-0/surfaces")
    assert reload_request.path.endswith("/applications/connection-hub%401-0/reload")
    assert reload_request.body == {}
    assert reload_request.operation == APPLICATION_RELOAD

    encoded = json.dumps(
        reload_request.canonical_payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    assert reload_request.request_digest == hashlib.sha256(encoded).hexdigest()


@pytest.mark.parametrize(
    "application_id",
    [
        "",
        "*",
        "../workspace",
        "https://runtime.example/app",
        "bad\\path",
        "app?query",
        "app#fragment",
        "two apps",
    ],
)
def test_request_rejects_non_exact_application_identifiers(application_id: str) -> None:
    with pytest.raises(AuthorizationError):
        ManagementRequest.reload(_target(), application_id=application_id)


def test_success_and_consent_are_bound_to_the_original_request() -> None:
    request = ManagementRequest.reload(
        _target(),
        application_id="connection-hub@1-0",
        invocation_id="reload-1",
    )
    result = ManagementResult.from_mapping(
        _success(
            request,
            {
                "application_id": "connection-hub@1-0",
                "state": "completed",
                "changed_application_ids": ["connection-hub@1-0"],
                "generation": "generation-2",
            },
        ),
        request=request,
    )
    recovery = ConsentRecovery.from_mapping(_recovery(request), request=request)

    assert result.invocation_id == "reload-1"
    assert result.replay is False
    assert recovery.request_digest == request.request_digest
    assert recovery.choices == ("allow_once", "allow_always")


def test_consent_rejects_changed_request_and_foreign_origin() -> None:
    request = ManagementRequest.reload(
        _target(),
        application_id="connection-hub@1-0",
        invocation_id="reload-1",
    )
    changed = _recovery(request)
    changed["request_digest"] = "0" * 64
    with pytest.raises(AuthorizationError) as digest:
        ConsentRecovery.from_mapping(changed, request=request)
    assert digest.value.code == "management_recovery_request_mismatch"

    foreign = _recovery(request)
    foreign["authorization_url"] = "https://attacker.example/approve"
    with pytest.raises(AuthorizationError) as origin:
        ConsentRecovery.from_mapping(foreign, request=request)
    assert origin.value.code == "management_recovery_origin_mismatch"

    wrong_path = _recovery(request)
    wrong_path["authorization_url"] = (
        "https://runtime.example.test/api/integrations/management/v1/deployment"
    )
    with pytest.raises(AuthorizationError) as path:
        ConsentRecovery.from_mapping(wrong_path, request=request)
    assert path.value.code == "management_recovery_path_mismatch"

    wrong_reason = _recovery(request)
    wrong_reason["reason"] = "delegated_operation_not_granted"
    with pytest.raises(AuthorizationError) as reason:
        ConsentRecovery.from_mapping(wrong_reason, request=request)
    assert reason.value.code == "management_recovery_invalid"

    relative_expiry = _recovery(request)
    relative_expiry.pop("expires_at")
    relative_expiry["permit_ttl_seconds"] = 600
    with pytest.raises(AuthorizationError) as expiry:
        ConsentRecovery.from_mapping(relative_expiry, request=request)
    assert expiry.value.code == "management_value_invalid"


def test_success_projection_drops_unknown_internal_values() -> None:
    marker = "must-not-reach-cli-output"
    request = ManagementRequest.inspect(_target(), invocation_id="inspect-1")
    payload = _success(
        request,
        {
            "platform_release": "2026.09.01.110",
            "readiness": "ready",
            "applications": [
                {
                    "application_id": "connection-hub@1-0",
                    "declared": True,
                    "preparation_state": "ready",
                    "generation": "generation-1",
                    "readiness_required": True,
                    "raw_error": marker,
                }
            ],
            "descriptor_secrets": marker,
        },
    )
    payload["authority"]["bearer"] = marker
    payload["internal_environment"] = marker

    result = ManagementResult.from_mapping(payload, request=request)
    serialized = json.dumps(
        {
            "authority": dict(result.authority),
            "result": dict(result.result),
        }
    )

    assert marker not in serialized


class _Transport:
    def __init__(self, status: int, payload: dict) -> None:
        self.status = status
        self.payload = payload
        self.calls: list[tuple[ManagementRequest, str]] = []

    async def execute(self, request: ManagementRequest, bearer: str):
        self.calls.append((request, bearer))
        return self.status, self.payload


@pytest.mark.asyncio
async def test_management_client_returns_typed_success_and_denial() -> None:
    request = ManagementRequest.inspect(_target(), invocation_id="inspect-1")
    success_transport = _Transport(200, _success(request))
    success = await ManagementClient(transport=success_transport).execute(
        request,
        bearer="secret-bearer",
    )
    assert isinstance(success, ManagementResult)

    denied_transport = _Transport(403, _denial(request))
    denied = await ManagementClient(transport=denied_transport).execute(
        request,
        bearer="secret-bearer",
    )
    assert isinstance(denied, ManagementDenial)
    assert denied.code == "delegated_request_permit_required"


@pytest.mark.asyncio
async def test_http_transport_sends_exact_headers_and_empty_reload_body() -> None:
    import httpx2

    request = ManagementRequest.reload(
        _target(),
        application_id="connection-hub@1-0",
        invocation_id="reload-1",
    )
    observed = {}

    def handler(incoming):
        observed["method"] = incoming.method
        observed["path"] = incoming.url.raw_path.decode()
        observed["authorization"] = incoming.headers["authorization"]
        observed["idempotency"] = incoming.headers["idempotency-key"]
        observed["body"] = json.loads(incoming.content)
        return httpx2.Response(200, json=_success(request))

    transport = HttpxManagementTransport(transport=httpx2.MockTransport(handler))
    status, _payload = await transport.execute(request, "secret-bearer")

    assert status == 200
    assert observed == {
        "method": "POST",
        "path": (
            "/api/integrations/management/v1/applications/connection-hub%401-0/reload"
        ),
        "authorization": "Bearer secret-bearer",
        "idempotency": "reload-1",
        "body": {},
    }


@pytest.mark.asyncio
async def test_http_transport_rejects_redirect_without_leaking_bearer() -> None:
    import httpx2

    marker = "secret-bearer-marker"
    request = ManagementRequest.inspect(_target(), invocation_id="inspect-1")

    def handler(_incoming):
        return httpx2.Response(
            302,
            headers={"location": "https://attacker.example/capture"},
        )

    transport = HttpxManagementTransport(transport=httpx2.MockTransport(handler))
    with pytest.raises(AuthorizationError) as raised:
        await transport.execute(request, marker)
    assert raised.value.code == "management_redirect_rejected"
    assert marker not in str(raised.value)
    assert raised.value.__cause__ is None


@pytest.mark.asyncio
async def test_http_transport_does_not_chain_backend_secret() -> None:
    import httpx2

    marker = "management-transport-secret-marker"
    request = ManagementRequest.inspect(_target(), invocation_id="inspect-1")

    def handler(_incoming):
        raise RuntimeError(marker)

    transport = HttpxManagementTransport(transport=httpx2.MockTransport(handler))
    with pytest.raises(AuthorizationError) as raised:
        await transport.execute(request, marker)
    assert marker not in str(raised.value)
    assert raised.value.__cause__ is None

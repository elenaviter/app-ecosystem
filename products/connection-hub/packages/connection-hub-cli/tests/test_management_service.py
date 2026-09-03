from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from connection_hub_cli.authorization.models import OAuthTokenSet
from connection_hub_cli.authorization.session import (
    OAuthSessionRecord,
    session_id_for_target,
)
from connection_hub_cli.errors import AuthorizationError
from connection_hub_cli.management.models import (
    ConsentRecovery,
    ManagementDenial,
    ManagementRequest,
    ManagementResult,
    ManagementTarget,
)
from connection_hub_cli.management.service import AuthorizedManagementService


def _target() -> ManagementTarget:
    return ManagementTarget.create(
        public_base_url="https://runtime.example.test",
        tenant="demo-tenant",
        project="demo-project",
        session_target_key="local:/runtime/demo:demo-tenant:demo-project",
    )


def _record() -> OAuthSessionRecord:
    return OAuthSessionRecord.create(
        target_key="local:/runtime/demo:demo-tenant:demo-project",
        resource_metadata_url=_target().protected_resource_metadata_url,
        resource=_target().resource,
        issuer="https://runtime.example.test/oauth",
        token_endpoint="https://runtime.example.test/oauth/token",
        revocation_endpoint="https://runtime.example.test/oauth/revoke",
        client_id="native-client",
        scope="",
        token=_token("old-access", expires_at=100),
    )


def _token(access: str, *, expires_at: int) -> OAuthTokenSet:
    return OAuthTokenSet(
        access_token=access,
        refresh_token="refresh-secret",
        expires_at=expires_at,
        access_id="access_cli",
    )


class _Sessions:
    def __init__(self, *, expiring: bool) -> None:
        self.expiring = expiring
        self.record = _record()
        self.token = _token(
            "old-access",
            expires_at=100 if expiring else int(time.time()) + 3600,
        )
        self.session_ids: list[str] = []
        self.removed: list[str] = []

    async def refresh_if_expiring(self, session_id: str, *, refresher, **_kwargs):
        self.session_ids.append(session_id)
        if self.expiring:
            self.token = await refresher(self.record, self.token)
        return self.record, self.token

    def load(self, session_id: str):
        self.session_ids.append(session_id)
        return self.record, self.token

    def remove(self, session_id: str):
        self.removed.append(session_id)
        return self.record


class _Discovery:
    async def discover(self, **_kwargs):
        return SimpleNamespace(
            authorization_server=SimpleNamespace(
                issuer="https://runtime.example.test/oauth",
                token_endpoint="https://runtime.example.test/oauth/token",
                revocation_endpoint="https://runtime.example.test/oauth/revoke",
                supports_refresh=True,
            )
        )


class _OAuth:
    def __init__(self) -> None:
        self.calls = []
        self.revocations = []
        self.revoke_error: AuthorizationError | None = None

    async def refresh(self, **kwargs):
        self.calls.append(kwargs)
        return _token("new-access", expires_at=int(time.time()) + 3600)

    async def revoke(self, **kwargs):
        self.revocations.append(kwargs)
        if self.revoke_error is not None:
            raise self.revoke_error


class _Management:
    def __init__(self, results) -> None:
        self.results = list(results)
        self.calls = []

    async def execute(self, request, *, bearer):
        self.calls.append((request, bearer))
        return self.results.pop(0)


def _result(request: ManagementRequest) -> ManagementResult:
    return ManagementResult(
        operation=request.operation,
        resource=request.target.resource,
        invocation_id=request.invocation_id,
        replay=False,
        authority={},
        result={
            "platform_release": "2026.09.01.110",
            "readiness": "ready",
            "applications": [],
        },
    )


@pytest.mark.asyncio
async def test_service_refreshes_inside_session_boundary_before_call() -> None:
    request = ManagementRequest.inspect(_target(), invocation_id="inspect-1")
    sessions = _Sessions(expiring=True)
    oauth = _OAuth()
    management = _Management([_result(request)])
    service = AuthorizedManagementService(
        sessions=sessions,
        discovery=_Discovery(),
        oauth=oauth,
        management=management,
    )

    result = await service.execute(request)

    assert isinstance(result, ManagementResult)
    assert management.calls == [(request, "new-access")]
    assert oauth.calls[0]["refresh_token"] == "refresh-secret"
    assert sessions.session_ids == [session_id_for_target(request.target_key)]


@pytest.mark.asyncio
async def test_consent_retry_reuses_the_exact_request_object() -> None:
    request = ManagementRequest.reload(
        _target(),
        application_id="connection-hub@1-0",
        invocation_id="reload-1",
    )
    recovery = ConsentRecovery(
        authorization_url="https://runtime.example.test/approve",
        access_id="access_cli",
        resource=request.target.resource,
        operation=request.operation,
        application_id=request.application_id,
        invocation_id=request.invocation_id,
        request_digest=request.request_digest,
        card_revision=4,
        catalog_version="catalog-1",
        expires_at=int(time.time()) + 600,
        choices=("allow_once", "allow_always"),
    )
    denial = ManagementDenial(
        status=403,
        code="delegated_request_permit_required",
        retryable=False,
        recovery=recovery,
    )
    management = _Management([denial, _result(request)])
    handled = []
    service = AuthorizedManagementService(
        sessions=_Sessions(expiring=False),
        discovery=_Discovery(),
        oauth=_OAuth(),
        management=management,
    )

    async def approve(value):
        handled.append(value)

    result = await service.execute_with_consent(
        request,
        consent_handler=approve,
    )

    assert isinstance(result, ManagementResult)
    assert handled == [recovery]
    assert management.calls[0][0] is request
    assert management.calls[1][0] is request
    assert management.calls[0][1] == management.calls[1][1]


@pytest.mark.asyncio
async def test_expired_consent_never_calls_handler_or_retries() -> None:
    request = ManagementRequest.reload(
        _target(),
        application_id="connection-hub@1-0",
        invocation_id="reload-expired",
    )
    recovery = ConsentRecovery(
        authorization_url="https://runtime.example.test/approve",
        access_id="access_cli",
        resource=request.target.resource,
        operation=request.operation,
        application_id=request.application_id,
        invocation_id=request.invocation_id,
        request_digest=request.request_digest,
        card_revision=4,
        catalog_version="catalog-1",
        expires_at=int(time.time()) - 1,
        choices=("allow_once", "allow_always"),
    )
    denial = ManagementDenial(
        status=403,
        code="delegated_request_permit_required",
        retryable=False,
        recovery=recovery,
    )
    management = _Management([denial])
    handled = []
    service = AuthorizedManagementService(
        sessions=_Sessions(expiring=False),
        discovery=_Discovery(),
        oauth=_OAuth(),
        management=management,
    )

    async def approve(value):
        handled.append(value)

    with pytest.raises(AuthorizationError) as raised:
        await service.execute_with_consent(
            request,
            consent_handler=approve,
        )

    assert raised.value.code == "management_recovery_expired"
    assert handled == []
    assert management.calls == [(request, "old-access")]


@pytest.mark.asyncio
async def test_disconnect_revokes_refresh_token_before_local_removal() -> None:
    target = _target()
    sessions = _Sessions(expiring=False)
    oauth = _OAuth()
    service = AuthorizedManagementService(
        sessions=sessions,
        discovery=_Discovery(),
        oauth=oauth,
        management=_Management([]),
    )

    removed = await service.disconnect(target.session_target_key)

    expected_session_id = session_id_for_target(target.session_target_key)
    assert removed == sessions.record
    assert sessions.session_ids == [expected_session_id]
    assert sessions.removed == [expected_session_id]
    assert oauth.revocations[0]["token"] == "refresh-secret"
    assert oauth.revocations[0]["token_type_hint"] == "refresh_token"


@pytest.mark.asyncio
async def test_disconnect_keeps_local_session_when_revocation_fails() -> None:
    target = _target()
    sessions = _Sessions(expiring=False)
    oauth = _OAuth()
    oauth.revoke_error = AuthorizationError(
        "oauth_token_request_failed",
        "The OAuth server rejected the request.",
    )
    service = AuthorizedManagementService(
        sessions=sessions,
        discovery=_Discovery(),
        oauth=oauth,
        management=_Management([]),
    )

    with pytest.raises(AuthorizationError):
        await service.disconnect(target.session_target_key)
    assert sessions.removed == []

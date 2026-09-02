from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Mapping

from fastapi.responses import JSONResponse

from connection_hub.delegated_credentials.admission import (
    MIN_SERVICE_SECRET_BYTES,
    AdmissionConfig,
    AdmissionRequest,
    ServiceProof,
    admission_allow,
    admission_denial,
    authorize_account_scope,
    pairwise_service_client_id,
    pairwise_service_subject,
    verify_admission_request,
)
from connection_hub.delegated_credentials.credential_view import DelegatedCredentialView
from connection_hub.invocation_policy import (
    SURFACE_OUTER,
    InvocationAuthority,
    InvocationPolicyService,
)
from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.oauth.surface_guard import (
    evaluate_delegated_rest_admission,
)


LOGGER = logging.getLogger("kdcube.connection_hub.admission")
SecretResolver = Callable[[str, str], Awaitable[str]]
RequestConfigBinder = Callable[[Any], Any]
InvocationRecoveryURLBuilder = Callable[
    [DelegatedCredentialView, AdmissionRequest], str
]
OperationGrantURLBuilder = Callable[
    [DelegatedCredentialView, AdmissionRequest, str], str
]


@dataclass(frozen=True)
class AdmissionHostContext:
    """KDCube capabilities supplied to the Connection Hub host adapter."""

    connections: Mapping[str, Any]
    redis: Any
    tenant: str
    project: str
    resolve_secret: SecretResolver
    bind_delegated_request: RequestConfigBinder
    invocation_policies: InvocationPolicyService | None = None
    invocation_recovery_url_builder: InvocationRecoveryURLBuilder | None = None
    operation_grant_url_builder: OperationGrantURLBuilder | None = None


def _request_bearer(request: Any) -> str:
    headers = getattr(request, "headers", None) or {}
    authorization = str(headers.get("authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


async def _claim_nonce(
    context: AdmissionHostContext,
    *,
    service_id: str,
    nonce: str,
    ttl_seconds: int,
) -> bool:
    digest = hashlib.sha256(f"{service_id}\n{nonce}".encode("utf-8")).hexdigest()
    key = (
        f"connection-hub:admission:{context.tenant}:{context.project}:nonce:{digest}"
    )
    claimed = await context.redis.set(
        key,
        "1",
        ex=max(1, int(ttl_seconds)),
        nx=True,
    )
    return bool(claimed)


def _guard_denial_facts(response: Any) -> tuple[str, str, dict[str, Any]]:
    payload: Dict[str, Any] = {}
    try:
        raw = getattr(response, "body", b"")
        parsed = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        payload = dict(parsed) if isinstance(parsed, Mapping) else {}
    except Exception:
        payload = {}

    error = payload.get("error")
    error_node = error if isinstance(error, Mapping) else {}
    ret = payload.get("ret")
    ret_node = ret if isinstance(ret, Mapping) else {}
    code = str(
        ret_node.get("reason")
        or error_node.get("code")
        or (error if isinstance(error, str) else "")
        or "delegated_admission_denied"
    ).strip()
    message = str(
        error_node.get("message")
        or payload.get("error_description")
        or payload.get("message")
        or "The delegated operation was denied."
    ).strip()
    return code, message, payload


def _denial_response(
    *,
    status_code: int,
    code: str,
    message: str,
    decision_id: str,
    retryable: bool = False,
    details: Mapping[str, Any] | None = None,
    consent: Mapping[str, Any] | None = None,
) -> JSONResponse:
    content = admission_denial(
        code=code,
        message=message,
        retryable=retryable,
        decision_id=decision_id,
        details=details,
    )
    if isinstance(consent, Mapping) and consent:
        content["consent"] = dict(consent)
    return JSONResponse(
        status_code=status_code,
        content=content,
    )


def _provider_invocation_policy(policy: Any) -> dict[str, Any]:
    """Project invocation state without the owner's card coordinates."""

    public = policy.to_public_dict()
    return {
        key: public[key]
        for key in ("mode", "revision", "state", "remaining", "updated_at")
        if key in public
    }


async def handle_delegated_admission(
    *,
    context: AdmissionHostContext,
    payload: Mapping[str, Any],
    request: Any,
) -> JSONResponse:
    """Evaluate one protected-service request against current authority."""

    decision_id = uuid.uuid4().hex
    config = AdmissionConfig.from_connections(context.connections)
    if not config.enabled:
        return _denial_response(
            status_code=404,
            code="delegated_admission_disabled",
            message="Direct protected-service admission is not enabled.",
            decision_id=decision_id,
        )
    if request is None:
        return _denial_response(
            status_code=400,
            code="request_context_missing",
            message="The protected-service request context is required.",
            decision_id=decision_id,
        )

    admission_request = AdmissionRequest.from_mapping(payload)
    validation_error = admission_request.validation_error()
    if validation_error:
        return _denial_response(
            status_code=400,
            code=validation_error,
            message="The admission request must name a resource and operation.",
            decision_id=decision_id,
        )

    proof = ServiceProof.from_headers(getattr(request, "headers", None))
    service = config.service(proof.service_id)
    if service is None:
        LOGGER.warning(
            "denied decision_id=%s reason=service_authentication_failed service_id=%s",
            decision_id,
            proof.service_id or "-",
        )
        return _denial_response(
            status_code=401,
            code="service_authentication_failed",
            message="The protected service could not be authenticated.",
            decision_id=decision_id,
        )
    if not service.allows_resource(admission_request.resource):
        LOGGER.warning(
            "denied decision_id=%s reason=service_resource_not_registered service_id=%s resource=%s",
            decision_id,
            service.service_id,
            admission_request.resource,
        )
        return _denial_response(
            status_code=403,
            code="service_resource_not_registered",
            message="The protected service is not registered for this resource.",
            decision_id=decision_id,
        )
    if not service.secret_ref:
        LOGGER.error(
            "unavailable decision_id=%s reason=service_secret_ref_missing service_id=%s",
            decision_id,
            service.service_id,
        )
        return _denial_response(
            status_code=503,
            code="service_authentication_unavailable",
            message="Protected-service authentication is unavailable.",
            decision_id=decision_id,
            retryable=True,
        )

    try:
        service_secret = await context.resolve_secret(
            service.secret_ref,
            f"delegated_admission.service.{service.service_id}",
        )
    except Exception:
        LOGGER.exception(
            "unavailable decision_id=%s reason=service_secret_unavailable service_id=%s",
            decision_id,
            service.service_id,
        )
        return _denial_response(
            status_code=503,
            code="service_authentication_unavailable",
            message="Protected-service authentication is unavailable.",
            decision_id=decision_id,
            retryable=True,
        )
    if len(service_secret.encode("utf-8")) < MIN_SERVICE_SECRET_BYTES:
        LOGGER.error(
            "unavailable decision_id=%s reason=service_secret_invalid service_id=%s",
            decision_id,
            service.service_id,
        )
        return _denial_response(
            status_code=503,
            code="service_authentication_unavailable",
            message="Protected-service authentication is unavailable.",
            decision_id=decision_id,
            retryable=True,
        )
    delegated_token = _request_bearer(request)
    proof_decision = verify_admission_request(
        secret=service_secret,
        proof=proof,
        delegated_token=delegated_token,
        request=admission_request,
        max_clock_skew_seconds=config.max_clock_skew_seconds,
    )
    if not proof_decision.allowed:
        LOGGER.warning(
            "denied decision_id=%s reason=%s service_id=%s resource=%s operation=%s",
            decision_id,
            proof_decision.reason,
            service.service_id,
            admission_request.resource,
            admission_request.operation,
        )
        return _denial_response(
            status_code=401,
            code="service_authentication_failed",
            message="The protected-service request proof is invalid.",
            decision_id=decision_id,
        )

    try:
        nonce_claimed = await _claim_nonce(
            context,
            service_id=service.service_id,
            nonce=proof.nonce,
            ttl_seconds=config.nonce_ttl_seconds,
        )
    except Exception:
        LOGGER.exception(
            "unavailable decision_id=%s reason=nonce_store_unavailable service_id=%s",
            decision_id,
            service.service_id,
        )
        return _denial_response(
            status_code=503,
            code="admission_replay_guard_unavailable",
            message="The admission replay guard is unavailable.",
            decision_id=decision_id,
            retryable=True,
        )
    if not nonce_claimed:
        LOGGER.warning(
            "denied decision_id=%s reason=request_replayed service_id=%s",
            decision_id,
            service.service_id,
        )
        return _denial_response(
            status_code=409,
            code="admission_request_replayed",
            message="This protected-service request was already evaluated.",
            decision_id=decision_id,
        )

    try:
        context.bind_delegated_request(request)
        result = await evaluate_delegated_rest_admission(
            request=request,
            auth={
                "mode": "managed",
                "authority_id": "delegated_client",
                "selected_operation_grants": True,
            },
            operation=admission_request.operation,
            method="POST",
            token=delegated_token,
            request_resource=admission_request.resource,
            log_identity_details=False,
        )
    except Exception:
        LOGGER.exception(
            "unavailable decision_id=%s reason=authority_resolution_unavailable service_id=%s",
            decision_id,
            service.service_id,
        )
        return _denial_response(
            status_code=503,
            code="delegated_authority_unavailable",
            message="The delegated authority could not be resolved.",
            decision_id=decision_id,
            retryable=True,
        )
    if result.denial is not None:
        code, message, guard_payload = _guard_denial_facts(result.denial)
        policy_denial = getattr(
            getattr(result, "decision", None),
            "denial",
            None,
        )
        policy_reason = str(
            getattr(policy_denial, "reason", "") or ""
        ).strip()
        policy_description = str(
            getattr(policy_denial, "description", "") or ""
        ).strip()
        if policy_reason:
            code = policy_reason
        if policy_description:
            message = policy_description
        status_code = int(getattr(result.denial, "status_code", 403) or 403)
        LOGGER.info(
            "denied decision_id=%s reason=%s service_id=%s resource=%s operation=%s",
            decision_id,
            code,
            service.service_id,
            admission_request.resource,
            admission_request.operation,
        )
        details: dict[str, Any] = {
            "resource": admission_request.resource,
            "operation": admission_request.operation,
        }
        consent: dict[str, Any] = {}
        requested = (
            guard_payload.get("ret", {}).get("requested_capability", {})
            if isinstance(guard_payload.get("ret"), Mapping)
            else {}
        )
        recoverable_outer_operation = code == "operation_not_consented" or (
            code == "delegated_capability_not_granted"
            and isinstance(requested, Mapping)
            and str(requested.get("kind") or "") == "outer_operation"
        )
        if recoverable_outer_operation:
            view = DelegatedCredentialView.from_envelope(
                result.envelope,
                result.grant_record,
            )
            change_id = (
                admission_request.invocation_id
                or f"admission-{decision_id}"
            )
            recovery_url = (
                context.operation_grant_url_builder(
                    view,
                    admission_request,
                    change_id,
                )
                if context.operation_grant_url_builder is not None
                else ""
            )
            if view.client_id and view.registry_access_id:
                grant_payload = {
                    "client_id": view.client_id,
                    "access_id": view.registry_access_id,
                    "resource": admission_request.resource,
                    "claims": [],
                    "resource_operations": {
                        admission_request.resource: [admission_request.operation]
                    },
                    "invocation_change_id": change_id,
                }
                consent = {
                    "kind": "delegated_agent_grant",
                    "reason": code,
                    "agent_client_id": view.client_id,
                    "access_id": view.registry_access_id,
                    "resource": admission_request.resource,
                    "claims": [],
                    "tool_name": admission_request.operation,
                    "outer_operation": admission_request.operation,
                    "connection_hub_url": recovery_url,
                    "invocation_policy": "choose",
                    "invocation_change_id": change_id,
                    "available_choices": ["allow_once", "allow_always"],
                    "grant": {
                        "operation": "delegated_agent_grant_create",
                        "payload": grant_payload,
                    },
                }
                details.update(
                    {
                        "access_id": view.registry_access_id,
                        "card_revision": view.card_revision,
                        "client_id": view.client_id,
                        "available_choices": ["allow_once", "allow_always"],
                        "recovery": consent,
                    }
                )
        return _denial_response(
            status_code=status_code,
            code=code,
            message=message,
            decision_id=decision_id,
            retryable=status_code >= 500,
            details=details,
            consent=consent,
        )

    view = DelegatedCredentialView.from_envelope(
        result.envelope,
        result.grant_record,
    )
    account_decision = authorize_account_scope(view, admission_request.account)
    if not account_decision.allowed:
        LOGGER.info(
            "denied decision_id=%s reason=%s service_id=%s resource=%s operation=%s",
            decision_id,
            account_decision.reason,
            service.service_id,
            admission_request.resource,
            admission_request.operation,
        )
        return _denial_response(
            status_code=403,
            code=account_decision.reason,
            message=(
                "The delegated card does not cover the requested connected "
                "account scope."
            ),
            decision_id=decision_id,
        )

    try:
        projection_secret = await context.resolve_secret(
            config.identity_projection_secret_ref,
            "delegated_admission.identity_projection",
        )
    except Exception:
        LOGGER.exception(
            "unavailable decision_id=%s reason=identity_projection_secret_unavailable service_id=%s",
            decision_id,
            service.service_id,
        )
        projection_secret = ""
    grantor_user_id = str(
        (result.runtime or {}).get("grantor_user_id") or ""
    ).strip()
    if (
        len(projection_secret.encode("utf-8")) < MIN_SERVICE_SECRET_BYTES
        or not grantor_user_id
        or result.decision is None
        or result.catalog is None
    ):
        LOGGER.error(
            "unavailable decision_id=%s reason=identity_projection_unavailable service_id=%s",
            decision_id,
            service.service_id,
        )
        return _denial_response(
            status_code=503,
            code="identity_projection_unavailable",
            message="The delegated principal projection is unavailable.",
            decision_id=decision_id,
            retryable=True,
        )

    subject = pairwise_service_subject(
        secret=projection_secret,
        service_id=service.service_id,
        grantor_user_id=grantor_user_id,
    )
    client_id = pairwise_service_client_id(
        secret=projection_secret,
        service_id=service.service_id,
        grantor_user_id=grantor_user_id,
        client_id=view.client_id,
    )
    grant_record = result.grant_record or {}
    try:
        expires_at = int(grant_record.get("expires_at") or 0)
    except (TypeError, ValueError):
        expires_at = 0
    response = admission_allow(
        decision_id=decision_id,
        service_id=service.service_id,
        subject=subject,
        client_id=client_id,
        view=view,
        request=admission_request,
        available_grants=result.decision.available_grants,
        account_claims=account_decision.claims,
        expires_at=expires_at,
        active_catalog_version=result.catalog.version,
    )
    if context.invocation_policies is not None:
        authority = InvocationAuthority(
            access_id=view.registry_access_id,
            resource=admission_request.resource,
            surface=SURFACE_OUTER,
            operation=admission_request.operation,
            provider_id=admission_request.account.provider_id,
            account_id=admission_request.account.account_id,
        )
        try:
            invocation_decision = await context.invocation_policies.begin(
                owner_subject=grantor_user_id,
                authority=authority,
                invocation_id=admission_request.invocation_id,
                request_digest=admission_request.request_digest,
                card_revision=view.card_revision,
                authority_revision=result.catalog.version,
            )
        except Exception:
            LOGGER.exception(
                "unavailable decision_id=%s reason=invocation_policy_unavailable "
                "service_id=%s resource=%s operation=%s",
                decision_id,
                service.service_id,
                admission_request.resource,
                admission_request.operation,
            )
            return _denial_response(
                status_code=503,
                code="delegated_invocation_policy_unavailable",
                message="The invocation policy could not be resolved.",
                decision_id=decision_id,
                retryable=True,
                details={
                    "access_id": view.registry_access_id,
                    "resource": admission_request.resource,
                    "surface": SURFACE_OUTER,
                    "operation": admission_request.operation,
                },
            )
        if not invocation_decision.dispatch:
            if (
                invocation_decision.replay
                and invocation_decision.invocation is not None
                and invocation_decision.invocation.state == "completed"
                and isinstance(invocation_decision.result, Mapping)
                and not invocation_decision.result_is_error
            ):
                replayed = dict(invocation_decision.result)
                replayed["replay"] = True
                replayed["invocation_id"] = admission_request.invocation_id
                return JSONResponse(replayed)
            details = {
                "access_id": view.registry_access_id,
                "card_revision": view.card_revision,
                "resource": admission_request.resource,
                "surface": SURFACE_OUTER,
                "operation": admission_request.operation,
                "invocation_id": admission_request.invocation_id,
                "available_choices": ["allow_once", "allow_always"],
                **invocation_decision.to_dict(),
            }
            if context.invocation_recovery_url_builder is not None:
                recovery_url = context.invocation_recovery_url_builder(
                    view,
                    admission_request,
                )
                if recovery_url:
                    details["recovery"] = {
                        "kind": "delegated_invocation_policy",
                        "connection_hub_url": recovery_url,
                        "access_id": view.registry_access_id,
                        "resource": admission_request.resource,
                        "outer_operation": admission_request.operation,
                        "available_choices": ["allow_once", "allow_always"],
                    }
            return _denial_response(
                status_code=(409 if invocation_decision.replay else 403),
                code=invocation_decision.reason,
                message="The delegated invocation policy denied this operation.",
                decision_id=decision_id,
                retryable=invocation_decision.retryable,
                details=details,
            )
        if invocation_decision.policy is not None:
            response["invocation_policy"] = _provider_invocation_policy(
                invocation_decision.policy
            )
            response["provenance"]["invocation_policy_revision"] = (
                invocation_decision.policy.revision
            )
        if admission_request.invocation_id:
            response["invocation_id"] = admission_request.invocation_id
            response["replay"] = False
        if invocation_decision.invocation is not None:
            try:
                await context.invocation_policies.complete(
                    owner_subject=grantor_user_id,
                    authority=authority,
                    invocation_id=admission_request.invocation_id,
                    request_digest=admission_request.request_digest,
                    result=response,
                )
            except Exception:
                LOGGER.exception(
                    "unavailable decision_id=%s reason=invocation_record_unavailable "
                    "service_id=%s resource=%s operation=%s",
                    decision_id,
                    service.service_id,
                    admission_request.resource,
                    admission_request.operation,
                )
                return _denial_response(
                    status_code=503,
                    code="delegated_invocation_record_unavailable",
                    message="The invocation decision could not be recorded.",
                    decision_id=decision_id,
                    retryable=True,
                    details={
                        "access_id": view.registry_access_id,
                        "resource": admission_request.resource,
                        "surface": SURFACE_OUTER,
                        "operation": admission_request.operation,
                        "invocation_id": admission_request.invocation_id,
                    },
                )
    LOGGER.info(
        "allowed decision_id=%s service_id=%s client_id=%s resource=%s operation=%s card_revision=%s catalog_version=%s",
        decision_id,
        service.service_id,
        view.client_id,
        admission_request.resource,
        admission_request.operation,
        view.card_revision,
        result.catalog.version,
    )
    return JSONResponse(response)


__all__ = [
    "AdmissionHostContext",
    "InvocationRecoveryURLBuilder",
    "OperationGrantURLBuilder",
    "handle_delegated_admission",
]

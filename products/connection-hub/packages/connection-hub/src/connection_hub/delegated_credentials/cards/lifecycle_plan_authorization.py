"""W578: the Hub's call to a project host's ``project_lifecycle_plan_authorize``.

``card_lifecycle_plan`` asks the project host that called it for ONE
authorization envelope covering every step of the plan. This module is that
call, for a host configured as a Card transaction caller
(``connections.card_transactions.callers.<service id>``):

- the request is exactly five unsigned fields: ``actor_subject``,
  ``project_ref``, ``request_id``, ``request_digest`` (the plan's digest) and
  the ordered ``steps`` (``ref``, ``operation``, ``target_subject``), plus
  ``service_proof``;
- the proof is the Hub's admission proof under the caller descriptor's
  authority request signer and secret, with its own protocol
  (``card-lifecycle-plan-authorize.v1``), so it never verifies as a
  transaction-authority request and the reverse; the host checks the signer,
  the exact body, the timestamp and a single-use nonce;
- the answer ``{ok: true, decisions: [{ref, decision}]}`` becomes a
  ``LifecyclePlanAuthorization``, which refuses at construction any missing,
  extra, repeated or mismatched step and any step decision for another plan
  digest (N1). Anything else is a ``ProjectAuthorizationError``.

Agreed with the project host's author (CodeApp, 2026-10-07 00:49 UTC).
"""

from __future__ import annotations

import os
import time
from typing import Any, Awaitable, Callable, Mapping

from service_foundation.coordination.durable_wire import canonical_json_bytes, sha256_hex

from ...bundle_operations import BundleOperationResultError, normalize_bundle_operation_result
from ..admission import AdmissionRequest, sign_admission_request
from ..project_authorization import (
    LifecyclePlanAuthorization, LifecyclePlanAuthorizationRequest, ProjectAuthorizationDecision,
    ProjectAuthorizationError,
)

PROTOCOL = "card-lifecycle-plan-authorize.v1"
OPERATION = "project_lifecycle_plan_authorize"

# call(bundle_id=..., operation=..., data=...) -> the host's response body
BundleCall = Callable[..., Awaitable[Any]]


def plan_authorization_body(request: LifecyclePlanAuthorizationRequest) -> dict[str, Any]:
    """The five unsigned fields the host authorizes, in their wire form."""

    return {
        "actor_subject": request.actor_subject,
        "project_ref": request.project_ref,
        "request_id": request.request_id,
        "request_digest": request.request_digest,
        "steps": [{"ref": step.ref, "operation": step.operation, "target_subject": step.target_subject}
                  for step in request.steps],
    }


def sign_plan_authorization(body: Mapping[str, Any], *, bundle_id: str, signer_id: str, secret: str,
                            clock: Callable[[], float] = time.time) -> dict[str, str]:
    """The Hub's ``service_proof`` over exactly ``body``."""

    proof = {"service_id": signer_id, "timestamp": str(int(clock())), "nonce": os.urandom(16).hex()}
    proof["signature"] = sign_admission_request(
        secret=secret, **proof, delegated_token=f"{PROTOCOL}:{body['request_id']}",
        request=AdmissionRequest(resource=bundle_id, operation=OPERATION, invocation_id=body["request_id"],
                                 request_digest=sha256_hex(canonical_json_bytes(dict(body))),
                                 approval_context={"protocol": PROTOCOL}))
    return proof


class PeerLifecyclePlanAuthorization:
    """``authorize_lifecycle_plan`` over one configured project host (a ``LifecyclePlanAuthorizationPort``)."""

    def __init__(self, *, call: BundleCall, bundle_id: str, signer_id: str, secret: str,
                 clock: Callable[[], float] = time.time) -> None:
        self._call = call
        self._bundle_id = bundle_id
        self._signer_id = signer_id
        self._secret = secret
        self._clock = clock

    async def authorize_lifecycle_plan(
            self, request: LifecyclePlanAuthorizationRequest) -> LifecyclePlanAuthorization:
        # Operator, 10 Oct: trace the PLAN callback into the project host (its refusal reached the
        # project only as card_plan_authorization_refused). The plan's request id is the trace.
        from ...card_save_trace import hop, trace_scope
        started = time.monotonic()
        with trace_scope(request.request_id):
            hop("hub.plan_authorize_callback", "entry")
            try:
                authorization = await self._authorize(request)
            except ProjectAuthorizationError as exc:
                hop("hub.plan_authorize_callback", "refused", code=exc.reason, started=started)
                raise
            except Exception:
                hop("hub.plan_authorize_callback", "error", started=started)
                raise
            hop("hub.plan_authorize_callback", "ok", started=started)
            return authorization

    async def _authorize(self, request: LifecyclePlanAuthorizationRequest) -> LifecyclePlanAuthorization:
        body = plan_authorization_body(request)
        proof = sign_plan_authorization(body, bundle_id=self._bundle_id, signer_id=self._signer_id,
                                        secret=self._secret, clock=self._clock)
        try:
            # The signed body travels whole under "data" with the identity hints given as None (else the
            # platform adds the session's and the host's exact-body check refuses), as managed_card_edit_forward.
            answer = await self._call(bundle_id=self._bundle_id, operation=OPERATION,
                                      data={"data": {**body, "service_proof": proof},
                                            "user_id": None, "fingerprint": None})
        except Exception:  # noqa: BLE001 - transport failure, by name only
            raise ProjectAuthorizationError("card_plan_authorization_unavailable") from None
        try:
            # Inside a request the operation route answers {"status": "ok", ..., "<operation>": answer}.
            answer = normalize_bundle_operation_result(OPERATION, answer)
        except BundleOperationResultError:
            raise ProjectAuthorizationError("card_plan_authorization_invalid") from None
        if answer.get("ok") is not True:
            raise ProjectAuthorizationError("card_plan_authorization_refused")
        decisions = answer.get("decisions")
        if not isinstance(decisions, list) or any(
                not isinstance(item, Mapping) or set(item) != {"ref", "decision"} or type(item["ref"]) is not str
                for item in decisions):
            raise ProjectAuthorizationError("card_plan_authorization_invalid")
        return LifecyclePlanAuthorization(request=request, decisions=tuple(
            (item["ref"], ProjectAuthorizationDecision.from_mapping(item["decision"])) for item in decisions))


__all__ = ["OPERATION", "PROTOCOL", "PeerLifecyclePlanAuthorization", "plan_authorization_body",
           "sign_plan_authorization"]

"""W578: ``card_lifecycle_plan``, a signed, non-writing plan of a Card lifecycle change for a peer.

Problem Board asks the Hub to construct the exact Card candidates of one
business change (a brand-new project's P, C and My; a person joining; an
invitation's redemption; a repair) so it can freeze them into its one
transaction intent (CodeApp 23:19, 23:48). This operation:

- authenticates exactly like ``card_census_read``: the caller is the admission
  proof's service id from the same caller descriptor, the proof covers the
  digest of every request field, the nonce is durable and single-use, and the
  request is frozen before any await;
- lets a caller plan only in scopes its descriptor entitles it to
  (``plan_scope_prefix``; absent, no planning);
- asks the project host for ONE request-bound authorization envelope
  (``LifecyclePlanAuthorization``): one exact decision per step, bound to the
  actor, the scope, the request id and this request's digest; a denied step
  refuses the plan by its own reason;
- calls the generic planner (W594) with that envelope; the planner writes
  nothing, reserves nothing and issues nothing;
- answers signed (``card-lifecycle-plan-answer.v1``, the shared participant
  answer construction under ``AnswerContract.PLAN``) with every request field
  echoed, and a ``plan`` or ``refused`` result.

A signed plan is a proposal, never an authorization or a reservation: the
initiator freezes it into its intent and the Hub's prepare rechecks every
original, absence, dependency and gate before any write.
"""

from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable, Mapping

from service_foundation.coordination.durable_wire import canonical_json_bytes
from service_foundation.coordination.participant_answer import AnswerContract
from service_foundation.coordination.participant_answer import request_digest as shared_request_digest
from service_foundation.coordination.participant_answer import sign_participant_answer

from ..admission import AdmissionRequest, ServiceProof, verify_admission_request
from ..project_authorization import (
    PROJECT_AGENT_CARD_UPDATE, PROJECT_CONTROL_CREATE, PROJECT_CONTROL_UPDATE, PROJECT_INVITATION_CONTROL_CREATE, PROJECT_INVITATION_CONTROL_REVOKE, PROJECT_INVITATION_CONTROL_UPDATE, PROJECT_PERSON_CONTROL_BIND_PROJECT,
    PROJECT_PERSON_CONTROL_CREATE, PROJECT_PERSON_CONTROL_REVOKE, PROJECT_PERSON_CONTROL_UPDATE, LifecyclePlanAuthorization, LifecyclePlanAuthorizationRequest,
    LifecyclePlanStep, ProjectAuthorizationError,
)
from .participant_operation import MAX_SKEW_SECONDS, ParticipantCaller

OPERATION = "card_lifecycle_plan"
REQUEST_SCHEMA = "card-lifecycle-plan-request.v1"
ANSWER_SCHEMA = "card-lifecycle-plan-answer.v1"
DIRECTION = "hub-to-authority"
REQUEST_FIELDS = ("schema", "request_echo", "scope", "actor_subject", "actor_kind", "request_id", "creations",
                  "updates")
PROOF_FIELDS = frozenset({"service_id", "timestamp", "nonce", "signature"})
MAX_STEPS = 8
MAX_REQUEST_BYTES = 64 * 1024
MAX_ANSWER_BYTES = 512 * 1024 - 4096
_ECHO = re.compile(r"[0-9a-f]{32,128}\Z")
_BOUNDED = 256

# Each generic constructor kind, the exact host operation that authorizes it and the identity field
# naming the person it is for (W594's shapes; the host evaluates its own policy for each step).
CREATION_STEPS = {
    "application_control": (PROJECT_CONTROL_CREATE, "holder_subject"),
    "project_person_control": (PROJECT_PERSON_CONTROL_CREATE, "target_subject"),
    "project_person_my_card": (PROJECT_PERSON_CONTROL_CREATE, "person_subject"),
    # W661 P4 (EMain 20:02Z): an invitation's pending Control Card, created on the link-only save.
    "project_invitation_control": (PROJECT_INVITATION_CONTROL_CREATE, "invitation_ref"),
}
UPDATE_STEPS = {"revoke": PROJECT_INVITATION_CONTROL_REVOKE, "attach": PROJECT_PERSON_CONTROL_BIND_PROJECT,
                # W607: an existing person Control or My Card takes a PB-supplied selection.
                "reselect": PROJECT_PERSON_CONTROL_UPDATE,
                # W661: a person's per-service Reset of their My Card to its Control (STAGE-owned).
                "reset_to_control": PROJECT_PERSON_CONTROL_UPDATE,
                # W661: a role change's Card side, a PB policy delta the Hub applies (EMain 18:16Z).
                "role_selection": PROJECT_PERSON_CONTROL_UPDATE,
                # W502: an active person leaves the project; their Control and My end in one decision.
                "remove_person": PROJECT_PERSON_CONTROL_REVOKE,
                # W638: the project's own Control and a project agent Card take a host-authorized selection.
                "reselect_project_control": PROJECT_CONTROL_UPDATE,
                "reselect_agent_card": PROJECT_AGENT_CARD_UPDATE,
                "reselect_invitation_control": PROJECT_INVITATION_CONTROL_UPDATE}

# W639: agent attendance/profile decisions use this same authorized PLAN.
from ..agent_lifecycle_plan import AGENT_UPDATE_STEPS
UPDATE_STEPS.update(AGENT_UPDATE_STEPS)

# planner(host, *, project_ref, creations, updates, actor_subject, actor_kind, request_id, authorization)
#   -> {"ok": True, "plan": {...}} or {"ok": False, "error": <code>}
Planner = Callable[..., Awaitable[Mapping[str, Any]]]


def plan_request_digest(request: Mapping[str, Any]) -> str:
    """The shared helper's digest of exactly the unsigned request fields, under the closed PLAN contract."""
    return shared_request_digest({name: request[name] for name in REQUEST_FIELDS}, contract=AnswerContract.PLAN)


def _bounded(value: Any) -> bool:
    return type(value) is str and 0 < len(value) <= _BOUNDED and value.isprintable()


def _valid_request(data: Any) -> bool:
    if not isinstance(data, Mapping) or set(data) != set(REQUEST_FIELDS) | {"service_proof"}:
        return False
    proof = data["service_proof"]
    creations, updates = data["creations"], data["updates"]
    return (isinstance(proof, Mapping) and set(proof) == PROOF_FIELDS
            and all(type(proof[name]) is str for name in PROOF_FIELDS)
            and data["schema"] == REQUEST_SCHEMA and type(data["request_echo"]) is str
            and bool(_ECHO.fullmatch(data["request_echo"])) and _bounded(data["scope"])
            and _bounded(data["actor_subject"]) and data["actor_subject"] == data["actor_subject"].strip()
            and data["actor_kind"] in ("caller", "grantor") and _bounded(data["request_id"])
            and type(creations) is list and type(updates) is list
            and 1 <= len(creations) + len(updates) <= MAX_STEPS
            and all(isinstance(item, Mapping) for item in (*creations, *updates))
            and len(canonical_json_bytes({name: data[name] for name in REQUEST_FIELDS})) <= MAX_REQUEST_BYTES)


def plan_steps(creations: list[Mapping[str, Any]], updates: list[Mapping[str, Any]]) -> tuple[LifecyclePlanStep, ...]:
    """The exact authorization step of every creation and update, or ``_Refused``.

    A creation's step is its own ``ref``; an update's is ``update:<index>``.
    """
    steps = []
    for creation in creations:
        kind = creation.get("kind")
        identity = creation.get("identity")
        if kind not in CREATION_STEPS or not isinstance(identity, Mapping) or not _bounded(creation.get("ref")):
            raise _Refused("card_plan_request_invalid", 400)
        operation, target_field = CREATION_STEPS[kind]
        target = identity.get(target_field)
        if not _bounded(target):
            raise _Refused("card_plan_request_invalid", 400)
        steps.append(LifecyclePlanStep(ref=creation["ref"], operation=operation, target_subject=target))
    for index, update in enumerate(updates):
        if update.get("kind") not in UPDATE_STEPS or not _bounded(update.get("target_subject")):
            raise _Refused("card_plan_request_invalid", 400)
        steps.append(LifecyclePlanStep(ref=f"update:{index}", operation=UPDATE_STEPS[update["kind"]],
                                       target_subject=update["target_subject"]))
    return tuple(steps)


class _Refused(Exception):
    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code, self.status = code, status


def _unsigned_refusal(code: str, status: int) -> dict[str, Any]:
    return {"ok": False, "status": status, "error": {"code": code}}


class CardLifecyclePlanOperation:
    """``answer(data)`` for ``card_lifecycle_plan``; bound by the composition root."""

    def __init__(self, *, callers: Mapping[str, ParticipantCaller], authorization: Any, planner: Planner,
                 host: Any, nonces: Any, clock: Callable[[], float],
                 nonce_prefix: str = "connection-hub:card-plan:nonce:") -> None:
        self._callers = dict(callers)
        self._authorization = authorization  # LifecyclePlanAuthorizationPort (the project host's)
        self._planner = planner
        self._host = host
        self._nonces = nonces
        self._clock = clock
        self._nonce_prefix = nonce_prefix

    async def answer(self, data: Any) -> dict[str, Any]:
        try:  # frozen before any await
            data = json.loads(json.dumps(dict(data), allow_nan=False)) if isinstance(data, Mapping) else None
        except (TypeError, ValueError):
            data = None
        try:
            valid = _valid_request(data)
        except Exception:  # noqa: BLE001 - a non-canonical request is invalid, by name
            valid = False
        if not valid:
            return _unsigned_refusal("card_plan_request_invalid", 400)
        raw_proof = data["service_proof"]
        caller = self._callers.get(raw_proof["service_id"])
        if caller is None:
            return _unsigned_refusal("card_participant_caller_unknown", 403)
        digest = plan_request_digest(data)
        proof = ServiceProof(**{name: raw_proof[name] for name in PROOF_FIELDS})
        verdict = verify_admission_request(
            secret=caller.request_secret, proof=proof, delegated_token=f"{REQUEST_SCHEMA}:{data['request_echo']}",
            request=AdmissionRequest(resource=caller.hub_resource, operation=OPERATION,
                                     invocation_id=data["request_echo"], request_digest=digest,
                                     approval_context={"protocol": REQUEST_SCHEMA}),
            max_clock_skew_seconds=MAX_SKEW_SECONDS, now=int(self._clock()))
        if not verdict.allowed:
            return _unsigned_refusal("card_participant_unauthenticated", 401)
        try:
            fresh = await self._nonces.set(f"{self._nonce_prefix}{caller.service_id}:{proof.nonce}", "1",
                                           ex=2 * MAX_SKEW_SECONDS, nx=True)
        except Exception:  # noqa: BLE001
            return _unsigned_refusal("card_participant_unavailable", 503)
        if not fresh:
            return _unsigned_refusal("card_participant_unauthenticated", 401)
        try:
            result = await self._plan(caller, data, digest)
        except _Refused as exc:
            result = {"kind": "refused", "code": exc.code, "status": exc.status}
        except Exception:  # noqa: BLE001 - authenticated, so signed; never internal text
            result = {"kind": "refused", "code": "card_participant_unavailable", "status": 503}
        return self._signed(caller, data, digest, result)

    async def _plan(self, caller: ParticipantCaller, data: Mapping[str, Any], digest: str) -> dict[str, Any]:
        prefix = caller.plan_scope_prefix
        if not prefix or not data["scope"].startswith(prefix):
            raise _Refused("card_plan_scope_forbidden", 403)
        steps = plan_steps(data["creations"], data["updates"])
        try:
            request = LifecyclePlanAuthorizationRequest(
                actor_subject=data["actor_subject"], project_ref=data["scope"], request_id=data["request_id"],
                request_digest=digest, steps=steps)
        except ProjectAuthorizationError as exc:
            raise _Refused(str(exc) or "card_plan_request_invalid", 400) from None
        # The caller's own project host answers for its plan; an explicit port
        # given to the operation (a test, a single-host composition) wins.
        port = self._authorization if self._authorization is not None else caller.plan_authorization
        if port is None:
            raise _Refused("card_plan_authorization_unavailable", 503)
        try:
            authorization = await port.authorize_lifecycle_plan(request)
        except ProjectAuthorizationError as exc:
            raise _Refused(str(exc) or "card_plan_authorization_unavailable", 503) from None
        if not isinstance(authorization, LifecyclePlanAuthorization):
            raise _Refused("card_plan_authorization_invalid", 503)
        try:
            authorization.validate_for(request)
        except ProjectAuthorizationError as exc:
            raise _Refused(str(exc), 503) from None
        if not authorization.allowed:
            raise _Refused(authorization.refusal(), 403)
        planned = await self._planner(self._host, project_ref=data["scope"], creations=list(data["creations"]),
                                      updates=list(data["updates"]), actor_subject=data["actor_subject"],
                                      actor_kind=data["actor_kind"], request_id=data["request_id"],
                                      authorization=authorization)
        if not isinstance(planned, Mapping) or planned.get("ok") is not True or not isinstance(
                planned.get("plan"), Mapping):
            code = planned.get("error") if isinstance(planned, Mapping) else None
            raise _Refused(code if _bounded(code) else "card_plan_refused", 409)
        return {"kind": "plan", "plan": dict(planned["plan"])}

    def _signed(self, caller: ParticipantCaller, data: Mapping[str, Any], digest: str,
                result: Mapping[str, Any]) -> dict[str, Any]:
        def unsigned_for(value: Mapping[str, Any]) -> dict[str, Any]:
            return {"schema": ANSWER_SCHEMA, "direction": DIRECTION, "audience": caller.audience,
                    "request_digest": digest, **{name: data[name] for name in REQUEST_FIELDS if name != "schema"},
                    "result": dict(value)}

        unsigned = unsigned_for(result)
        if len(canonical_json_bytes(unsigned)) > MAX_ANSWER_BYTES:
            unsigned = unsigned_for({"kind": "refused", "code": "card_plan_too_large", "status": 413})
        proof = sign_participant_answer(unsigned, schema=ANSWER_SCHEMA, secret=caller.receipt_secret,
                                        signer_id=caller.receipt_signer_id, timestamp=str(int(self._clock())),
                                        contract=AnswerContract.PLAN)
        return {"ok": unsigned["result"].get("kind") != "refused", "plan_answer": {**unsigned, "receipt_proof": proof}}


__all__ = ["ANSWER_SCHEMA", "CREATION_STEPS", "CardLifecyclePlanOperation", "OPERATION", "REQUEST_FIELDS",
           "REQUEST_SCHEMA", "UPDATE_STEPS", "plan_request_digest", "plan_steps"]

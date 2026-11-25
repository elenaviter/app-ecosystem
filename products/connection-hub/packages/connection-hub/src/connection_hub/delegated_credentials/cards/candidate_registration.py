"""Admit a peer's request to seal its exact Hub-signed lifecycle plan."""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any, Mapping

from service_foundation.coordination.durable_decision_log import DecisionRefused
from service_foundation.coordination.durable_wire import (
    WireRefused, canonical_json_bytes, parse_canonical_json_bytes, sha256_hex,
)
from service_foundation.coordination.participant_answer import (
    AnswerContract, ParticipantAnswerRefused, verify_participant_answer,
)

from ..admission import AdmissionRequest, ServiceProof, verify_admission_request
from .candidate_custody import CandidateCustody, MAX_SEAL_SECONDS
from .lifecycle_plan_operation import (
    ANSWER_SCHEMA as PLAN_ANSWER, DIRECTION as PLAN_DIRECTION, REQUEST_FIELDS as PLAN_FIELDS,
    REQUEST_SCHEMA as PLAN_SCHEMA,
)
from .participant_operation import MAX_SKEW_SECONDS, ParticipantCaller
from .transaction_authority_v2 import card_authority_signature

OPERATION = "card_candidate_set_register"
REQUEST_SCHEMA = "card-candidate-set-register-request.v1"
READ_OPERATION = "card_candidate_set_read_prepared"
READ_REQUEST_SCHEMA = "card-candidate-set-read-prepared-request.v1"
READ_ANSWER_SCHEMA = "card-candidate-set-read-prepared-answer.v1"
READ_FIELDS = {"schema", "request_echo", "scope", "incarnation", "request_id", "candidate_ref",
               "transaction_id", "intent_digest"}
ANSWER_SCHEMA = "card-candidate-set-register-answer.v1"
MAX_REQUEST_BYTES = 512 * 1024
REQUEST_FIELDS = {"schema", "request_echo", "scope", "incarnation", "request_id", "deadline", "plan_answer",
                  "plan_attestation"}
_PROOF_FIELDS = {"service_id", "timestamp", "nonce", "signature"}
_ECHO = re.compile(r"[0-9a-f]{32,128}\Z")


def registration_digest(request: Mapping[str, Any]) -> str:
    return sha256_hex(canonical_json_bytes({key: request[key] for key in REQUEST_FIELDS}))


def plan_attestation(answer: Mapping[str, Any], *, caller: ParticipantCaller, secret: str) -> str:
    """A Hub-only proof, separate from both secrets shared with the peer."""
    key = secret.encode("utf-8")
    if len(key) < 32 or any(
        hmac.compare_digest(key, value.encode("utf-8") if isinstance(value, str) else value)
        for value in (caller.request_secret, caller.receipt_secret)
    ):
        raise DecisionRefused("candidate_custody_attestation_unavailable")
    body = canonical_json_bytes({"schema": "card-candidate-set-attestation.v1", "peer": caller.service_id,
                                 "answer": dict(answer)})
    return hmac.new(key, body, hashlib.sha256).hexdigest()


def _text(value: Any) -> bool:
    return type(value) is str and 0 < len(value) <= 512 and value.isprintable()


def _refuse(code: str, status: int) -> dict[str, Any]:
    return {"ok": False, "status": status, "error": {"code": code}}


def _snapshot(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, Mapping) or set(data) != REQUEST_FIELDS | {"service_proof"}:
        return None
    try:
        raw = canonical_json_bytes(dict(data))
        if len(raw) > MAX_REQUEST_BYTES:
            return None
        data = parse_canonical_json_bytes(raw)
    except WireRefused:
        return None
    proof = data["service_proof"]
    if (data["schema"] != REQUEST_SCHEMA or not all(_text(data[key]) for key in ("scope", "incarnation", "request_id"))
            or type(data["request_echo"]) is not str or not _ECHO.fullmatch(data["request_echo"])
            or type(data["deadline"]) is not int or type(data["plan_answer"]) is not dict
            or type(data["plan_attestation"]) is not str or not re.fullmatch("[0-9a-f]{64}", data["plan_attestation"])
            or type(proof) is not dict or set(proof) != _PROOF_FIELDS
            or not all(_text(proof[key]) for key in _PROOF_FIELDS)):
        return None
    return data


class CandidateRegistration:
    def __init__(self, *, callers: Mapping[str, ParticipantCaller], custody: CandidateCustody,
                 nonces: Any, nonce_prefix: str, tenant: str, app: str, attestation_secret: str) -> None:
        self.callers, self.custody, self.nonces = dict(callers), custody, nonces
        self.nonce_prefix, self.tenant, self.app = nonce_prefix, tenant, app
        self._attestation_secret = attestation_secret

    async def _authenticate(self, request, *, operation, digest):
        proof = ServiceProof(**request["service_proof"])
        caller = self.callers.get(proof.service_id)
        if caller is None:
            return None, _refuse("card_participant_caller_unknown", 403)
        echo, schema = request["request_echo"], request["schema"]
        verdict = verify_admission_request(
            secret=caller.request_secret, proof=proof, delegated_token=f"{schema}:{echo}",
            request=AdmissionRequest(resource=caller.hub_resource, operation=operation, invocation_id=echo,
                                     request_digest=digest, approval_context={"protocol": schema}),
            max_clock_skew_seconds=MAX_SKEW_SECONDS, now=int(self.custody.clock()),
        )
        if not verdict.allowed:
            return None, _refuse("card_participant_unauthenticated", 401)
        try:
            fresh = await self.nonces.set(f"{self.nonce_prefix}{caller.service_id}:{proof.nonce}", "1",
                                          ex=2 * MAX_SKEW_SECONDS, nx=True)
        except Exception:
            return None, _refuse("candidate_custody_unavailable", 503)
        if not fresh:
            return None, _refuse("card_participant_unauthenticated", 401)
        return caller, None

    async def answer(self, data: Any) -> dict[str, Any]:
        request = _snapshot(data)
        if request is None:
            return _refuse("candidate_custody_request_invalid", 400)
        digest = registration_digest(request)
        caller, failure = await self._authenticate(request, operation=OPERATION, digest=digest)
        if failure:
            return failure
        echo = request["request_echo"]
        try:
            ref = await self._seal(request, caller)
            result = {"kind": "candidate_set", "ref": ref}
        except DecisionRefused as exc:
            code = str(exc)
            result = {"kind": "refused", "code": code, "status": 503 if code.endswith("unavailable") else 409}
        except Exception:  # authenticated refusal, with no storage or request detail
            result = {"kind": "refused", "code": "candidate_custody_unavailable", "status": 503}
        unsigned = {"schema": ANSWER_SCHEMA, "audience": caller.audience, "request_echo": echo,
                    "request_digest": digest, "scope": request["scope"], "incarnation": request["incarnation"],
                    "request_id": request["request_id"], "deadline": request["deadline"], "result": result}
        timestamp = str(int(self.custody.clock()))
        signature = card_authority_signature(unsigned, secret=caller.receipt_secret,
                                            service_id=caller.receipt_signer_id, timestamp=timestamp,
                                            protocol=ANSWER_SCHEMA)
        return {"ok": result["kind"] == "candidate_set", "candidate_set_answer": {**unsigned, "receipt_proof": {
            "service_id": caller.receipt_signer_id, "timestamp": timestamp, "signature": signature,
        }}}

    async def read_prepared(self, data: Any) -> dict[str, Any]:
        try:
            if not isinstance(data, Mapping) or set(data) != READ_FIELDS | {"service_proof"}:
                raise ValueError
            raw = canonical_json_bytes(dict(data))
            if len(raw) > 8192:
                raise ValueError
            request = parse_canonical_json_bytes(raw)
            proof = request["service_proof"]
            if (request["schema"] != READ_REQUEST_SCHEMA or type(proof) is not dict
                    or set(proof) != _PROOF_FIELDS or not all(_text(proof[key]) for key in _PROOF_FIELDS)
                    or not all(_text(request[key]) for key in ("scope", "incarnation", "request_id", "transaction_id"))
                    or not _text(request["request_echo"]) or not _ECHO.fullmatch(request["request_echo"])
                    or not _text(request["intent_digest"]) or not re.fullmatch("[0-9a-f]{64}", request["intent_digest"])):
                raise ValueError
            from .candidate_custody import validate_candidate_ref

            validate_candidate_ref(request["candidate_ref"])
        except (WireRefused, DecisionRefused, ValueError, TypeError):
            return _refuse("candidate_custody_request_invalid", 400)
        original = {key: request[key] for key in READ_FIELDS}
        digest = sha256_hex(canonical_json_bytes(original))
        caller, failure = await self._authenticate(request, operation=READ_OPERATION, digest=digest)
        if failure:
            return failure
        try:
            if not caller.plan_scope_prefix or not request["scope"].startswith(caller.plan_scope_prefix):
                raise DecisionRefused("candidate_custody_scope_forbidden")
            candidate = await self.custody.read_prepared(
                reference=request["candidate_ref"], peer=caller.service_id, project=request["scope"],
                incarnation=request["incarnation"], request_id=request["request_id"],
                transaction_id=request["transaction_id"], intent_digest=request["intent_digest"],
            )
            result = {"kind": "prepared_candidate", "candidate": candidate}
        except DecisionRefused as exc:
            code = str(exc)
            result = {"kind": "refused", "code": code, "status": 503 if code.endswith("unavailable") else 409}
        except Exception:
            result = {"kind": "refused", "code": "candidate_custody_unavailable", "status": 503}
        unsigned = {"schema": READ_ANSWER_SCHEMA, "audience": caller.audience, "request_digest": digest,
                    "request_echo": original["request_echo"], "request": original, "result": result}
        timestamp = str(int(self.custody.clock()))
        signature = card_authority_signature(unsigned, secret=caller.receipt_secret,
                                            service_id=caller.receipt_signer_id, timestamp=timestamp,
                                            protocol=READ_ANSWER_SCHEMA)
        return {"ok": result["kind"] == "prepared_candidate", "candidate_set_answer": {**unsigned, "receipt_proof": {
            "service_id": caller.receipt_signer_id, "timestamp": timestamp, "signature": signature,
        }}}

    async def _seal(self, request: Mapping[str, Any], caller: ParticipantCaller) -> dict[str, Any]:
        project = request["scope"]
        if not caller.plan_scope_prefix or not project.startswith(caller.plan_scope_prefix):
            raise DecisionRefused("candidate_custody_scope_forbidden")
        answer = request["plan_answer"]
        expected = plan_attestation(answer, caller=caller, secret=self._attestation_secret)
        if not hmac.compare_digest(expected, request["plan_attestation"]):
            raise DecisionRefused("candidate_custody_plan_invalid")
        try:
            plan_request = {**{key: answer[key] for key in PLAN_FIELDS if key != "schema"}, "schema": PLAN_SCHEMA}
            verified = verify_participant_answer(
                answer, schema=PLAN_ANSWER, secret=caller.receipt_secret, signer_id=caller.receipt_signer_id,
                audience=caller.audience, direction=PLAN_DIRECTION, request=plan_request,
                now=int(self.custody.clock()), max_skew=MAX_SKEW_SECONDS, contract=AnswerContract.PLAN,
            )
            if (plan_request["scope"] != project or verified.get("kind") != "plan"
                    or request["deadline"] > int(answer["receipt_proof"]["timestamp"]) + MAX_SEAL_SECONDS):
                raise ValueError("candidate plan scope or deadline mismatch")
            plan = verified["plan"]
            projection, candidate = plan["participant_input"], plan["candidate_value"]
            if projection["actor_subject"] != plan_request["actor_subject"]:
                raise ValueError("candidate actor mismatch")
            scope = {"tenant": self.tenant, "app": self.app, "peer": caller.service_id,
                     "actor": plan_request["actor_subject"], "project": project,
                     "incarnation": request["incarnation"], "target_scope": projection["target_scope"]}
        except (KeyError, TypeError, ValueError, ParticipantAnswerRefused) as exc:
            raise DecisionRefused("candidate_custody_plan_invalid") from exc
        return await self.custody.seal(
            scope=scope, request={"id": request["request_id"], "approval_basis": answer["request_digest"]},
            deadline=request["deadline"], projection=projection, candidate=candidate,
        )

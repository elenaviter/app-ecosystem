"""Authenticate a participant answer against a frozen unsigned request.

Peer configuration and result semantics belong to the application. This module
owns only canonical bytes, exact envelope binding and the response HMAC frame.
It does not authenticate requests, choose peers or operate a decision ledger.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import math
import re
from enum import Enum
from typing import Any, Mapping

from .durable_wire import WireRefused, canonical_json_bytes, parse_canonical_json_bytes, sha256_hex


REQUEST_FIELDS = frozenset({
    "schema", "action", "request_echo", "scope", "transaction_id", "decision", "limit", "cursor",
})
ECHO_FIELDS = REQUEST_FIELDS - {"schema"}
ANSWER_FIELDS = ECHO_FIELDS | {"schema", "direction", "audience", "request_digest", "result"}
CENSUS_REQUEST_FIELDS = frozenset({"schema", "request_echo", "scope", "persons", "include_catalog"})
CENSUS_ECHO_FIELDS = CENSUS_REQUEST_FIELDS - {"schema"}
CENSUS_ANSWER_FIELDS = CENSUS_ECHO_FIELDS | {"schema", "direction", "audience", "request_digest", "result"}
# W578: a Card lifecycle plan request and its signed answer (Connection Hub's card_lifecycle_plan).
PLAN_REQUEST_FIELDS = frozenset({
    "schema", "request_echo", "scope", "actor_subject", "actor_kind", "request_id", "creations", "updates",
})
PLAN_ECHO_FIELDS = PLAN_REQUEST_FIELDS - {"schema"}
PLAN_ANSWER_FIELDS = PLAN_ECHO_FIELDS | {"schema", "direction", "audience", "request_digest", "result"}
# W502 lane D: register a Hub-sealed Card read collection (Connection Hub's card_read_collection_register).
COLLECTION_REQUEST_FIELDS = frozenset({
    "schema", "request_echo", "scope", "persons", "exclude", "actor_subject", "request_id", "deadline",
})
COLLECTION_ECHO_FIELDS = COLLECTION_REQUEST_FIELDS - {"schema"}
COLLECTION_ANSWER_FIELDS = COLLECTION_ECHO_FIELDS | {"schema", "direction", "audience", "request_digest", "result"}
# W661: PB's STAGE / PUBLISH / ROLLBACK of one Card save (Connection Hub's card_version).
CARD_VERSION_REQUEST_FIELDS = frozenset({
    "schema", "op", "request_echo", "scope", "txn", "request_id", "catalog", "actor_subject", "actor_kind",
    "delegable_grants", "project_control", "creations", "updates",
})
CARD_VERSION_ECHO_FIELDS = CARD_VERSION_REQUEST_FIELDS - {"schema"}
CARD_VERSION_ANSWER_FIELDS = CARD_VERSION_ECHO_FIELDS | {"schema", "direction", "audience", "request_digest", "result"}
PROOF_FIELDS = frozenset({"service_id", "timestamp", "signature"})
_ECHO = re.compile(r"[0-9a-f]{32,128}\Z")
_TIMESTAMP = re.compile(r"(?:0|[1-9][0-9]{0,19})\Z")
_SIGNATURE = re.compile(r"[A-Za-z0-9_-]{43}\Z")


class ParticipantAnswerRefused(ValueError):
    """A finite verification failure, never a participant's semantic refusal.

    Unauthenticated/unsigned transport errors must stay unavailable to the
    coordinator. Only a successfully verified result may be interpreted using
    the application's result/refusal vocabulary.
    """

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class AnswerContract(Enum):
    """Closed exact-field contracts selected by trusted producer/consumer code.

    Request data never supplies a field set. The existing participant contract
    is the default, with identical bytes and binding rules.
    """

    PARTICIPANT = "participant"
    CENSUS = "census"
    PLAN = "plan"
    COLLECTION = "collection"
    CARD_VERSION = "card_version"


def _fields(contract: AnswerContract) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    if contract is AnswerContract.PARTICIPANT:
        return REQUEST_FIELDS, ECHO_FIELDS, ANSWER_FIELDS
    if contract is AnswerContract.CENSUS:
        return CENSUS_REQUEST_FIELDS, CENSUS_ECHO_FIELDS, CENSUS_ANSWER_FIELDS
    if contract is AnswerContract.PLAN:
        return PLAN_REQUEST_FIELDS, PLAN_ECHO_FIELDS, PLAN_ANSWER_FIELDS
    if contract is AnswerContract.COLLECTION:
        return COLLECTION_REQUEST_FIELDS, COLLECTION_ECHO_FIELDS, COLLECTION_ANSWER_FIELDS
    if contract is AnswerContract.CARD_VERSION:
        return CARD_VERSION_REQUEST_FIELDS, CARD_VERSION_ECHO_FIELDS, CARD_VERSION_ANSWER_FIELDS
    raise ParticipantAnswerRefused("answer_configuration_invalid")


def _snapshot(value: Mapping[str, Any], code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ParticipantAnswerRefused(code)
    try:
        # Return only detached canonical values, not the caller's mutable graph.
        return parse_canonical_json_bytes(canonical_json_bytes(dict(value)))
    except (WireRefused, ValueError, TypeError, RecursionError) as exc:
        raise ParticipantAnswerRefused(code) from exc


def _text(value: Any) -> bool:
    return (type(value) is str and 0 < len(value) <= 256
            and all(char.isprintable() for char in value))


def _key(secret: str | bytes) -> bytes:
    try:
        key = secret.encode("utf-8", "strict") if type(secret) is str else secret
    except UnicodeEncodeError as exc:
        raise ParticipantAnswerRefused("answer_configuration_invalid") from exc
    if type(key) is not bytes or len(key) < 32:
        raise ParticipantAnswerRefused("answer_configuration_invalid")
    return key


def _request(fields: Mapping[str, Any], contract: AnswerContract) -> dict[str, Any]:
    request_fields, _, _ = _fields(contract)
    value = _snapshot(fields, "request_invalid")
    if (set(value) != request_fields or not _text(value["schema"])
            or type(value["request_echo"]) is not str
            or _ECHO.fullmatch(value["request_echo"]) is None):
        raise ParticipantAnswerRefused("request_invalid")
    # Action/decision/scope/limit semantics are deliberately application-owned.
    return value


def request_digest(fields: Mapping[str, Any], *,
                   contract: AnswerContract = AnswerContract.PARTICIPANT) -> str:
    """Hash the selected fixed unsigned contract; select fields before calling.

    A full transport request containing ``service_proof`` is refused, never
    silently hashed or stripped. Preserve explicit nulls for inactive fields.
    """
    return sha256_hex(canonical_json_bytes(_request(fields, contract)))


def _unsigned(value: Mapping[str, Any], schema: str, contract: AnswerContract) -> dict[str, Any]:
    _, _, answer_fields = _fields(contract)
    answer = _snapshot(value, "answer_malformed")
    if (set(answer) != answer_fields or answer["schema"] != schema
            or not _text(answer["direction"]) or not _text(answer["audience"])
            or type(answer["request_echo"]) is not str
            or _ECHO.fullmatch(answer["request_echo"]) is None
            or type(answer["request_digest"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", answer["request_digest"]) is None
            or type(answer["result"]) is not dict):
        raise ParticipantAnswerRefused("answer_malformed")
    return answer


def _signature(unsigned: Mapping[str, Any], *, schema: str, key: bytes,
               signer_id: str, timestamp: str) -> str:
    frame = "\n".join((schema, signer_id, timestamp, unsigned["request_echo"],
                       sha256_hex(canonical_json_bytes(dict(unsigned))))).encode("utf-8")
    return base64.urlsafe_b64encode(hmac.new(key, frame, hashlib.sha256).digest()).rstrip(b"=").decode("ascii")


def sign_participant_answer(unsigned: Mapping[str, Any], *, schema: str,
                            secret: str | bytes, signer_id: str,
                            timestamp: str,
                            contract: AnswerContract = AnswerContract.PARTICIPANT) -> dict[str, str]:
    """Return the exact three-field proof; schema and signer are trusted config."""
    key = _key(secret)
    if (not _text(schema) or not _text(signer_id) or type(timestamp) is not str
            or _TIMESTAMP.fullmatch(timestamp) is None):
        raise ParticipantAnswerRefused("answer_configuration_invalid")
    answer = _unsigned(unsigned, schema, contract)
    return {"service_id": signer_id, "timestamp": timestamp,
            "signature": _signature(answer, schema=schema, key=key,
                                    signer_id=signer_id, timestamp=timestamp)}


def verify_participant_answer(answer: Mapping[str, Any], *, schema: str,
                              secret: str | bytes, signer_id: str,
                              audience: str, direction: str,
                              request: Mapping[str, Any], now: int | float,
                              max_skew: int = 300,
                              contract: AnswerContract = AnswerContract.PARTICIPANT) -> dict[str, Any]:
    """Return an authenticated detached result, or a finite local failure.

    ``answer`` is the inner signed envelope, NOT its unsigned transport wrapper.
    All expected identity/configuration values come from trusted local config;
    ``request`` is the frozen unsigned request for the trusted fixed contract.
    The application must subsequently validate its result tagged union and
    Receipt values. A verified refusal remains a refusal, not success.
    """
    key = _key(secret)
    if (any(not _text(value) for value in (schema, signer_id, audience, direction))
            or type(now) not in (int, float) or not 0 <= now <= 10 ** 20
            or not math.isfinite(now)
            or type(max_skew) is not int or not 0 <= max_skew <= 300):
        raise ParticipantAnswerRefused("answer_configuration_invalid")
    _, echo_fields, answer_fields = _fields(contract)
    frozen = _request(request, contract)
    value = _snapshot(answer, "answer_malformed")
    if set(value) != answer_fields | {"receipt_proof"}:
        raise ParticipantAnswerRefused("answer_malformed")
    proof = value.pop("receipt_proof")
    unsigned = _unsigned(value, schema, contract)
    if (type(proof) is not dict or set(proof) != PROOF_FIELDS
            or type(proof["timestamp"]) is not str
            or _TIMESTAMP.fullmatch(proof["timestamp"]) is None
            or type(proof["signature"]) is not str
            or _SIGNATURE.fullmatch(proof["signature"]) is None):
        raise ParticipantAnswerRefused("answer_malformed")
    if (proof["service_id"] != signer_id or unsigned["audience"] != audience
            or unsigned["direction"] != direction
            or unsigned["request_digest"] != request_digest(frozen, contract=contract)
            or any(canonical_json_bytes(unsigned[name]) != canonical_json_bytes(frozen[name])
                   for name in echo_fields)):
        raise ParticipantAnswerRefused("answer_binding_mismatch")
    if abs(now - int(proof["timestamp"])) > max_skew:
        raise ParticipantAnswerRefused("answer_stale")
    expected = _signature(unsigned, schema=schema, key=key,
                          signer_id=signer_id, timestamp=proof["timestamp"])
    if not hmac.compare_digest(expected, proof["signature"]):
        raise ParticipantAnswerRefused("answer_proof_invalid")
    return unsigned["result"]


__all__ = ["ANSWER_FIELDS", "ECHO_FIELDS", "PROOF_FIELDS", "REQUEST_FIELDS",
           "CENSUS_ANSWER_FIELDS", "CENSUS_ECHO_FIELDS", "CENSUS_REQUEST_FIELDS", "AnswerContract",
           "COLLECTION_ANSWER_FIELDS", "COLLECTION_ECHO_FIELDS", "COLLECTION_REQUEST_FIELDS",
           "PLAN_ANSWER_FIELDS", "PLAN_ECHO_FIELDS", "PLAN_REQUEST_FIELDS",
           "ParticipantAnswerRefused", "request_digest", "sign_participant_answer",
           "verify_participant_answer"]

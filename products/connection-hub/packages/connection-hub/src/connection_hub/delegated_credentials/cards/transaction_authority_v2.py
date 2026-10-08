"""W502 v2: verify a Card transaction authority response over the ONE generic decision record.

The application that initiated a transaction (Problem Board) owns its decision
store; the Hub cannot read it. The Hub pulls a signed response from the
authority its Card's trusted binding names and stages or materializes only
what that response proves (CodeApp 16:58, signer ``services/card_business_
authority.py`` at Applications d4aaeafe):

- the exact persisted ``GlobalIntent`` canonical bytes (W581 v2), whose digest,
  transaction id and Hub projection are recomputed here, never trusted;
- the Hub's selected candidate ``{access_id, original_revision, candidate,
  effects}``, whose kernel-canonical hash must equal the projection's
  ``candidate_digest`` and whose revisions must match the projection;
- the phase's decision: ``stage`` is undecided, ``decision`` is a recorded
  terminal value with its decided time;
- an HMAC-SHA256 proof over this Hub's fresh request echo and the canonical
  hash of every unsigned field, so an old response cannot be replayed.

Pure function, no I/O and no module state. The secret, service id, audience
and clock come from the hosting composition and the Card's binding, never
from the caller or the response.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import Any, Mapping

from service_foundation.coordination.durable_wire import (
    GlobalIntent, WireRefused, canonical_json_bytes, sha256_hex, verify_participant_projection,
)

from .card_participant import candidate_value_digest

GROUP_BINDING_KIND = "connection-hub.card-group"  # cards/card_group.py; imported lazily (no import cycle)
READ_SET_BINDING_KIND = "connection-hub.card-read-set"  # cards/card_read_set.py; imported lazily
READ_COLLECTION_BINDING_KIND = "connection-hub.card-read-collection"  # W502 lane D, card_read_set.py

PROTOCOL = "card-transaction-authority.v2"
UNSIGNED_FIELDS = frozenset({
    "schema", "phase", "request_echo", "audience", "participant", "global_intent_bytes",
    "global_intent_digest", "projection", "candidate", "decision", "decided_at",
})
PROOF_FIELDS = frozenset({"service_id", "timestamp", "signature"})
CANDIDATE_FIELDS = frozenset({"access_id", "original_revision", "candidate", "effects"})
MIN_SECRET_BYTES = 32
_ECHO = re.compile(r"[0-9a-f]{32,128}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_TIMESTAMP = re.compile(r"(0|[1-9][0-9]{0,15})\Z")


class TransactionAuthorityRefused(ValueError):
    """The response does not prove what this phase needs; nothing is staged or materialized."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class VerifiedCardAuthority:
    intent: GlobalIntent
    projection: dict[str, Any]
    candidate: dict[str, Any]
    decision: str
    decided_at: int | None


def _refuse(reason: str) -> None:
    raise TransactionAuthorityRefused(reason)


def _key(secret: str | bytes) -> bytes:
    if type(secret) not in (str, bytes):
        _refuse("authority_secret_invalid")
    key = secret.encode("utf-8") if type(secret) is str else secret
    if len(key) < MIN_SECRET_BYTES:
        _refuse("authority_secret_invalid")
    return key


def card_authority_signature(unsigned: Mapping[str, Any], *, secret: str | bytes, service_id: str,
                             timestamp: str) -> str:
    """The agreed proof: HMAC-SHA256 over the newline join, base64url without padding."""

    message = "\n".join((PROTOCOL, service_id, timestamp, unsigned["request_echo"],
                         sha256_hex(canonical_json_bytes(dict(unsigned))))).encode("utf-8")
    return base64.urlsafe_b64encode(hmac.new(_key(secret), message, hashlib.sha256).digest()).rstrip(b"=").decode("ascii")


def verify_card_authority_v2(
    response: Mapping[str, Any], *, secret: str | bytes, service_id: str, audience: str, participant: str,
    transaction_id: str, phase: str, request_echo: str, now: int, max_skew: int = 300,
) -> VerifiedCardAuthority:
    """The verified intent, Hub projection, candidate and decision, or a named refusal."""

    if (phase not in ("stage", "decision") or type(request_echo) is not str or not _ECHO.fullmatch(request_echo)
            or type(now) is not int or now < 0 or type(max_skew) is not int or max_skew < 0
            or type(transaction_id) is not str or not transaction_id):
        _refuse("authority_request_invalid")
    key = _key(secret)
    if not isinstance(response, Mapping) or set(response) != UNSIGNED_FIELDS | {"authority_proof"}:
        _refuse("authority_response_invalid")
    proof = response["authority_proof"]
    if not isinstance(proof, Mapping) or set(proof) != PROOF_FIELDS:
        _refuse("authority_proof_invalid")
    unsigned = {name: response[name] for name in UNSIGNED_FIELDS}
    # Bound to THIS request: an echo, phase, audience or participant that is
    # not ours is a replay or a mix-up, whatever its proof says.
    if (unsigned["schema"] != PROTOCOL or unsigned["phase"] != phase or unsigned["request_echo"] != request_echo
            or unsigned["audience"] != audience or unsigned["participant"] != participant):
        _refuse("authority_response_mismatch")
    if proof["service_id"] != service_id:
        _refuse("authority_service_mismatch")
    timestamp = proof["timestamp"]
    if type(timestamp) is not str or not _TIMESTAMP.fullmatch(timestamp) or abs(now - int(timestamp)) > max_skew:
        _refuse("authority_proof_stale")
    try:
        expected = card_authority_signature(unsigned, secret=key, service_id=service_id, timestamp=timestamp)
    except WireRefused:
        _refuse("authority_response_invalid")
    if type(proof["signature"]) is not str or not hmac.compare_digest(proof["signature"], expected):
        _refuse("authority_signature_invalid")

    # The immutable intent: parsed from its exact canonical bytes, never a dictionary.
    raw = unsigned["global_intent_bytes"]
    if type(raw) is not str:
        _refuse("authority_intent_invalid")
    try:
        intent = GlobalIntent.from_canonical_bytes(raw.encode("utf-8", "strict"))
    except (WireRefused, UnicodeEncodeError):
        _refuse("authority_intent_invalid")
    if intent.transaction_id != transaction_id:
        _refuse("authority_transaction_mismatch")
    if unsigned["global_intent_digest"] != intent.digest:
        _refuse("authority_intent_digest_mismatch")
    projection = unsigned["projection"]
    try:
        verify_participant_projection(intent, participant, projection)
    except WireRefused:
        _refuse("authority_projection_mismatch")

    candidate = unsigned["candidate"]
    if isinstance(projection, Mapping) and projection.get("binding_kind") == GROUP_BINDING_KIND:
        # W578: a card group. Every aggregate field is recomputed from the members
        # (binding_ref, target_scope, the fixed revisions, the digest), never trusted.
        from service_foundation.coordination.durable_decision_log import DecisionRefused

        from .card_group import verify_group_projection
        try:
            verify_group_projection(projection, candidate)
        except DecisionRefused:
            _refuse("authority_candidate_invalid")
    elif isinstance(projection, Mapping) and projection.get("binding_kind") == READ_SET_BINDING_KIND:
        # W578: a read set; its reads and catalog are the projection's dependencies, exactly.
        from service_foundation.coordination.durable_decision_log import DecisionRefused

        from .card_read_set import verify_read_set_projection
        try:
            verify_read_set_projection(projection, candidate)
        except DecisionRefused:
            _refuse("authority_candidate_invalid")
    elif isinstance(projection, Mapping) and projection.get("binding_kind") == READ_COLLECTION_BINDING_KIND:
        # W502 lane D: a bounded reference to a Hub-sealed collection; every field compared.
        from service_foundation.coordination.durable_decision_log import DecisionRefused

        from .card_read_set import verify_read_collection_projection
        try:
            verify_read_collection_projection(projection, candidate)
        except DecisionRefused:
            _refuse("authority_candidate_invalid")
    elif (type(candidate) is not dict or set(candidate) != CANDIDATE_FIELDS
            or type(candidate["access_id"]) is not str or not candidate["access_id"]
            or candidate["access_id"] != projection["binding_ref"]
            or type(candidate["original_revision"]) is not int
            or candidate["original_revision"] != projection["before_revision"]
            or type(candidate["candidate"]) is not dict
            or candidate["candidate"].get("access_id") != candidate["access_id"]
            or type(candidate["candidate"].get("card_revision")) is not int
            or candidate["candidate"]["card_revision"] != projection["candidate_revision"]
            or type(candidate["effects"]) is not list
            or any(type(effect) is not dict for effect in candidate["effects"])):
        _refuse("authority_candidate_invalid")
    try:
        digest = candidate_value_digest(candidate)
    except Exception:  # noqa: BLE001 - a non-canonical value is refused by name
        _refuse("authority_candidate_invalid")
    if digest != projection["candidate_digest"]:
        _refuse("authority_candidate_mismatch")

    decision, decided_at = unsigned["decision"], unsigned["decided_at"]
    if phase == "stage":
        if decision != "undecided" or decided_at is not None:
            _refuse("authority_decision_invalid")
        if now >= intent.expires_at:
            _refuse("authority_intent_expired")
    elif decision not in ("committed", "aborted") or type(decided_at) is not int or decided_at < 0:
        _refuse("authority_decision_invalid")
    return VerifiedCardAuthority(intent=intent, projection=dict(projection), candidate=dict(candidate),
                                 decision=decision, decided_at=decided_at)


__all__ = ["CANDIDATE_FIELDS", "PROTOCOL", "TransactionAuthorityRefused", "UNSIGNED_FIELDS",
           "VerifiedCardAuthority", "card_authority_signature", "verify_card_authority_v2"]

"""W578: verify a Card transaction record pulled from its external decision authority.

The Hub never trusts what an inbound stage or decision call asserts: it pulls
the transaction's durable intent and recorded decision from the authority that
the Card's own trusted binding names, and stages or materializes only what a
response signed by that authority proves (Ops and Root, 11:29 to 11:32).

Pure functions only. The authority's endpoint and secret are resolved from the
Card's binding by the hosting composition, never from the caller or the record.
The response is bound to this Hub's fresh request echo, so an old response
cannot be replayed and no nonce store is needed.

The envelope name is generic (no PB-aware branch); the record fields are the
ones CodeApp named for problem-board.card-business-transaction.v1. CodeApp
owns the authority's operation and may replace the envelope; this module is
the Hub side of whatever is agreed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time
from dataclasses import dataclass
from typing import Any, Mapping

from ..issuer_gate import change_digest
from ..remote_issuer import issuer_payload_digest
from .model import CardAuthority

PROTOCOL = "card-transaction-authority.v1"
RECORD_SCHEMA = "card-business-transaction.v1"
PHASES = ("stage", "decision")
RECORDED = ("undecided", "committed", "aborted")
MAX_CLOCK_SKEW_SECONDS = 300
MIN_SECRET_BYTES = 32

# The immutable intent: what the authority approved, bound by intent_digest.
INTENT_FIELDS = (
    "transaction_id", "epoch", "participant", "binding_kind", "binding_ref", "actor_subject", "actor_kind",
    "action", "subject_hash", "access_id", "expected_card_revision", "candidate_revision", "candidate_digest",
    "expected_dependency_revisions", "membership_incarnation", "request_id", "context_ref", "expires_at",
    "audience",
)
# What the authority adds per response: the phase asked, its recorded decision,
# this request's echo and the full non-secret candidate (bound by candidate_digest).
RECORD_FIELDS = ("schema", *INTENT_FIELDS, "intent_digest", "phase", "decision", "decided_at", "request_echo",
                 "candidate")
PROOF_FIELDS = ("service_id", "timestamp", "signature")

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class TransactionAuthorityRefused(ValueError):
    """The pulled record does not prove what this phase needs; nothing is staged or materialized."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class VerifiedTransactionRecord:
    record: Mapping[str, Any]
    candidate: CardAuthority
    decision: str

    @property
    def transaction_id(self) -> str:
        return self.record["transaction_id"]

    @property
    def intent_digest(self) -> str:
        return self.record["intent_digest"]


def intent_digest(record: Mapping[str, Any]) -> str:
    return issuer_payload_digest({name: record[name] for name in INTENT_FIELDS})


def _message(service_id: str, timestamp: str, request_echo: str, record: Mapping[str, Any]) -> bytes:
    return "\n".join((PROTOCOL, service_id, timestamp, request_echo, issuer_payload_digest(record))).encode("utf-8")


def _key(secret: str | bytes) -> bytes:
    key = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
    if len(key) < MIN_SECRET_BYTES:
        raise TransactionAuthorityRefused("authority_secret_invalid")
    return key


def _signature(key: bytes, message: bytes) -> str:
    return base64.urlsafe_b64encode(hmac.new(key, message, hashlib.sha256).digest()).rstrip(b"=").decode("ascii")


def sign_transaction_authority(record: Mapping[str, Any], *, secret: str | bytes, service_id: str,
                               now: int | None = None) -> dict[str, Any]:
    """The authority side: seal one response record for the Hub that asked."""

    timestamp = str(int(time.time()) if now is None else int(now))
    payload = dict(record)
    signature = _signature(_key(secret), _message(service_id, timestamp, str(payload["request_echo"]), payload))
    return {**payload, "authority_proof": {"service_id": service_id, "timestamp": timestamp,
                                           "signature": signature}}


def _verify_proof(body: Mapping[str, Any], record: Mapping[str, Any], *, secret: str | bytes,
                  expected_service_id: str, request_echo: str, now: int, max_skew: int) -> None:
    proof = body.get("authority_proof")
    if not isinstance(proof, Mapping) or set(proof) != set(PROOF_FIELDS) or not all(
            type(proof[name]) is str for name in PROOF_FIELDS):
        raise TransactionAuthorityRefused("authority_proof_missing")
    if not expected_service_id or not hmac.compare_digest(proof["service_id"], expected_service_id):
        raise TransactionAuthorityRefused("authority_service_invalid")
    if not proof["timestamp"].isdigit() or abs(now - int(proof["timestamp"])) > max_skew:
        raise TransactionAuthorityRefused("authority_proof_stale")
    expected = _signature(_key(secret), _message(proof["service_id"], proof["timestamp"], request_echo, record))
    if not hmac.compare_digest(proof["signature"], expected):
        raise TransactionAuthorityRefused("authority_signature_invalid")


def _int(value: Any, *, minimum: int) -> bool:
    return type(value) is int and value >= minimum


def verify_transaction_authority(
    body: Any, *, secret: str | bytes, expected_service_id: str, request_echo: str, audience: str,
    transaction_id: str, phase: str, binding: tuple[str, str], subject_hash: str, access_id: str,
    current_revision: int | None = None, expected_intent_digest: str = "", now: int | None = None,
    max_skew: int = MAX_CLOCK_SKEW_SECONDS,
) -> VerifiedTransactionRecord:
    """Verify a pulled record for one phase, or refuse by name.

    ``binding`` is the Card's trusted (issuer_kind, issuer_ref); ``audience``
    is this Hub runtime; ``request_echo`` is the fresh value this Hub sent.
    STAGE needs an unexpired, undecided intent and the Card still at its
    expected revision. DECISION needs a recorded COMMITTED or ABORTED, even
    after the original expiry, and, when given, the local receipt's intent.
    """

    moment = int(time.time()) if now is None else int(now)
    if phase not in PHASES:
        raise TransactionAuthorityRefused("card_transaction_phase_invalid")
    if not isinstance(body, Mapping) or set(body) != {*RECORD_FIELDS, "authority_proof"}:
        raise TransactionAuthorityRefused("card_transaction_record_invalid")
    record = {name: body[name] for name in RECORD_FIELDS}
    # 1. Integrity first: nothing below is read from an unauthenticated response.
    _verify_proof(body, record, secret=secret, expected_service_id=expected_service_id,
                  request_echo=request_echo, now=moment, max_skew=max_skew)
    # 2. The response answers THIS request, for THIS transaction, phase and Hub.
    if record["schema"] != RECORD_SCHEMA:
        raise TransactionAuthorityRefused("card_transaction_record_invalid")
    if not request_echo or record["request_echo"] != request_echo:
        raise TransactionAuthorityRefused("card_transaction_request_mismatch")
    if not _HEX64.match(str(transaction_id)) or record["transaction_id"] != transaction_id:
        raise TransactionAuthorityRefused("card_transaction_id_mismatch")
    if record["phase"] != phase:
        raise TransactionAuthorityRefused("card_transaction_phase_mismatch")
    if not audience or record["audience"] != audience:
        raise TransactionAuthorityRefused("card_transaction_audience_mismatch")
    # 3. The Card's own binding names this authority; the record cannot pick its Card.
    kind, ref = binding
    if not kind or not ref or (record["binding_kind"], record["binding_ref"]) != (kind, ref):
        raise TransactionAuthorityRefused("card_transaction_binding_mismatch")
    if record["subject_hash"] != subject_hash or record["access_id"] != access_id:
        raise TransactionAuthorityRefused("card_transaction_card_mismatch")
    # 4. Shapes, then the digests are recomputed, never taken from the record.
    if not (_int(record["epoch"], minimum=1) and _int(record["expected_card_revision"], minimum=0)
            and _int(record["candidate_revision"], minimum=1) and _int(record["expires_at"], minimum=1)
            and _int(record["decided_at"], minimum=0) and record["decision"] in RECORDED
            and isinstance(record["candidate"], Mapping)
            and isinstance(record["expected_dependency_revisions"], Mapping)):
        raise TransactionAuthorityRefused("card_transaction_record_invalid")
    if (record["decision"] == "undecided") != (record["decided_at"] == 0):
        raise TransactionAuthorityRefused("card_transaction_record_invalid")
    if change_digest(record["candidate"]) != record["candidate_digest"]:
        raise TransactionAuthorityRefused("card_transaction_candidate_digest_mismatch")
    if intent_digest(record) != record["intent_digest"]:
        raise TransactionAuthorityRefused("card_transaction_intent_digest_mismatch")
    if expected_intent_digest and record["intent_digest"] != expected_intent_digest:
        raise TransactionAuthorityRefused("card_transaction_intent_mismatch")
    try:
        candidate = CardAuthority.from_mapping(record["candidate"])
    except Exception as exc:  # noqa: BLE001 - any malformed candidate is not authority
        raise TransactionAuthorityRefused("card_transaction_candidate_invalid") from exc
    if (candidate.access_id != access_id or candidate.card_revision != record["candidate_revision"]
            or record["candidate_revision"] != record["expected_card_revision"] + 1):
        raise TransactionAuthorityRefused("card_transaction_candidate_invalid")
    # 5. The phase rule.
    if phase == "stage":
        if record["decision"] != "undecided":
            raise TransactionAuthorityRefused("card_transaction_late_stage")
        if moment >= record["expires_at"]:
            raise TransactionAuthorityRefused("card_transaction_intent_expired")
        if current_revision is None or current_revision != record["expected_card_revision"]:
            raise TransactionAuthorityRefused("card_transaction_revision_moved")
    elif record["decision"] == "undecided":
        raise TransactionAuthorityRefused("card_transaction_undecided")
    return VerifiedTransactionRecord(record=record, candidate=candidate, decision=record["decision"])


__all__ = ["INTENT_FIELDS", "PROTOCOL", "RECORD_FIELDS", "RECORD_SCHEMA", "TransactionAuthorityRefused",
           "VerifiedTransactionRecord", "intent_digest", "sign_transaction_authority",
           "verify_transaction_authority"]

"""W502: the Hub's Card participant, called by another application's coordinator.

When Problem Board (or any configured application) initiates a Card
transaction, its kernel ``Coordinator`` drives the Hub participant through
this one operation, ``card_transaction_participant``. The contract is
CodeApp's (18:53) with EMain's review requirements (18:46):

- Request ``card-transaction-participant.v1``: exactly ``schema, action,
  request_echo, scope, transaction_id, decision, limit, cursor`` plus
  ``service_proof``, unused fields present as null. It is authenticated with
  the existing admission proof (``AdmissionRequest`` over the request digest)
  and a durable single-use nonce. The CALLER is the proof's service id; it
  alone selects the configured authority, keys, audience and scope field.
- Answer ``{ok, participant_answer}``: ``card-transaction-participant-receipt.v1``
  echoing every request field and the request digest, with a tagged
  ``result`` (``receipt``, ``pending``, ``page`` or ``refused``) and a
  ``receipt_proof`` HMAC over the newline join of schema, signer id,
  timestamp, echo and the digest of every unsigned answer field. Every
  authenticated answer is signed, refusals included; an unauthenticated
  request gets an unsigned refusal, which the caller treats as transport
  unavailable.

The Hub never trusts the caller's claims: the scope must equal the configured
scope field of the intent the AUTHORITY verified, and ``finish`` applies only
the decision the authority verified. The Hub stays generic: the scope field
name (``project_ref`` for Problem Board) comes from the caller's descriptor.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping

from service_foundation.coordination.durable_decision_log import DecisionRefused
from service_foundation.coordination.durable_wire import canonical_json_bytes, sha256_hex

from ..admission import AdmissionRequest, ServiceProof, verify_admission_request
from .authority_intent_source import intent_scope
from .card_participant import LocalCardIntentSource
from .transaction_authority_v2 import TransactionAuthorityRefused

OPERATION = "card_transaction_participant"
REQUEST_SCHEMA = "card-transaction-participant.v1"
ANSWER_SCHEMA = "card-transaction-participant-receipt.v1"
DIRECTION = "hub-to-authority"
REQUEST_FIELDS = ("schema", "action", "request_echo", "scope", "transaction_id", "decision", "limit", "cursor")
PROOF_FIELDS = frozenset({"service_id", "timestamp", "nonce", "signature"})
ACTIONS = frozenset({"prepare", "finish", "read_pending", "list_prepared"})
MIN_SECRET_BYTES = 32
MAX_SKEW_SECONDS = 300
_ECHO = re.compile(r"[0-9a-f]{32,128}\Z")
_BOUNDED = 256
# The Hub reasons an answer may carry; anything else is card_participant_refused.
HUB_REFUSALS = frozenset({
    "transaction_unknown", "card_intent_unknown", "card_intent_not_bound", "card_intent_base_moved",
    "card_intent_invalid", "card_decision_mismatch", "card_dependency_moved", "card_dependency_reserved",
    "card_dependency_invalid", "card_transaction_undecided", "card_transaction_prepared",
    "card_transaction_unresolved", "card_transaction_staged_revision_mismatch", "card_transaction_not_committed",
    "card_transactions_unavailable", "card_participant_cursor_invalid", "card_participant_timeout",
    "card_participant_unavailable", "authority_decision_pending", "authority_late_stage",
    "authority_intent_expired", "authority_transaction_unknown", "authority_unavailable",
    "authority_intent_mismatch", "authority_refused",
})
_STATUS = {"transaction_unknown": 404, "authority_transaction_unknown": 404, "card_intent_unknown": 404,
           "card_transactions_unavailable": 503,
           "card_participant_unavailable": 503, "authority_unavailable": 503, "card_participant_timeout": 504}


@dataclass(frozen=True)
class ParticipantCaller:
    """One configured application allowed to drive the Hub participant (from the trusted descriptor)."""

    service_id: str                 # the authenticated peer id that selects this entry
    request_secret: str | bytes = field(repr=False)   # verifies the caller's admission proof
    receipt_secret: str | bytes = field(repr=False)   # signs the Hub's answers to this caller
    receipt_signer_id: str          # the Hub's signer id in receipt_proof
    audience: str                   # the caller's bundle, bound into every answer
    hub_resource: str               # the Hub bundle the caller's AdmissionRequest names
    participant: Any                # a HubCardParticipant over this caller's authority
    decisions: Any                  # this caller's AuthorityDecisionReader
    scope_field: str = ""           # the verified intent payload key the request scope must equal

    def __post_init__(self) -> None:
        for secret in (self.request_secret, self.receipt_secret):
            raw = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret or b"")
            if len(raw) < MIN_SECRET_BYTES:
                raise ValueError("participant_caller_secret_too_short")
        if not all(type(value) is str and value for value in (
                self.service_id, self.receipt_signer_id, self.audience, self.hub_resource)):
            raise ValueError("participant_caller_invalid")


def answer_signature(unsigned: Mapping[str, Any], *, secret: str | bytes, service_id: str, timestamp: str) -> str:
    """HMAC-SHA256 over the newline join (CodeApp 18:53), base64url without padding."""
    key = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
    message = "\n".join((ANSWER_SCHEMA, service_id, timestamp, unsigned["request_echo"],
                         sha256_hex(canonical_json_bytes(dict(unsigned))))).encode("utf-8")
    return base64.urlsafe_b64encode(hmac.new(key, message, hashlib.sha256).digest()).rstrip(b"=").decode("ascii")


def request_digest(request: Mapping[str, Any]) -> str:
    return sha256_hex(canonical_json_bytes({name: request[name] for name in REQUEST_FIELDS}))


def _bounded(value: Any) -> bool:
    return type(value) is str and 0 < len(value) <= _BOUNDED and value.isprintable()


def _valid_request(data: Any) -> bool:
    if not isinstance(data, Mapping) or set(data) != set(REQUEST_FIELDS) | {"service_proof"}:
        return False
    proof = data["service_proof"]
    if (not isinstance(proof, Mapping) or set(proof) != PROOF_FIELDS
            or any(type(proof[name]) is not str for name in PROOF_FIELDS)):
        return False
    action, txid, decision, limit, cursor = (data[name] for name in (
        "action", "transaction_id", "decision", "limit", "cursor"))
    if (data["schema"] != REQUEST_SCHEMA or action not in ACTIONS or type(data["request_echo"]) is not str
            or not _ECHO.fullmatch(data["request_echo"]) or not _bounded(data["scope"])):
        return False
    if action in ("prepare", "read_pending"):
        return _bounded(txid) and decision is None and limit is None and cursor is None
    if action == "finish":
        return _bounded(txid) and decision in ("committed", "aborted") and limit is None and cursor is None
    return (txid is None and decision is None and type(limit) is int and 1 <= limit <= 1000
            and (cursor is None or _bounded(cursor)))


def _unsigned_refusal(code: str, status: int) -> dict[str, Any]:
    return {"ok": False, "status": status, "error": {"code": code}}


class _Refused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code if code in HUB_REFUSALS else "card_participant_refused"


class CardTransactionParticipantOperation:
    """``answer(data)`` for ``card_transaction_participant``; bound by the composition root."""

    def __init__(self, *, callers: Mapping[str, ParticipantCaller], card_store: Any, nonces: Any,
                 enabled: bool, clock: Callable[[], float], budget_seconds: float = 20.0) -> None:
        self._callers = dict(callers)
        self._card_store = card_store
        self._intents = LocalCardIntentSource(card_store)
        self._nonces = nonces
        self._enabled = enabled is True
        self._clock = clock
        self._budget = budget_seconds

    async def answer(self, data: Any) -> dict[str, Any]:
        if not _valid_request(data):
            return _unsigned_refusal("card_participant_request_invalid", 400)
        raw_proof = data["service_proof"]
        caller = self._callers.get(raw_proof["service_id"])
        if caller is None:
            return _unsigned_refusal("card_participant_caller_unknown", 403)
        digest = request_digest(data)
        proof = ServiceProof(service_id=raw_proof["service_id"], timestamp=raw_proof["timestamp"],
                             nonce=raw_proof["nonce"], signature=raw_proof["signature"])
        verdict = verify_admission_request(
            secret=caller.request_secret, proof=proof, delegated_token=f"{REQUEST_SCHEMA}:{data['request_echo']}",
            request=AdmissionRequest(resource=caller.hub_resource, operation=OPERATION,
                                     invocation_id=data["request_echo"], request_digest=digest,
                                     approval_context={"protocol": REQUEST_SCHEMA}),
            max_clock_skew_seconds=MAX_SKEW_SECONDS, now=int(self._clock()))
        if not verdict.allowed:
            return _unsigned_refusal("card_participant_unauthenticated", 401)
        try:
            fresh = await self._nonces.set(f"connection-hub:card-participant:nonce:{caller.service_id}:{proof.nonce}",
                                           "1", ex=2 * MAX_SKEW_SECONDS, nx=True)
        except Exception:  # noqa: BLE001 - no replay protection, no answer
            return _unsigned_refusal("card_participant_unavailable", 503)
        if not fresh:
            return _unsigned_refusal("card_participant_unauthenticated", 401)
        try:
            result = await asyncio.wait_for(self._dispatch(caller, data), timeout=self._budget)
        except asyncio.TimeoutError:
            result = self._refusal("card_participant_timeout")
        except _Refused as exc:
            result = self._refusal(exc.code)
        except TransactionAuthorityRefused as exc:
            result = self._refusal(exc.reason)
        except DecisionRefused as exc:
            result = self._refusal(str(exc))
        except Exception:  # noqa: BLE001 - authenticated, so signed; never internal text
            result = self._refusal("card_participant_unavailable")
        return self._signed(caller, data, digest, result)

    @staticmethod
    def _refusal(code: str) -> dict[str, Any]:
        code = code if code in HUB_REFUSALS else "card_participant_refused"
        return {"kind": "refused", "code": code, "status": _STATUS.get(code, 409)}

    def _signed(self, caller: ParticipantCaller, data: Mapping[str, Any], digest: str,
                result: Mapping[str, Any]) -> dict[str, Any]:
        unsigned = {"schema": ANSWER_SCHEMA, "direction": DIRECTION, "audience": caller.audience,
                    "request_echo": data["request_echo"], "request_digest": digest,
                    **{name: data[name] for name in ("action", "scope", "transaction_id", "decision", "limit",
                                                     "cursor")},
                    "result": dict(result)}
        timestamp = str(int(self._clock()))
        proof = {"service_id": caller.receipt_signer_id, "timestamp": timestamp,
                 "signature": answer_signature(unsigned, secret=caller.receipt_secret,
                                               service_id=caller.receipt_signer_id, timestamp=timestamp)}
        return {"ok": result.get("kind") != "refused", "participant_answer": {**unsigned, "receipt_proof": proof}}

    async def _local_intent(self, transaction_id: str):
        try:
            return await self._intents.load(transaction_id)
        except DecisionRefused as exc:
            if str(exc) != "card_intent_unknown":
                raise
            return None

    async def _bound(self, caller: ParticipantCaller, transaction_id: str, scope: str):
        """The authority-verified record, refused unless its scope and any local intent belong to this caller."""
        record = await caller.decisions.read(transaction_id)
        if record is None:
            raise _Refused("transaction_unknown")
        if caller.scope_field and intent_scope(record.intent, caller.scope_field) != scope:
            raise _Refused("card_intent_not_bound")
        local = await self._local_intent(transaction_id)
        if local is not None and (local.authority != caller.service_id or local.scope != (
                scope if caller.scope_field else "")):
            raise _Refused("card_intent_not_bound")
        return record

    async def _list_prepared(self, caller: ParticipantCaller, scope: str, limit: int,
                             cursor: str | None) -> dict[str, Any]:
        """This caller's and scope's prepared receipts, in transaction-id order after ``cursor``.

        The partition is the intents recorded with this caller's authority and
        verified scope at prepare, so it survives a restart. The underlying
        in-doubt list is complete or fails closed (never truncated), so a page
        is never a filtered partial list passed off as complete. A cursor must
        name a transaction of this same partition.
        """
        from .transaction_store import list_in_doubt

        expected_scope = scope if caller.scope_field else ""

        def owned(intent: Any) -> bool:
            return intent is not None and intent.authority == caller.service_id and intent.scope == expected_scope

        if cursor is not None and not owned(await self._local_intent(cursor)):
            raise _Refused("card_participant_cursor_invalid")
        listed = sorted(entry["transaction_id"] for entry in await list_in_doubt(self._card_store))
        receipts: list[dict[str, Any]] = []
        next_cursor = None
        for transaction_id in listed:
            if cursor is not None and transaction_id <= cursor:
                continue
            if not owned(await self._local_intent(transaction_id)):
                continue
            pending = await caller.participant.read_pending(transaction_id)
            if pending is None:
                continue
            if len(receipts) == limit:
                next_cursor = receipts[-1]["transaction_id"]
                break
            receipts.append(asdict(pending))
        return {"kind": "page", "receipts": receipts, "next_cursor": next_cursor}

    async def _dispatch(self, caller: ParticipantCaller, data: Mapping[str, Any]) -> dict[str, Any]:
        action, txid, scope = data["action"], data["transaction_id"], data["scope"]
        if action == "list_prepared":
            return await self._list_prepared(caller, scope, data["limit"], data["cursor"])
        if action == "prepare":
            if not self._enabled:
                raise _Refused("card_transactions_unavailable")
            await self._bound(caller, txid, scope)
            return {"kind": "receipt", "receipt": asdict(await caller.participant.prepare(txid))}
        if action == "read_pending":
            await self._bound(caller, txid, scope)
            pending = await caller.participant.read_pending(txid)
            return {"kind": "pending", "receipt": asdict(pending) if pending is not None else None}
        record = await self._bound(caller, txid, scope)
        if not record.terminal or record.state != data["decision"]:
            # Only the decision the authority verified is ever applied (EMain 18:46 #3).
            raise _Refused("card_decision_mismatch")
        return {"kind": "receipt", "receipt": asdict(await caller.participant.finish(txid, record.state))}


class RoutedDecisionPort:
    """The Card store's decision port: each staged intent's decision comes from the authority that staged it.

    An intent recorded with ``authority == ""`` was staged by the Hub's own
    coordinator and reads the Hub's decision store; one recorded by a
    configured caller reads that caller's verified authority. An unknown
    authority decides nothing (``undecided``), so the Card stays unreadable
    rather than guessing.
    """

    def __init__(self, *, local: Any, card_store: Any, authorities: Mapping[str, Any]) -> None:
        self._local = local
        self._intents = LocalCardIntentSource(card_store)
        self._authorities = dict(authorities)

    async def decision(self, receipt: Mapping[str, Any]) -> str:
        try:
            intent = await self._intents.load(receipt["transaction_id"])
            authority = intent.authority
        except DecisionRefused:
            authority = ""
        reader = self._local if not authority else self._authorities.get(authority)
        if reader is None:
            return "undecided"
        record = await reader.read(receipt["transaction_id"])
        if record is None or record.intent.digest != receipt["intent_digest"]:
            return "undecided"
        return record.state if record.terminal else "undecided"


__all__ = ["ANSWER_SCHEMA", "CardTransactionParticipantOperation", "DIRECTION", "HUB_REFUSALS", "OPERATION",
           "ParticipantCaller", "REQUEST_FIELDS", "REQUEST_SCHEMA", "RoutedDecisionPort", "answer_signature",
           "request_digest"]

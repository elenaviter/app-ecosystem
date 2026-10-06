"""W578: the Hub's Card participant in the ONE generalized commit protocol (W581 kernel).

The generic ``Coordinator`` runs prepare, one durable decision and finish for
one participant or several; this module is the Hub's ``Participant`` for it.
It never takes a candidate from the caller of ``prepare``: it resolves the
transaction's exact intent BY TRANSACTION ID from a trusted intent source,
stages it through the existing W578 participant (fence, serving marker,
prepared receipt with its effects), and on ``finish`` materializes exactly
the decision the coordinator's store recorded (applying effects only for
COMMITTED). One-Card and multi-Card edits use this same code.

Intent sources:
- ``LocalCardIntentSource``: an edit the Hub itself initiates writes its
  immutable intent record (candidate, effects, actor) before the coordinator
  prepares; the participant reads only that record.
- A verified v2 pull from the binding's authority (H-T) is the second
  implementation of the same port, for edits another application initiates.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, Sequence

from service_foundation.coordination.durable_decision_log import DecisionRefused, Receipt

from ..durable_io import read_json_or_none, write_json_atomic
from .model import CardAuthority
from .transaction_store import CardTransactionRefused, list_in_doubt, state as read_state

PARTICIPANT = "connection-hub.card"
INTENT_RECORD_SCHEMA = "connection-hub.card-intent.v1"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def receipt_digest(receipt: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(dict(receipt))).hexdigest()


@dataclass(frozen=True)
class CardIntent:
    """What one transaction may do to one Card; immutable once recorded."""

    transaction_id: str
    intent_digest: str
    subject_hash: str
    original: CardAuthority
    candidate: CardAuthority
    effects: tuple[Mapping[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"schema": INTENT_RECORD_SCHEMA, "transaction_id": self.transaction_id,
                "intent_digest": self.intent_digest, "subject_hash": self.subject_hash,
                "original": self.original.to_dict(), "candidate": self.candidate.to_dict(),
                "effects": [dict(effect) for effect in self.effects]}

    @classmethod
    def from_mapping(cls, raw: Any) -> "CardIntent":
        if not isinstance(raw, Mapping) or raw.get("schema") != INTENT_RECORD_SCHEMA:
            raise DecisionRefused("card_intent_invalid")
        try:
            return cls(transaction_id=raw["transaction_id"], intent_digest=raw["intent_digest"],
                       subject_hash=raw["subject_hash"], original=CardAuthority.from_mapping(raw["original"]),
                       candidate=CardAuthority.from_mapping(raw["candidate"]),
                       effects=tuple(dict(effect) for effect in raw.get("effects") or ()))
        except (KeyError, TypeError, ValueError) as exc:
            raise DecisionRefused("card_intent_invalid") from exc


class CardIntentSource(Protocol):
    async def load(self, transaction_id: str) -> CardIntent: ...


class LocalCardIntentSource:
    """The durable intent of an edit the Hub itself initiates; written once, before prepare."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def _path(self, transaction_id: str):
        from .transaction_store import _checked_id
        return self._store.root / "card-transactions" / "intents" / f"{_checked_id(transaction_id)}.json"

    async def record(self, intent: CardIntent) -> None:
        path = self._path(intent.transaction_id)
        existing = await read_json_or_none(path)
        if existing is not None:
            if existing != intent.to_dict():
                raise DecisionRefused("card_intent_conflict")  # immutable: an exact replay only
            return
        await write_json_atomic(path, intent.to_dict())

    async def load(self, transaction_id: str) -> CardIntent:
        raw = await read_json_or_none(self._path(transaction_id))
        if raw is None:
            raise DecisionRefused("card_intent_unknown")
        intent = CardIntent.from_mapping(raw)
        if intent.transaction_id != transaction_id:
            raise DecisionRefused("card_intent_invalid")
        return intent


class DecisionStorePort:
    """The Hub participant's TransactionDecisionPort over the coordinator's own store."""

    def __init__(self, store: Any) -> None:
        self._store = store

    async def decision(self, receipt: Mapping[str, Any]) -> str:
        record = await self._store.read(receipt["transaction_id"])
        if record is None or record.intent.digest != receipt["intent_digest"]:
            return "undecided"
        return record.state if record.terminal else "undecided"


class HubCardParticipant:
    """The Hub's ``Participant``; bound by the composition root, never chosen by a caller."""

    name = PARTICIPANT

    def __init__(self, *, service: Any, store: Any, intents: CardIntentSource,
                 now: Any = None) -> None:
        self._service = service
        self._store = store
        self._intents = intents
        self._now = now or (lambda: datetime.now(timezone.utc))

    def _receipt(self, prepared: Mapping[str, Any]) -> Receipt:
        return Receipt(prepared["transaction_id"], prepared["intent_digest"], PARTICIPANT,
                       receipt_digest(prepared))

    async def prepare(self, transaction_id: str) -> Receipt:
        intent = await self._intents.load(transaction_id)
        try:
            prepared = await self._service.stage_transaction(
                transaction_id=transaction_id, intent_digest=intent.intent_digest, participant=PARTICIPANT,
                subject_hash=intent.subject_hash, original=intent.original, candidate=intent.candidate,
                now=self._now(), effects=intent.effects)
        except CardTransactionRefused as exc:
            raise DecisionRefused(str(exc)) from exc
        return self._receipt(prepared)

    async def finish(self, transaction_id: str, decision: str) -> Receipt:
        intent = await self._intents.load(transaction_id)
        if decision == "aborted" and await read_state(self._store, transaction_id=transaction_id) is None:
            # Never durably prepared here (a lost prepare reply, or a stage
            # that crashed first): an idempotent abort tombstone (W581 F1).
            from .transaction_store import abort_unstaged
            tombstone = await abort_unstaged(self._store, transaction_id)
            return Receipt(transaction_id, intent.intent_digest, PARTICIPANT, receipt_digest(tombstone))
        try:
            decided = await self._service.decide_transaction(
                transaction_id=transaction_id, intent_digest=intent.intent_digest, decision=decision,
                subject_hash=intent.subject_hash, access_id=intent.original.access_id)
        except CardTransactionRefused as exc:
            raise DecisionRefused(str(exc)) from exc
        return self._receipt(decided)

    async def read_pending(self, transaction_id: str) -> Receipt | None:
        receipt = await read_state(self._store, transaction_id=transaction_id)
        return self._receipt(receipt) if receipt is not None and receipt["state"] == "prepared" else None

    async def list_prepared(self, *, limit: int) -> Sequence[Receipt]:
        listed = await list_in_doubt(self._store)
        if len(listed) > limit:
            raise DecisionRefused("recovery_unbounded")
        result = []
        for entry in listed:
            receipt = await read_state(self._store, transaction_id=entry["transaction_id"])
            if receipt is not None and receipt["state"] == "prepared":
                result.append(self._receipt(receipt))
        return result


__all__ = ["CardIntent", "CardIntentSource", "DecisionStorePort", "HubCardParticipant",
           "LocalCardIntentSource", "PARTICIPANT", "receipt_digest"]

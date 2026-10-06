"""One durable decision protocol for one or several prepared participants.

The coordinator never interprets an application's authority or candidate. The
store owns durable compare-and-set, the participant owns its staged effects,
and the verifier authenticates receipts from the participant's own realm.
Neither a timeout nor an unreachable realm is a decision.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Protocol, Sequence

TERMINAL = frozenset({"committed", "aborted"})
STATES = frozenset({"preparing", "prepared", *TERMINAL})


class DecisionRefused(ValueError):
    """An exact identity, state transition, or durable proof is unavailable."""


@dataclass(frozen=True)
class Intent:
    """Opaque original request binding; ``participants`` is an ordered set.

    The application retains the signed full candidate behind ``payload_digest``.
    A participant resolves that trusted binding by transaction ID; the caller
    cannot select an authority or candidate with a later prepare argument.
    """

    actor: str
    request_id: str
    context: str
    payload_digest: str
    participants: tuple[str, ...]
    expires_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.participants, (tuple, list)):
            raise DecisionRefused("intent_invalid")
        object.__setattr__(self, "participants", tuple(self.participants))
        if (not all(type(value) is str and value for value in
                    (self.actor, self.request_id, self.context))
                or len(self.payload_digest) != 64
                or any(char not in "0123456789abcdef" for char in self.payload_digest)
                or not self.participants
                or any(type(name) is not str or not name for name in self.participants)
                or len(set(self.participants)) != len(self.participants)
                or not isinstance(self.expires_at, datetime)
                or self.expires_at.tzinfo is None
                or self.expires_at.utcoffset() is None):
            raise DecisionRefused("intent_invalid")

    @property
    def canonical_bytes(self) -> bytes:
        """Persist these bytes, not a JSONB round-trip, for identity/digest use."""

        value = {"actor": self.actor, "request_id": self.request_id,
                 "context": self.context, "payload_digest": self.payload_digest,
                 "participants": list(self.participants),
                 "expires_at": self.expires_at.astimezone(timezone.utc).isoformat()}
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()


@dataclass(frozen=True)
class Receipt:
    transaction_id: str
    intent_digest: str
    participant: str
    receipt_digest: str


@dataclass(frozen=True)
class DecisionRecord:
    transaction_id: str
    intent: Intent
    state: str
    prepared: Mapping[str, Receipt]
    finished: Mapping[str, Receipt]

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL


class DecisionStore(Protocol):
    """Injected durable CAS adapter; PB may back this with its existing v42 table.

    ``begin`` binds the original request to exactly one transaction/expiry.
    ``record_prepared`` is an immutable receipt CAS and refuses late stages.
    ``decide`` is the one monotonic terminal CAS: commit requires every exact
    prepared receipt and a trusted application witness; an abort after expiry
    may only CAS an undecided row. Same-decision replay returns the old row.
    ``record_finished`` persists each verified finish receipt idempotently.
    The list is bounded and fails closed rather than silently truncating.
    """

    async def begin(self, intent: Intent) -> DecisionRecord: ...

    async def read(self, transaction_id: str) -> DecisionRecord | None: ...

    async def record_prepared(self, receipt: Receipt) -> DecisionRecord: ...

    async def decide(self, transaction_id: str, decision: str,
                     *, witness_digest: str = "") -> DecisionRecord: ...

    async def abort_expired(self, transaction_id: str) -> DecisionRecord: ...

    async def record_finished(self, receipt: Receipt) -> DecisionRecord: ...

    async def list_in_doubt(self, *, limit: int) -> Sequence[DecisionRecord]: ...


class Participant(Protocol):
    """Trusted realm adapter bound by the composition root, not caller input."""

    async def prepare(self, transaction_id: str) -> Receipt: ...

    async def finish(self, transaction_id: str, decision: str) -> Receipt: ...

    async def read_pending(self, transaction_id: str) -> Receipt | None: ...

    async def list_prepared(self, *, limit: int) -> Sequence[Receipt]: ...


class ReceiptVerifier(Protocol):
    """Authenticates realm receipts and exact intent/participant binding."""

    async def prepared(self, record: DecisionRecord, receipt: Receipt) -> None: ...

    async def finished(self, record: DecisionRecord, receipt: Receipt) -> None: ...


class Coordinator:
    """Runs the same durable path for one participant or many."""

    def __init__(self, store: DecisionStore, participants: Mapping[str, Participant],
                 verifier: ReceiptVerifier) -> None:
        self.store = store
        self.participants = dict(participants)
        self.verifier = verifier

    def _participant(self, name: str) -> Participant:
        try:
            return self.participants[name]
        except KeyError as exc:
            raise DecisionRefused("participant_unavailable") from exc

    async def prepare(self, intent: Intent) -> DecisionRecord:
        record = await self.store.begin(intent)
        if record.intent != intent or record.state not in STATES:
            raise DecisionRefused("intent_conflict")
        if record.terminal:
            return record
        for name in intent.participants:
            if name in record.prepared:
                continue
            receipt = await self._participant(name).prepare(record.transaction_id)
            if (receipt.transaction_id != record.transaction_id
                    or receipt.intent_digest != intent.digest or receipt.participant != name):
                raise DecisionRefused("prepared_receipt_mismatch")
            await self.verifier.prepared(record, receipt)
            record = await self.store.record_prepared(receipt)
        return record

    async def decide(self, transaction_id: str, decision: str,
                     *, witness_digest: str = "") -> DecisionRecord:
        if decision not in TERMINAL:
            raise DecisionRefused("decision_invalid")
        record = await self.store.read(transaction_id)
        if record is None:
            raise DecisionRefused("transaction_unknown")
        if (decision == "committed" and
                set(record.prepared) != set(record.intent.participants)):
            raise DecisionRefused("participants_not_prepared")
        return await self.store.decide(transaction_id, decision,
                                       witness_digest=witness_digest)

    async def finish(self, transaction_id: str) -> DecisionRecord:
        record = await self.store.read(transaction_id)
        if record is None or not record.terminal:
            raise DecisionRefused("decision_unknown")
        for name in record.intent.participants:
            if name in record.finished or name not in record.prepared:
                continue
            receipt = await self._participant(name).finish(transaction_id, record.state)
            if (receipt.transaction_id != transaction_id
                    or receipt.intent_digest != record.intent.digest
                    or receipt.participant != name):
                raise DecisionRefused("finish_receipt_mismatch")
            await self.verifier.finished(record, receipt)
            record = await self.store.record_finished(receipt)
        return record

    async def recover(self, *, limit: int = 100) -> list[DecisionRecord]:
        if not 1 <= limit <= 1000:
            raise DecisionRefused("recovery_limit_invalid")
        rows = await self.store.list_in_doubt(limit=limit)
        if len(rows) > limit:
            raise DecisionRefused("recovery_unbounded")
        result = []
        for row in rows:
            if row.terminal:
                result.append(await self.finish(row.transaction_id))
            else:
                # Only the store can compare original expiry to its durable
                # clock and record a presumed abort. A missing answer is not
                # permission to release a participant fence.
                result.append(await self.store.abort_expired(row.transaction_id))
        return result


__all__ = ["Coordinator", "DecisionRecord", "DecisionRefused", "DecisionStore",
           "Intent", "Participant", "Receipt", "ReceiptVerifier"]

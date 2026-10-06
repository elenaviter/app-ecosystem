"""One durable decision protocol for one or several prepared participants.

The coordinator never interprets an application's authority or candidate. The
store owns durable compare-and-set, the participant owns its staged effects,
and the verifier authenticates receipts from the participant's own realm.
Neither a timeout nor an unreachable realm is a decision.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, Sequence

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
    witness_digest: str = ""

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
    ``record_finished`` persists each verified finish receipt idempotently;
    concurrent recovery loops may finish the same participant. ``abort_expired``
    refuses with ``DecisionRefused('not_expired')`` while the original approval
    remains live. ``list_in_doubt`` returns at most ``limit`` rows only after
    fetching ``limit + 1`` internally; excess must raise, never truncate.
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
    """Trusted realm adapter bound by the composition root, not caller input.

    ``finish`` is idempotent across crash replay and concurrent recovery. On
    ABORT it also acknowledges a transaction that never staged locally, so a
    missing central prepare receipt cannot strand an orphaned realm fence.
    """

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
        if record.terminal:
            if record.state != decision or (decision == "committed" and
                                            record.witness_digest != witness_digest):
                raise DecisionRefused("decision_conflict")
            return record
        if (decision == "committed" and
                set(record.prepared) != set(record.intent.participants)):
            raise DecisionRefused("participants_not_prepared")
        if (decision == "committed" and
                (len(witness_digest) != 64 or
                 any(char not in "0123456789abcdef" for char in witness_digest))):
            raise DecisionRefused("commit_witness_missing")
        result = await self.store.decide(transaction_id, decision,
                                         witness_digest=witness_digest)
        if result.state != decision or (decision == "committed" and
                                        result.witness_digest != witness_digest):
            raise DecisionRefused("decision_record_mismatch")
        return result

    async def finish(self, transaction_id: str) -> DecisionRecord:
        record = await self.store.read(transaction_id)
        if record is None or not record.terminal:
            raise DecisionRefused("decision_unknown")
        for name in record.intent.participants:
            if name in record.finished:
                continue
            # ABORT may race a stage whose realm receipt was written but not
            # copied into the decision store before a crash. Every named
            # participant must acknowledge the terminal decision or an exact
            # no-stage observation; an absent central receipt is not release.
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
                try:
                    decided = await self.store.abort_expired(row.transaction_id)
                except DecisionRefused as exc:
                    if str(exc) != "not_expired":
                        raise
                    result.append(row)
                    continue
                result.append(await self.finish(row.transaction_id)
                              if decided.terminal else decided)
        return result


_SQL_NAME = re.compile(r"[a-z_][a-z0-9_]*\Z")


class PostgresDecisionStore:
    """Reusable PostgreSQL implementation of :class:`DecisionStore`.

    A hosting application supplies its own pool, schema and workflow namespace.
    The same table layout and CAS code serves every initiator; applications
    keep their business witness, participant storage and recovery schedule.
    ``ensure_schema`` is idempotent and must run before traffic. No connection
    is held across a participant call.
    """

    def __init__(self, pool: Any, *, schema: str, namespace: str) -> None:
        if not _SQL_NAME.fullmatch(schema) or not namespace or type(namespace) is not str:
            raise DecisionRefused("store_scope_invalid")
        self.pool = pool
        self.schema = schema
        self.namespace = namespace
        self.table = f"{schema}.service_foundation_decisions"

    async def ensure_schema(self) -> None:
        async with self.pool.acquire() as connection:
            await connection.execute(f"""CREATE TABLE IF NOT EXISTS {self.table} (
                namespace TEXT NOT NULL,
                transaction_id TEXT NOT NULL,
                actor TEXT NOT NULL,
                request_id TEXT NOT NULL,
                context TEXT NOT NULL,
                intent_bytes BYTEA NOT NULL,
                intent_digest TEXT NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL,
                participant_count INTEGER NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('preparing','prepared','committed','aborted')),
                prepared JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                finished JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                finished_count INTEGER NOT NULL DEFAULT 0,
                witness_digest TEXT NOT NULL DEFAULT '',
                decided_at TIMESTAMPTZ,
                PRIMARY KEY (namespace,transaction_id),
                UNIQUE (namespace,context,actor,request_id))""")
            await connection.execute(f"""CREATE INDEX IF NOT EXISTS
                service_foundation_decisions_in_doubt
                ON {self.table} (namespace,transaction_id)
                WHERE state IN ('preparing','prepared') OR finished_count<participant_count""")

    @staticmethod
    def _decode(row: Any) -> DecisionRecord | None:
        if row is None:
            return None
        raw = json.loads(bytes(row["intent_bytes"]))
        intent = Intent(raw["actor"], raw["request_id"], raw["context"],
                        raw["payload_digest"], tuple(raw["participants"]),
                        datetime.fromisoformat(raw["expires_at"]))
        if (intent.canonical_bytes != bytes(row["intent_bytes"])
                or intent.digest != row["intent_digest"]):
            raise DecisionRefused("stored_intent_invalid")
        return DecisionRecord(
            row["transaction_id"], intent, row["state"],
            {key: Receipt(**value) for key, value in json.loads(row["prepared"]).items()},
            {key: Receipt(**value) for key, value in json.loads(row["finished"]).items()},
            row["witness_digest"])

    async def _row(self, connection: Any, transaction_id: str, *, lock: bool = False) -> Any:
        return await connection.fetchrow(
            f"SELECT * FROM {self.table} WHERE namespace=$1 AND transaction_id=$2"
            f" {'FOR UPDATE' if lock else ''}", self.namespace, transaction_id)

    async def begin(self, intent: Intent) -> DecisionRecord:
        # Freeze the complete canonical bytes before the first await. A replay
        # never changes identity, participant order, payload digest or expiry.
        canonical = intent.canonical_bytes
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                f"""INSERT INTO {self.table}
                    (namespace,transaction_id,actor,request_id,context,intent_bytes,
                     intent_digest,expires_at,participant_count,state)
                    SELECT $1,$2,$3,$4,$5,$6,$7,$8,$9,'preparing'
                    WHERE $8::timestamptz>clock_timestamp()
                    ON CONFLICT (namespace,context,actor,request_id) DO NOTHING
                    RETURNING *""",
                self.namespace, secrets.token_hex(32), intent.actor, intent.request_id,
                intent.context, canonical, intent.digest, intent.expires_at,
                len(intent.participants))
            if row is not None:
                return self._decode(row)
            previous = await connection.fetchrow(
                f"""SELECT * FROM {self.table}
                    WHERE namespace=$1 AND context=$2 AND actor=$3 AND request_id=$4""",
                self.namespace, intent.context, intent.actor, intent.request_id)
            if previous is None:
                raise DecisionRefused("intent_expired")
            if bytes(previous["intent_bytes"]) != canonical:
                raise DecisionRefused("intent_conflict")
            return self._decode(previous)

    async def read(self, transaction_id: str) -> DecisionRecord | None:
        async with self.pool.acquire() as connection:
            return self._decode(await self._row(connection, transaction_id))

    async def record_prepared(self, receipt: Receipt) -> DecisionRecord:
        async with self.pool.acquire() as connection, connection.transaction():
            row = await self._row(connection, receipt.transaction_id, lock=True)
            record = self._decode(row)
            if record is None or receipt.intent_digest != record.intent.digest:
                raise DecisionRefused("prepared_intent_mismatch")
            if receipt.participant not in record.intent.participants:
                raise DecisionRefused("prepared_participant_mismatch")
            old = record.prepared.get(receipt.participant)
            if old is not None:
                if old != receipt:
                    raise DecisionRefused("prepared_conflict")
                return record
            if record.state != "preparing":
                raise DecisionRefused("late_preparation")
            prepared = {**record.prepared, receipt.participant: receipt}
            state = "prepared" if len(prepared) == len(record.intent.participants) else "preparing"
            updated = await connection.fetchrow(
                f"""UPDATE {self.table} SET prepared=$3::jsonb,state=$4
                    WHERE namespace=$1 AND transaction_id=$2 AND state='preparing'
                      AND expires_at>clock_timestamp() RETURNING *""",
                self.namespace, receipt.transaction_id,
                json.dumps({key: asdict(value) for key, value in prepared.items()}), state)
            if updated is None:
                raise DecisionRefused("late_preparation")
            return self._decode(updated)

    async def decide(self, transaction_id: str, decision: str,
                     *, witness_digest: str = "") -> DecisionRecord:
        if decision not in TERMINAL:
            raise DecisionRefused("decision_invalid")
        async with self.pool.acquire() as connection, connection.transaction():
            row = await self._row(connection, transaction_id, lock=True)
            record = self._decode(row)
            if record is None:
                raise DecisionRefused("transaction_unknown")
            if record.terminal:
                if record.state != decision or (decision == "committed" and
                                                record.witness_digest != witness_digest):
                    raise DecisionRefused("decision_conflict")
                return record
            if decision == "committed" and (record.state != "prepared"
                    or set(record.prepared) != set(record.intent.participants)
                    or len(witness_digest) != 64):
                raise DecisionRefused("commit_unprepared")
            # The terminal transition is one conditional SQL statement, with
            # database time for the original approval expiry.
            updated = await connection.fetchrow(
                f"""UPDATE {self.table}
                    SET state=$3,witness_digest=$4,decided_at=clock_timestamp()
                    WHERE namespace=$1 AND transaction_id=$2
                      AND state IN ('preparing','prepared')
                      AND ($3 <> 'committed' OR
                           (state='prepared' AND expires_at>clock_timestamp()))
                    RETURNING *""", self.namespace, transaction_id, decision,
                witness_digest if decision == "committed" else "")
            if updated is None:
                raise DecisionRefused("decision_conflict")
            return self._decode(updated)

    async def abort_expired(self, transaction_id: str) -> DecisionRecord:
        async with self.pool.acquire() as connection, connection.transaction():
            row = await self._row(connection, transaction_id, lock=True)
            record = self._decode(row)
            if record is None:
                raise DecisionRefused("transaction_unknown")
            if record.terminal:
                return record
            updated = await connection.fetchrow(
                f"""UPDATE {self.table}
                    SET state='aborted',decided_at=clock_timestamp()
                    WHERE namespace=$1 AND transaction_id=$2
                      AND state IN ('preparing','prepared')
                      AND expires_at<=clock_timestamp() RETURNING *""",
                self.namespace, transaction_id)
            if updated is None:
                raise DecisionRefused("not_expired")
            return self._decode(updated)

    async def record_finished(self, receipt: Receipt) -> DecisionRecord:
        async with self.pool.acquire() as connection, connection.transaction():
            row = await self._row(connection, receipt.transaction_id, lock=True)
            record = self._decode(row)
            if record is None or not record.terminal:
                raise DecisionRefused("decision_unknown")
            if (receipt.intent_digest != record.intent.digest
                    or receipt.participant not in record.intent.participants):
                raise DecisionRefused("finish_mismatch")
            old = record.finished.get(receipt.participant)
            if old is not None:
                if old != receipt:
                    raise DecisionRefused("finish_conflict")
                return record
            finished = {**record.finished, receipt.participant: receipt}
            updated = await connection.fetchrow(
                f"""UPDATE {self.table} SET finished=$3::jsonb,finished_count=$4
                    WHERE namespace=$1 AND transaction_id=$2
                      AND state IN ('committed','aborted') RETURNING *""",
                self.namespace, receipt.transaction_id,
                json.dumps({key: asdict(value) for key, value in finished.items()}),
                len(finished))
            if updated is None:
                raise DecisionRefused("finish_conflict")
            return self._decode(updated)

    async def list_in_doubt(self, *, limit: int) -> Sequence[DecisionRecord]:
        if not 1 <= limit <= 1000:
            raise DecisionRefused("recovery_limit_invalid")
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                f"""SELECT * FROM {self.table}
                    WHERE namespace=$1 AND
                    (state IN ('preparing','prepared') OR finished_count<participant_count)
                    ORDER BY transaction_id LIMIT $2""", self.namespace, limit + 1)
        if len(rows) > limit:
            raise DecisionRefused("recovery_unbounded")
        return [self._decode(row) for row in rows]


__all__ = ["Coordinator", "DecisionRecord", "DecisionRefused", "DecisionStore",
           "Intent", "Participant", "PostgresDecisionStore", "Receipt", "ReceiptVerifier"]

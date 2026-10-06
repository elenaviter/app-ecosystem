"""One durable decision for one or several independently staged realms.

Applications own authorization, receipt authentication, business effects and
recovery scheduling. This module owns only the immutable intent and one
monotonic terminal decision in PostgreSQL.
"""

from __future__ import annotations

import json
import re
import secrets
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Protocol, Sequence

from .durable_wire import (
    GlobalIntent, IntentDraft, WireRefused, participant_projection,
    projection_digest,
)

TERMINAL = frozenset({"committed", "aborted"})
STATES = frozenset({"preparing", "prepared", *TERMINAL})
_NAME = re.compile(r"[a-z_][a-z0-9_]*\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ROW_COLUMNS = ("*, prepared::text AS prepared_json_text, "
                "finished::text AS finished_json_text")


class DecisionRefused(ValueError):
    """An exact identity, trusted proof or durable transition is unavailable."""


class RecoveryIncomplete(DecisionRefused):
    """A bounded pass continued past failed rows; retry their durable IDs."""

    def __init__(self, failures: Mapping[str, Exception],
                 completed: Sequence[DecisionRecord]) -> None:
        super().__init__("recovery_incomplete")
        self.failures = dict(failures)
        self.completed = tuple(completed)


@dataclass(frozen=True)
class Receipt:
    transaction_id: str
    epoch: int
    global_intent_digest: str
    participant: str
    projection_digest: str
    candidate_digest: str
    receipt_digest: str

    def __post_init__(self) -> None:
        if (type(self.transaction_id) is not str or not self.transaction_id
                or type(self.epoch) is not int or self.epoch < 1
                or type(self.participant) is not str or not self.participant
                or any(type(value) is not str or not _DIGEST.fullmatch(value)
                       for value in (self.global_intent_digest,
                                     self.projection_digest,
                                     self.candidate_digest, self.receipt_digest))):
            raise DecisionRefused("receipt_invalid")


@dataclass(frozen=True)
class DecisionRecord:
    intent: GlobalIntent
    state: str
    prepared: Mapping[str, Receipt]
    finished: Mapping[str, Receipt]
    witness_digest: str = ""
    decided_at: int | None = None

    @property
    def transaction_id(self) -> str:
        return self.intent.transaction_id

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL


class DecisionStore(Protocol):
    """Generic durable CAS port; optional connection belongs to the caller.

    A caller-supplied connection must already be inside its outer transaction.
    The store uses a nested savepoint and neither commits nor closes that
    connection. With no connection it owns the whole transaction. Expiry and
    decided time always come from PostgreSQL's clock, not the app clock.
    """

    async def begin(self, draft: IntentDraft, *, transaction_id: str | None = None,
                    epoch: int | None = None,
                    connection: Any | None = None) -> DecisionRecord: ...

    async def read(self, transaction_id: str, *, connection: Any | None = None
                   ) -> DecisionRecord | None: ...

    async def record_prepared(self, receipt: Receipt, *, connection: Any | None = None
                              ) -> DecisionRecord: ...

    async def decide(self, transaction_id: str, decision: str, *,
                     witness_digest: str = "", connection: Any | None = None
                     ) -> DecisionRecord: ...

    async def abort_expired(self, transaction_id: str, *, connection: Any | None = None
                            ) -> DecisionRecord: ...

    async def record_finished(self, receipt: Receipt, *, connection: Any | None = None
                              ) -> DecisionRecord: ...

    async def list_in_doubt(self, *, limit: int) -> Sequence[DecisionRecord]: ...


class Participant(Protocol):
    """Realm adapter whose finish is idempotent across crashes and recovery.

    An abort finish for a transaction never staged locally returns an exact
    tombstone acknowledgement. That closes the lost-prepare-reply fence case.
    """

    async def prepare(self, transaction_id: str) -> Receipt: ...

    async def finish(self, transaction_id: str, decision: str) -> Receipt: ...

    async def read_pending(self, transaction_id: str) -> Receipt | None: ...

    async def list_prepared(self, *, limit: int) -> Sequence[Receipt]: ...


class ReceiptVerifier(Protocol):
    """Application-provided authentication of a realm's exact receipt."""

    async def prepared(self, record: DecisionRecord, receipt: Receipt) -> None: ...

    async def finished(self, record: DecisionRecord, receipt: Receipt) -> None: ...


def _check_receipt(record: DecisionRecord, receipt: Receipt) -> None:
    if (receipt.transaction_id != record.transaction_id
            or receipt.epoch != record.intent.epoch
            or receipt.global_intent_digest != record.intent.digest
            or receipt.participant not in record.intent.participants):
        raise DecisionRefused("receipt_intent_mismatch")
    selected = participant_projection(record.intent, receipt.participant)
    if (receipt.projection_digest != projection_digest(record.intent,
                                                        receipt.participant)
            or receipt.candidate_digest != selected["candidate_digest"]):
        raise DecisionRefused("receipt_projection_mismatch")


class Coordinator:
    """The same entry points for one participant or many."""

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

    async def prepare(self, draft: IntentDraft, *,
                      transaction_id: str | None = None,
                      epoch: int | None = None) -> DecisionRecord:
        record = await self.store.begin(draft, transaction_id=transaction_id,
                                        epoch=epoch)
        return await self.prepare_existing(record.transaction_id)

    async def prepare_existing(self, transaction_id: str) -> DecisionRecord:
        """Prepare after a caller atomically reserved identity in its own DB txn."""

        record = await self.store.read(transaction_id)
        if record is None:
            raise DecisionRefused("transaction_unknown")
        if record.terminal:
            return record
        for name in record.intent.participants:
            if name in record.prepared:
                continue
            receipt = await self._participant(name).prepare(transaction_id)
            _check_receipt(record, receipt)
            await self.verifier.prepared(record, receipt)
            record = await self.store.record_prepared(receipt)
        return record

    async def decide(self, transaction_id: str, decision: str, *,
                     witness_digest: str = "") -> DecisionRecord:
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
        if decision == "committed":
            if set(record.prepared) != set(record.intent.participants):
                raise DecisionRefused("participants_not_prepared")
            if type(witness_digest) is not str or not _DIGEST.fullmatch(witness_digest):
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
            receipt = await self._participant(name).finish(transaction_id, record.state)
            _check_receipt(record, receipt)
            await self.verifier.finished(record, receipt)
            record = await self.store.record_finished(receipt)
        return record

    async def recover(self, *, limit: int = 100) -> list[DecisionRecord]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise DecisionRefused("recovery_limit_invalid")
        rows = await self.store.list_in_doubt(limit=limit)
        if len(rows) > limit:
            raise DecisionRefused("recovery_unbounded")
        result = []
        failures: dict[str, Exception] = {}
        for row in rows:
            try:
                if row.terminal:
                    result.append(await self.finish(row.transaction_id))
                    continue
                try:
                    decided = await self.store.abort_expired(row.transaction_id)
                except DecisionRefused as exc:
                    if str(exc) != "not_expired":
                        raise
                    result.append(row)
                    continue
                result.append(await self.finish(row.transaction_id)
                              if decided.terminal else decided)
            except Exception as exc:
                failures[row.transaction_id] = exc
        if failures:
            raise RecoveryIncomplete(failures, result)
        return result


class PostgresDecisionStore:
    """The one production PostgreSQL store shared by product compositions."""

    def __init__(self, pool: Any, *, schema: str, namespace: str) -> None:
        if (type(schema) is not str or not _NAME.fullmatch(schema)
                or type(namespace) is not str or not namespace):
            raise DecisionRefused("store_scope_invalid")
        self.pool = pool
        self.schema = schema
        self.namespace = namespace
        self.table = f"{schema}.service_foundation_decisions"
        self.sequence = f"{schema}.service_foundation_decision_epoch_seq"

    async def ensure_schema(self) -> None:
        async with self.pool.acquire() as connection:
            await connection.execute(f"CREATE SEQUENCE IF NOT EXISTS {self.sequence}")
            await connection.execute(f"""CREATE TABLE IF NOT EXISTS {self.table} (
                namespace TEXT NOT NULL,
                transaction_id TEXT NOT NULL,
                epoch BIGINT NOT NULL CHECK (epoch > 0),
                replay_scope TEXT NOT NULL,
                request_id TEXT NOT NULL,
                intent_bytes BYTEA NOT NULL,
                intent_digest TEXT NOT NULL,
                expires_at_epoch BIGINT NOT NULL,
                participant_count INTEGER NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('preparing','prepared','committed','aborted')),
                prepared JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                prepared_count INTEGER NOT NULL DEFAULT 0,
                finished JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                finished_count INTEGER NOT NULL DEFAULT 0,
                witness_digest TEXT NOT NULL DEFAULT '',
                decided_at_epoch BIGINT,
                PRIMARY KEY (namespace, transaction_id),
                UNIQUE (namespace, replay_scope, request_id),
                UNIQUE (namespace, replay_scope, epoch))""")
            await connection.execute(f"""CREATE INDEX IF NOT EXISTS
                service_foundation_decisions_in_doubt
                ON {self.table} (namespace, transaction_id)
                WHERE state IN ('preparing','prepared') OR finished_count<participant_count""")
            # A previous unreleased draft used the same table name with a
            # different layout. Never silently accept that layout.
            try:
                await connection.fetch(f"""SELECT epoch, replay_scope,
                    expires_at_epoch, prepared_count, decided_at_epoch
                    FROM {self.table} LIMIT 0""")
            except Exception as exc:
                raise DecisionRefused("store_schema_incompatible") from exc

    @asynccontextmanager
    async def _transaction(self, connection: Any | None):
        if connection is not None:
            if not connection.is_in_transaction():
                raise DecisionRefused("caller_transaction_required")
            async with connection.transaction():
                yield connection
        else:
            async with self.pool.acquire() as owned:
                async with owned.transaction():
                    yield owned

    @staticmethod
    def _decode(row: Any) -> DecisionRecord | None:
        if row is None:
            return None
        try:
            intent = GlobalIntent.from_canonical_bytes(bytes(row["intent_bytes"]))
        except WireRefused as exc:
            raise DecisionRefused("stored_intent_invalid") from exc
        if (intent.digest != row["intent_digest"]
                or intent.transaction_id != row["transaction_id"]
                or intent.epoch != row["epoch"]
                or intent.request_id != row["request_id"]
                or intent.expires_at != row["expires_at_epoch"]
                or len(intent.participants) != row["participant_count"]):
            raise DecisionRefused("stored_intent_invalid")
        def receipts(column: str) -> dict[str, Receipt]:
            # asyncpg's default JSONB decoder returns text; application pools
            # can instead install a decoder that returns a dict. Select the
            # database's raw JSONB text when available: parsing it exactly
            # once also distinguishes an object from a legacy JSONB string.
            try:
                value = row[f"{column}_json_text"]
            except (KeyError, IndexError):
                value = row[column]
            try:
                if isinstance(value, str):
                    value = json.loads(value)
                if not isinstance(value, Mapping):
                    raise DecisionRefused("stored_receipt_invalid")
                result = {}
                for participant, fields in value.items():
                    if not isinstance(participant, str) or not isinstance(fields, Mapping):
                        raise DecisionRefused("stored_receipt_invalid")
                    receipt = Receipt(**fields)
                    if receipt.participant != participant:
                        raise DecisionRefused("stored_receipt_invalid")
                    result[participant] = receipt
                return result
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise DecisionRefused("stored_receipt_invalid") from exc

        prepared = receipts("prepared")
        finished = receipts("finished")
        if (len(prepared) != row["prepared_count"]
                or len(finished) != row["finished_count"]):
            raise DecisionRefused("stored_receipt_count_invalid")
        return DecisionRecord(intent, row["state"], prepared, finished,
                              row["witness_digest"], row["decided_at_epoch"])

    async def _row(self, connection: Any, transaction_id: str, *, lock: bool = False):
        return await connection.fetchrow(
            f"SELECT {_ROW_COLUMNS} FROM {self.table} WHERE namespace=$1 AND transaction_id=$2"
            f" {'FOR UPDATE' if lock else ''}", self.namespace, transaction_id)

    async def begin(self, draft: IntentDraft, *, transaction_id: str | None = None,
                    epoch: int | None = None,
                    connection: Any | None = None) -> DecisionRecord:
        if (transaction_id is None) != (epoch is None):
            raise DecisionRefused("identity_pair_required")
        if transaction_id is not None and (
                type(transaction_id) is not str or not transaction_id
                or type(epoch) is not int or epoch < 1):
            raise DecisionRefused("identity_invalid")
        # The draft captured payload bytes at construction, before this await.
        async with self._transaction(connection) as conn:
            previous = await conn.fetchrow(
                f"""SELECT {_ROW_COLUMNS} FROM {self.table} WHERE namespace=$1 AND
                    replay_scope=$2 AND request_id=$3 FOR UPDATE""",
                self.namespace, draft.replay_scope, draft.request_id)
            if previous is not None:
                record = self._decode(previous)
                if (draft.bind(record.transaction_id, record.intent.epoch).canonical_bytes
                        != record.intent.canonical_bytes
                        or transaction_id is not None and transaction_id != record.transaction_id
                        or epoch is not None and epoch != record.intent.epoch):
                    raise DecisionRefused("intent_conflict")
                return record
            chosen_id = transaction_id or secrets.token_hex(32)
            chosen_epoch = epoch if epoch is not None else await conn.fetchval(
                f"SELECT nextval('{self.sequence}')")
            intent = draft.bind(chosen_id, chosen_epoch)
            row = await conn.fetchrow(
                f"""INSERT INTO {self.table}
                    (namespace,transaction_id,epoch,replay_scope,request_id,intent_bytes,
                     intent_digest,expires_at_epoch,participant_count,state)
                    SELECT $1,$2,$3,$4,$5,$6,$7,$8,$9,'preparing'
                    WHERE $8>floor(extract(epoch from clock_timestamp()))::bigint
                    ON CONFLICT DO NOTHING RETURNING {_ROW_COLUMNS}""",
                self.namespace, chosen_id, chosen_epoch, draft.replay_scope,
                draft.request_id, intent.canonical_bytes, intent.digest,
                intent.expires_at, len(intent.participants))
            if row is not None:
                return self._decode(row)
            previous = await conn.fetchrow(
                f"""SELECT {_ROW_COLUMNS} FROM {self.table} WHERE namespace=$1 AND
                    replay_scope=$2 AND request_id=$3""",
                self.namespace, draft.replay_scope, draft.request_id)
            if previous is None:
                raise DecisionRefused("intent_expired_or_identity_conflict")
            record = self._decode(previous)
            if (draft.bind(record.transaction_id, record.intent.epoch).canonical_bytes
                    != record.intent.canonical_bytes
                    or transaction_id is not None and transaction_id != record.transaction_id
                    or epoch is not None and epoch != record.intent.epoch):
                raise DecisionRefused("intent_conflict")
            return record

    async def read(self, transaction_id: str, *, connection: Any | None = None
                   ) -> DecisionRecord | None:
        if connection is not None:
            return self._decode(await self._row(connection, transaction_id))
        async with self.pool.acquire() as conn:
            return self._decode(await self._row(conn, transaction_id))

    async def record_prepared(self, receipt: Receipt, *, connection: Any | None = None
                              ) -> DecisionRecord:
        async with self._transaction(connection) as conn:
            record = self._decode(await self._row(conn, receipt.transaction_id))
            if record is None:
                raise DecisionRefused("transaction_unknown")
            _check_receipt(record, receipt)
            # The UPDATE itself merges one immutable receipt. Concurrent
            # writers recheck its WHERE clause after PostgreSQL's row lock;
            # they cannot overwrite another participant's JSONB entry.
            updated = await conn.fetchrow(
                f"""UPDATE {self.table}
                    SET prepared=prepared || jsonb_build_object($3::text,($4::text)::jsonb),
                        prepared_count=prepared_count+1,
                        state=CASE WHEN prepared_count+1=participant_count
                                   THEN 'prepared' ELSE 'preparing' END
                    WHERE namespace=$1 AND transaction_id=$2 AND state='preparing'
                      AND NOT (prepared ? $3::text)
                      AND expires_at_epoch>floor(extract(epoch from clock_timestamp()))::bigint
                    RETURNING {_ROW_COLUMNS}""", self.namespace, receipt.transaction_id,
                receipt.participant, json.dumps(asdict(receipt)))
            if updated is not None:
                return self._decode(updated)
            latest = self._decode(await self._row(conn, receipt.transaction_id))
            old = latest.prepared.get(receipt.participant)
            if old is not None:
                if old != receipt:
                    raise DecisionRefused("prepared_conflict")
                return latest
            raise DecisionRefused("late_preparation")

    async def decide(self, transaction_id: str, decision: str, *,
                     witness_digest: str = "", connection: Any | None = None
                     ) -> DecisionRecord:
        if decision not in TERMINAL:
            raise DecisionRefused("decision_invalid")
        if decision == "committed" and (
                type(witness_digest) is not str or not _DIGEST.fullmatch(witness_digest)):
            raise DecisionRefused("commit_witness_missing")
        async with self._transaction(connection) as conn:
            record = self._decode(await self._row(conn, transaction_id, lock=True))
            if record is None:
                raise DecisionRefused("transaction_unknown")
            if record.terminal:
                if record.state != decision or (decision == "committed" and
                                                record.witness_digest != witness_digest):
                    raise DecisionRefused("decision_conflict")
                return record
            if decision == "committed" and (
                    record.state != "prepared" or
                    set(record.prepared) != set(record.intent.participants)):
                raise DecisionRefused("commit_unprepared")
            updated = await conn.fetchrow(
                f"""UPDATE {self.table} SET state=$3,witness_digest=$4,
                    decided_at_epoch=floor(extract(epoch from clock_timestamp()))::bigint
                    WHERE namespace=$1 AND transaction_id=$2
                      AND state IN ('preparing','prepared')
                      AND ($3 <> 'committed' OR
                           (state='prepared' AND prepared_count=participant_count AND
                            expires_at_epoch>floor(extract(epoch from clock_timestamp()))::bigint))
                    RETURNING {_ROW_COLUMNS}""", self.namespace, transaction_id, decision,
                witness_digest if decision == "committed" else "")
            if updated is None:
                if decision == "committed":
                    expired = await conn.fetchval(
                        f"""SELECT expires_at_epoch<=
                            floor(extract(epoch from clock_timestamp()))::bigint
                            FROM {self.table} WHERE namespace=$1 AND transaction_id=$2
                              AND state IN ('preparing','prepared')""",
                        self.namespace, transaction_id)
                    if expired:
                        raise DecisionRefused("commit_expired")
                raise DecisionRefused("decision_conflict")
            return self._decode(updated)

    async def abort_expired(self, transaction_id: str, *, connection: Any | None = None
                            ) -> DecisionRecord:
        async with self._transaction(connection) as conn:
            record = self._decode(await self._row(conn, transaction_id, lock=True))
            if record is None:
                raise DecisionRefused("transaction_unknown")
            if record.terminal:
                return record
            updated = await conn.fetchrow(
                f"""UPDATE {self.table} SET state='aborted',
                    decided_at_epoch=floor(extract(epoch from clock_timestamp()))::bigint
                    WHERE namespace=$1 AND transaction_id=$2
                      AND state IN ('preparing','prepared')
                      AND expires_at_epoch<=floor(extract(epoch from clock_timestamp()))::bigint
                    RETURNING {_ROW_COLUMNS}""", self.namespace, transaction_id)
            if updated is None:
                raise DecisionRefused("not_expired")
            return self._decode(updated)

    async def record_finished(self, receipt: Receipt, *, connection: Any | None = None
                              ) -> DecisionRecord:
        async with self._transaction(connection) as conn:
            record = self._decode(await self._row(conn, receipt.transaction_id))
            if record is None or not record.terminal:
                raise DecisionRefused("decision_unknown")
            _check_receipt(record, receipt)
            updated = await conn.fetchrow(
                f"""UPDATE {self.table}
                    SET finished=finished || jsonb_build_object($3::text,($4::text)::jsonb),
                        finished_count=finished_count+1
                    WHERE namespace=$1 AND transaction_id=$2
                      AND state IN ('committed','aborted')
                      AND NOT (finished ? $3::text) RETURNING {_ROW_COLUMNS}""",
                self.namespace, receipt.transaction_id,
                receipt.participant, json.dumps(asdict(receipt)))
            if updated is not None:
                return self._decode(updated)
            latest = self._decode(await self._row(conn, receipt.transaction_id))
            old = latest.finished.get(receipt.participant)
            if old is not None:
                if old != receipt:
                    raise DecisionRefused("finish_conflict")
                return latest
            raise DecisionRefused("finish_conflict")

    async def list_in_doubt(self, *, limit: int) -> Sequence[DecisionRecord]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise DecisionRefused("recovery_limit_invalid")
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""SELECT {_ROW_COLUMNS} FROM {self.table} WHERE namespace=$1 AND
                    (state IN ('preparing','prepared') OR finished_count<participant_count)
                    ORDER BY transaction_id LIMIT $2""", self.namespace, limit + 1)
        if len(rows) > limit:
            raise DecisionRefused("recovery_unbounded")
        return [self._decode(row) for row in rows]


__all__ = ["Coordinator", "DecisionRecord", "DecisionRefused", "DecisionStore",
           "Participant", "PostgresDecisionStore", "Receipt", "ReceiptVerifier",
           "RecoveryIncomplete"]

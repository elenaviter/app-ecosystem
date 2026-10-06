"""Disposable PostgreSQL reference adapter for the generic CAS contract.

This is test code, not a second application decision table. Set
SERVICE_FOUNDATION_TEST_POSTGRES_DSN to a disposable database to run it.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from service_foundation.coordination.durable_decision_log import (
    Coordinator, DecisionRecord, DecisionRefused, Intent, Receipt,
)


def _receipt(value):
    return Receipt(**value)


def _decode(row):
    if row is None:
        return None
    raw = json.loads(bytes(row["intent_bytes"]))
    intent = Intent(raw["actor"], raw["request_id"], raw["context"],
                    raw["payload_digest"], tuple(raw["participants"]),
                    datetime.fromisoformat(raw["expires_at"]))
    return DecisionRecord(row["transaction_id"], intent, row["state"],
                          {k: _receipt(v) for k, v in json.loads(row["prepared"]).items()},
                          {k: _receipt(v) for k, v in json.loads(row["finished"]).items()},
                          row["witness_digest"])


class PostgresReferenceStore:
    def __init__(self, pool, table):
        self.pool, self.table = pool, table

    async def _row(self, connection, transaction_id, *, lock=False):
        return await connection.fetchrow(
            f"SELECT * FROM {self.table} WHERE transaction_id=$1 {'FOR UPDATE' if lock else ''}",
            transaction_id)

    async def begin(self, intent):
        async with self.pool.acquire() as connection, connection.transaction():
            inserted = await connection.fetchrow(
                f"""INSERT INTO {self.table}
                    (transaction_id,actor,request_id,context,intent_bytes,intent_digest,
                     expires_at,participant_count,state,prepared,finished)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'preparing','{{}}'::jsonb,'{{}}'::jsonb)
                    ON CONFLICT (context,actor,request_id) DO NOTHING RETURNING *""",
                secrets.token_hex(32), intent.actor, intent.request_id, intent.context,
                intent.canonical_bytes, intent.digest, intent.expires_at,
                len(intent.participants))
            if inserted is not None:
                return _decode(inserted)
            row = await connection.fetchrow(
                f"SELECT * FROM {self.table} WHERE context=$1 AND actor=$2 AND request_id=$3",
                intent.context, intent.actor, intent.request_id)
            if bytes(row["intent_bytes"]) != intent.canonical_bytes:
                raise DecisionRefused("intent_conflict")
            return _decode(row)

    async def read(self, transaction_id):
        async with self.pool.acquire() as connection:
            return _decode(await self._row(connection, transaction_id))

    async def record_prepared(self, receipt):
        async with self.pool.acquire() as connection, connection.transaction():
            row = await self._row(connection, receipt.transaction_id, lock=True)
            if row is None or receipt.intent_digest != row["intent_digest"]:
                raise DecisionRefused("prepared_intent_mismatch")
            record = _decode(row)
            if receipt.participant not in record.intent.participants:
                raise DecisionRefused("prepared_participant_mismatch")
            old = record.prepared.get(receipt.participant)
            if old is not None:
                if old != receipt:
                    raise DecisionRefused("prepared_conflict")
                return record
            if row["state"] != "preparing" or row["expires_at"] <= datetime.now(timezone.utc):
                raise DecisionRefused("late_preparation")
            prepared = {**record.prepared, receipt.participant: receipt}
            state = "prepared" if len(prepared) == row["participant_count"] else "preparing"
            updated = await connection.fetchrow(
                f"""UPDATE {self.table} SET prepared=$2::jsonb,state=$3
                    WHERE transaction_id=$1 AND state='preparing' AND expires_at>clock_timestamp()
                    RETURNING *""", receipt.transaction_id,
                json.dumps({k: asdict(v) for k, v in prepared.items()}), state)
            if updated is None:
                raise DecisionRefused("late_preparation")
            return _decode(updated)

    async def decide(self, transaction_id, decision, *, witness_digest=""):
        if decision not in {"committed", "aborted"}:
            raise DecisionRefused("decision_invalid")
        async with self.pool.acquire() as connection, connection.transaction():
            row = await self._row(connection, transaction_id, lock=True)
            if row is None:
                raise DecisionRefused("transaction_unknown")
            if row["state"] in {"committed", "aborted"}:
                if row["state"] != decision:
                    raise DecisionRefused("decision_conflict")
                return _decode(row)
            if decision == "committed":
                if (row["state"] != "prepared" or row["expires_at"] <= datetime.now(timezone.utc)
                        or len(witness_digest) != 64):
                    raise DecisionRefused("commit_unprepared")
            updated = await connection.fetchrow(
                f"""UPDATE {self.table} SET state=$2,witness_digest=$3
                    WHERE transaction_id=$1 AND state IN ('preparing','prepared')
                      AND ($2 <> 'committed' OR (state='prepared' AND expires_at>clock_timestamp()))
                    RETURNING *""", transaction_id, decision,
                witness_digest if decision == "committed" else "")
            if updated is None:
                raise DecisionRefused("decision_conflict")
            return _decode(updated)

    async def abort_expired(self, transaction_id):
        async with self.pool.acquire() as connection, connection.transaction():
            row = await self._row(connection, transaction_id, lock=True)
            if row is None:
                raise DecisionRefused("transaction_unknown")
            if row["state"] in {"committed", "aborted"}:
                return _decode(row)
            updated = await connection.fetchrow(
                f"""UPDATE {self.table} SET state='aborted'
                    WHERE transaction_id=$1 AND state IN ('preparing','prepared')
                      AND expires_at<=clock_timestamp() RETURNING *""", transaction_id)
            if updated is None:
                raise DecisionRefused("not_expired")
            return _decode(updated)

    async def record_finished(self, receipt):
        async with self.pool.acquire() as connection, connection.transaction():
            row = await self._row(connection, receipt.transaction_id, lock=True)
            if row is None or row["state"] not in {"committed", "aborted"}:
                raise DecisionRefused("decision_unknown")
            record = _decode(row)
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
                f"""UPDATE {self.table} SET finished=$2::jsonb,finished_count=$3
                    WHERE transaction_id=$1 AND state IN ('committed','aborted') RETURNING *""",
                receipt.transaction_id, json.dumps({k: asdict(v) for k, v in finished.items()}),
                len(finished))
            return _decode(updated)

    async def list_in_doubt(self, *, limit):
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                f"""SELECT * FROM {self.table}
                    WHERE state IN ('preparing','prepared') OR finished_count<participant_count
                    ORDER BY transaction_id LIMIT $1""", limit + 1)
            if len(rows) > limit:
                raise DecisionRefused("recovery_unbounded")
            return [_decode(row) for row in rows]


class Realm:
    def __init__(self, name, intent):
        self.name, self.intent = name, intent
        self.finishes = 0

    async def prepare(self, transaction_id):
        return Receipt(transaction_id, self.intent.digest, self.name, "a" * 64)

    async def finish(self, transaction_id, decision):
        self.finishes += 1
        return Receipt(transaction_id, self.intent.digest, self.name, "b" * 64)


class Verifier:
    async def prepared(self, record, receipt):
        if receipt.receipt_digest != "a" * 64:
            raise DecisionRefused("receipt_untrusted")

    async def finished(self, record, receipt):
        if receipt.receipt_digest != "b" * 64:
            raise DecisionRefused("receipt_untrusted")


@pytest_asyncio.fixture
async def postgres_store():
    dsn = os.environ.get("SERVICE_FOUNDATION_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("SERVICE_FOUNDATION_TEST_POSTGRES_DSN is not set")
    import asyncpg

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=6)
    table = "w581_" + uuid.uuid4().hex
    async with pool.acquire() as connection:
        await connection.execute(
            f"""CREATE TABLE {table} (
                transaction_id TEXT PRIMARY KEY, actor TEXT NOT NULL, request_id TEXT NOT NULL,
                context TEXT NOT NULL, intent_bytes BYTEA NOT NULL, intent_digest TEXT NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL, participant_count INTEGER NOT NULL,
                state TEXT NOT NULL, prepared JSONB NOT NULL, finished JSONB NOT NULL,
                witness_digest TEXT NOT NULL DEFAULT '',
                finished_count INTEGER NOT NULL DEFAULT 0,
                UNIQUE (context,actor,request_id))""")
    try:
        yield PostgresReferenceStore(pool, table)
    finally:
        async with pool.acquire() as connection:
            await connection.execute(f"DROP TABLE IF EXISTS {table}")
        await pool.close()


def make_intent(names, *, seconds=60):
    return Intent("actor", secrets.token_hex(8), "context", "f" * 64, tuple(names),
                  datetime.now(timezone.utc) + timedelta(seconds=seconds))


@pytest.mark.asyncio
async def test_postgres_decision_cas_conflict_and_restart_finish(postgres_store):
    intent = make_intent(("business", "card"))
    realms = {name: Realm(name, intent) for name in intent.participants}
    manager = Coordinator(postgres_store, realms, Verifier())
    prepared = await manager.prepare(intent)
    assert prepared.state == "prepared"
    winner, loser = await asyncio.gather(
        manager.decide(prepared.transaction_id, "committed", witness_digest="e" * 64),
        manager.decide(prepared.transaction_id, "aborted"), return_exceptions=True)
    assert sorted([type(winner).__name__, type(loser).__name__]) == [
        "DecisionRecord", "DecisionRefused"]
    terminal = await postgres_store.read(prepared.transaction_id)
    assert terminal.state in {"committed", "aborted"}
    assert (await postgres_store.abort_expired(prepared.transaction_id)).state == terminal.state

    # A new coordinator object over the durable row finishes without replaying
    # prepare or making a second terminal decision.
    restarted = Coordinator(postgres_store, realms, Verifier())
    finished = await restarted.finish(prepared.transaction_id)
    assert set(finished.finished) == set(intent.participants)
    assert all(realm.finishes == 1 for realm in realms.values())
    assert await restarted.recover() == []


@pytest.mark.asyncio
async def test_postgres_expiry_cas_refuses_late_stage_and_commit(postgres_store):
    intent = make_intent(("card",), seconds=0.03)
    row = await postgres_store.begin(intent)
    await asyncio.sleep(0.05)
    receipt = Receipt(row.transaction_id, intent.digest, "card", "a" * 64)
    with pytest.raises(DecisionRefused, match="late_preparation"):
        await postgres_store.record_prepared(receipt)
    with pytest.raises(DecisionRefused, match="participants_not_prepared"):
        await Coordinator(postgres_store, {"card": Realm("card", intent)}, Verifier()).decide(
            row.transaction_id, "committed", witness_digest="e" * 64)
    aborted = await postgres_store.abort_expired(row.transaction_id)
    assert aborted.state == "aborted"
    with pytest.raises(DecisionRefused, match="decision_conflict"):
        await postgres_store.decide(row.transaction_id, "committed", witness_digest="e" * 64)

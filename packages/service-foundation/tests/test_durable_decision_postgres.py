"""The production PostgreSQL decision store against disposable real storage.

Set SERVICE_FOUNDATION_TEST_POSTGRES_DSN to a disposable database to run it.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from service_foundation.coordination.durable_decision_log import (
    Coordinator, DecisionRefused, Intent, PostgresDecisionStore, Receipt,
)


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
    schema = "w581_" + uuid.uuid4().hex
    async with pool.acquire() as connection:
        await connection.execute(f"CREATE SCHEMA {schema}")
    store = PostgresDecisionStore(pool, schema=schema, namespace="test")
    await store.ensure_schema()
    await store.ensure_schema()  # idempotent production DDL
    try:
        yield store
    finally:
        async with pool.acquire() as connection:
            await connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
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

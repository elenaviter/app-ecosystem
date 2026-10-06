"""The v2 generic store against disposable PostgreSQL, when a DSN is set."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
import pytest_asyncio

from service_foundation.coordination.durable_decision_v2 import (
    Coordinator, DecisionRefused, PostgresDecisionStore, Receipt,
)
from service_foundation.coordination.durable_wire import (
    IntentDraft, participant_projection, projection_digest,
)


@pytest_asyncio.fixture
async def store():
    dsn = os.environ.get("SERVICE_FOUNDATION_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("requires a disposable PostgreSQL DSN")
    import asyncpg

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=6)
    schema = "w581v2_" + uuid.uuid4().hex
    async with pool.acquire() as connection:
        await connection.execute(f"CREATE SCHEMA {schema}")
    result = PostgresDecisionStore(pool, schema=schema, namespace="test")
    await result.ensure_schema()
    await result.ensure_schema()
    try:
        yield result
    finally:
        async with pool.acquire() as connection:
            await connection.execute(f"DROP SCHEMA {schema} CASCADE")
        await pool.close()


def draft(*, request_id="req", expires_at=2000000000, names=("card",)):
    inputs = {}
    for name in names:
        inputs[name] = {
            "participant": name, "binding_kind": "card", "binding_ref": name,
            "target_scope": "project-a", "target_incarnation": "inc-a",
            "action": "update", "before_revision": 2, "candidate_revision": 3,
            "candidate_digest": "a" * 64, "dependency_revisions": {},
            "actor_subject": "user-a", "actor_kind": "human", "provisioning": None,
        }
    return IntentDraft("project-a:user-a", request_id, expires_at, names,
                       {"participant_inputs": inputs, "project_ref": "project-a"})


class Realm:
    def __init__(self, store, name):
        self.store, self.name = store, name
        self.finishes = 0

    async def _receipt(self, transaction_id, digest):
        record = await self.store.read(transaction_id)
        intent = record.intent
        return Receipt(transaction_id, intent.epoch, intent.digest, self.name,
                       projection_digest(intent, self.name),
                       participant_projection(intent, self.name)["candidate_digest"],
                       digest)

    async def prepare(self, transaction_id):
        return await self._receipt(transaction_id, "b" * 64)

    async def finish(self, transaction_id, decision):
        self.finishes += 1
        return await self._receipt(transaction_id, "c" * 64)


class Verifier:
    async def prepared(self, record, receipt):
        if receipt.receipt_digest != "b" * 64:
            raise DecisionRefused("untrusted_receipt")

    async def finished(self, record, receipt):
        if receipt.receipt_digest != "c" * 64:
            raise DecisionRefused("untrusted_receipt")


@pytest.mark.asyncio
async def test_reserved_identity_and_same_connection_rollback(store):
    import asyncpg

    first = draft()
    async with store.pool.acquire() as connection:
        with pytest.raises(RuntimeError, match="rollback"):
            async with connection.transaction():
                row = await store.begin(first, transaction_id="tx-a", epoch=7,
                                        connection=connection)
                assert row.intent.digest == first.bind("tx-a", 7).digest
                assert (await store.read("tx-a", connection=connection)) == row
                raise RuntimeError("rollback")
    assert await store.read("tx-a") is None
    row = await store.begin(first, transaction_id="tx-a", epoch=7)
    assert (await store.begin(first, transaction_id="tx-a", epoch=7)) == row
    with pytest.raises(DecisionRefused, match="intent_conflict"):
        await store.begin(first, transaction_id="tx-b", epoch=8)
    with pytest.raises(DecisionRefused, match="identity_pair_required"):
        await store.begin(first, epoch=7)
    async with store.pool.acquire() as connection:
        with pytest.raises(DecisionRefused, match="caller_transaction_required"):
            await store.begin(draft(request_id="other"), connection=connection)


@pytest.mark.asyncio
async def test_v2_one_terminal_cas_and_recovery_finish(store):
    names = ("card", "business")
    realms = {name: Realm(store, name) for name in names}
    manager = Coordinator(store, realms, Verifier())
    row = await manager.prepare(draft(request_id="two", names=names))
    assert row.state == "prepared"
    results = await asyncio.gather(
        manager.decide(row.transaction_id, "committed", witness_digest="e" * 64),
        manager.decide(row.transaction_id, "aborted"), return_exceptions=True)
    assert sum(result.state in ("committed", "aborted") for result in results
               if not isinstance(result, Exception)) == 1
    terminal = await store.read(row.transaction_id)
    assert terminal.decided_at is not None
    assert terminal.state in ("committed", "aborted")
    if terminal.state == "committed":
        with pytest.raises(DecisionRefused, match="decision_conflict"):
            await manager.decide(row.transaction_id, "committed",
                                 witness_digest="f" * 64)
    finished = await Coordinator(store, realms, Verifier()).finish(row.transaction_id)
    assert set(finished.finished) == set(names)
    assert await manager.recover() == []
    assert all(realm.finishes == 1 for realm in realms.values())


@pytest.mark.asyncio
async def test_v2_expiry_and_witness_are_database_enforced(store):
    import time

    row = await store.begin(draft(request_id="expired", expires_at=int(time.time()) + 2))
    with pytest.raises(DecisionRefused, match="not_expired"):
        await store.abort_expired(row.transaction_id)
    await asyncio.sleep(2.1)
    with pytest.raises(DecisionRefused, match="late_preparation"):
        await store.record_prepared(await Realm(store, "card").prepare(row.transaction_id))
    assert (await store.abort_expired(row.transaction_id)).state == "aborted"
    with pytest.raises(DecisionRefused, match="decision_conflict"):
        await store.decide(row.transaction_id, "committed", witness_digest="e" * 64)


@pytest.mark.asyncio
async def test_expired_prepared_commit_has_distinct_refusal_and_preserves_identity(store):
    import time

    row = await store.begin(draft(request_id="late-commit",
                                  expires_at=int(time.time()) + 2))
    receipt = await Realm(store, "card").prepare(row.transaction_id)
    assert (await store.record_prepared(receipt)).state == "prepared"
    await asyncio.sleep(2.1)
    with pytest.raises(DecisionRefused, match="commit_expired"):
        await store.decide(row.transaction_id, "committed", witness_digest="e" * 64)
    same = await store.read(row.transaction_id)
    assert same.state == "prepared" and same.intent.digest == row.intent.digest
    assert (await store.abort_expired(row.transaction_id)).state == "aborted"


@pytest.mark.asyncio
async def test_v2_receipt_and_decision_respect_caller_transaction(store):
    row = await store.begin(draft(request_id="outer"),
                            transaction_id="outer-tx", epoch=11)
    receipt = await Realm(store, "card").prepare(row.transaction_id)
    async with store.pool.acquire() as connection:
        with pytest.raises(RuntimeError, match="rollback"):
            async with connection.transaction():
                prepared = await store.record_prepared(receipt, connection=connection)
                assert prepared.state == "prepared"
                decided = await store.decide(row.transaction_id, "committed",
                                             witness_digest="e" * 64,
                                             connection=connection)
                assert decided.state == "committed"
                raise RuntimeError("rollback")
    assert (await store.read(row.transaction_id)).state == "preparing"


@pytest.mark.asyncio
async def test_concurrent_receipt_merges_keep_both_participants(store):
    names = ("card", "business")
    for number in range(20):
        row = await store.begin(draft(request_id=f"pair-{number}", names=names))
        realms = {name: Realm(store, name) for name in names}
        receipts = await asyncio.gather(*(
            realm.prepare(row.transaction_id) for realm in realms.values()))
        prepared = await asyncio.gather(*(
            store.record_prepared(receipt) for receipt in receipts))
        assert set((await store.read(row.transaction_id)).prepared) == set(names)
        assert any(result.state == "prepared" for result in prepared)
        await store.decide(row.transaction_id, "aborted")
        receipts = await asyncio.gather(*(
            realm.finish(row.transaction_id, "aborted") for realm in realms.values()))
        await asyncio.gather(*(
            store.record_finished(receipt) for receipt in receipts))
        assert set((await store.read(row.transaction_id)).finished) == set(names)

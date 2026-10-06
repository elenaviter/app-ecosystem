"""Backlog paging, restart and durable effects on disposable PG, both codecs."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

from service_foundation.coordination.durable_decision_v2 import (
    Coordinator, DecisionRefused, PostgresDecisionStore, RecoveryIncomplete,
)
from service_foundation.coordination.durable_wire import IntentDraft
from _recovery_paging_process import Realm, Verifier, install_codecs


@pytest_asyncio.fixture(params=[False, True], ids=["plain-json", "platform-json"])
async def store(request):
    dsn = os.environ.get("SERVICE_FOUNDATION_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("requires a disposable PostgreSQL DSN")
    import asyncpg

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4,
                                     init=install_codecs if request.param else None)
    schema = "paging_" + uuid.uuid4().hex
    async with pool.acquire() as connection:
        await connection.execute(f"CREATE SCHEMA {schema}")
        await connection.execute(f"CREATE TABLE {schema}.effects "
                                 "(transaction_id TEXT PRIMARY KEY, decision TEXT NOT NULL)")
    result = PostgresDecisionStore(pool, schema=schema, namespace="paging-test")
    await result.ensure_schema()
    try:
        yield result, request.param
    finally:
        async with pool.acquire() as connection:
            await connection.execute(f"DROP SCHEMA {schema} CASCADE")
        await pool.close()


async def seed(store, transaction_id, *, expires_at=1, state="preparing"):
    inputs = {"realm": {
        "participant": "realm", "binding_kind": "effect", "binding_ref": "record",
        "target_scope": "test-scope", "target_incarnation": "test-incarnation",
        "action": "update", "before_revision": 1, "candidate_revision": 2,
        "candidate_digest": "a" * 64, "dependency_revisions": {},
        "actor_subject": "test-actor", "actor_kind": "human", "provisioning": None,
    }}
    # Production begin correctly refuses already-expired intents. Admit a
    # short-lived real intent, then let the DB deadline pass before recovery.
    deadline = int(time.time()) + 2 if expires_at == 1 else expires_at
    async with store.pool.acquire() as connection:
        epoch = await connection.fetchval(f"SELECT nextval('{store.sequence}')")
    row = await store.begin(IntentDraft("test-replay", transaction_id, deadline,
                                         ("realm",), {"participant_inputs": inputs}),
                            transaction_id=transaction_id, epoch=epoch)
    if state == "aborted":
        await store.decide(row.transaction_id, state)
    elif state == "committed":
        await store.record_prepared(await Realm(store).prepare(row.transaction_id))
        await store.decide(row.transaction_id, state, witness_digest="e" * 64)
    return row


def recover_in_fresh_process(store, codec, *, after="", limit=3):
    process = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("_recovery_paging_process.py")),
         store.schema, store.namespace, after, str(limit), "on" if codec else "off"],
        capture_output=True, text=True, check=True, timeout=30)
    return json.loads(process.stdout)


@pytest.mark.asyncio
async def test_store_pages_are_exclusive_bounded_and_namespace_scoped(store):
    sql, _ = store
    for transaction_id in ("a", "b", "c", "d", "e"):
        await seed(sql, transaction_id, expires_at=int(time.time()) + 600)
    other = PostgresDecisionStore(sql.pool, schema=sql.schema, namespace="other")
    await seed(other, "0-other", expires_at=int(time.time()) + 600)
    first, more = await sql.list_in_doubt_page(limit=2)
    assert [row.transaction_id for row in first] == ["a", "b"] and more is True
    second, more = await sql.list_in_doubt_page(limit=2, after="b")
    assert [row.transaction_id for row in second] == ["c", "d"] and more is True
    last, more = await sql.list_in_doubt_page(limit=2, after="d")
    assert [row.transaction_id for row in last] == ["e"] and more is False
    assert await sql.list_in_doubt_page(limit=2, after="e") == ([], False)
    with pytest.raises(DecisionRefused, match="recovery_unbounded"):
        await sql.list_in_doubt(limit=2)


@pytest.mark.asyncio
async def test_backlog_drains_across_true_process_restarts_both_codecs(store):
    sql, codec = store
    await seed(sql, "a-unexpired", expires_at=int(time.time()) + 600)
    for index in range(7):
        await seed(sql, f"b-{index:02}",
                   expires_at=int(time.time()) + 600 if index == 3 else 1,
                   state="committed" if index == 3 else "aborted" if index == 4 else "preparing")
    other = PostgresDecisionStore(sql.pool, schema=sql.schema, namespace="other")
    await seed(other, "0-other")
    await asyncio.sleep(2.1)
    with pytest.raises(DecisionRefused, match="recovery_unbounded"):
        await sql.list_in_doubt(limit=3)
    with pytest.raises(DecisionRefused, match="recovery_unbounded"):
        await Coordinator(sql, {"realm": Realm(sql)}, Verifier()).recover(limit=3)

    cursor, scanned = "", []
    for pass_number in range(3):
        page = recover_in_fresh_process(sql, codec, after=cursor)
        assert 0 < len(page["ids"]) <= 3
        scanned.extend(page["ids"])
        cursor = page["next_after"]
        assert page["has_more"] is (pass_number < 2)
    assert scanned == ["a-unexpired", *[f"b-{i:02}" for i in range(7)]]
    assert cursor == "b-06"
    async with sql.pool.acquire() as connection:
        assert await connection.fetchval(f"SELECT count(*) FROM {sql.schema}.effects") == 7
    for index in range(7):
        assert set((await sql.read(f"b-{index:02}")).finished) == {"realm"}
    assert (await sql.read("a-unexpired")).state == "preparing"
    assert (await other.read("0-other")).state == "preparing"

    # Losing the application cursor on restart does not lose durable progress.
    replay = recover_in_fresh_process(sql, codec, after="")
    assert replay == {"ids": ["a-unexpired"], "next_after": "a-unexpired", "has_more": False}
    async with sql.pool.acquire() as connection:
        assert await connection.fetchval(f"SELECT count(*) FROM {sql.schema}.effects") == 7


@pytest.mark.asyncio
async def test_failed_page_advances_then_wrap_retries_without_starvation(store):
    sql, _ = store
    for transaction_id in ("a-stuck", "b-good", "d-good"):
        await seed(sql, transaction_id)
    await seed(sql, "c-unexpired", expires_at=int(time.time()) + 600)
    await asyncio.sleep(2.1)
    realm = Realm(sql, fail_id="a-stuck")
    manager = Coordinator(sql, {"realm": realm}, Verifier())
    with pytest.raises(RecoveryIncomplete) as failure:
        await manager.recover_page(limit=2)
    assert set(failure.value.failures) == {"a-stuck"}
    assert [row.transaction_id for row in failure.value.completed] == ["b-good"]
    assert failure.value.next_after == "b-good" and failure.value.has_more is True
    rows, cursor, more = await manager.recover_page(limit=2, after=failure.value.next_after)
    assert [row.transaction_id for row in rows] == ["c-unexpired", "d-good"]
    assert cursor == "d-good" and more is False
    assert (await sql.read("a-stuck")).finished == {}
    with pytest.raises(RecoveryIncomplete) as repeated:
        await manager.recover_page(limit=2, after="")
    assert set(repeated.value.failures) == {"a-stuck"}
    assert repeated.value.next_after == "c-unexpired" and repeated.value.has_more is False
    assert realm.calls.count("a-stuck") == 2
    assert realm.calls.count("b-good") == realm.calls.count("d-good") == 1
    realm.fail_id = ""
    rows, cursor, more = await manager.recover_page(limit=2, after="")
    assert [row.transaction_id for row in rows] == ["a-stuck", "c-unexpired"]
    assert cursor == "c-unexpired" and more is False
    async with sql.pool.acquire() as connection:
        assert await connection.fetchval(f"SELECT count(*) FROM {sql.schema}.effects") == 3


@pytest.mark.asyncio
async def test_rows_inserted_behind_cursor_are_revisited_and_effect_replay_is_idempotent(store):
    sql, codec = store
    await seed(sql, "z-original", state="aborted")
    # Simulate a participant crash after the effect but before its receipt.
    await Realm(sql).finish("z-original", "aborted")
    first = recover_in_fresh_process(sql, codec, limit=1)
    assert first["ids"] == ["z-original"] and first["has_more"] is False
    await seed(sql, "a-later")
    await asyncio.sleep(2.1)
    assert await sql.list_in_doubt_page(limit=1, after="z-original") == ([], False)
    wrapped = recover_in_fresh_process(sql, codec, after="", limit=1)
    assert wrapped["ids"] == ["a-later"] and wrapped["has_more"] is False
    async with sql.pool.acquire() as connection:
        assert await connection.fetchval(f"SELECT count(*) FROM {sql.schema}.effects") == 2
    assert await sql.list_in_doubt_page(limit=1000, after="") == ([], False)
    assert await sql.list_in_doubt_page(limit=1, after="x" * 128) == ([], False)
    assert await sql.list_in_doubt_page(limit=1, after="' OR 1=1 --") == ([], False)


@pytest.mark.asyncio
async def test_concurrent_page_replay_preserves_one_effect_and_receipt(store):
    sql, _ = store
    for transaction_id in ("a-effect", "b-effect"):
        await seed(sql, transaction_id, state="aborted")
    managers = [Coordinator(sql, {"realm": Realm(sql)}, Verifier()) for _ in range(2)]
    await asyncio.gather(*(manager.recover_page(limit=2) for manager in managers))
    async with sql.pool.acquire() as connection:
        assert await connection.fetchval(f"SELECT count(*) FROM {sql.schema}.effects") == 2
    for transaction_id in ("a-effect", "b-effect"):
        assert set((await sql.read(transaction_id)).finished) == {"realm"}
    assert await managers[0].recover_page() == ([], "", False)

"""Independent exact-53 public-pointer cuts: actual SDK R + flock + PG, scheduling only.

A has a one-second non-renewed lease to model a VM stall. B has actual checked
renewal, so its save is not refused because of a separate successor TTL lapse.
No replacement SQL, DEL, fake clock, fake owner guard, or product Source edit.
"""
import asyncio
import contextlib
import json
import os
import uuid

import asyncpg
import pytest
from redis.asyncio import Redis

from connection_hub.delegated_credentials import durable_io
from connection_hub.delegated_credentials.cards.locks import (
    guard_card_store_writes,
    hub_redis_card_mutation_lock,
)
from connection_hub.delegated_credentials.cards.pointer_authority import PostgresCardRows
from connection_hub.delegated_credentials.cards.redis_lock import (
    _assert_card_lock_held,
    assert_card_lock_owned,
    card_lock_key,
)
from connection_hub.delegated_credentials.cards.store import (
    BundleStorageDelegatedCardStore,
    CardStorageError,
)
from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.cards.service import (
    _kdcube_card_mutation_lock,
)
from kdcube_ai_app.storage.observed_file_locks import make_lock_metadata
from kdcube_ai_app.storage.observed_redis_locks import observed_redis_lock_async

pytestmark = pytest.mark.skipif(
    not (os.environ.get("W661_TEST_PG_DSN") and os.environ.get("W661_TEST_REDIS_URL")),
    reason="Owned real PostgreSQL and Redis required",
)
W661_OWN_R_LOCK = True
SUBJECT = "6" * 64
ACCESS = "aut_pointer_a"


def pointer(by, n):
    return {"schema": "connection_hub.card-current.v1", "access_id": ACCESS,
            "card_revision": n, "revision_name": f"r{n:08d}", "by": by}


@contextlib.asynccontextmanager
async def pointer_world(tmp_path):
    pool = await asyncpg.create_pool(os.environ["W661_TEST_PG_DSN"], min_size=1, max_size=2)
    redis = Redis.from_url(os.environ["W661_TEST_REDIS_URL"])
    try:
        project = "ip" + uuid.uuid4().hex[:12]
        rows = PostgresCardRows(pool, schema="kdcube_t_" + project, tenant="t", project=project)
        await rows.ensure_schema()
        await rows.activate()
        store = BundleStorageDelegatedCardStore(tmp_path / "shared-cards")
        store.card_rows = rows
        guard_card_store_writes(store, role=lambda: "proc")

        async def owner_guard():
            await assert_card_lock_owned()
        owner_guard.sync = _assert_card_lock_held
        durable_io.guard_writes_under(store.root, owner_guard, name="card_lock_owner")
        locks = [
            hub_redis_card_mutation_lock(
                redis, _kdcube_card_mutation_lock, tmp_path / name,
                tenant="t", project=project, observed_lock=observed_redis_lock_async,
                make_metadata=make_lock_metadata, role=lambda: "proc",
                ttl_seconds=1, renew_seconds=None if index == 0 else 0.2,
            )
            for index, name in enumerate(["old-container", "successor-container"])
        ]
        path = store.current_path(subject_hash=SUBJECT, access_id=ACCESS).with_name(".lock")
        resource = f"delegated-card:{ACCESS}"

        def held(index):
            return locks[index](lock_path=path, resource_id=resource,
                                operation="independent-public-pointer", wait_seconds=3)
        key = card_lock_key(tenant="t", project=project, resource_id=resource, lock_path=path)
        async with held(0):
            assert await store.read_pointer_document(subject_hash=SUBJECT, access_id=ACCESS) is None
            await store.write_pointer_document(subject_hash=SUBJECT, access_id=ACCESS,
                                               document=pointer("seed", 1))
        yield store, rows, redis, key, held
    finally:
        await redis.aclose()
        await pool.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cut", ["after_owner_guard_before_cas", "after_owner_guard_before_claim"])
@pytest.mark.parametrize("stall", [False, True])
async def test_public_pointer_live_successor_survives_old_owner(tmp_path, monkeypatch, cut, stall):
    async with pointer_world(tmp_path) as (store, rows, redis, key, held):
        at_cut, resume, a_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        seen = {"cut": cut, "stall": stall}
        actual_fetchrow = rows._fetchrow

        async def schedule_actual_sql(query, *args):
            is_pointer = "connection_hub_card_pointers" in query
            target = ("SET document" in query if cut.endswith("cas") else
                      "SET fence" in query and "SET document" not in query)
            if asyncio.current_task().get_name() == "pointer-old-A" and is_pointer and target and not at_cut.is_set():
                seen["a_key_at_cut"] = await redis.get(key)
                assert seen["a_key_at_cut"]
                at_cut.set()
                if stall:
                    await asyncio.wait_for(resume.wait(), 8)
            return await actual_fetchrow(query, *args)
        monkeypatch.setattr(rows, "_fetchrow", schedule_actual_sql)

        async def old_a():
            try:
                async with held(0):
                    try:
                        seen["a_read"] = await store.read_pointer_document(subject_hash=SUBJECT, access_id=ACCESS)
                        await store.write_pointer_document(subject_hash=SUBJECT, access_id=ACCESS,
                                                           document=pointer("A", 2))
                        seen["a_result"] = "written"
                    except CardStorageError as exc:
                        seen["a_result"] = str(exc)
            except CardStorageError as exc:
                seen.setdefault("a_result", str(exc))
            finally:
                a_done.set()

        async def successor_b():
            await asyncio.wait_for(at_cut.wait(), 5)
            if stall:
                async def expires():
                    while await redis.exists(key):
                        await asyncio.sleep(0.02)
                await asyncio.wait_for(expires(), 4)
            else:
                await asyncio.wait_for(a_done.wait(), 8)
            async with held(1):
                seen["b_read"] = await store.read_pointer_document(subject_hash=SUBJECT, access_id=ACCESS)
                b_token = await redis.get(key)
                assert b_token and b_token != seen["a_key_at_cut"]
                resume.set()
                await asyncio.wait_for(a_done.wait(), 8)
                await assert_card_lock_owned()
                seen["b_key_after_a"] = await redis.get(key) == b_token
                before_b, before_b_revision = await rows.read_pointer(subject_hash=SUBJECT, access_id=ACCESS,
                                                                    claim=False)
                seen["before_b"] = (before_b["by"], before_b_revision)
                await store.write_pointer_document(subject_hash=SUBJECT, access_id=ACCESS,
                                                   document=pointer("B", 2 if stall else 3))
                seen["b_key_after_cas"] = await redis.get(key) == b_token

        a = asyncio.create_task(old_a(), name="pointer-old-A")
        b = asyncio.create_task(successor_b(), name="pointer-live-B")
        try:
            await asyncio.wait_for(asyncio.gather(a, b), 20)
        finally:
            resume.set()
            await asyncio.gather(a, b, return_exceptions=True)
        final, revision = await rows.read_pointer(subject_hash=SUBJECT, access_id=ACCESS)
        seen["final"] = (final["by"], revision)
        # Key values are test metadata; print only booleans and documents, not lock tokens.
        seen["a_key_at_cut"] = bool(seen["a_key_at_cut"])
        print("actual public pointer cut:", json.dumps(seen, sort_keys=True))
        assert seen["b_key_after_a"] and seen["b_key_after_cas"]
        assert seen["final"] == ("B", 2 if stall else 3), seen
        if stall:
            assert seen["b_read"]["by"] == "seed" and seen["before_b"] == ("seed", 1), seen
            expected = "card_pointer_moved" if cut.endswith("cas") else "card_lock_lost"
            assert seen["a_result"] == expected, seen
        else:
            assert seen["a_result"] == "written" and seen["b_read"]["by"] == "A", seen

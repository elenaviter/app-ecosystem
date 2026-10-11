"""W700 P: a row write passes R's owner guard BEFORE its CAS (Ops' R+P stall run, 11 Oct).

Actual KDCube R (SDK observed_redis_lock_async, 1 s TTL, renewal off to model a stall), observed flock, real PG
rows; world() is Infra's actual-R fixture. A reads the marker under its lock and stalls BEFORE writing; its key
really expires; B takes the lock and reads the SAME row revision; A then writes. Without the guard A's row CAS
landed (only its file hint was refused card_lock_lost) and B's CAS then failed: the row held A's stale write
while A's caller saw a failure. With the guard A is refused before the row moves, and B commits.

Two owned local lock roots model two containers over the same card files.
The old owner uses a 1-second lease with renewal disabled ONLY to model a
VM stall. Its actual file syscall is paused after the production owner guard.
No fake exclusion lock, SQL edits or substituted file contents; not live E2E.
"""
import asyncio
import contextlib
import json
import os
import threading
import uuid

import asyncpg
import pytest

pytestmark = pytest.mark.skipif(not (os.environ.get("W661_TEST_PG_DSN") and os.environ.get("W661_TEST_REDIS_URL")),
                                reason="W661_TEST_PG_DSN and W661_TEST_REDIS_URL are required")
W661_OWN_R_LOCK = True  # builds its own real SDK R locks; the R-mode harness must not wrap them again
from redis.asyncio import Redis

from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.cards.service import _kdcube_card_mutation_lock
from kdcube_ai_app.storage.observed_file_locks import make_lock_metadata
from kdcube_ai_app.storage.observed_redis_locks import observed_redis_lock_async
from connection_hub.delegated_credentials.cards.locks import hub_redis_card_mutation_lock, guard_card_store_writes
from connection_hub.delegated_credentials.cards.redis_lock import assert_card_lock_owned, _assert_card_lock_held, card_lock_key
from connection_hub.delegated_credentials.cards.pointer_authority import PostgresCardRows
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore
from connection_hub.delegated_credentials import durable_io

TXN = "txn-" + "c" * 32


@contextlib.asynccontextmanager
async def world(tmp_path):
    pool = await asyncpg.create_pool(os.environ["W661_TEST_PG_DSN"], min_size=1, max_size=2)
    client = Redis.from_url(os.environ["W661_TEST_REDIS_URL"])
    project = "gap" + uuid.uuid4().hex[:12]
    try:
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
        locks = []
        for name in ["old-container", "successor-container"]:
            locks.append(hub_redis_card_mutation_lock(
                client, _kdcube_card_mutation_lock, tmp_path / name,
                tenant="t", project=project, observed_lock=observed_redis_lock_async,
                make_metadata=make_lock_metadata, role=lambda: "proc", ttl_seconds=1, renew_seconds=None))
        path = store.root / "txn.lock"
        resource = f"card-version-txn:{TXN}"
        def held(index):
            return locks[index](lock_path=path, resource_id=resource, operation="independent-p-gap", wait_seconds=3)
        key = card_lock_key(tenant="t", project=project, resource_id=resource, lock_path=path)
        yield store, rows, client, key, held
    finally:
        await client.aclose()
        await pool.close()


@pytest.mark.asyncio
async def test_a_writer_whose_r_lock_expired_is_refused_before_its_row_cas(tmp_path):
    from connection_hub.delegated_credentials.cards.store import CardStorageError

    async with world(tmp_path) as (store, rows, redis, key, held):
        staged = {"schema": "connection_hub.card-version-txn.v1", "txn": TXN, "state": "staged", "members": []}
        async with held(0):
            assert await store.read_marker_document(TXN) is None
            await store.write_marker_document(TXN, staged)
        a_read, b_read, a_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        outcome = {}

        async def stale_a():
            async with held(0):
                assert await store.read_marker_document(TXN) == staged  # observes revision 1
                a_read.set()
                await asyncio.wait_for(b_read.wait(), 8)  # stalls past its TTL; B reads the same revision
                try:
                    await store.write_marker_document(TXN, {**staged, "state": "published", "by": "A"})
                    outcome["a"] = "written"
                except CardStorageError as exc:
                    outcome["a"] = str(exc)
            a_done.set()

        async def successor_b():
            await asyncio.wait_for(a_read.wait(), 5)

            async def expired():
                while await redis.exists(key):
                    await asyncio.sleep(.02)

            await asyncio.wait_for(expired(), 4)  # A's key really expired: no DEL, no swap
            async with held(1):
                assert await store.read_marker_document(TXN) == staged  # the same revision A read
                b_read.set()
                await asyncio.wait_for(a_done.wait(), 8)  # A writes first, while B holds the lock
                await store.write_marker_document(TXN, {**staged, "state": "published", "by": "B"})
                outcome["b"] = "written"

        await asyncio.wait_for(asyncio.gather(stale_a(), successor_b()), 20)
        assert outcome == {"a": "card_lock_lost", "b": "written"}, outcome
        assert (await rows.read_marker(TXN))[0]["by"] == "B"

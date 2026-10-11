"""P marker late-hint cut with actual KDCube R, observed flock and real PG (Infra, retained on the P fix head).

One change from Infra's original (kept verbatim, not collected, in evidence/infra_p_marker_actual_r_gap_original_matcher.py.txt):
the pause matches the published marker by STATE, not by exact JSON equality, because the fixed store tags
the hint it writes (_card_rows_era); with exact equality the pause would never fire on the fix and the test
would fail on its own pause check, not on the authority boundary. With the state matcher the late syscall is
exercised on both the pre-fix store (which then fails as Infra reported) and the fix (which passes).

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
from redis.asyncio import Redis

from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.cards.service import _kdcube_card_mutation_lock
from kdcube_ai_app.storage.observed_file_locks import make_lock_metadata
from kdcube_ai_app.storage.observed_redis_locks import observed_redis_lock_async
from connection_hub.delegated_credentials.cards.locks import hub_redis_card_mutation_lock, guard_card_store_writes
from connection_hub.delegated_credentials.cards.redis_lock import assert_card_lock_owned, _assert_card_lock_held, card_lock_key
from connection_hub.delegated_credentials.cards.pointer_authority import PostgresCardRows
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore
from connection_hub.delegated_credentials import durable_io

TXN = "txn-" + "b" * 32


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
@pytest.mark.parametrize("late_writer", [False, True])
async def test_removed_marker_authority_survives_actual_r_owner_check_to_syscall_gap(tmp_path, monkeypatch, late_writer):
    async with world(tmp_path) as (store, rows, redis, key, held):
        staged = {"schema": "connection_hub.card-version-txn.v1", "txn": TXN, "state": "staged", "members": []}
        published = {**staged, "state": "published"}
        async with held(0):
            assert await store.read_marker_document(TXN) is None
            await store.write_marker_document(TXN, staged)
        hint = store.card_version_marker_path(TXN)
        at_syscall, resume_syscall = threading.Event(), threading.Event()
        actual_syscall = durable_io._write_text_atomic

        def pause_actual_syscall(path, text):
            if late_writer and path == hint and json.loads(text).get("state") == "published":
                # Actual write_json_atomic has ALREADY run the actual Redis guard.
                at_syscall.set()
                if not resume_syscall.wait(8):
                    raise RuntimeError("independent syscall stall timed out")
            return actual_syscall(path, text)

        monkeypatch.setattr(durable_io, "_write_text_atomic", pause_actual_syscall)
        old = None
        try:
            if late_writer:
                async def old_writer():
                    async with held(0):
                        assert await store.read_marker_document(TXN) == staged
                        await store.write_marker_document(TXN, published)
                old = asyncio.create_task(old_writer())
                assert await asyncio.wait_for(asyncio.to_thread(at_syscall.wait, 5), 6)
                assert (await rows.read_marker(TXN))[0] == published
                # Observe actual lease expiry, not a DEL or synthetic ownership swap.
                async def expires():
                    while await redis.exists(key):
                        await asyncio.sleep(.02)
                await asyncio.wait_for(expires(), 3)
            async with held(1):
                assert await store.read_marker_document(TXN) == (published if late_writer else staged)
                await store.delete_marker_document(TXN)
                assert await rows.read_marker(TXN) == (None, None)
                assert not hint.exists()
        finally:
            resume_syscall.set()
            if old is not None:
                await asyncio.wait_for(old, 5)
        assert await rows.read_marker(TXN) == (None, None)
        lock_free = await store.read_marker_document(TXN)
        async with held(1):
            fresh_locked = await store.read_marker_document(TXN)
        pg_after = await rows.read_marker(TXN)
        assert (lock_free, fresh_locked, pg_after) == (None, None, (None, None)), (
            f"actual R gap: lock_free={lock_free!r}; fresh_locked={fresh_locked!r}; pg_after={pg_after!r}")

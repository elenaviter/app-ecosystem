"""Independent P storage cut (Infra, W700 HOLD 00:26Z), retained on the fix head: a late hint after the row's
conditional removal.

Adapted (Spark) in two places only: the pause matches the PUBLISHED marker by state, because the fixed store
tags the hint it writes (_card_rows_era), so the payload is no longer equal to the bare document; and both
reads, lock-free and locked, are recorded before asserting, so the locked/adoption check runs even when the
first read is wrong (Infra 00:26Z).

Real PG rows and the public BundleStorageDelegatedCardStore APIs, exact actual
file writes; no SQL edits or file substitutions. The lock is explicitly a
losable stand-in to isolate the storage fence promised for R's stale-write gap.
This is not a deployed/R/file-lock E2E proof.
"""
import asyncio
import contextlib
import os
import uuid

import asyncpg
import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("W661_TEST_PG_DSN"), reason="W661_TEST_PG_DSN is not set")

from connection_hub.delegated_credentials.cards.locks import row_fenced_lock
from connection_hub.delegated_credentials.cards.pointer_authority import PostgresCardRows
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore
from connection_hub.delegated_credentials.cards import store as store_module

TXN = "txn-" + "a" * 32


@contextlib.asynccontextmanager
async def _world(tmp_path):
    pool = await asyncpg.create_pool(os.environ["W661_TEST_PG_DSN"], min_size=1, max_size=2)
    project = "p" + uuid.uuid4().hex[:12]
    rows = PostgresCardRows(pool, schema="kdcube_t_" + project, tenant="t", project=project)
    await rows.ensure_schema()
    await rows.activate()
    store = BundleStorageDelegatedCardStore(tmp_path)
    store.card_rows = rows

    @contextlib.asynccontextmanager
    async def losable_stand_in(**kwargs):
        # The row CAS must preserve authority even if an old lock no longer excludes its successor.
        yield {}

    lock = row_fenced_lock(losable_stand_in)

    def held():
        return lock(lock_path=tmp_path / "txn-fixture.lock", resource_id=f"card-version-txn:{TXN}",
                    operation="independent-marker-cut", wait_seconds=2)

    try:
        yield store, rows, held
    finally:
        await pool.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("late_writer", [False, True])
async def test_removed_marker_row_must_not_be_resurrected_from_a_delayed_hint(tmp_path, monkeypatch, late_writer):
    async with _world(tmp_path) as (store, rows, held):
        staged = {"schema": "connection_hub.card-version-txn.v1", "txn": TXN,
                  "state": "staged", "members": []}
        published = {**staged, "state": "published"}
        async with held():
            assert await store.read_marker_document(TXN) is None
            await store.write_marker_document(TXN, staged)
        hint_path = store.card_version_marker_path(TXN)
        row_written, successor_done = asyncio.Event(), asyncio.Event()
        actual_write = store_module.write_json_atomic

        async def pause_after_actual_row_before_hint(path, payload):
            if late_writer and path == hint_path and payload.get("state") == "published":
                assert (await rows.read_marker(TXN))[0] == published  # real CAS already completed
                row_written.set()
                await asyncio.wait_for(successor_done.wait(), 5)
            return await actual_write(path, payload)

        monkeypatch.setattr(store_module, "write_json_atomic", pause_after_actual_row_before_hint)

        async def old_writer():
            async with held():
                assert await store.read_marker_document(TXN) == staged
                await store.write_marker_document(TXN, published)

        async def successor():
            if late_writer:
                await asyncio.wait_for(row_written.wait(), 5)
            async with held():
                assert await store.read_marker_document(TXN) == (published if late_writer else staged)
                await store.delete_marker_document(TXN)
                assert await rows.read_marker(TXN) == (None, None)
                assert not hint_path.exists()
            successor_done.set()

        if late_writer:
            await asyncio.wait_for(asyncio.gather(asyncio.create_task(old_writer()), asyncio.create_task(successor())), 8)
        else:
            await successor()
        if late_writer:
            assert hint_path.exists()  # the schedule really produced the late hint
        observed = await store.read_marker_document(TXN)  # lock-free public reader
        async with held():
            locked = await store.read_marker_document(TXN)  # a new writer must not adopt that stale hint
        row_after = await rows.read_marker(TXN)
        assert (observed, locked, row_after) == (None, None, (None, None)), (
            f"a removed authoritative marker was resurrected from its delayed file hint: "
            f"lock-free={observed} locked={locked} row={row_after}")

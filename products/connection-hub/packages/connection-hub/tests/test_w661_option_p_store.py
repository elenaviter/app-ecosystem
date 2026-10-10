"""W661 option P (the PG version check under R): the Card store's pointer and marker through their rows.

Real PostgreSQL rows (W661_TEST_PG_DSN), the real BundleStorageDelegatedCardStore, and locks.row_fenced_lock
around a stand-in mutation lock. Covers Spark's C2 (a reader prefers the row over the file hint), Infra's two
schedules (a delayed N+1 hint is never consumed; a legacy file writer after first touch never overrides the
row), the minimum pool, and the lock-free precondition read that must not write a marker.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid

import pytest

DSN = os.environ.get("W661_TEST_PG_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="W661_TEST_PG_DSN is not set")

S, A = "5" * 64, "a-1"


def _doc(n):
    return {"schema": "connection_hub.card-current.v1", "access_id": A, "card_revision": n,
            "revision_name": f"r{n:08d}"}


@contextlib.asynccontextmanager
async def _world(tmp_path, *, max_size=4):
    import asyncpg

    from connection_hub.delegated_credentials.cards.locks import row_fenced_lock
    from connection_hub.delegated_credentials.cards.pointer_authority import PostgresCardRows
    from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore

    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=max_size)
    project = "p" + uuid.uuid4().hex[:12]
    rows = PostgresCardRows(pool, schema="kdcube_t_" + project, tenant="t", project=project)
    await rows.ensure_schema()
    store = BundleStorageDelegatedCardStore(tmp_path)
    store.card_rows = rows
    locks: dict = {}

    @contextlib.asynccontextmanager
    async def base(*, lock_path, resource_id, operation, wait_seconds):
        lock = locks.setdefault(resource_id, asyncio.Lock())
        async with lock:
            yield {}

    try:
        yield store, rows, row_fenced_lock(base)
    finally:
        await pool.close()


def _card_lock(lock, store, access_id=A):
    return lock(lock_path=store.root / f"{access_id}.lock", resource_id=f"delegated-card:{access_id}",
                operation="test", wait_seconds=5.0)


async def _write_file_only(store, document):
    """A pointer hint written straight to current.json: a pre-P binary, or a hint written after its row."""
    from connection_hub.delegated_credentials.durable_io import write_json_atomic

    await write_json_atomic(store.current_path(subject_hash=S, access_id=A), document)


@pytest.mark.asyncio
async def test_c2_a_crash_between_the_row_and_its_hint_leaves_a_lock_free_reader_on_the_row(tmp_path):
    async with _world(tmp_path) as (store, rows, lock):
        await _write_file_only(store, _doc(1))
        async with _card_lock(lock, store):
            assert await store.read_pointer_document(subject_hash=S, access_id=A) == _doc(1)  # first touch
            await rows.write_pointer(subject_hash=S, access_id=A, document=_doc(2))  # row only: crash before hint
        assert await store.read_pointer_document(subject_hash=S, access_id=A) == _doc(2)  # lock-free: the row


@pytest.mark.asyncio
async def test_a_delayed_n_plus_1_hint_is_never_consumed(tmp_path):
    """Infra: the row says N; a late hint says N+1 (a stale writer's file write after its CAS was refused)."""
    async with _world(tmp_path) as (store, rows, lock):
        async with _card_lock(lock, store):
            await store.read_pointer_document(subject_hash=S, access_id=A)
            await store.write_pointer_document(subject_hash=S, access_id=A, document=_doc(1))
        await _write_file_only(store, _doc(2))  # the delayed hint
        assert await store.read_pointer_document(subject_hash=S, access_id=A) == _doc(1)
        async with _card_lock(lock, store):
            assert await store.read_pointer_document(subject_hash=S, access_id=A) == _doc(1)


@pytest.mark.asyncio
async def test_a_legacy_file_writer_after_first_touch_never_overrides_the_row(tmp_path):
    """Infra: migration against a legacy writer. Under the lock the file is adopted into the row; a pre-P
    writer then changes the file; the row stays authoritative, and the next locked write CASes from the row."""
    async with _world(tmp_path) as (store, rows, lock):
        await _write_file_only(store, _doc(3))
        async with _card_lock(lock, store):
            assert await store.read_pointer_document(subject_hash=S, access_id=A) == _doc(3)
        await _write_file_only(store, _doc(7))  # the legacy writer
        assert await store.read_pointer_document(subject_hash=S, access_id=A) == _doc(3)
        async with _card_lock(lock, store):
            await store.read_pointer_document(subject_hash=S, access_id=A)
            await store.write_pointer_document(subject_hash=S, access_id=A, document=_doc(4))
        assert await rows.read_pointer(subject_hash=S, access_id=A) == (_doc(4), 2)


@pytest.mark.asyncio
async def test_a_reader_without_the_cards_lock_never_creates_a_row(tmp_path):
    async with _world(tmp_path) as (store, rows, _lock):
        await _write_file_only(store, _doc(1))
        assert await store.read_pointer_document(subject_hash=S, access_id=A) == _doc(1)  # the hint
        assert await rows.read_pointer(subject_hash=S, access_id=A) == (None, None)


@pytest.mark.asyncio
async def test_the_minimum_pool_serves_concurrent_card_operations_without_a_deadlock(tmp_path):
    """One connection for everything: each row statement acquires and releases it, never across an await of
    another acquire, so independent Cards proceed and none waits on a held connection."""
    async with _world(tmp_path, max_size=1) as (store, rows, lock):
        async def one(index):
            access_id = f"a-{index}"
            async with _card_lock(lock, store, access_id):
                await store.read_pointer_document(subject_hash=S, access_id=access_id)
                await store.write_pointer_document(subject_hash=S, access_id=access_id,
                                                   document={**_doc(1), "access_id": access_id})

        await asyncio.wait_for(asyncio.gather(*(one(i) for i in range(8))), timeout=20)
        for index in range(8):
            assert (await rows.read_pointer(subject_hash=S, access_id=f"a-{index}"))[1] == 1


@pytest.mark.asyncio
async def test_a_write_outside_a_card_operation_is_refused_by_the_rows(tmp_path):
    from connection_hub.delegated_credentials.cards.store import CardStorageError

    async with _world(tmp_path) as (store, _rows, _lock):
        # The class method: PG mode's conftest wraps the instance's writer to seed fixtures under a lock.
        with pytest.raises(CardStorageError, match="card_lock_not_held"):
            await type(store).write_pointer_document(store, subject_hash=S, access_id=A, document=_doc(1))


@pytest.mark.asyncio
async def test_the_lock_free_precondition_read_never_writes_a_marker(tmp_path, monkeypatch):
    """service.current_revision's finalize runs outside the Card lock: under rows it checks, never marks."""
    from connection_hub.delegated_credentials.cards import transaction_store as tx

    async with _world(tmp_path) as (store, _rows, lock):
        marked = []
        monkeypatch.setattr(tx, "_mark_published", lambda store_, marker: marked.append(marker))
        assert tx._may_write_markers(store) is False
        async with _card_lock(lock, store):
            assert tx._may_write_markers(store) is True
        assert marked == []


@pytest.mark.asyncio
async def test_concurrent_replicas_bootstrap_the_schema_once_without_a_race(tmp_path):
    """KDCube critical-section-README, "Database Bootstrap Critical Section": proc replicas load the bundle at the
    same time; the DDL runs under pg_advisory_xact_lock, so concurrent CREATE ... IF NOT EXISTS never collide."""
    import asyncpg

    from connection_hub.delegated_credentials.cards.pointer_authority import PostgresCardRows

    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=8)
    try:
        project = "p" + uuid.uuid4().hex[:12]
        replicas = [PostgresCardRows(pool, schema="kdcube_t_" + project, tenant="t", project=project)
                    for _ in range(8)]
        await asyncio.gather(*(rows.ensure_schema() for rows in replicas))
        assert await replicas[0].is_activated() is False
    finally:
        await pool.close()

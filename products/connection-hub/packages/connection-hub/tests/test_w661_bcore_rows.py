"""W661 lock (b), b-core: the pointer and txn-marker rows fenced by compare-and-swap, on a REAL PostgreSQL.

Set W661_TEST_PG_DSN to a disposable database. Each test uses its own tenant/project schema.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DSN = os.environ.get("W661_TEST_PG_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="W661_TEST_PG_DSN is not set")

PLAIN = {"schema": "connection_hub.card-current.v1", "access_id": "a-1", "card_revision": 1}


async def _world(size=6):
    import asyncpg

    from connection_hub.delegated_credentials.cards.locks import postgres_advisory_lock
    from connection_hub.delegated_credentials.cards.pointer_authority import PostgresCardRows

    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=size)
    project = "p" + uuid.uuid4().hex[:12]
    rows = PostgresCardRows(pool, schema="kdcube_t_" + project, tenant="t", project=project)
    await rows.ensure_schema()
    return pool, rows, postgres_advisory_lock(pool, tenant="t", project=project)


def _held(lock, tmp_path, resource="delegated-card:a-1", wait=2.0):
    return lock(lock_path=tmp_path / resource.replace(":", "_"), resource_id=resource, operation="o",
                wait_seconds=wait)


def _doc(n):
    return {**PLAIN, "card_revision": n, "revision_name": f"r{n:08d}"}


@pytest.mark.asyncio
async def test_the_pointer_row_is_created_then_advanced_only_from_the_revision_read(tmp_path):
    pool, rows, lock = await _world()
    try:
        async with _held(lock, tmp_path):
            assert await rows.read_pointer(subject_hash="s", access_id="a-1") == (None, None)
            assert await rows.write_pointer(subject_hash="s", access_id="a-1", document=_doc(1)) == 1
            assert await rows.write_pointer(subject_hash="s", access_id="a-1", document=_doc(2)) == 2
        assert await rows.read_pointer(subject_hash="s", access_id="a-1") == (_doc(2), 2)  # lock-free read
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_a_stale_writer_is_refused_by_the_row_whatever_its_lock_state(tmp_path):
    """Two operations read revision 1 (A holding a lock it has in fact lost: here another resource id).
    B advances the row; A's write CASes from 1 and is refused card_pointer_moved; the row stays B's."""
    from connection_hub.delegated_credentials.cards.store import CardStorageError

    pool, rows, lock = await _world()
    try:
        async with _held(lock, tmp_path):
            await rows.read_pointer(subject_hash="s", access_id="a-1")
            await rows.write_pointer(subject_hash="s", access_id="a-1", document=_doc(1))
        a_read, b_done = asyncio.Event(), asyncio.Event()

        async def stale_a():
            async with _held(lock, tmp_path, resource="delegated-card:lost"):
                await rows.read_pointer(subject_hash="s", access_id="a-1")
                a_read.set()
                await b_done.wait()
                with pytest.raises(CardStorageError, match="card_pointer_moved"):
                    await rows.write_pointer(subject_hash="s", access_id="a-1", document=_doc(9))

        async def live_b():
            await a_read.wait()
            async with _held(lock, tmp_path):
                await rows.read_pointer(subject_hash="s", access_id="a-1")
                await rows.write_pointer(subject_hash="s", access_id="a-1", document=_doc(2))
            b_done.set()

        await asyncio.gather(asyncio.create_task(stale_a()), asyncio.create_task(live_b()))
        assert await rows.read_pointer(subject_hash="s", access_id="a-1") == (_doc(2), 2)
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_a_killed_session_cannot_write_after_another_writer_published(tmp_path):
    """The deterministic stale-writer schedule: A reads under its lock and pauses; its session is terminated
    (the lock is gone); B locks, reads and publishes; A resumes and its write is refused. The row is B's."""
    import asyncpg

    from connection_hub.delegated_credentials.cards.locks import current_lock_session

    pool, rows, lock = await _world()
    admin = await asyncpg.connect(DSN)
    try:
        a_paused, b_done = asyncio.Event(), asyncio.Event()

        async def stale_a():
            with pytest.raises(Exception):
                async with _held(lock, tmp_path):
                    await rows.read_pointer(subject_hash="s", access_id="a-1")
                    pid = await current_lock_session().fetchval("SELECT pg_backend_pid()")
                    a_paused.set()
                    await admin.execute("SELECT pg_terminate_backend($1)", pid)
                    await b_done.wait()
                    await rows.write_pointer(subject_hash="s", access_id="a-1", document=_doc(9))

        async def live_b():
            await a_paused.wait()
            async with _held(lock, tmp_path, wait=5.0):
                await rows.read_pointer(subject_hash="s", access_id="a-1")
                await rows.write_pointer(subject_hash="s", access_id="a-1", document=_doc(1))
            b_done.set()

        await asyncio.gather(asyncio.create_task(stale_a()), asyncio.create_task(live_b()))
        assert await rows.read_pointer(subject_hash="s", access_id="a-1") == (_doc(1), 1)
    finally:
        await admin.close()
        await pool.close()


@pytest.mark.asyncio
async def test_a_write_needs_the_lock_and_a_read_of_that_row_in_this_operation(tmp_path):
    from connection_hub.delegated_credentials.cards.store import CardStorageError

    pool, rows, lock = await _world()
    try:
        with pytest.raises(CardStorageError, match="card_lock_not_held"):
            await rows.write_pointer(subject_hash="s", access_id="a-1", document=_doc(1))
        async with _held(lock, tmp_path):
            with pytest.raises(CardStorageError, match="card_row_unobserved"):
                await rows.write_pointer(subject_hash="s", access_id="a-1", document=_doc(1))
        assert await rows.read_pointer(subject_hash="s", access_id="a-1") == (None, None)
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_first_touch_adopts_the_file_document_only_under_the_lock(tmp_path):
    from connection_hub.delegated_credentials.cards.store import CardStorageError

    pool, rows, lock = await _world()
    try:
        with pytest.raises(CardStorageError, match="card_lock_not_held"):
            await rows.adopt_pointer(subject_hash="s", access_id="a-1", document=_doc(4))
        assert await rows.read_pointer(subject_hash="s", access_id="a-1") == (None, None)  # a reader never creates
        async with _held(lock, tmp_path):
            await rows.read_pointer(subject_hash="s", access_id="a-1")
            assert await rows.adopt_pointer(subject_hash="s", access_id="a-1", document=_doc(4)) == (_doc(4), 1)
            # A second adoption keeps the existing row (ON CONFLICT DO NOTHING), never the file's.
            assert await rows.adopt_pointer(subject_hash="s", access_id="a-1", document=_doc(3)) == (_doc(4), 1)
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_the_marker_row_moves_only_by_cas_and_a_stale_delete_is_refused(tmp_path):
    from connection_hub.delegated_credentials.cards.store import CardStorageError

    pool, rows, lock = await _world()
    try:
        async with _held(lock, tmp_path):
            assert await rows.read_marker("txn-1") == (None, None)
            assert await rows.write_marker("txn-1", {"state": "staging"}) == 1
            assert await rows.write_marker("txn-1", {"state": "staged"}) == 2
        stale_read, live_done = asyncio.Event(), asyncio.Event()

        async def stale():
            async with _held(lock, tmp_path, resource="delegated-card:lost"):
                assert (await rows.read_marker("txn-1"))[0] == {"state": "staged"}
                stale_read.set()
                await live_done.wait()
                with pytest.raises(CardStorageError, match="card_version_marker_moved"):
                    await rows.delete_marker("txn-1")  # a stale ROLLBACK after the live PUBLISH
                with pytest.raises(CardStorageError, match="card_version_marker_moved"):
                    await rows.write_marker("txn-1", {"state": "staged"})

        async def live():
            await stale_read.wait()
            async with _held(lock, tmp_path):
                await rows.read_marker("txn-1")
                await rows.write_marker("txn-1", {"state": "published"})
            live_done.set()

        await asyncio.gather(asyncio.create_task(stale()), asyncio.create_task(live()))
        assert await rows.read_marker("txn-1") == ({"state": "published"}, 3)
        async with _held(lock, tmp_path):
            await rows.read_marker("txn-1")
            await rows.delete_marker("txn-1")
        assert await rows.read_marker("txn-1") == (None, None)
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_a_child_task_that_outlives_its_operation_cannot_use_the_session(tmp_path):
    from connection_hub.delegated_credentials.cards.store import CardStorageError

    pool, rows, lock = await _world()
    try:
        release = asyncio.Event()

        async def child():
            await release.wait()
            await rows.read_pointer(subject_hash="s", access_id="a-1")

        async with _held(lock, tmp_path):
            task = asyncio.create_task(child())  # inherits the operation's session
        release.set()
        with pytest.raises(CardStorageError, match="card_lock_session_closed"):
            await task
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_the_cutover_activation_is_recorded_once(tmp_path):
    pool, rows, _lock = await _world()
    try:
        assert await rows.is_activated() is False
        await rows.activate()
        await rows.activate()
        assert await rows.is_activated() is True
    finally:
        await pool.close()

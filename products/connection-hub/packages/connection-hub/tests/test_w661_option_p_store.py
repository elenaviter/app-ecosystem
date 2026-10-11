"""W661 option P (the PG version check under R): the Card store's pointer and marker through their rows.

Real PostgreSQL rows (W661_TEST_PG_DSN), the real BundleStorageDelegatedCardStore, and locks.row_fenced_lock
around a stand-in mutation lock. Covers Spark's C2 (a reader prefers the row over the file hint), Infra's two
schedules (a delayed N+1 hint is never consumed; a legacy file writer after first touch never overrides the
row), the minimum pool, and the lock-free precondition read that must not write a marker.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
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


@pytest.mark.asyncio
async def test_a_late_marker_hint_never_resurrects_a_deleted_row(tmp_path, monkeypatch):
    """Infra's HOLD on 24fdd314: A CASes its marker row to published and pauses before the file hint; B (whose lock
    A has lost) reads the row and deletes it, row and hint; A then writes the hint. The tagged late hint reads as
    absent, lock-free or locked; it is never adopted, and a locked reader removes it."""
    from connection_hub.delegated_credentials.cards import store as store_module
    from connection_hub.delegated_credentials.cards.store import MARKER_ROW_ERA_KEY

    txn = "w661-txn-" + "7" * 32
    async with _world(tmp_path) as (store, rows, lock):
        def held(resource):
            return lock(lock_path=store.root / f"{resource}.lock", resource_id=resource, operation="test",
                        wait_seconds=5.0)

        async with held("card-version-txn:" + txn):
            assert await store.read_marker_document(txn) is None
            await store.write_marker_document(txn, {"txn": txn, "state": "staged"})
        real_write, a_paused, b_done = store_module.write_json_atomic, asyncio.Event(), asyncio.Event()

        async def pausing(path, payload):
            if path == store.card_version_marker_path(txn) and payload.get("state") == "published":
                a_paused.set()
                await b_done.wait()  # A stalls between its row CAS and its hint write
            await real_write(path, payload)

        monkeypatch.setattr(store_module, "write_json_atomic", pausing)

        async def stale_a():
            async with held("card-version-txn:" + txn):
                assert (await store.read_marker_document(txn))["state"] == "staged"
                await store.write_marker_document(txn, {"txn": txn, "state": "published"})

        async def live_b():
            await a_paused.wait()
            async with held("card-version-txn:lost-" + txn):  # B holds the lock A has lost
                assert (await store.read_marker_document(txn))["state"] == "published"
                await store.delete_marker_document(txn)
            b_done.set()

        await asyncio.gather(stale_a(), live_b())
        late = store.card_version_marker_path(txn)
        assert late.exists() and json.loads(late.read_text())[MARKER_ROW_ERA_KEY] is True  # the late hint landed
        assert await rows.read_marker(txn) == (None, None)
        assert await store.read_marker_document(txn) is None  # lock-free: absent, not the late hint
        async with held("card-version-txn:" + txn):
            assert await store.read_marker_document(txn) is None  # locked: absent, never adopted
        assert not late.exists()  # the orphan hint was removed by the locked reader
        assert await rows.read_marker(txn) == (None, None)


@pytest.mark.asyncio
async def test_a_pre_cutover_marker_file_is_still_adopted_under_the_lock(tmp_path):
    """The control for the late-hint fix: an UNTAGGED marker file (written before activation) is adopted."""
    from connection_hub.delegated_credentials.durable_io import write_json_atomic

    txn = "w661-txn-" + "8" * 32
    async with _world(tmp_path) as (store, rows, lock):
        await write_json_atomic(store.card_version_marker_path(txn), {"txn": txn, "state": "staged"})
        assert (await store.read_marker_document(txn))["state"] == "staged"  # lock-free: the hint, nothing created
        assert await rows.read_marker(txn) == (None, None)
        async with lock(lock_path=store.root / "t.lock", resource_id="card-version-txn:" + txn, operation="test",
                        wait_seconds=5.0):
            assert (await store.read_marker_document(txn))["state"] == "staged"
        assert (await rows.read_marker(txn))[0] == {"txn": txn, "state": "staged"}


@pytest.mark.asyncio
async def test_a_stale_marker_cas_never_matches_a_re_created_row_at_the_same_revision(tmp_path):
    """Infra's ABA HOLD on 9cd86893: A reads the staging marker (revision 1) and stalls; B deletes the marker
    (ROLLBACK) and re-creates it for the same txn (re-STAGE), again at revision 1; A's CAS from its revision-1
    observation must be refused (card_version_marker_moved), and B's row must stay B's. The CAS names the row's
    incarnation as well as its revision, so a re-created row never matches a stale observation."""
    from connection_hub.delegated_credentials.cards.store import CardStorageError

    txn = "w661-txn-" + "9" * 32
    async with _world(tmp_path) as (store, rows, lock):
        def held(resource):
            return lock(lock_path=store.root / f"{resource}.lock", resource_id=resource, operation="test",
                        wait_seconds=5.0)

        async with held("card-version-txn:" + txn):
            assert await store.read_marker_document(txn) is None
            await store.write_marker_document(txn, {"txn": txn, "state": "staging", "by": "A"})
        a_read, b_done = asyncio.Event(), asyncio.Event()

        async def stale_a():
            async with held("card-version-txn:" + txn):
                assert (await store.read_marker_document(txn))["by"] == "A"  # observes revision 1
                a_read.set()
                await b_done.wait()
                with pytest.raises(CardStorageError, match="card_version_marker_moved"):
                    await store.write_marker_document(txn, {"txn": txn, "state": "staged", "by": "A"})

        async def live_b():
            await a_read.wait()
            async with held("card-version-txn:lost-" + txn):  # B holds the lock A has lost
                assert (await store.read_marker_document(txn))["by"] == "A"
                await store.delete_marker_document(txn)  # ROLLBACK
            async with held("card-version-txn:lost2-" + txn):
                assert await store.read_marker_document(txn) is None
                await store.write_marker_document(txn, {"txn": txn, "state": "staging", "by": "B"})  # re-STAGE
            assert (await rows.read_marker(txn))[1] == 1  # the same revision number A observed
            b_done.set()

        await asyncio.gather(stale_a(), live_b())
        assert await rows.read_marker(txn) == ({"txn": txn, "state": "staging", "by": "B"}, 1)
        assert (await store.read_marker_document(txn))["by"] == "B"

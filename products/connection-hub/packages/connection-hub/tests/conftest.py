"""Two opt-in modes that re-run the Hub Card suites under the W661 lock stack; both may be on at once.

W661 Card lock option R: an opt-in mode that re-runs the Hub suites with the per-Card KDCube Redis lock.

With W661_CARD_LOCK_R_MODE=1 and W661_TEST_REDIS_URL set, every DelegatedCardService takes the R lock (the SDK's
observed_redis_lock_async per Card, renewed) INSIDE the test's own lock (its pause points model "before the lock"),
and every BundleStorageDelegatedCardStore checks, before each write made inside an R operation, that the operation
still owns every Card key it holds. Writes outside any R operation are test fixtures seeding the store; production
Card writers all run inside the lock (the b-core PG-mode run showed the only unlocked writes were fixtures). Off by
default.

W661 option P (b-core): an opt-in mode that re-runs the Card suites with PostgreSQL pointer and marker rows.

With W661_BCORE_PG_MODE=1 and W661_TEST_PG_DSN set, every BundleStorageDelegatedCardStore gets PostgreSQL pointer
and marker rows (its own schema per store) and every DelegatedCardService's lock opens the row context
(locks.row_fenced_lock) inside the test's own lock. The same assertions then run against the row authority. Off by default.
"""


from __future__ import annotations

import asyncio
import contextlib
import os
import uuid

import pytest

_R_MODE = os.environ.get("W661_CARD_LOCK_R_MODE") == "1" and bool(os.environ.get("W661_TEST_REDIS_URL"))
_PG_MODE = os.environ.get("W661_BCORE_PG_MODE") == "1" and bool(os.environ.get("W661_TEST_PG_DSN"))

# A reviewer witness retained byte-for-byte (W700 P, Infra's) that needs the real PostgreSQL and Redis but carries
# no skip of its own: skipped here, as its sibling P tests skip themselves, so a run without them shows no false red.
_REVIEWER_REAL_PG_REDIS_MODULES = {"test_w661_p_delayed_claim_read_infra.py"}


def pytest_collection_modifyitems(config, items):
    if os.environ.get("W661_TEST_PG_DSN") and os.environ.get("W661_TEST_REDIS_URL"):
        return
    skip = pytest.mark.skip(reason="W661_TEST_PG_DSN and W661_TEST_REDIS_URL are required")
    for item in items:
        if item.path.name in _REVIEWER_REAL_PG_REDIS_MODULES:
            item.add_marker(skip)


class _LazyPool:
    """A pool stand-in that works under any event loop (pytest-asyncio's loop per test, or a sync test's own
    asyncio.run): ONE connection per running loop, reused across acquires and serialized by a per-loop lock, as a
    one-connection pool would. The connection is closed inside its loop's own close(), so none outlives its loop
    and nothing is left for the garbage collector ("unclosed" under -W always::ResourceWarning), without paying a
    new connection per statement (timing-sensitive tests, e.g. test_lifecycle_store's cancellation drain)."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._per_loop: dict = {}  # id(loop) -> [loop, lock, connection or None]

    async def _slot(self):
        """This loop's [loop, lock, connection]. The entry and its lock are created synchronously, before any
        await, so concurrent first acquires in one loop share ONE lock; the connection is opened by whichever of
        them holds that lock first (see acquire), so a second connection is never opened and left behind."""
        loop = asyncio.get_running_loop()
        slot = self._per_loop.get(id(loop))
        if slot is None or slot[0] is not loop:
            slot = [loop, asyncio.Lock(), None]
            self._per_loop[id(loop)] = slot
            self._close_with(loop)
        return slot

    def _close_with(self, loop) -> None:
        """Wrap this loop's close() once PER POOL: close this pool's connection in the loop first (asyncio.run and
        pytest-asyncio both stop the loop, then call close() on it). Each pool registers itself on the loop, so a
        second pool on the same loop is closed too."""
        pools = getattr(loop, "_w661_lazy_pools", None)
        if pools is None:
            pools = loop._w661_lazy_pools = []
            original_close = loop.close

            def close() -> None:
                for pool in list(pools):
                    pool._close_in(loop)
                original_close()

            loop.close = close
        if self not in pools:
            pools.append(self)

    def _close_in(self, loop) -> None:
        slot = self._per_loop.pop(id(loop), None)
        connection = None if slot is None or slot[0] is not loop else slot[2]
        if connection is None or connection.is_closed():
            return
        if not loop.is_running() and not loop.is_closed():
            try:
                loop.run_until_complete(connection.close())
                return
            except Exception:  # noqa: BLE001 - a test stand-in: never mask the test's own outcome
                pass
        connection.terminate()

    def acquire(self):
        pool = self

        class _Acquire:
            async def __aenter__(self):
                slot = await pool._slot()
                self._lock = slot[1]
                await self._lock.acquire()
                try:
                    if slot[2] is None or slot[2].is_closed():
                        import asyncpg

                        slot[2] = await asyncpg.connect(pool._dsn)
                except BaseException:
                    self._lock.release()
                    raise
                return slot[2]

            async def __aexit__(self, *exc):
                self._lock.release()

        return _Acquire()

    async def release(self, connection) -> None:
        return None

    def terminate(self) -> None:
        """Test teardown: a connection whose loop stopped without close() (a thread's own loop) is closed in that
        loop now; one on a loop still running elsewhere is aborted."""
        for loop, _lock, connection in list(self._per_loop.values()):
            if connection is None or connection.is_closed():
                continue
            if not loop.is_running() and not loop.is_closed():
                try:
                    loop.run_until_complete(connection.close())
                    continue
                except Exception:  # noqa: BLE001 - a test stand-in: never mask the test's own outcome
                    pass
            try:
                connection.terminate()
            except RuntimeError:
                pass
        self._per_loop.clear()


class _LazyRows:
    """PostgresCardRows whose schema is created on first use in the running loop."""

    def __init__(self, pool: _LazyPool, root: str) -> None:
        import hashlib

        from connection_hub.delegated_credentials.cards.pointer_authority import PostgresCardRows

        # One schema per storage root, the same in every process (a test's child processes share its rows).
        project = "p" + hashlib.sha256(root.encode()).hexdigest()[:12]
        self._rows = PostgresCardRows(pool, schema="kdcube_t_" + project, tenant="t", project=project)
        self._ready: set = set()

    def __getattr__(self, name):
        method = getattr(self._rows, name)
        if not callable(method) or name.startswith("_"):
            return method

        async def call(*args, **kwargs):
            loop = asyncio.get_running_loop()
            if loop not in self._ready:
                await self._rows.ensure_schema()
                self._ready.add(loop)
            return await method(*args, **kwargs)

        return call



def _audit_unlocked_write(kind: str) -> None:
    """W661_PG_AUDIT=<file>: record each pointer/marker write made outside any Card row operation, as
    "<kind> <entry> <- <test>". The entry is the first source frame below the test: a store or transaction_store
    function means the test drives the store directly (a fixture seeding it); a service, participant or Hub
    method there would be a production writer outside the Card lock, which P refuses (Ops' N1)."""
    path = os.environ.get("W661_PG_AUDIT")
    if not path:
        return
    import traceback

    frames = [frame for frame in traceback.extract_stack()[:-2] if "conftest.py" not in frame.filename]
    test_index = max((i for i, frame in enumerate(frames) if "/tests/" in frame.filename), default=-1)
    below = [frame for frame in frames[test_index + 1:] if "/src/" in frame.filename]
    entry = below[0] if below else None
    test = frames[test_index] if test_index >= 0 else None
    with open(path, "a") as out:
        if os.environ.get("W661_PG_AUDIT_STACK") == "1":
            out.write("".join(f"  {frame.filename.split('/src/')[-1]}:{frame.lineno} {frame.name}\n"
                              for frame in frames[max(test_index, 0):] if "/src/" in frame.filename
                              or "/tests/" in frame.filename))
        out.write(f"{kind} {entry.filename.split('/src/')[-1] + ':' + entry.name if entry else '-'} <- "
                  f"{test.filename.split('/tests/')[-1] + ':' + test.name if test else '-'}\n")


@pytest.fixture(autouse=True)
def _w661_bcore_pg_mode(monkeypatch):
    if not _PG_MODE:
        yield
        return
    pool = install_pg_mode(lambda target, name, value: monkeypatch.setattr(target, name, value, raising=False))
    yield
    pool.terminate()


def install_pg_mode(set_attr):
    """Patch the store and service classes for PG mode; ``set_attr`` is monkeypatch.setattr, or setattr in a
    test's child process (a child runs without this conftest, and must not write file-only next to rows)."""
    from connection_hub.delegated_credentials.cards import service as service_module
    from connection_hub.delegated_credentials.cards import store as store_module
    from connection_hub.delegated_credentials.cards.locks import row_fenced_lock

    @contextlib.asynccontextmanager
    async def _no_extra_lock(**_kwargs):
        yield {}

    pool = _LazyPool(os.environ["W661_TEST_PG_DSN"])
    original_store_init = store_module.BundleStorageDelegatedCardStore.__init__
    rows_by_root: dict = {}

    fixture_lock = row_fenced_lock(_no_extra_lock)

    def store_init(self, storage_root, *args, **kwargs):
        original_store_init(self, storage_root, *args, **kwargs)
        # Stores over the same root share one authority, as two processes over one bundle storage would.
        self.card_rows = rows_by_root.get(str(self.root)) or rows_by_root.setdefault(str(self.root), _LazyRows(pool, str(self.root)))
        _lock_fixture_writes(self)

    def _lock_fixture_writes(store):
        """Test fixtures seed and corrupt the store directly, outside any service. Production writers always
        hold the Card lock (the strict refusal has its own tests); here a direct write outside a lock runs as a
        locked writer would: it takes that Card's lock and reads the row first, then CASes."""
        from connection_hub.delegated_credentials.cards.locks import current_lock_session

        write_pointer, write_marker = store.write_pointer_document, store.write_marker_document
        delete_marker = store.delete_marker_document

        async def locked_pointer(*, subject_hash, access_id, document, card_version_txn=""):
            if current_lock_session() is not None:
                return await write_pointer(subject_hash=subject_hash, access_id=access_id, document=document,
                                           card_version_txn=card_version_txn)
            _audit_unlocked_write("pointer")
            async with fixture_lock(lock_path=store.root / ".fixture-lock", resource_id=f"delegated-card:{access_id}",
                                    operation="test-fixture", wait_seconds=5.0):
                await store.read_pointer_document(subject_hash=subject_hash, access_id=access_id)
                return await write_pointer(subject_hash=subject_hash, access_id=access_id, document=document,
                                           card_version_txn=card_version_txn)

        async def locked_marker(write, txn, *args):
            if current_lock_session() is not None:
                return await write(txn, *args)
            _audit_unlocked_write("marker")
            async with fixture_lock(lock_path=store.root / ".fixture-lock", resource_id=f"card-version-txn:{txn}",
                                    operation="test-fixture", wait_seconds=5.0):
                await store.card_rows.read_marker(txn)
                return await write(txn, *args)

        store.write_pointer_document = locked_pointer
        store.write_marker_document = lambda txn, document: locked_marker(write_marker, txn, document)
        store.delete_marker_document = lambda txn: locked_marker(delete_marker, txn)

    pg_lock = row_fenced_lock(_no_extra_lock)

    def with_rows(test_lock):
        if test_lock is None or getattr(test_lock, "_w661_rows", False):
            return test_lock

        @contextlib.asynccontextmanager
        async def mutation_lock(**kwargs_):
            # The test's own lock stays OUTSIDE (its pause points model "before the lock"); the row context
            # (option P's observations) is inside it, as under the production lock stack.
            async with test_lock(**kwargs_) as held:
                async with pg_lock(**kwargs_):
                    yield held

        mutation_lock._w661_rows = True
        return mutation_lock

    # A property, so a lock a test assigns after construction (service._mutation_lock = ...) is wrapped too. The
    # wrapped lock is also the instance's own attribute: a test's monkeypatch.undo() removes this class property,
    # and the instance then keeps using the wrapped lock it already had.
    def get_lock(self):
        return self.__dict__.get("_mutation_lock")

    def set_lock(self, lock):
        self.__dict__["_mutation_lock"] = with_rows(lock)

    set_attr(store_module.BundleStorageDelegatedCardStore, "__init__", store_init)
    set_attr(service_module.DelegatedCardService, "_mutation_lock", property(get_lock, set_lock))
    return pool


# The R-mode Redis clients of the running test. W707: they were never closed, so their connections were
# finalized later, inside whatever ran next; on CPython 3.11 < 3.11.8 a finalizer error inside ast.parse
# raised "AST constructor recursion depth mismatch" in test_issuer_gate (cpython issue 106905).
_R_CLIENTS: list = []


@pytest.fixture(autouse=True)
def _w661_card_lock_r_mode(monkeypatch, request):
    # A module that builds its own R locks (W661_OWN_R_LOCK) is not wrapped again: a second, production-TTL
    # lock around a test's short-TTL lock would hold the key the test expects to expire.
    if not _R_MODE or getattr(request.module, "W661_OWN_R_LOCK", False):
        yield
        return
    import redis.asyncio as aioredis
    from kdcube_ai_app.storage.observed_file_locks import make_lock_metadata
    from kdcube_ai_app.storage.observed_redis_locks import observed_redis_lock_async

    from connection_hub.delegated_credentials.cards import redis_lock
    from connection_hub.delegated_credentials.cards import service as service_module
    from connection_hub.delegated_credentials.cards import store as store_module
    from connection_hub.delegated_credentials.durable_io import guard_writes_under

    clients = []
    project = "r-" + uuid.uuid4().hex[:12]  # one project per test: parallel workers never share Card keys

    @contextlib.asynccontextmanager
    async def no_file_lock(**kwargs):
        yield None

    def r_lock():
        client = aioredis.from_url(os.environ["W661_TEST_REDIS_URL"])
        clients.append(client)
        _R_CLIENTS.append(client)
        return redis_lock.redis_card_mutation_lock(client, observed_lock=observed_redis_lock_async,
                                                   make_metadata=make_lock_metadata, tenant="t",
                                                   project=project, base_lock=no_file_lock)

    def _record_outside():
        # Diagnostic (W661_CARD_LOCK_R_AUDIT=<file>): which SOURCE caller wrote outside any R operation.
        audit = os.environ.get("W661_CARD_LOCK_R_AUDIT")
        if not audit:
            return
        import traceback
        frames = [f for f in traceback.extract_stack()[:-2]
                  if "/src/connection_hub/" in f.filename and "durable_io.py" not in f.filename
                  and "redis_lock.py" not in f.filename]
        if frames:
            top = frames[-1]
            with open(audit, "a", encoding="utf-8") as fh:
                fh.write(f"{top.filename.split('/src/')[-1]}:{top.lineno}:{top.name}\n")

    async def owner_guard():
        if redis_lock._OPERATION.get() is None:
            _record_outside()
        await redis_lock.assert_card_lock_owned()  # the production guard, unchanged

    def owner_guard_sync():
        if redis_lock._OPERATION.get() is None:
            _record_outside()
        return redis_lock._assert_card_lock_held()

    owner_guard.sync = owner_guard_sync
    original_store_init = store_module.BundleStorageDelegatedCardStore.__init__
    original_service_init = service_module.DelegatedCardService.__init__

    def store_init(self, storage_root, *args, **kwargs):
        original_store_init(self, storage_root, *args, **kwargs)
        guard_writes_under(self.root, owner_guard, name="card_lock_owner")

    def service_init(self, *args, mutation_lock=None, **kwargs):
        if mutation_lock is not None:
            test_lock, lock_r = mutation_lock, r_lock()

            @contextlib.asynccontextmanager
            async def mutation_lock(**kwargs_):
                async with test_lock(**kwargs_) as held:
                    async with lock_r(**kwargs_):
                        yield held
        original_service_init(self, *args, mutation_lock=mutation_lock, **kwargs)

    monkeypatch.setattr(store_module.BundleStorageDelegatedCardStore, "__init__", store_init)
    monkeypatch.setattr(service_module.DelegatedCardService, "__init__", service_init)
    yield


@pytest.fixture(autouse=True)
async def _w661_card_lock_r_mode_clients(_w661_card_lock_r_mode):
    """Close this test's R-mode Redis clients in its own event loop, before that loop closes (W707)."""
    yield
    clients, _R_CLIENTS[:] = list(_R_CLIENTS), []
    for client in clients:
        try:
            await client.aclose()
        except RuntimeError:
            pass  # used on a loop the test closed itself (asyncio.run in a sync test): collected just below
    if clients:
        import gc
        gc.collect()  # any connection left runs its finalizer HERE, in this test's teardown, not in a later test

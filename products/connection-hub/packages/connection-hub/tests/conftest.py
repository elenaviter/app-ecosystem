"""W661 Card lock option R: an opt-in mode that re-runs the Hub suites with the per-Card KDCube Redis lock.

With W661_CARD_LOCK_R_MODE=1 and W661_TEST_REDIS_URL set, every DelegatedCardService takes the R lock (the SDK's
observed_redis_lock_async per Card, renewed) INSIDE the test's own lock (its pause points model "before the lock"),
and every BundleStorageDelegatedCardStore checks, before each write made inside an R operation, that the operation
still owns every Card key it holds. Writes outside any R operation are test fixtures seeding the store; production
Card writers all run inside the lock (the b-core PG-mode run showed the only unlocked writes were fixtures). Off by
default.
"""

from __future__ import annotations

import contextlib
import os
import uuid

import pytest

_R_MODE = os.environ.get("W661_CARD_LOCK_R_MODE") == "1" and bool(os.environ.get("W661_TEST_REDIS_URL"))


@pytest.fixture(autouse=True)
def _w661_card_lock_r_mode(monkeypatch):
    if not _R_MODE:
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
        return redis_lock.redis_card_mutation_lock(client, observed_lock=observed_redis_lock_async,
                                                   make_metadata=make_lock_metadata, tenant="t",
                                                   project=project, base_lock=no_file_lock)

    async def owner_guard():
        if redis_lock._OPERATION.get() is None:
            return None  # a fixture write outside any R operation
        await redis_lock.assert_card_lock_owned()

    owner_guard.sync = lambda: None if redis_lock._OPERATION.get() is None else redis_lock._assert_card_lock_held()
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

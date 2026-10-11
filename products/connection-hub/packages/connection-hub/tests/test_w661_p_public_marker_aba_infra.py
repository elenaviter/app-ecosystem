"""Independent public signed STAGE/ROLLBACK/re-STAGE marker CAS cut.

Real public handler, planner, ServiceCardVersionStore, Card service, PG rows,
SDK R and observed flock; owned local roots model two containers. Only
catalog resolution, serving cache and proc role use declared author fixtures.
The old writer's real TTL expires; no Redis DEL, SQL edits or file substitution.
Pause before real SQL CAS, not a fake CAS. Not deployed or full PB E2E.


Retained on the P fix head (Spark): unchanged except the helper path (see _historical below). It fails on the
pre-fix pointer authority (a stale public STAGE overwrote the public retry's re-created row) and passes once the
marker CAS names the row incarnation.
"""
import asyncio
import importlib.util
import os
import uuid
from pathlib import Path

import asyncpg
import pytest

pytestmark = pytest.mark.skipif(not (os.environ.get("W661_TEST_PG_DSN") and os.environ.get("W661_TEST_REDIS_URL")),
                                reason="W661_TEST_PG_DSN and W661_TEST_REDIS_URL are required")
W661_OWN_R_LOCK = True  # builds its own real SDK R locks (1 s TTL); the R-mode harness must not wrap them again
from redis.asyncio import Redis

from connection_hub.delegated_credentials import durable_io
from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.locks import guard_card_store_writes, hub_redis_card_mutation_lock
from connection_hub.delegated_credentials.cards.pointer_authority import PostgresCardRows
from connection_hub.delegated_credentials.cards.redis_lock import assert_card_lock_owned, _assert_card_lock_held, card_lock_key
from connection_hub.delegated_credentials.cards.service import DelegatedCardService
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore, subject_hash_for
from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.cards.service import _kdcube_card_mutation_lock
from kdcube_ai_app.storage.observed_file_locks import make_lock_metadata
from kdcube_ai_app.storage.observed_redis_locks import observed_redis_lock_async
from test_card_service import _Cache

# Retained in P (Spark): Infra's historical public-handler helper, verbatim, kept next to this file under a
# non-collected name (it is only a helper here).
_historical = Path(__file__).parent / "_infra_historical_p2_public_boundary.py"
_spec = importlib.util.spec_from_file_location("independent_historical_public_helpers", _historical)
hist = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hist)


class PWorld(hist._World):
    def __init__(self, root, redis, rows, container):
        super().__init__(root, redis)
        self.rows, self.container = rows, container

    def storage(self):
        store = BundleStorageDelegatedCardStore(self.root / "cards")
        store.card_rows = self.rows
        guard_card_store_writes(store, role=lambda: "proc")
        async def owner_guard():
            await assert_card_lock_owned()
        owner_guard.sync = _assert_card_lock_held
        durable_io.guard_writes_under(store.root, owner_guard, name="card_lock_owner")
        lock = hub_redis_card_mutation_lock(
            self.redis, _kdcube_card_mutation_lock, self.root / self.container,
            tenant="t", project=self.rows.project, observed_lock=observed_redis_lock_async,
            make_metadata=make_lock_metadata, role=lambda: "proc", ttl_seconds=1, renew_seconds=None)
        return store, DelegatedCardService(store=store, cache=_Cache(), mutation_lock=lock)


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_writer", [False, True])
async def test_a_stale_public_stage_never_overwrites_the_same_txn_recreated_by_a_public_retry(tmp_path, monkeypatch, stale_writer):
    pool = await asyncpg.create_pool(os.environ["W661_TEST_PG_DSN"], min_size=1, max_size=4)
    redis = Redis.from_url(os.environ["W661_TEST_REDIS_URL"])
    a, b = None, None
    release_a, release_b = asyncio.Event(), asyncio.Event()
    try:
        project = "aba" + uuid.uuid4().hex[:12]
        rows = PostgresCardRows(pool, schema="kdcube_t_" + project, tenant="t", project=project)
        await rows.ensure_schema()
        await rows.activate()
        old = PWorld(tmp_path, redis, rows, "old-container")
        live = PWorld(tmp_path, redis, rows, "successor-container")
        _, card, _ = await old.seed()
        a_at_cas, b_row_created = asyncio.Event(), asyncio.Event()
        actual_marker_write = tx._write_card_version_marker

        async def pause_marker_write(store, marker):
            if stale_writer and marker.get("request_id") == "old-stage" and marker["state"] == "staged":
                a_at_cas.set()
                await asyncio.wait_for(release_a.wait(), 12)
            await actual_marker_write(store, marker)
            if marker.get("request_id") == "retry-stage" and marker["state"] == "staging":
                b_row_created.set()
                await asyncio.wait_for(release_b.wait(), 12)
        monkeypatch.setattr(tx, "_write_card_version_marker", pause_marker_write)

        a = asyncio.create_task(hist._call(old, "stage", request_id="old-stage", updates=[hist._edit(card, 1)]))
        if stale_writer:
            await asyncio.wait_for(a_at_cas.wait(), 5)
            before_rollback = await rows.read_marker(hist.wire.TXN)
            assert before_rollback[0]["state"] == "staging" and before_rollback[1] == 1
            _, service = old.storage()
            key = card_lock_key(tenant="t", project=project, resource_id=f"delegated-card:{card.access_id}",
                                lock_path=service._lock_path(subject_hash=subject_hash_for(card.grantor_subject),
                                                           access_id=card.access_id))
            async def expired():
                while await redis.exists(key):
                    await asyncio.sleep(.02)
            await asyncio.wait_for(expired(), 3)
        else:
            result, _ = await asyncio.wait_for(a, 5)
            assert result["kind"] == "staged"

        rolled_back, _ = await hist._call(live, "rollback", links=[])
        assert rolled_back == {"kind": "rollback", "state": "rolled_back"}, rolled_back
        assert await rows.read_marker(hist.wire.TXN) == (None, None)

        b = asyncio.create_task(hist._call(live, "stage", request_id="retry-stage", updates=[hist._edit(card, 2)]))
        await asyncio.wait_for(b_row_created.wait(), 5)
        retry_before = await rows.read_marker(hist.wire.TXN)
        assert retry_before[0]["request_id"] == "retry-stage" and retry_before[1] == 1
        release_a.set()
        old_answer, _ = await asyncio.wait_for(a, 5)
        after_old_cas = await rows.read_marker(hist.wire.TXN)
        release_b.set()
        retry_answer, _ = await asyncio.wait_for(b, 5)
        final_row = await rows.read_marker(hist.wire.TXN)
        assert (after_old_cas == retry_before and retry_answer["kind"] == "staged"
                and final_row[0]["request_id"] == "retry-stage"), (
            f"public marker ABA: stale={stale_writer}; old={old_answer!r}; retry={retry_answer!r}; "
            f"before_retry={retry_before!r}; after_old_CAS={after_old_cas!r}; final={final_row!r}")
    finally:
        release_a.set()
        release_b.set()
        await asyncio.gather(*(task for task in (a, b) if task is not None), return_exceptions=True)
        await redis.aclose()
        await pool.close()

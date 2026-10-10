"""W661 Card lock option R (operator 10 Oct, "R now, with P"): the KDCube per-Card Redis lock on a REAL Redis.

Uses the SDK's own observed_redis_lock_async and make_lock_metadata, unchanged. Set W661_TEST_REDIS_URL to a
disposable Redis (default maxmemory-policy noeviction).
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from contextlib import asynccontextmanager

import pytest

W661_OWN_R_LOCK = True  # these tests build their own R locks; the R-mode harness must not wrap them again

URL = os.environ.get("W661_TEST_REDIS_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="W661_TEST_REDIS_URL is not set")


def _client(url=None):
    import redis.asyncio as aioredis

    return aioredis.from_url(url or URL)


@asynccontextmanager
async def _file_lock(**kwargs):
    yield {"file_lock": kwargs["resource_id"]}


def _lock(redis, **settings):
    from kdcube_ai_app.storage.observed_file_locks import make_lock_metadata
    from kdcube_ai_app.storage.observed_redis_locks import observed_redis_lock_async

    from connection_hub.delegated_credentials.cards.redis_lock import redis_card_mutation_lock

    project = "p" + uuid.uuid4().hex[:8]
    lock = redis_card_mutation_lock(redis, observed_lock=observed_redis_lock_async, make_metadata=make_lock_metadata,
                                    tenant="t", project=project, base_lock=_file_lock, **settings)
    lock.keys = f"kdcube:cards:lock:t:{project}:*"  # this test's own keys only
    return lock


def _card(tmp_path, access_id="a-1", subject="s" * 64):
    path = tmp_path / "grantors" / subject / "cards" / access_id / "card.lock"
    return {"lock_path": path, "resource_id": f"delegated-card:{access_id}", "operation": "test"}


def _store(tmp_path):
    from connection_hub.delegated_credentials.cards.redis_lock import guard_card_store_lock_owner
    from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore

    return guard_card_store_lock_owner(BundleStorageDelegatedCardStore(tmp_path / "bundle"))


async def _write(store, name="x.json"):
    from connection_hub.delegated_credentials.durable_io import write_json_atomic

    await write_json_atomic(store.root / name, {"ok": True})


@pytest.mark.asyncio
async def test_one_card_is_serialized_across_operations_and_a_held_key_times_out(tmp_path):
    from connection_hub.delegated_credentials.cards.service import CardMutationLockTimeout

    redis = _client()
    lock = _lock(redis)
    entered, release = asyncio.Event(), asyncio.Event()

    async def holder():
        async with lock(**_card(tmp_path), wait_seconds=2):
            entered.set()
            await release.wait()

    task = asyncio.create_task(holder())
    await entered.wait()
    with pytest.raises(CardMutationLockTimeout, match="card_mutation_lock_timeout"):
        await asyncio.create_task(_enter(lock, tmp_path, wait=0.3))
    release.set()
    await task
    await asyncio.create_task(_enter(lock, tmp_path, wait=1))  # free again
    await redis.aclose()


async def _enter(lock, tmp_path, *, wait, access_id="a-1"):
    async with lock(**_card(tmp_path, access_id=access_id), wait_seconds=wait):
        pass


@pytest.mark.asyncio
async def test_two_different_cards_are_held_at_once(tmp_path):
    redis = _client()
    lock = _lock(redis)
    release = asyncio.Event()
    entered = asyncio.Event()

    async def holder():
        async with lock(**_card(tmp_path, "a-1"), wait_seconds=1):
            entered.set()
            await release.wait()

    task = asyncio.create_task(holder())
    await entered.wait()
    started = time.monotonic()
    await asyncio.create_task(_enter(lock, tmp_path, wait=1, access_id="a-2"))
    assert time.monotonic() - started < 0.5
    release.set()
    await task
    await redis.aclose()


@pytest.mark.asyncio
async def test_renewal_keeps_a_long_operation_the_owner(tmp_path):
    redis = _client()
    lock = _lock(redis, ttl_seconds=1, renew_seconds=0.2)
    store = _store(tmp_path)
    async with lock(**_card(tmp_path), wait_seconds=1):
        await asyncio.sleep(2.0)  # twice the TTL: only renewal keeps the key
        await _write(store)
    await redis.aclose()


@pytest.mark.asyncio
async def test_an_owner_stalled_past_the_ttl_is_refused_and_the_successor_survives(tmp_path):
    """The knowledge keeper's schedule: A stalls past the TTL (no renewal: a paused process renews nothing), B
    reacquires and writes, A resumes: its write is refused card_lock_lost, and B's key survives A's release."""
    from connection_hub.delegated_credentials.cards.redis_lock import card_lock_key
    from connection_hub.delegated_credentials.cards.store import CardStorageError

    redis = _client()
    lock = _lock(redis, ttl_seconds=1, renew_seconds=None)
    store = _store(tmp_path)
    a_in, b_in, b_wrote, b_done = asyncio.Event(), asyncio.Event(), asyncio.Event(), asyncio.Event()
    outcome = {}

    async def stale_a():
        async with lock(**_card(tmp_path), wait_seconds=1):
            a_in.set()
            await b_wrote.wait()  # stalled past the TTL; B holds the Card and has written
            try:
                await _write(store, "a.json")
            except CardStorageError as exc:
                outcome["a"] = str(exc)
        outcome["a_released"] = True

    async def successor_b():
        await a_in.wait()
        async with lock(**_card(tmp_path), wait_seconds=3):
            b_in.set()
            await asyncio.sleep(0.2)
            await _write(store, "b.json")
            b_wrote.set()
            await b_done.wait()

    a_task, b_task = asyncio.create_task(stale_a()), asyncio.create_task(successor_b())
    await asyncio.wait_for(asyncio.shield(a_task), timeout=5)
    assert outcome == {"a": "card_lock_lost", "a_released": True}
    assert not (store.root / "a.json").exists() and (store.root / "b.json").exists()
    key = [k async for k in redis.scan_iter(match=lock.keys)]
    assert len(key) == 1  # B's key survived A's (owner-checked) release
    b_done.set()
    await b_task
    assert [k async for k in redis.scan_iter(match=lock.keys)] == []
    del card_lock_key
    await redis.aclose()


@pytest.mark.asyncio
async def test_a_lost_renewal_refuses_the_next_write(tmp_path):
    from connection_hub.delegated_credentials.cards.store import CardStorageError

    redis = _client()
    lock = _lock(redis, ttl_seconds=2, renew_seconds=0.2)
    store = _store(tmp_path)
    async with lock(**_card(tmp_path), wait_seconds=1):
        await _write(store, "first.json")
        async for key in redis.scan_iter(match=lock.keys):
            await redis.set(key, "another-owner")  # ownership moved under us
        await asyncio.sleep(0.5)  # the renewal sees it and marks the operation lost
        with pytest.raises(CardStorageError, match="card_lock_lost"):
            await _write(store, "second.json")
    assert not (store.root / "second.json").exists()
    await redis.aclose()


@pytest.mark.asyncio
async def test_an_unreachable_redis_refuses_at_once_not_after_the_wait(tmp_path):
    from connection_hub.delegated_credentials.cards.store import CardStorageError

    for settings, code in (({}, "card_lock_redis_policy_unverified"),
                           ({"require_noeviction": False}, "card_lock_unavailable")):
        redis = _client("redis://127.0.0.1:1/0")
        lock = _lock(redis, **settings)
        started = time.monotonic()
        with pytest.raises(CardStorageError, match=code):
            await _enter(lock, tmp_path, wait=10)
        assert time.monotonic() - started < 3  # never the 10 s wait
        await redis.aclose()


@pytest.mark.asyncio
async def test_an_evicting_redis_is_refused_before_any_lock(tmp_path):
    from connection_hub.delegated_credentials.cards.store import CardStorageError

    admin = _client()
    await admin.config_set("maxmemory-policy", "allkeys-lru")
    try:
        redis = _client()
        with pytest.raises(CardStorageError, match="card_lock_redis_policy_not_noeviction"):
            await _enter(_lock(redis), tmp_path, wait=1)
        await redis.aclose()
    finally:
        await admin.config_set("maxmemory-policy", "noeviction")
        await admin.aclose()


@pytest.mark.asyncio
async def test_writes_inside_an_operation_need_ownership_and_reentry_and_child_tasks_are_safe(tmp_path):
    from connection_hub.delegated_credentials.cards.store import CardStorageError
    from connection_hub.delegated_credentials.durable_io import unlink_guarded, unlink_guarded_async

    redis = _client()
    lock = _lock(redis)
    store = _store(tmp_path)
    await _write(store, "outside.json")  # outside any R operation: no Card-lock claim, phase-1 rules only
    release = asyncio.Event()

    async def child():
        await release.wait()
        await _write(store, "late.json")

    async with lock(**_card(tmp_path), wait_seconds=1):
        async with lock(**_card(tmp_path), wait_seconds=1):  # re-entry: no second SET NX on our own key
            await _write(store, "inner.json")
        await _write(store, "outer.json")
        await unlink_guarded_async(store.root / "inner.json")
        unlink_guarded(store.root / "outer.json")  # the synchronous form: held, not closed, not lost
        task = asyncio.create_task(child())
    release.set()
    with pytest.raises(CardStorageError, match="card_lock_session_closed"):
        await task
    assert not (store.root / "late.json").exists()
    await redis.aclose()


@pytest.mark.asyncio
async def test_cancellation_while_waiting_or_holding_leaves_no_key(tmp_path):
    redis = _client()
    lock = _lock(redis)
    entered = asyncio.Event()

    async def holder():
        async with lock(**_card(tmp_path), wait_seconds=1):
            entered.set()
            await asyncio.sleep(10)

    holding = asyncio.create_task(holder())
    await entered.wait()
    waiting = asyncio.create_task(_enter(lock, tmp_path, wait=5))
    await asyncio.sleep(0.2)
    waiting.cancel()
    holding.cancel()
    for task in (waiting, holding):
        with pytest.raises(asyncio.CancelledError):
            await task
    assert [k async for k in redis.scan_iter(match=lock.keys)] == []
    await redis.aclose()


def test_keys_carry_their_kind_and_a_mismatched_card_path_is_refused(tmp_path):
    from connection_hub.delegated_credentials.cards.redis_lock import card_lock_key
    from connection_hub.delegated_credentials.cards.store import CardStorageError

    card = _card(tmp_path, "a-9", "f" * 64)
    assert card_lock_key(tenant="t", project="p", resource_id=card["resource_id"], lock_path=card["lock_path"]) \
        == "kdcube:cards:lock:t:p:card:" + "f" * 64 + ":a-9"
    assert card_lock_key(tenant="t", project="p", resource_id="card-collection:c1") == "kdcube:cards:lock:t:p:collection:c1"
    with pytest.raises(CardStorageError, match="card_lock_resource_invalid"):
        card_lock_key(tenant="t", project="p", resource_id="delegated-card:a-1", lock_path=card["lock_path"])
    with pytest.raises(CardStorageError, match="card_lock_resource_invalid"):
        card_lock_key(tenant="t", project="p", resource_id="unknown-kind:x")


def test_the_bounds_stay_ordered():
    from connection_hub.delegated_credentials.cards import redis_lock

    # R-5: wait 30 < PB lock_timeout 40 < statement 45 < idle-in-transaction 60 < TTL 120; renewal under TTL/2.
    assert 30 < 40 < 45 < 60 < redis_lock.REDIS_LOCK_TTL_SECONDS
    assert redis_lock.REDIS_LOCK_RENEW_SECONDS < redis_lock.REDIS_LOCK_TTL_SECONDS / 2
    with pytest.raises(ValueError):
        _lock(None, ttl_seconds=10, renew_seconds=6)


@pytest.mark.asyncio
async def test_each_lock_instance_verifies_the_policy_itself_never_by_client_id(tmp_path):
    """Mint F1: no module-level cache keyed by id(client). A lock built after the policy changed reads it again,
    even on the very same client object."""
    from connection_hub.delegated_credentials.cards.store import CardStorageError

    redis = _client()
    await _enter(_lock(redis), tmp_path, wait=1)  # verified for that lock instance only
    await redis.config_set("maxmemory-policy", "allkeys-lru")
    try:
        with pytest.raises(CardStorageError, match="card_lock_redis_policy_not_noeviction"):
            await _enter(_lock(redis), tmp_path, wait=1)
    finally:
        await redis.config_set("maxmemory-policy", "noeviction")
        await redis.aclose()


@pytest.mark.asyncio
async def test_a_publish_stalled_past_the_ttl_is_refused_and_the_retry_publishes(tmp_path):
    """Mint's open point: the W661 card-version path under R. A's PUBLISH stalls past the TTL just before its
    pointer write; B (the same txn's retry, another process) takes the key and publishes; A's resumed write is
    refused card_lock_lost and the published version is current exactly once."""
    from dataclasses import replace

    from connection_hub.delegated_credentials.cards import transaction_store as tx
    from connection_hub.delegated_credentials.cards.redis_lock import guard_card_store_lock_owner
    from connection_hub.delegated_credentials.cards.service import DelegatedCardService
    from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore
    from test_card_service import _Cache
    from test_card_transaction_store import Decisions, SUBJECT_HASH, _authority
    from test_w661_card_versions import WHEN, _stage

    redis = _client()
    lock = _lock(redis, ttl_seconds=1, renew_seconds=None)
    store = guard_card_store_lock_owner(BundleStorageDelegatedCardStore(tmp_path))
    tx.bind_transaction_decisions(store, Decisions())

    @asynccontextmanager
    async def no_lock(**kwargs):
        yield

    seed = DelegatedCardService(store=store, cache=_Cache(), mutation_lock=no_lock)
    before = _authority()
    await seed.commit(before, subject_hash=SUBJECT_HASH, expected_revision=0, now=1_780_000_000)
    after = replace(before, card_revision=2, label="published by the retry")
    store = guard_card_store_lock_owner(BundleStorageDelegatedCardStore(tmp_path))
    tx.bind_transaction_decisions(store, Decisions())
    service_a = DelegatedCardService(store=store, cache=_Cache(), mutation_lock=lock)
    await _stage(service_a, [(SUBJECT_HASH, before.access_id, 1, after)])

    original_advance = store.advance_current
    stalled, retried = asyncio.Event(), asyncio.Event()
    calls = {"n": 0}

    async def advance_current(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:  # A: stalled past the TTL right before its guarded pointer write
            stalled.set()
            await retried.wait()
        return await original_advance(**kwargs)

    store.advance_current = advance_current
    outcome = {}

    async def a():
        try:
            await service_a.publish_card_version(txn=tx_id())
        except Exception as exc:  # noqa: BLE001 - the refusal may be wrapped; its cause chain names it
            chain, cause = [], exc
            while cause is not None:
                chain.append(str(cause))
                cause = cause.__cause__ or cause.__context__
            outcome["a"] = chain

    def tx_id():
        from test_w661_card_versions import TXN
        return TXN

    async def b():
        await stalled.wait()
        await asyncio.sleep(1.2)  # A's key has expired
        # B is another worker: separate objects, same durable files and Redis key.
        retry_store = guard_card_store_lock_owner(BundleStorageDelegatedCardStore(tmp_path))
        tx.bind_transaction_decisions(retry_store, Decisions())
        service_b = DelegatedCardService(store=retry_store, cache=_Cache(), mutation_lock=lock)
        await service_b.publish_card_version(txn=tx_id())
        retried.set()

    await asyncio.wait_for(asyncio.gather(asyncio.create_task(a()), asyncio.create_task(b())), timeout=10)
    assert any("card_lock_lost" in item for item in outcome["a"]), outcome
    found = await store.read_current_authority(subject_hash=SUBJECT_HASH, access_id=before.access_id)
    assert found[1] == after
    assert [k async for k in redis.scan_iter(match=lock.keys)] == []
    await redis.aclose()

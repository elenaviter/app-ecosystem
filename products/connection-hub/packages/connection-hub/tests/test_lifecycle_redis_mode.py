"""Author fail-closed capability/recovery tests; real Cluster proof is Mint's."""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
import pytest

from connection_hub.delegated_credentials.cache_io import encode_cache_value
from connection_hub.delegated_credentials.cards.cache import DelegatedCardRuntimeCache, CardCacheUnusable
from connection_hub.delegated_credentials.cards.lifecycle import LifecycleRequest, LifecycleRefused
from connection_hub.delegated_credentials.cards.lifecycle_store import read_receipt, active_intent_path
from connection_hub.delegated_credentials.cards.service import DelegatedCardService
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore
from test_lifecycle_store import _pair, _wire, _seed, _states, _service_cache, ACTOR


@pytest.mark.asyncio
@pytest.mark.parametrize("raw,reason", [({"cluster_enabled": 0}, ""), ({"cluster_enabled": "0"}, ""),
    ({"cluster_enabled": 1}, "lifecycle_redis_cluster_unsupported"),
    ({"node1": {"cluster_enabled": 1}}, "lifecycle_redis_cluster_unsupported"),
    ({"cluster_enabled": False}, "lifecycle_redis_mode_unverified"), ({}, "lifecycle_redis_mode_unverified"),
    (None, "lifecycle_redis_mode_unverified")])
async def test_only_positive_standalone_mode_enables_pair_mutation(raw, reason):
    redis = MagicMock()
    redis.info = AsyncMock(return_value=raw)
    cache = DelegatedCardRuntimeCache(redis, tenant="t", project="p")
    if reason:
        with pytest.raises(CardCacheUnusable, match=reason):
            await cache.require_lifecycle_backend()
    else:
        await cache.require_lifecycle_backend()
    redis.info.assert_awaited_once_with("cluster")
    redis.eval.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["lifecycle_redis_cluster_unsupported", "lifecycle_redis_mode_unverified"])
async def test_preflight_refusal_has_no_active_intent_or_card_cache_effect_and_replays_terminally(tmp_path, reason):
    store = BundleStorageDelegatedCardStore(tmp_path, lifecycle_lock_scope="same-host-flock")
    cards = _pair()
    await _seed(store, cards)
    request = LifecycleRequest.from_mapping(_wire(cards))
    before = {p: p.read_bytes() for p in store.root.rglob("*") if p.is_file()}
    @asynccontextmanager
    async def lock(**kwargs):
        yield {}
    cache = _service_cache()
    cache.require_lifecycle_backend = AsyncMock(side_effect=CardCacheUnusable(reason))
    gate = AsyncMock(return_value=datetime.now(timezone.utc) + timedelta(seconds=30))
    cleanup = AsyncMock()
    service = DelegatedCardService(store=store, cache=cache, mutation_lock=lock)
    with pytest.raises(LifecycleRefused, match="issuer_" + reason):
        await service.revoke_lifecycle(request, actor_subject=ACTOR, before_commit=gate, after_commit=cleanup)
    assert not active_intent_path(store, request.transaction_id(ACTOR)).exists()
    assert all(p.read_bytes() == data for p, data in before.items())
    assert await _states(store, request) == [("active", 1), ("active", 1)]
    receipt = await read_receipt(store, request.transaction_id(ACTOR))
    assert receipt["state"] == "refused" and receipt["serving_state"] == "complete"
    assert await service.revoke_lifecycle(request, actor_subject=ACTOR, before_commit=gate, after_commit=cleanup) == receipt
    gate.assert_awaited_once()
    cache.require_lifecycle_backend.assert_awaited_once()
    for name in ("reconcile_projection", "claim_lifecycle", "release_lifecycle", "finish_lifecycle", "index_remove"):
        getattr(cache, name).assert_not_called()
    cleanup.assert_not_called()
    for target in request.targets:
        await service._assert_no_lifecycle_preparation(subject_hash=target.subject_hash, access_id=target.access_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["absent", "other-owner", "owned", "damaged", "generic-error", "unverified-mode"])
async def test_legacy_crossslot_release_requires_positive_no_owned_marker_evidence(case):
    redis = MagicMock()
    redis.info = AsyncMock(return_value={} if case == "unverified-mode" else {"cluster_enabled": 1})
    redis.eval = AsyncMock(side_effect=RuntimeError("backend unavailable" if case == "generic-error" else "CROSSSLOT"))
    marker = None
    if case in ("other-owner", "owned"):
        marker = encode_cache_value({"kind": "updating", "card_revision": 1,
            "mutation_id": "tx" if case == "owned" else "other"})
    if case == "damaged":
        marker = b"not-json"
    redis.get = AsyncMock(side_effect=[marker, None])
    cache = DelegatedCardRuntimeCache(redis, tenant="t", project="p")
    if case in ("absent", "other-owner"):
        await cache.release_lifecycle(("card-0", "card-1"), mutation_id="tx")
        assert redis.get.await_count == 2
    else:
        with pytest.raises(Exception):
            await cache.release_lifecycle(("card-0", "card-1"), mutation_id="tx")
    redis.delete.assert_not_called()  # no sequential cleanup fallback

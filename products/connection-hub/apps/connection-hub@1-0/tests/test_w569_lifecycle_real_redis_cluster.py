"""W569 R2: the two-Card lifecycle on a real Redis Cluster.

The draft states that Redis Cluster keys in different slots refuse CROSSSLOT,
with no sequential fallback. The lifecycle keys carry no hash tag: both Card
projections, the projection epoch and the sweep lock hash to unrelated slots,
so on a Cluster every pair is refused. These cases hold that refusal to the
documented contract on a real three-master cluster named by
``REDIS_CLUSTER_NODE`` (``host:port`` of any node), using the hosted
composition (KDCube ``DurableCardPersistence`` over a real
``BundleStorageDelegatedCardStore``):

- the claim is refused on the client and on the server, and writes no key;
- a refused pair leaves both Cards active;
- an interrupted preparation is "deterministically aborted under both
  fences" and the identical retry "returns the recorded refusal", after which
  each Card's ordinary writer works again.
"""

from __future__ import annotations

import dataclasses
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from connection_hub.delegated_credentials.cards.cache import DelegatedCardRuntimeCache
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore, subject_hash_for
from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.cards.persistence import (
    DurableCardPersistence,
)
from test_w569_lifecycle_real_redis import ACTOR, _durable, _pair, _request, _seed


def _node() -> str:
    return os.environ.get("REDIS_CLUSTER_NODE") or ""


pytestmark = pytest.mark.skipif(not _node(), reason="REDIS_CLUSTER_NODE is not set; real Redis Cluster is skipped")


@pytest_asyncio.fixture
async def cluster():
    from redis.asyncio.cluster import RedisCluster

    host, port = _node().rsplit(":", 1)
    client = RedisCluster(host=host, port=int(port))
    await client.initialize()
    yield client
    await client.aclose()


@pytest.fixture
def namespace() -> tuple[str, str]:
    return f"t-{uuid.uuid4().hex[:8]}", f"p-{uuid.uuid4().hex[:8]}"


def _keys(cache, cards):
    return [cache.card_key(card.access_id) for card in cards] + [cache.projection_epoch_key(), cache.reconcile_lock_key()]


async def _decision(_authorities):
    return datetime.now(timezone.utc) + timedelta(seconds=30)


@pytest.mark.asyncio
async def test_the_pair_claim_is_refused_on_the_client_and_the_server_and_writes_no_key(cluster, namespace):
    import redis.asyncio as redis_asyncio
    from redis.exceptions import RedisClusterException, ResponseError

    cache = DelegatedCardRuntimeCache(cluster, tenant=namespace[0], project=namespace[1])
    cards = _pair()
    keys = _keys(cache, cards)
    assert len({cluster.keyslot(key) for key in keys}) > 1, "no hash tag: the four keys do not share a slot"

    with pytest.raises(RedisClusterException):
        await cache.claim_lifecycle(cards, mutation_id="tx-1")

    # The same script sent straight to the node owning the first key.
    node = cluster.get_node_from_key(keys[0])
    direct = redis_asyncio.Redis(host=node.host, port=node.port)
    try:
        with pytest.raises(ResponseError, match="CROSSSLOT|MOVED|don't hash to the same slot"):
            await DelegatedCardRuntimeCache(direct, tenant=namespace[0], project=namespace[1]).claim_lifecycle(
                cards, mutation_id="tx-1")
    finally:
        await direct.aclose()
    assert [await cluster.exists(key) for key in keys] == [0, 0, 0, 0]


@pytest.mark.asyncio
async def test_a_refused_cluster_pair_leaves_both_cards_active(tmp_path, cluster, namespace):
    store = BundleStorageDelegatedCardStore(tmp_path, lifecycle_lock_scope="same-host-flock")
    persistence = DurableCardPersistence(redis=cluster, tenant=namespace[0], project=namespace[1], card_store=store)
    cards = _pair()
    await _seed(store, cards)

    with pytest.raises(Exception):
        await persistence.revoke_lifecycle(_request(cards), actor_subject=ACTOR, before_commit=_decision)

    assert await _durable(store, cards) == [("active", 1), ("active", 1)]


@pytest.mark.asyncio
async def test_the_identical_retry_resolves_a_refused_cluster_pair_and_frees_both_cards(tmp_path, cluster, namespace):
    """RED on d886ce5a: the retry raises again and both Cards stay blocked.

    The claim runs after the durable intent is published. On a Cluster it
    raises; the identical retry aborts the prepared intent and then calls
    release_lifecycle, which raises the same cross-slot error, so the refusal
    is never recorded as serving-complete. Both ordinary writers then refuse
    lifecycle_preparation_unresolved, with no path back.
    """

    store = BundleStorageDelegatedCardStore(tmp_path, lifecycle_lock_scope="same-host-flock")
    persistence = DurableCardPersistence(redis=cluster, tenant=namespace[0], project=namespace[1], card_store=store)
    cards = _pair()
    await _seed(store, cards)
    request = _request(cards)
    with pytest.raises(Exception):
        await persistence.revoke_lifecycle(request, actor_subject=ACTOR, before_commit=_decision)

    retried = await persistence.revoke_lifecycle(request, actor_subject=ACTOR, before_commit=_decision)

    assert retried["state"] == "refused" and retried["serving_state"] == "complete", retried
    assert await _durable(store, cards) == [("active", 1), ("active", 1)]
    for card in cards:
        await persistence._cards.commit(dataclasses.replace(card, card_revision=2),
                                        subject_hash=subject_hash_for(card.grantor_subject), expected_revision=1)

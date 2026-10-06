"""W569 R1: the two-Card lifecycle against a real Redis server.

The draft unit tests drive the lifecycle with a MagicMock cache, so its Lua
(cjson decode, revision checks, one-operation pair markers, epoch and sweep
token invalidation) has never run on Redis. These cases run the production
composition the hosted app uses: the KDCube ``DurableCardPersistence`` (real
``observed_file_lock_async`` fences) over a real ``BundleStorageDelegatedCardStore``
and a real Redis named by ``REDIS_URL``. Keys live under a fresh random
tenant/project, so a shared server is never touched outside them.

Only the configured-issuer closure is the test's own: it stands for the
issuers' sealed decisions and returns their earliest ``valid_until``.
"""

from __future__ import annotations

import dataclasses
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from connection_hub.delegated_credentials.cards.cache import DelegatedCardRuntimeCache
from connection_hub.delegated_credentials.cards.lifecycle import LifecycleRequest
from connection_hub.delegated_credentials.cards.model import CardAuthority, NamedServiceSelection
from connection_hub.delegated_credentials.cards.service import CardConflict
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore, subject_hash_for
from connection_hub.delegated_credentials.issuer_gate import change_digest
from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.cards.persistence import (
    DurableCardPersistence,
)

ACTOR = "authenticated-human-admin"
MOMENT = datetime(2026, 10, 6, tzinfo=timezone.utc)


def _redis_url() -> str:
    return os.environ.get("REDIS_URL") or ""


pytestmark = pytest.mark.skipif(not _redis_url(), reason="REDIS_URL is not set; real-Redis lifecycle is skipped")


@pytest_asyncio.fixture
async def redis_client():
    import redis.asyncio as redis_asyncio

    client = redis_asyncio.from_url(_redis_url())
    await client.ping()
    yield client
    await client.aclose()


@pytest.fixture
def namespace() -> tuple[str, str]:
    return f"t-{uuid.uuid4().hex[:8]}", f"p-{uuid.uuid4().hex[:8]}"


def _pair():
    return tuple(CardAuthority(
        access_id=f"card-{index}", grantor_subject=f"owner-{index}", source="control",
        client_id="", delegate_subject="",
        card_kind="control", card_revision=1, state="active", issuer_kind=f"opaque-{index}",
        issuer_ref="opaque-lineage", resource_grants={}, resource_operations={},
        named_service_operations=NamedServiceSelection.none(),
        properties={"opaque.identity-marker": {"paired-with": f"card-{1 - index}"}},
    ) for index in range(2))


def _request(authorities, request_id="synthetic-request"):
    targets = [{"owner_subject": card.grantor_subject, "access_id": card.access_id,
                "expected_card_revision": card.card_revision,
                "expected_authority_fingerprint": card.content_hash(),
                "issuer_kind": card.issuer_kind, "issuer_ref": card.issuer_ref}
               for card in authorities]
    targets.sort(key=lambda target: (target["owner_subject"], target["access_id"]))
    return LifecycleRequest.from_mapping({
        "context_ref": "synthetic-context", "request_id": request_id, "action": "revoke",
        "change_digest": change_digest({"action": "revoke", "targets": targets}), "targets": targets})


async def _seed(store, authorities):
    for card in authorities:
        scope = subject_hash_for(card.grantor_subject)
        pointer = await store.write_revision(subject_hash=scope, authority=card, updated_at=MOMENT)
        await store.advance_current(subject_hash=scope, pointer=pointer)


async def _durable(store, authorities):
    states = []
    for card in authorities:
        loaded = await store.read_current_authority(subject_hash=subject_hash_for(card.grantor_subject),
                                                    access_id=card.access_id)
        states.append(None if loaded is None else (loaded[1].state, loaded[1].card_revision))
    return states


async def _cached(redis_client, cache, authorities):
    values = []
    for card in authorities:
        raw = await redis_client.get(cache.card_key(card.access_id))
        values.append(None if raw is None else json.loads(raw))
    return values


def _persistence(tmp_path, redis_client, namespace):
    tenant, project = namespace
    store = BundleStorageDelegatedCardStore(tmp_path, lifecycle_lock_scope="same-host-flock")
    return store, DurableCardPersistence(redis=redis_client, tenant=tenant, project=project, card_store=store)


def _decision():
    calls = []

    async def before_commit(authorities):
        calls.append(tuple(card.access_id for card in authorities))
        return datetime.now(timezone.utc) + timedelta(seconds=30)

    return before_commit, calls


@pytest.mark.asyncio
async def test_the_production_pair_revoke_commits_both_and_serves_both_revoked_on_real_redis(tmp_path, redis_client, namespace):
    store, persistence = _persistence(tmp_path, redis_client, namespace)
    cache = DelegatedCardRuntimeCache(redis_client, tenant=namespace[0], project=namespace[1])
    cards = _pair()
    await _seed(store, cards)
    await redis_client.set(cache.projection_epoch_key(), "pre-existing-run")
    await redis_client.set(cache.reconcile_lock_key(), "pre-existing-sweep")
    request = _request(cards)
    before_commit, calls = _decision()

    receipt = await persistence.revoke_lifecycle(request, actor_subject=ACTOR, before_commit=before_commit)

    assert (receipt["state"], receipt["serving_state"]) == ("committed", "complete")
    assert await _durable(store, cards) == [("revoked", 2), ("revoked", 2)]
    served = await _cached(redis_client, cache, cards)
    assert [(entry["kind"], entry["card_revision"]) for entry in served] == [("revoked", 2), ("revoked", 2)]
    # A sweep that began before the pair cannot bless the superseded projection.
    assert await redis_client.get(cache.projection_epoch_key()) is None
    assert await redis_client.get(cache.reconcile_lock_key()) is None
    assert len(calls) == 2  # sealed decision, then revalidated before publication
    # The identical replay returns the historical receipt and decides nothing again.
    assert await persistence.revoke_lifecycle(request, actor_subject=ACTOR, before_commit=before_commit) == receipt
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_stale_cached_revision_on_either_card_claims_neither_marker(redis_client, namespace):
    cache = DelegatedCardRuntimeCache(redis_client, tenant=namespace[0], project=namespace[1])
    cards = _pair()
    fresh = json.dumps({"kind": "card", "card_revision": 1})
    stale = json.dumps({"kind": "card", "card_revision": 7})
    await redis_client.set(cache.card_key(cards[0].access_id), fresh)
    await redis_client.set(cache.card_key(cards[1].access_id), stale)
    await redis_client.set(cache.projection_epoch_key(), "run")

    assert await cache.claim_lifecycle(cards, mutation_id="tx-1") is False

    # One Lua operation: the matching first Card is not marked either.
    assert await redis_client.get(cache.card_key(cards[0].access_id)) == fresh.encode()
    assert await redis_client.get(cache.card_key(cards[1].access_id)) == stale.encode()
    assert await redis_client.get(cache.projection_epoch_key()) == b"run"


@pytest.mark.asyncio
async def test_a_damaged_cached_value_claims_nothing(redis_client, namespace):
    cache = DelegatedCardRuntimeCache(redis_client, tenant=namespace[0], project=namespace[1])
    cards = _pair()
    await redis_client.set(cache.card_key(cards[0].access_id), "{not json")

    assert await cache.claim_lifecycle(cards, mutation_id="tx-1") is False

    assert await redis_client.get(cache.card_key(cards[0].access_id)) == b"{not json"
    assert await redis_client.get(cache.card_key(cards[1].access_id)) is None


@pytest.mark.asyncio
async def test_the_fence_is_lost_when_a_rebuild_records_an_epoch_or_another_mutation_owns_a_marker(redis_client, namespace):
    cache = DelegatedCardRuntimeCache(redis_client, tenant=namespace[0], project=namespace[1])
    cards = _pair()
    access_ids = tuple(card.access_id for card in cards)
    assert await cache.claim_lifecycle(cards, mutation_id="tx-1") is True
    assert await cache.lifecycle_fenced(access_ids, mutation_id="tx-1") is True
    assert await cache.lifecycle_fenced(access_ids, mutation_id="tx-other") is False

    await redis_client.set(cache.projection_epoch_key(), "rebuilt-run")
    assert await cache.lifecycle_fenced(access_ids, mutation_id="tx-1") is False


@pytest.mark.asyncio
async def test_finish_and_release_touch_only_the_owning_mutations_markers(redis_client, namespace):
    cache = DelegatedCardRuntimeCache(redis_client, tenant=namespace[0], project=namespace[1])
    cards = _pair()
    access_ids = tuple(card.access_id for card in cards)
    assert await cache.claim_lifecycle(cards, mutation_id="tx-1") is True
    marked = await _cached(redis_client, cache, cards)

    assert await cache.finish_lifecycle(cards, mutation_id="tx-other") is False
    await cache.release_lifecycle(access_ids, mutation_id="tx-other")
    assert await _cached(redis_client, cache, cards) == marked

    assert await cache.finish_lifecycle(cards, mutation_id="tx-1") is True
    finished = await _cached(redis_client, cache, cards)
    assert [(entry["kind"], entry["card_revision"]) for entry in finished] == [("revoked", 2), ("revoked", 2)]
    # Finishing again is idempotent on the recorded revoked revision.
    assert await cache.finish_lifecycle(cards, mutation_id="tx-1") is True
    await cache.release_lifecycle(access_ids, mutation_id="tx-1")
    assert await _cached(redis_client, cache, cards) == finished


@pytest.mark.asyncio
async def test_an_unclaimable_pair_leaves_both_cards_active_and_a_retry_resolves_the_reservation(tmp_path, redis_client, namespace):
    store, persistence = _persistence(tmp_path, redis_client, namespace)
    cache = DelegatedCardRuntimeCache(redis_client, tenant=namespace[0], project=namespace[1])
    cards = _pair()
    await _seed(store, cards)
    request = _request(cards)
    before_commit, _ = _decision()
    # Another mutation already holds the second Card's serving marker.
    foreign = json.dumps({"kind": "updating", "mutation_id": "someone-else", "card_revision": 1})
    original_reconcile = persistence._cards._reconcile

    async def reconcile_then_foreign_marker(**kwargs):
        await original_reconcile(**kwargs)
        if kwargs["access_id"] == cards[1].access_id:
            await redis_client.set(cache.card_key(cards[1].access_id), foreign)

    persistence._cards._reconcile = reconcile_then_foreign_marker

    with pytest.raises(CardConflict):
        await persistence.revoke_lifecycle(request, actor_subject=ACTOR, before_commit=before_commit)

    assert await _durable(store, cards) == [("active", 1), ("active", 1)]
    assert await redis_client.get(cache.card_key(cards[1].access_id)) == foreign.encode()
    persistence._cards._reconcile = original_reconcile
    retried = await persistence.revoke_lifecycle(request, actor_subject=ACTOR, before_commit=before_commit)
    assert retried["state"] in ("refused", "aborted"), retried
    assert retried["serving_state"] == "complete", retried
    assert await _durable(store, cards) == [("active", 1), ("active", 1)]
    # The resolved reservation no longer blocks the first Card's ordinary writer;
    # the second stays held by the other mutation's own marker, not by this pair.
    await persistence._cards.commit(dataclasses.replace(cards[0], card_revision=2),
                                    subject_hash=subject_hash_for(cards[0].grantor_subject), expected_revision=1)
    with pytest.raises(CardConflict, match="card_transition_not_claimed"):
        await persistence._cards.commit(dataclasses.replace(cards[1], card_revision=2),
                                        subject_hash=subject_hash_for(cards[1].grantor_subject), expected_revision=1)
    assert await redis_client.get(cache.card_key(cards[1].access_id)) == foreign.encode()

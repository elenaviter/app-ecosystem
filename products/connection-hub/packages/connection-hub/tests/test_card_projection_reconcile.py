# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""A Card projection that Redis rolled back behind its durable revision.

2026-09-21: Redis restarted from an older snapshot and three Card projections
came back one revision behind their durable ``current.json``. Every reconnect
then failed the marker fence (``card_transition_not_claimed``, surfaced as
``invalid_grant``), and a present projection is served without reading the
durable pointer, so a rolled-back revocation would keep a revoked Card usable.

These run the real Lua against a Redis named by ``REDIS_URL`` (keys live under
a random tenant and project), as the SDK's cache transition tests do.
"""

from __future__ import annotations

import hashlib
import os
import time
import uuid
from contextlib import asynccontextmanager

import pytest

from connection_hub.delegated_credentials.cards.cache import (
    DelegatedCardRuntimeCache,
)
from connection_hub.delegated_credentials.cache_io import encode_cache_value
from connection_hub.delegated_credentials.cards.identity import CARD_KIND_CONNECTOR
from connection_hub.delegated_credentials.cards.model import (
    CARD_STATE_ACTIVE,
    CardAuthority,
    ControlCardBinding,
    NamedServiceSelection,
)
from connection_hub.delegated_credentials.controls.cache import (
    CONTROL_CACHE_KIND_CARD,
    ControlCardRuntimeCache,
)
from connection_hub.delegated_credentials.controls.model import (
    ProjectControlCardAuthority,
    control_card_from_legacy,
)
from connection_hub.delegated_credentials.cards.reconcile import (
    CardProjectionEpochGate,
    CardProjectionReconciler,
)
from connection_hub.delegated_credentials.cards.resolver import (
    CardUnavailable,
    DelegatedCardResolver,
)
from connection_hub.delegated_credentials.live_grant import (
    LiveGrantCardError,
    resolve_live_grant_composition,
)
from connection_hub.delegated_credentials.cards.service import (
    DelegatedCardService,
    replace_state,
)
from connection_hub.delegated_credentials.cards.store import (
    BundleStorageDelegatedCardStore,
)

GRANTOR = "platform-user-1"
SUBJECT_HASH = hashlib.sha256(GRANTOR.encode("utf-8")).hexdigest()
ACCESS_ID = "oauth-0123456789abcdef"
NOW = int(time.time())


def _redis_url() -> str:
    return os.environ.get("REDIS_URL") or ""


pytestmark = pytest.mark.skipif(
    not _redis_url(), reason="REDIS_URL is not set; real-Redis projection repair is skipped"
)


@asynccontextmanager
async def _no_lock(**_kwargs):
    yield {}


@pytest.fixture
async def redis_client():
    import redis.asyncio as redis_asyncio

    client = redis_asyncio.from_url(_redis_url())
    try:
        await client.ping()
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"Redis at REDIS_URL is unreachable: {exc}")
    yield client
    await client.aclose()


@pytest.fixture
def namespace() -> tuple[str, str]:
    return f"t-{uuid.uuid4().hex[:8]}", f"p-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def cache(redis_client, namespace) -> DelegatedCardRuntimeCache:
    tenant, project = namespace
    return DelegatedCardRuntimeCache(redis_client, tenant=tenant, project=project)


@pytest.fixture
def store(tmp_path) -> BundleStorageDelegatedCardStore:
    return BundleStorageDelegatedCardStore(tmp_path)


@pytest.fixture
def service(store, cache) -> DelegatedCardService:
    return DelegatedCardService(store=store, cache=cache, mutation_lock=_no_lock)


def _reconciler(cache, store, **kwargs) -> CardProjectionReconciler:
    return CardProjectionReconciler(cache=cache, store=store, **kwargs)


async def _mark_swept(redis_client, cache) -> None:
    """The current Redis run is recorded as swept, as after startup."""
    run_id = (await redis_client.info("server"))["run_id"]
    await redis_client.set(cache.projection_epoch_key(), run_id)


async def _roll_back_epoch(redis_client, cache) -> None:
    """Redis restarted from a snapshot recorded under an older run."""
    await redis_client.set(cache.projection_epoch_key(), "run-before-the-restart")


def _authority(*, revision: int = 1, label: str = "worker") -> CardAuthority:
    return CardAuthority(
        access_id=ACCESS_ID,
        client_id="client-1",
        grantor_subject=GRANTOR,
        delegate_subject="integration:client-1",
        source="oauth",
        card_kind=CARD_KIND_CONNECTOR,
        label=label,
        card_revision=revision,
        state=CARD_STATE_ACTIVE,
        resource_grants={"https://ex/mcp": ("board:read",)},
        resource_operations={"https://ex/mcp": ("board.read",)},
        named_service_operations=NamedServiceSelection.none(),
        created_at=NOW - 60,
        expires_at=NOW + 3600,
    )


async def _commit_two_revisions_and_roll_back(redis_client, service, cache):
    """Durable at revision 2, the projection restored to revision 1."""
    await service.commit(_authority(), subject_hash=SUBJECT_HASH, expected_revision=0, now=NOW)
    snapshot = await redis_client.get(cache.card_key(ACCESS_ID))
    await service.commit(
        _authority(revision=2, label="renamed"),
        subject_hash=SUBJECT_HASH,
        expected_revision=1,
        now=NOW,
    )
    await redis_client.set(cache.card_key(ACCESS_ID), snapshot)
    assert (await cache.read(ACCESS_ID)).card_revision == 1


async def test_a_write_fenced_on_the_durable_revision_passes_over_a_projection_left_behind(
    redis_client, service, cache
):
    await _commit_two_revisions_and_roll_back(redis_client, service, cache)

    pointer = await service.commit(
        _authority(revision=3), subject_hash=SUBJECT_HASH, expected_revision=2, now=NOW
    )

    assert pointer.card_revision == 3
    assert (await cache.read(ACCESS_ID)).card_revision == 3


async def test_a_revoke_fenced_on_the_durable_revision_passes_over_a_projection_left_behind(
    redis_client, service, store, cache
):
    await _commit_two_revisions_and_roll_back(redis_client, service, cache)

    pointer = await service.revoke(subject_hash=SUBJECT_HASH, access_id=ACCESS_ID, expected_revision=2)

    assert pointer is not None and pointer.card_revision == 3
    await _mark_swept(redis_client, cache)
    resolver = DelegatedCardResolver(cache=cache, store=store)
    assert await resolver.resolve(subject_hash=SUBJECT_HASH, access_id=ACCESS_ID, now=NOW) is None


async def test_a_revocation_lost_by_redis_is_not_served_before_or_after_the_sweep(
    redis_client, service, store, cache
):
    await service.commit(_authority(), subject_hash=SUBJECT_HASH, expected_revision=0, now=NOW)
    snapshot = await redis_client.get(cache.card_key(ACCESS_ID))
    await service.revoke(subject_hash=SUBJECT_HASH, access_id=ACCESS_ID, expected_revision=1)
    # Redis restarts from a snapshot taken before the revocation.
    await redis_client.set(cache.card_key(ACCESS_ID), snapshot)
    await _roll_back_epoch(redis_client, cache)
    assert (await cache.read(ACCESS_ID)).card_revision == 1, "the rolled-back projection is back"

    # The first store-owning read sweeps before it serves anything.
    resolver = DelegatedCardResolver(cache=cache, store=store)
    assert await resolver.resolve(subject_hash=SUBJECT_HASH, access_id=ACCESS_ID, now=NOW) is None
    assert await cache.read(ACCESS_ID) is None


async def test_while_the_sweep_cannot_complete_nothing_is_served(
    redis_client, service, store, cache, monkeypatch
):
    # Review 2026-09-21: a failed sweep returned and the stale revision 1 was served.
    await service.commit(_authority(), subject_hash=SUBJECT_HASH, expected_revision=0, now=NOW)
    snapshot = await redis_client.get(cache.card_key(ACCESS_ID))
    await service.revoke(subject_hash=SUBJECT_HASH, access_id=ACCESS_ID, expected_revision=1)
    await redis_client.set(cache.card_key(ACCESS_ID), snapshot)
    await _roll_back_epoch(redis_client, cache)

    async def _unreadable(**_kwargs):
        raise OSError("storage down")

    monkeypatch.setattr(store, "read_current_authority", _unreadable)
    resolver = DelegatedCardResolver(cache=cache, store=store)
    for _ in range(2):
        with pytest.raises(CardUnavailable) as exc:
            await resolver.resolve(subject_hash=SUBJECT_HASH, access_id=ACCESS_ID, now=NOW)
        assert exc.value.reason == "card_projection_reconciling"
    assert await redis_client.get(cache.projection_epoch_key()) == b"run-before-the-restart"

    monkeypatch.undo()
    assert await resolver.resolve(subject_hash=SUBJECT_HASH, access_id=ACCESS_ID, now=NOW) is None


async def test_a_projection_without_a_durable_card_is_removed_by_the_sweep(
    redis_client, service, store, cache, tmp_path
):
    # Review 2026-09-21: an orphan was logged, the epoch recorded, and it stayed served.
    await service.commit(_authority(), subject_hash=SUBJECT_HASH, expected_revision=0, now=NOW)
    empty_store = BundleStorageDelegatedCardStore(tmp_path / "empty")
    await _roll_back_epoch(redis_client, cache)

    report = await _reconciler(cache, empty_store).reconcile(now=NOW)

    assert (report.checked, report.repaired, report.completed) == (1, 1, True)
    assert await cache.read(ACCESS_ID) is None
    resolver = DelegatedCardResolver(cache=cache, store=empty_store)
    assert await resolver.resolve(subject_hash=SUBJECT_HASH, access_id=ACCESS_ID, now=NOW) is None


async def test_a_reader_without_a_durable_store_fails_closed_until_a_store_owner_sweeps(
    redis_client, service, store, cache, namespace
):
    await service.commit(_authority(), subject_hash=SUBJECT_HASH, expected_revision=0, now=NOW)
    await _roll_back_epoch(redis_client, cache)
    tenant, project = namespace

    with pytest.raises(LiveGrantCardError) as exc:
        await resolve_live_grant_composition(
            redis_client, tenant=tenant, project=project, access_id=ACCESS_ID
        )
    assert exc.value.reason == "card_projection_reconciling"

    # The Connection Hub cron is the store owner.
    assert (await _reconciler(cache, store).reconcile(now=NOW)).completed
    composition = await resolve_live_grant_composition(
        redis_client, tenant=tenant, project=project, access_id=ACCESS_ID
    )
    assert composition is not None and composition.effective_card.card_revision == 1


async def test_the_sweep_installs_the_durable_revision_of_a_card_still_active(
    redis_client, service, store, cache
):
    await _commit_two_revisions_and_roll_back(redis_client, service, cache)
    await _roll_back_epoch(redis_client, cache)

    await _reconciler(cache, store).ensure_ready(now=NOW)

    entry = await cache.read(ACCESS_ID)
    assert entry.card_revision == 2 and entry.authority.label == "renamed"


async def test_the_first_read_after_a_rollback_is_refused_and_never_returns_the_restored_card(
    redis_client, service, store, cache, namespace
):
    # Re-review 2026-09-21: a confirmed process trusted its last check for 10
    # seconds and served the restored pre-revocation Card in that window.
    await service.commit(_authority(), subject_hash=SUBJECT_HASH, expected_revision=0, now=NOW)
    await _mark_swept(redis_client, cache)
    tenant, project = namespace
    served = await resolve_live_grant_composition(
        redis_client, tenant=tenant, project=project, access_id=ACCESS_ID
    )
    assert served is not None, "a swept run serves"

    snapshot = await redis_client.get(cache.card_key(ACCESS_ID))
    await service.revoke(subject_hash=SUBJECT_HASH, access_id=ACCESS_ID, expected_revision=1)
    await redis_client.set(cache.card_key(ACCESS_ID), snapshot)
    await _roll_back_epoch(redis_client, cache)

    with pytest.raises(LiveGrantCardError) as exc:
        await resolve_live_grant_composition(
            redis_client, tenant=tenant, project=project, access_id=ACCESS_ID
        )
    assert exc.value.reason == "card_projection_reconciling"
    assert not await CardProjectionEpochGate(cache).is_ready()


class _NoRunId:
    """A Redis client whose INFO reports no run_id."""

    def __init__(self, client) -> None:
        self._client = client

    def __getattr__(self, name):
        return getattr(self._client, name)

    async def info(self, *_args, **_kwargs):
        return {}

    def pipeline(self, *args, **kwargs):
        pipe = self._client.pipeline(*args, **kwargs)
        execute = pipe.execute

        async def _execute(*a, **k):
            results = await execute(*a, **k)
            results[0] = {}
            return results

        pipe.execute = _execute
        return pipe


async def test_a_redis_that_cannot_prove_its_run_serves_nothing(
    redis_client, service, store, cache, namespace
):
    # Re-review 2026-09-21: a missing run_id turned the fence off.
    await service.commit(_authority(), subject_hash=SUBJECT_HASH, expected_revision=0, now=NOW)
    await _mark_swept(redis_client, cache)
    tenant, project = namespace
    blind = _NoRunId(redis_client)
    blind_cache = DelegatedCardRuntimeCache(blind, tenant=tenant, project=project)

    assert not await CardProjectionEpochGate(blind_cache).is_ready()
    with pytest.raises(CardUnavailable) as exc:
        await DelegatedCardResolver(cache=blind_cache, store=store).resolve(
            subject_hash=SUBJECT_HASH, access_id=ACCESS_ID, now=NOW
        )
    assert exc.value.reason == "card_projection_reconciling"
    with pytest.raises(LiveGrantCardError):
        await resolve_live_grant_composition(
            blind, tenant=tenant, project=project, access_id=ACCESS_ID
        )


async def test_a_restored_broader_legacy_control_card_is_never_effective(
    redis_client, service, store, cache, namespace
):
    # Re-review 2026-09-21: the live path fell back to legacy Control Card
    # projections, which live only in Redis and escape the sweep.
    tenant, project = namespace
    legacy_id = "project-control-legacy1"
    bound = CardAuthority.from_mapping(
        {
            **_authority().to_dict(),
            "control_card": ControlCardBinding(
                control_id=legacy_id, issuer_ref="work:project:demo"
            ).to_dict(),
        }
    )
    await service.commit(bound, subject_hash=SUBJECT_HASH, expected_revision=0, now=NOW)
    await _mark_swept(redis_client, cache)
    legacy_key = ControlCardRuntimeCache(redis_client, tenant=tenant, project=project).key(legacy_id)
    legacy = ProjectControlCardAuthority.from_card(
        bound, control_id=legacy_id, issuer_ref="work:project:demo", revision=1, now=NOW
    )
    payload = {"kind": CONTROL_CACHE_KIND_CARD, "revision": 1, "authority": legacy.to_dict()}
    # The restored projection is a valid Control Card the old fallback would
    # have made effective.
    assert control_card_from_legacy(
        ProjectControlCardAuthority.from_mapping(payload["authority"])
    ).resource_operations
    await redis_client.set(legacy_key, encode_cache_value(payload))

    for store_backed in (False, True):
        with pytest.raises(LiveGrantCardError) as exc:
            await resolve_live_grant_composition(
                redis_client,
                tenant=tenant,
                project=project,
                access_id=ACCESS_ID,
                expected_grantor_subject=GRANTOR if store_backed else "",
                card_store=store if store_backed else None,
            )
        assert exc.value.reason == "control_card_unresolvable"


async def test_a_tombstone_restored_from_before_a_reconsent_is_removed_by_the_sweep(
    redis_client, service, store, cache
):
    # Re-review 2026-09-21: the sweep skipped tombstones, so an old one denied
    # the re-consented active Card until another mutation.
    await service.commit(_authority(), subject_hash=SUBJECT_HASH, expected_revision=0, now=NOW)
    await service.revoke(subject_hash=SUBJECT_HASH, access_id=ACCESS_ID, expected_revision=1)
    tombstone = await redis_client.get(cache.card_key(ACCESS_ID))
    await service.commit(_authority(revision=3), subject_hash=SUBJECT_HASH, expected_revision=2, now=NOW)
    await redis_client.set(cache.card_key(ACCESS_ID), tombstone)
    await _roll_back_epoch(redis_client, cache)
    assert (await cache.read(ACCESS_ID)).is_revoked

    report = await _reconciler(cache, store).reconcile(now=NOW)

    assert report.completed and report.repaired == 1
    resolved = await DelegatedCardResolver(cache=cache, store=store).resolve(
        subject_hash=SUBJECT_HASH, access_id=ACCESS_ID, now=NOW
    )
    assert resolved is not None and resolved.card_revision == 3


async def test_a_lock_restored_from_an_older_run_does_not_block_the_sweep(
    redis_client, service, store, cache
):
    await _commit_two_revisions_and_roll_back(redis_client, service, cache)
    await _roll_back_epoch(redis_client, cache)
    await redis_client.set(cache.reconcile_lock_key(), "run-before-the-restart|owner")

    report = await _reconciler(cache, store).reconcile(now=NOW)

    assert report is not None and report.completed and report.repaired == 1
    assert await redis_client.get(cache.reconcile_lock_key()) is None


async def test_a_sweep_of_this_run_held_elsewhere_is_waited_for_not_duplicated(
    redis_client, service, store, cache
):
    await _commit_two_revisions_and_roll_back(redis_client, service, cache)
    await _roll_back_epoch(redis_client, cache)
    run_id = (await redis_client.info("server"))["run_id"]
    held = f"{run_id}|other-worker"
    await redis_client.set(cache.reconcile_lock_key(), held)

    assert await _reconciler(cache, store).reconcile(now=NOW) is None
    resolver = DelegatedCardResolver(cache=cache, store=store)
    with pytest.raises(CardUnavailable):
        await resolver.resolve(subject_hash=SUBJECT_HASH, access_id=ACCESS_ID, now=NOW)
    assert (await redis_client.get(cache.reconcile_lock_key())).decode() == held


async def test_a_sweep_that_lost_its_lock_records_nothing_and_leaves_the_new_owner(
    redis_client, service, store, cache, monkeypatch
):
    from connection_hub.delegated_credentials.cards import reconcile

    await _commit_two_revisions_and_roll_back(redis_client, service, cache)
    await _roll_back_epoch(redis_client, cache)
    run_id = (await redis_client.info("server"))["run_id"]
    monkeypatch.setattr(reconcile, "RECONCILE_LOCK_RENEW_EVERY", 1)
    newer = f"{run_id}|newer-owner"
    read = cache.read

    async def _read_after_expiry(access_id):
        # The lock expired mid-sweep and another worker took it.
        await redis_client.set(cache.reconcile_lock_key(), newer)
        return await read(access_id)

    monkeypatch.setattr(cache, "read", _read_after_expiry)
    report = await _reconciler(cache, store).reconcile(now=NOW)

    assert report is not None and not report.completed
    assert await redis_client.get(cache.projection_epoch_key()) == b"run-before-the-restart"
    assert (await redis_client.get(cache.reconcile_lock_key())).decode() == newer


async def test_repair_leaves_markers_and_equal_or_newer_projections_alone(
    redis_client, service, cache
):
    await service.commit(_authority(), subject_hash=SUBJECT_HASH, expected_revision=0, now=NOW)
    newer = _authority(revision=5)

    assert not await cache.reconcile_projection(
        ACCESS_ID, durable_revision=1, authority=None, ttl_seconds=None
    )
    assert (await cache.read(ACCESS_ID)).card_revision == 1

    await cache.claim_transition(ACCESS_ID, mutation_id="in-flight", expected_revision=1, ttl_seconds=15)
    assert not await cache.reconcile_projection(
        ACCESS_ID, durable_revision=5, authority=newer, ttl_seconds=60
    )
    assert (await cache.read(ACCESS_ID)).is_updating


async def test_a_tombstone_behind_a_reconsented_card_is_replaced(redis_client, service, store, cache):
    await service.commit(_authority(), subject_hash=SUBJECT_HASH, expected_revision=0, now=NOW)
    await service.revoke(subject_hash=SUBJECT_HASH, access_id=ACCESS_ID, expected_revision=1)
    tombstone = await redis_client.get(cache.card_key(ACCESS_ID))
    await service.commit(_authority(revision=3), subject_hash=SUBJECT_HASH, expected_revision=2, now=NOW)
    await redis_client.set(cache.card_key(ACCESS_ID), tombstone)

    pointer = await service.commit(
        replace_state(_authority(revision=3), CARD_STATE_ACTIVE),
        subject_hash=SUBJECT_HASH,
        expected_revision=3,
        now=NOW,
    )

    assert pointer.card_revision == 4

"""W701 (e)/(f): a v6.3 card_version PUBLISH serves what it published.

Live 10 Oct 23:0xZ (EMain, read-only): a person Control was durable at v13 while the Hub's serving
projection still held v12 with no expiry, the only stale one of 45. The phase-1 PUBLISH wrote current.json
and never touched the projection, so authorization kept evaluating v12 and the next Save was refused by the
properties pre-check that compares with the served copy.

These run the real Lua against a Redis named by ``REDIS_URL``, as test_card_projection_reconcile does.
"""

from __future__ import annotations

import hashlib
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.cache import DelegatedCardRuntimeCache
from connection_hub.delegated_credentials.cards.identity import CARD_KIND_CONNECTOR
from connection_hub.delegated_credentials.cards.model import (
    CARD_STATE_ACTIVE, CardAuthority, NamedServiceSelection, authority_projection_ttl,
)
from connection_hub.delegated_credentials.cards.service import DelegatedCardService
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore

GRANTOR = "platform-user-1"
SUBJECT_HASH = hashlib.sha256(GRANTOR.encode("utf-8")).hexdigest()
ACCESS_ID = "person-control-0123456789abcdef"
NOW = int(time.time())
WHEN = datetime.fromtimestamp(NOW, timezone.utc)

pytestmark = pytest.mark.skipif(not os.environ.get("REDIS_URL"),
                                reason="REDIS_URL is not set; real-Redis serving projection is skipped")


@asynccontextmanager
async def _no_lock(**_kwargs):
    yield {}


@pytest.fixture
async def redis_client():
    import redis.asyncio as redis_asyncio

    client = redis_asyncio.from_url(os.environ["REDIS_URL"])
    try:
        await client.ping()
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"Redis at REDIS_URL is unreachable: {exc}")
    yield client
    await client.aclose()


@pytest.fixture
def cache(redis_client) -> DelegatedCardRuntimeCache:
    return DelegatedCardRuntimeCache(redis_client, tenant=f"t-{uuid.uuid4().hex[:8]}",
                                     project=f"p-{uuid.uuid4().hex[:8]}")


@pytest.fixture
def store(tmp_path) -> BundleStorageDelegatedCardStore:
    return BundleStorageDelegatedCardStore(tmp_path)


@pytest.fixture
def service(store, cache) -> DelegatedCardService:
    return DelegatedCardService(store=store, cache=cache, mutation_lock=_no_lock)


def _card(revision: int = 1, **changes) -> CardAuthority:
    return replace(CardAuthority(
        access_id=ACCESS_ID, client_id="client-1", grantor_subject=GRANTOR, delegate_subject="integration:client-1",
        source="oauth", card_kind=CARD_KIND_CONNECTOR, label="person control", card_revision=revision,
        state=CARD_STATE_ACTIVE, resource_grants={"https://ex/mcp": ("work:observe",)},
        resource_operations={"https://ex/mcp": ("project.plan.read",)},
        named_service_operations=NamedServiceSelection.none(), created_at=NOW - 60, expires_at=NOW + 3600,
        properties={"connection_hub.control_snapshot": {"basis_catalog_version": "catalog-10-04"}}), **changes)


def _saved(revision: int) -> CardAuthority:
    """The person's managed save: two operations ticked, the snapshot basis moved (as v12 -> v13 live)."""
    return _card(revision, resource_operations={"https://ex/mcp": (
        "project.control.initialize", "project.control.update", "project.plan.read")},
        properties={"connection_hub.control_snapshot": {"basis_catalog_version": "catalog-10-09"}})


async def _save(service, card, *, txn):
    await service.stage_card_version(txn=txn, request_digest="d" * 64, catalog="catalog-1",
                                     members=[(SUBJECT_HASH, card.access_id, card.card_revision - 1, card)], now=WHEN)
    return await service.publish_card_version(txn=txn)


async def _served(cache):
    entry = await cache.read(ACCESS_ID)
    return None if entry is None else entry.authority


async def _durable(store):
    return (await store.read_current_authority(subject_hash=SUBJECT_HASH, access_id=ACCESS_ID))[1]


async def test_a_published_save_is_what_the_hub_serves_revision_properties_and_operations(service, store, cache):
    await service.commit(_card(), subject_hash=SUBJECT_HASH, expected_revision=0, now=NOW)
    assert (await _served(cache)).card_revision == 1  # served, as authorization reads it

    await _save(service, _saved(2), txn="w661-txn-" + "a" * 32)

    served, durable = await _served(cache), await _durable(store)
    assert durable.card_revision == 2
    assert served == durable  # revision, properties and resource_operations: the whole published Card
    assert served.properties["connection_hub.control_snapshot"]["basis_catalog_version"] == "catalog-10-09"


async def test_a_projection_left_behind_by_an_earlier_publish_is_brought_up_and_the_next_save_is_served(
        service, store, cache):
    """Her Card: a PUBLISH before this fix left the projection at v1 under a durable v2. The next PUBLISH
    fences on the durable revision, replaces the old projection first, and serves its own result."""
    await service.commit(_card(), subject_hash=SUBJECT_HASH, expected_revision=0, now=NOW)
    left_behind = "w661-txn-" + "b" * 32
    await service.stage_card_version(txn=left_behind, request_digest="d" * 64, catalog="catalog-1",
                                     members=[(SUBJECT_HASH, ACCESS_ID, 1, _saved(2))], now=WHEN)
    await tx.card_version_publish(store, txn=left_behind)  # the pre-fix PUBLISH: durable only
    assert (await _served(cache)).card_revision == 1 and (await _durable(store)).card_revision == 2

    await _save(service, _saved(3), txn="w661-txn-" + "c" * 32)
    assert await _served(cache) == await _durable(store)
    assert (await _served(cache)).card_revision == 3


async def test_a_refused_publish_leaves_no_marker_and_serves_the_revision_that_stands(service, store, cache):
    await service.commit(_card(), subject_hash=SUBJECT_HASH, expected_revision=0, now=NOW)
    txn = "w661-txn-" + "d" * 32
    await service.stage_card_version(txn=txn, request_digest="d" * 64, catalog="catalog-1",
                                     members=[(SUBJECT_HASH, ACCESS_ID, 1, _saved(2))], now=WHEN)
    await service.commit(_card(2, label="moved meanwhile"), subject_hash=SUBJECT_HASH, expected_revision=1, now=NOW)
    with pytest.raises(tx.CardTransactionRefused, match="card_changed"):
        await service.publish_card_version(txn=txn)
    entry = await cache.read(ACCESS_ID)
    assert entry is None or not entry.is_updating
    assert entry is None or entry.authority == await _durable(store)


async def test_a_rollback_that_finds_the_save_published_serves_it(service, store, cache):
    await service.commit(_card(), subject_hash=SUBJECT_HASH, expected_revision=0, now=NOW)
    txn = "w661-txn-" + "e" * 32
    answer = await service.stage_card_version(txn=txn, request_digest="d" * 64, catalog="catalog-1",
                                              members=[(SUBJECT_HASH, ACCESS_ID, 1, _saved(2))], now=WHEN)
    await tx.card_version_publish(store, txn=txn)  # published, its reply lost; the projection not refreshed
    links = [{k: m[k] for k in ("subject_hash", "access_id", "version", "checksum")} for m in answer]
    assert await service.rollback_card_version(txn=txn, links=links, at=WHEN) == "already_published"
    assert await _served(cache) == await _durable(store)


async def test_f_the_one_card_repair_reprojects_a_named_card_from_durable_current_without_any_listing(
        service, store, cache, monkeypatch):
    """(f) for a projection already left behind: one direct read of the named Card's durable current and one
    restore_projection, which installs only over an absent key or a strictly older ordinary projection
    (never over a marker, a tombstone or a newer revision)."""
    await service.commit(_card(), subject_hash=SUBJECT_HASH, expected_revision=0, now=NOW)
    txn = "w661-txn-" + "f" * 32
    await service.stage_card_version(txn=txn, request_digest="d" * 64, catalog="catalog-1",
                                     members=[(SUBJECT_HASH, ACCESS_ID, 1, _saved(2))], now=WHEN)
    await tx.card_version_publish(store, txn=txn)
    assert (await _served(cache)).card_revision == 1
    monkeypatch.setattr(type(store), "list_revision_names", None, raising=False)  # no listing is used

    durable = await _durable(store)  # the repair: one named Card, read directly
    assert await cache.restore_projection(durable, ttl_seconds=authority_projection_ttl(durable, int(time.time())))
    assert await _served(cache) == durable
    assert not await cache.restore_projection(_card(), ttl_seconds=None)  # an older one never displaces it

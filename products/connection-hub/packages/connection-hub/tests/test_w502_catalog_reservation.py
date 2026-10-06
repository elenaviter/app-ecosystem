"""W502: a Card transaction reserves the ACTIVE catalog version; publication waits for it.

EMain/CodeApp 19:18: the initiator's admin-minimum check evaluates the active
catalog and names it as ``catalog-active:<sha256(version)>``; the Hub holds
that version from prepare until the decision, and ``ensure_delegated_catalog``
(both publication routes) refuses, retryably, to publish another version
meanwhile. The same version republishing is allowed.
"""

from __future__ import annotations

import json
import logging

import pytest

from service_foundation.coordination.durable_decision_log import Coordinator, DecisionRefused

from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.card_participant import (
    PARTICIPANT, CardIntent, DecisionStorePort, HubCardParticipant, HubLocalReceiptVerifier, LocalCardIntentSource,
    hub_participant_input,
)
from connection_hub.delegated_credentials.catalog.publisher import CatalogPublicationError, ensure_delegated_catalog
from connection_hub.delegated_credentials.catalog.reservations import (
    PUBLICATION_MARKER, CatalogReservationRefused, CatalogReservations, catalog_version_digest,
)
from connection_hub.delegated_credentials.catalog.store import BundleStorageDelegatedCatalogStore
from test_card_participant import EFFECTS, SUBJECT_HASH, TXID, WITNESS, _draft, _Store
from test_card_transaction_store import _Applier, _setup
from test_catalog_publisher import CONNECTIONS, _Cache

INTENT = "d" * 64
OTHER_CONNECTIONS = {"delegated_credentials": {"oauth": {"enabled": True, "resources": [
    {"resource": "https://example.test/mcp", "grants": ["named_services:use", "named_services:admin"]}]}}}


async def _runner(**kwargs):
    if not await kwargs["ready"]():
        await kwargs["action"]()


async def _publish(store, connections):
    return await ensure_delegated_catalog(connections=connections, store=store, cache=_Cache(),
                                          operation_runner=_runner, reason="test")


async def _catalog(tmp_path):
    store = BundleStorageDelegatedCatalogStore(tmp_path / "bundle")
    first = await _publish(store, CONNECTIONS)
    return store, CatalogReservations(store), first.version


def _digest(version, content_hash):
    return catalog_version_digest(version, content_hash)


async def _active(store):
    active = await store.read_active()
    return _digest(active.version, active.content_hash)


# ── the reservation and the publisher, both orders ──


@pytest.mark.asyncio
async def test_a_reserved_version_blocks_another_publication_until_released(tmp_path, caplog):
    store, reservations, version = await _catalog(tmp_path)
    await reservations.reserve(transaction_id=TXID, intent_digest=INTENT, version_digest=await _active(store))
    with caplog.at_level(logging.WARNING), pytest.raises(CatalogPublicationError, match="catalog_reserved"):
        await _publish(store, OTHER_CONNECTIONS)
    assert (await store.read_active()).version == version
    assert not (store.root / PUBLICATION_MARKER).exists()
    assert any(f"catalog publication waiting on transaction {TXID}" in r.getMessage() for r in caplog.records)
    await reservations.release(TXID, intent_digest=INTENT)
    assert (await _publish(store, OTHER_CONNECTIONS)).version != version


@pytest.mark.asyncio
async def test_the_same_version_republishes_while_reserved(tmp_path):
    store, reservations, version = await _catalog(tmp_path)
    await reservations.reserve(transaction_id=TXID, intent_digest=INTENT, version_digest=await _active(store))
    assert (await _publish(store, CONNECTIONS)).version == version  # unchanged content: no new publication


@pytest.mark.asyncio
async def test_a_reservation_after_publication_of_a_newer_version_is_refused(tmp_path):
    store, reservations, version = await _catalog(tmp_path)
    old = await _active(store)
    await _publish(store, OTHER_CONNECTIONS)
    with pytest.raises(CatalogReservationRefused, match="catalog_version_moved"):
        await reservations.reserve(transaction_id=TXID, intent_digest=INTENT, version_digest=old)
    assert await reservations.holders() == []  # the refused fence is removed


@pytest.mark.asyncio
async def test_a_reservation_during_a_pending_publication_is_refused(tmp_path):
    # The other order: the publisher has marked its publication before this fence.
    store, reservations, version = await _catalog(tmp_path)
    await reservations.begin_publication(await store.read_active())
    with pytest.raises(CatalogReservationRefused, match="catalog_publication_pending") as refused:
        await reservations.reserve(transaction_id=TXID, intent_digest=INTENT, version_digest=await _active(store))
    assert refused.value.age_seconds is not None and refused.value.age_seconds >= 0
    await reservations.end_publication()
    await reservations.reserve(transaction_id=TXID, intent_digest=INTENT, version_digest=await _active(store))


@pytest.mark.asyncio
async def test_a_replay_holds_the_same_reservation_and_a_changed_one_is_refused(tmp_path):
    store, reservations, version = await _catalog(tmp_path)
    digest = await _active(store)
    await reservations.reserve(transaction_id=TXID, intent_digest=INTENT, version_digest=digest)
    await reservations.reserve(transaction_id=TXID, intent_digest=INTENT, version_digest=digest)
    with pytest.raises(CatalogReservationRefused, match="catalog_reservation_changed"):
        await reservations.reserve(transaction_id=TXID, intent_digest="a" * 64, version_digest=digest)
    assert len(await reservations.holders()) == 1


# ── through the Hub participant ──


async def _hub(tmp_path, *, bind=True, recorded_catalog=None):
    store, service, before, after = await _setup(tmp_path)
    service.bind_effect_applier(_Applier())
    catalog_store, reservations, version = await _catalog(tmp_path)
    digest = await _active(catalog_store)
    if bind:
        tx.bind_catalog_reservations(store, reservations)
    decisions = _Store()
    tx.bind_transaction_decisions(store, DecisionStorePort(decisions))
    hub_input = hub_participant_input(original=before, candidate=after, subject_hash=SUBJECT_HASH, action="update",
                                      actor_subject="person", actor_kind="caller", effects=EFFECTS,
                                      catalog_version_digest=digest)
    draft = _draft(before, after, hub_input=hub_input)
    row = await decisions.begin(draft)
    intents = LocalCardIntentSource(store)
    await intents.record(CardIntent(
        transaction_id=row.transaction_id, intent_digest=row.intent.digest, subject_hash=SUBJECT_HASH,
        original=before, candidate=after, effects=tuple(EFFECTS), action="update", actor_subject="person",
        actor_kind="caller",
        catalog=digest if recorded_catalog is None else recorded_catalog))
    hub = HubCardParticipant(service=service, store=store, intents=intents, decisions=decisions)
    return store, catalog_store, reservations, Coordinator(decisions, {PARTICIPANT: hub},
                                                           HubLocalReceiptVerifier(store)), draft, version


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["committed", "aborted"])
async def test_a_card_transaction_holds_the_catalog_from_prepare_to_its_decision(tmp_path, decision):
    store, catalog_store, reservations, coordinator, draft, version = await _hub(tmp_path)
    await coordinator.prepare(draft)
    assert (await tx.state(store, transaction_id=TXID))["catalog"] == await _active(catalog_store)
    with pytest.raises(CatalogPublicationError, match="catalog_reserved"):
        await _publish(catalog_store, OTHER_CONNECTIONS)
    await coordinator.decide(TXID, decision, **({"witness_digest": WITNESS} if decision == "committed" else {}))
    await coordinator.finish(TXID)
    assert await reservations.holders() == []
    assert (await _publish(catalog_store, OTHER_CONNECTIONS)).version != version


@pytest.mark.asyncio
async def test_a_moved_catalog_refuses_prepare_and_holds_nothing(tmp_path):
    store, catalog_store, reservations, coordinator, draft, version = await _hub(tmp_path)
    await _publish(catalog_store, OTHER_CONNECTIONS)
    with pytest.raises(DecisionRefused, match="catalog_version_moved"):
        await coordinator.prepare(draft)
    assert await tx.state(store, transaction_id=TXID) is None and await reservations.holders() == []
    await coordinator.decide(TXID, "aborted")
    await coordinator.finish(TXID)  # the abort tombstone; nothing was held


@pytest.mark.asyncio
async def test_a_crash_after_the_fence_before_the_receipt_is_released_by_the_abort(tmp_path):
    store, catalog_store, reservations, coordinator, draft, version = await _hub(tmp_path)
    intent = draft.bind(TXID, 1).digest
    await reservations.reserve(transaction_id=TXID, intent_digest=intent,
                               version_digest=await _active(catalog_store))  # the stage died here
    with pytest.raises(CatalogPublicationError, match="catalog_reserved"):
        await _publish(catalog_store, OTHER_CONNECTIONS)
    # No receipt is UNKNOWN, not ABORT (CodeApp 19:29): a replay holds it, and an abort
    # without this exact intent releases nothing.
    await reservations.reserve(transaction_id=TXID, intent_digest=intent, version_digest=await _active(catalog_store))
    await tx.abort_unstaged(store, TXID)
    await tx.abort_unstaged(store, "f" * 64, intent_digest=intent)
    await reservations.release(TXID, intent_digest="a" * 64)
    assert len(await reservations.holders()) == 1
    await tx.abort_unstaged(store, TXID, intent_digest=intent)  # the coordinator's authenticated ABORT
    assert await reservations.holders() == []


@pytest.mark.asyncio
async def test_the_recorded_catalog_must_equal_the_projections(tmp_path):
    store, catalog_store, reservations, coordinator, draft, version = await _hub(tmp_path, recorded_catalog="")
    with pytest.raises(DecisionRefused, match="card_intent_not_bound"):
        await coordinator.prepare(draft)
    assert await reservations.holders() == []


@pytest.mark.asyncio
async def test_without_a_catalog_store_a_catalog_reservation_is_refused(tmp_path):
    store, catalog_store, reservations, coordinator, draft, version = await _hub(tmp_path, bind=False)
    with pytest.raises(DecisionRefused, match="card_catalog_reservation_unavailable"):
        await coordinator.prepare(draft)


@pytest.mark.parametrize("dependencies", [
    {"catalog-active:" + "a" * 64: 2}, {"catalog-active:short": 1},
    {"catalog-active:" + "a" * 64: 1, "catalog-active:" + "b" * 64: 1},
])
def test_malformed_catalog_keys_are_refused(dependencies):
    from connection_hub.delegated_credentials.cards.card_participant import catalog_reservation_from_dependencies

    with pytest.raises(DecisionRefused, match="card_dependency_invalid"):
        catalog_reservation_from_dependencies(dependencies)


@pytest.mark.asyncio
async def test_the_digest_binds_the_versions_content_not_only_its_name(tmp_path):
    store, reservations, version = await _catalog(tmp_path)
    with pytest.raises(CatalogReservationRefused, match="catalog_version_moved"):
        await reservations.reserve(transaction_id=TXID, intent_digest=INTENT,
                                   version_digest=_digest(version, "0" * 64))  # same name, other content


@pytest.mark.asyncio
async def test_a_dead_publishers_marker_is_cleared_by_the_next_publication(tmp_path):
    store, reservations, version = await _catalog(tmp_path)
    await reservations.begin_publication(await store.read_active())  # a publisher killed here
    assert (await _publish(store, OTHER_CONNECTIONS)).version != version
    assert not (store.root / PUBLICATION_MARKER).exists()
    await reservations.reserve(transaction_id=TXID, intent_digest=INTENT, version_digest=await _active(store))


def _runner_lock(store, *, age_seconds=None):
    import os
    import time

    from connection_hub.delegated_credentials.catalog.reservations import PUBLISHER_OPERATION

    lock = store.root / ".kdcube.once" / f"{PUBLISHER_OPERATION}.lock"
    lock.mkdir(parents=True, exist_ok=True)
    heartbeat = lock / "heartbeat"
    heartbeat.write_text("beat\n")
    if age_seconds is not None:
        then = time.time() - age_seconds
        os.utime(heartbeat, (then, then))


def test_the_runner_lock_name_is_the_publishers_operation():
    from connection_hub.delegated_credentials.catalog.publisher import CATALOG_OPERATION
    from connection_hub.delegated_credentials.catalog.reservations import PUBLISHER_OPERATION

    assert PUBLISHER_OPERATION == CATALOG_OPERATION


@pytest.mark.asyncio
async def test_an_old_marker_is_kept_while_its_publisher_heartbeats(tmp_path):
    # EMain #611: a live publisher has no maximum hold; only its lock decides.
    import time

    store, reservations, version = await _catalog(tmp_path)
    await reservations.begin_publication(await store.read_active())
    _runner_lock(store)  # heartbeating now
    marker = store.root / PUBLICATION_MARKER
    marker.write_text(json.dumps({"version_digest": "x", "started_at": int(time.time()) - 86_400}))  # a day old
    assert await reservations.clear_stale_publication() is None
    assert marker.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("lock", ["absent", "stale"])
async def test_a_marker_whose_runner_lock_is_absent_or_stale_is_cleared(tmp_path, lock):
    from connection_hub.delegated_credentials.catalog.reservations import RUNNER_LOCK_TTL_SECONDS

    store, reservations, version = await _catalog(tmp_path)
    await reservations.begin_publication(await store.read_active())
    if lock == "stale":
        _runner_lock(store, age_seconds=RUNNER_LOCK_TTL_SECONDS + 5)
    assert await reservations.clear_stale_publication() is not None
    assert not (store.root / PUBLICATION_MARKER).exists()
    await reservations.reserve(transaction_id=TXID, intent_digest=INTENT, version_digest=await _active(store))


@pytest.mark.asyncio
async def test_the_cron_never_deletes_a_marker_rewritten_after_its_check(tmp_path):
    # EMain #611: compare-and-delete; a new publisher's marker written between check and delete survives.
    store, reservations, version = await _catalog(tmp_path)
    await reservations.begin_publication(await store.read_active())  # a dead publisher's marker, no lock
    original_alive = reservations.publisher_alive

    def alive_then_replaced(**kwargs):
        result = original_alive(**kwargs)  # stale: no lock
        (store.root / PUBLICATION_MARKER).write_text(json.dumps({"version_digest": "new", "started_at": 1,
                                                                  "nonce": "fresh"}))
        return result

    reservations.publisher_alive = alive_then_replaced
    assert await reservations.clear_stale_publication() is None
    assert json.loads((store.root / PUBLICATION_MARKER).read_text())["nonce"] == "fresh"


@pytest.mark.asyncio
async def test_a_publisher_whose_marker_was_deleted_rechecks_the_fences(tmp_path):
    # The other side: the cron deleted this publisher's marker and a reservation slipped in.
    store, reservations, version = await _catalog(tmp_path)
    original_assert = CatalogReservations.assert_publishable
    calls = []

    async def assert_then_race(self, document):
        calls.append(document.version)
        if len(calls) == 1:
            await original_assert(self, document)  # no fence yet
            await self.end_publication()  # the cron deletes the live marker
            await self.reserve(transaction_id=TXID, intent_digest=INTENT, version_digest=await _active(store))
            return
        await original_assert(self, document)

    CatalogReservations.assert_publishable = assert_then_race
    try:
        with pytest.raises(CatalogPublicationError, match="catalog_reserved"):
            await _publish(store, OTHER_CONNECTIONS)
    finally:
        CatalogReservations.assert_publishable = original_assert
    assert len(calls) == 2 and (await store.read_active()).version == version

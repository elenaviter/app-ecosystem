"""W502: a Card transaction reserves the ACTIVE catalog version; publication waits for it.

EMain/CodeApp 19:18: the initiator's admin-minimum check evaluates the active
catalog and names it as ``catalog-active:<sha256(version)>``; the Hub holds
that version from prepare until the decision, and ``ensure_delegated_catalog``
(both publication routes) refuses, retryably, to publish another version
meanwhile. The same version republishing is allowed.
"""

from __future__ import annotations

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


# ── the reservation and the publisher, both orders ──


@pytest.mark.asyncio
async def test_a_reserved_version_blocks_another_publication_until_released(tmp_path, caplog):
    store, reservations, version = await _catalog(tmp_path)
    await reservations.reserve(transaction_id=TXID, intent_digest=INTENT,
                               version_digest=catalog_version_digest(version))
    with caplog.at_level(logging.WARNING), pytest.raises(CatalogPublicationError, match="catalog_reserved"):
        await _publish(store, OTHER_CONNECTIONS)
    assert (await store.read_active()).version == version
    assert not (store.root / PUBLICATION_MARKER).exists()
    assert any(f"catalog publication waiting on transaction {TXID}" in r.getMessage() for r in caplog.records)
    await reservations.release(TXID)
    assert (await _publish(store, OTHER_CONNECTIONS)).version != version


@pytest.mark.asyncio
async def test_the_same_version_republishes_while_reserved(tmp_path):
    store, reservations, version = await _catalog(tmp_path)
    await reservations.reserve(transaction_id=TXID, intent_digest=INTENT,
                               version_digest=catalog_version_digest(version))
    assert (await _publish(store, CONNECTIONS)).version == version  # unchanged content: no new publication


@pytest.mark.asyncio
async def test_a_reservation_after_publication_of_a_newer_version_is_refused(tmp_path):
    store, reservations, version = await _catalog(tmp_path)
    await _publish(store, OTHER_CONNECTIONS)
    with pytest.raises(CatalogReservationRefused, match="catalog_version_moved"):
        await reservations.reserve(transaction_id=TXID, intent_digest=INTENT,
                                   version_digest=catalog_version_digest(version))
    assert await reservations.holders() == []  # the refused fence is removed


@pytest.mark.asyncio
async def test_a_reservation_during_a_pending_publication_is_refused(tmp_path):
    # The other order: the publisher has marked its publication before this fence.
    store, reservations, version = await _catalog(tmp_path)
    await reservations.begin_publication("next-version")
    with pytest.raises(CatalogReservationRefused, match="catalog_version_moved"):
        await reservations.reserve(transaction_id=TXID, intent_digest=INTENT,
                                   version_digest=catalog_version_digest(version))
    await reservations.end_publication()
    await reservations.reserve(transaction_id=TXID, intent_digest=INTENT,
                               version_digest=catalog_version_digest(version))


@pytest.mark.asyncio
async def test_a_replay_holds_the_same_reservation_and_a_changed_one_is_refused(tmp_path):
    store, reservations, version = await _catalog(tmp_path)
    digest = catalog_version_digest(version)
    await reservations.reserve(transaction_id=TXID, intent_digest=INTENT, version_digest=digest)
    await reservations.reserve(transaction_id=TXID, intent_digest=INTENT, version_digest=digest)
    with pytest.raises(CatalogReservationRefused, match="catalog_reservation_changed"):
        await reservations.reserve(transaction_id=TXID, intent_digest="a" * 64, version_digest=digest)
    assert len(await reservations.holders()) == 1


# ── through the Hub participant ──


async def _hub(tmp_path, *, digest, bind=True, recorded_catalog=None):
    store, service, before, after = await _setup(tmp_path)
    service.bind_effect_applier(_Applier())
    catalog_store, reservations, version = await _catalog(tmp_path)
    if bind:
        tx.bind_catalog_reservations(store, reservations)
    decisions = _Store()
    tx.bind_transaction_decisions(store, DecisionStorePort(decisions))
    hub_input = hub_participant_input(original=before, candidate=after, subject_hash=SUBJECT_HASH, action="update",
                                      actor_subject="person", actor_kind="caller", effects=EFFECTS,
                                      catalog_version_digest=digest(version))
    draft = _draft(before, after, hub_input=hub_input)
    row = await decisions.begin(draft)
    intents = LocalCardIntentSource(store)
    await intents.record(CardIntent(
        transaction_id=row.transaction_id, intent_digest=row.intent.digest, subject_hash=SUBJECT_HASH,
        original=before, candidate=after, effects=tuple(EFFECTS), action="update", actor_subject="person",
        actor_kind="caller",
        catalog=digest(version) if recorded_catalog is None else recorded_catalog))
    hub = HubCardParticipant(service=service, store=store, intents=intents, decisions=decisions)
    return store, catalog_store, reservations, Coordinator(decisions, {PARTICIPANT: hub},
                                                           HubLocalReceiptVerifier(store)), draft, version


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["committed", "aborted"])
async def test_a_card_transaction_holds_the_catalog_from_prepare_to_its_decision(tmp_path, decision):
    store, catalog_store, reservations, coordinator, draft, version = await _hub(tmp_path,
                                                                                 digest=catalog_version_digest)
    await coordinator.prepare(draft)
    assert (await tx.state(store, transaction_id=TXID))["catalog"] == catalog_version_digest(version)
    with pytest.raises(CatalogPublicationError, match="catalog_reserved"):
        await _publish(catalog_store, OTHER_CONNECTIONS)
    await coordinator.decide(TXID, decision, **({"witness_digest": WITNESS} if decision == "committed" else {}))
    await coordinator.finish(TXID)
    assert await reservations.holders() == []
    assert (await _publish(catalog_store, OTHER_CONNECTIONS)).version != version


@pytest.mark.asyncio
async def test_a_moved_catalog_refuses_prepare_and_holds_nothing(tmp_path):
    store, catalog_store, reservations, coordinator, draft, version = await _hub(tmp_path,
                                                                                 digest=catalog_version_digest)
    await _publish(catalog_store, OTHER_CONNECTIONS)
    with pytest.raises(DecisionRefused, match="catalog_version_moved"):
        await coordinator.prepare(draft)
    assert await tx.state(store, transaction_id=TXID) is None and await reservations.holders() == []
    await coordinator.decide(TXID, "aborted")
    await coordinator.finish(TXID)  # the abort tombstone; nothing was held


@pytest.mark.asyncio
async def test_a_crash_after_the_fence_before_the_receipt_is_released_by_the_abort(tmp_path):
    store, catalog_store, reservations, coordinator, draft, version = await _hub(tmp_path,
                                                                                 digest=catalog_version_digest)
    await reservations.reserve(transaction_id=TXID, intent_digest=draft.bind(TXID, 1).digest,
                               version_digest=catalog_version_digest(version))  # the stage died here
    with pytest.raises(CatalogPublicationError, match="catalog_reserved"):
        await _publish(catalog_store, OTHER_CONNECTIONS)
    await tx.abort_unstaged(store, TXID)  # the coordinator's ABORT finishes it
    assert await reservations.holders() == []


@pytest.mark.asyncio
async def test_the_recorded_catalog_must_equal_the_projections(tmp_path):
    store, catalog_store, reservations, coordinator, draft, version = await _hub(
        tmp_path, digest=catalog_version_digest, recorded_catalog="")
    with pytest.raises(DecisionRefused, match="card_intent_not_bound"):
        await coordinator.prepare(draft)
    assert await reservations.holders() == []


@pytest.mark.asyncio
async def test_without_a_catalog_store_a_catalog_reservation_is_refused(tmp_path):
    store, catalog_store, reservations, coordinator, draft, version = await _hub(
        tmp_path, digest=catalog_version_digest, bind=False)
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

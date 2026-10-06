"""W578: a read set holds Cards, absences and the catalog under one decision, and writes nothing.

At the store and service, then through the Hub participant from a signed
Problem Board-style authority: every read is verified at its revision and
fenced before the receipt; while prepared no write lands on a held Card and
no Card is created at a held absence; the recorded decision (either one)
only releases the fences; nothing is ever staged or served.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from service_foundation.coordination.durable_decision_log import DecisionRecord, DecisionRefused, IntentDraft

from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.card_participant import PARTICIPANT
from connection_hub.delegated_credentials.cards.card_read_set import (
    hub_read_set_participant_input, read_set_candidate_value,
)
from connection_hub.delegated_credentials.cards.store import CardStorageError
from connection_hub.delegated_credentials.catalog.reservations import CatalogReservations, catalog_version_digest
from test_card_transaction_store import INTENT, NOW, SUBJECT_HASH, _setup

RS = "e" * 64
CATALOG_VERSION, CATALOG_HASH = "catalog-v1", "h" * 64
CATALOG = catalog_version_digest(CATALOG_VERSION, CATALOG_HASH)


def _bind_catalog(store, tmp_path):
    async def read_active():
        return SimpleNamespace(version=CATALOG_VERSION, content_hash=CATALOG_HASH)

    reservations = CatalogReservations(SimpleNamespace(root=tmp_path / "catalog", read_active=read_active))
    tx.bind_catalog_reservations(store, reservations)
    return reservations


def _reads(before):
    return [{"subject_hash": SUBJECT_HASH, "access_id": before.access_id, "revision": before.card_revision},
            {"subject_hash": SUBJECT_HASH, "access_id": "aut_absent", "revision": 0}]


async def _prepare(service, before, **extra):
    return await service.stage_read_set_transaction(transaction_id=RS, intent_digest=INTENT, participant="project",
                                                    reads=_reads(before), catalog=CATALOG, **extra)


@pytest.mark.asyncio
async def test_a_prepared_read_set_fences_every_read_and_the_catalog_and_writes_nothing(tmp_path):
    store, service, before, after = await _setup(tmp_path)
    reservations = _bind_catalog(store, tmp_path)
    receipt = await _prepare(service, before)
    assert receipt["state"] == "prepared" and receipt["catalog"] == CATALOG
    for access_id in (before.access_id, "aut_absent"):
        with pytest.raises(CardStorageError, match="card_transaction_unresolved"):
            await tx.assert_replaceable(store, subject_hash=SUBJECT_HASH, access_id=access_id)
    assert [holder["transaction_id"] for holder in await reservations.holders()] == [RS]
    assert (await store.read_current_authority(subject_hash=SUBJECT_HASH, access_id=before.access_id))[1] == before
    assert [entry["transaction_id"] for entry in await tx.list_in_doubt(store)] == [RS]


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["committed", "aborted"])
async def test_the_recorded_decision_only_releases_the_fences(tmp_path, decision):
    store, service, before, after = await _setup(tmp_path)
    reservations = _bind_catalog(store, tmp_path)
    await _prepare(service, before)
    store._card_transaction_decisions.recorded[RS] = decision
    decided = await service.decide_read_set_transaction(transaction_id=RS, intent_digest=INTENT, decision=decision)
    assert decided["state"] == decision and await tx.list_in_doubt(store) == []
    assert await reservations.holders() == []
    for access_id in (before.access_id, "aut_absent"):
        await tx.assert_replaceable(store, subject_hash=SUBJECT_HASH, access_id=access_id)
    assert (await store.read_current_authority(subject_hash=SUBJECT_HASH, access_id=before.access_id))[1] == before
    again = await service.decide_read_set_transaction(transaction_id=RS, intent_digest=INTENT, decision=decision)
    assert again == decided


@pytest.mark.asyncio
async def test_a_moved_read_is_refused_and_leaves_no_receipt(tmp_path):
    store, service, before, after = await _setup(tmp_path)
    _bind_catalog(store, tmp_path)
    stale = [{"subject_hash": SUBJECT_HASH, "access_id": before.access_id, "revision": before.card_revision + 1}]
    with pytest.raises(tx.CardTransactionRefused, match="card_dependency_moved"):
        await service.stage_read_set_transaction(transaction_id=RS, intent_digest=INTENT, participant="project",
                                                 reads=stale)
    assert await tx.read_receipt(store, RS) is None


@pytest.mark.asyncio
async def test_a_decision_must_be_recorded_and_a_read_set_is_finished_only_as_one(tmp_path):
    store, service, before, after = await _setup(tmp_path)
    _bind_catalog(store, tmp_path)
    await _prepare(service, before)
    with pytest.raises(tx.CardTransactionRefused, match="card_transaction_decision_not_recorded"):
        await service.decide_read_set_transaction(transaction_id=RS, intent_digest=INTENT, decision="committed")
    with pytest.raises(tx.CardTransactionRefused, match="card_transaction_is_read_set"):
        await tx.decide(store, transaction_id=RS, intent_digest=INTENT, decision="committed")
    with pytest.raises(tx.CardTransactionRefused, match="card_transaction_replay_changed"):
        await tx.stage(store, transaction_id=RS, intent_digest=INTENT, participant="project",
                       subject_hash=SUBJECT_HASH, original=before, candidate=after,
                       now=__import__("datetime").datetime.fromtimestamp(NOW, __import__("datetime").timezone.utc))


@pytest.mark.asyncio
async def test_a_replay_with_other_reads_is_refused(tmp_path):
    store, service, before, after = await _setup(tmp_path)
    _bind_catalog(store, tmp_path)
    await _prepare(service, before)
    assert (await _prepare(service, before))["state"] == "prepared"  # an exact replay
    with pytest.raises(tx.CardTransactionRefused, match="card_transaction_replay_changed"):
        await service.stage_read_set_transaction(transaction_id=RS, intent_digest=INTENT, participant="project",
                                                 reads=_reads(before)[:1], catalog=CATALOG)


# ── through the Hub participant, from a signed authority ──

@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["committed", "aborted"])
async def test_a_pb_initiated_read_set_prepares_and_finishes_through_the_participant(tmp_path, decision):
    from test_w578_card_group_participant import AUTHORITY, TX, _Authority

    from connection_hub.delegated_credentials.cards.authority_intent_source import (
        AuthorityCardIntentSource, AuthorityDecisionReader,
    )
    from connection_hub.delegated_credentials.cards.card_participant import DecisionStorePort, HubCardParticipant

    store, service, before, after = await _setup(tmp_path)
    reservations = _bind_catalog(store, tmp_path)
    reads = _reads(before)
    authority = _Authority(hub_read_set_participant_input(reads=reads, catalog_version_digest=CATALOG,
                                                          actor_subject="platform-user-1", actor_kind="caller"),
                           read_set_candidate_value(reads, CATALOG))
    clock = lambda: 1_800_000_000  # noqa: E731
    decisions = AuthorityDecisionReader(fetch=authority.fetch, authority=AUTHORITY, clock=clock)
    tx.bind_transaction_decisions(store, DecisionStorePort(decisions))
    hub = HubCardParticipant(service=service, store=store,
                             intents=AuthorityCardIntentSource(store=store, fetch=authority.fetch,
                                                               authority=AUTHORITY, clock=clock),
                             decisions=decisions)
    receipt = await hub.prepare(TX)
    assert receipt.transaction_id == TX and [r.transaction_id for r in await hub.list_prepared(limit=5)] == [TX]
    with pytest.raises(CardStorageError, match="card_transaction_unresolved"):
        await tx.assert_replaceable(store, subject_hash=SUBJECT_HASH, access_id=before.access_id)
    authority.decision = decision
    await hub.finish(TX, decision)
    assert (await tx.state(store, transaction_id=TX))["state"] == decision
    assert await reservations.holders() == [] and await tx.list_in_doubt(store) == []
    await tx.assert_replaceable(store, subject_hash=SUBJECT_HASH, access_id=before.access_id)
    assert (await store.read_current_authority(subject_hash=SUBJECT_HASH, access_id=before.access_id))[1] == before

"""W756 reserve-before-DECIDE leaf; productive PB/PG qualification is separate."""

from dataclasses import replace

import pytest

from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.store import CardStorageError
from test_w661_card_versions import DIGEST, SUBJECT_HASH, TXN, WHEN, _setup

OTHER = "w756-other-" + "b" * 32


@pytest.mark.asyncio
async def test_a_successful_stage_reserves_the_base_until_its_own_abort(tmp_path):
    store, service, before, after = await _setup(tmp_path)
    await service.stage_card_version(txn=TXN, request_digest=DIGEST, catalog="catalog-1", now=WHEN,
                                     members=[(SUBJECT_HASH, before.access_id, 1, after)])
    prepared = []

    async def prepare():
        prepared.append(True)

    with pytest.raises(CardStorageError, match="card_version_base_reserved"):
        await service.stage_card_version(
            txn=OTHER, request_digest="e" * 64, catalog="catalog-1", now=WHEN,
            members=[(SUBJECT_HASH, before.access_id, 1, replace(after, label="different whole Card"))],
            prepare=prepare,
        )
    assert prepared == []  # refused before any second policy prepare/decision
    assert await tx.read_card_version_marker(store, OTHER) is None
    assert (await store.read_current_authority(subject_hash=SUBJECT_HASH, access_id=before.access_id))[1] == before
    with pytest.raises(CardStorageError, match="card_version_base_reserved"):
        await service.commit(replace(after, label="ordinary competing writer"),
                             subject_hash=SUBJECT_HASH, expected_revision=1, now=int(WHEN.timestamp()))
    assert await service.rollback_card_version(txn=TXN) == "rolled_back"
    await service.stage_card_version(txn=OTHER, request_digest="e" * 64, catalog="catalog-1", now=WHEN,
                                     members=[(SUBJECT_HASH, before.access_id, 1, after)])
    await service.publish_card_version(txn=OTHER)
    assert (await store.read_current_authority(subject_hash=SUBJECT_HASH, access_id=before.access_id))[1] == after


@pytest.mark.asyncio
async def test_abort_cleanup_cut_is_retired_and_cannot_publish_then_redrives(tmp_path, monkeypatch):
    store, service, before, after = await _setup(tmp_path)
    await service.stage_card_version(txn=TXN, request_digest=DIGEST, catalog="catalog-1", now=WHEN,
                                     members=[(SUBJECT_HASH, before.access_id, 1, after)])
    real = tx._release_card_version_bases

    async def stop(*args, **kwargs):
        raise CardStorageError("injected_abort_cleanup_cut")

    monkeypatch.setattr(tx, "_release_card_version_bases", stop)
    with pytest.raises(CardStorageError, match="injected_abort_cleanup_cut"):
        await service.rollback_card_version(txn=TXN)
    assert (await tx.read_card_version_marker(store, TXN))["state"] == "rolled_back"
    with pytest.raises(tx.CardTransactionRefused, match="txn_closed"):
        await service.publish_card_version(txn=TXN)
    with pytest.raises(CardStorageError, match="card_version_base_reserved"):
        await service.stage_card_version(txn=OTHER, request_digest="e" * 64, catalog="catalog-1", now=WHEN,
                                         members=[(SUBJECT_HASH, before.access_id, 1, after)])
    monkeypatch.setattr(tx, "_release_card_version_bases", real)
    assert await service.rollback_card_version(txn=TXN) == "rolled_back"
    assert (await store.read_current_authority(subject_hash=SUBJECT_HASH, access_id=before.access_id))[1] == before
    await service.stage_card_version(txn=OTHER, request_digest="e" * 64, catalog="catalog-1", now=WHEN,
                                     members=[(SUBJECT_HASH, before.access_id, 1, after)])


@pytest.mark.asyncio
async def test_completed_effects_do_not_report_saved_until_reservation_cleanup_finishes(tmp_path, monkeypatch):
    store, service, before, after = await _setup(tmp_path)
    await service.stage_card_version(txn=TXN, request_digest=DIGEST, catalog="catalog-1", now=WHEN,
                                     members=[(SUBJECT_HASH, before.access_id, 1, after)])
    real = tx._release_card_version_bases

    async def stop(*args, **kwargs):
        raise CardStorageError("injected_publish_cleanup_cut")

    monkeypatch.setattr(tx, "_release_card_version_bases", stop)
    with pytest.raises(CardStorageError, match="injected_publish_cleanup_cut"):
        await service.publish_card_version(txn=TXN)
    with pytest.raises(tx.CardTransactionRefused, match="effects_pending"):
        await tx.card_version_outcome(store, txn=TXN)
    monkeypatch.setattr(tx, "_release_card_version_bases", real)
    await service.publish_card_version(txn=TXN)
    assert await tx.read_card_version_marker(store, TXN) is None
    later = replace(after, card_revision=3, label="next whole Card")
    await service.stage_card_version(txn=OTHER, request_digest="e" * 64, catalog="catalog-1", now=WHEN,
                                     members=[(SUBJECT_HASH, before.access_id, 2, later)])
    assert (await store.read_current_authority(subject_hash=SUBJECT_HASH, access_id=before.access_id))[1] == after


@pytest.mark.asyncio
async def test_partial_prepare_keeps_the_base_reserved_and_abort_releases_every_effect(tmp_path):
    store, service, before, after = await _setup(tmp_path)
    effects = [{"kind": "invocation_policy", "key": key} for key in ("one", "two", "three")]

    async def interrupted():
        raise CardStorageError("injected_second_prepare_cut")

    with pytest.raises(CardStorageError, match="injected_second_prepare_cut"):
        await service.stage_card_version(
            txn=TXN, request_digest=DIGEST, catalog="catalog-1", now=WHEN,
            members=[(SUBJECT_HASH, before.access_id, 1, after)], effects=effects, prepare=interrupted,
        )
    with pytest.raises(CardStorageError, match="card_version_base_reserved"):
        await service.stage_card_version(txn=OTHER, request_digest="e" * 64, catalog="catalog-1", now=WHEN,
                                         members=[(SUBJECT_HASH, before.access_id, 1, after)])
    released = []

    async def release(effect, marker):
        released.append(effect["key"])

    assert await service.rollback_card_version(txn=TXN, release=release) == "rolled_back"
    assert released == ["one", "two", "three"]
    await service.stage_card_version(txn=OTHER, request_digest="e" * 64, catalog="catalog-1", now=WHEN,
                                     members=[(SUBJECT_HASH, before.access_id, 1, after)])
    await service.publish_card_version(txn=OTHER)

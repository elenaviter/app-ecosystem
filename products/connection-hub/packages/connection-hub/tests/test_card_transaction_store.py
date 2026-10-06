"""W578: the generic staged Card participant on the real filesystem store and Card service."""

from __future__ import annotations

from dataclasses import replace

import pytest

from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.service import CardConflict, DelegatedCardService
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore, CardStorageError
from test_card_service import _Cache, _authority, SUBJECT_HASH, NOW

TX = "a" * 64
INTENT = "b" * 64


async def _setup(tmp_path):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def mutation_lock(**kwargs):
        yield

    store = BundleStorageDelegatedCardStore(tmp_path)
    service = DelegatedCardService(store=store, cache=_Cache(), mutation_lock=mutation_lock)
    before = _authority()
    await service.commit(before, subject_hash=SUBJECT_HASH, expected_revision=0, now=NOW)
    after = replace(before, card_revision=before.card_revision + 1, label="staged change")
    return store, service, before, after


async def _visible(store, card):
    return (await store.read_current_authority(subject_hash=SUBJECT_HASH, access_id=card.access_id))[1]


async def _stage(store, before, after, **changes):
    from datetime import datetime, timezone
    when = NOW if isinstance(NOW, datetime) else datetime.fromtimestamp(NOW, timezone.utc)
    values = dict(transaction_id=TX, intent_digest=INTENT, participant="project", subject_hash=SUBJECT_HASH,
                  original=before, candidate=after, now=when)
    values.update(changes)
    return await tx.stage(store, **values)


@pytest.mark.asyncio
async def test_a_staged_change_is_invisible_until_the_recorded_decision_commits_it(tmp_path):
    store, _, before, after = await _setup(tmp_path)
    receipt = await _stage(store, before, after)
    assert receipt["state"] == "prepared"
    assert await _visible(store, before) == before  # readers still get BEFORE
    decided = await tx.decide(store, transaction_id=TX, intent_digest=INTENT, decision="committed")
    assert decided["state"] == "committed"
    assert await _visible(store, before) == after  # the one rename made AFTER visible


@pytest.mark.asyncio
async def test_an_aborted_transaction_leaves_before_and_releases_the_card(tmp_path):
    store, service, before, after = await _setup(tmp_path)
    await _stage(store, before, after)
    await tx.decide(store, transaction_id=TX, intent_digest=INTENT, decision="aborted", reason="last_admin")
    assert await _visible(store, before) == before
    # An ordinary write proceeds again once the transaction is decided.
    nxt = replace(before, card_revision=before.card_revision + 1, label="ordinary edit")
    await service.commit(nxt, subject_hash=SUBJECT_HASH, expected_revision=before.card_revision, now=NOW)
    assert await _visible(store, before) == nxt


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["commit", "revoke"])
async def test_no_ordinary_writer_publishes_around_an_undecided_staged_card(tmp_path, writer):
    store, service, before, after = await _setup(tmp_path)
    await _stage(store, before, after)
    with pytest.raises(CardConflict, match="card_transaction_unresolved"):
        if writer == "commit":
            await service.commit(replace(before, card_revision=before.card_revision + 1, label="bypass"),
                                 subject_hash=SUBJECT_HASH, expected_revision=before.card_revision, now=NOW)
        else:
            await service.revoke(subject_hash=SUBJECT_HASH, access_id=before.access_id,
                                 expected_revision=before.card_revision)
    assert await _visible(store, before) == before


@pytest.mark.asyncio
async def test_the_decision_is_exactly_once_and_bound_to_the_intent(tmp_path):
    store, _, before, after = await _setup(tmp_path)
    await _stage(store, before, after)
    with pytest.raises(tx.CardTransactionRefused, match="card_transaction_intent_mismatch"):
        await tx.decide(store, transaction_id=TX, intent_digest="c" * 64, decision="committed")
    await tx.decide(store, transaction_id=TX, intent_digest=INTENT, decision="committed")
    again = await tx.decide(store, transaction_id=TX, intent_digest=INTENT, decision="committed")
    assert again["state"] == "committed"  # idempotent
    with pytest.raises(tx.CardTransactionRefused, match="card_transaction_decision_conflict"):
        await tx.decide(store, transaction_id=TX, intent_digest=INTENT, decision="aborted")
    with pytest.raises(tx.CardTransactionRefused, match="card_transaction_decision_invalid"):
        await tx.decide(store, transaction_id=TX, intent_digest=INTENT, decision="maybe")


@pytest.mark.asyncio
async def test_staging_replays_exactly_and_refuses_a_changed_replay(tmp_path):
    store, _, before, after = await _setup(tmp_path)
    first = await _stage(store, before, after)
    assert await _stage(store, before, after) == first
    with pytest.raises(tx.CardTransactionRefused, match="card_transaction_replay_changed"):
        await _stage(store, before, replace(after, label="different"))


@pytest.mark.asyncio
async def test_staging_refuses_a_moved_card_or_a_wrong_candidate(tmp_path):
    store, _, before, after = await _setup(tmp_path)
    with pytest.raises(tx.CardTransactionRefused, match="card_transaction_revision_moved"):
        await _stage(store, replace(before, label="not what is stored"), after)
    with pytest.raises(tx.CardTransactionRefused, match="card_transaction_candidate_invalid"):
        await _stage(store, before, replace(after, card_revision=before.card_revision + 2))


@pytest.mark.asyncio
async def test_a_crash_after_the_prepared_receipt_leaves_the_card_readable_as_before(tmp_path, monkeypatch):
    store, _, before, after = await _setup(tmp_path)

    async def crash(**kwargs):
        raise RuntimeError("killed after the receipt, before the staged revision")

    monkeypatch.setattr(store, "write_revision", crash)
    with pytest.raises(RuntimeError):
        await _stage(store, before, after)
    assert await _visible(store, before) == before
    assert (await tx.state(store, transaction_id=TX))["state"] == "prepared"


@pytest.mark.asyncio
async def test_an_unknown_transaction_or_a_bad_id_is_refused(tmp_path):
    store, _, _, _ = await _setup(tmp_path)
    with pytest.raises(tx.CardTransactionRefused, match="card_transaction_unknown"):
        await tx.decide(store, transaction_id=TX, intent_digest=INTENT, decision="committed")
    with pytest.raises(CardStorageError, match="card_transaction_id_invalid"):
        await tx.state(store, transaction_id="not-hex")

"""Live 19:21:40Z: one refresh refused "card identity refused (reason=durable_card_unreadable)" while another
refresh of the same owner was inside its Card transaction; the same client succeeded a minute later.

The owner listing (identity lookup, the owner's Cards) needs each Card's identity, never its current grants,
so a Card inside a Card transaction lists as its last committed revision instead of refusing the whole listing.
Reading that Card for use still waits for the decision and its effects.
"""
from __future__ import annotations

import logging
from dataclasses import replace

import pytest

from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.service import CardServingUnavailable
from connection_hub.delegated_credentials.cards.store import CardStorageError
from test_card_service import SUBJECT_HASH
from test_card_transaction_store import EFFECTS, _Applier, _service_decide, _setup, _stage, _staged_with_effects
from test_credentialless_card_persistence import _Handles, _persistence


async def _owner(tmp_path, *, effects_pending: bool):
    if effects_pending:
        applier = _Applier(fail_on=("invocation_policy", "memories/search"))
        store, service, before, after = await _staged_with_effects(tmp_path, applier)
        with pytest.raises(CardServingUnavailable):  # committed; its effects are still outstanding
            await _service_decide(store, service, before, "committed")
    else:
        store, service, before, after = await _setup(tmp_path)
        await _stage(store, before, after)  # prepared; the coordinator has recorded no decision
    other = replace(before, access_id="aut_other1", label="another Card of the owner")
    await service.commit(other, subject_hash=SUBJECT_HASH, expected_revision=0, now=1_780_000_000)
    persistence = _persistence({}, _Handles({}))
    persistence._store = store  # type: ignore[attr-defined]
    return store, persistence, before, after, other


@pytest.mark.asyncio
@pytest.mark.parametrize("effects_pending", [False, True], ids=["undecided", "committed-effects-pending"])
async def test_the_owner_listing_reads_through_another_cards_transaction(tmp_path, effects_pending):
    store, persistence, before, after, other = await _owner(tmp_path, effects_pending=effects_pending)
    listed = {authority.access_id: authority for authority in
              await persistence.list_all_current(subject_hash=SUBJECT_HASH)}
    assert set(listed) == {before.access_id, other.access_id}
    # Undecided lists BEFORE; committed lists AFTER even before its effects finish. Identity is the same.
    assert listed[before.access_id] == (after if effects_pending else before)
    assert (listed[before.access_id].grantor_subject, listed[before.access_id].client_id) == (
        before.grantor_subject, before.client_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("effects_pending", [False, True], ids=["undecided", "committed-effects-pending"])
async def test_using_the_card_inside_its_transaction_still_waits(tmp_path, effects_pending):
    store, _, before, _, _ = await _owner(tmp_path, effects_pending=effects_pending)
    reason = "card_effects_pending" if effects_pending else "card_transaction_undecided"
    with pytest.raises(CardStorageError, match=reason):
        await store.read_current_authority(subject_hash=SUBJECT_HASH, access_id=before.access_id)


@pytest.mark.asyncio
async def test_a_listing_that_still_fails_logs_its_fixed_reason_only(tmp_path, caplog):
    store, persistence, before, _, _ = await _owner(tmp_path, effects_pending=False)
    tx.bind_transaction_decisions(store, None)  # a corrupt binding: the listing must still fail closed
    store.current_path(subject_hash=SUBJECT_HASH, access_id=before.access_id).write_text("{not json")
    caplog.set_level(logging.WARNING)
    from connection_hub.delegated_credentials.cards.persistence import CardUnavailable
    with pytest.raises(CardUnavailable, match="durable_card_unreadable"):
        await persistence.list_all_current(subject_hash=SUBJECT_HASH)
    assert "Card listing could not read a Card (reason=" in caplog.text
    assert "not json" not in caplog.text

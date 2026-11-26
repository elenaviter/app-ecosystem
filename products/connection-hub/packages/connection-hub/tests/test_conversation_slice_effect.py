"""A slice change rides a Card transaction as the conversation_slice effect."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.cards.effect_targets import ConversationSliceTarget
from connection_hub.delegated_credentials.cards.participant_effects import ParticipantEffectRefused
from connection_hub.delegated_credentials.slices.store import (
    BundleStorageConversationSliceStore, ConversationSlice, SlicePrepared,
)
from connection_hub.delegated_credentials.slices.turns import TurnRefused, TurnTokens, begin_turn
from test_turn_token import CARD, KEY, Registry, SEED

GRANTED = {"named_service_operations": {"https://n.example/mcp": {"slack": ["object.list", "object.action.post"]}}}


def _slice(selection=SEED, **changes):
    return ConversationSlice(grantor_subject="user-1", card_access_id=CARD.access_id, conversation_id="c1",
                             card_generation=1, base_card_revision=7, selection=selection, **changes)


def _binding(tx="tx-1", key="slice"):
    return SimpleNamespace(transaction_id=tx, key=key, effect_digest=hashlib.sha256(f"{tx}:{key}".encode()).hexdigest())


def _payload(selection=GRANTED, expected=1):
    return {"access_id": CARD.access_id, "owner_subject": "user-1", "expected_revision": expected,
            "slice": _slice(selection).to_document()}


async def _seeded(tmp_path):
    store = BundleStorageConversationSliceStore(tmp_path)
    await store.write(_slice(), expected_revision=0)
    return store


@pytest.mark.asyncio
async def test_stage_closes_the_slice_and_commit_applies_it_once(tmp_path):
    store = await _seeded(tmp_path)
    target = ConversationSliceTarget(store)
    await target.prepare_once(_binding(), _payload())

    # A picker-only write refuses while it is prepared (and a bounded wait gives up).
    with pytest.raises(SlicePrepared):
        await store.write(_slice({}), expected_revision=1)
    with pytest.raises(SlicePrepared):
        await store.write_when_open(_slice({}), expected_revision=1, wait_seconds=0.05, poll_seconds=0.01)
    # Option c: a turn is not blocked; it keeps the committed slice.
    _, kept = await begin_turn(store=store, registry=Registry(), tokens=TurnTokens(KEY), card=CARD,
                               application="app@1-0", conversation_id="c1", turn_id="t1", seed_selection=SEED,
                               card_generation=1, now=10)
    assert kept.slice_revision == 1 and kept.selection == SEED

    await target.apply_once(_binding(), _payload())
    await target.apply_once(_binding(), _payload())  # FINISH replay
    current = await store.read(grantor_subject="user-1", card_access_id=CARD.access_id, conversation_id="c1")
    assert current.slice_revision == 2 and current.selection == GRANTED
    assert await store.prepared_changes(grantor_subject="user-1", card_access_id=CARD.access_id,
                                        conversation_id="c1") == []


@pytest.mark.asyncio
async def test_abort_or_an_expired_undecided_transaction_releases_only_its_marker(tmp_path):
    store = await _seeded(tmp_path)
    target = ConversationSliceTarget(store)
    await target.prepare_once(_binding(), _payload())
    # Another transaction cannot stage over it.
    with pytest.raises(ParticipantEffectRefused, match="card_effect_target_busy"):
        await target.prepare_once(_binding("tx-2"), _payload())

    # The recovery that aborts an expired undecided intent calls release_once.
    await target.release_once(_binding(), _payload())
    await target.release_once(_binding(), _payload())  # idempotent
    current = await store.read(grantor_subject="user-1", card_access_id=CARD.access_id, conversation_id="c1")
    assert current.slice_revision == 1 and current.selection == SEED
    await store.write(_slice({}), expected_revision=1)  # open again


@pytest.mark.asyncio
async def test_a_moved_slice_refuses_the_transaction_before_its_decision(tmp_path):
    store = await _seeded(tmp_path)
    await store.write(_slice({}), expected_revision=1)
    with pytest.raises(ParticipantEffectRefused, match="card_effect_target_revision_moved"):
        await ConversationSliceTarget(store).prepare_once(_binding(), _payload(expected=1))


@pytest.mark.asyncio
async def test_consent_builds_one_effect_that_adds_only_the_granted_choice(tmp_path):
    from connection_hub.delegated_credentials.slices.consent import conversation_slice_effect

    store = await _seeded(tmp_path)
    effect = await conversation_slice_effect(store, card=CARD, conversation_id="c1", granted={
        "named_service_operations": {"https://n.example/mcp": {"slack": ["object.action.post"]}}})
    assert effect["kind"] == "conversation_slice" and effect["payload"]["expected_revision"] == 1
    target = ConversationSliceTarget(store)
    await target.prepare_once(_binding(), effect["payload"])
    await target.apply_once(_binding(), effect["payload"])
    current = await store.read(grantor_subject="user-1", card_access_id=CARD.access_id, conversation_id="c1")
    assert current.selection == {"named_service_operations": {"https://n.example/mcp": {
        "slack": ["object.action.post", "object.list"]}}}
    with pytest.raises(Exception, match="conversation_slice_category_not_guarded"):
        await conversation_slice_effect(store, card=CARD, conversation_id="c1", granted={"model": ["x"]})
    with pytest.raises(Exception, match="conversation_slice_missing"):
        await conversation_slice_effect(store, card=CARD, conversation_id="other", granted={})


@pytest.mark.asyncio
async def test_a_replayed_commit_finds_its_own_revision_after_a_later_picker_write(tmp_path):
    store = await _seeded(tmp_path)
    target = ConversationSliceTarget(store)
    await target.prepare_once(_binding(), _payload())
    await target.apply_once(_binding(), _payload())
    # The marker is gone, so a picker write lands before FINISH records the effect.
    await store.write(_slice({}), expected_revision=2)
    # FINISH replays COMMIT: it finds the revision its change wrote, not the newest content.
    await target.apply_once(_binding(), _payload())
    current = await store.read(grantor_subject="user-1", card_access_id=CARD.access_id, conversation_id="c1")
    assert current.slice_revision == 3 and current.selection == {}


@pytest.mark.asyncio
async def test_a_bounded_picker_write_lands_once_the_transaction_is_decided(tmp_path):
    import asyncio

    store = await _seeded(tmp_path)
    target = ConversationSliceTarget(store)
    await target.prepare_once(_binding(), _payload())

    async def decide_later():
        await asyncio.sleep(0.05)
        await target.release_once(_binding(), _payload())

    waiting = asyncio.create_task(store.write_when_open(_slice({}), expected_revision=1, wait_seconds=2,
                                                        poll_seconds=0.01))
    await decide_later()
    assert (await waiting).slice_revision == 2


def test_card_version_never_carries_a_conversation_slice_effect():
    from connection_hub.delegated_credentials.cards import participant_card_version

    # CardVersion's Hub effects are a closed set; its recovery
    # proof is not the legacy decision log, so a slice marker there would stay forever.
    source = open(participant_card_version.__file__, encoding="utf-8").read()
    assert "conversation_slice" not in source

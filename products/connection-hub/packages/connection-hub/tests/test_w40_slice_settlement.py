"""W40 review E2: an undecided transaction past its intent lease releases its slice marker."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from service_foundation.coordination.durable_decision_log import DecisionRefused

from connection_hub.delegated_credentials.cards import composition
from connection_hub.delegated_credentials.cards.effect_targets import ConversationSliceTarget
from connection_hub.delegated_credentials.slices.store import BundleStorageConversationSliceStore, SlicePrepared
from test_w40_slice_effect import _binding, _payload, _slice

MEMBER = "a" * 64


class Decisions:
    def __init__(self, expired: bool):
        self.expired, self.state = expired, None

    async def read(self, transaction_id):
        return SimpleNamespace(terminal=self.state is not None, state=self.state,
                               intent=SimpleNamespace(digest="digest-1"))

    async def abort_expired(self, transaction_id):
        if not self.expired:
            raise DecisionRefused("not_expired")
        self.state = "aborted"
        return await self.read(transaction_id)


async def _world(tmp_path, monkeypatch, *, expired):
    decisions = Decisions(expired)
    store = BundleStorageConversationSliceStore(tmp_path)
    target = ConversationSliceTarget(store)
    finished = []

    async def finish(transaction_id):
        finished.append(transaction_id)
        await target.release_once(_binding(MEMBER), _payload())

    async def receipt(_store, member_id):
        return {"transaction_id": member_id, "intent_digest": "digest-1"}

    monkeypatch.setattr("connection_hub.delegated_credentials.cards.transaction_store.read_receipt", receipt)
    coordinator = SimpleNamespace(store=decisions, finish=finish)
    intents = SimpleNamespace(binding=lambda transaction_id: _async(SimpleNamespace(authority="")))
    store.bind_settler(composition.slice_marker_settler(coordinator, intents, card_store=None))
    await store.write(_slice(), expected_revision=0)
    await target.prepare_once(_binding(MEMBER), _payload())
    return store, finished


async def _async(value):
    return value


@pytest.mark.asyncio
async def test_e2_an_expired_undecided_transaction_is_aborted_and_its_marker_released(tmp_path, monkeypatch):
    store, finished = await _world(tmp_path, monkeypatch, expired=True)
    # The next writer settles the stale marker instead of staying closed forever.
    written = await store.write(_slice({}), expected_revision=1)
    assert written.slice_revision == 2 and finished == [MEMBER]


@pytest.mark.asyncio
async def test_a_live_undecided_transaction_keeps_its_marker(tmp_path, monkeypatch):
    store, finished = await _world(tmp_path, monkeypatch, expired=False)
    with pytest.raises(SlicePrepared):
        await store.write(_slice({}), expected_revision=1)
    assert finished == []
    assert await store.prepared_changes(grantor_subject="user-1", card_access_id=_slice().card_access_id,
                                        conversation_id="c1") == [f"{MEMBER}:slice"]

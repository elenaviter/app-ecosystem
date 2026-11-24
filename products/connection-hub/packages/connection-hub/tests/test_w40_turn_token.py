"""W40 T4, T4b, T4c, T5, T6: the turn token and the one-time slice seeding."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.slices.store import (
    BundleStorageConversationSliceStore, ConversationSlice, SLICE_STATE_DELETING,
)
from connection_hub.delegated_credentials.slices.turns import (
    TOKEN_SECONDS, TURN_CAP_SECONDS, RegisteredConversation, TurnRefused, TurnTokens, begin_turn,
)

KEY = b"k" * 32
CARD = SimpleNamespace(access_id="agent-user-1", grantor_subject="user-1", card_revision=7)
SEED = {"named_service_operations": {"https://n.example/mcp": {"slack": ["object.list"]}}}


class Registry:
    def __init__(self, owner="user-1", application="app@1-0", alive=True, fail=False):
        self.owner, self.application, self.alive, self.fail, self.reads = owner, application, alive, fail, 0

    async def conversation(self, *, owner_subject, conversation_id):
        self.reads += 1
        if self.fail:
            raise RuntimeError("registry down")
        if self.owner is None or owner_subject != self.owner:
            return None
        return RegisteredConversation(owner_subject=self.owner, application=self.application)

    async def turn_alive(self, *, owner_subject, conversation_id, turn_id):
        return self.alive


async def _begin(tmp_path, registry, *, now=1000, token="", turn="t1", conversation="c1", generation=1, store=None):
    return await begin_turn(store=store or BundleStorageConversationSliceStore(tmp_path), registry=registry,
                            tokens=TurnTokens(KEY), card=CARD, application="app@1-0", conversation_id=conversation,
                            turn_id=turn, seed_selection=SEED, card_generation=generation, now=now,
                            existing_token=token)


@pytest.mark.asyncio
async def test_the_first_turn_seeds_once_and_later_turns_never_reread_the_registry(tmp_path):
    registry, store = Registry(), BundleStorageConversationSliceStore(tmp_path)
    token, seeded = await _begin(tmp_path, registry, store=store)
    assert seeded.slice_revision == 1 and seeded.selection == SEED and seeded.base_card_revision == 7
    claims = TurnTokens(KEY).verify(token, card_access_id=CARD.access_id, now=1001)
    assert (claims.conversation_id, claims.turn_id, claims.expires_at) == ("c1", "t1", 1000 + TOKEN_SECONDS)

    await _begin(tmp_path, registry, store=store, turn="t2", now=2000)
    assert registry.reads == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("registry,reason", [
    (Registry(owner=None), "conversation_not_owned"),
    (Registry(owner="user-2"), "conversation_not_owned"),
    (Registry(application="other@1-0"), "conversation_application_mismatch"),
    (Registry(fail=True), "conversation_registry_unavailable"),
])
async def test_t5_a_foreign_unknown_or_unverifiable_conversation_gets_no_slice(tmp_path, registry, reason):
    with pytest.raises(TurnRefused, match=reason):
        await _begin(tmp_path, registry)
    assert await BundleStorageConversationSliceStore(tmp_path).read(
        grantor_subject="user-1", card_access_id=CARD.access_id, conversation_id="c1") is None


def test_t4_t4b_the_token_names_one_card_and_is_not_a_bearer():
    tokens = TurnTokens(KEY)
    from connection_hub.delegated_credentials.slices.turns import TurnClaims
    token = tokens.issue(TurnClaims("agent-user-1", "c1", "t1", 1000, 1600))
    with pytest.raises(TurnRefused, match="turn_token_card_mismatch"):
        tokens.verify(token, card_access_id="agent-user-2", now=1001)
    with pytest.raises(TurnRefused, match="turn_token_expired"):
        tokens.verify(token, card_access_id="agent-user-1", now=1600)
    with pytest.raises(TurnRefused, match="turn_token_invalid"):
        TurnTokens(b"x" * 32).verify(token, card_access_id="agent-user-1", now=1001)
    with pytest.raises(TurnRefused, match="turn_token_invalid"):
        tokens.verify(token[:-1] + ("0" if token[-1] != "0" else "1"), card_access_id="agent-user-1", now=1001)


@pytest.mark.asyncio
async def test_t4c_renewal_needs_the_same_live_turn_and_stops_at_the_cap(tmp_path):
    store = BundleStorageConversationSliceStore(tmp_path)
    token, _ = await _begin(tmp_path, Registry(), store=store)
    renewed, _ = await _begin(tmp_path, Registry(), store=store, token=token, now=1000 + TOKEN_SECONDS - 5)
    assert TurnTokens(KEY).verify(renewed, card_access_id=CARD.access_id, now=1000 + TOKEN_SECONDS).turn_started_at == 1000
    with pytest.raises(TurnRefused, match="turn_not_alive"):
        await _begin(tmp_path, Registry(alive=False), store=store, token=token, now=1100)
    with pytest.raises(TurnRefused, match="turn_token_renewal_mismatch"):
        await _begin(tmp_path, Registry(), store=store, token=token, turn="t2", now=1100)
    with pytest.raises(TurnRefused, match="turn_token_cap_reached"):
        await _begin(tmp_path, Registry(), store=store, token=token, now=1000 + TURN_CAP_SECONDS)


@pytest.mark.asyncio
async def test_t6_a_deleting_slice_gets_no_token_and_a_new_card_generation_reseeds(tmp_path):
    store = BundleStorageConversationSliceStore(tmp_path)
    _, first = await _begin(tmp_path, Registry(), store=store)
    reseeded = (await _begin(tmp_path, Registry(), store=store, generation=2))[1]
    assert reseeded.card_generation == 2 and reseeded.slice_revision == 2
    await store.write(ConversationSlice(**{**reseeded.__dict__, "state": SLICE_STATE_DELETING}), expected_revision=2)
    with pytest.raises(TurnRefused, match="conversation_deleting"):
        await _begin(tmp_path, Registry(), store=store, generation=3)


@pytest.mark.asyncio
async def test_b3_a_token_of_an_earlier_card_generation_is_refused(tmp_path):
    token, _ = await _begin(tmp_path, Registry(), generation=1)
    with pytest.raises(TurnRefused, match="turn_token_card_generation_mismatch"):
        TurnTokens(KEY).verify(token, card_access_id=CARD.access_id, now=1001, card_generation=2)
    assert TurnTokens(KEY).verify(token, card_access_id=CARD.access_id, now=1001, card_generation=1).card_generation == 1

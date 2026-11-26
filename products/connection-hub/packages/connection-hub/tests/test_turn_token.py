"""The turn token and the one-time slice seeding."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.slices.store import (
    BundleStorageConversationSliceStore, ConversationSlice,
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
async def test_a_foreign_unknown_or_unverifiable_conversation_gets_no_slice(tmp_path, registry, reason):
    with pytest.raises(TurnRefused, match=reason):
        await _begin(tmp_path, registry)
    assert await BundleStorageConversationSliceStore(tmp_path).read(
        grantor_subject="user-1", card_access_id=CARD.access_id, conversation_id="c1") is None


def test_the_token_names_one_card_and_is_not_a_bearer():
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
async def test_renewal_needs_the_same_live_turn_and_stops_at_the_cap(tmp_path):
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
async def test_a_new_card_generation_reseeds(tmp_path):
    store = BundleStorageConversationSliceStore(tmp_path)
    _, first = await _begin(tmp_path, Registry(), store=store)
    reseeded = (await _begin(tmp_path, Registry(), store=store, generation=2))[1]
    assert reseeded.card_generation == 2 and reseeded.slice_revision == 2


@pytest.mark.asyncio
async def test_a_token_of_an_earlier_card_generation_is_refused(tmp_path):
    token, _ = await _begin(tmp_path, Registry(), generation=1)
    with pytest.raises(TurnRefused, match="turn_token_card_generation_mismatch"):
        TurnTokens(KEY).verify(token, card_access_id=CARD.access_id, now=1001, card_generation=2)
    assert TurnTokens(KEY).verify(token, card_access_id=CARD.access_id, now=1001, card_generation=1).card_generation == 1


def test_the_turn_token_key_is_purpose_bound_and_needs_the_secret():
    from connection_hub.delegated_credentials.grantor_authority_proof import _key as proof_key
    from connection_hub.delegated_credentials.slices.turns import turn_tokens_from_secret

    secret = "s" * 40
    tokens = turn_tokens_from_secret(secret)
    assert tokens is not None and turn_tokens_from_secret("short") is None and turn_tokens_from_secret("") is None
    # Same secret, different purpose: the turn-token key is not the grantor-proof key.
    assert tokens._key != proof_key(secret)
    from connection_hub.delegated_credentials.slices.turns import TurnClaims
    token = tokens.issue(TurnClaims("agent-1", "c1", "t1", 1, 100, 5))
    with pytest.raises(TurnRefused, match="turn_token_invalid"):
        turn_tokens_from_secret("t" * 40).verify(token, card_access_id="agent-1", now=2)


@pytest.mark.asyncio
async def test_a_per_call_token_lives_to_the_turn_cap_and_is_never_renewed(tmp_path):
    store = BundleStorageConversationSliceStore(tmp_path)
    token, _ = await begin_turn(store=store, registry=Registry(), tokens=TurnTokens(KEY), card=CARD,
                                application="app@1-0", conversation_id="c1", turn_id="t1", seed_selection=SEED,
                                card_generation=1, now=1000, mode="per_call")
    claims = TurnTokens(KEY).verify(token, card_access_id=CARD.access_id, now=1001)
    assert claims.mode == "per_call" and claims.expires_at == 1000 + TURN_CAP_SECONDS
    with pytest.raises(TurnRefused, match="turn_token_not_renewable"):
        await begin_turn(store=store, registry=Registry(), tokens=TurnTokens(KEY), card=CARD, application="app@1-0",
                         conversation_id="c1", turn_id="t1", seed_selection=SEED, card_generation=1, now=1100,
                         existing_token=token)
    with pytest.raises(TurnRefused, match="turn_token_mode_invalid"):
        await begin_turn(store=store, registry=Registry(), tokens=TurnTokens(KEY), card=CARD, application="app@1-0",
                         conversation_id="c1", turn_id="t2", seed_selection=SEED, card_generation=1, now=1100,
                         mode="forever")


NARROWED = {"named_service_operations": {"https://n.example/mcp": {"slack": []}}}


class EpochRegistry(Registry):
    def __init__(self, epoch=0):
        super().__init__()
        self.epoch = epoch

    async def conversation(self, *, owner_subject, conversation_id):
        self.reads += 1
        return RegisteredConversation(owner_subject=self.owner, application=self.application, epoch=self.epoch)


async def _begin_epoch(store, registry, *, epoch, turn, now):
    return await begin_turn(store=store, registry=registry, tokens=TurnTokens(KEY), card=CARD,
                            application="app@1-0", conversation_id="c1", turn_id=turn, seed_selection=SEED,
                            card_generation=1, now=now, conversation_epoch=epoch)


@pytest.mark.asyncio
async def test_a_conversation_created_again_under_its_id_does_not_inherit_the_earlier_slice(tmp_path):
    """The slice records the deletion epoch it was seeded under; another epoch means another conversation."""
    store, registry = BundleStorageConversationSliceStore(tmp_path), EpochRegistry(epoch=0)
    _, first = await _begin_epoch(store, registry, epoch=0, turn="t1", now=1000)
    narrowed = await store.write(
        ConversationSlice(grantor_subject="user-1", card_access_id=CARD.access_id, conversation_id="c1",
                          card_generation=1, base_card_revision=7, selection=NARROWED, conversation_epoch=0),
        expected_revision=first.slice_revision)
    # Same conversation, same epoch: the slice and its choice are kept, no registry read.
    _, same = await _begin_epoch(store, registry, epoch=0, turn="t2", now=1100)
    assert same.slice_revision == narrowed.slice_revision and same.selection == NARROWED
    assert registry.reads == 1
    # The conversation was deleted at 5000 and a new one uses the id: seeded again from the defaults.
    registry.epoch = 5000
    _, reseeded = await _begin_epoch(store, registry, epoch=5000, turn="t3", now=6000)
    assert reseeded.selection == SEED and reseeded.conversation_epoch == 5000 and registry.reads == 2
    # From then on the new slice is used as it is.
    _, kept = await _begin_epoch(store, registry, epoch=5000, turn="t4", now=6100)
    assert kept.slice_revision == reseeded.slice_revision and registry.reads == 2


@pytest.mark.asyncio
async def test_a_request_without_an_epoch_uses_the_slice_as_it_is(tmp_path):
    store, registry = BundleStorageConversationSliceStore(tmp_path), EpochRegistry(epoch=0)
    _, first = await _begin_epoch(store, registry, epoch=0, turn="t1", now=1000)
    _, again = await _begin_epoch(store, registry, epoch=None, turn="t2", now=1100)
    assert again.slice_revision == first.slice_revision and registry.reads == 1


def test_the_epoch_survives_the_stored_document():
    value = ConversationSlice(grantor_subject="user-1", card_access_id=CARD.access_id, conversation_id="c1",
                              card_generation=1, base_card_revision=7, conversation_epoch=4242)
    assert ConversationSlice.from_document(value.to_document()).conversation_epoch == 4242
    legacy = value.to_document()
    legacy.pop("conversation_epoch")
    assert ConversationSlice.from_document(legacy).conversation_epoch == 0


@pytest.mark.asyncio
async def test_a_turn_that_loses_the_seeding_race_uses_the_slice_the_winner_seeded(tmp_path):
    """Two requests seed one conversation at once; the loser reads the winner instead of refusing."""
    store, registry = BundleStorageConversationSliceStore(tmp_path), EpochRegistry(epoch=0)
    _, winner = await _begin_epoch(store, registry, epoch=0, turn="t1", now=1000)
    original = store.read
    calls = {"n": 0}

    async def stale_first_read(**scope):
        calls["n"] += 1
        return None if calls["n"] == 1 else await original(**scope)

    store.read = stale_first_read
    token, used = await _begin_epoch(store, registry, epoch=0, turn="t2", now=1100)
    assert token and used.slice_revision == winner.slice_revision


@pytest.mark.asyncio
async def test_a_seeding_race_won_under_another_epoch_still_refuses(tmp_path):
    store, registry = BundleStorageConversationSliceStore(tmp_path), EpochRegistry(epoch=0)
    await _begin_epoch(store, registry, epoch=0, turn="t1", now=1000)
    original = store.read
    calls = {"n": 0}

    async def stale_first_read(**scope):
        calls["n"] += 1
        return None if calls["n"] == 1 else await original(**scope)

    store.read = stale_first_read
    registry.epoch = 5000
    with pytest.raises(TurnRefused, match="conversation_slice_revision_conflict"):
        await _begin_epoch(store, registry, epoch=5000, turn="t2", now=1100)

"""A call in a conversation is slice AND Agent Card AND Control; a slice only narrows."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
import time

import pytest

from connection_hub.delegated_credentials.agent_capability_policy import AGENT_CAPABILITY_METADATA_PROPERTY
from connection_hub.delegated_credentials.controls.effective import effective_card_authority
from connection_hub.delegated_credentials.slices.guard import SliceGuardRefused, effective_for_turn, narrow_by_slice
from connection_hub.delegated_credentials.slices.store import (
    BundleStorageConversationSliceStore, ConversationSlice, SLICE_STATE_DELETING,
)
from connection_hub.delegated_credentials.slices.turns import TurnClaims, TurnRefused, TurnTokens
from test_agent_capability_control_sync import NAMED_RESOURCE, OWNER
from test_descriptor_agent_consent import POST, READ, _consent_world

KEY = b"g" * 32
CONNECTOR = "urn:connection-hub:remote-mcp:user-1:weather"


async def _pair(tmp_path):
    service, store, decisions, resident, control, _, _ = await _consent_world(tmp_path)
    # The Card holds READ and POST; the Control allows both.
    resident = dataclasses.replace(
        resident,
        resource_grants={**resident.resource_grants, NAMED_RESOURCE: ("named_services:use", "slack:post", "slack:read"),
                         CONNECTOR: ("mcp:call",)},
        resource_operations={**resident.resource_operations, CONNECTOR: ("forecast",)})
    metadata = dict(control.properties.get(AGENT_CAPABILITY_METADATA_PROPERTY) or {})
    entries = dict(metadata.get("entries") or {})
    entries["resource_families"] = {"user_external_mcp": {"resource_patterns": ["urn:connection-hub:remote-mcp:*"]}}
    control = dataclasses.replace(control, properties={**control.properties,
                                                       AGENT_CAPABILITY_METADATA_PROPERTY: {**metadata, "entries": entries}})
    return resident, control


def _slice(card, selection, **changes):
    return ConversationSlice(grantor_subject=OWNER, card_access_id=card.access_id, conversation_id="c1",
                             card_generation=card.created_at, base_card_revision=card.card_revision, selection=selection, **changes)


def _slack(card):
    return set(card.named_service_operations.operations.get(NAMED_RESOURCE, {}).get("slack", ()))


@pytest.mark.asyncio
async def test_a_slice_narrows_the_card_and_never_widens_it(tmp_path):
    card, control = await _pair(tmp_path)
    # The Card's own (unmaterialized) selection holds READ and POST.
    card = dataclasses.replace(card, named_service_operations=type(card.named_service_operations).exact(
        {NAMED_RESOURCE: {"slack": [READ, POST]}}))
    narrowed = narrow_by_slice(card, control, _slice(card, {"named_service_operations": {NAMED_RESOURCE: {
        "slack": [READ]}}}))
    assert _slack(narrowed) == {READ}

    # A slice naming more than the Card holds grants nothing more.
    reader = dataclasses.replace(card, named_service_operations=type(card.named_service_operations).exact(
        {NAMED_RESOURCE: {"slack": [READ]}}))
    widened = narrow_by_slice(reader, control, _slice(card, {"named_service_operations": {NAMED_RESOURCE: {
        "slack": [READ, POST]}}}))
    assert _slack(widened) == {READ}


@pytest.mark.asyncio
async def test_the_family_switch_closes_the_users_connectors_in_this_conversation_only(tmp_path):
    card, control = await _pair(tmp_path)
    off = narrow_by_slice(card, control, _slice(card, {"resource_families": []}))
    assert CONNECTOR not in off.resource_grants and NAMED_RESOURCE in off.resource_grants
    on = narrow_by_slice(card, control, _slice(card, {"resource_families": ["user_external_mcp"],
                                                      "resource_grants": {NAMED_RESOURCE: ["named_services:use", "slack:post", "slack:read"]}}))
    # A family that stays on is governed by its switch, not by the exact resource list.
    assert CONNECTOR in on.resource_grants and on.resource_operations[CONNECTOR] == ("forecast",)


@pytest.mark.asyncio
async def test_the_guard_entry_reads_the_slice_by_the_turn_token_and_fails_closed(tmp_path):
    card, control = await _pair(tmp_path)
    slices, tokens, now = BundleStorageConversationSliceStore(tmp_path / "slices"), TurnTokens(KEY), int(time.time())
    token = tokens.issue(TurnClaims(card.access_id, "c1", "t1", now, now + 600, card.created_at))

    # Outside any conversation the Card's own authority applies.
    assert await effective_for_turn(effective=card, control=control, slices=slices, tokens=tokens,
                                    turn_token="", now=now) == card
    with pytest.raises(SliceGuardRefused, match="conversation_slice_missing"):
        await effective_for_turn(effective=card, control=control, slices=slices, tokens=tokens, turn_token=token, now=now)
    await slices.write(_slice(card, {"named_service_operations": {NAMED_RESOURCE: {"slack": [READ]}}}), expected_revision=0)
    narrowed = await effective_for_turn(effective=card, control=control, slices=slices, tokens=tokens,
                                        turn_token=token, now=now)
    assert _slack(narrowed) == {READ}
    with pytest.raises(TurnRefused, match="turn_token_card_mismatch"):
        await effective_for_turn(effective=dataclasses.replace(card, access_id="agent-other"), control=control,
                                 slices=slices, tokens=tokens, turn_token=token, now=now)
    await slices.write(_slice(card, {}, state=SLICE_STATE_DELETING), expected_revision=1)
    with pytest.raises(SliceGuardRefused, match="conversation_deleting"):
        await effective_for_turn(effective=card, control=control, slices=slices, tokens=tokens, turn_token=token, now=now)


@pytest.mark.asyncio
async def test_the_request_path_narrows_the_live_composition_and_denies_on_any_refusal(tmp_path):
    from types import SimpleNamespace

    from connection_hub.delegated_credentials.live_grant import LiveGrantCardError, ResolvedCardComposition
    from connection_hub.delegated_credentials.slices import guard

    card, control = await _pair(tmp_path)
    composition = ResolvedCardComposition(caller_card=card, control_card=control, effective_card=card)
    card_store = SimpleNamespace(_root=tmp_path / "cards")
    now = int(time.time())
    token = TurnTokens(KEY).issue(TurnClaims(card.access_id, "c1", "t1", now, now + 600, card.created_at))

    with pytest.raises(LiveGrantCardError, match="conversation_slices_unavailable"):
        await guard.narrow_live_composition_for_turn(composition, card_store=card_store, turn_token=token,
                                                     resolvers=SimpleNamespace(turn_tokens=None))
    readers = SimpleNamespace(turn_tokens=TurnTokens(KEY), conversation_registry=None)
    assert await guard.narrow_live_composition_for_turn(composition, card_store=card_store, turn_token="",
                                                        resolvers=readers) is composition
    with pytest.raises(LiveGrantCardError, match="conversation_slice_missing"):
        await guard.narrow_live_composition_for_turn(composition, card_store=card_store, turn_token=token,
                                                     resolvers=readers)
    await BundleStorageConversationSliceStore(card_store._root).write(
        _slice(card, {"resource_families": []}), expected_revision=0)
    narrowed = await guard.narrow_live_composition_for_turn(composition, card_store=card_store, turn_token=token,
                                                            resolvers=readers)
    assert CONNECTOR not in narrowed.effective_card.resource_grants and narrowed.caller_card is card


@pytest.mark.asyncio
async def test_a_call_outside_any_conversation_gets_the_cards_defaults_not_every_grant(tmp_path):
    from connection_hub.delegated_credentials.agent_capability_policy import AGENT_CAPABILITY_DEFAULT_STANDARD_PROPERTY

    card, control = await _pair(tmp_path)
    card = dataclasses.replace(card, named_service_operations=type(card.named_service_operations).exact(
        {NAMED_RESOURCE: {"slack": [READ, POST]}}))
    slices, tokens = BundleStorageConversationSliceStore(tmp_path / "slices"), TurnTokens(KEY)
    # A legacy Card (no defaults recorded) keeps its authority.
    assert await effective_for_turn(effective=card, control=control, slices=slices, tokens=tokens,
                                    turn_token="", now=0) == card
    defaulted = dataclasses.replace(card, properties={**card.properties, AGENT_CAPABILITY_DEFAULT_STANDARD_PROPERTY: {
        "resource_grants": {NAMED_RESOURCE: ["named_services:use", "slack:post", "slack:read"]},
        "resource_operations": {NAMED_RESOURCE: list(card.resource_operations.get(NAMED_RESOURCE, ()))},
        "named_service_operations": {NAMED_RESOURCE: {"slack": [READ]}}}})
    headless = await effective_for_turn(effective=defaulted, control=control, slices=slices, tokens=tokens,
                                        turn_token="", now=0)
    assert _slack(headless) == {READ}


def test_the_fence_reasons_answer_one_retryable_authority_update_reason():
    from connection_hub.delegated_credentials.live_grant import AUTHORITY_UPDATING, authority_update_reason

    for reason in ("card_updating", "control_card_updating", "card_transaction_undecided", "card_effects_pending"):
        assert authority_update_reason(reason) == AUTHORITY_UPDATING
    for reason in ("source_invalid", "access_id_mismatch", ""):
        assert authority_update_reason(reason) is None


@pytest.mark.asyncio
async def test_alpha_2_a_rotation_with_a_warm_cache_still_verifies_a_token_of_the_new_key():
    from connection_hub.delegated_credentials.slices.serving_parts import TurnTokenSource
    from connection_hub.delegated_credentials.slices.turns import turn_tokens_from_secret

    secret, clock = {"value": "a" * 40}, {"now": 0.0}

    async def read():
        return secret["value"]

    source = TurnTokenSource(read, ttl_seconds=300, reread_seconds=30, clock=lambda: clock["now"])
    old = turn_tokens_from_secret("a" * 40).issue(TurnClaims("agent-1", "c1", "t1", 0, 999, 0))
    assert (await source.verify(old, card_access_id="agent-1", now=1)).turn_id == "t1"
    secret["value"] = "b" * 40  # rotated; ingress's cache still holds the old key
    new = turn_tokens_from_secret("b" * 40).issue(TurnClaims("agent-1", "c1", "t2", 0, 999, 0))
    clock["now"] = 31.0
    assert (await source.verify(new, card_access_id="agent-1", now=1)).turn_id == "t2"
    assert not hasattr(source, "_secret") and "b" * 40 not in repr(vars(source))


@pytest.mark.asyncio
async def test_alpha_3_a_per_call_token_is_refused_once_the_turn_ended_within_five_seconds(tmp_path):
    from connection_hub.delegated_credentials.slices.serving_parts import LivenessCache

    card, control = await _pair(tmp_path)
    slices, tokens, clock = BundleStorageConversationSliceStore(tmp_path / "s"), TurnTokens(KEY), {"now": 0.0}
    await slices.write(_slice(card, {"named_service_operations": {NAMED_RESOURCE: {"slack": [READ]}}}),
                       expected_revision=0)
    now = int(time.time())
    token = tokens.issue(TurnClaims(card.access_id, "c1", "t1", now, now + 7200, card.created_at, "per_call"))
    state = {"alive": True, "reads": 0}

    class Registry:
        async def turn_alive(self, **_):
            state["reads"] += 1
            return state["alive"]

    registry = LivenessCache(Registry(), ttl_seconds=5, clock=lambda: clock["now"])
    call = dict(effective=card, control=control, slices=slices, tokens=tokens, turn_token=token, now=now,
                registry=registry)
    await effective_for_turn(**call)
    clock["now"] = 2.0
    await effective_for_turn(**call)
    assert state["reads"] == 1  # a positive answer is cached briefly
    state["alive"] = False  # the turn ended: its lock was released
    clock["now"] = 6.0
    with pytest.raises(SliceGuardRefused, match="turn_not_alive"):
        await effective_for_turn(**call)
    with pytest.raises(SliceGuardRefused, match="conversation_registry_unavailable"):
        await effective_for_turn(**{**call, "registry": None})

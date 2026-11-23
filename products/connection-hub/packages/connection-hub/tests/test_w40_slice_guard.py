"""W40 T6, T9: a call in a conversation is slice AND Agent Card AND Control; a slice only narrows."""

from __future__ import annotations

import dataclasses
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
                             card_generation=1, base_card_revision=card.card_revision, selection=selection, **changes)


def _slack(card):
    return set(card.named_service_operations.operations.get(NAMED_RESOURCE, {}).get("slack", ()))


@pytest.mark.asyncio
async def test_t9_a_slice_narrows_the_card_and_never_widens_it(tmp_path):
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
async def test_t6_the_guard_entry_reads_the_slice_by_the_turn_token_and_fails_closed(tmp_path):
    card, control = await _pair(tmp_path)
    slices, tokens, now = BundleStorageConversationSliceStore(tmp_path / "slices"), TurnTokens(KEY), int(time.time())
    token = tokens.issue(TurnClaims(card.access_id, "c1", "t1", now, now + 600))

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

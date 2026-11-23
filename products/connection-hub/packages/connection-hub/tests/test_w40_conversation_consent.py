"""W40 T7: a consent given in a conversation changes the Agent Card and that conversation's slice in one decision."""

from __future__ import annotations

import time

import pytest

from connection_hub.delegated_credentials.cards.effect_targets import compose_card_effects
from connection_hub.delegated_credentials.slices.store import BundleStorageConversationSliceStore, ConversationSlice
from connection_hub.delegated_credentials.slices.turns import TurnClaims, TurnTokens
from test_agent_capability_control_sync import NAMED_RESOURCE, OWNER
from test_descriptor_agent_card_transaction import _current
from test_descriptor_agent_consent import POST, READ, _consent_world

KEY = b"t" * 32


async def _conversation(service, store, resident, conversation="c1", *, selection=None, card_service=None):
    slices = BundleStorageConversationSliceStore(store._root)
    service.bind_conversation_slices(slices, TurnTokens(KEY))
    if card_service is not None:
        # The production composition (cards/composition.py) passes the same store.
        compose_card_effects(card_service=card_service, card_store=store, grant_store=service._store,
                             policies=None, slices=slices)
    await slices.write(ConversationSlice(
        grantor_subject=OWNER, card_access_id=resident.access_id, conversation_id=conversation, card_generation=1,
        base_card_revision=resident.card_revision,
        selection=selection or {"named_service_operations": {NAMED_RESOURCE: {"slack": [READ]}}}), expected_revision=0)
    now = int(time.time())
    token = TurnTokens(KEY).issue(TurnClaims(resident.access_id, conversation, "turn-1", now, now + 600))
    return slices, token


def _consent(service, resident, token, **changes):
    return service.create_access(
        {"user_id": OWNER, "roles": ["kdcube:role:super-admin"]}, label="Focused consent",
        client_id=resident.client_id, merge_existing=True, expected_card_revision=resident.card_revision,
        resource_grants={NAMED_RESOURCE: ["named_services:use", "slack:post"]},
        resource_operations={NAMED_RESOURCE: []},
        named_service_operations={NAMED_RESOURCE: {"slack": [POST]}},
        request_id="conversation-consent-1", turn_token=token, **changes)


@pytest.mark.asyncio
async def test_t7_consent_in_a_conversation_commits_card_and_slice_together(tmp_path):
    service, store, decisions, resident, _, card_service, _ = await _consent_world(tmp_path)
    slices, token = await _conversation(service, store, resident, card_service=card_service)
    _, other_token = await _conversation(service, store, resident, "c2")

    answer = await _consent(service, resident, token)
    assert answer["ok"], answer
    assert decisions.decisions == ["committed"]
    saved = await _current(store, resident)
    assert set(saved.named_service_operations.operations[NAMED_RESOURCE]["slack"]) == {READ, POST}
    current = await slices.read(grantor_subject=OWNER, card_access_id=resident.access_id, conversation_id="c1")
    assert current.slice_revision == 2
    assert set(current.selection["named_service_operations"][NAMED_RESOURCE]["slack"]) == {READ, POST}
    assert await slices.prepared_changes(grantor_subject=OWNER, card_access_id=resident.access_id,
                                         conversation_id="c1") == []
    # Another conversation keeps its own choice.
    other = await slices.read(grantor_subject=OWNER, card_access_id=resident.access_id, conversation_id="c2")
    assert other.slice_revision == 1 and other.selection["named_service_operations"][NAMED_RESOURCE]["slack"] == [READ]


@pytest.mark.asyncio
async def test_a_consent_naming_a_foreign_or_unseeded_conversation_commits_nothing(tmp_path):
    service, store, decisions, resident, _, _, _ = await _consent_world(tmp_path)
    await _conversation(service, store, resident)

    forged = TurnTokens(b"x" * 32).issue(TurnClaims(resident.access_id, "c1", "turn-1", 0, int(time.time()) + 600))
    refused = await _consent(service, resident, forged)
    assert refused["ok"] is False and refused["error"] == "turn_token_invalid", refused

    now = int(time.time())
    unseeded = TurnTokens(KEY).issue(TurnClaims(resident.access_id, "never-seeded", "turn-1", now, now + 600))
    refused = await _consent(service, resident, unseeded)
    assert refused["ok"] is False and refused["error"] == "conversation_slice_missing", refused
    assert decisions.decisions == []
    assert (await _current(store, resident)).card_revision == resident.card_revision

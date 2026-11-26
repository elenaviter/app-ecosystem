"""The Hub computes the seed; the runtime names only the turn."""

from __future__ import annotations

import pytest

from connection_hub.contract import AGENT_CAPABILITY_TURN, LOCAL_ONLY_OPERATIONS
from connection_hub.delegated_credentials.slices.store import BundleStorageConversationSliceStore
from connection_hub.delegated_credentials.slices.turn_operation import seed_selection_of
from connection_hub.delegated_credentials.slices.turns import TurnTokens, card_generation_of
from test_agent_capability_control_sync import AGENT, APPLICATION, NAMED_RESOURCE, OWNER
from test_descriptor_agent_consent import READ, _consent_world
from test_turn_token import Registry

KEY = b"o" * 32


@pytest.mark.asyncio
async def test_the_turn_operation_seeds_from_the_card_and_answers_a_token(tmp_path):
    service, store, decisions, resident, _, _, _ = await _consent_world(tmp_path)
    slices = BundleStorageConversationSliceStore(store._root)
    service.bind_conversation_slices(slices, TurnTokens(KEY))
    service.bind_conversation_registry(Registry(owner=OWNER, application=APPLICATION))

    answer = await service.begin_agent_turn({"user_id": OWNER}, application=APPLICATION, agent_id=AGENT,
                                            conversation_id="c1", turn_id="t1")
    assert answer["ok"], answer
    claims = TurnTokens(KEY).verify(answer["turn_token"], card_access_id=resident.access_id, now=0,
                                    card_generation=card_generation_of(resident))
    assert claims.conversation_id == "c1"
    seeded = await slices.read(grantor_subject=OWNER, card_access_id=resident.access_id, conversation_id="c1")
    assert seeded.selection == seed_selection_of(resident)
    assert seeded.selection["named_service_operations"][NAMED_RESOURCE]["slack"] == [READ]
    # The runtime cannot pass a selection of its own.
    with pytest.raises(TypeError):
        await service.begin_agent_turn({"user_id": OWNER}, application=APPLICATION, agent_id=AGENT,
                                       conversation_id="c2", turn_id="t1", seed_selection={})


@pytest.mark.asyncio
async def test_the_turn_operation_refuses_without_its_parts_or_for_another_owner(tmp_path):
    service, store, decisions, resident, _, _, _ = await _consent_world(tmp_path)
    refused = await service.begin_agent_turn({"user_id": OWNER}, application=APPLICATION, agent_id=AGENT,
                                             conversation_id="c1", turn_id="t1")
    assert refused["error"] == "conversation_slices_unavailable"
    service.bind_conversation_slices(BundleStorageConversationSliceStore(store._root), TurnTokens(KEY))
    service.bind_conversation_registry(Registry(owner="someone-else", application=APPLICATION))
    refused = await service.begin_agent_turn({"user_id": OWNER}, application=APPLICATION, agent_id=AGENT,
                                             conversation_id="c1", turn_id="t1")
    assert refused["error"] == "conversation_not_owned"


def test_the_turn_operation_is_local_only():
    assert AGENT_CAPABILITY_TURN in LOCAL_ONLY_OPERATIONS

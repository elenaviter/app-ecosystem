"""The conversation picker's write."""

from __future__ import annotations

import pytest

from connection_hub.delegated_credentials.slices.store import BundleStorageConversationSliceStore
from connection_hub.delegated_credentials.slices.turns import TurnTokens
from test_agent_capability_control_sync import AGENT, APPLICATION, NAMED_RESOURCE, OWNER
from test_descriptor_agent_consent import POST, READ, _consent_world
from test_turn_token import EpochRegistry

KEY = b"p" * 32
USER = {"user_id": OWNER}
ONLY_READ = {"named_service_operations": {NAMED_RESOURCE: {"slack": [READ]}}}
NOTHING = {"named_service_operations": {NAMED_RESOURCE: {"slack": []}}}


async def _world(tmp_path):
    service, store, decisions, resident, _, _, _ = await _consent_world(tmp_path)
    slices = BundleStorageConversationSliceStore(store._root)
    service.bind_conversation_slices(slices, TurnTokens(KEY))
    registry = EpochRegistry(epoch=0)
    registry.owner, registry.application = OWNER, APPLICATION
    service.bind_conversation_registry(registry)
    return service, slices, resident


def _picker(**change):
    return dict(application=APPLICATION, agent_id=AGENT, conversation_id="c1", **change)


@pytest.mark.asyncio
async def test_a_picker_write_before_the_first_turn_seeds_then_replaces_only_this_conversation(tmp_path):
    service, slices, resident = await _world(tmp_path)
    answer = await service.update_conversation_selection(USER, **_picker(selection=NOTHING))
    assert answer["ok"] and answer["slice_revision"] == 2, answer  # seeded (1), then the pick (2)
    stale = await service.update_conversation_selection(USER, **_picker(selection=ONLY_READ, expected_slice_revision=1))
    assert stale["error"] == "conversation_slice_revision_conflict"
    refused = await service.update_conversation_selection(USER, **_picker(selection={"model": "x"}))
    assert refused["error"] == "conversation_slice_category_not_guarded"


@pytest.mark.asyncio
async def test_a_picker_write_in_a_conversation_created_again_under_its_id_starts_from_the_defaults(tmp_path):
    """The earlier conversation's choice is not inherited: another deletion epoch, another slice."""
    service, slices, resident = await _world(tmp_path)
    first = await service.update_conversation_selection(USER, **_picker(selection=NOTHING, conversation_epoch=0))
    assert first["ok"] and first["selection"] == NOTHING
    registry = service._conversation_registry
    registry.epoch = 5000
    again = await service.update_conversation_selection(USER, **_picker(selection=ONLY_READ, conversation_epoch=5000))
    assert again["ok"] and again["slice_revision"] == 4, again  # seeded (1), pick (2), reseeded (3), pick (4)
    current = await slices.read(grantor_subject=OWNER, card_access_id=resident.access_id, conversation_id="c1")
    assert current.conversation_epoch == 5000 and current.selection == ONLY_READ


@pytest.mark.asyncio
async def test_the_picker_may_send_kdcubes_capability_selection_and_the_hub_converts_it(tmp_path):
    from test_agent_capability_control_sync import _policy_with_named_operations

    service, slices, resident = await _world(tmp_path)
    answer = await service.update_conversation_selection(
        USER, **_picker(selected_capabilities=_policy_with_named_operations(READ)))
    assert answer["ok"], answer
    assert answer["selection"]["named_service_operations"][NAMED_RESOURCE]["slack"] == [READ]
    # A choice outside the shared Control's authority never enters the slice.
    wider = await service.update_conversation_selection(
        USER, **_picker(selected_capabilities=_policy_with_named_operations(READ, "object.action.delete_message")))
    assert wider["ok"], wider
    assert "object.action.delete_message" not in str(wider["selection"])
    missing = await service.update_conversation_selection(USER, **_picker())
    assert missing["error"] == "conversation_selection_required"

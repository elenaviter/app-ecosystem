"""W40: the picker write (T8) and the delete order (A1)."""

from __future__ import annotations

import pytest

from connection_hub.delegated_credentials.slices.store import BundleStorageConversationSliceStore, SLICE_STATE_DELETING
from connection_hub.delegated_credentials.slices.turns import TurnTokens
from test_agent_capability_control_sync import AGENT, APPLICATION, NAMED_RESOURCE, OWNER
from test_descriptor_agent_consent import POST, READ, _consent_world
from test_w40_turn_token import Registry

KEY = b"p" * 32
USER = {"user_id": OWNER}
ONLY_READ = {"named_service_operations": {NAMED_RESOURCE: {"slack": [READ]}}}
NOTHING = {"named_service_operations": {NAMED_RESOURCE: {"slack": []}}}


async def _world(tmp_path):
    service, store, decisions, resident, _, _, _ = await _consent_world(tmp_path)
    slices = BundleStorageConversationSliceStore(store._root)
    service.bind_conversation_slices(slices, TurnTokens(KEY))
    service.bind_conversation_registry(Registry(owner=OWNER, application=APPLICATION))
    return service, slices, resident


def _picker(**change):
    return dict(application=APPLICATION, agent_id=AGENT, conversation_id="c1", **change)


@pytest.mark.asyncio
async def test_t8_a_picker_write_before_the_first_turn_seeds_then_replaces_only_this_conversation(tmp_path):
    service, slices, resident = await _world(tmp_path)
    answer = await service.update_conversation_selection(USER, **_picker(selection=NOTHING))
    assert answer["ok"] and answer["slice_revision"] == 2, answer  # seeded (1), then the pick (2)
    stale = await service.update_conversation_selection(USER, **_picker(selection=ONLY_READ, expected_slice_revision=1))
    assert stale["error"] == "conversation_slice_revision_conflict"
    refused = await service.update_conversation_selection(USER, **_picker(selection={"model": "x"}))
    assert refused["error"] == "conversation_slice_category_not_guarded"


@pytest.mark.asyncio
async def test_a1_delete_marks_first_closes_the_conversation_and_removes_only_after(tmp_path):
    service, slices, resident = await _world(tmp_path)
    await service.begin_agent_turn(USER, application=APPLICATION, agent_id=AGENT, conversation_id="c1", turn_id="t1")
    early = await service.remove_conversation_slices(USER, conversation_id="c1")
    assert early["error"] == "conversation_not_marked_deleting"
    assert (await service.mark_conversation_deleting(USER, conversation_id="c1")) == {"ok": True, "marked": 1}
    current = await slices.read(grantor_subject=OWNER, card_access_id=resident.access_id, conversation_id="c1")
    assert current.state == SLICE_STATE_DELETING
    # Frozen: no token, no picker write, never reseeded.
    refused = await service.begin_agent_turn(USER, application=APPLICATION, agent_id=AGENT,
                                             conversation_id="c1", turn_id="t2")
    assert refused["error"] == "conversation_deleting"
    assert (await service.update_conversation_selection(USER, **_picker(selection=ONLY_READ)))["error"] == \
        "conversation_deleting"
    removed = await service.remove_conversation_slices(USER, conversation_id="c1")
    assert removed["ok"] and removed["removed_revisions"] == 2
    assert await slices.read(grantor_subject=OWNER, card_access_id=resident.access_id, conversation_id="c1") is None


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

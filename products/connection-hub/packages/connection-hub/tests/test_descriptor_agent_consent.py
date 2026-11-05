"""A focused resident consent and its saved descriptor selection are one revision."""

import pytest

from connection_hub.delegated_credentials.agent_capability_policy import AGENT_CAPABILITY_SELECTION_PROPERTY
from test_agent_capability_control_sync import (
    AGENT, APPLICATION, NAMED_RESOURCE, OWNER, _policy_with_named_operations, _service,
)
from test_descriptor_agent_card_transaction import _current, _world

READ = "object.list"
POST = "object.action.post_message"


def _sync_arguments(*, allow_post=True):
    operations = [READ, POST] if allow_post else [READ]
    return {
        "application": APPLICATION, "agent_id": AGENT, "descriptor_revision": "descriptor-r1",
        "descriptor_payload": {"revision": "descriptor-r1"},
        "capability_authority": _policy_with_named_operations(*operations),
        "resource_grants": {NAMED_RESOURCE: ["named_services:use", "slack:read", *(["slack:post"] if allow_post else [])]},
        "named_service_operations": {NAMED_RESOURCE: {"slack": operations}},
    }


async def _consent_world(tmp_path, *, allow_post=True):
    service, persistence = _service(named_services=True)
    answer = await service.sync_agent_capability_control(
        {"user_id": OWNER}, **_sync_arguments(allow_post=allow_post),
        selected_capabilities=_policy_with_named_operations(READ))
    assert answer["ok"], answer
    resident = persistence.records[answer["card"]["access_id"]][0]
    control = persistence.records[answer["control_card"]["access_id"]][0]
    return await _world(tmp_path, pair=(service, resident, control))


async def _consent(service, resident, *, merge=True, operation=POST):
    return await service.create_access(
        {"user_id": OWNER, "roles": ["kdcube:role:super-admin"]}, label="Focused consent",
        client_id=resident.client_id, merge_existing=merge, expected_card_revision=resident.card_revision,
        resource_grants={NAMED_RESOURCE: ["named_services:use", "slack:post" if operation == POST else "slack:read"]},
        resource_operations={NAMED_RESOURCE: []},
        named_service_operations={NAMED_RESOURCE: {"slack": [operation]}},
        request_id=f"focused-consent-{resident.card_revision}-{operation}")


def _selection(card):
    return card.properties[AGENT_CAPABILITY_SELECTION_PROPERTY]["capabilities"]["named_service_operations"]


@pytest.mark.asyncio
async def test_focused_consent_commits_selection_with_grants_and_survives_repeated_sync(tmp_path, monkeypatch):
    service, store, decisions, resident, _, _, _ = await _consent_world(tmp_path)

    async def mint(*args, **kwargs):
        raise AssertionError("a live resident consent keeps its bearer")

    monkeypatch.setattr(service, "_mint_card_credential", mint)
    answer = await _consent(service, resident)
    assert answer["ok"], answer
    saved = await _current(store, resident)
    assert saved.card_revision == resident.card_revision + 1
    assert set(saved.named_service_operations.operations[NAMED_RESOURCE]["slack"]) == {READ, POST}
    assert set(_selection(saved)) == {f"slack/{READ}", f"slack/{POST}"}
    assert decisions.decisions == ["committed"] and service._persistence.direct == []
    for _ in range(3):
        replay = await service.sync_agent_capability_control({"user_id": OWNER}, **_sync_arguments())
        assert replay["ok"], replay
        assert await _current(store, resident) == saved
    assert decisions.decisions == ["committed"]


@pytest.mark.asyncio
async def test_replace_consent_removes_the_saved_choice_and_sync_does_not_reintroduce_it(tmp_path):
    service, store, decisions, resident, _, _, _ = await _consent_world(tmp_path)
    assert (await _consent(service, resident))["ok"]
    saved = await _current(store, resident)
    answer = await _consent(service, saved, merge=False, operation=READ)
    assert answer["ok"], answer
    narrowed = await _current(store, resident)
    assert set(narrowed.named_service_operations.operations[NAMED_RESOURCE]["slack"]) == {READ}
    assert set(_selection(narrowed)) == {f"slack/{READ}"}
    replay = await service.sync_agent_capability_control({"user_id": OWNER}, **_sync_arguments())
    assert replay["ok"], replay
    assert await _current(store, resident) == narrowed
    assert decisions.decisions == ["committed", "committed"]


@pytest.mark.asyncio
async def test_consent_outside_descriptor_control_refuses_before_mint_or_commit(tmp_path, monkeypatch):
    service, store, decisions, resident, _, _, _ = await _consent_world(tmp_path, allow_post=False)

    async def mint(*args, **kwargs):
        raise AssertionError("an out-of-ceiling consent must not mint")

    monkeypatch.setattr(service, "_mint_card_credential", mint)
    answer = await _consent(service, resident)
    assert answer["ok"] is False and answer["error"] == "agent_capability_selection_outside_control"
    assert await _current(store, resident) == resident
    assert decisions.decisions == [] and service._persistence.direct == []

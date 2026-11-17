"""W38: one ceiling rule for named-service object.action (kdcube#270 D1, D4).

| descriptor for a namespace       | action ceiling                         |
| object.action only               | every action of the namespace in the Hub catalog |
| object.action + list             | every action in the Hub catalog        |
| list only                        | exactly the listed actions             |

The standard part and the capability ceiling of a descriptor Control are equal.
"""
import pytest

from connection_hub.delegated_credentials.agent_capability_policy import (
    AGENT_CAPABILITY_AUTHORITY_PROPERTY,
    AGENT_CAPABILITY_DEFAULTS_PROPERTY,
    AGENT_CAPABILITY_SELECTION_PROPERTY,
    CAPABILITY_ALLOWED_SELECTED,
)
from test_agent_capability_control_sync import (
    AGENT, APPLICATION, NAMED_RESOURCE, OWNER, _policy_with_named_operations, _service,
)

LIST = "object.list"
PARENT = "object.action"
POST = "object.action.post_message"


def _arguments(*declared: str):
    return {
        "application": APPLICATION, "agent_id": AGENT, "descriptor_revision": "descriptor-" + "-".join(declared),
        # The SDK sends what the bundle declares; Connection Hub resolves it against its catalog.
        "descriptor_payload": {
            "revision": "descriptor-" + "-".join(declared),
            "standard_authority": {"named_services": [{"namespace": "slack", "operations": list(declared)}]},
        },
        "capability_authority": _policy_with_named_operations(*declared),
    }


async def _sync(service, *declared: str, selected=None):
    return await service.sync_agent_capability_control(
        {"user_id": OWNER}, **_arguments(*declared),
        selected_capabilities=selected if selected is not None else _policy_with_named_operations(*declared))


def _keys(record, prop):
    return set(record.properties[prop]["capabilities"].get("named_service_operations", ()))


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", [(LIST, PARENT), (LIST, PARENT, POST)], ids=["parent-only", "parent-plus-list"])
async def test_a_parent_object_action_expands_the_capability_ceiling_to_every_catalog_action(declared):
    service, persistence = _service(named_services=True)
    answer = await _sync(service, *declared)
    assert answer["ok"], answer
    control = persistence.records[answer["control_card"]["access_id"]][0]
    standard = set(control.named_service_operations.operations[NAMED_RESOURCE]["slack"])
    assert POST in standard
    assert f"slack/{POST}" in _keys(control, AGENT_CAPABILITY_AUTHORITY_PROPERTY), "the ceiling equals the standard part"
    assert f"slack/{POST}" in _keys(control, AGENT_CAPABILITY_DEFAULTS_PROPERTY)
    # The parent stays allowed: the SDK reads it as the switch for the action call itself.
    assert f"slack/{PARENT}" in _keys(control, AGENT_CAPABILITY_AUTHORITY_PROPERTY)
    assert answer["states"]["named_service_operations"].get(f"slack/{PARENT}") == CAPABILITY_ALLOWED_SELECTED
    # D1a: the picker state of a concrete action is decided by the expanded ceiling.
    assert answer["states"]["named_service_operations"].get(f"slack/{POST}") == CAPABILITY_ALLOWED_SELECTED


@pytest.mark.asyncio
async def test_a_list_without_the_parent_is_exactly_the_listed_actions():
    service, persistence = _service(named_services=True)
    answer = await _sync(service, LIST, POST)
    assert answer["ok"], answer
    control = persistence.records[answer["control_card"]["access_id"]][0]
    assert f"slack/{POST}" in _keys(control, AGENT_CAPABILITY_AUTHORITY_PROPERTY)
    assert f"slack/{PARENT}" not in _keys(control, AGENT_CAPABILITY_AUTHORITY_PROPERTY)


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", [(LIST, PARENT), (LIST, PARENT, POST)], ids=["parent-only", "parent-plus-list"])
async def test_an_agent_card_editor_save_keeps_the_actions_of_a_parent_only_descriptor(tmp_path, declared):
    """D1c: aligning the base after a Save intersects the Card's concrete actions with the ceiling."""
    from test_descriptor_agent_card_transaction import _current, _world

    service, persistence = _service(named_services=True)
    answer = await _sync(service, *declared)
    assert answer["ok"], answer
    resident = persistence.records[answer["card"]["access_id"]][0]
    control = persistence.records[answer["control_card"]["access_id"]][0]
    assert POST in resident.named_service_operations.operations[NAMED_RESOURCE]["slack"]
    service, store, _, resident, _, _, _ = await _world(tmp_path, pair=(service, resident, control))
    saved = await service.create_access(
        {"user_id": OWNER, "roles": ["kdcube:role:super-admin"]}, label="Editor save",
        client_id=resident.client_id, merge_existing=False, expected_card_revision=resident.card_revision,
        resource_grants={NAMED_RESOURCE: list(resident.resource_grants[NAMED_RESOURCE])},
        resource_operations={NAMED_RESOURCE: list(resident.resource_operations[NAMED_RESOURCE])},
        named_service_operations={NAMED_RESOURCE: {"slack": sorted(resident.named_service_operations.operations[NAMED_RESOURCE]["slack"])}},
        request_id="w38-editor-save")
    assert saved["ok"], saved
    after = await _current(store, resident)
    assert f"slack/{POST}" in _keys(after, AGENT_CAPABILITY_SELECTION_PROPERTY), "the Save keeps the action in the base"
    replay = await _sync(service, *declared, selected=None)
    assert replay["ok"], replay
    synced = await _current(store, resident)
    assert POST in synced.named_service_operations.operations[NAMED_RESOURCE]["slack"], "and the next sync keeps it on the Card"


@pytest.mark.asyncio
async def test_a_props_override_from_an_older_sdk_does_not_narrow_the_control():
    """Only the stored Control Card narrows. Before W757 a Control save also wrote
    bundle props, and an SDK that still reads them marks the payload overridden and
    sends the narrower list: the descriptor's own standard part still counts."""
    service, persistence = _service(named_services=True)
    arguments = _arguments(LIST, PARENT)
    arguments["descriptor_payload"] = {**arguments["descriptor_payload"], "standard_authority_overridden": True}
    answer = await service.sync_agent_capability_control(
        {"user_id": OWNER}, **arguments,
        resource_grants={NAMED_RESOURCE: ["named_services:use", "slack:read"]},
        resource_operations={NAMED_RESOURCE: ["named_services_call"]},
        named_service_operations={NAMED_RESOURCE: {"slack": [LIST]}},
        selected_capabilities=_policy_with_named_operations(LIST, PARENT))
    assert answer["ok"], answer
    control = persistence.records[answer["control_card"]["access_id"]][0]
    assert set(control.named_service_operations.operations[NAMED_RESOURCE]["slack"]) == {LIST, POST}
    assert f"slack/{POST}" in _keys(control, AGENT_CAPABILITY_AUTHORITY_PROPERTY)


@pytest.mark.asyncio
async def test_a_human_edit_of_the_descriptor_control_narrows_its_capability_ceiling():
    """An administrator Save on the descriptor Control is an explicit list too."""
    service, persistence = _service(named_services=True)
    created = await _sync(service, LIST, PARENT)
    assert created["ok"], created
    control_id = created["control_card"]["access_id"]
    changed = await service.update_access(
        {"user_id": OWNER, "roles": ["kdcube:role:super-admin"]}, access_id=control_id,
        resource_grants={NAMED_RESOURCE: ["named_services:use", "slack:read"]}, resource_operations={},
        named_service_operations={NAMED_RESOURCE: {"slack": [LIST]}}, account_scope={},
        expected_card_revision=created["control_card"]["card_revision"],
        expected_catalog_version=created["control_card"]["catalog_version"],
        composition_mode="and", properties=created["control_card"]["properties"])
    assert changed["ok"], changed
    saved = persistence.records[control_id][0]
    assert f"slack/{POST}" not in _keys(saved, AGENT_CAPABILITY_AUTHORITY_PROPERTY), "right after the Save"
    refreshed = await service.sync_agent_capability_control(
        {"user_id": OWNER}, **{**_arguments(LIST, PARENT), "descriptor_revision": "descriptor-r2",
                               "descriptor_payload": {**_arguments(LIST, PARENT)["descriptor_payload"], "revision": "descriptor-r2"}})
    assert refreshed["ok"], refreshed
    control = persistence.records[control_id][0]
    assert set(control.named_service_operations.operations[NAMED_RESOURCE]["slack"]) == {LIST}
    assert f"slack/{POST}" not in _keys(control, AGENT_CAPABILITY_AUTHORITY_PROPERTY), "and after a descriptor refresh"


@pytest.mark.asyncio
async def test_a_props_override_does_not_intersect_an_administrator_save():
    """The administrator keeps both operations; an older SDK then sends a leftover
    props override listing only the action. The stored Save wins, and nothing is
    lost for good once the leftover goes away."""
    service, persistence = _service(named_services=True)
    created = await _sync(service, LIST, PARENT)
    control_id = created["control_card"]["access_id"]
    both = {NAMED_RESOURCE: {"slack": [LIST, POST]}}
    changed = await service.update_access(
        {"user_id": OWNER, "roles": ["kdcube:role:super-admin"]}, access_id=control_id,
        resource_grants={NAMED_RESOURCE: ["named_services:use", "slack:read", "slack:post"]},
        resource_operations={}, named_service_operations=both, account_scope={},
        expected_card_revision=created["control_card"]["card_revision"],
        expected_catalog_version=created["control_card"]["catalog_version"],
        composition_mode="and", properties=created["control_card"]["properties"])
    assert changed["ok"], changed
    stale = _arguments(LIST, PARENT)
    stale["descriptor_revision"] = "descriptor-with-props"
    stale["descriptor_payload"] = {**stale["descriptor_payload"], "revision": "descriptor-with-props",
                                   "standard_authority_overridden": True}
    answer = await service.sync_agent_capability_control(
        {"user_id": OWNER}, **stale,
        resource_grants={NAMED_RESOURCE: ["named_services:use", "slack:post"]},
        named_service_operations={NAMED_RESOURCE: {"slack": [POST]}})
    assert answer["ok"], answer
    control = persistence.records[control_id][0]
    assert set(control.named_service_operations.operations[NAMED_RESOURCE]["slack"]) == {LIST, POST}



HOST_FILE = "object.host_file"


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", [(LIST, PARENT, HOST_FILE), (LIST, PARENT, POST, HOST_FILE)], ids=["parent-only", "parent-plus-list"])
async def test_an_operation_outside_the_hub_catalog_stays_in_the_ceiling(declared):
    """Live W38 regression: the descriptor also allows an SDK-side operation the Hub
    catalog does not list (object.host_file). Bounding the ceiling to the standard part
    narrows only object.action keys, so the operation stays and the selection the SDK
    sends is not refused as agent_capability_selection_outside_control."""
    service, persistence = _service(named_services=True)
    answer = await _sync(service, *declared)
    assert answer["ok"], answer
    control = persistence.records[answer["control_card"]["access_id"]][0]
    assert HOST_FILE not in set(control.named_service_operations.operations[NAMED_RESOURCE]["slack"])
    ceiling = _keys(control, AGENT_CAPABILITY_AUTHORITY_PROPERTY)
    assert f"slack/{HOST_FILE}" in ceiling and f"slack/{POST}" in ceiling and f"slack/{PARENT}" in ceiling, ceiling
    replay = await _sync(service, *declared)
    assert replay["ok"], replay

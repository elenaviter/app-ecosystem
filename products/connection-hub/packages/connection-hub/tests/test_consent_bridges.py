"""A consent that adds a named-service operation adds the bridge tool too.

A passive sync never re-creates a bridge the owner removed, so a consent that
adds only the inner operation left the agent holding it with nothing to call it
with. The consent merge adds the bridges the requested operations need, when
the linked Control allows them, and nothing for operations it did not request
or that the Card already held.
"""
import pytest

from test_agent_capability_control_sync import (
    NAMED_RESOURCE, OWNER, _policy_with_named_operations, _service,
)
from test_descriptor_agent_card_transaction import _current, _world
from test_descriptor_agent_consent import POST, READ, _consent, _sync_arguments


async def _world_without_bridge(tmp_path, *, control_bridges=("named_services_call",), held=(READ,)):
    service, persistence = _service(named_services=True)
    arguments = {**_sync_arguments(), "resource_operations": {NAMED_RESOURCE: list(control_bridges)}}
    answer = await service.sync_agent_capability_control(
        {"user_id": OWNER}, **arguments,
        selected_capabilities=_policy_with_named_operations(*held),
        selected_resource_grants={NAMED_RESOURCE: ["named_services:use", "slack:read"]
                                  + (["slack:post"] if POST in held else [])},
        # The owner holds object.list and no bridge tool.
        selected_resource_operations={NAMED_RESOURCE: []},
        selected_named_service_operations={NAMED_RESOURCE: {"slack": list(held)}})
    assert answer["ok"], answer
    resident = persistence.records[answer["card"]["access_id"]][0]
    control = persistence.records[answer["control_card"]["access_id"]][0]
    assert "named_services_call" not in resident.resource_operations.get(NAMED_RESOURCE, ())
    return await _world(tmp_path, pair=(service, resident, control))


@pytest.mark.asyncio
async def test_a_consented_operation_brings_its_bridge(tmp_path):
    service, store, _decisions, resident, _, _, _ = await _world_without_bridge(tmp_path)
    answer = await _consent(service, resident)
    assert answer["ok"], answer
    saved = await _current(store, resident)
    assert POST in saved.named_service_operations.operations[NAMED_RESOURCE]["slack"]
    assert "named_services_call" in saved.resource_operations[NAMED_RESOURCE]


@pytest.mark.asyncio
async def test_a_bridge_the_control_does_not_allow_is_not_added(tmp_path):
    service, store, _decisions, resident, _, _, _ = await _world_without_bridge(tmp_path, control_bridges=())
    answer = await _consent(service, resident)
    assert answer["ok"], answer
    saved = await _current(store, resident)
    assert POST in saved.named_service_operations.operations[NAMED_RESOURCE]["slack"]
    assert "named_services_call" not in saved.resource_operations.get(NAMED_RESOURCE, ())


@pytest.mark.asyncio
async def test_an_operation_the_card_already_holds_brings_no_removed_bridge_back(tmp_path):
    """The review resends the Card's own operations; a bridge the owner removed beside one stays removed."""
    service, store, _decisions, resident, _, _, _ = await _world_without_bridge(tmp_path, held=(READ, POST))
    assert POST in resident.named_service_operations.operations[NAMED_RESOURCE]["slack"]
    answer = await _consent(service, resident, operation=POST)
    assert answer["ok"], answer
    saved = await _current(store, resident)
    assert "named_services_call" not in saved.resource_operations.get(NAMED_RESOURCE, ())

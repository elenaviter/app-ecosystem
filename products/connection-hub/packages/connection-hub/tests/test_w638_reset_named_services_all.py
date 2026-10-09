"""W638 Reset to Control when the Control's named-service selection is ALL (or legacy UNKNOWN).

claude-main 02:46Z: both Reset paths passed no named-service selection for an all/unknown Control, so
``reset_candidate`` skipped the update and the My Card's narrower entry for that service was kept
silently while its operations and grants followed the Control. "All" for one service is representable:
the Control's materialized boundary under the Control's grants for that service, frozen into an exact
entry, the rule an unrelated edit already applies (automation_access._inherited_selection). A legacy
UNKNOWN Control without a materialized boundary cannot be represented and refuses with a named code.
"""

from __future__ import annotations

import dataclasses

import pytest

from connection_hub.delegated_credentials.automation_access import card_authority_from_record
from connection_hub.delegated_credentials.caller_writer_gate import CallerWriteRefused
from connection_hub.delegated_credentials.cards.model import ControlCardBinding, NamedServiceSelection
from connection_hub.delegated_credentials.managed_card_reset import managed_card_reset_display
from test_resident_profile_cards import (
    CLIENT,
    MEMORIES,
    NAMED_SERVICES,
    NAMED_SERVICES_CONCRETE,
    USER,
    _connections_with_named_services,
    _Harness,
)

FROZEN_ALL = {"slack": ["object.search"]}  # the harness descriptor tree under named_services:use + slack:read


async def _world(tmp_path):
    h = _Harness(tmp_path, connections=_connections_with_named_services())
    created = await h.service.create_access(
        USER, label="lg-react",
        resource_grants={NAMED_SERVICES_CONCRETE: ["named_services:use", "slack:read"], MEMORIES: ["memories:read"]},
        resource_operations={MEMORIES: ["search"]},
        named_service_operations={NAMED_SERVICES_CONCRETE: {}},  # the My Card's narrower entry: nothing
        client_id=CLIENT)
    assert created["ok"], created
    access_id = created["access"]["access_id"]
    authority, handles = h.persistence.cards[access_id]
    bound = dataclasses.replace(authority, control_card=ControlCardBinding(
        control_id="control-person", issuer_ref="work:project:one", issuer_kind="project", control_revision=1))
    h.persistence.cards[access_id] = (bound, handles)
    existing = await h.service._load_record(access_id, grantor_subject=bound.grantor_subject)
    # The Control selects ALL: its materialized boundary is the descriptor tree of its catalog generation.
    full = await h.service.create_access(
        USER, label="all", resource_grants={NAMED_SERVICES_CONCRETE: ["named_services:use", "slack:read"]},
        named_service_operations="*", client_id="control-source")
    boundary = h.persistence.cards[full["access"]["access_id"]][0].named_services
    control = dataclasses.replace(
        existing, access_id="control-person", control_card=None,
        resource_grants={NAMED_SERVICES: ("named_services:use", "slack:read")}, resource_operations={NAMED_SERVICES: ()},
        named_service_operations=NamedServiceSelection.all(), named_services=boundary)
    return h, access_id, existing, control


@pytest.mark.asyncio
async def test_a_reset_to_an_all_control_carries_all_for_that_service(tmp_path):
    h, access_id, existing, control = await _world(tmp_path)

    async def compose_with_control(_record):
        return control, None

    h.service._compose_with_control = compose_with_control
    result = await h.service.reset_service_to_control(
        USER, access_id=access_id, resource=NAMED_SERVICES, expected_card_revision=existing.card_revision)
    assert result["ok"], result
    assert result["access"]["named_service_operations"] == {NAMED_SERVICES: FROZEN_ALL}
    assert result["access"]["resource_grants"][MEMORIES] == ["memories:read"]  # other services untouched


@pytest.mark.asyncio
async def test_the_displayed_reset_to_an_all_control_shows_all_for_that_service(tmp_path):
    _, access_id, existing, control = await _world(tmp_path)
    changes, display, _ = managed_card_reset_display(card_authority_from_record(existing),
                                                     card_authority_from_record(control), resource=NAMED_SERVICES)
    assert display["after"]["named_services"] == FROZEN_ALL
    assert changes["named_service_operations"] == {NAMED_SERVICES: FROZEN_ALL}


@pytest.mark.asyncio
async def test_a_legacy_unknown_control_without_a_boundary_refuses_rather_than_keep_the_old_entry(tmp_path):
    h, access_id, existing, control = await _world(tmp_path)
    unknown = dataclasses.replace(control, named_service_operations=NamedServiceSelection.unknown(), named_services={})

    async def compose_with_control(_record):
        return unknown, None

    h.service._compose_with_control = compose_with_control
    result = await h.service.reset_service_to_control(
        USER, access_id=access_id, resource=NAMED_SERVICES, expected_card_revision=existing.card_revision)
    assert not result["ok"] and result["error"] == "caller_writer_reset_control_named_services_unknown", result
    with pytest.raises(CallerWriteRefused, match="caller_writer_reset_control_named_services_unknown"):
        managed_card_reset_display(card_authority_from_record(existing), card_authority_from_record(unknown),
                                   resource=NAMED_SERVICES)


@pytest.mark.asyncio
async def test_an_all_control_carries_only_what_its_grants_for_the_service_authorize(tmp_path):
    """The frozen entry is filtered by the Control's own grants for the service: without slack:read the
    descriptor's object.search is not authorized, so "all" for that service is nothing."""
    _, _, existing, control = await _world(tmp_path)
    my = dataclasses.replace(existing, named_service_operations=NamedServiceSelection.exact({NAMED_SERVICES: FROZEN_ALL}),
                             resource_grants={**existing.resource_grants, NAMED_SERVICES: ("named_services:use",)})
    narrow = dataclasses.replace(control, resource_grants={NAMED_SERVICES: ("named_services:use",)})
    changes, display, _ = managed_card_reset_display(card_authority_from_record(my), card_authority_from_record(narrow),
                                                     resource=NAMED_SERVICES)
    assert display["before"]["named_services"] == FROZEN_ALL and display["after"]["named_services"] in (None, {})


# Second commit (claude-main 03:13Z): the two adjacent gaps, test-first.

async def _reset(h, access_id, existing, control):
    async def compose_with_control(_record):
        return control, None

    h.service._compose_with_control = compose_with_control
    return await h.service.reset_service_to_control(
        USER, access_id=access_id, resource=NAMED_SERVICES, expected_card_revision=existing.card_revision)


@pytest.mark.asyncio
async def test_gap_b_an_exact_control_without_an_entry_for_the_service_resets_it_to_none(tmp_path):
    h, access_id, existing, control = await _world(tmp_path)
    my = dataclasses.replace(existing, named_service_operations=NamedServiceSelection.exact({NAMED_SERVICES: FROZEN_ALL}))
    authority, handles = h.persistence.cards[access_id]
    h.persistence.cards[access_id] = (dataclasses.replace(authority, named_service_operations=my.named_service_operations),
                                      handles)
    existing = await h.service._load_record(access_id, grantor_subject=authority.grantor_subject)
    for selection in (NamedServiceSelection.exact({"https://elsewhere/*": {"slack": ["object.search"]}}),
                      NamedServiceSelection.none()):
        changes, display, _ = managed_card_reset_display(
            card_authority_from_record(existing),
            card_authority_from_record(dataclasses.replace(control, named_service_operations=selection)),
            resource=NAMED_SERVICES)
        assert display["before"]["named_services"] == FROZEN_ALL
        assert display["after"]["named_services"] in (None, {})


@pytest.mark.asyncio
async def test_gap_a_a_my_card_selecting_all_takes_the_controls_narrower_entry_for_that_service(tmp_path):
    h, access_id, existing, control = await _world(tmp_path)
    full = await h.service.create_access(
        USER, label="mine-all", resource_grants={NAMED_SERVICES_CONCRETE: ["named_services:use", "slack:read"]},
        named_service_operations="*", client_id="mine-all")
    boundary = h.persistence.cards[full["access"]["access_id"]][0].named_services
    elsewhere = "https://elsewhere/api/mcp/named-services*"
    my = dataclasses.replace(existing, named_service_operations=NamedServiceSelection.all(), named_services=boundary,
                             resource_grants={**existing.resource_grants, elsewhere: ("named_services:use", "slack:read")})
    narrower = dataclasses.replace(control, named_service_operations=NamedServiceSelection.exact({NAMED_SERVICES: {}}))
    changes, display, _ = managed_card_reset_display(card_authority_from_record(my), card_authority_from_record(narrower),
                                                     resource=NAMED_SERVICES)
    assert display["before"]["named_services"] == "*"
    assert display["after"]["named_services"] in (None, {})  # the Control's narrower entry for this service
    # Every OTHER service keeps exactly what "all" meant for it, frozen under its own grants.
    assert changes["named_service_operations"] == {elsewhere: FROZEN_ALL}


@pytest.mark.asyncio
async def test_gap_a_a_legacy_unknown_my_card_without_a_boundary_refuses(tmp_path):
    _, _, existing, control = await _world(tmp_path)
    my = dataclasses.replace(existing, named_service_operations=NamedServiceSelection.unknown(), named_services={})
    with pytest.raises(CallerWriteRefused, match="caller_writer_reset_named_services_unknown"):
        managed_card_reset_display(card_authority_from_record(my), card_authority_from_record(control),
                                   resource=NAMED_SERVICES)

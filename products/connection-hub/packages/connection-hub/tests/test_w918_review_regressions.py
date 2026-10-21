"""Independent H4 boundary checks for App Ecosystem PR867 at 1db7e123."""

import dataclasses

import pytest

from connection_hub.delegated_credentials.controls.project_mcp_family import (
    configure_project_resource_families,
    project_family_gate,
)
from test_w918_project_mcp_family import (
    CONNECTOR_A, CONNECTOR_B, CREATOR, FAMILY, agent, person_chain,
    project_control, resolve, resolve_person,
)


@pytest.fixture(autouse=True)
def family_configuration():
    configure_project_resource_families([FAMILY])
    yield
    configure_project_resource_families(None)


async def test_same_holder_implicit_binding_still_gets_the_project_gate():
    p = project_control("on")
    caller = agent(p)
    caller = dataclasses.replace(
        caller, grantor_subject=CREATOR, source="agent", delegate_subject="integration:review",
        control_card=dataclasses.replace(caller.control_card, holder_subject=""),
    )
    # Existing managed_target_kind explicitly resolves an empty holder to
    # the caller grantor for an agent owned by P's own holder.
    assert project_family_gate(caller, (p,)) is True
    assert CONNECTOR_A in (await resolve(caller, [p])).resource_grants


async def test_root_or_cannot_contribute_a_connector_when_gate_is_off():
    my, c, p, _ = person_chain("off", connectors=())
    p = dataclasses.replace(
        p, composition_mode="or",
        resource_grants={**p.resource_grants, CONNECTOR_B: ("external_mcp:use",)},
        resource_operations={**p.resource_operations, CONNECTOR_B: ("search",)},
    )
    effective = await resolve_person(my, c, p)
    assert CONNECTOR_B not in effective.resource_grants


async def test_pattern_miss_cannot_escape_gate_off_through_or_folding():
    configure_project_resource_families([
        {**FAMILY, "resource_patterns": [CONNECTOR_B]},
    ])
    my, c, p, _ = person_chain("off", connectors=(CONNECTOR_A,))
    c = dataclasses.replace(c, composition_mode="or")
    p = dataclasses.replace(p, composition_mode="or")
    effective = await resolve_person(my, c, p)
    assert CONNECTOR_A not in effective.resource_grants


async def test_same_holder_or_control_cannot_add_unselected_connector():
    p = project_control("on", mode="or", selection=(CONNECTOR_B,))
    p = dataclasses.replace(p, resource_grants={CONNECTOR_B: ("external_mcp:use",)})
    caller = agent(p, connectors=(CONNECTOR_A,))
    caller = dataclasses.replace(caller, grantor_subject=CREATOR, source="agent",
                                 delegate_subject="integration:review")
    effective = await resolve(caller, [p])
    assert CONNECTOR_A in effective.resource_grants
    assert CONNECTOR_B not in effective.resource_grants

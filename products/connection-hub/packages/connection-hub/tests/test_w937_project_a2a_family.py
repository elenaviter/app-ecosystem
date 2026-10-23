# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W937: a project's A2A switch over its members' own A2A connectors.

The same rule as W918's External MCP switch, read per family: on a validated
project chain a caller's own A2A connectors never fold; the project Control's
``a2a_family`` decides them, independently of ``external_mcp_family``.
Absent is off. This delivery reads the switch; writing it is a later one.
"""

from __future__ import annotations

import pytest

from connection_hub.delegated_credentials.cards.model import (
    CardAuthority, CardRecordError, ControlCardBinding, NamedServiceSelection,
)
from connection_hub.delegated_credentials.controls.model import control_card_id_for_issuer, new_credentialless_card
from connection_hub.delegated_credentials.controls.project_mcp_family import (
    A2A_FAMILY_PROPERTY, EXTERNAL_MCP_FAMILY_PROPERTY, ProjectFamilyConfigError, ProjectFamilyPolicy,
    ProjectResourceFamily,
)

PROJECT = "work:project:w937-test"
CREATOR = "user:creator"
OWNER = "user:owner"
PB = "https://board.example.test/mcp"
MCP = "urn:connection-hub:remote-mcp:mcp_aaaaaaaaaaaaaaaaaaaaaaaa"
A2A = "urn:connection-hub:remote-a2a:a2a_aaaaaaaaaaaaaaaaaaaaaaaa"
A2A_B = "urn:connection-hub:remote-a2a:a2a_bbbbbbbbbbbbbbbbbbbbbbbb"
MCP_FAMILY = {
    "id": "user_external_mcp", "resource_kinds": ["remote_mcp"],
    "resource_patterns": ["urn:connection-hub:remote-mcp:*"],
    "allowed_tools": ["*"], "max_resources": 8, "max_tools_per_resource": 64,
}
A2A_FAMILY = {
    "id": "user_external_a2a", "resource_kinds": ["remote_a2a"],
    "resource_patterns": ["urn:connection-hub:remote-a2a:*"],
    "allowed_tools": ["*"], "max_resources": 8, "max_tools_per_resource": 36,
}
BOTH = ProjectFamilyPolicy.from_declaration([MCP_FAMILY, A2A_FAMILY])


def project_control(*, mcp=None, a2a=None):
    properties = {}
    if mcp is not None:
        properties[EXTERNAL_MCP_FAMILY_PROPERTY] = mcp
    if a2a is not None:
        properties[A2A_FAMILY_PROPERTY] = a2a
    seed = CardAuthority(
        access_id="seed", client_id="seed", grantor_subject=CREATOR, delegate_subject="caller",
        source="manual", card_kind="automation",
        resource_grants={PB: ("work:use",)}, resource_operations={PB: ("plan.read",)},
        named_service_operations=NamedServiceSelection.exact({}), identity_scope="grantor", expires_at=2000000000,
    )
    return new_credentialless_card(
        grantor_subject=CREATOR, catalog_version="catalog",
        control_id=control_card_id_for_issuer("application", PROJECT, grantor_subject=CREATOR),
        issuer_ref=PROJECT, issuer_kind="application", initial_selection=seed,
        composition_mode="and", properties=properties, now=1,
    )


def agent(parent, *, resources=(MCP, A2A)):
    grants = {PB: ("work:use",)}
    operations = {PB: ("plan.read",)}
    for resource in resources:
        if resource.startswith("urn:connection-hub:remote-a2a:"):
            grants[resource] = ("external_a2a:use",)
            operations[resource] = ("send_answer", "get_task")
        else:
            grants[resource] = ("external_mcp:use",)
            operations[resource] = ("search",)
    return CardAuthority(
        access_id="agent-card", client_id="agent-client", grantor_subject=OWNER, delegate_subject="agent",
        source="manual", card_kind="agent", card_revision=3, resource_grants=grants,
        resource_operations=operations, named_service_operations=NamedServiceSelection.exact({}),
        identity_scope="grantor", created_at=1, expires_at=2000000000,
        control_card=ControlCardBinding(
            control_id=parent.access_id, issuer_ref=PROJECT, issuer_kind="application",
            control_revision=parent.card_revision, holder_subject=CREATOR,
        ),
    )


async def hierarchy(card, controls, *, families=BOTH):
    from connection_hub.delegated_credentials.controls.hierarchy import compose_control_hierarchy

    by_coordinate = {(c.grantor_subject, c.access_id): c for c in controls}

    async def load_control(control_id, *, grantor_subject):
        return by_coordinate.get((grantor_subject, control_id))

    return await compose_control_hierarchy(card, load_control=load_control, project_families=families)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mcp", "a2a", "mcp_in", "a2a_in"),
    [
        ("on", "on", True, True),
        ("on", None, True, False),
        ("on", "off", True, False),
        (None, "on", False, True),
        ("off", "on", False, True),
        (None, None, False, False),
    ],
)
async def test_each_connector_kind_follows_its_own_switch(mcp, a2a, mcp_in, a2a_in) -> None:
    p = project_control(mcp=mcp, a2a=a2a)
    result = await hierarchy(agent(p), [p])
    effective = result.effective_card

    assert (MCP in effective.resource_grants) is mcp_in
    assert (A2A in effective.resource_grants) is a2a_in
    assert effective.resource_grants[PB] == ("work:use",)
    assert result.external_mcp_family == ("on" if mcp_in else "off")
    assert result.a2a_family == ("on" if a2a_in else "off")
    if a2a_in:
        assert effective.resource_grants[A2A] == ("external_a2a:use",)
        assert effective.resource_operations[A2A] == ("get_task", "send_answer")


@pytest.mark.asyncio
async def test_without_the_a2a_family_declared_the_a2a_switch_stays_closed() -> None:
    p = project_control(mcp="on", a2a="on")
    result = await hierarchy(agent(p), [p], families=ProjectFamilyPolicy.from_declaration([MCP_FAMILY]))
    assert MCP in result.effective_card.resource_grants
    assert A2A not in result.effective_card.resource_grants
    assert result.a2a_family == "off"


@pytest.mark.asyncio
async def test_the_a2a_family_ceiling_bounds_the_callers_connectors() -> None:
    from connection_hub.delegated_credentials.controls.effective import ControlCardMismatch

    p = project_control(a2a="on")
    one = ProjectFamilyPolicy.from_declaration([MCP_FAMILY, {**A2A_FAMILY, "max_resources": 1}])
    with pytest.raises(ControlCardMismatch) as refused:
        await hierarchy(agent(p, resources=(A2A, A2A_B)), [p], families=one)
    assert refused.value.args[0] == "project_resource_family_resource_limit"


@pytest.mark.asyncio
async def test_off_a_project_chain_a2a_connectors_fold_as_any_resource() -> None:
    from dataclasses import replace

    unbound = replace(agent(project_control(a2a="on")), control_card=None)
    result = await hierarchy(unbound, [])
    assert A2A in result.effective_card.resource_grants
    assert result.a2a_family == "" and result.external_mcp_family == ""


def test_the_a2a_switch_is_a_control_only_on_or_off_value() -> None:
    with pytest.raises(CardRecordError) as invalid:
        project_control(a2a="yes")
    assert invalid.value.args[0] == "control_card_a2a_family_invalid"


@pytest.mark.parametrize(
    "raw",
    [
        {**A2A_FAMILY, "resource_patterns": ["urn:connection-hub:remote-mcp:*"]},
        {**A2A_FAMILY, "resource_kinds": ["remote_a2a", "remote_mcp"]},
        {**A2A_FAMILY, "resource_kinds": ["remote_other"]},
        {**MCP_FAMILY, "resource_patterns": ["urn:connection-hub:remote-a2a:*"]},
    ],
)
def test_a_family_must_name_one_known_kind_and_only_its_own_urns(raw) -> None:
    with pytest.raises(ProjectFamilyConfigError):
        ProjectResourceFamily.from_mapping(raw)


def test_a_family_matches_only_its_own_kind() -> None:
    a2a = ProjectResourceFamily.from_mapping(A2A_FAMILY)
    mcp = ProjectResourceFamily.from_mapping(MCP_FAMILY)
    assert a2a.matches(A2A) and not a2a.matches(MCP)
    assert mcp.matches(MCP) and not mcp.matches(A2A)
    assert not a2a.matches("urn:connection-hub:remote-a2a:not-an-id")
    assert ProjectResourceFamily.from_mapping({k: v for k, v in MCP_FAMILY.items() if k != "resource_kinds"}).resource_kind == "remote_mcp"


@pytest.mark.asyncio
async def test_a_family_declared_for_another_kind_never_opens_the_a2a_switch() -> None:
    # A deployment mistake: the A2A family id declared with the MCP kind and patterns.
    mislabelled = ProjectFamilyPolicy.from_declaration([{**MCP_FAMILY, "id": "user_external_a2a"}])
    p = project_control(mcp="off", a2a="on")
    result = await hierarchy(agent(p), [p], families=mislabelled)
    assert MCP not in result.effective_card.resource_grants
    assert A2A not in result.effective_card.resource_grants
    assert result.a2a_family == "off"

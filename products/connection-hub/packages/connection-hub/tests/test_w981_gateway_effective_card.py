# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W981: the delegated MCP gateway judges a linked agent by its Card and its Control.

The gateway's Card view (``effective_card_for_access_id``) is the authority the
platform guard composes: ``compose_control_hierarchy`` with the service's own
family policy, so the Control's rule and its External MCP and A2A switches
apply. An agent with no Control is judged by its own Card.
"""

from __future__ import annotations

import dataclasses

import pytest

from connection_hub.delegated_credentials.automation_access import (
    AutomationAccessService,
    card_authority_from_record,
    record_from_card,
)
from connection_hub.delegated_credentials.cards.model import (
    CardAuthority, ControlCardBinding, NamedServiceSelection,
)
from connection_hub.delegated_credentials.controls.effective import ControlCardMismatch
from connection_hub.delegated_credentials.controls.hierarchy import compose_control_hierarchy
from connection_hub.delegated_credentials.controls.model import control_card_id_for_issuer, new_credentialless_card
from connection_hub.delegated_credentials.controls.project_mcp_family import (
    A2A_FAMILY_PROPERTY, EXTERNAL_MCP_FAMILY_PROPERTY, ProjectFamilyPolicy,
)

ISSUER = "work:project:w981-test"
CREATOR = "user:creator"
OWNER = "user:owner"
PB = "https://board.example.test/mcp"
DOCS = "https://docs.example.test/mcp"
MCP = "urn:connection-hub:remote-mcp:mcp_aaaaaaaaaaaaaaaaaaaaaaaa"
A2A = "urn:connection-hub:remote-a2a:a2a_aaaaaaaaaaaaaaaaaaaaaaaa"
FAMILIES = ProjectFamilyPolicy.from_declaration([
    {"id": "user_external_mcp", "resource_kinds": ["remote_mcp"],
     "resource_patterns": ["urn:connection-hub:remote-mcp:*"],
     "allowed_tools": ["*"], "max_resources": 8, "max_tools_per_resource": 64},
    {"id": "user_external_a2a", "resource_kinds": ["remote_a2a"],
     "resource_patterns": ["urn:connection-hub:remote-a2a:*"],
     "allowed_tools": ["*"], "max_resources": 8, "max_tools_per_resource": 36},
])


def control(*, mode="and", mcp=None, a2a=None, selection=(PB,)):
    properties = {}
    if mcp is not None:
        properties[EXTERNAL_MCP_FAMILY_PROPERTY] = mcp
    if a2a is not None:
        properties[A2A_FAMILY_PROPERTY] = a2a
    seed = CardAuthority(
        access_id="seed", client_id="seed", grantor_subject=CREATOR, delegate_subject="caller",
        source="manual", card_kind="automation",
        resource_grants={r: ("work:use",) for r in selection},
        resource_operations={r: ("plan.read",) for r in selection},
        named_service_operations=NamedServiceSelection.exact({}), identity_scope="grantor", expires_at=2000000000,
    )
    return new_credentialless_card(
        grantor_subject=CREATOR, catalog_version="catalog",
        control_id=control_card_id_for_issuer("application", ISSUER, grantor_subject=CREATOR),
        issuer_ref=ISSUER, issuer_kind="application", initial_selection=seed,
        composition_mode=mode, properties=properties, now=1,
    )


def agent(parent=None, *, resources=(DOCS, MCP, A2A), owner=OWNER):
    grants = {PB: ("work:use",)}
    operations = {PB: ("plan.read",)}
    for resource in resources:
        if resource.startswith("urn:connection-hub:remote-a2a:"):
            grants[resource], operations[resource] = ("external_a2a:use",), ("send_answer",)
        elif resource.startswith("urn:connection-hub:remote-mcp:"):
            grants[resource], operations[resource] = ("external_mcp:use",), ("search",)
        else:
            grants[resource], operations[resource] = ("work:use",), ("plan.read",)
    binding = None if parent is None else ControlCardBinding(
        control_id=parent.access_id, issuer_ref=ISSUER, issuer_kind="application",
        control_revision=parent.card_revision, holder_subject=CREATOR,
    )
    return CardAuthority(
        access_id="agent-card", client_id="agent-client", grantor_subject=owner, delegate_subject="agent",
        source="manual", card_kind="agent", card_revision=3, resource_grants=grants,
        resource_operations=operations, named_service_operations=NamedServiceSelection.exact({}),
        identity_scope="grantor", created_at=1, expires_at=2000000000, control_card=binding,
    )


def service(*cards):
    """The access service with its stored Cards; the view returns the authority it was built from."""

    svc = AutomationAccessService.__new__(AutomationAccessService)
    svc._project_resource_families = FAMILIES
    stored = {(c.grantor_subject, c.access_id): record_from_card(c) for c in cards}

    async def load_record(access_id, *, grantor_subject):
        return stored.get((grantor_subject, access_id))

    async def card_view(record, **_kw):
        return card_authority_from_record(record)

    svc._load_record = load_record
    svc._card_view = card_view
    return svc


async def view(*cards, owner=OWNER):
    return await service(*cards).effective_card_for_access_id(grantor_subject=owner, access_id="agent-card")


async def guard(card, *controls):
    """What the platform guard composes for the same Cards."""

    by_coordinate = {(c.grantor_subject, c.access_id): c for c in controls}

    async def load_control(control_id, *, grantor_subject):
        return by_coordinate.get((grantor_subject, control_id))

    return (await compose_control_hierarchy(card, load_control=load_control,
                                            project_families=FAMILIES)).effective_card


async def test_and_a_resource_the_control_does_not_allow_is_not_in_the_view():
    p = control(mode="and")
    seen = await view(agent(p), p)
    assert PB in seen.resource_grants
    assert DOCS not in seen.resource_grants, "under AND the agent keeps only what the Control allows"


@pytest.mark.parametrize("mode", ["and", "or"])
@pytest.mark.parametrize(("mcp", "a2a"), [("on", "on"), ("off", "off"), ("on", "off"), ("off", "on"), (None, None)])
async def test_the_view_is_exactly_what_the_guard_composes(mode, mcp, a2a):
    # OR takes effect only for an agent of the Control's own holder, as the guard composes it.
    owner = CREATOR if mode == "or" else OWNER
    p = control(mode=mode, mcp=mcp, a2a=a2a, selection=(PB, DOCS))
    card = agent(p, owner=owner)
    seen = await view(card, p, owner=owner)
    judged = await guard(card, p)
    assert seen.resource_grants == judged.resource_grants
    assert seen.resource_operations == judged.resource_operations


async def test_or_keeps_what_either_card_allows():
    p = control(mode="or")
    seen = await view(agent(p, owner=CREATOR), p, owner=CREATOR)
    assert PB in seen.resource_grants and DOCS in seen.resource_grants


async def test_or_on_another_holders_agent_is_refused_as_the_guard_refuses_it():
    p = control(mode="or")
    with pytest.raises(ControlCardMismatch, match="control_card_foreign_holder_requires_and"):
        await view(agent(p), p)
    with pytest.raises(ControlCardMismatch, match="control_card_foreign_holder_requires_and"):
        await guard(agent(p), p)


@pytest.mark.parametrize("switch", ["on", "off", None])
async def test_the_external_mcp_switch_decides_the_agents_own_connectors(switch):
    p = control(mcp=switch)
    seen = await view(agent(p), p)
    assert (MCP in seen.resource_grants) is (switch == "on")


@pytest.mark.parametrize("switch", ["on", "off", None])
async def test_the_a2a_switch_decides_the_agents_own_a2a_connectors(switch):
    p = control(a2a=switch)
    seen = await view(agent(p), p)
    assert (A2A in seen.resource_grants) is (switch == "on")


async def test_an_unlinked_agent_is_judged_by_its_own_card():
    card = agent(None)
    seen = await view(card)
    assert seen.resource_grants == card.resource_grants
    assert seen.resource_operations == card.resource_operations


async def test_an_unresolvable_control_fails_closed():
    p = control()
    with pytest.raises(ControlCardMismatch):
        await view(agent(p))  # the Control is not stored


async def test_another_grantors_card_is_not_read():
    p = control()
    svc = service(agent(p), p)
    assert await svc.effective_card_for_access_id(grantor_subject="user:else", access_id="agent-card") is None


async def test_the_stored_card_view_is_still_the_raw_card():
    # The owner's own read model is unchanged: card_for_access_id keeps the stored Card.
    p = control(mcp="off")
    card = agent(p)
    raw = await service(card, p).card_for_access_id(grantor_subject=OWNER, access_id="agent-card")
    assert MCP in raw.resource_grants and DOCS in raw.resource_grants
    assert dataclasses.replace(raw, card_revision=card.card_revision).resource_grants == card.resource_grants


async def test_the_composed_view_keeps_the_stored_cards_identity():
    # The gateway still matches access id, client id and caller kind against the caller (review, push 1).
    p = control(mcp="off", a2a="off")
    card = agent(p)
    seen = await view(card, p)
    assert (seen.access_id, seen.client_id, seen.card_kind, seen.grantor_subject) == (
        card.access_id, card.client_id, card.card_kind, card.grantor_subject)

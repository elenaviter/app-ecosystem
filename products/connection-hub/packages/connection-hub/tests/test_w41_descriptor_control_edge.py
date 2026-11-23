"""W41 T2: an Agent Card reaches the shared descriptor Control only by its exact edge.

The holder and the Control id are computed from the binding's issuer_ref. A
binding that names another application's holder, or a non-deterministic id,
is refused at the guard, and the descriptor's resource families (the user's
own connectors) admit nothing on any edge but the exact one.
"""

from __future__ import annotations

import dataclasses

import pytest

from connection_hub.delegated_credentials.agent_capability_policy import (
    AGENT_CAPABILITY_AUTHORITY_PROPERTY,
    AGENT_CAPABILITY_METADATA_PROPERTY,
    AGENT_CAPABILITY_METADATA_SCHEMA,
    AGENT_CAPABILITY_SELECTION_PROPERTY,
    AGENT_DESCRIPTOR_CONTROL_PROPERTY,
    AgentCapabilityPolicy,
    AgentDescriptorControl,
)
from connection_hub.delegated_credentials.application_resources import application_resource
from connection_hub.delegated_credentials.cards.identity import CARD_KIND_AGENT, CARD_KIND_CONTROL
from connection_hub.delegated_credentials.cards.model import CardAuthority, ControlCardBinding, NamedServiceSelection
from connection_hub.delegated_credentials.controls.descriptor_control import (
    APPLICATION_AUTHORITY_PREFIX,
    descriptor_control_coordinates,
    descriptor_control_parent,
    descriptor_families_admitted,
)
from connection_hub.delegated_credentials.controls.effective import ControlCardMismatch, effective_card_authority
from connection_hub.delegated_credentials.controls.hierarchy import compose_control_hierarchy
from connection_hub.delegated_credentials.controls.snapshot import materialize_control_snapshot

TENANT, PROJECT = "tenant-a", "project-a"
AGENT = application_resource(tenant=TENANT, project=PROJECT, application="app-a@1-0", agent="worker")
OTHER_AGENT = application_resource(tenant=TENANT, project=PROJECT, application="app-a@1-0", agent="helper")
OTHER_APP_AGENT = application_resource(tenant=TENANT, project=PROJECT, application="app-b@1-0", agent="worker")
PROVIDER = "https://provider.example/mcp"
CONNECTOR = "urn:connection-hub:remote-mcp:mcp_user"
FAMILY = {
    "title": "My MCP connectors",
    "resource_patterns": ["urn:connection-hub:remote-mcp:*"],
    "allowed_tools": ["*"],
    "max_resources": 8,
    "max_tools_per_resource": 64,
}


def _policy(resource: str, **categories: tuple[str, ...]) -> AgentCapabilityPolicy:
    return AgentCapabilityPolicy(resource=resource, capabilities=categories)


def _control(resource: str, *, holder: str, control_id: str) -> CardAuthority:
    authority = _policy(resource, tools=("tool.a",), resource_families=("user_external_mcp",))
    control = CardAuthority(
        access_id=control_id,
        client_id="control-card:kdcube_agent_descriptor",
        grantor_subject=holder,
        delegate_subject="",
        source="control",
        card_kind=CARD_KIND_CONTROL,
        card_revision=3,
        catalog_version="catalog-v1",
        resource_grants={PROVIDER: ("records:read",)},
        resource_operations={PROVIDER: ("object.search",)},
        named_service_operations=NamedServiceSelection.none(),
        identity_scope="grantor",
        issuer_ref=resource,
        issuer_kind="kdcube_agent_descriptor",
        composition_mode="and",
        properties={
            AGENT_DESCRIPTOR_CONTROL_PROPERTY: AgentDescriptorControl(resource=resource).to_property(),
            AGENT_CAPABILITY_AUTHORITY_PROPERTY: authority.to_property(),
            AGENT_CAPABILITY_METADATA_PROPERTY: {
                "schema": AGENT_CAPABILITY_METADATA_SCHEMA,
                "resource": resource,
                "entries": {"resource_families": {"user_external_mcp": FAMILY}},
            },
        },
    )
    return materialize_control_snapshot(control, basis_catalog_version="catalog-v1", origin="agent_descriptor")


def _agent_card(resource: str, *, holder: str, control_id: str, owner: str = "user-1") -> CardAuthority:
    return CardAuthority(
        access_id=f"agent-{owner}",
        client_id="agent-worker",
        grantor_subject=owner,
        delegate_subject=f"integration:agent:{owner}",
        source="agent",
        card_kind=CARD_KIND_AGENT,
        card_revision=1,
        catalog_version="catalog-v1",
        resource_grants={PROVIDER: ("records:read",), CONNECTOR: ("remote_mcp:use",)},
        resource_operations={PROVIDER: ("object.search",), CONNECTOR: ("read",)},
        named_service_operations=NamedServiceSelection.none(),
        identity_scope="grantor",
        expires_at=4_000_000_000,
        control_card=ControlCardBinding(
            control_id=control_id, issuer_ref=resource, issuer_kind="kdcube_agent_descriptor",
            holder_subject=holder, control_revision=3),
        properties={
            AGENT_CAPABILITY_SELECTION_PROPERTY: _policy(
                resource, tools=("tool.a",), resource_families=("user_external_mcp",)).to_property(),
        },
    )


def _shared(resource: str = AGENT, owner: str = "user-1") -> tuple[CardAuthority, CardAuthority]:
    coordinates = descriptor_control_coordinates(resource)
    control = _control(resource, holder=coordinates.holder_subject, control_id=coordinates.control_id)
    card = _agent_card(resource, holder=coordinates.holder_subject, control_id=coordinates.control_id, owner=owner)
    return card, control


def test_coordinates_are_computed_from_the_agent_resource() -> None:
    worker, helper, other_app = (descriptor_control_coordinates(r) for r in (AGENT, OTHER_AGENT, OTHER_APP_AGENT))

    assert worker == descriptor_control_coordinates(AGENT)
    assert worker.holder_subject.startswith(APPLICATION_AUTHORITY_PREFIX)
    # One holder per application, one Control per agent.
    assert worker.holder_subject == helper.holder_subject != other_app.holder_subject
    assert len({worker.control_id, helper.control_id, other_app.control_id}) == 3


def test_every_user_composes_with_the_one_shared_control() -> None:
    for owner in ("user-1", "user-2"):
        card, control = _shared(owner=owner)

        effective = effective_card_authority(card, control)

        assert effective.grantor_subject == owner
        assert effective.resource_grants[PROVIDER] == ("records:read",)
        # The descriptor family admits the user's own connector on the exact edge.
        assert effective.resource_grants[CONNECTOR] == ("remote_mcp:use",)


@pytest.mark.asyncio
async def test_the_guard_walks_the_exact_edge_to_the_shared_control() -> None:
    card, control = _shared()

    async def load(access_id: str, *, grantor_subject: str) -> CardAuthority | None:
        if (grantor_subject, access_id) == (control.grantor_subject, control.access_id):
            return control
        return None

    hierarchy = await compose_control_hierarchy(card, load_control=load)

    assert hierarchy.control_card == control
    assert hierarchy.effective_card.resource_grants[CONNECTOR] == ("remote_mcp:use",)


def test_a_binding_to_another_applications_control_is_refused() -> None:
    card, _ = _shared()
    foreign = descriptor_control_coordinates(OTHER_APP_AGENT)
    foreign_control = _control(AGENT, holder=foreign.holder_subject, control_id=foreign.control_id)
    forged = dataclasses.replace(card, control_card=dataclasses.replace(
        card.control_card, holder_subject=foreign.holder_subject, control_id=foreign.control_id))

    assert descriptor_control_parent(forged) is None
    with pytest.raises(ControlCardMismatch, match="descriptor_control_edge_invalid"):
        effective_card_authority(forged, foreign_control)


def test_a_non_deterministic_control_id_under_the_right_holder_is_refused() -> None:
    card, _ = _shared()
    holder = card.control_card.holder_subject
    stray = _control(AGENT, holder=holder, control_id="control-chosen-by-caller")
    forged = dataclasses.replace(card, control_card=dataclasses.replace(card.control_card, control_id=stray.access_id))

    with pytest.raises(ControlCardMismatch, match="descriptor_control_edge_invalid"):
        effective_card_authority(forged, stray)


def test_a_person_held_descriptor_control_is_not_a_descriptor_edge() -> None:
    # Another person's own descriptor Control, named as holder: no exact edge,
    # so it is refused, and its families would admit nothing.
    card, _ = _shared()
    other_person = _control(AGENT, holder="user-2", control_id="control-user-2-copy")
    forged = dataclasses.replace(card, control_card=dataclasses.replace(
        card.control_card, holder_subject="user-2", control_id=other_person.access_id))

    assert not descriptor_families_admitted(forged, other_person)
    with pytest.raises(ControlCardMismatch, match="descriptor_control_edge_invalid"):
        effective_card_authority(forged, other_person)


def test_the_owners_own_per_user_copy_still_admits_its_families_until_migration() -> None:
    card, _ = _shared()
    own = _control(AGENT, holder="user-1", control_id="control-own-copy")
    legacy = dataclasses.replace(card, control_card=dataclasses.replace(
        card.control_card, holder_subject="", control_id=own.access_id))

    effective = effective_card_authority(legacy, own)

    assert effective.resource_grants[CONNECTOR] == ("remote_mcp:use",)

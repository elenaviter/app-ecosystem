from __future__ import annotations

import dataclasses

import pytest

from connection_hub.delegated_credentials.agent_capability_policy import (
    AGENT_CAPABILITY_AUTHORITY_PROPERTY,
    AGENT_CAPABILITY_METADATA_PROPERTY,
    AGENT_CAPABILITY_METADATA_SCHEMA,
    AGENT_CAPABILITY_PROJECTION_PROPERTY,
    AGENT_CAPABILITY_SELECTION_PROPERTY,
    AGENT_DESCRIPTOR_CONTROL_PROPERTY,
    CAPABILITY_ALLOWED_SELECTED,
    CAPABILITY_ALLOWED_UNSELECTED,
    CAPABILITY_NOT_ALLOWED,
    AgentCapabilityPolicy,
    AgentDescriptorControl,
    capability_states,
    replace_visible_selection,
)
from connection_hub.delegated_credentials.application_resources import (
    application_resource,
)
from connection_hub.delegated_credentials.cards.identity import (
    CARD_KIND_AGENT,
    CARD_KIND_CONTROL,
)
from connection_hub.delegated_credentials.cards.model import (
    CardAuthority,
    ControlCardBinding,
    NamedServiceSelection,
)
from connection_hub.delegated_credentials.controls.effective import (
    ControlCardMismatch,
    effective_card_authority,
)
from connection_hub.delegated_credentials.controls.snapshot import (
    materialize_control_snapshot,
)
from connection_hub.delegated_credentials.conversation_target_policy import (
    CONVERSATION_TARGETS_PROPERTY,
    conversation_targets,
)

AGENT_RESOURCE = application_resource(
    tenant="tenant-a",
    project="project-a",
    application="problem-board@1-0",
    agent="worker",
)
ALL_DEPLOYMENT_AGENTS = application_resource(
    tenant="tenant-a",
    project="project-a",
    application="*",
    agent="*",
)
PROVIDER_RESOURCE = "https://provider.example/mcp"


def _policy(**categories: tuple[str, ...]) -> AgentCapabilityPolicy:
    return AgentCapabilityPolicy(resource=AGENT_RESOURCE, capabilities=categories)


def test_picker_states_separate_ceiling_from_durable_selection() -> None:
    catalog = _policy(tools=("tool.old", "tool.new", "tool.blocked"))
    authority = _policy(tools=("tool.old", "tool.new"))
    selection = _policy(tools=("tool.old", "tool.blocked"))

    assert capability_states(
        catalog=catalog,
        authority=authority,
        selection=selection,
    ) == {
        "tools": {
            "tool.blocked": CAPABILITY_NOT_ALLOWED,
            "tool.new": CAPABILITY_ALLOWED_UNSELECTED,
            "tool.old": CAPABILITY_ALLOWED_SELECTED,
        }
    }


def test_visible_replacement_preserves_hidden_selection_for_later_restore() -> None:
    current = _policy(tools=("tool.old", "tool.hidden"))
    authority = _policy(tools=("tool.old", "tool.new"))
    requested = _policy(tools=("tool.new", "tool.outside"))

    replaced = replace_visible_selection(
        current=current,
        authority=authority,
        requested=requested,
    )

    assert replaced.capabilities == {"tools": ("tool.hidden", "tool.new")}


def _composition() -> tuple[CardAuthority, CardAuthority]:
    selection = _policy(
        tools=("tool.old", "tool.hidden"),
        conversation_targets=(ALL_DEPLOYMENT_AGENTS,),
    )
    authority = _policy(
        tools=("tool.old", "tool.new"),
        conversation_targets=(ALL_DEPLOYMENT_AGENTS,),
    )
    control = CardAuthority(
        access_id="control-agent-worker",
        client_id="control-card:kdcube_agent_descriptor",
        grantor_subject="user-1",
        delegate_subject="",
        source="control",
        card_kind=CARD_KIND_CONTROL,
        card_revision=2,
        catalog_version="catalog-v2",
        resource_grants={PROVIDER_RESOURCE: ("records:read",)},
        resource_operations={PROVIDER_RESOURCE: ("object.search",)},
        named_service_operations=NamedServiceSelection.none(),
        account_scope={},
        identity_scope="grantor",
        issuer_ref=AGENT_RESOURCE,
        issuer_kind="kdcube_agent_descriptor",
        composition_mode="and",
        properties={
            AGENT_DESCRIPTOR_CONTROL_PROPERTY: AgentDescriptorControl(
                resource=AGENT_RESOURCE
            ).to_property(),
            AGENT_CAPABILITY_AUTHORITY_PROPERTY: authority.to_property(),
            AGENT_CAPABILITY_METADATA_PROPERTY: {
                "schema": AGENT_CAPABILITY_METADATA_SCHEMA,
                "resource": AGENT_RESOURCE,
                "entries": {"tools": {"tool.old": {"title": "Old tool"}}},
            },
            CONVERSATION_TARGETS_PROPERTY: [ALL_DEPLOYMENT_AGENTS],
        },
    )
    control = materialize_control_snapshot(
        control,
        basis_catalog_version="catalog-v2",
        origin="agent_descriptor",
    )
    caller = CardAuthority(
        access_id="agent-user-worker",
        client_id="agent-worker",
        grantor_subject="user-1",
        delegate_subject="integration:agent:user-1",
        source="agent",
        card_kind=CARD_KIND_AGENT,
        card_revision=4,
        catalog_version="catalog-v1",
        resource_grants={PROVIDER_RESOURCE: ("records:read",)},
        resource_operations={PROVIDER_RESOURCE: ("object.search",)},
        named_service_operations=NamedServiceSelection.none(),
        account_scope={"records": {"account-1": ("records:read",)}},
        identity_scope="grantor",
        expires_at=4_000_000_000,
        control_card=ControlCardBinding(
            control_id=control.access_id,
            issuer_ref=control.issuer_ref,
            issuer_kind=control.issuer_kind,
            control_revision=1,
        ),
        properties={
            AGENT_CAPABILITY_SELECTION_PROPERTY: selection.to_property(),
            CONVERSATION_TARGETS_PROPERTY: [ALL_DEPLOYMENT_AGENTS],
        },
    )
    return caller, control


def test_descriptor_control_composes_live_capability_and_keeps_account_choice() -> None:
    caller, control = _composition()

    effective = effective_card_authority(caller, control)

    assert AgentCapabilityPolicy.from_property(
        effective.properties[AGENT_CAPABILITY_PROJECTION_PROPERTY]
    ).capabilities == {
        "conversation_targets": (ALL_DEPLOYMENT_AGENTS,),
        "tools": ("tool.old",),
    }
    assert AgentCapabilityPolicy.from_property(
        effective.properties[AGENT_CAPABILITY_SELECTION_PROPERTY]
    ).capabilities == {
        "conversation_targets": (ALL_DEPLOYMENT_AGENTS,),
        "tools": ("tool.hidden", "tool.old"),
    }
    assert AgentCapabilityPolicy.from_property(
        effective.properties[AGENT_CAPABILITY_AUTHORITY_PROPERTY]
    ).capabilities == {
        "conversation_targets": (ALL_DEPLOYMENT_AGENTS,),
        "tools": ("tool.new", "tool.old"),
    }
    assert effective.properties[AGENT_CAPABILITY_METADATA_PROPERTY]["entries"] == {
        "tools": {"tool.old": {"title": "Old tool"}}
    }
    assert effective.account_scope == {
        "records": {"account-1": ("records:read",)}
    }
    assert conversation_targets(effective.properties) == (ALL_DEPLOYMENT_AGENTS,)


def test_descriptor_control_cannot_contribute_authority_with_or() -> None:
    caller, control = _composition()
    control = dataclasses.replace(control, composition_mode="or")

    with pytest.raises(ControlCardMismatch, match="requires_and"):
        effective_card_authority(caller, control)

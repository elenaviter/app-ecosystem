# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Descriptor evidence and Card properties for resident-agent control."""

from __future__ import annotations

import copy
from typing import Any, Iterable, Mapping

from connection_hub.delegated_credentials.agent_capability_policy import (
    AGENT_CAPABILITY_AUTHORITY_PROPERTY,
    AGENT_CAPABILITY_METADATA_PROPERTY,
    AGENT_CAPABILITY_METADATA_SCHEMA,
    AGENT_CAPABILITY_PROJECTION_PROPERTY,
    AGENT_CAPABILITY_SELECTION_PROPERTY,
    AGENT_DESCRIPTOR_CONTROL_PROPERTY,
    AgentCapabilityPolicy,
    AgentCapabilityPolicyError,
    AgentDescriptorControl,
)
from connection_hub.delegated_credentials.catalog.descriptors import (
    ResourceAcceptance,
    canonical_digest,
)
from connection_hub.delegated_credentials.conversation_target_policy import (
    CONVERSATION_TARGETS_PROPERTY,
    conversation_targets,
)

AGENT_DESCRIPTOR_ISSUER_KIND = "kdcube_agent_descriptor"
AGENT_DESCRIPTOR_ACCEPTANCE_KIND = "kdcube_agent_descriptor"


def descriptor_control_properties(
    *,
    authority: AgentCapabilityPolicy,
    metadata: Mapping[str, Any] | None = None,
    targets: Iterable[str] = (),
) -> dict[str, Any]:
    """Build the structurally separate descriptor authority and metadata."""

    marker = AgentDescriptorControl(resource=authority.resource)
    raw_targets = list(targets)
    properties: dict[str, Any] = {
        AGENT_DESCRIPTOR_CONTROL_PROPERTY: marker.to_property(),
        AGENT_CAPABILITY_AUTHORITY_PROPERTY: authority.to_property(),
        CONVERSATION_TARGETS_PROPERTY: raw_targets,
    }
    normalized_targets = conversation_targets(properties)
    if raw_targets and not normalized_targets:
        raise AgentCapabilityPolicyError("agent_conversation_targets_invalid")
    properties[CONVERSATION_TARGETS_PROPERTY] = list(normalized_targets)

    if metadata is not None:
        if not isinstance(metadata, Mapping):
            raise AgentCapabilityPolicyError("agent_capability_metadata_invalid")
        value = copy.deepcopy(dict(metadata))
        if str(value.get("schema") or "").strip() != AGENT_CAPABILITY_METADATA_SCHEMA:
            raise AgentCapabilityPolicyError(
                "agent_capability_metadata_schema_mismatch"
            )
        if str(value.get("resource") or "").strip() != authority.resource:
            raise AgentCapabilityPolicyError(
                "agent_capability_metadata_resource_mismatch"
            )
        properties[AGENT_CAPABILITY_METADATA_PROPERTY] = value
    return properties


def descriptor_acceptance(
    *,
    authority: AgentCapabilityPolicy,
    descriptor_revision: str,
    descriptor_payload: Mapping[str, Any],
) -> ResourceAcceptance:
    """Evidence for the descriptor revision behind one capability boundary."""

    revision = str(descriptor_revision or "").strip()
    if not revision:
        raise AgentCapabilityPolicyError("agent_descriptor_revision_missing")
    payload = copy.deepcopy(dict(descriptor_payload))
    operations = {
        f"{category}:{capability}": canonical_digest(
            {
                "category": category,
                "capability": capability,
                "descriptor": payload,
            }
        )
        for category, capabilities in authority.capabilities.items()
        for capability in capabilities
    }
    return ResourceAcceptance(
        kind=AGENT_DESCRIPTOR_ACCEPTANCE_KIND,
        revision=revision,
        digest=canonical_digest(payload),
        grants=tuple(
            sorted(
                f"{category}:{capability}"
                for category, capabilities in authority.capabilities.items()
                for capability in capabilities
            )
        ),
        operations=operations,
        provider=AGENT_DESCRIPTOR_ISSUER_KIND,
    )


def preserve_descriptor_acceptance(
    current: Mapping[str, ResourceAcceptance] | None,
    computed: Mapping[str, ResourceAcceptance] | None,
) -> dict[str, ResourceAcceptance]:
    """Keep synthetic descriptor evidence outside catalog resource rewrites."""

    result = dict(computed or {})
    for resource, acceptance in dict(current or {}).items():
        if (
            acceptance.kind == AGENT_DESCRIPTOR_ACCEPTANCE_KIND
            and resource not in result
        ):
            result[resource] = acceptance
    return result


def resident_selection_properties(
    properties: Mapping[str, Any] | None,
    *,
    selection: AgentCapabilityPolicy,
) -> dict[str, Any]:
    """Store only the user selection on the resident Card."""

    result = copy.deepcopy(dict(properties or {}))
    for name in (
        AGENT_CAPABILITY_AUTHORITY_PROPERTY,
        AGENT_CAPABILITY_METADATA_PROPERTY,
        AGENT_CAPABILITY_PROJECTION_PROPERTY,
        AGENT_DESCRIPTOR_CONTROL_PROPERTY,
    ):
        result.pop(name, None)
    result[AGENT_CAPABILITY_SELECTION_PROPERTY] = selection.to_property()
    return result


__all__ = [
    "AGENT_DESCRIPTOR_ACCEPTANCE_KIND",
    "AGENT_DESCRIPTOR_ISSUER_KIND",
    "descriptor_acceptance",
    "descriptor_control_properties",
    "preserve_descriptor_acceptance",
    "resident_selection_properties",
]

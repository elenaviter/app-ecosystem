# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Typed descriptor ceilings and user selections for one KDCube agent."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from connection_hub.delegated_credentials.application_resources import (
    ApplicationResource,
    ApplicationResourceError,
)

AGENT_CAPABILITY_POLICY_SCHEMA = "connection_hub.agent_capability_policy.v1"
AGENT_CAPABILITY_METADATA_SCHEMA = "connection_hub.agent_capability_metadata.v1"
AGENT_DESCRIPTOR_CONTROL_SCHEMA = "connection_hub.agent_descriptor_control.v1"

AGENT_CAPABILITY_AUTHORITY_PROPERTY = "kdcube.agent_capability_authority"
AGENT_CAPABILITY_SELECTION_PROPERTY = "kdcube.agent_capability_selection"
AGENT_CAPABILITY_METADATA_PROPERTY = "kdcube.agent_capability_metadata"
AGENT_CAPABILITY_PROJECTION_PROPERTY = "kdcube.agent_capability_projection"
AGENT_DESCRIPTOR_CONTROL_PROPERTY = "kdcube.agent_descriptor_control"

CAPABILITY_ALLOWED_SELECTED = "allowed_selected"
CAPABILITY_ALLOWED_UNSELECTED = "allowed_unselected"
CAPABILITY_NOT_ALLOWED = "not_allowed"

DESCRIPTOR_CONTROL_DIMENSIONS = (
    "agent_capabilities",
    "conversation_targets",
    "named_services",
    "resource_grants",
    "resource_operations",
)

_CATEGORY_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")


class AgentCapabilityPolicyError(ValueError):
    """A descriptor capability property cannot be trusted as authority."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _capability(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text != value or "\x00" in text or len(text) > 4096:
        raise AgentCapabilityPolicyError("agent_capability_invalid")
    return text


def _resource(value: Any) -> str:
    try:
        parsed = ApplicationResource.parse(str(value or ""))
    except ApplicationResourceError as exc:
        raise AgentCapabilityPolicyError("agent_capability_resource_invalid") from exc
    if not parsed.exact:
        raise AgentCapabilityPolicyError("agent_capability_resource_not_exact")
    return parsed.resource


@dataclass(frozen=True)
class AgentCapabilityPolicy:
    """A finite set of selectable capability identities for one agent."""

    resource: str
    capabilities: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized: dict[str, tuple[str, ...]] = {}
        for raw_category, raw_values in dict(self.capabilities or {}).items():
            category = str(raw_category or "").strip().lower()
            if not _CATEGORY_RE.fullmatch(category):
                raise AgentCapabilityPolicyError("agent_capability_category_invalid")
            if not isinstance(raw_values, (list, tuple, set, frozenset)):
                raise AgentCapabilityPolicyError("agent_capability_values_invalid")
            normalized[category] = tuple(
                sorted({_capability(value) for value in raw_values})
            )
        object.__setattr__(self, "resource", _resource(self.resource))
        object.__setattr__(self, "capabilities", normalized)

    @classmethod
    def empty(cls, resource: str) -> "AgentCapabilityPolicy":
        return cls(resource=resource)

    @classmethod
    def from_property(cls, value: Any) -> "AgentCapabilityPolicy":
        if not isinstance(value, Mapping):
            raise AgentCapabilityPolicyError("agent_capability_policy_invalid")
        if str(value.get("schema") or "").strip() != AGENT_CAPABILITY_POLICY_SCHEMA:
            raise AgentCapabilityPolicyError("agent_capability_policy_schema_mismatch")
        if set(value) - {"schema", "resource", "capabilities"}:
            raise AgentCapabilityPolicyError("agent_capability_policy_field_invalid")
        capabilities = value.get("capabilities")
        if not isinstance(capabilities, Mapping):
            raise AgentCapabilityPolicyError("agent_capability_policy_capabilities_invalid")
        if any(not isinstance(values, (list, tuple)) for values in capabilities.values()):
            raise AgentCapabilityPolicyError("agent_capability_values_invalid")
        return cls(
            resource=str(value.get("resource") or ""),
            capabilities={
                str(category): tuple(values)
                for category, values in capabilities.items()
            },
        )

    def to_property(self) -> dict[str, Any]:
        return {
            "schema": AGENT_CAPABILITY_POLICY_SCHEMA,
            "resource": self.resource,
            "capabilities": {
                category: list(values)
                for category, values in sorted(self.capabilities.items())
            },
        }

    def contains(self, category: str, capability: str) -> bool:
        return capability in self.capabilities.get(category, ())

    def intersection(self, other: "AgentCapabilityPolicy") -> "AgentCapabilityPolicy":
        self._same_resource(other)
        return AgentCapabilityPolicy(
            resource=self.resource,
            capabilities={
                category: tuple(
                    sorted(
                        set(self.capabilities.get(category, ()))
                        & set(other.capabilities.get(category, ()))
                    )
                )
                for category in set(self.capabilities) | set(other.capabilities)
            },
        )

    def union(self, other: "AgentCapabilityPolicy") -> "AgentCapabilityPolicy":
        self._same_resource(other)
        return AgentCapabilityPolicy(
            resource=self.resource,
            capabilities={
                category: tuple(
                    sorted(
                        set(self.capabilities.get(category, ()))
                        | set(other.capabilities.get(category, ()))
                    )
                )
                for category in set(self.capabilities) | set(other.capabilities)
            },
        )

    def subtract(self, other: "AgentCapabilityPolicy") -> "AgentCapabilityPolicy":
        self._same_resource(other)
        return AgentCapabilityPolicy(
            resource=self.resource,
            capabilities={
                category: tuple(
                    sorted(
                        set(self.capabilities.get(category, ()))
                        - set(other.capabilities.get(category, ()))
                    )
                )
                for category in self.capabilities
            },
        )

    def _same_resource(self, other: "AgentCapabilityPolicy") -> None:
        if self.resource != other.resource:
            raise AgentCapabilityPolicyError("agent_capability_resource_mismatch")


@dataclass(frozen=True)
class AgentDescriptorControl:
    """Marker distinguishing live descriptor control from frozen snapshots."""

    resource: str
    controlled_dimensions: tuple[str, ...] = DESCRIPTOR_CONTROL_DIMENSIONS

    def __post_init__(self) -> None:
        dimensions = tuple(sorted({str(value or "").strip() for value in self.controlled_dimensions}))
        if dimensions != tuple(sorted(DESCRIPTOR_CONTROL_DIMENSIONS)):
            raise AgentCapabilityPolicyError("agent_descriptor_control_dimensions_invalid")
        object.__setattr__(self, "resource", _resource(self.resource))
        object.__setattr__(self, "controlled_dimensions", dimensions)

    @classmethod
    def from_property(cls, value: Any) -> "AgentDescriptorControl":
        if not isinstance(value, Mapping):
            raise AgentCapabilityPolicyError("agent_descriptor_control_invalid")
        if str(value.get("schema") or "").strip() != AGENT_DESCRIPTOR_CONTROL_SCHEMA:
            raise AgentCapabilityPolicyError("agent_descriptor_control_schema_mismatch")
        if set(value) - {"schema", "resource", "controlled_dimensions"}:
            raise AgentCapabilityPolicyError("agent_descriptor_control_field_invalid")
        dimensions = value.get("controlled_dimensions")
        if not isinstance(dimensions, (list, tuple)):
            raise AgentCapabilityPolicyError("agent_descriptor_control_dimensions_invalid")
        return cls(
            resource=str(value.get("resource") or ""),
            controlled_dimensions=tuple(str(item) for item in dimensions),
        )

    def to_property(self) -> dict[str, Any]:
        return {
            "schema": AGENT_DESCRIPTOR_CONTROL_SCHEMA,
            "resource": self.resource,
            "controlled_dimensions": list(self.controlled_dimensions),
        }


def descriptor_control(
    properties: Mapping[str, Any] | None,
) -> AgentDescriptorControl | None:
    raw = (properties or {}).get(AGENT_DESCRIPTOR_CONTROL_PROPERTY)
    if raw is None:
        return None
    return AgentDescriptorControl.from_property(raw)


def capability_states(
    *,
    catalog: AgentCapabilityPolicy,
    authority: AgentCapabilityPolicy,
    selection: AgentCapabilityPolicy,
) -> dict[str, dict[str, str]]:
    catalog._same_resource(authority)
    catalog._same_resource(selection)
    return {
        category: {
            capability: (
                CAPABILITY_NOT_ALLOWED
                if not authority.contains(category, capability)
                else (
                    CAPABILITY_ALLOWED_SELECTED
                    if selection.contains(category, capability)
                    else CAPABILITY_ALLOWED_UNSELECTED
                )
            )
            for capability in values
        }
        for category, values in catalog.capabilities.items()
    }


def replace_visible_selection(
    *,
    current: AgentCapabilityPolicy,
    authority: AgentCapabilityPolicy,
    requested: AgentCapabilityPolicy,
) -> AgentCapabilityPolicy:
    """Replace visible choices while retaining grants hidden by the ceiling."""
    current._same_resource(authority)
    current._same_resource(requested)
    hidden = current.subtract(authority)
    return hidden.union(requested.intersection(authority))


def compose_agent_capability_properties(
    caller: Mapping[str, Any] | None,
    control: Mapping[str, Any] | None,
    *,
    mode: str,
) -> dict[str, Any]:
    """Compose the descriptor ceiling with the caller's durable selection."""
    marker = descriptor_control(control)
    if marker is None:
        return {}
    if mode != "and":
        raise AgentCapabilityPolicyError("agent_descriptor_control_requires_and")
    raw_authority = (control or {}).get(AGENT_CAPABILITY_AUTHORITY_PROPERTY)
    if raw_authority is None:
        raise AgentCapabilityPolicyError("agent_capability_authority_missing")
    authority = AgentCapabilityPolicy.from_property(raw_authority)
    if authority.resource != marker.resource:
        raise AgentCapabilityPolicyError("agent_capability_resource_mismatch")

    raw_selection = (caller or {}).get(AGENT_CAPABILITY_SELECTION_PROPERTY)
    selection = (
        AgentCapabilityPolicy.from_property(raw_selection)
        if raw_selection is not None
        else AgentCapabilityPolicy.empty(marker.resource)
    )
    projection = selection.intersection(authority)
    result: dict[str, Any] = {
        AGENT_DESCRIPTOR_CONTROL_PROPERTY: marker.to_property(),
        AGENT_CAPABILITY_AUTHORITY_PROPERTY: authority.to_property(),
        AGENT_CAPABILITY_SELECTION_PROPERTY: selection.to_property(),
        AGENT_CAPABILITY_PROJECTION_PROPERTY: projection.to_property(),
    }
    metadata = (control or {}).get(AGENT_CAPABILITY_METADATA_PROPERTY)
    if metadata is not None:
        if not isinstance(metadata, Mapping):
            raise AgentCapabilityPolicyError("agent_capability_metadata_invalid")
        if str(metadata.get("schema") or "").strip() != AGENT_CAPABILITY_METADATA_SCHEMA:
            raise AgentCapabilityPolicyError("agent_capability_metadata_schema_mismatch")
        if str(metadata.get("resource") or "").strip() != marker.resource:
            raise AgentCapabilityPolicyError("agent_capability_metadata_resource_mismatch")
        result[AGENT_CAPABILITY_METADATA_PROPERTY] = copy.deepcopy(dict(metadata))
    return result


__all__ = [
    "AGENT_CAPABILITY_AUTHORITY_PROPERTY",
    "AGENT_CAPABILITY_METADATA_PROPERTY",
    "AGENT_CAPABILITY_METADATA_SCHEMA",
    "AGENT_CAPABILITY_POLICY_SCHEMA",
    "AGENT_CAPABILITY_PROJECTION_PROPERTY",
    "AGENT_CAPABILITY_SELECTION_PROPERTY",
    "AGENT_DESCRIPTOR_CONTROL_PROPERTY",
    "AGENT_DESCRIPTOR_CONTROL_SCHEMA",
    "CAPABILITY_ALLOWED_SELECTED",
    "CAPABILITY_ALLOWED_UNSELECTED",
    "CAPABILITY_NOT_ALLOWED",
    "DESCRIPTOR_CONTROL_DIMENSIONS",
    "AgentCapabilityPolicy",
    "AgentCapabilityPolicyError",
    "AgentDescriptorControl",
    "capability_states",
    "compose_agent_capability_properties",
    "descriptor_control",
    "replace_visible_selection",
]

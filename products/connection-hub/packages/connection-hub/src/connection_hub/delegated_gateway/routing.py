# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Qualified route construction and accepted/current tool intersection."""

from __future__ import annotations

from collections.abc import Sequence

from connection_hub.delegated_gateway.models import (
    DESCRIPTOR_CURRENT,
    RESOURCE_ACTIVE,
    DelegatedCardView,
    DelegatedGatewayError,
    DelegatedResourceEntry,
    GatewayContractError,
    GatewayToolRoute,
    ProviderDescriptor,
    ProviderTool,
)
from connection_hub.delegated_gateway.naming import QualifiedToolNameIndex
from connection_hub.delegated_gateway.ports import DelegatedMCPResourceProvider
from connection_hub.delegated_gateway.registry import DelegatedMCPProviderRegistry


def route_for(
    resource: DelegatedResourceEntry,
    provider: DelegatedMCPResourceProvider,
    operation: str,
) -> GatewayToolRoute:
    return GatewayToolRoute(
        resource_id=resource.resource_id,
        resource_kind=resource.kind,
        operation=operation,
        accepted_descriptor_identity=(
            resource.accepted_descriptor.operation_identity(operation)
        ),
        provider_id=str(provider.provider_id),
    )


def accepted_route_index(
    card: DelegatedCardView,
    providers: DelegatedMCPProviderRegistry,
) -> QualifiedToolNameIndex:
    routes: list[GatewayToolRoute] = []
    for resource in card.resources:
        if resource.state != RESOURCE_ACTIVE:
            continue
        try:
            provider = providers.provider_for(resource)
        except DelegatedGatewayError:
            continue
        routes.extend(
            route_for(resource, provider, operation)
            for operation in resource.operations
        )
    try:
        return QualifiedToolNameIndex(routes)
    except GatewayContractError:
        raise DelegatedGatewayError(
            "qualified_tool_name_collision",
            access_id=card.access_id,
            card_revision=card.card_revision,
        ) from None


def current_tool_map(
    *,
    resource: DelegatedResourceEntry,
    descriptor: ProviderDescriptor,
    tools: Sequence[ProviderTool],
) -> dict[str, ProviderTool]:
    if descriptor.resource_id != resource.resource_id:
        raise GatewayContractError("provider_resource_mismatch")
    if not descriptor.available:
        return {}
    current: dict[str, ProviderTool] = {}
    for tool in tools:
        if tool.operation in current:
            raise GatewayContractError("provider_tool_duplicate")
        descriptor_digest = descriptor.operation_digests.get(tool.operation)
        accepted_digest = resource.accepted_descriptor.operation_digests.get(
            tool.operation
        )
        if (
            tool.operation in resource.operations
            and descriptor_digest
            and descriptor_digest == tool.descriptor_digest
            and descriptor_digest == accepted_digest
        ):
            current[tool.operation] = tool
    return current


def operation_unavailable_reason(
    *,
    resource: DelegatedResourceEntry,
    descriptor: ProviderDescriptor,
    operation: str,
) -> str:
    if descriptor.unavailable_reason:
        return descriptor.unavailable_reason
    if descriptor.state != DESCRIPTOR_CURRENT:
        return {
            "changed": "descriptor_changed",
            "missing": "descriptor_missing",
            "disabled": "resource_disabled",
        }.get(descriptor.state, "descriptor_not_current")
    current = descriptor.operation_digests.get(operation)
    if current is None:
        return "operation_removed_by_provider"
    if current != resource.accepted_descriptor.operation_digests.get(operation):
        return "operation_descriptor_changed"
    return "operation_not_current"


__all__ = [
    "accepted_route_index",
    "current_tool_map",
    "operation_unavailable_reason",
    "route_for",
]

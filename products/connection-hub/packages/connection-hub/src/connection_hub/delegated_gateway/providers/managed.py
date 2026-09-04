# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Host-port provider for managed KDCube MCP-compatible resources."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from connection_hub.delegated_gateway.models import (
    GatewayContractError,
    ProviderCallAdmission,
    ProviderCallResult,
    ProviderDescriptor,
    ProviderTool,
)
from connection_hub.delegated_gateway.ports import (
    GatewayProviderContext,
    ManagedKDCubeMCPHost,
)

MANAGED_KDCUBE_MCP_PROVIDER_ID = "managed_kdcube_mcp"


class ManagedKDCubeMCPProvider:
    """Route cataloged managed surfaces through an injected trusted host.

    The host implementation retains the managed surface's own admission
    boundary. This portable adapter neither imports KDCube nor performs HTTP
    recursion through public ingress.
    """

    def __init__(
        self,
        host: ManagedKDCubeMCPHost,
        *,
        resource_kinds: Iterable[str] = ("managed_kdcube_mcp",),
    ) -> None:
        kinds = frozenset(str(value or "").strip().lower() for value in resource_kinds)
        if not kinds or "" in kinds:
            raise GatewayContractError("managed_resource_kinds_invalid")
        self._host = host
        self._resource_kinds = kinds

    @property
    def provider_id(self) -> str:
        return MANAGED_KDCUBE_MCP_PROVIDER_ID

    @property
    def resource_kinds(self) -> frozenset[str]:
        return self._resource_kinds

    async def current_descriptor(
        self, context: GatewayProviderContext
    ) -> ProviderDescriptor:
        return await self._host.current_descriptor(context)

    async def list_tools(
        self, context: GatewayProviderContext
    ) -> Sequence[ProviderTool]:
        return await self._host.list_tools(context)

    async def admit_call(
        self,
        context: GatewayProviderContext,
        *,
        operation: str,
        arguments: Mapping[str, Any],
        invocation_id: str,
    ) -> ProviderCallAdmission:
        return await self._host.admit_call(
            context,
            operation=operation,
            arguments=arguments,
            invocation_id=invocation_id,
        )

    async def call_tool(
        self,
        context: GatewayProviderContext,
        *,
        operation: str,
        arguments: Mapping[str, Any],
        invocation_id: str,
    ) -> ProviderCallResult:
        return await self._host.call_tool(
            context,
            operation=operation,
            arguments=arguments,
            invocation_id=invocation_id,
        )


__all__ = ["MANAGED_KDCUBE_MCP_PROVIDER_ID", "ManagedKDCubeMCPProvider"]

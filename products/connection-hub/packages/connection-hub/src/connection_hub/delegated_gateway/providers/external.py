# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Provider adapter for existing user-owned remote MCP connectors."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from connection_hub.delegated_credentials.credential_view import (
    DelegatedCredentialView,
)
from connection_hub.delegated_gateway.models import (
    DESCRIPTOR_CURRENT,
    RESOURCE_DISABLED,
    ProviderCallAdmission,
    ProviderCallResult,
    ProviderDescriptor,
    ProviderTool,
)
from connection_hub.delegated_gateway.ports import GatewayProviderContext
from connection_hub.remote_mcp.catalog import REMOTE_MCP_RESOURCE_KIND
from connection_hub.remote_mcp.models import (
    CONNECTOR_ACTIVE,
    connector_id_from_resource,
    proxy_tool_name,
)
from connection_hub.remote_mcp.proxy import RemoteMCPProxy, RemoteMCPProxyError
from connection_hub.remote_mcp.service import RemoteMCPConnectorService

EXTERNAL_MCP_PROVIDER_ID = "remote_mcp"
_PUBLIC_ADMISSION_REASONS = frozenset(
    {
        "connector_grant_not_consented",
        "connector_not_active",
        "operation_descriptor_changed",
        "operation_not_consented",
        "operation_removed_by_server",
        "tool_not_in_delegated_resources",
    }
)


class ExternalRemoteMCPProvider:
    """Translate the existing external-only proxy into the aggregate port.

    The adapter calls ``RemoteMCPProxy.resolve`` immediately before dispatch
    for its established connector, grant, selected-operation, and live-schema
    checks. Gateway owns invocation-policy consumption, so this adapter invokes
    the connector service only after that resolve and never calls
    ``RemoteMCPProxy.call``.
    """

    def __init__(
        self,
        service: RemoteMCPConnectorService,
        *,
        proxy: RemoteMCPProxy | None = None,
    ) -> None:
        self._service = service
        self._proxy = proxy or RemoteMCPProxy(service)

    @property
    def provider_id(self) -> str:
        return EXTERNAL_MCP_PROVIDER_ID

    @property
    def resource_kinds(self) -> frozenset[str]:
        return frozenset({REMOTE_MCP_RESOURCE_KIND})

    async def current_descriptor(
        self, context: GatewayProviderContext
    ) -> ProviderDescriptor:
        connector = await self._connector(context)
        state = (
            DESCRIPTOR_CURRENT
            if connector.state == CONNECTOR_ACTIVE
            else RESOURCE_DISABLED
        )
        discovery = None
        if state == DESCRIPTOR_CURRENT:
            # This is also the provider-readiness check: credential resolution,
            # endpoint policy, and live discovery all happen inside the trusted
            # existing connector service.
            discovery = await self._service.observe(connector)
        return ProviderDescriptor(
            resource_id=context.resource.resource_id,
            revision=str(connector.descriptor_revision),
            digest=(
                discovery.descriptor_digest
                if discovery is not None
                else connector.descriptor_digest
            ),
            operation_digests={
                tool.name: tool.descriptor_digest
                for tool in (
                    discovery.tools if discovery is not None else connector.tools
                )
            },
            state=state,
            unavailable_reason=""
            if state == DESCRIPTOR_CURRENT
            else "connector_not_active",
        )

    async def list_tools(
        self, context: GatewayProviderContext
    ) -> Sequence[ProviderTool]:
        decisions = await self._proxy.list_authorized(self._legacy_view(context))
        return tuple(
            ProviderTool(
                operation=decision.tool.name,
                descriptor_digest=decision.tool.descriptor_digest,
                title=decision.tool.name,
                description=decision.tool.description,
                input_schema=decision.tool.input_schema,
                output_schema=decision.tool.output_schema,
            )
            for decision in decisions
        )

    async def admit_call(
        self,
        context: GatewayProviderContext,
        *,
        operation: str,
        arguments: Mapping[str, Any],
        invocation_id: str,
    ) -> ProviderCallAdmission:
        del arguments
        connector_id = connector_id_from_resource(context.resource.resource_id)
        try:
            await self._proxy.resolve(
                view=self._legacy_view(context),
                proxy_name=proxy_tool_name(connector_id, operation),
                invocation_id=invocation_id,
            )
        except RemoteMCPProxyError as exc:
            reason = (
                exc.reason
                if exc.reason in _PUBLIC_ADMISSION_REASONS
                else "provider_admission_denied"
            )
            return ProviderCallAdmission(
                allowed=False,
                reason=reason,
                retryable=bool(exc.retryable),
            )
        return ProviderCallAdmission(allowed=True)

    async def call_tool(
        self,
        context: GatewayProviderContext,
        *,
        operation: str,
        arguments: Mapping[str, Any],
        invocation_id: str,
    ) -> ProviderCallResult:
        connector_id = connector_id_from_resource(context.resource.resource_id)
        decision = await self._proxy.resolve(
            view=self._legacy_view(context),
            proxy_name=proxy_tool_name(connector_id, operation),
            invocation_id=invocation_id,
        )
        value = await self._service.call_tool(
            connector=decision.connector,
            tool_name=decision.tool.name,
            arguments=dict(arguments),
        )
        return ProviderCallResult.from_value(value)

    async def _connector(self, context: GatewayProviderContext):
        connector_id = connector_id_from_resource(context.resource.resource_id)
        if not connector_id:
            raise ValueError("remote_mcp_resource_invalid")
        return await self._service.get(
            owner_subject=context.card.grantor_subject,
            connector_id=connector_id,
        )

    @staticmethod
    def _legacy_view(context: GatewayProviderContext) -> DelegatedCredentialView:
        resource = context.resource
        return DelegatedCredentialView(
            client_id=context.caller.client_id,
            identity_scope=context.card.identity_scope,
            grantor_user_id=context.card.grantor_subject,
            registry_access_id=context.card.access_id,
            card_revision=context.card.card_revision,
            catalog_version=resource.accepted_descriptor.revision,
            resource_grants={resource.resource_id: tuple(resource.grants)},
            grants=frozenset(resource.grants),
            operations=tuple(resource.operations),
            resource_operations={resource.resource_id: tuple(resource.operations)},
            present=True,
        )


__all__ = ["EXTERNAL_MCP_PROVIDER_ID", "ExternalRemoteMCPProvider"]

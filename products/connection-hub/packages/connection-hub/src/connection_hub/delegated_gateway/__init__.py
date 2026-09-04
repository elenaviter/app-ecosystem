# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Portable aggregate delegated MCP gateway contract."""

from connection_hub.delegated_gateway.card_adapter import (
    CardResourceMetadataResolver,
    GatewayResourceMetadata,
    adapt_card_view,
    caller_profile_id_for_card,
)
from connection_hub.delegated_gateway.models import (
    ACCESS_DESCRIBE_TOOL,
    DISCOVER_REQUESTABLE,
    AcceptedDescriptor,
    DelegatedCardView,
    DelegatedGatewayError,
    DelegatedResourceEntry,
    GatewayCaller,
    GatewayCallResult,
    GatewayContractError,
    GatewayTool,
    GatewayToolRoute,
    InvocationPolicyView,
    ProviderCallAdmission,
    ProviderCallResult,
    ProviderDescriptor,
    ProviderTool,
    RecoveryLink,
    RequestableResource,
)
from connection_hub.delegated_gateway.naming import (
    QualifiedToolNameIndex,
    is_qualified_tool_name,
    qualified_tool_name,
)
from connection_hub.delegated_gateway.ports import (
    DelegatedCardReader,
    DelegatedMCPResourceProvider,
    GatewayAuditEvent,
    GatewayAuditSink,
    GatewayInvocationDecision,
    GatewayInvocationPolicy,
    GatewayInvocationRequest,
    GatewayProviderContext,
    ManagedKDCubeMCPHost,
    MemoryGatewayAuditSink,
    RequestableResourceReader,
)
from connection_hub.delegated_gateway.registry import (
    DelegatedMCPProviderRegistry,
)
from connection_hub.delegated_gateway.service import DelegatedMCPGateway

__all__ = [
    "ACCESS_DESCRIBE_TOOL",
    "DISCOVER_REQUESTABLE",
    "AcceptedDescriptor",
    "CardResourceMetadataResolver",
    "DelegatedCardReader",
    "DelegatedCardView",
    "DelegatedGatewayError",
    "DelegatedMCPGateway",
    "DelegatedMCPProviderRegistry",
    "DelegatedMCPResourceProvider",
    "DelegatedResourceEntry",
    "GatewayAuditEvent",
    "GatewayAuditSink",
    "GatewayCallResult",
    "GatewayCaller",
    "GatewayContractError",
    "GatewayInvocationDecision",
    "GatewayInvocationPolicy",
    "GatewayInvocationRequest",
    "GatewayProviderContext",
    "GatewayResourceMetadata",
    "GatewayTool",
    "GatewayToolRoute",
    "InvocationPolicyView",
    "ManagedKDCubeMCPHost",
    "MemoryGatewayAuditSink",
    "ProviderCallAdmission",
    "ProviderCallResult",
    "ProviderDescriptor",
    "ProviderTool",
    "QualifiedToolNameIndex",
    "RecoveryLink",
    "RequestableResource",
    "RequestableResourceReader",
    "adapt_card_view",
    "caller_profile_id_for_card",
    "is_qualified_tool_name",
    "qualified_tool_name",
]

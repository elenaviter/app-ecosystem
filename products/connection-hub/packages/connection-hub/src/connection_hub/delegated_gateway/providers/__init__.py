# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Built-in provider adapters for the delegated MCP gateway."""

from connection_hub.delegated_gateway.providers.external import (
    EXTERNAL_MCP_PROVIDER_ID,
    ExternalRemoteMCPProvider,
)
from connection_hub.delegated_gateway.providers.managed import (
    MANAGED_KDCUBE_MCP_PROVIDER_ID,
    ManagedKDCubeMCPProvider,
)

__all__ = [
    "EXTERNAL_MCP_PROVIDER_ID",
    "MANAGED_KDCUBE_MCP_PROVIDER_ID",
    "ExternalRemoteMCPProvider",
    "ManagedKDCubeMCPProvider",
]

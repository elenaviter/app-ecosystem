# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""User-owned remote MCP connectors and governed delegated calls."""

from connection_hub.remote_mcp.models import (
    AUTH_BEARER,
    AUTH_HEADER,
    AUTH_NONE,
    AUTH_OAUTH,
    CONNECTOR_ACTIVE,
    CONNECTOR_DELETED,
    CONNECTOR_DISABLED,
    DESCRIPTOR_ACCEPTED,
    DESCRIPTOR_DRIFTED,
    OAUTH_CLIENT_SOURCE_CIMD,
    OAUTH_CLIENT_SOURCE_DCR,
    OAUTH_CLIENT_SOURCE_PROVISIONED,
    RemoteMCPConnector,
    RemoteMCPCredential,
    RemoteMCPDiscovery,
    RemoteMCPOAuthCredential,
    RemoteMCPProvisionedOAuthClient,
    RemoteMCPRecordError,
    RemoteMCPTool,
    connector_id_from_resource,
    connector_resource,
)
from connection_hub.remote_mcp.oauth_state import (
    BundleStorageRemoteMCPOAuthStateStore,
    RemoteMCPOAuthStateError,
    RemoteMCPOAuthStateHandle,
)
from connection_hub.remote_mcp.catalog import (
    RemoteMCPResourceRow,
    remote_mcp_resource_row,
    remote_mcp_resource_rows,
)
from connection_hub.remote_mcp.proxy import (
    EXTERNAL_MCP_GRANT,
    RemoteMCPProxy,
    RemoteMCPProxyDecision,
    RemoteMCPProxyError,
)
from connection_hub.remote_mcp.security import (
    RemoteMCPEndpointDenied,
    RemoteMCPEndpointPolicy,
)
from connection_hub.remote_mcp.service import (
    RemoteMCPConnectorConflict,
    RemoteMCPConnectorNotFound,
    RemoteMCPConnectorService,
    RemoteMCPMutationLock,
    RemoteMCPSecretStore,
    RemoteMCPTransport,
)
from connection_hub.remote_mcp.store import (
    BundleStorageRemoteMCPConnectorStore,
    RemoteMCPConnectorStore,
    RemoteMCPStorageError,
)

__all__ = [
    "AUTH_BEARER",
    "AUTH_HEADER",
    "AUTH_NONE",
    "AUTH_OAUTH",
    "CONNECTOR_ACTIVE",
    "CONNECTOR_DELETED",
    "CONNECTOR_DISABLED",
    "DESCRIPTOR_ACCEPTED",
    "DESCRIPTOR_DRIFTED",
    "OAUTH_CLIENT_SOURCE_CIMD",
    "OAUTH_CLIENT_SOURCE_DCR",
    "OAUTH_CLIENT_SOURCE_PROVISIONED",
    "EXTERNAL_MCP_GRANT",
    "BundleStorageRemoteMCPConnectorStore",
    "BundleStorageRemoteMCPOAuthStateStore",
    "RemoteMCPConnector",
    "RemoteMCPConnectorConflict",
    "RemoteMCPConnectorNotFound",
    "RemoteMCPConnectorService",
    "RemoteMCPConnectorStore",
    "RemoteMCPCredential",
    "RemoteMCPDiscovery",
    "RemoteMCPOAuthCredential",
    "RemoteMCPProvisionedOAuthClient",
    "RemoteMCPOAuthStateError",
    "RemoteMCPOAuthStateHandle",
    "RemoteMCPEndpointDenied",
    "RemoteMCPEndpointPolicy",
    "RemoteMCPMutationLock",
    "RemoteMCPProxy",
    "RemoteMCPProxyDecision",
    "RemoteMCPProxyError",
    "RemoteMCPRecordError",
    "RemoteMCPSecretStore",
    "RemoteMCPStorageError",
    "RemoteMCPTool",
    "RemoteMCPTransport",
    "connector_id_from_resource",
    "connector_resource",
    "RemoteMCPResourceRow",
    "remote_mcp_resource_row",
    "remote_mcp_resource_rows",
]

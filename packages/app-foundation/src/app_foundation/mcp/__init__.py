"""Host-neutral MCP client primitives."""

from app_foundation.mcp.client import (
    ConnectedRemoteTools,
    MCP_CLIENT_MODE_AUTO,
    McpProbeResult,
    MessageHandler,
    ProgressHandler,
    RemoteMcpConnectionError,
    RemoteTools,
    connect_remote_tools,
    mcp_tool_schema,
    normalize_mcp_tool_result,
    open_mcp_client,
    probe_remote_tools,
)

__all__ = [
    "ConnectedRemoteTools",
    "MCP_CLIENT_MODE_AUTO",
    "McpProbeResult",
    "MessageHandler",
    "ProgressHandler",
    "RemoteMcpConnectionError",
    "RemoteTools",
    "connect_remote_tools",
    "mcp_tool_schema",
    "normalize_mcp_tool_result",
    "open_mcp_client",
    "probe_remote_tools",
]

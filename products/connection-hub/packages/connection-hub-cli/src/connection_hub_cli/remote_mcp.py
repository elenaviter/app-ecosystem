from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app_foundation.mcp import (
    ConnectedRemoteTools,
    MessageHandler,
    ProgressHandler,
    RemoteMcpConnectionError,
    RemoteTools,
)
from app_foundation.mcp import connect_remote_tools as connect_foundation_tools
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from connection_hub_cli import __version__
from connection_hub_cli.errors import UpstreamError
from connection_hub_cli.models import ProbeResult


def _connection_hub_error(exc: RemoteMcpConnectionError) -> UpstreamError:
    messages = {
        "mcp_connection_timeout": (
            "The Connection Hub MCP endpoint did not respond before the connection timeout."
        ),
        "mcp_endpoint_unreachable": (
            "The Connection Hub MCP endpoint could not be reached."
        ),
        "mcp_connection_failed": (
            "The Connection Hub MCP endpoint rejected or could not complete the MCP connection."
        ),
    }
    return UpstreamError(
        exc.code,
        messages.get(exc.code, "The Connection Hub MCP connection failed."),
    )


@asynccontextmanager
async def connect_remote_tools(
    *,
    endpoint: str,
    bearer: str,
    message_handler: MessageHandler | None = None,
    timeout_seconds: float = 120.0,
) -> AsyncIterator[tuple[ConnectedRemoteTools, Client]]:
    """Open the governed MCP connection for one Connection Hub caller."""

    try:
        async with connect_foundation_tools(
            endpoint=endpoint,
            bearer=bearer,
            client_name="connection-hub-cli",
            client_version=__version__,
            user_agent="connection-hub-cli",
            message_handler=message_handler,
            timeout_seconds=timeout_seconds,
            transport_factory=streamable_http_client,
        ) as connected:
            yield connected
    except RemoteMcpConnectionError as exc:
        raise _connection_hub_error(exc) from exc
    except ValueError as exc:
        raise UpstreamError(
            "mcp_connection_failed",
            "The Connection Hub MCP endpoint rejected or could not complete the MCP connection.",
        ) from exc


async def probe_remote_tools(*, endpoint: str, bearer: str) -> ProbeResult:
    try:
        async with connect_remote_tools(endpoint=endpoint, bearer=bearer) as (
            remote,
            client,
        ):
            tools = await remote.list_tools()
            server_info = client.server_info
            return ProbeResult(
                tool_count=len(tools.tools),
                server_name=server_info.name if server_info else None,
                server_version=server_info.version if server_info else None,
            )
    except UpstreamError:
        raise
    except Exception as exc:
        raise UpstreamError(
            "mcp_connection_failed",
            "The Connection Hub MCP endpoint rejected or could not complete the MCP connection.",
        ) from exc


__all__ = [
    "ConnectedRemoteTools",
    "MessageHandler",
    "ProgressHandler",
    "RemoteTools",
    "connect_remote_tools",
    "probe_remote_tools",
]

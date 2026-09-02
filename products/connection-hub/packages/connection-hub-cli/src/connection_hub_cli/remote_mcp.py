from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, Protocol

import anyio
import httpx2
from mcp import Client, types
from mcp.client.streamable_http import streamable_http_client
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

from connection_hub_cli import __version__
from connection_hub_cli.errors import UpstreamError
from connection_hub_cli.models import ProbeResult

MessageHandler = Callable[[Any], Awaitable[None]]
ProgressHandler = Callable[[float, float | None, str | None], Awaitable[None]]


class RemoteTools(Protocol):
    async def list_tools(
        self,
        *,
        params: types.PaginatedRequestParams | None = None,
    ) -> types.ListToolsResult: ...

    async def call_tool(
        self,
        *,
        name: str,
        arguments: dict[str, Any] | None,
        input_responses: types.InputResponses | None,
        request_state: str | None,
        meta: types.RequestParamsMeta | None,
        progress_callback: ProgressHandler | None,
    ) -> types.CallToolResult | types.InputRequiredResult | types.Result: ...


class ConnectedRemoteTools:
    def __init__(self, client: Client) -> None:
        self.client = client

    async def list_tools(
        self,
        *,
        params: types.PaginatedRequestParams | None = None,
    ) -> types.ListToolsResult:
        return await self.client.session.list_tools(params=params)

    async def call_tool(
        self,
        *,
        name: str,
        arguments: dict[str, Any] | None,
        input_responses: types.InputResponses | None,
        request_state: str | None,
        meta: types.RequestParamsMeta | None,
        progress_callback: ProgressHandler | None,
    ) -> types.CallToolResult | types.InputRequiredResult | types.Result:
        return await self.client.session.call_tool(
            name,
            arguments,
            progress_callback=progress_callback,
            input_responses=input_responses,
            request_state=request_state,
            meta=meta,
            allow_input_required=True,
        )


def _safe_connection_error(exc: Exception) -> UpstreamError:
    if isinstance(exc, httpx2.TimeoutException):
        return UpstreamError(
            "mcp_connection_timeout",
            "The Connection Hub MCP endpoint did not respond before the connection timeout.",
        )
    if isinstance(exc, httpx2.ConnectError):
        return UpstreamError(
            "mcp_endpoint_unreachable",
            "The Connection Hub MCP endpoint could not be reached.",
        )
    return UpstreamError(
        "mcp_connection_failed",
        "The Connection Hub MCP endpoint rejected or could not complete the MCP connection.",
    )


async def _consume_modern_tool_changes(
    client: Client,
    *,
    task_status: anyio.abc.TaskStatus[None] = anyio.TASK_STATUS_IGNORED,
) -> None:
    async with client.listen(tools_list_changed=True) as subscription:
        task_status.started()
        async for _event in subscription:
            # ClientSession also sends subscribed events to message_handler.
            # Draining this iterator keeps the bounded subscription route clear.
            pass


@asynccontextmanager
async def connect_remote_tools(
    *,
    endpoint: str,
    bearer: str,
    message_handler: MessageHandler | None = None,
    timeout_seconds: float = 120.0,
) -> AsyncIterator[tuple[ConnectedRemoteTools, Client]]:
    headers = {
        "Authorization": f"Bearer {bearer}",
        "User-Agent": "connection-hub-cli",
    }
    stack = AsyncExitStack()
    try:
        http_client = await stack.enter_async_context(
            httpx2.AsyncClient(
                headers=headers,
                timeout=httpx2.Timeout(timeout_seconds, connect=15.0),
                follow_redirects=False,
                trust_env=False,
            )
        )
        transport = streamable_http_client(
            endpoint,
            http_client=http_client,
            terminate_on_close=True,
        )
        client = await stack.enter_async_context(
            Client(
                transport,
                mode="auto",
                cache=None,
                message_handler=message_handler,
                client_info=types.Implementation(
                    name="connection-hub-cli",
                    version=__version__,
                ),
            )
        )
        tools_capability = client.server_capabilities.tools
        if (
            message_handler is not None
            and client.protocol_version in MODERN_PROTOCOL_VERSIONS
            and tools_capability is not None
            and tools_capability.list_changed is True
        ):
            tasks = await stack.enter_async_context(anyio.create_task_group())
            stack.callback(tasks.cancel_scope.cancel)
            await tasks.start(_consume_modern_tool_changes, client)
    except Exception as exc:
        await stack.aclose()
        raise _safe_connection_error(exc) from exc
    try:
        yield ConnectedRemoteTools(client), client
    finally:
        await stack.aclose()


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
        raise _safe_connection_error(exc) from exc

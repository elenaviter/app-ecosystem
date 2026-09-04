from __future__ import annotations

from typing import Any

import anyio
from mcp import MCPError, stdio_server, types
from mcp.server import NotificationOptions, Server
from mcp.server.session import ServerSession
from mcp.server.subscriptions import (
    InMemorySubscriptionBus,
    ListenHandler,
    ToolsListChanged,
)
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

from connection_hub_cli import __version__
from connection_hub_cli.authorization.profile_session import (
    OAuthProfileSessionService,
)
from connection_hub_cli.credentials import CredentialStore
from connection_hub_cli.profile_connection import connect_profile_tools
from connection_hub_cli.remote_mcp import RemoteTools
from connection_hub_cli.state import ProfileStore


class DownstreamToolChanges:
    def __init__(self) -> None:
        self._session: ServerSession | None = None
        self._bus = InMemorySubscriptionBus()
        self.listen = ListenHandler(self._bus)

    def attach(self, session: ServerSession) -> None:
        self._session = session

    async def handle_upstream_message(self, message: Any) -> None:
        if not isinstance(message, types.ToolListChangedNotification):
            return
        await self._bus.publish(ToolsListChanged())
        session = self._session
        if session is None or session.protocol_version in MODERN_PROTOCOL_VERSIONS:
            return
        try:
            await session.send_tool_list_changed()
        except Exception:  # noqa: BLE001 - a failed legacy notification detaches the session
            self._session = None

    def close(self) -> None:
        self.listen.close()


class McpToolRelay:
    def __init__(
        self, upstream: RemoteTools, downstream_changes: DownstreamToolChanges
    ) -> None:
        self.upstream = upstream
        self.downstream_changes = downstream_changes
        self.server = Server(
            "connection-hub-local-helper",
            version=__version__,
            description="Relays a local MCP client to one governed Connection Hub caller profile.",
            on_list_tools=self._list_tools,
            on_call_tool=self._call_tool,
            on_subscriptions_listen=self.downstream_changes.listen,
        )

    async def _list_tools(
        self,
        ctx: Any,
        params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        self.downstream_changes.attach(ctx.session)
        try:
            return await self.upstream.list_tools(params=params)
        except anyio.get_cancelled_exc_class():
            raise
        except Exception:  # noqa: BLE001 - MCP boundary drops untrusted upstream details
            raise MCPError(
                types.INTERNAL_ERROR,
                "Connection Hub could not provide the current tool list.",
            ) from None

    async def _call_tool(
        self,
        ctx: Any,
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult | types.InputRequiredResult | types.Result:
        self.downstream_changes.attach(ctx.session)

        async def report_progress(
            progress: float,
            total: float | None,
            message: str | None,
        ) -> None:
            await ctx.session.report_progress(progress, total, message)

        try:
            return await self.upstream.call_tool(
                name=params.name,
                arguments=params.arguments,
                input_responses=params.input_responses,
                request_state=params.request_state,
                meta=params.meta,
                progress_callback=report_progress,
            )
        except anyio.get_cancelled_exc_class():
            raise
        except Exception:  # noqa: BLE001 - MCP boundary returns one stable safe result
            message = "Connection Hub could not complete this tool call."
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=message)],
                structuredContent={
                    "ok": False,
                    "error": {
                        "code": "connection_hub_unavailable",
                        "message": message,
                    },
                },
                isError=True,
            )

    async def run_stdio(self) -> None:
        options = self.server.create_initialization_options(
            NotificationOptions(tools_changed=True)
        )
        try:
            async with stdio_server() as (read_stream, write_stream):
                await self.server.run(read_stream, write_stream, options)
        finally:
            self.downstream_changes.close()


async def serve_profile(
    *,
    profile_name: str,
    profiles: ProfileStore,
    credentials: CredentialStore,
    oauth_sessions: OAuthProfileSessionService | None = None,
) -> None:
    downstream_changes = DownstreamToolChanges()
    async with connect_profile_tools(
        profile_name=profile_name,
        profiles=profiles,
        credentials=credentials,
        oauth_sessions=oauth_sessions,
        message_handler=downstream_changes.handle_upstream_message,
    ) as (upstream, _client):
        await McpToolRelay(upstream, downstream_changes).run_stdio()

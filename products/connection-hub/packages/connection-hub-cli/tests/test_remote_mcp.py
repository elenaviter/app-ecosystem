from __future__ import annotations

import socket
from typing import Any

import anyio
import pytest
import uvicorn
from mcp import types
from mcp.server.mcpserver import MCPServer
from mcp.server.subscriptions import InMemorySubscriptionBus, ToolsListChanged

from connection_hub_cli import remote_mcp
from connection_hub_cli.remote_mcp import connect_remote_tools, probe_remote_tools


class _CaptureHeaders:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.authorization: list[str | None] = []

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            headers = {
                key.decode().lower(): value.decode() for key, value in scope["headers"]
            }
            self.authorization.append(headers.get("authorization"))
        await self.app(scope, receive, send)


@pytest.mark.asyncio
async def test_real_streamable_http_connection_uses_keychain_bearer_and_preserves_results(
    monkeypatch,
) -> None:
    subscriptions = InMemorySubscriptionBus()
    mcp = MCPServer(
        "connection-hub-fixture",
        version="1",
        subscriptions=subscriptions,
    )

    @mcp.tool()
    async def echo(value: str) -> dict[str, str]:
        return {"value": value}

    capture = _CaptureHeaders(mcp.streamable_http_app())
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            capture,
            log_config=None,
            log_level="critical",
            access_log=False,
            lifespan="on",
        )
    )
    endpoint = f"http://127.0.0.1:{port}/mcp"
    bearer = "synthetic-keychain-bearer"
    termination_settings: list[bool] = []
    original_transport = remote_mcp.streamable_http_client

    def recording_transport(*args, **kwargs):
        termination_settings.append(kwargs["terminate_on_close"])
        return original_transport(*args, **kwargs)

    monkeypatch.setattr(remote_mcp, "streamable_http_client", recording_transport)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(server.serve, [listener])
        with anyio.fail_after(5):
            while not server.started:
                await anyio.sleep(0.01)
        try:
            probe = await probe_remote_tools(endpoint=endpoint, bearer=bearer)
            assert probe.tool_count == 1
            assert probe.server_name == "connection-hub-fixture"

            async with connect_remote_tools(endpoint=endpoint, bearer=bearer) as (
                remote,
                _client,
            ):
                result = await remote.call_tool(
                    name="echo",
                    arguments={"value": "ready"},
                    input_responses=None,
                    request_state=None,
                    meta=None,
                    progress_callback=None,
                )
                assert result.structured_content == {"value": "ready"}
        finally:
            server.should_exit = True

    assert capture.authorization
    assert set(capture.authorization) == {f"Bearer {bearer}"}
    assert termination_settings == [True, True]


@pytest.mark.asyncio
async def test_modern_upstream_tool_list_change_reaches_message_handler() -> None:
    subscriptions = InMemorySubscriptionBus()
    mcp = MCPServer(
        "connection-hub-fixture",
        version="1",
        subscriptions=subscriptions,
    )

    @mcp.tool()
    async def echo(value: str) -> dict[str, str]:
        return {"value": value}

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            mcp.streamable_http_app(),
            log_config=None,
            log_level="critical",
            access_log=False,
            lifespan="on",
        )
    )
    received = anyio.Event()
    received_messages: list[types.ToolListChangedNotification] = []

    async def messages(message: Any) -> None:
        if isinstance(message, types.ToolListChangedNotification):
            received_messages.append(message)
            received.set()

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(server.serve, [listener])
        with anyio.fail_after(5):
            while not server.started:
                await anyio.sleep(0.01)
        try:
            async with connect_remote_tools(
                endpoint=f"http://127.0.0.1:{port}/mcp",
                bearer="synthetic-keychain-bearer",
                message_handler=messages,
            ) as (_remote, client):
                assert client.protocol_version == "2026-07-28"
                await subscriptions.publish(ToolsListChanged())
                with anyio.fail_after(1):
                    await received.wait()
                await anyio.sleep(0.01)
                assert len(received_messages) == 1
        finally:
            server.should_exit = True

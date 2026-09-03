from __future__ import annotations

import socket
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
import uvicorn
from mcp import types
from mcp.server.mcpserver import MCPServer
from mcp.server.subscriptions import InMemorySubscriptionBus, ToolsListChanged

from app_foundation.mcp import (
    connect_remote_tools,
    mcp_tool_schema,
    normalize_mcp_tool_result,
    probe_remote_tools,
)


class _CaptureHeaders:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.authorization: list[str | None] = []
        self.user_agents: list[str | None] = []

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            headers = {
                key.decode().lower(): value.decode() for key, value in scope["headers"]
            }
            self.authorization.append(headers.get("authorization"))
            self.user_agents.append(headers.get("user-agent"))
        await self.app(scope, receive, send)


@pytest.mark.asyncio
async def test_remote_connection_is_host_neutral_and_preserves_results() -> None:
    mcp = MCPServer("foundation-fixture", version="1")

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

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(server.serve, [listener])
        with anyio.fail_after(5):
            while not server.started:
                await anyio.sleep(0.01)
        try:
            probe = await probe_remote_tools(
                endpoint=endpoint,
                bearer="synthetic-bearer",
                client_name="foundation-test",
                client_version="1",
            )
            assert probe.tool_count == 1
            assert probe.server_name == "foundation-fixture"

            async with connect_remote_tools(
                endpoint=endpoint,
                bearer="synthetic-bearer",
                client_name="foundation-test",
                client_version="1",
            ) as (remote, _client):
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

    assert set(capture.authorization) == {"Bearer synthetic-bearer"}
    assert set(capture.user_agents) == {"foundation-test"}


@pytest.mark.asyncio
async def test_modern_tool_change_reaches_supplied_handler() -> None:
    subscriptions = InMemorySubscriptionBus()
    mcp = MCPServer("foundation-fixture", version="1", subscriptions=subscriptions)

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
    messages: list[types.ToolListChangedNotification] = []

    async def handle(message: Any) -> None:
        if isinstance(message, types.ToolListChangedNotification):
            messages.append(message)
            received.set()

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(server.serve, [listener])
        with anyio.fail_after(5):
            while not server.started:
                await anyio.sleep(0.01)
        try:
            async with connect_remote_tools(
                endpoint=f"http://127.0.0.1:{port}/mcp",
                bearer="synthetic-bearer",
                client_name="foundation-test",
                client_version="1",
                message_handler=handle,
            ):
                await subscriptions.publish(ToolsListChanged())
                with anyio.fail_after(1):
                    await received.wait()
                assert len(messages) == 1
        finally:
            server.should_exit = True


def test_tool_schema_and_result_normalization_preserve_extracted_contract() -> None:
    tool = SimpleNamespace(
        name="summarize",
        description="Summarize one object.",
        inputSchema={"type": "object"},
        outputSchema={"type": "object"},
    )
    result = SimpleNamespace(
        structuredContent=None,
        content=[SimpleNamespace(type="text", text='{"summary":"ready"}')],
        isError=False,
    )

    assert mcp_tool_schema(tool) == {
        "name": "summarize",
        "description": "Summarize one object.",
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
    }
    assert normalize_mcp_tool_result(result) == {
        "summary": "ready",
        "is_error": False,
    }

from __future__ import annotations

from typing import Any

import anyio
import pytest
from mcp import Client, types
from mcp.shared.subscriptions import ToolsListChanged

from connection_hub_cli.mcp_relay import DownstreamToolChanges, McpToolRelay


class _Remote:
    def __init__(self) -> None:
        self.tools = [
            types.Tool(
                name="search",
                description="Search the fixture.",
                inputSchema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
            )
        ]
        self.calls: list[dict[str, Any]] = []
        self.raise_on_call: Exception | None = None
        self.started = anyio.Event()
        self.cancelled = anyio.Event()
        self.block = False

    async def list_tools(self, *, params=None) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=self.tools, nextCursor=params.cursor if params else None
        )

    async def call_tool(
        self,
        *,
        name,
        arguments,
        input_responses,
        request_state,
        meta,
        progress_callback,
    ):
        self.calls.append(
            {
                "name": name,
                "arguments": arguments,
                "input_responses": input_responses,
                "request_state": request_state,
                "meta": meta,
            }
        )
        if self.raise_on_call:
            raise self.raise_on_call
        if progress_callback:
            await progress_callback(1, 2, "half")
        if self.block:
            self.started.set()
            try:
                await anyio.sleep_forever()
            finally:
                self.cancelled.set()
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="ok")],
            structuredContent={"ok": True, "name": name},
        )


@pytest.mark.asyncio
async def test_relay_preserves_tools_call_metadata_structured_result_and_progress() -> (
    None
):
    remote = _Remote()
    relay = McpToolRelay(remote, DownstreamToolChanges())
    progress: list[tuple[float, float | None, str | None]] = []

    async def on_progress(
        value: float, total: float | None, message: str | None
    ) -> None:
        progress.append((value, total, message))

    async with Client(relay.server, mode="legacy", cache=None) as client:
        tools = await client.list_tools(cache_mode="reload")
        result = await client.call_tool(
            "search",
            {"query": "boats"},
            meta={"connection_hub/invocation_id": "invocation-1"},
            progress_callback=on_progress,
        )

    assert [tool.name for tool in tools.tools] == ["search"]
    assert result.structured_content == {"ok": True, "name": "search"}
    assert remote.calls[0]["arguments"] == {"query": "boats"}
    assert remote.calls[0]["meta"]["connection_hub/invocation_id"] == "invocation-1"
    assert "progress_token" in remote.calls[0]["meta"]
    assert progress == [(1.0, 2.0, "half")]


@pytest.mark.asyncio
async def test_relay_forwards_tool_list_change_notification() -> None:
    remote = _Remote()
    changes = DownstreamToolChanges()
    relay = McpToolRelay(remote, changes)
    received: list[str] = []

    async def messages(message: Any) -> None:
        received.append(type(message).__name__)

    async with Client(
        relay.server, mode="legacy", cache=None, message_handler=messages
    ) as client:
        await client.list_tools(cache_mode="reload")
        remote.tools.append(types.Tool(name="delete", inputSchema={"type": "object"}))
        await changes.handle_upstream_message(types.ToolListChangedNotification())
        await anyio.sleep(0.01)

    assert received == ["ToolListChangedNotification"]


@pytest.mark.asyncio
async def test_relay_publishes_tool_list_changes_to_modern_subscribers() -> None:
    remote = _Remote()
    changes = DownstreamToolChanges()
    relay = McpToolRelay(remote, changes)

    async with Client(relay.server, mode="2026-07-28", cache=None) as client:
        async with client.listen(tools_list_changed=True) as subscription:
            await changes.handle_upstream_message(types.ToolListChangedNotification())
            with anyio.fail_after(1):
                event = await subscription.__anext__()

    assert isinstance(event, ToolsListChanged)


@pytest.mark.asyncio
async def test_relay_cancels_the_upstream_call_when_downstream_cancels() -> None:
    remote = _Remote()
    remote.block = True
    relay = McpToolRelay(remote, DownstreamToolChanges())

    async with Client(relay.server, mode="legacy", cache=None) as client:
        with anyio.move_on_after(0.05) as scope:
            await client.call_tool("search", {"query": "slow"})
        assert scope.cancel_called
        with anyio.fail_after(1):
            await remote.cancelled.wait()


@pytest.mark.asyncio
async def test_relay_sanitizes_an_upstream_exception() -> None:
    remote = _Remote()
    secret = "delegated-secret-must-not-cross"
    remote.raise_on_call = RuntimeError(secret)
    relay = McpToolRelay(remote, DownstreamToolChanges())

    async with Client(relay.server, mode="legacy", cache=None) as client:
        result = await client.call_tool("search", {})

    rendered = result.model_dump_json(by_alias=True)
    assert result.is_error is True
    assert result.structured_content["error"]["code"] == "connection_hub_unavailable"
    assert secret not in rendered

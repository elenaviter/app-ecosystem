"""Host-neutral MCP client construction, normalization, and remote-tool access."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

import anyio
import httpx2
from mcp import Client, types
from mcp.client.streamable_http import streamable_http_client
from mcp_types.version import MODERN_PROTOCOL_VERSIONS


MCP_CLIENT_MODE_AUTO = "auto"
MessageHandler = Callable[[Any], Awaitable[None]]
ProgressHandler = Callable[[float, float | None, str | None], Awaitable[None]]
TransportFactory = Callable[..., Any]


@asynccontextmanager
async def open_mcp_client(
    *,
    transport: str,
    endpoint: str = "",
    command: str | None = None,
    args: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
    mode: str = MCP_CLIENT_MODE_AUTO,
    read_timeout_seconds: float | None = None,
    follow_redirects: bool = True,
    http_transport: Any = None,
    trust_env: bool = True,
    http_timeout_seconds: float = 30.0,
    http_read_timeout_seconds: float = 300.0,
    http_connect_timeout_seconds: float | None = None,
    terminate_on_close: bool = True,
    message_handler: MessageHandler | None = None,
    client_info: types.Implementation | None = None,
    cache: Any = ...,
    streamable_http_transport_factory: TransportFactory = streamable_http_client,
) -> AsyncIterator[Client]:
    """Open an MCP SDK v2 client for stdio, SSE, or Streamable HTTP.

    ``mode="auto"`` first probes ``server/discover`` and falls back to the
    pre-2026 ``initialize`` handshake when the peer is legacy. HTTP headers
    remain transport data and are never copied into MCP parameters.
    """

    normalized = str(transport or "stdio").strip().lower()
    if normalized in {"stdio", "local"}:
        target = _stdio_transport(command=command, args=args, env=env)
    elif normalized == "sse":
        target = _sse_transport(endpoint=endpoint, headers=headers)
    elif normalized in {"streamable-http", "streamable_http", "http", ""}:
        target = _streamable_http_transport(
            endpoint=endpoint,
            headers=headers,
            follow_redirects=follow_redirects,
            http_transport=http_transport,
            trust_env=trust_env,
            timeout_seconds=http_timeout_seconds,
            read_timeout_seconds=http_read_timeout_seconds,
            connect_timeout_seconds=http_connect_timeout_seconds,
            terminate_on_close=terminate_on_close,
            transport_factory=streamable_http_transport_factory,
        )
    else:
        raise ValueError(f"Unsupported MCP transport: {transport!r}")

    client_options: dict[str, Any] = {
        "mode": mode,
        "read_timeout_seconds": read_timeout_seconds,
    }
    if message_handler is not None:
        client_options["message_handler"] = message_handler
    if client_info is not None:
        client_options["client_info"] = client_info
    if cache is not ...:
        client_options["cache"] = cache
    async with Client(target, **client_options) as client:
        yield client


def mcp_tool_schema(tool: Any) -> dict[str, Any]:
    """Return one SDK tool as a stable, JSON-schema-shaped mapping."""

    schema = (
        getattr(tool, "input_schema", None)
        or getattr(tool, "inputSchema", None)
        or {}
    )
    if hasattr(schema, "model_dump"):
        schema = schema.model_dump(mode="json", by_alias=True)
    if not isinstance(schema, dict):
        schema = {}
    return {
        "name": str(getattr(tool, "name", None) or getattr(tool, "id", None) or ""),
        "description": str(getattr(tool, "description", None) or ""),
        "input_schema": schema,
        "output_schema": _model_or_mapping(
            getattr(tool, "output_schema", None)
            or getattr(tool, "outputSchema", None)
        ),
    }


def normalize_mcp_tool_result(result: Any) -> Any:
    """Convert an SDK call result into a transport-neutral Python value."""

    structured = getattr(result, "structured_content", None)
    if structured is None:
        structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured

    content = [_content_block(block) for block in (getattr(result, "content", None) or [])]
    json_payload = _single_json_text_payload(content)
    if json_payload is not None:
        is_error = getattr(result, "is_error", None)
        if is_error is None:
            is_error = getattr(result, "isError", None)
        if (
            isinstance(json_payload, Mapping)
            and is_error is not None
            and "is_error" not in json_payload
            and "isError" not in json_payload
        ):
            json_payload = {**json_payload, "is_error": bool(is_error)}
        return json_payload

    payload: dict[str, Any] = {"content": content}
    is_error = getattr(result, "is_error", None)
    if is_error is None:
        is_error = getattr(result, "isError", None)
    if is_error is not None:
        payload["is_error"] = bool(is_error)
    return payload


def _single_json_text_payload(content: Sequence[Any]) -> Any:
    if len(content) != 1:
        return None
    block = content[0]
    if isinstance(block, Mapping):
        if str(block.get("type") or "") != "text":
            return None
        text = block.get("text")
    else:
        text = getattr(block, "text", None)
    if not isinstance(text, str):
        return None
    raw = text.strip()
    if not raw.startswith(("{", "[")):
        return None
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    return parsed if isinstance(parsed, (dict, list)) else None


def _content_block(block: Any) -> Any:
    if hasattr(block, "model_dump"):
        return block.model_dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(block, Mapping):
        return dict(block)
    return {
        "type": getattr(block, "type", None),
        "text": getattr(block, "text", None),
    }


def _model_or_mapping(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", by_alias=True)
    return dict(value) if isinstance(value, Mapping) else None


def _stdio_transport(
    *,
    command: str | None,
    args: Sequence[str] | None,
    env: Mapping[str, str] | None,
) -> Any:
    from mcp.client.stdio import StdioServerParameters, stdio_client

    return stdio_client(
        StdioServerParameters(
            command=str(command or ""),
            args=[str(item) for item in (args or ())],
            env=dict(env) if env is not None else None,
        )
    )


def _sse_transport(*, endpoint: str, headers: Mapping[str, str] | None) -> Any:
    from mcp.client.sse import sse_client

    if not endpoint:
        raise ValueError("MCP SSE transport requires an endpoint")
    return sse_client(url=endpoint, headers=dict(headers or {}))


@asynccontextmanager
async def _streamable_http_transport(
    *,
    endpoint: str,
    headers: Mapping[str, str] | None,
    follow_redirects: bool,
    http_transport: Any,
    trust_env: bool,
    timeout_seconds: float,
    read_timeout_seconds: float,
    connect_timeout_seconds: float | None,
    terminate_on_close: bool,
    transport_factory: TransportFactory,
) -> AsyncIterator[Any]:
    if not endpoint:
        raise ValueError("MCP streamable HTTP transport requires an endpoint")
    timeout = httpx2.Timeout(
        timeout_seconds,
        read=read_timeout_seconds,
        connect=(connect_timeout_seconds or timeout_seconds),
    )
    async with httpx2.AsyncClient(
        headers=dict(headers or {}),
        timeout=timeout,
        follow_redirects=bool(follow_redirects),
        transport=http_transport,
        trust_env=bool(trust_env),
    ) as http_client:
        async with transport_factory(
            endpoint,
            http_client=http_client,
            terminate_on_close=terminate_on_close,
        ) as streams:
            yield streams


class RemoteMcpConnectionError(RuntimeError):
    """A safe, classified failure while opening an MCP connection."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class McpProbeResult:
    tool_count: int
    server_name: str | None = None
    server_version: str | None = None


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


def _safe_connection_error(exc: Exception) -> RemoteMcpConnectionError:
    if isinstance(exc, httpx2.TimeoutException):
        return RemoteMcpConnectionError(
            "mcp_connection_timeout",
            "The remote MCP endpoint did not respond before the connection timeout.",
        )
    if isinstance(exc, httpx2.ConnectError):
        return RemoteMcpConnectionError(
            "mcp_endpoint_unreachable",
            "The remote MCP endpoint could not be reached.",
        )
    return RemoteMcpConnectionError(
        "mcp_connection_failed",
        "The remote MCP endpoint rejected or could not complete the MCP connection.",
    )


async def _consume_modern_tool_changes(
    client: Client,
    *,
    task_status: anyio.abc.TaskStatus[None] = anyio.TASK_STATUS_IGNORED,
) -> None:
    async with client.listen(tools_list_changed=True) as subscription:
        task_status.started()
        async for _event in subscription:
            pass


@asynccontextmanager
async def connect_remote_tools(
    *,
    endpoint: str,
    bearer: str,
    client_name: str,
    client_version: str,
    user_agent: str | None = None,
    message_handler: MessageHandler | None = None,
    timeout_seconds: float = 120.0,
    transport_factory: TransportFactory = streamable_http_client,
) -> AsyncIterator[tuple[ConnectedRemoteTools, Client]]:
    """Open one authenticated Streamable HTTP MCP connection.

    Authentication policy and bearer custody belong to the caller. This
    function only applies the supplied credential to the outbound transport.
    """

    endpoint_value = str(endpoint or "").strip()
    bearer_value = str(bearer or "").strip()
    client_name_value = str(client_name or "").strip()
    client_version_value = str(client_version or "").strip()
    if not endpoint_value or not bearer_value or not client_name_value or not client_version_value:
        raise ValueError("endpoint, bearer, client_name, and client_version are required")

    stack = AsyncExitStack()
    try:
        client = await stack.enter_async_context(
            open_mcp_client(
                transport="streamable-http",
                endpoint=endpoint_value,
                headers={
                    "Authorization": f"Bearer {bearer_value}",
                    "User-Agent": str(user_agent or client_name_value),
                },
                mode="auto",
                follow_redirects=False,
                trust_env=False,
                http_timeout_seconds=timeout_seconds,
                http_read_timeout_seconds=timeout_seconds,
                http_connect_timeout_seconds=15.0,
                terminate_on_close=True,
                message_handler=message_handler,
                client_info=types.Implementation(
                    name=client_name_value,
                    version=client_version_value,
                ),
                cache=None,
                streamable_http_transport_factory=transport_factory,
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


async def probe_remote_tools(
    *,
    endpoint: str,
    bearer: str,
    client_name: str,
    client_version: str,
    user_agent: str | None = None,
) -> McpProbeResult:
    async with connect_remote_tools(
        endpoint=endpoint,
        bearer=bearer,
        client_name=client_name,
        client_version=client_version,
        user_agent=user_agent,
    ) as (remote, client):
        tools = await remote.list_tools()
        server_info = client.server_info
        return McpProbeResult(
            tool_count=len(tools.tools),
            server_name=server_info.name if server_info else None,
            server_version=server_info.version if server_info else None,
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

"""Request-scoped host surface for the aggregate delegated MCP gateway.

This module intentionally contains no route decorators. CH Lead owns hosted
route registration after the Card read contract is ready.
"""

from __future__ import annotations

import inspect
import json
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from connection_hub.delegated_gateway import (
    DelegatedGatewayError,
    DelegatedMCPGateway,
    GatewayCaller,
    GatewayTool,
)

CallerResolver = Callable[[Any], GatewayCaller | Awaitable[GatewayCaller]]
_MCP_TOOL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")


def _request_message(body: bytes) -> Mapping[str, Any]:
    try:
        value = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, ValueError):
        return {}
    return value if isinstance(value, Mapping) else {}


def _invocation_id(message: Mapping[str, Any]) -> str:
    params = message.get("params")
    params = params if isinstance(params, Mapping) else {}
    meta = params.get("_meta")
    meta = meta if isinstance(meta, Mapping) else {}
    explicit = str(meta.get("connection_hub/invocation_id") or "").strip()
    if explicit:
        return explicit
    return f"mcp-generated-{uuid.uuid4().hex}"


async def _caller(request: Any, resolver: CallerResolver) -> GatewayCaller:
    if request is None:
        raise RuntimeError("delegated gateway requires an authenticated request")
    try:
        value = resolver(request)
        if inspect.isawaitable(value):
            value = await value
    except Exception:  # noqa: BLE001 - caller failures cross a public boundary
        raise RuntimeError("delegated gateway caller resolution failed") from None
    if not isinstance(value, GatewayCaller):
        raise TypeError("delegated gateway caller resolution failed")
    return value


def _fixed_gateway_failure() -> dict[str, Any]:
    return {
        "ok": False,
        "error": "delegated_mcp_gateway_failed",
        "reason": "delegated_mcp_gateway_failed",
        "retryable": True,
    }


def _bound_tool(
    *,
    tool: GatewayTool,
    gateway: DelegatedMCPGateway,
    caller: GatewayCaller,
    invocation_id: str,
) -> Any:
    try:
        from mcp.server.mcpserver.tools.base import Tool
        from mcp_types import CallToolResult, TextContent
    except Exception as exc:  # pragma: no cover - runtime dependency
        raise ImportError("MCP server SDK v2 is not installed") from exc

    async def _placeholder() -> dict[str, Any]:
        return {}

    base = Tool.from_function(
        _placeholder,
        name=tool.name,
        description=tool.description or tool.title,
    )

    class BoundDelegatedGatewayTool(Tool):
        async def run(self, arguments, context, convert_result=False):
            del context, convert_result
            try:
                gateway_result = await gateway.call_tool(
                    caller,
                    tool_name=tool.name,
                    arguments=dict(arguments or {}),
                    invocation_id=invocation_id,
                )
                value = gateway_result.to_public_dict()
                blocks = list(gateway_result.result.content)
                if not blocks:
                    blocks = [
                        TextContent(
                            type="text",
                            text=json.dumps(value, ensure_ascii=False),
                        )
                    ]
                return CallToolResult(
                    content=blocks,
                    structuredContent=value,
                    isError=gateway_result.result.is_error,
                )
            except DelegatedGatewayError as exc:
                value = exc.to_dict()
            except Exception:  # noqa: BLE001 - public MCP failures are fixed
                value = _fixed_gateway_failure()
            return CallToolResult(
                content=[
                    TextContent(type="text", text=json.dumps(value, ensure_ascii=False))
                ],
                structuredContent=value,
                isError=True,
            )

    route_meta = {}
    if tool.route is not None:
        route_meta = {
            "resource_id": tool.route.resource_id,
            "resource_kind": tool.route.resource_kind,
            "operation": tool.route.operation,
        }
    return BoundDelegatedGatewayTool(
        fn=base.fn,
        name=tool.name,
        title=tool.title,
        description=tool.description or tool.title,
        parameters=dict(tool.input_schema),
        fn_metadata=base.fn_metadata,
        is_async=True,
        meta={"connection_hub": route_meta},
    )


def _unknown_requested_tool(name: str) -> GatewayTool:
    safe_name = str(name or "").strip()
    if not _MCP_TOOL_NAME.fullmatch(safe_name):
        safe_name = "connection_hub_unknown_tool"
    return GatewayTool(
        name=safe_name,
        route=None,
        title="Unavailable delegated tool",
        description="Resolve this request against the caller's current card.",
        input_schema={"type": "object", "additionalProperties": True},
    )


async def build_delegated_mcp_gateway_app(
    *,
    request: Any,
    gateway: DelegatedMCPGateway,
    caller_resolver: CallerResolver,
) -> Any:
    """Build one request-scoped MCP server without registering its route."""

    try:
        from kdcube_ai_app.apps.chat.sdk.runtime.mcp.server import KDCubeMCPServer
    except Exception as exc:  # pragma: no cover - runtime dependency
        raise ImportError("MCP server SDK v2 is not installed") from exc

    resolved_caller = await _caller(request, caller_resolver)
    message = _request_message(await request.body())
    method = str(message.get("method") or "").strip()
    invocation_id = _invocation_id(message) if method == "tools/call" else ""
    selected: list[GatewayTool] = []
    if method == "tools/list":
        selected.extend(await gateway.list_tools(resolved_caller))
    elif method == "tools/call":
        params = message.get("params")
        params = params if isinstance(params, Mapping) else {}
        requested = str(params.get("name") or "").strip()
        try:
            current = {
                tool.name: tool for tool in await gateway.list_tools(resolved_caller)
            }
        except DelegatedGatewayError:
            current = {}
        selected.append(current.get(requested) or _unknown_requested_tool(requested))

    tools = [
        _bound_tool(
            tool=tool,
            gateway=gateway,
            caller=resolved_caller,
            invocation_id=invocation_id,
        )
        for tool in selected
    ]
    return KDCubeMCPServer(
        "Connection Hub delegated MCP gateway",
        description=(
            "Live delegated access to every compatible resource on the "
            "authenticated caller's current Connection Hub card."
        ),
        tools=tools,
    )


async def describe_delegated_gateway_access(
    *,
    request: Any,
    gateway: DelegatedMCPGateway,
    caller_resolver: CallerResolver,
    include_requestable: bool = False,
) -> dict[str, Any]:
    """Normal API handler contract for caller-self authority discovery."""

    if not isinstance(include_requestable, bool):
        return {
            "ok": False,
            "error": "delegated_mcp_gateway_denied",
            "code": "access_describe_arguments_invalid",
            "reason": "access_describe_arguments_invalid",
            "retryable": False,
        }
    try:
        resolved_caller = await _caller(request, caller_resolver)
        access = await gateway.describe_access(
            resolved_caller, include_requestable=include_requestable
        )
    except DelegatedGatewayError as exc:
        return exc.to_dict()
    except Exception:  # noqa: BLE001 - public API failures are fixed
        return _fixed_gateway_failure()
    return {"ok": True, "access": access}


__all__ = [
    "build_delegated_mcp_gateway_app",
    "describe_delegated_gateway_access",
]

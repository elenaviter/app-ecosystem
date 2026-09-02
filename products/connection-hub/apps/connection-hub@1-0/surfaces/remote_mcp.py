"""Request-scoped MCP server for delegated calls to user-owned connectors."""

from __future__ import annotations

import json
import uuid
from typing import Any, Awaitable, Callable, Mapping

from connection_hub.delegated_credentials.credential_view import (
    DelegatedCredentialView,
)
from connection_hub.remote_mcp import (
    RemoteMCPProxy,
    RemoteMCPProxyError,
    connector_id_from_resource,
)


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
    # Standard MCP clients do not yet create a Connection Hub id. Give every
    # call one so a once policy remains usable; clients that need replay after
    # an uncertain response send the explicit metadata key above.
    return f"mcp-generated-{uuid.uuid4().hex}"


def _public_upstream_failure(exc: Exception) -> dict[str, Any]:
    return {
        "ok": False,
        "error": "remote_mcp_call_failed",
        "reason": "remote_mcp_call_failed",
        "failure_type": type(exc).__name__,
        "retryable": True,
    }


async def _known_requested_tool(
    *,
    service: Any,
    view: DelegatedCredentialView,
    proxy_name: str,
) -> Any | None:
    owner = str(view.grantor_user_id or "").strip()
    if not owner:
        return None
    for resource in view.resource_grants:
        connector_id = connector_id_from_resource(resource)
        if not connector_id:
            continue
        try:
            connector = await service.get(
                owner_subject=owner, connector_id=connector_id
            )
        except Exception:
            continue
        tool = connector.proxy_tool_map().get(proxy_name)
        if tool is not None:
            return tool
    return None


def _proxy_tool(
    *,
    tool: Any,
    callback: Callable[[Mapping[str, Any]], Awaitable[Any]],
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
        name=tool.proxy_name,
        description=tool.description or tool.name,
    )

    class BoundProxyTool(Tool):
        async def run(self, arguments, context, convert_result=False):
            del context, convert_result
            is_error = False
            try:
                value = await callback(dict(arguments or {}))
            except RemoteMCPProxyError as exc:
                value = exc.to_dict()
                is_error = True
            except Exception as exc:  # upstream failures are tool failures
                value = _public_upstream_failure(exc)
                is_error = True
            text = json.dumps(value, ensure_ascii=False, default=str)
            return CallToolResult(
                content=[TextContent(type="text", text=text)],
                structuredContent=value,
                isError=is_error,
            )

    return BoundProxyTool(
        fn=base.fn,
        name=tool.proxy_name,
        title=tool.name,
        description=tool.description or tool.name,
        parameters=dict(tool.input_schema),
        fn_metadata=base.fn_metadata,
        is_async=True,
        meta={
            "connection_hub": {
                "upstream_tool": tool.name,
            }
        },
    )


async def build_remote_mcp_proxy_app(
    *,
    request: Any,
    service: Any,
    invocation_policies: Any = None,
    recovery_url_builder: Any = None,
    grant_url_builder: Any = None,
) -> Any:
    try:
        from kdcube_ai_app.apps.chat.sdk.runtime.mcp.server import KDCubeMCPServer
    except Exception as exc:  # pragma: no cover - runtime dependency
        raise ImportError("MCP server SDK v2 is not installed") from exc

    if request is None:
        raise RuntimeError("remote MCP proxy requires an authenticated request")
    view = DelegatedCredentialView.from_request(request)
    proxy = RemoteMCPProxy(
        service,
        invocation_policies=invocation_policies,
        recovery_url_builder=recovery_url_builder,
        grant_url_builder=grant_url_builder,
    )
    message = _request_message(await request.body())
    method = str(message.get("method") or "").strip()
    invocation_id = _invocation_id(message) if method == "tools/call" else ""

    selected_tools: dict[str, Any] = {}
    if method == "tools/list":
        for decision in await proxy.list_authorized(view):
            selected_tools[decision.tool.proxy_name] = decision.tool
    elif method == "tools/call":
        params = message.get("params")
        params = params if isinstance(params, Mapping) else {}
        requested = str(params.get("name") or "").strip()
        tool = await _known_requested_tool(
            service=service, view=view, proxy_name=requested
        )
        if tool is not None:
            selected_tools[tool.proxy_name] = tool

    tools = [
        _proxy_tool(
            tool=tool,
            callback=lambda arguments, proxy_name=tool.proxy_name: proxy.call(
                view=view,
                proxy_name=proxy_name,
                arguments=arguments,
                invocation_id=invocation_id,
            ),
        )
        for tool in selected_tools.values()
    ]
    return KDCubeMCPServer(
        "Connection Hub remote MCP proxy",
        description=(
            "Delegated access to the exact tools selected on the caller's live "
            "Connection Hub card."
        ),
        tools=tools,
    )


__all__ = ["build_remote_mcp_proxy_app"]

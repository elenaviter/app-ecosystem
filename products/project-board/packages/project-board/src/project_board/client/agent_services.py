"""The services an agent's Card allows, reached through Connection Hub (W918).

A Problem Board agent session calls every service its live Card allows through
one door: ``pb worker services``, ``service-describe`` and ``service-call``.
The relay that already holds this worker's Card session forwards each request
to Connection Hub's ``delegated_mcp_gateway`` with the Card's own bearer, so
the credential never reaches the model and nothing is configured in the agent
runtime ("codex has nothing to do with that. it should call this service from
kdcube", operator, 2026-10-23). Connection Hub decides what is listed and what
a call may do from the composed Card: the Card, its project Card (AND or OR),
and the project's External MCP switch.

The board's own MCP surface is hidden here: the board stays on
``pb coordinate``. Results are bounded and upstream text is never echoed in a
refusal (W918 design revision 2).
"""

from __future__ import annotations

import json
from contextlib import AbstractAsyncContextManager
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from ..contract.errors import DomainError
from .github_key import CONNECTION_HUB_BUNDLE_ID

GATEWAY_ALIAS = "delegated_mcp_gateway"
ACTION_PREFIX = "pb.agent_services."
LIST_ACTION = ACTION_PREFIX + "list"
DESCRIBE_ACTION = ACTION_PREFIX + "describe"
CALL_ACTION = ACTION_PREFIX + "call"
ACTIONS = frozenset({LIST_ACTION, DESCRIBE_ACTION, CALL_ACTION})
# The gateway's meta-tool describes the caller's own access; the list carries it separately.
ACCESS_DESCRIBE_TOOL = "connection_hub_access_describe"
INVOCATION_META_KEY = "connection_hub/invocation_id"
BOARD_SURFACE_MARK = "/public/mcp/problem_board"
MAX_RESULT_BYTES = 64 * 1024
MAX_LIST_PAGES = 16
MAX_ARGUMENT_BYTES = 256 * 1024

OpenTools = Callable[[], AbstractAsyncContextManager[Any]]


def gateway_url(
    endpoint: str, tenant: str, platform_project: str, bundle_id: str = CONNECTION_HUB_BUNDLE_ID
) -> str:
    """Connection Hub's delegated MCP gateway on the platform the board endpoint names."""

    parts = urlsplit(endpoint)
    return (
        f"{parts.scheme}://{parts.netloc}/api/integrations/bundles/"
        f"{tenant}/{platform_project}/{bundle_id}/public/mcp/{GATEWAY_ALIAS}"
    )


def is_agent_services_action(action: str) -> bool:
    return str(action or "") in ACTIONS


def _dump(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dict(dump(by_alias=True, mode="json", exclude_none=True))
    return {}


def _route(tool: Mapping[str, Any]) -> dict[str, Any]:
    meta = tool.get("_meta") if isinstance(tool.get("_meta"), Mapping) else {}
    route = meta.get("connection_hub") if isinstance(meta.get("connection_hub"), Mapping) else {}
    return dict(route)


def _is_board_surface(route: Mapping[str, Any]) -> bool:
    return BOARD_SURFACE_MARK in str(route.get("resource_id") or "")


def _service_row(tool: Mapping[str, Any]) -> dict[str, Any]:
    route = _route(tool)
    schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), Mapping) else {}
    properties = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
    return {
        "name": str(tool.get("name") or ""),
        "title": str(tool.get("title") or tool.get("name") or ""),
        "resource_kind": str(route.get("resource_kind") or ""),
        "resource": str(route.get("resource_id") or ""),
        "operation": str(route.get("operation") or ""),
        "arguments": sorted(str(key) for key in properties),
        "required": sorted(str(key) for key in (schema.get("required") or ()) if str(key)),
    }


async def _listed_tools(remote: Any) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(MAX_LIST_PAGES):
        params = None
        if cursor:
            from mcp import types

            params = types.PaginatedRequestParams(cursor=cursor)
        page = _dump(await remote.list_tools(params=params))
        tools.extend(_dump(tool) for tool in page.get("tools") or ())
        cursor = str(page.get("nextCursor") or "") or None
        if not cursor:
            break
    return tools


def _offered(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        tool for tool in tools
        if str(tool.get("name") or "") != ACCESS_DESCRIBE_TOOL and not _is_board_surface(_route(tool))
    ]


def _bounded_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """The call's result as the agent may read it, at most MAX_RESULT_BYTES."""

    structured = result.get("structuredContent")
    texts = [
        str(item.get("text") or "")
        for item in result.get("content") or ()
        if isinstance(item, Mapping) and item.get("type") == "text"
    ]
    body = structured if structured is not None else "\n".join(texts)
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True)
    truncated = len(encoded.encode("utf-8")) > MAX_RESULT_BYTES
    if truncated:
        body = encoded.encode("utf-8")[:MAX_RESULT_BYTES].decode("utf-8", errors="ignore")
    route = _route(result)
    return {
        "result": body,
        "truncated": truncated,
        "result_bytes": len(encoded.encode("utf-8")),
        "invocation_id": str(route.get("invocation_id") or ""),
        "replay": bool(route.get("replay")),
    }


def _refusal(result: Mapping[str, Any]) -> DomainError:
    """A refused call as a fixed code; upstream text is never echoed (W918)."""

    structured = result.get("structuredContent") if isinstance(result.get("structuredContent"), Mapping) else {}
    error = structured.get("error") if isinstance(structured.get("error"), Mapping) else structured
    code = str(error.get("code") or error.get("reason") or "agent_service_refused")
    details: dict[str, Any] = {}
    for key in ("recovery_url", "authorization_url", "invocation_id", "resource", "operation"):
        value = error.get(key)
        if isinstance(value, str) and value:
            details[key] = value[:2048]
    recovery = error.get("recovery")
    if isinstance(recovery, Mapping):
        details["recovery"] = {
            str(key): str(value)[:2048]
            for key, value in recovery.items()
            if isinstance(value, (str, int)) and key in {"type", "reason", "authorization_url", "invocation_id", "expires_at"}
        }
    return DomainError(
        code if code.replace("_", "").isalnum() else "agent_service_refused",
        "Connection Hub refused this service call; the code and recovery link say why.",
        status=403,
        details=details,
    )


async def run_agent_services(action: str, payload: Mapping[str, Any], *, open_tools: OpenTools) -> dict[str, Any]:
    """Answer one agent-services request through the gateway session ``open_tools`` opens."""

    if action not in ACTIONS:
        raise DomainError("work_agent_services_action_unknown", "Unknown agent services request.", status=400)
    async with open_tools() as (remote, _client):
        tools = _offered(await _listed_tools(remote))
        if action == LIST_ACTION:
            return {"services": [_service_row(tool) for tool in tools]}
        name = str(payload.get("name") or "").strip()
        tool = next((item for item in tools if str(item.get("name") or "") == name), None)
        if tool is None:
            raise DomainError(
                "agent_service_not_on_card",
                "The Card does not allow this service now; pb worker services lists what it allows.",
                status=404,
                details={"name": name[:128]},
            )
        if action == DESCRIBE_ACTION:
            row = _service_row(tool)
            row["description"] = str(tool.get("description") or "")[:4096]
            row["input_schema"] = tool.get("inputSchema") or {}
            return {"service": row}
        arguments = payload.get("arguments")
        if not isinstance(arguments, Mapping):
            raise DomainError("agent_service_arguments_invalid", "Arguments must be a JSON object.", status=400)
        if len(json.dumps(arguments).encode("utf-8")) > MAX_ARGUMENT_BYTES:
            raise DomainError("agent_service_arguments_too_large", "Arguments exceed 256 KiB.", status=413)
        invocation_id = str(payload.get("invocation_id") or "").strip()
        if not invocation_id:
            raise DomainError("agent_service_invocation_id_missing", "A call needs its invocation id.", status=400)
        result = _dump(await remote.call_tool(
            name=name,
            arguments=dict(arguments),
            input_responses=None,
            request_state=None,
            meta={INVOCATION_META_KEY: invocation_id},
            progress_callback=None,
        ))
        if result.get("isError"):
            raise _refusal(result)
        answer = _bounded_result(result)
        answer["invocation_id"] = answer["invocation_id"] or invocation_id
        answer["name"] = name
        return answer


__all__ = [
    "ACTIONS",
    "CALL_ACTION",
    "DESCRIBE_ACTION",
    "LIST_ACTION",
    "MAX_RESULT_BYTES",
    "gateway_url",
    "is_agent_services_action",
    "run_agent_services",
]

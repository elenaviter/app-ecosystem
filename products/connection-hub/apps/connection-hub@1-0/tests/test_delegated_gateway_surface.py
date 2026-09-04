from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from connection_hub.delegated_gateway import (
    DelegatedGatewayError,
    GatewayCaller,
    GatewayCallResult,
    GatewayTool,
    GatewayToolRoute,
    ProviderCallResult,
)


def _install_fake_mcp(monkeypatch):
    class Tool:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

        @classmethod
        def from_function(cls, fn, *, name, description):
            return cls(
                fn=fn,
                name=name,
                description=description,
                fn_metadata={"fixture": True},
            )

    class CallToolResult:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class TextContent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class KDCubeMCPServer:
        def __init__(self, name, *, description, tools):
            self.name = name
            self.description = description
            self.tools = tools

    modules = {
        "mcp": ModuleType("mcp"),
        "mcp.server": ModuleType("mcp.server"),
        "mcp.server.mcpserver": ModuleType("mcp.server.mcpserver"),
        "mcp.server.mcpserver.tools": ModuleType("mcp.server.mcpserver.tools"),
        "mcp.server.mcpserver.tools.base": ModuleType(
            "mcp.server.mcpserver.tools.base"
        ),
        "mcp_types": ModuleType("mcp_types"),
        "kdcube_ai_app": ModuleType("kdcube_ai_app"),
        "kdcube_ai_app.apps": ModuleType("kdcube_ai_app.apps"),
        "kdcube_ai_app.apps.chat": ModuleType("kdcube_ai_app.apps.chat"),
        "kdcube_ai_app.apps.chat.sdk": ModuleType("kdcube_ai_app.apps.chat.sdk"),
        "kdcube_ai_app.apps.chat.sdk.runtime": ModuleType(
            "kdcube_ai_app.apps.chat.sdk.runtime"
        ),
        "kdcube_ai_app.apps.chat.sdk.runtime.mcp": ModuleType(
            "kdcube_ai_app.apps.chat.sdk.runtime.mcp"
        ),
        "kdcube_ai_app.apps.chat.sdk.runtime.mcp.server": ModuleType(
            "kdcube_ai_app.apps.chat.sdk.runtime.mcp.server"
        ),
    }
    modules["mcp.server.mcpserver.tools.base"].Tool = Tool
    modules["mcp_types"].CallToolResult = CallToolResult
    modules["mcp_types"].TextContent = TextContent
    modules[
        "kdcube_ai_app.apps.chat.sdk.runtime.mcp.server"
    ].KDCubeMCPServer = KDCubeMCPServer
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)


def _surface_module(monkeypatch):
    _install_fake_mcp(monkeypatch)
    path = Path(__file__).resolve().parents[1] / "surfaces" / "delegated_gateway.py"
    spec = importlib.util.spec_from_file_location(
        "delegated_gateway_surface_test", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _caller() -> GatewayCaller:
    return GatewayCaller(
        caller_type="resident",
        access_id="access-1",
        caller_profile_id="workspace:researcher",
    )


def _tool() -> GatewayTool:
    return GatewayTool(
        name="ch_fake_0123456789abcdef__search_0123456789abcdef",
        route=GatewayToolRoute(
            resource_id="urn:test:one",
            resource_kind="fake",
            operation="search",
            accepted_descriptor_identity="a" * 64,
            provider_id="fake",
        ),
        title="Search",
        description="Search one source",
        input_schema={"type": "object"},
    )


class _Request:
    def __init__(self, message):
        self._body = json.dumps(message).encode("utf-8")

    async def body(self):
        return self._body


class _Gateway:
    def __init__(self):
        self.calls = []
        self.tools = (_tool(),)

    async def list_tools(self, caller):
        assert caller == _caller()
        return self.tools

    async def call_tool(self, caller, *, tool_name, arguments, invocation_id):
        assert caller == _caller()
        self.calls.append(
            {
                "tool_name": tool_name,
                "arguments": arguments,
                "invocation_id": invocation_id,
            }
        )
        if tool_name == "removed-cached-tool":
            raise DelegatedGatewayError("tool_not_in_current_card", tool_name=tool_name)
        return GatewayCallResult(
            result=ProviderCallResult.from_value({"ok": True}),
            access_id="access-1",
            card_revision=2,
            resource_id="urn:test:one",
            resource_kind="fake",
            operation="search",
            tool_name=tool_name,
            invocation_id=invocation_id,
            provider_id="fake",
            descriptor_revision="v1",
            descriptor_digest="b" * 64,
        )

    async def describe_access(self, caller, *, include_requestable=False):
        assert caller == _caller()
        return {
            "access_id": caller.access_id,
            "include_requestable": include_requestable,
        }


@pytest.mark.asyncio
async def test_builder_lists_request_scoped_gateway_tools(monkeypatch):
    module = _surface_module(monkeypatch)
    gateway = _Gateway()
    request = _Request({"jsonrpc": "2.0", "method": "tools/list", "id": 1})

    server = await module.build_delegated_mcp_gateway_app(
        request=request,
        gateway=gateway,
        caller_resolver=lambda _request: _caller(),
    )

    assert server.name == "Connection Hub delegated MCP gateway"
    assert [tool.name for tool in server.tools] == [_tool().name]


@pytest.mark.asyncio
async def test_builder_forwards_stable_invocation_id_and_returns_bounded_result(
    monkeypatch,
):
    module = _surface_module(monkeypatch)
    gateway = _Gateway()
    request = _Request(
        {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 2,
            "params": {
                "name": _tool().name,
                "arguments": {"query": "value"},
                "_meta": {"connection_hub/invocation_id": "client-call-1"},
            },
        }
    )
    server = await module.build_delegated_mcp_gateway_app(
        request=request,
        gateway=gateway,
        caller_resolver=lambda _request: _caller(),
    )

    result = await server.tools[0].run({"query": "value"}, context=SimpleNamespace())

    assert gateway.calls == [
        {
            "tool_name": _tool().name,
            "arguments": {"query": "value"},
            "invocation_id": "client-call-1",
        }
    ]
    assert result.isError is False
    assert result.structuredContent["structured_content"] == {"ok": True}


@pytest.mark.asyncio
async def test_cached_unknown_tool_reaches_live_gateway_denial(monkeypatch):
    module = _surface_module(monkeypatch)
    gateway = _Gateway()
    gateway.tools = ()
    request = _Request(
        {
            "method": "tools/call",
            "params": {"name": "removed-cached-tool", "arguments": {}},
        }
    )
    server = await module.build_delegated_mcp_gateway_app(
        request=request,
        gateway=gateway,
        caller_resolver=lambda _request: _caller(),
    )

    result = await server.tools[0].run({}, context=SimpleNamespace())

    assert result.isError is True
    assert result.structuredContent["code"] == "tool_not_in_current_card"
    assert gateway.calls[0]["invocation_id"].startswith("mcp-generated-")


@pytest.mark.asyncio
async def test_normal_api_contract_uses_same_caller_self_view(monkeypatch):
    module = _surface_module(monkeypatch)
    response = await module.describe_delegated_gateway_access(
        request=SimpleNamespace(),
        gateway=_Gateway(),
        caller_resolver=lambda _request: _caller(),
        include_requestable=True,
    )

    assert response == {
        "ok": True,
        "access": {"access_id": "access-1", "include_requestable": True},
    }

    invalid = await module.describe_delegated_gateway_access(
        request=SimpleNamespace(),
        gateway=_Gateway(),
        caller_resolver=lambda _request: _caller(),
        include_requestable="false",
    )
    assert invalid["code"] == "access_describe_arguments_invalid"


@pytest.mark.asyncio
async def test_caller_resolution_failure_is_secret_safe(monkeypatch):
    module = _surface_module(monkeypatch)
    secret = "caller-token-must-not-leak"

    def failed(_request):
        raise RuntimeError(secret)

    response = await module.describe_delegated_gateway_access(
        request=SimpleNamespace(),
        gateway=_Gateway(),
        caller_resolver=failed,
    )

    assert response["reason"] == "delegated_mcp_gateway_failed"
    assert secret not in repr(response)


@pytest.mark.asyncio
async def test_builder_conforms_to_real_kdcube_mcp_server_when_available():
    pytest.importorskip("kdcube_ai_app.apps.chat.sdk.runtime.mcp.server")
    path = Path(__file__).resolve().parents[1] / "surfaces" / "delegated_gateway.py"
    spec = importlib.util.spec_from_file_location(
        "delegated_gateway_surface_real_sdk_test", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    gateway = _Gateway()
    server = await module.build_delegated_mcp_gateway_app(
        request=_Request(
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "id": 1,
                "params": {
                    "name": _tool().name,
                    "arguments": {"query": "real-sdk"},
                    "_meta": {"connection_hub/invocation_id": "real-sdk-call"},
                },
            }
        ),
        gateway=gateway,
        caller_resolver=lambda _request: _caller(),
    )
    capabilities = server._lowlevel_server.get_capabilities(
        protocol_version="2026-07-28"
    )
    registered = server._tool_manager.list_tools()
    result = await registered[0].run({"query": "real-sdk"}, context=SimpleNamespace())
    wire_result = result.model_dump(by_alias=True)

    assert capabilities.tools is not None
    assert capabilities.tools.list_changed is True
    assert [tool.name for tool in registered] == [_tool().name]
    assert wire_result["isError"] is False
    assert wire_result["structuredContent"]["structured_content"] == {"ok": True}
    assert gateway.calls[0]["arguments"] == {"query": "real-sdk"}
    assert gateway.calls[0]["invocation_id"] == "real-sdk-call"

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

import pytest
from connection_hub.delegated_gateway import (
    AcceptedDescriptor,
    DelegatedMCPGateway,
    DelegatedMCPProviderRegistry,
    DelegatedResourceEntry,
    MemoryGatewayAuditSink,
    ProviderCallAdmission,
    ProviderCallResult,
    ProviderDescriptor,
    ProviderTool,
)
from connection_hub.delegated_gateway.ports import GatewayProviderContext
from connection_hub.delegated_gateway.providers import (
    ExternalRemoteMCPProvider,
    ManagedKDCubeMCPProvider,
)
from connection_hub.remote_mcp import (
    EXTERNAL_MCP_GRANT,
    RemoteMCPConnector,
    RemoteMCPDiscovery,
    RemoteMCPProxy,
)
from connection_hub.remote_mcp.models import proxy_tool_name

from .fakes import NOW, MemoryPolicy, MutableCardReader, caller, card, resource


def _connector_and_discovery():
    discovery = RemoteMCPDiscovery.build(
        connector_id="mcp_0123456789abcdef01234567",
        tools=[
            {
                "name": "records.search",
                "description": "Search records",
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
            },
            {
                "name": "records.read",
                "description": "Read one record",
                "input_schema": {"type": "object"},
            },
        ],
        server_name="Fixture",
        server_version="1",
        protocol_version="2026-07-28",
    )
    connector = RemoteMCPConnector(
        connector_id="mcp_0123456789abcdef01234567",
        owner_subject="owner-secret-subject",
        label="Records",
        endpoint="https://mcp.example.test/mcp",
        transport="streamable-http",
        resource=("urn:connection-hub:remote-mcp:mcp_0123456789abcdef01234567"),
        revision=3,
        state="active",
        credential_mode="none",
        tools=discovery.tools,
        descriptor_digest=discovery.descriptor_digest,
        descriptor_revision=2,
        descriptor_state="accepted",
        created_at=1,
        updated_at=1,
        last_checked_at=1,
    )
    return connector, discovery


def _external_resource(connector: RemoteMCPConnector) -> DelegatedResourceEntry:
    operations = tuple(tool.name for tool in connector.tools)
    return DelegatedResourceEntry(
        resource_id=connector.resource,
        kind="remote_mcp",
        display_label="Records",
        endpoint_relation="delegated_mcp_gateway",
        grants=(EXTERNAL_MCP_GRANT,),
        operations=operations,
        accepted_descriptor=AcceptedDescriptor(
            revision=str(connector.descriptor_revision),
            digest=connector.descriptor_digest,
            operation_digests={
                tool.name: tool.descriptor_digest for tool in connector.tools
            },
        ),
        identity_scope="grantor",
        invocation_policies={
            operation: resource(
                f"urn:policy:{operation}", operations=(operation,)
            ).invocation_policies[operation]
            for operation in operations
        },
    )


@dataclass
class _RemoteService:
    connector: RemoteMCPConnector
    discovery: RemoteMCPDiscovery
    calls: list[dict[str, Any]] = field(default_factory=list)
    fail_observe: bool = False

    async def get(self, *, owner_subject: str, connector_id: str):
        if (
            owner_subject != self.connector.owner_subject
            or connector_id != self.connector.connector_id
        ):
            raise LookupError("connector_not_found")
        return self.connector

    async def observe(self, connector):
        assert connector is self.connector
        if self.fail_observe:
            raise RuntimeError("provider-secret-marker")
        return self.discovery

    async def call_tool(self, *, connector, tool_name: str, arguments):
        assert connector is self.connector
        call = {"tool_name": tool_name, "arguments": dict(arguments)}
        self.calls.append(call)
        return {"ok": True, **call}


@pytest.mark.asyncio
async def test_external_adapter_preserves_legacy_proxy_and_adds_aggregate_name():
    connector, discovery = _connector_and_discovery()
    service = _RemoteService(connector, discovery)
    provider = ExternalRemoteMCPProvider(service, proxy=RemoteMCPProxy(service))
    entry = _external_resource(connector)
    gateway = DelegatedMCPGateway(
        cards=MutableCardReader(card(entry)),
        providers=DelegatedMCPProviderRegistry([provider]),
        invocation_policy=MemoryPolicy(),
        audit=MemoryGatewayAuditSink(),
        clock=lambda: NOW,
    )

    tools = await gateway.list_tools(caller())
    selected = next(
        tool
        for tool in tools
        if tool.route is not None and tool.route.operation == "records.search"
    )
    result = await gateway.call_tool(
        caller(),
        tool_name=selected.name,
        arguments={"query": "failed jobs"},
        invocation_id="external-1",
    )

    legacy_name = connector.tool_map()["records.search"].proxy_name
    assert legacy_name == proxy_tool_name(connector.connector_id, "records.search")
    assert selected.name != legacy_name
    assert service.calls == [
        {"tool_name": "records.search", "arguments": {"query": "failed jobs"}}
    ]
    assert result.result.structured_content["ok"] is True
    description = await gateway.describe_access(caller())
    assert description["resources"][0]["current_descriptor"]["revision"] == "2"


@pytest.mark.asyncio
async def test_external_adapter_hides_live_drift_and_sanitizes_readiness_failure():
    connector, discovery = _connector_and_discovery()
    service = _RemoteService(connector, discovery)
    provider = ExternalRemoteMCPProvider(service)
    entry = _external_resource(connector)
    cards = MutableCardReader(card(entry))
    gateway = DelegatedMCPGateway(
        cards=cards,
        providers=DelegatedMCPProviderRegistry([provider]),
        invocation_policy=MemoryPolicy(),
        audit=MemoryGatewayAuditSink(),
        clock=lambda: NOW,
    )

    changed = RemoteMCPDiscovery.build(
        connector_id=connector.connector_id,
        tools=[
            {
                "name": "records.search",
                "description": "Changed without acceptance",
                "input_schema": {"type": "object"},
            },
            connector.tool_map()["records.read"],
        ],
    )
    service.discovery = changed
    tools = await gateway.list_tools(caller())
    operations = {tool.route.operation for tool in tools if tool.route is not None}
    assert operations == {"records.read"}

    service.fail_observe = True
    description = await gateway.describe_access(caller())
    assert description["resources"][0]["unavailable_reason"] == (
        "resource_provider_unavailable"
    )
    assert "provider-secret-marker" not in repr(description)


@pytest.mark.asyncio
async def test_external_adapter_lists_only_resources_with_existing_proxy_grant():
    connector, discovery = _connector_and_discovery()
    service = _RemoteService(connector, discovery)
    provider = ExternalRemoteMCPProvider(service)
    entry = replace(_external_resource(connector), grants=())
    gateway = DelegatedMCPGateway(
        cards=MutableCardReader(card(entry)),
        providers=DelegatedMCPProviderRegistry([provider]),
        invocation_policy=MemoryPolicy(),
        audit=MemoryGatewayAuditSink(),
        clock=lambda: NOW,
    )

    tools = await gateway.list_tools(caller())

    assert all(tool.route is None for tool in tools)
    assert service.calls == []


@dataclass
class _ManagedHost:
    entry: DelegatedResourceEntry
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def current_descriptor(self, context: GatewayProviderContext):
        return ProviderDescriptor(
            resource_id=context.resource.resource_id,
            revision=self.entry.accepted_descriptor.revision,
            digest=self.entry.accepted_descriptor.digest,
            operation_digests=self.entry.accepted_descriptor.operation_digests,
        )

    async def list_tools(self, context: GatewayProviderContext):
        return tuple(
            # Reuse the exact operation digest accepted on the managed row.
            replace_tool_digest(operation, self.entry)
            for operation in context.resource.operations
        )

    async def admit_call(
        self,
        context: GatewayProviderContext,
        *,
        operation: str,
        arguments: Mapping[str, Any],
        invocation_id: str,
    ) -> ProviderCallAdmission:
        del context, operation, arguments, invocation_id
        return ProviderCallAdmission(allowed=True)

    async def call_tool(
        self,
        context: GatewayProviderContext,
        *,
        operation: str,
        arguments: Mapping[str, Any],
        invocation_id: str,
    ) -> ProviderCallResult:
        self.calls.append(
            {
                "caller": context.caller.caller_profile_id,
                "access_id": context.card.access_id,
                "resource_id": context.resource.resource_id,
                "operation": operation,
                "arguments": dict(arguments),
                "invocation_id": invocation_id,
            }
        )
        return ProviderCallResult.from_value({"ok": True})


def replace_tool_digest(operation: str, entry: DelegatedResourceEntry):
    return ProviderTool(
        operation=operation,
        descriptor_digest=entry.accepted_descriptor.operation_digests[operation],
        title=operation,
        input_schema={"type": "object"},
    )


@pytest.mark.asyncio
async def test_managed_provider_passes_full_context_through_host_port():
    entry = resource(
        "urn:kdcube:knowledge",
        kind="managed_kdcube_mcp",
        operations=("knowledge.search",),
    )
    host = _ManagedHost(entry)
    provider = ManagedKDCubeMCPProvider(host)
    gateway = DelegatedMCPGateway(
        cards=MutableCardReader(card(entry)),
        providers=DelegatedMCPProviderRegistry([provider]),
        invocation_policy=MemoryPolicy(),
        audit=MemoryGatewayAuditSink(),
        clock=lambda: NOW,
    )
    selected = next(
        tool for tool in await gateway.list_tools(caller()) if tool.route is not None
    )

    await gateway.call_tool(
        caller(),
        tool_name=selected.name,
        arguments={"value": "query"},
        invocation_id="managed-1",
    )

    assert host.calls == [
        {
            "caller": "workspace:researcher",
            "access_id": "agent-access-1",
            "resource_id": "urn:kdcube:knowledge",
            "operation": "knowledge.search",
            "arguments": {"value": "query"},
            "invocation_id": "managed-1",
        }
    ]

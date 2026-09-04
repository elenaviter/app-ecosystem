# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Injected authority, provider, policy, audit, and host ports for Gateway."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from connection_hub.delegated_gateway.models import (
    DelegatedCardView,
    DelegatedResourceEntry,
    GatewayCaller,
    InvocationPolicyView,
    ProviderCallAdmission,
    ProviderCallResult,
    ProviderDescriptor,
    ProviderTool,
    RequestableResource,
)


@dataclass(frozen=True)
class GatewayProviderContext:
    caller: GatewayCaller
    card: DelegatedCardView
    resource: DelegatedResourceEntry


@dataclass(frozen=True)
class GatewayInvocationRequest:
    caller: GatewayCaller
    card: DelegatedCardView
    resource: DelegatedResourceEntry
    provider_id: str
    operation: str
    tool_name: str
    invocation_id: str
    request_digest: str
    authority_revision: str


@dataclass(frozen=True)
class GatewayInvocationDecision:
    dispatch: bool
    replay: bool = False
    result: ProviderCallResult | None = None
    reason: str = ""
    retryable: bool = False
    public_policy: InvocationPolicyView | None = None


@dataclass(frozen=True)
class GatewayAuditEvent:
    phase: str
    caller_type: str
    caller_profile_id: str
    access_id: str
    card_revision: int
    resource_id: str
    resource_kind: str
    provider_id: str
    operation: str
    tool_name: str
    invocation_id: str
    descriptor_revision: str
    descriptor_digest: str
    policy_revision: int = 0
    outcome: str = ""
    replay: bool = False


class DelegatedCardReader(Protocol):
    async def read_current(self, caller: GatewayCaller) -> DelegatedCardView | None: ...


class DelegatedMCPResourceProvider(Protocol):
    @property
    def provider_id(self) -> str: ...

    @property
    def resource_kinds(self) -> frozenset[str]: ...

    async def current_descriptor(
        self, context: GatewayProviderContext
    ) -> ProviderDescriptor: ...

    async def list_tools(
        self, context: GatewayProviderContext
    ) -> Sequence[ProviderTool]: ...

    async def admit_call(
        self,
        context: GatewayProviderContext,
        *,
        operation: str,
        arguments: Mapping[str, Any],
        invocation_id: str,
    ) -> ProviderCallAdmission: ...

    async def call_tool(
        self,
        context: GatewayProviderContext,
        *,
        operation: str,
        arguments: Mapping[str, Any],
        invocation_id: str,
    ) -> ProviderCallResult: ...


class GatewayInvocationPolicy(Protocol):
    async def begin(
        self, request: GatewayInvocationRequest
    ) -> GatewayInvocationDecision: ...

    async def complete(
        self,
        request: GatewayInvocationRequest,
        *,
        result: ProviderCallResult,
    ) -> None: ...


class GatewayAuditSink(Protocol):
    async def record(self, event: GatewayAuditEvent) -> None: ...


class RequestableResourceReader(Protocol):
    async def list_requestable(
        self, *, caller: GatewayCaller, card: DelegatedCardView
    ) -> Sequence[RequestableResource]: ...


class ManagedKDCubeMCPHost(Protocol):
    """Trusted in-runtime calls for one cataloged managed MCP surface.

    Implementations retain the managed surface's own admission boundary and
    receive caller/card/invocation context without a provider credential.
    """

    async def current_descriptor(
        self, context: GatewayProviderContext
    ) -> ProviderDescriptor: ...

    async def list_tools(
        self, context: GatewayProviderContext
    ) -> Sequence[ProviderTool]: ...

    async def admit_call(
        self,
        context: GatewayProviderContext,
        *,
        operation: str,
        arguments: Mapping[str, Any],
        invocation_id: str,
    ) -> ProviderCallAdmission: ...

    async def call_tool(
        self,
        context: GatewayProviderContext,
        *,
        operation: str,
        arguments: Mapping[str, Any],
        invocation_id: str,
    ) -> ProviderCallResult: ...


@dataclass
class MemoryGatewayAuditSink:
    """Small deterministic sink used by portable contract tests."""

    events: list[GatewayAuditEvent] = field(default_factory=list)

    async def record(self, event: GatewayAuditEvent) -> None:
        self.events.append(event)


__all__ = [
    "DelegatedCardReader",
    "DelegatedMCPResourceProvider",
    "GatewayAuditEvent",
    "GatewayAuditSink",
    "GatewayInvocationDecision",
    "GatewayInvocationPolicy",
    "GatewayInvocationRequest",
    "GatewayProviderContext",
    "ManagedKDCubeMCPHost",
    "MemoryGatewayAuditSink",
    "RequestableResourceReader",
]

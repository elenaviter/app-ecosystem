from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from connection_hub.delegated_gateway import (
    AcceptedDescriptor,
    DelegatedCardView,
    DelegatedResourceEntry,
    GatewayCaller,
    GatewayInvocationDecision,
    GatewayInvocationRequest,
    InvocationPolicyView,
    ProviderCallAdmission,
    ProviderCallResult,
    ProviderDescriptor,
    ProviderTool,
    RequestableResource,
)
from connection_hub.delegated_gateway.models import canonical_digest
from connection_hub.delegated_gateway.ports import GatewayProviderContext

NOW = 1_788_400_000


def operation_digest(operation: str, version: str = "v1") -> str:
    return canonical_digest({"operation": operation, "version": version})


def provider_tool(operation: str, version: str = "v1") -> ProviderTool:
    return ProviderTool(
        operation=operation,
        descriptor_digest=operation_digest(operation, version),
        title=f"Tool {operation}",
        description=f"Call {operation}",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "additionalProperties": False,
        },
    )


def resource(
    resource_id: str,
    *,
    kind: str = "fake_external",
    operations: tuple[str, ...] = ("search",),
    version: str = "v1",
    state: str = "active",
    label: str = "Resource",
    relation: str = "delegated_mcp_gateway",
    identity_scope: str = "grantor",
    grants: tuple[str, ...] = ("mcp:use",),
    policy_mode: str = "always",
    policy_remaining: int | None = None,
) -> DelegatedResourceEntry:
    op_digests = {
        operation: operation_digest(operation, version) for operation in operations
    }
    return DelegatedResourceEntry(
        resource_id=resource_id,
        kind=kind,
        display_label=label,
        endpoint_relation=relation,
        grants=grants,
        operations=operations,
        accepted_descriptor=AcceptedDescriptor(
            revision=version,
            digest=canonical_digest({"resource_id": resource_id, "version": version}),
            operation_digests=op_digests,
        ),
        identity_scope=identity_scope,
        state=state,
        invocation_policies={
            operation: InvocationPolicyView(
                mode=policy_mode,
                state="available",
                revision=1,
                remaining=policy_remaining,
            )
            for operation in operations
        },
    )


def caller(
    *,
    capabilities: tuple[str, ...] = (),
    resource_ceiling: tuple[str, ...] | None = None,
) -> GatewayCaller:
    return GatewayCaller(
        caller_type="resident",
        access_id="agent-access-1",
        caller_profile_id="workspace:researcher",
        client_id="kdcube-agent:workspace:researcher",
        capabilities=capabilities,
        resource_ceiling=resource_ceiling,
    )


def card(
    *resources: DelegatedResourceEntry,
    revision: int = 1,
    status: str = "active",
    expires_at: int = NOW + 3600,
    capabilities: tuple[str, ...] = (),
) -> DelegatedCardView:
    return DelegatedCardView(
        caller_type="resident",
        caller_profile_id="workspace:researcher",
        access_id="agent-access-1",
        card_revision=revision,
        status=status,
        expires_at=expires_at,
        source="resident_profile",
        identity_scope="grantor",
        grantor_subject="owner-secret-subject",
        resources=tuple(resources),
        capabilities=capabilities,
    )


@dataclass
class MutableCardReader:
    current: DelegatedCardView | None
    reads: int = 0

    async def read_current(self, caller: GatewayCaller) -> DelegatedCardView | None:
        del caller
        self.reads += 1
        return self.current


@dataclass
class FakeProvider:
    provider_id: str
    resource_kinds: frozenset[str]
    descriptors: dict[str, ProviderDescriptor]
    tools: dict[str, tuple[ProviderTool, ...]]
    results: dict[tuple[str, str], ProviderCallResult] = field(default_factory=dict)
    fail_descriptor: set[str] = field(default_factory=set)
    fail_list: set[str] = field(default_factory=set)
    fail_admission: set[str] = field(default_factory=set)
    denied_admission: dict[str, str] = field(default_factory=dict)
    fail_call: set[str] = field(default_factory=set)
    secret_marker: str = ""
    calls: list[dict[str, Any]] = field(default_factory=list)
    list_calls: list[str] = field(default_factory=list)
    descriptor_calls: list[str] = field(default_factory=list)
    admission_calls: list[dict[str, Any]] = field(default_factory=list)
    call_started: asyncio.Event | None = None
    call_release: asyncio.Event | None = None

    @classmethod
    def for_resources(
        cls,
        provider_id: str,
        kind: str,
        *resources: DelegatedResourceEntry,
    ) -> FakeProvider:
        descriptors = {}
        tools = {}
        for entry in resources:
            entries = tuple(provider_tool(operation) for operation in entry.operations)
            descriptors[entry.resource_id] = ProviderDescriptor(
                resource_id=entry.resource_id,
                revision=entry.accepted_descriptor.revision,
                digest=entry.accepted_descriptor.digest,
                operation_digests={
                    item.operation: item.descriptor_digest for item in entries
                },
            )
            tools[entry.resource_id] = entries
        return cls(
            provider_id=provider_id,
            resource_kinds=frozenset({kind}),
            descriptors=descriptors,
            tools=tools,
        )

    async def current_descriptor(
        self, context: GatewayProviderContext
    ) -> ProviderDescriptor:
        resource_id = context.resource.resource_id
        self.descriptor_calls.append(resource_id)
        if resource_id in self.fail_descriptor:
            raise RuntimeError(f"descriptor failed {self.secret_marker}")
        return self.descriptors[resource_id]

    async def list_tools(
        self, context: GatewayProviderContext
    ) -> Sequence[ProviderTool]:
        resource_id = context.resource.resource_id
        self.list_calls.append(resource_id)
        if resource_id in self.fail_list:
            raise RuntimeError(f"list failed {self.secret_marker}")
        return self.tools[resource_id]

    async def call_tool(
        self,
        context: GatewayProviderContext,
        *,
        operation: str,
        arguments: Mapping[str, Any],
        invocation_id: str,
    ) -> ProviderCallResult:
        resource_id = context.resource.resource_id
        self.calls.append(
            {
                "resource_id": resource_id,
                "operation": operation,
                "arguments": dict(arguments),
                "invocation_id": invocation_id,
            }
        )
        if self.call_started is not None:
            self.call_started.set()
        if self.call_release is not None:
            await self.call_release.wait()
        if resource_id in self.fail_call:
            raise RuntimeError(f"call failed {self.secret_marker}")
        return self.results.get(
            (resource_id, operation),
            ProviderCallResult.from_value(
                {
                    "ok": True,
                    "resource_id": resource_id,
                    "operation": operation,
                    "arguments": dict(arguments),
                }
            ),
        )

    async def admit_call(
        self,
        context: GatewayProviderContext,
        *,
        operation: str,
        arguments: Mapping[str, Any],
        invocation_id: str,
    ) -> ProviderCallAdmission:
        resource_id = context.resource.resource_id
        self.admission_calls.append(
            {
                "resource_id": resource_id,
                "operation": operation,
                "arguments": dict(arguments),
                "invocation_id": invocation_id,
            }
        )
        if resource_id in self.fail_admission:
            raise RuntimeError(f"admission failed {self.secret_marker}")
        reason = self.denied_admission.get(resource_id, "")
        if reason:
            return ProviderCallAdmission(allowed=False, reason=reason)
        return ProviderCallAdmission(allowed=True)


@dataclass
class MemoryPolicy:
    reservations: dict[tuple[str, str, str], str] = field(default_factory=dict)
    invocations: dict[
        tuple[str, str, str, str], tuple[str, ProviderCallResult | None]
    ] = field(default_factory=dict)
    begins: list[GatewayInvocationRequest] = field(default_factory=list)
    completions: list[GatewayInvocationRequest] = field(default_factory=list)

    async def begin(
        self, request: GatewayInvocationRequest
    ) -> GatewayInvocationDecision:
        self.begins.append(request)
        policy = request.resource.invocation_policies.get(request.operation)
        if policy is None:
            return GatewayInvocationDecision(
                dispatch=False, reason="invocation_policy_missing"
            )
        authority = (
            request.card.access_id,
            request.resource.resource_id,
            request.operation,
        )
        key = (*authority, request.invocation_id)
        existing = self.invocations.get(key)
        if existing is not None:
            digest, result = existing
            if digest != request.request_digest:
                return GatewayInvocationDecision(
                    dispatch=False,
                    reason="delegated_invocation_id_conflict",
                    public_policy=policy,
                )
            if result is None:
                return GatewayInvocationDecision(
                    dispatch=False,
                    reason="delegated_invocation_in_progress",
                    retryable=True,
                    public_policy=policy,
                )
            return GatewayInvocationDecision(
                dispatch=False,
                replay=True,
                result=result,
                public_policy=policy,
            )
        if policy.mode == "once" and authority in self.reservations:
            return GatewayInvocationDecision(
                dispatch=False,
                reason="delegated_invocation_limit_exhausted",
                public_policy=policy,
            )
        self.reservations[authority] = request.invocation_id
        self.invocations[key] = (request.request_digest, None)
        return GatewayInvocationDecision(dispatch=True, public_policy=policy)

    async def complete(
        self,
        request: GatewayInvocationRequest,
        *,
        result: ProviderCallResult,
    ) -> None:
        self.completions.append(request)
        key = (
            request.card.access_id,
            request.resource.resource_id,
            request.operation,
            request.invocation_id,
        )
        digest, _old_result = self.invocations[key]
        self.invocations[key] = (digest, result)


@dataclass
class FakeRequestableReader:
    resources: tuple[RequestableResource, ...]
    calls: int = 0

    async def list_requestable(
        self, *, caller: GatewayCaller, card: DelegatedCardView
    ) -> Sequence[RequestableResource]:
        del caller, card
        self.calls += 1
        return self.resources


def with_version(entry: DelegatedResourceEntry, version: str) -> DelegatedResourceEntry:
    operations = entry.operations
    return replace(
        entry,
        accepted_descriptor=AcceptedDescriptor(
            revision=version,
            digest=canonical_digest(
                {"resource_id": entry.resource_id, "version": version}
            ),
            operation_digests={
                operation: operation_digest(operation, version)
                for operation in operations
            },
        ),
    )

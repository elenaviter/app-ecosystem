# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Live authority checks and exact dispatch for the delegated MCP gateway."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Mapping
from typing import Any

from connection_hub.delegated_gateway.access import (
    ACCESS_DESCRIBE_SCHEMA,
    access_describe_tool,
)
from connection_hub.delegated_gateway.access import (
    describe_access as build_access_description,
)
from connection_hub.delegated_gateway.models import (
    ACCESS_DESCRIBE_TOOL,
    CARD_ACTIVE,
    RESOURCE_ACTIVE,
    DelegatedCardView,
    DelegatedGatewayError,
    GatewayCaller,
    GatewayCallResult,
    GatewayContractError,
    GatewayTool,
    GatewayToolRoute,
    ProviderCallAdmission,
    ProviderCallResult,
    ProviderDescriptor,
    canonical_digest,
    json_copy,
)
from connection_hub.delegated_gateway.naming import QualifiedToolNameIndex
from connection_hub.delegated_gateway.ports import (
    DelegatedCardReader,
    DelegatedMCPResourceProvider,
    GatewayAuditEvent,
    GatewayAuditSink,
    GatewayInvocationDecision,
    GatewayInvocationPolicy,
    GatewayInvocationRequest,
    GatewayProviderContext,
    RequestableResourceReader,
)
from connection_hub.delegated_gateway.registry import (
    DelegatedMCPProviderRegistry,
)
from connection_hub.delegated_gateway.routing import (
    accepted_route_index,
    current_tool_map,
    operation_unavailable_reason,
    route_for,
)

DEFAULT_MAX_ARGUMENT_BYTES = 256 * 1024
DEFAULT_MAX_RESULT_BYTES = 1024 * 1024
DEFAULT_MAX_TOOLS = 512

_POLICY_DENIAL_REASONS = frozenset(
    {
        "delegated_invocation_id_conflict",
        "delegated_invocation_in_progress",
        "delegated_invocation_limit_exhausted",
        "delegated_invocation_policy_missing",
        "delegated_request_permit_required",
        "invocation_policy_denied",
        "invocation_policy_missing",
    }
)

_PROVIDER_ADMISSION_REASONS = frozenset(
    {
        "connector_grant_not_consented",
        "connector_not_active",
        "managed_surface_denied",
        "operation_descriptor_changed",
        "operation_not_consented",
        "operation_removed_by_server",
        "provider_admission_denied",
        "tool_not_in_delegated_resources",
    }
)


def _json_size(value: Any) -> int:
    # canonical_digest performs the stricter canonical-JSON validation first.
    canonical_digest(value)
    return len(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


def _fixed_provider_failure() -> ProviderCallResult:
    return ProviderCallResult(
        structured_content={
            "ok": False,
            "error": "delegated_mcp_provider_failed",
            "reason": "delegated_mcp_provider_failed",
            "retryable": True,
        },
        is_error=True,
    )


class DelegatedMCPGateway:
    """One aggregate delegated MCP authority and dispatch surface.

    The object is stateless with respect to card authority. Every public
    operation reads the current card, and every effect call rechecks the
    provider descriptor immediately before invocation-policy consumption.
    """

    def __init__(
        self,
        *,
        cards: DelegatedCardReader,
        providers: DelegatedMCPProviderRegistry,
        invocation_policy: GatewayInvocationPolicy,
        audit: GatewayAuditSink,
        requestable: RequestableResourceReader | None = None,
        clock: Callable[[], int] | None = None,
        max_argument_bytes: int = DEFAULT_MAX_ARGUMENT_BYTES,
        max_result_bytes: int = DEFAULT_MAX_RESULT_BYTES,
        max_tools: int = DEFAULT_MAX_TOOLS,
    ) -> None:
        if max_argument_bytes < 1 or max_result_bytes < 1 or max_tools < 1:
            raise GatewayContractError("gateway_limits_invalid")
        self._cards = cards
        self._providers = providers
        self._invocation_policy = invocation_policy
        self._audit = audit
        self._requestable = requestable
        self._clock = clock or (lambda: int(time.time()))
        self._max_argument_bytes = int(max_argument_bytes)
        self._max_result_bytes = int(max_result_bytes)
        self._max_tools = int(max_tools)
        self._background: set[asyncio.Task[ProviderCallResult]] = set()

    async def list_tools(self, caller: GatewayCaller) -> tuple[GatewayTool, ...]:
        card = await self._current_card(caller, require_active=True)
        listed: list[GatewayTool] = [access_describe_tool()]
        name_index = QualifiedToolNameIndex()

        for resource in sorted(card.resources, key=lambda item: item.resource_id):
            if resource.state != RESOURCE_ACTIVE:
                continue
            try:
                provider = self._providers.provider_for(resource)
                context = GatewayProviderContext(
                    caller=caller, card=card, resource=resource
                )
                descriptor = await provider.current_descriptor(context)
                tools = await provider.list_tools(context)
                current = current_tool_map(
                    resource=resource,
                    descriptor=descriptor,
                    tools=tools,
                )
            except Exception:  # noqa: BLE001, S112 - isolate provider inventory
                # One unavailable provider cannot erase or widen another
                # provider's granted inventory.
                continue

            for operation in resource.operations:
                tool = current.get(operation)
                if tool is None:
                    continue
                route = route_for(resource, provider, operation)
                try:
                    name = name_index.add(route)
                except GatewayContractError:
                    # Retaining the first side would still publish an
                    # ambiguous route. Reject the complete inventory.
                    raise DelegatedGatewayError(
                        "qualified_tool_name_collision",
                        access_id=card.access_id,
                        card_revision=card.card_revision,
                    ) from None
                listed.append(
                    GatewayTool(
                        name=name,
                        route=route,
                        title=tool.title,
                        description=tool.description,
                        input_schema=tool.input_schema,
                        output_schema=tool.output_schema,
                    )
                )
                if len(listed) > self._max_tools:
                    raise DelegatedGatewayError(
                        "tool_inventory_limit_exceeded",
                        access_id=card.access_id,
                        card_revision=card.card_revision,
                    )

        return tuple(sorted(listed, key=lambda item: item.name))

    async def call_tool(
        self,
        caller: GatewayCaller,
        *,
        tool_name: str,
        arguments: Mapping[str, Any] | None,
        invocation_id: str,
    ) -> GatewayCallResult:
        requested = str(tool_name or "").strip()
        if requested == ACCESS_DESCRIBE_TOOL:
            if arguments is not None and not isinstance(arguments, Mapping):
                raise DelegatedGatewayError(
                    "access_describe_arguments_invalid", tool_name=requested
                )
            payload = dict(arguments or {})
            unknown = set(payload) - {"include_requestable"}
            if unknown or not isinstance(
                payload.get("include_requestable", False), bool
            ):
                raise DelegatedGatewayError(
                    "access_describe_arguments_invalid", tool_name=requested
                )
            card, description = await self._describe_current_access(
                caller,
                include_requestable=bool(payload.get("include_requestable", False)),
            )
            return GatewayCallResult(
                result=ProviderCallResult.from_value(description),
                access_id=card.access_id,
                card_revision=card.card_revision,
                resource_id="connection-hub:caller-self",
                resource_kind="gateway_meta",
                operation=ACCESS_DESCRIBE_TOOL,
                tool_name=ACCESS_DESCRIBE_TOOL,
                invocation_id="",
                provider_id="gateway",
                descriptor_revision="gateway-v1",
                descriptor_digest=canonical_digest(ACCESS_DESCRIBE_SCHEMA),
            )

        call_arguments = self._validated_arguments(arguments)
        invocation = str(invocation_id or "").strip()
        if (
            not invocation
            or len(invocation) > 256
            or any(
                ord(character) < 33 or ord(character) > 126 for character in invocation
            )
        ):
            raise DelegatedGatewayError(
                "delegated_invocation_id_required", tool_name=requested
            )

        card = await self._current_card(caller, require_active=True)
        name_index = accepted_route_index(card, self._providers)
        route = name_index.resolve(requested)
        if route is None:
            raise DelegatedGatewayError(
                "tool_not_in_current_card",
                tool_name=requested,
                access_id=card.access_id,
                card_revision=card.card_revision,
                invocation_id=invocation,
            )
        resource = card.resource_map().get(route.resource_id)
        if resource is None or resource.state != RESOURCE_ACTIVE:
            raise self._denial(
                "resource_not_active", card, route, requested, invocation
            )

        provider = self._providers.provider_for(resource)
        context = GatewayProviderContext(caller=caller, card=card, resource=resource)
        try:
            descriptor = await provider.current_descriptor(context)
            tools = await provider.list_tools(context)
            current = current_tool_map(
                resource=resource,
                descriptor=descriptor,
                tools=tools,
            )
        except DelegatedGatewayError:
            raise
        except Exception:  # noqa: BLE001 - provider errors are secret-bearing
            raise self._denial(
                "resource_provider_unavailable",
                card,
                route,
                requested,
                invocation,
                retryable=True,
            ) from None
        if route.operation not in current:
            reason = operation_unavailable_reason(
                resource=resource, descriptor=descriptor, operation=route.operation
            )
            raise self._denial(reason, card, route, requested, invocation)

        try:
            admission = await provider.admit_call(
                context,
                operation=route.operation,
                arguments=call_arguments,
                invocation_id=invocation,
            )
            if not isinstance(admission, ProviderCallAdmission):
                raise GatewayContractError("provider_admission_invalid")
        except Exception:  # noqa: BLE001 - admission errors are secret-bearing
            raise self._denial(
                "resource_provider_unavailable",
                card,
                route,
                requested,
                invocation,
                retryable=True,
            ) from None
        if not admission.allowed:
            reason = (
                admission.reason
                if admission.reason in _PROVIDER_ADMISSION_REASONS
                else "provider_admission_denied"
            )
            raise self._denial(
                reason,
                card,
                route,
                requested,
                invocation,
                retryable=admission.retryable,
            )

        request_digest = canonical_digest(
            {
                "resource_id": route.resource_id,
                "operation": route.operation,
                "accepted_descriptor_identity": route.accepted_descriptor_identity,
                "arguments": call_arguments,
            }
        )
        policy_request = GatewayInvocationRequest(
            caller=caller,
            card=card,
            resource=resource,
            provider_id=str(provider.provider_id),
            operation=route.operation,
            tool_name=requested,
            invocation_id=invocation,
            request_digest=request_digest,
            authority_revision=route.accepted_descriptor_identity,
        )
        await self._audit_admission(policy_request, descriptor)
        try:
            decision = await self._invocation_policy.begin(policy_request)
            if not isinstance(decision, GatewayInvocationDecision):
                raise GatewayContractError("invocation_policy_decision_invalid")
        except Exception:  # noqa: BLE001 - policy storage is an injected port
            raise self._denial(
                "invocation_policy_unavailable",
                card,
                route,
                requested,
                invocation,
                retryable=True,
            ) from None
        if not decision.dispatch:
            return self._replay_or_deny(
                decision=decision,
                request=policy_request,
                descriptor=descriptor,
            )

        effect = asyncio.create_task(
            self._dispatch_and_complete(
                provider=provider,
                context=context,
                request=policy_request,
                descriptor=descriptor,
                decision=decision,
                arguments=call_arguments,
            ),
            name=f"delegated-mcp:{invocation}",
        )
        self._retain_effect(effect)
        result = await asyncio.shield(effect)
        return self._call_result(
            result=result,
            request=policy_request,
            descriptor=descriptor,
            replay=False,
        )

    async def describe_access(
        self,
        caller: GatewayCaller,
        *,
        include_requestable: bool = False,
    ) -> dict[str, Any]:
        _card, payload = await self._describe_current_access(
            caller,
            include_requestable=include_requestable,
        )
        return payload

    async def _describe_current_access(
        self,
        caller: GatewayCaller,
        *,
        include_requestable: bool,
    ) -> tuple[DelegatedCardView, dict[str, Any]]:
        if not isinstance(include_requestable, bool):
            raise DelegatedGatewayError("access_describe_arguments_invalid")
        card = await self._current_card(caller, require_active=False)
        payload = await build_access_description(
            caller=caller,
            card=card,
            providers=self._providers,
            requestable=self._requestable,
            now=self._clock(),
            include_requestable=include_requestable,
        )
        return card, payload

    @property
    def inflight_count(self) -> int:
        return len(self._background)

    async def drain(self, *, timeout: float | None = None) -> bool:
        """Wait for already-dispatched effects without starting new work.

        A timeout leaves unfinished effects running; service shutdown can then
        choose its own hard-stop policy without this method falsely recording
        completion.
        """

        pending = tuple(self._background)
        if not pending:
            return True
        _done, remaining = await asyncio.wait(pending, timeout=timeout)
        return not remaining

    async def _current_card(
        self, caller: GatewayCaller, *, require_active: bool
    ) -> DelegatedCardView:
        try:
            card = await self._cards.read_current(caller)
        except Exception:  # noqa: BLE001 - Card storage is an injected port
            raise DelegatedGatewayError(
                "card_authority_unavailable",
                access_id=caller.access_id,
                retryable=True,
            ) from None
        if card is None:
            raise DelegatedGatewayError("card_not_found", access_id=caller.access_id)
        if (
            card.access_id != caller.access_id
            or card.caller_type != caller.caller_type
            or card.caller_profile_id != caller.caller_profile_id
        ):
            raise DelegatedGatewayError(
                "card_caller_mismatch", access_id=caller.access_id
            )
        if require_active and card.status != CARD_ACTIVE:
            raise DelegatedGatewayError(
                "card_revoked",
                access_id=card.access_id,
                card_revision=card.card_revision,
            )
        if require_active and card.expires_at and card.expires_at <= self._clock():
            raise DelegatedGatewayError(
                "card_expired",
                access_id=card.access_id,
                card_revision=card.card_revision,
            )
        return card

    def _validated_arguments(
        self, arguments: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        value = {} if arguments is None else arguments
        if not isinstance(value, Mapping):
            raise DelegatedGatewayError("tool_arguments_not_object")
        try:
            copied = json_copy(dict(value), reason="tool_arguments_not_json")
            size = _json_size(copied)
        except GatewayContractError:
            raise DelegatedGatewayError("tool_arguments_not_json") from None
        if size > self._max_argument_bytes:
            raise DelegatedGatewayError("tool_arguments_too_large")
        return copied

    def _validate_result(self, result: ProviderCallResult) -> ProviderCallResult:
        if not isinstance(result, ProviderCallResult):
            raise GatewayContractError("provider_result_invalid")
        if _json_size(result.to_public_dict()) > self._max_result_bytes:
            return ProviderCallResult(
                structured_content={
                    "ok": False,
                    "error": "delegated_mcp_result_too_large",
                    "reason": "delegated_mcp_result_too_large",
                    "retryable": False,
                },
                is_error=True,
            )
        return result

    async def _dispatch_and_complete(
        self,
        *,
        provider: DelegatedMCPResourceProvider,
        context: GatewayProviderContext,
        request: GatewayInvocationRequest,
        descriptor: ProviderDescriptor,
        decision: GatewayInvocationDecision,
        arguments: Mapping[str, Any],
    ) -> ProviderCallResult:
        try:
            value = await provider.call_tool(
                context,
                operation=request.operation,
                arguments=arguments,
                invocation_id=request.invocation_id,
            )
            result = self._validate_result(value)
        except asyncio.CancelledError:
            # Service shutdown may cancel the effect task itself. Keep the
            # policy reservation incomplete rather than claiming an outcome.
            raise
        except Exception:  # noqa: BLE001 - provider failures cross a public boundary
            result = _fixed_provider_failure()

        try:
            await self._invocation_policy.complete(request, result=result)
        except Exception:  # noqa: BLE001, S110 - effect already happened
            # The effect has already happened. Never invite an immediate
            # duplicate by replacing its result with a retryable denial.
            pass
        try:
            await self._audit.record(
                self._audit_event(
                    request=request,
                    descriptor=descriptor,
                    phase="complete",
                    outcome="error" if result.is_error else "completed",
                    replay=False,
                    policy_revision=(
                        decision.public_policy.revision
                        if decision.public_policy is not None
                        else 0
                    ),
                )
            )
        except Exception:  # noqa: BLE001, S110 - result remains authoritative
            pass
        return result

    async def _audit_admission(
        self,
        request: GatewayInvocationRequest,
        descriptor: ProviderDescriptor,
    ) -> None:
        try:
            await self._audit.record(
                self._audit_event(
                    request=request,
                    descriptor=descriptor,
                    phase="admission",
                    outcome="current_authority",
                    replay=False,
                )
            )
        except Exception:  # noqa: BLE001 - audit storage is an injected port
            raise self._denial(
                "audit_unavailable",
                request.card,
                route_for(
                    request.resource,
                    self._providers.provider_for(request.resource),
                    request.operation,
                ),
                request.tool_name,
                request.invocation_id,
                retryable=True,
            ) from None

    def _retain_effect(self, task: asyncio.Task[ProviderCallResult]) -> None:
        self._background.add(task)

        def _completed(done: asyncio.Task[ProviderCallResult]) -> None:
            self._background.discard(done)
            if done.cancelled():
                return
            # Retrieve the terminal exception for a request that was cancelled
            # while shield kept policy completion alive.
            done.exception()

        task.add_done_callback(_completed)

    def _replay_or_deny(
        self,
        *,
        decision: GatewayInvocationDecision,
        request: GatewayInvocationRequest,
        descriptor: ProviderDescriptor,
    ) -> GatewayCallResult:
        if decision.replay and decision.result is not None:
            result = self._validate_result(decision.result)
            return self._call_result(
                result=result,
                request=request,
                descriptor=descriptor,
                replay=True,
            )
        proposed = str(decision.reason or "").strip().lower()
        reason = (
            proposed
            if proposed in _POLICY_DENIAL_REASONS
            else "invocation_policy_denied"
        )
        raise DelegatedGatewayError(
            reason,
            resource_id=request.resource.resource_id,
            operation=request.operation,
            tool_name=request.tool_name,
            access_id=request.card.access_id,
            card_revision=request.card.card_revision,
            invocation_id=request.invocation_id,
            retryable=decision.retryable,
            recovery=request.resource.recovery_for(reason),
        )

    @staticmethod
    def _call_result(
        *,
        result: ProviderCallResult,
        request: GatewayInvocationRequest,
        descriptor: ProviderDescriptor,
        replay: bool,
    ) -> GatewayCallResult:
        return GatewayCallResult(
            result=result,
            access_id=request.card.access_id,
            card_revision=request.card.card_revision,
            resource_id=request.resource.resource_id,
            resource_kind=request.resource.kind,
            operation=request.operation,
            tool_name=request.tool_name,
            invocation_id=request.invocation_id,
            provider_id=request.provider_id,
            descriptor_revision=descriptor.revision,
            descriptor_digest=descriptor.digest,
            replay=replay,
        )

    @staticmethod
    def _audit_event(
        *,
        request: GatewayInvocationRequest,
        descriptor: ProviderDescriptor,
        phase: str,
        outcome: str,
        replay: bool,
        policy_revision: int = 0,
    ) -> GatewayAuditEvent:
        return GatewayAuditEvent(
            phase=phase,
            caller_type=request.caller.caller_type,
            caller_profile_id=request.caller.caller_profile_id,
            access_id=request.card.access_id,
            card_revision=request.card.card_revision,
            resource_id=request.resource.resource_id,
            resource_kind=request.resource.kind,
            provider_id=request.provider_id,
            operation=request.operation,
            tool_name=request.tool_name,
            invocation_id=request.invocation_id,
            descriptor_revision=descriptor.revision,
            descriptor_digest=descriptor.digest,
            policy_revision=policy_revision,
            outcome=outcome,
            replay=replay,
        )

    @staticmethod
    def _denial(
        reason: str,
        card: DelegatedCardView,
        route: GatewayToolRoute,
        tool_name: str,
        invocation_id: str,
        *,
        retryable: bool = False,
    ) -> DelegatedGatewayError:
        resource = card.resource_map().get(route.resource_id)
        recovery = resource.recovery_for(reason) if resource is not None else {}
        return DelegatedGatewayError(
            reason,
            resource_id=route.resource_id,
            operation=route.operation,
            tool_name=tool_name,
            access_id=card.access_id,
            card_revision=card.card_revision,
            invocation_id=invocation_id,
            retryable=retryable,
            recovery=recovery,
        )


__all__ = [
    "DEFAULT_MAX_ARGUMENT_BYTES",
    "DEFAULT_MAX_RESULT_BYTES",
    "DEFAULT_MAX_TOOLS",
    "DelegatedMCPGateway",
]

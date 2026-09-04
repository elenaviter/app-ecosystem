"""Production adapters for the hosted delegated MCP Gateway.

The portable Gateway owns routing and enforcement order. This module binds one
authenticated KDCube request to the exact live Card, durable invocation-policy
storage, the existing external connector service, and KDCube's trusted local
MCP bridge.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import quote, urlencode

from connection_hub.delegated_credentials.cards.read_model import (
    CardResourceView,
)
from connection_hub.delegated_credentials.credential_view import (
    DelegatedCredentialView,
)
from connection_hub.delegated_gateway import (
    DISCOVER_REQUESTABLE,
    DelegatedCardView,
    DelegatedGatewayError,
    DelegatedMCPGateway,
    DelegatedMCPProviderRegistry,
    GatewayAuditEvent,
    GatewayCaller,
    GatewayInvocationDecision,
    GatewayInvocationRequest,
    GatewayProviderContext,
    GatewayResourceMetadata,
    InvocationPolicyView,
    ProviderCallAdmission,
    ProviderCallResult,
    ProviderDescriptor,
    ProviderTool,
    RecoveryLink,
    RequestableResource,
    adapt_card_view,
    caller_profile_id_for_card,
)
from connection_hub.delegated_gateway.providers import (
    ExternalRemoteMCPProvider,
    ManagedKDCubeMCPProvider,
)
from connection_hub.invocation_policy import (
    SURFACE_OUTER,
    InvocationAuthority,
)
from connection_hub.remote_mcp import connector_id_from_resource


LOGGER = logging.getLogger("connection_hub.delegated_gateway.host")

_MANAGED_MCP_RESOURCE = re.compile(
    r"/api/integrations/bundles/[^/]+/[^/]+/"
    r"(?P<bundle>[^/]+)/(?P<route>public|operations)/mcp/"
    r"(?P<alias>[^/*?]+)"
)
_POLICY_REASON_MAP = {
    "delegated_invocation_outcome_pending": "delegated_invocation_in_progress",
    "delegated_invocation_policy_changing": "invocation_policy_denied",
    "delegated_invocation_policy_required": "invocation_policy_missing",
    "delegated_request_policy_required": "invocation_policy_missing",
}

ManagedDispatch = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class ManagedSurfaceLocator:
    bundle_id: str
    endpoint_alias: str
    route: str


@dataclass(frozen=True)
class HostedGatewayBinding:
    gateway: DelegatedMCPGateway
    caller: GatewayCaller

    def resolve_caller(self, _request: Any) -> GatewayCaller:
        return self.caller


def managed_surface_locator(resource: str) -> ManagedSurfaceLocator | None:
    match = _MANAGED_MCP_RESOURCE.search(str(resource or ""))
    if match is None:
        return None
    return ManagedSurfaceLocator(
        bundle_id=match.group("bundle"),
        endpoint_alias=match.group("alias"),
        route=match.group("route"),
    )


def _card_settings_path(
    *, tenant: str, project: str, access_id: str, resource: str
) -> str:
    query = urlencode(
        {
            "tab": "delegated_by_kdcube",
            "manual_access_id": access_id,
            "resource": resource,
        }
    )
    return (
        "/api/integrations/bundles/"
        f"{quote(tenant, safe='')}/{quote(project, safe='')}/"
        f"connection-hub%401-0/widgets/connections_settings?{query}"
    )


def gateway_resource_metadata(
    resource: CardResourceView,
    *,
    tenant: str,
    project: str,
    access_id: str,
) -> GatewayResourceMetadata:
    """Add non-secret routing facts that intentionally do not live on Card."""

    recovery_path = _card_settings_path(
        tenant=tenant,
        project=project,
        access_id=access_id,
        resource=resource.resource,
    )
    recovery = tuple(
        RecoveryLink(code=code, href=recovery_path)
        for code in (
            "delegated_invocation_limit_exhausted",
            "invocation_policy_missing",
            "operation_descriptor_changed",
            "resource_disabled",
        )
    )
    connector_id = connector_id_from_resource(resource.resource)
    if connector_id:
        return GatewayResourceMetadata(
            endpoint_relation="connection_hub_remote_mcp_proxy",
            recovery=recovery,
            provider_metadata={
                "gateway_compatible": True,
                "connector_id": connector_id,
            },
        )

    locator = managed_surface_locator(resource.resource)
    if locator is None:
        return GatewayResourceMetadata(
            endpoint_relation="not_mcp_compatible",
            recovery=recovery,
            provider_metadata={"gateway_compatible": False},
        )
    return GatewayResourceMetadata(
        endpoint_relation=(
            f"same_kdcube:{locator.bundle_id}:{locator.route}:mcp:"
            f"{locator.endpoint_alias}"
        ),
        recovery=recovery,
        provider_metadata={
            "gateway_compatible": True,
            "bundle_id": locator.bundle_id,
            "endpoint_alias": locator.endpoint_alias,
            "route": locator.route,
            "current_revision": resource.current_revision,
            "current_digest": resource.current_digest,
            "current_operation_digests": {
                operation.name: operation.current_digest
                for operation in resource.operations
                if operation.current_digest
            },
        },
    )


class HostedCardReader:
    def __init__(
        self,
        *,
        access_service: Any,
        grantor_subject: str,
        access_id: str,
        tenant: str,
        project: str,
        capabilities: Sequence[str] = (),
    ) -> None:
        self._access_service = access_service
        self._grantor_subject = grantor_subject
        self._access_id = access_id
        self._tenant = tenant
        self._project = project
        self._capabilities = tuple(capabilities)

    async def read_current(self, caller: GatewayCaller) -> DelegatedCardView | None:
        if caller.access_id != self._access_id:
            return None
        card = await self._access_service.card_for_access_id(
            grantor_subject=self._grantor_subject,
            access_id=self._access_id,
        )
        if card is None:
            return None
        return adapt_card_view(
            card,
            metadata_for=lambda resource: gateway_resource_metadata(
                resource,
                tenant=self._tenant,
                project=self._project,
                access_id=self._access_id,
            ),
            capabilities=self._capabilities,
        )


def _public_policy(policy: Any) -> InvocationPolicyView | None:
    if policy is None:
        return None
    return InvocationPolicyView(
        mode=str(policy.mode),
        state=str(policy.state),
        revision=int(policy.revision),
        remaining=policy.remaining,
    )


def _stored_provider_result(value: Any, *, is_error: bool) -> ProviderCallResult:
    if isinstance(value, Mapping) and {
        "structured_content",
        "content",
        "is_error",
    }.issubset(value):
        content = value.get("content")
        return ProviderCallResult(
            structured_content=value.get("structured_content"),
            content=tuple(content) if isinstance(content, list) else (),
            is_error=bool(value.get("is_error")),
        )
    return ProviderCallResult(structured_content=value, is_error=is_error)


class HostedInvocationPolicy:
    def __init__(self, service: Any) -> None:
        self._service = service

    @staticmethod
    def _authority(request: GatewayInvocationRequest) -> InvocationAuthority:
        return InvocationAuthority(
            access_id=request.card.access_id,
            resource=request.resource.resource_id,
            surface=SURFACE_OUTER,
            operation=request.operation,
        )

    async def begin(
        self, request: GatewayInvocationRequest
    ) -> GatewayInvocationDecision:
        expected = request.resource.invocation_policies.get(request.operation)
        if expected is None:
            return GatewayInvocationDecision(
                dispatch=False,
                reason="invocation_policy_missing",
            )
        authority = self._authority(request)
        current = await self._service.get(
            owner_subject=request.card.grantor_subject,
            authority=authority,
        )
        if current is None:
            return GatewayInvocationDecision(
                dispatch=False,
                reason="invocation_policy_missing",
            )
        decision = await self._service.begin(
            owner_subject=request.card.grantor_subject,
            authority=authority,
            invocation_id=request.invocation_id,
            request_digest=request.request_digest,
            card_revision=request.card.card_revision,
            authority_revision=request.authority_revision,
        )
        result = None
        if decision.replay and decision.invocation is not None:
            result = _stored_provider_result(
                decision.result,
                is_error=decision.result_is_error,
            )
        reason = _POLICY_REASON_MAP.get(decision.reason, decision.reason)
        return GatewayInvocationDecision(
            dispatch=bool(decision.dispatch),
            replay=bool(decision.replay),
            result=result,
            reason=reason,
            retryable=bool(decision.retryable),
            public_policy=_public_policy(decision.policy or current),
        )

    async def complete(
        self,
        request: GatewayInvocationRequest,
        *,
        result: ProviderCallResult,
    ) -> None:
        await self._service.complete(
            owner_subject=request.card.grantor_subject,
            authority=self._authority(request),
            invocation_id=request.invocation_id,
            request_digest=request.request_digest,
            result=result.to_public_dict(),
            result_is_error=result.is_error,
        )


class HostedGatewayAudit:
    async def record(self, event: GatewayAuditEvent) -> None:
        try:
            LOGGER.info(
                "delegated_mcp_gateway %s",
                json.dumps(asdict(event), ensure_ascii=True, sort_keys=True),
            )
        except Exception:
            # Logging configuration is outside the authority path. The event
            # shape contains no arguments, results, or credential material.
            LOGGER.warning("delegated_mcp_gateway audit encoding failed")


class HostedRequestableResources:
    def __init__(
        self,
        *,
        access_service: Any,
        grantor_user: Mapping[str, Any],
        tenant: str,
        project: str,
    ) -> None:
        self._access_service = access_service
        self._grantor_user = dict(grantor_user)
        self._tenant = tenant
        self._project = project

    async def list_requestable(
        self, *, caller: GatewayCaller, card: DelegatedCardView
    ) -> Sequence[RequestableResource]:
        del caller
        options = await self._access_service.resource_options(self._grantor_user)
        out: list[RequestableResource] = []
        for option in options:
            resource_id = str(option.get("resource") or "").strip()
            if not resource_id:
                continue
            connector_id = connector_id_from_resource(resource_id)
            locator = managed_surface_locator(resource_id)
            if not connector_id and locator is None:
                continue
            kind = str(option.get("kind") or "").strip()
            if not kind:
                kind = "remote_mcp" if connector_id else "catalog"
            out.append(
                RequestableResource(
                    resource_id=resource_id,
                    kind=kind,
                    display_label=str(option.get("label") or resource_id),
                    identity_scope=str(option.get("identity_scope") or "grantor"),
                    owner_subject=card.grantor_subject,
                    recovery={
                        "href": _card_settings_path(
                            tenant=self._tenant,
                            project=self._project,
                            access_id=card.access_id,
                            resource=resource_id,
                        )
                    },
                )
            )
        return tuple(out)


class HostedManagedKDCubeMCPHost:
    """Translate the Gateway host port to KDCube's request-bound MCP bridge."""

    def __init__(self, *, dispatch: ManagedDispatch | None = None) -> None:
        self._dispatch = dispatch
        self._inventory: dict[
            tuple[str, int, str], tuple[ProviderTool, ...]
        ] = {}

    async def _invoke(
        self,
        context: GatewayProviderContext,
        *,
        method: str,
        params: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        metadata = context.resource.provider_metadata
        if not bool(metadata.get("gateway_compatible")):
            raise RuntimeError("managed_surface_not_mcp_compatible")
        bundle_id = str(metadata.get("bundle_id") or "").strip()
        endpoint_alias = str(metadata.get("endpoint_alias") or "").strip()
        route = str(metadata.get("route") or "public").strip()
        if not bundle_id or not endpoint_alias or route not in {"public", "operations"}:
            raise RuntimeError("managed_surface_locator_invalid")
        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": "connection-hub-gateway",
            "method": method,
        }
        if params is not None:
            message["params"] = dict(params)
        dispatch = self._dispatch
        if dispatch is None:
            from kdcube_ai_app.apps.chat.sdk.infra.bundle_operations import (
                call_bundle_mcp_surface,
            )

            dispatch = call_bundle_mcp_surface
        response = await dispatch(
            bundle_id=bundle_id,
            endpoint_alias=endpoint_alias,
            message=message,
            route=route,
        )
        status = int(
            getattr(response, "status_code", None)
            or (response.get("status_code") if isinstance(response, Mapping) else 0)
            or 0
        )
        body = getattr(response, "body", None)
        if body is None and isinstance(response, Mapping):
            body = response.get("body")
        if isinstance(body, str):
            body = body.encode("utf-8")
        if status < 200 or status >= 300 or not isinstance(body, bytes):
            raise RuntimeError("managed_surface_unavailable")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError("managed_surface_response_invalid") from exc
        if not isinstance(payload, Mapping):
            raise RuntimeError("managed_surface_response_invalid")
        return payload

    async def _tools(
        self, context: GatewayProviderContext
    ) -> tuple[ProviderTool, ...]:
        key = (
            context.card.access_id,
            context.card.card_revision,
            context.resource.resource_id,
        )
        cached = self._inventory.get(key)
        if cached is not None:
            return cached
        payload = await self._invoke(context, method="tools/list")
        result = payload.get("result")
        result = result if isinstance(result, Mapping) else {}
        raw_tools = result.get("tools")
        if not isinstance(raw_tools, list):
            raise RuntimeError("managed_surface_inventory_invalid")
        current_digests = context.resource.provider_metadata.get(
            "current_operation_digests"
        )
        current_digests = (
            current_digests if isinstance(current_digests, Mapping) else {}
        )
        accepted = context.resource.accepted_descriptor.operation_digests
        tools: list[ProviderTool] = []
        for raw in raw_tools:
            if not isinstance(raw, Mapping):
                continue
            operation = str(raw.get("name") or "").strip()
            if not operation or operation not in context.resource.operations:
                continue
            digest = str(
                current_digests.get(operation) or accepted.get(operation) or ""
            ).strip()
            if not digest:
                continue
            input_schema = raw.get("inputSchema")
            output_schema = raw.get("outputSchema")
            tools.append(
                ProviderTool(
                    operation=operation,
                    descriptor_digest=digest,
                    title=str(raw.get("title") or operation),
                    description=str(raw.get("description") or ""),
                    input_schema=(
                        dict(input_schema)
                        if isinstance(input_schema, Mapping)
                        else {"type": "object"}
                    ),
                    output_schema=(
                        dict(output_schema)
                        if isinstance(output_schema, Mapping)
                        else None
                    ),
                )
            )
        resolved = tuple(tools)
        self._inventory[key] = resolved
        return resolved

    async def current_descriptor(
        self, context: GatewayProviderContext
    ) -> ProviderDescriptor:
        tools = await self._tools(context)
        metadata = context.resource.provider_metadata
        return ProviderDescriptor(
            resource_id=context.resource.resource_id,
            revision=str(
                metadata.get("current_revision")
                or context.resource.accepted_descriptor.revision
            ),
            digest=str(
                metadata.get("current_digest")
                or context.resource.accepted_descriptor.digest
            ),
            operation_digests={
                tool.operation: tool.descriptor_digest for tool in tools
            },
        )

    async def list_tools(
        self, context: GatewayProviderContext
    ) -> Sequence[ProviderTool]:
        return await self._tools(context)

    async def admit_call(
        self,
        context: GatewayProviderContext,
        *,
        operation: str,
        arguments: Mapping[str, Any],
        invocation_id: str,
    ) -> ProviderCallAdmission:
        del arguments, invocation_id
        if operation not in {tool.operation for tool in await self._tools(context)}:
            return ProviderCallAdmission(
                allowed=False,
                reason="managed_surface_denied",
            )
        return ProviderCallAdmission(allowed=True)

    async def call_tool(
        self,
        context: GatewayProviderContext,
        *,
        operation: str,
        arguments: Mapping[str, Any],
        invocation_id: str,
    ) -> ProviderCallResult:
        payload = await self._invoke(
            context,
            method="tools/call",
            params={
                "name": operation,
                "arguments": dict(arguments),
                "_meta": {"connection_hub/invocation_id": invocation_id},
            },
        )
        error = payload.get("error")
        if error is not None:
            return ProviderCallResult(
                structured_content={
                    "ok": False,
                    "error": "managed_surface_denied",
                    "reason": "managed_surface_denied",
                    "retryable": False,
                },
                is_error=True,
            )
        result = payload.get("result")
        if not isinstance(result, Mapping):
            raise RuntimeError("managed_surface_result_invalid")
        content = result.get("content")
        return ProviderCallResult(
            structured_content=result.get("structuredContent"),
            content=tuple(content) if isinstance(content, list) else (),
            is_error=bool(result.get("isError", False)),
        )


def _discovery_caller_types(connections: Mapping[str, Any]) -> set[str]:
    delegated = connections.get("delegated_credentials")
    delegated = delegated if isinstance(delegated, Mapping) else {}
    gateway = delegated.get("gateway")
    gateway = gateway if isinstance(gateway, Mapping) else {}
    discovery = gateway.get("requestable_discovery")
    discovery = discovery if isinstance(discovery, Mapping) else {}
    raw = discovery.get("caller_types")
    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()
    if not isinstance(raw, (list, tuple, set)):
        return set()
    return {str(item or "").strip().lower() for item in raw if str(item or "").strip()}


async def build_hosted_gateway_binding(
    *,
    request: Any,
    access_service: Any,
    remote_mcp_service: Any,
    invocation_policy_service: Any,
    tenant: str,
    project: str,
    connections: Mapping[str, Any],
    managed_dispatch: ManagedDispatch | None = None,
) -> HostedGatewayBinding:
    view = DelegatedCredentialView.from_request(request)
    if (
        not view.present
        or not view.grantor_user_id
        or not view.registry_access_id
        or not view.client_id
    ):
        raise DelegatedGatewayError("gateway_caller_credential_missing")
    card = await access_service.card_for_access_id(
        grantor_subject=view.grantor_user_id,
        access_id=view.registry_access_id,
    )
    if card is None:
        raise DelegatedGatewayError(
            "card_not_found", access_id=view.registry_access_id
        )
    if card.client_id != view.client_id:
        raise DelegatedGatewayError(
            "card_caller_mismatch", access_id=view.registry_access_id
        )
    capabilities: tuple[str, ...] = ()
    if card.caller_kind in _discovery_caller_types(connections):
        capabilities = (DISCOVER_REQUESTABLE,)
    caller = GatewayCaller(
        caller_type=card.caller_kind,
        access_id=card.access_id,
        caller_profile_id=caller_profile_id_for_card(card),
        client_id=card.client_id,
        capabilities=capabilities,
    )
    cards = HostedCardReader(
        access_service=access_service,
        grantor_subject=view.grantor_user_id,
        access_id=view.registry_access_id,
        tenant=tenant,
        project=project,
        capabilities=capabilities,
    )
    managed_host = HostedManagedKDCubeMCPHost(dispatch=managed_dispatch)
    providers = DelegatedMCPProviderRegistry(
        (
            ExternalRemoteMCPProvider(remote_mcp_service),
            ManagedKDCubeMCPProvider(
                managed_host,
                resource_kinds=("catalog", "managed_kdcube_mcp"),
            ),
        )
    )
    requestable = HostedRequestableResources(
        access_service=access_service,
        grantor_user={
            "user_id": view.grantor_user_id,
            "roles": list(view.grantor_roles),
        },
        tenant=tenant,
        project=project,
    )
    return HostedGatewayBinding(
        gateway=DelegatedMCPGateway(
            cards=cards,
            providers=providers,
            invocation_policy=HostedInvocationPolicy(invocation_policy_service),
            audit=HostedGatewayAudit(),
            requestable=requestable,
        ),
        caller=caller,
    )


__all__ = [
    "HostedCardReader",
    "HostedGatewayAudit",
    "HostedGatewayBinding",
    "HostedInvocationPolicy",
    "HostedManagedKDCubeMCPHost",
    "HostedRequestableResources",
    "ManagedSurfaceLocator",
    "build_hosted_gateway_binding",
    "gateway_resource_metadata",
    "managed_surface_locator",
]

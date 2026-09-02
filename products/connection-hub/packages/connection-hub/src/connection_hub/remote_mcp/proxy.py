# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Live card, connector, descriptor, and tool checks for proxy calls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from connection_hub.delegated_credentials.credential_view import (
    DelegatedCredentialView,
)
from connection_hub.invocation_policy import (
    SURFACE_OUTER,
    InvocationAuthority,
    InvocationDecision,
    InvocationPolicyService,
    canonical_request_digest,
)
from connection_hub.remote_mcp.models import (
    CONNECTOR_ACTIVE,
    DESCRIPTOR_ACCEPTED,
    RemoteMCPConnector,
    RemoteMCPTool,
    connector_id_from_resource,
)
from connection_hub.remote_mcp.service import RemoteMCPConnectorService

EXTERNAL_MCP_GRANT = "external_mcp:use"


class RemoteMCPProxyError(PermissionError):
    def __init__(
        self,
        reason: str,
        *,
        resource: str = "",
        operation: str = "",
        connector_id: str = "",
        proxy_name: str = "",
        consent_required: bool = False,
        retryable: bool = False,
        access_id: str = "",
        card_revision: int = 0,
        invocation_id: str = "",
        policy: Mapping[str, Any] | None = None,
        details: Mapping[str, Any] | None = None,
        client_id: str = "",
        recovery_url: str = "",
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.resource = resource
        self.operation = operation
        self.connector_id = connector_id
        self.proxy_name = proxy_name
        self.consent_required = consent_required
        self.retryable = retryable
        self.access_id = access_id
        self.card_revision = max(0, int(card_revision or 0))
        self.invocation_id = invocation_id
        self.policy = dict(policy or {})
        self.details = dict(details or {})
        self.client_id = client_id
        self.recovery_url = recovery_url

    def to_dict(self) -> dict[str, Any]:
        out = {
            "ok": False,
            "error": "remote_mcp_proxy_denied",
            "code": self.reason,
            "reason": self.reason,
            "resource": self.resource,
            "operation": self.operation,
            "connector_id": self.connector_id,
            "tool_name": self.proxy_name,
            "consent_required": self.consent_required,
            "retryable": self.retryable,
        }
        if self.access_id:
            out["access_id"] = self.access_id
        if self.card_revision:
            out["card_revision"] = self.card_revision
        if self.invocation_id:
            out["invocation_id"] = self.invocation_id
        if self.policy:
            out["policy"] = self.policy
        if self.details:
            out["details"] = self.details
        if self.reason in {
            "delegated_invocation_id_required",
            "delegated_invocation_limit_exhausted",
        }:
            out["available_choices"] = ["allow_once", "allow_always"]
            out["consent"] = {
                "kind": "delegated_invocation_policy",
                "reason": self.reason,
                "agent_client_id": self.client_id,
                "access_id": self.access_id,
                "resource": self.resource,
                "outer_operation": self.operation,
                "tool_name": self.proxy_name,
                "connection_hub_url": self.recovery_url,
                "available_choices": ["allow_once", "allow_always"],
            }
        elif self.reason in {
            "connector_grant_not_consented",
            "operation_not_consented",
        }:
            choices = ["allow_once", "allow_always"]
            grant_payload = {
                "client_id": self.client_id,
                "access_id": self.access_id,
                "resource": self.resource,
                "claims": [EXTERNAL_MCP_GRANT],
                "resource_operations": {
                    self.resource: [self.operation],
                },
                "invocation_change_id": self.invocation_id,
            }
            out["available_choices"] = choices
            out["consent"] = {
                "kind": "delegated_agent_grant",
                "reason": self.reason,
                "agent_client_id": self.client_id,
                "access_id": self.access_id,
                "resource": self.resource,
                "claims": [EXTERNAL_MCP_GRANT],
                "outer_operation": self.operation,
                "tool_name": self.proxy_name,
                "connection_hub_url": self.recovery_url,
                "invocation_change_id": self.invocation_id,
                "invocation_policy": "choose",
                "available_choices": choices,
                "grant": {
                    "operation": "delegated_agent_grant_create",
                    "payload": grant_payload,
                },
            }
        return out


@dataclass(frozen=True)
class RemoteMCPProxyDecision:
    connector: RemoteMCPConnector
    tool: RemoteMCPTool
    resource: str


class RemoteMCPProxy:
    def __init__(
        self,
        service: RemoteMCPConnectorService,
        *,
        invocation_policies: InvocationPolicyService | None = None,
        recovery_url_builder: Callable[
            [DelegatedCredentialView, RemoteMCPProxyDecision], str
        ]
        | None = None,
        grant_url_builder: Callable[
            [DelegatedCredentialView, RemoteMCPProxyDecision, str], str
        ]
        | None = None,
    ) -> None:
        self._service = service
        self._invocation_policies = invocation_policies
        self._recovery_url_builder = recovery_url_builder
        self._grant_url_builder = grant_url_builder

    async def list_authorized(
        self, view: DelegatedCredentialView
    ) -> list[RemoteMCPProxyDecision]:
        owner = self._owner(view)
        decisions: list[RemoteMCPProxyDecision] = []
        for resource in view.resource_grants:
            connector_id = connector_id_from_resource(resource)
            if not connector_id:
                continue
            try:
                connector = await self._service.get(
                    owner_subject=owner, connector_id=connector_id
                )
                discovery = await self._service.observe(connector)
            except Exception:
                continue
            if connector.state != CONNECTOR_ACTIVE:
                continue
            observed = discovery.tool_map()
            grants = view.grants_for_resource(resource)
            selected = view.operations_for_resource(
                resource, matched_resource=resource
            )
            if EXTERNAL_MCP_GRANT not in grants:
                continue
            for tool in connector.tools:
                live = observed.get(tool.name)
                if tool.name not in selected or live is None:
                    continue
                if live.descriptor_digest != tool.descriptor_digest:
                    continue
                decisions.append(
                    RemoteMCPProxyDecision(
                        connector=connector, tool=tool, resource=resource
                    )
                )
        decisions.sort(key=lambda item: item.tool.proxy_name)
        return decisions

    async def resolve(
        self,
        *,
        view: DelegatedCredentialView,
        proxy_name: str,
        invocation_id: str = "",
    ) -> RemoteMCPProxyDecision:
        owner = self._owner(view)
        requested = str(proxy_name or "").strip()
        for resource in view.resource_grants:
            connector_id = connector_id_from_resource(resource)
            if not connector_id:
                continue
            connector = await self._service.get(
                owner_subject=owner, connector_id=connector_id
            )
            tool = connector.proxy_tool_map().get(requested)
            if tool is None:
                continue
            if connector.state != CONNECTOR_ACTIVE:
                raise self._error(
                    "connector_not_active", view, connector, tool, resource,
                    invocation_id=invocation_id,
                )
            grants = view.grants_for_resource(resource)
            if EXTERNAL_MCP_GRANT not in grants:
                raise self._error(
                    "connector_grant_not_consented",
                    view,
                    connector,
                    tool,
                    resource,
                    consent_required=True,
                    invocation_id=invocation_id,
                )
            selected = view.operations_for_resource(
                resource, matched_resource=resource
            )
            if tool.name not in selected:
                raise self._error(
                    "operation_not_consented",
                    view,
                    connector,
                    tool,
                    resource,
                    consent_required=True,
                    invocation_id=invocation_id,
                )
            discovery = await self._service.observe(connector)
            live = discovery.tool_map().get(tool.name)
            if live is None:
                raise self._error(
                    "operation_removed_by_server", view, connector, tool, resource,
                    invocation_id=invocation_id,
                )
            if live.descriptor_digest != tool.descriptor_digest:
                raise self._error(
                    "operation_descriptor_changed", view, connector, tool, resource,
                    invocation_id=invocation_id,
                )
            if connector.descriptor_state == DESCRIPTOR_ACCEPTED:
                return RemoteMCPProxyDecision(
                    connector=connector, tool=tool, resource=resource
                )
            # Drift elsewhere does not suspend an unchanged selected tool. The
            # current call is bounded by this tool's accepted digest.
            return RemoteMCPProxyDecision(
                connector=connector, tool=tool, resource=resource
            )
        raise RemoteMCPProxyError(
            "tool_not_in_delegated_resources", proxy_name=requested
        )

    async def call(
        self,
        *,
        view: DelegatedCredentialView,
        proxy_name: str,
        arguments: Mapping[str, Any],
        invocation_id: str = "",
    ) -> Any:
        decision = await self.resolve(
            view=view,
            proxy_name=proxy_name,
            invocation_id=invocation_id,
        )
        owner = self._owner(view)
        call_arguments = dict(arguments or {})
        authority: InvocationAuthority | None = None
        request_digest = ""
        policy_decision: InvocationDecision | None = None
        if self._invocation_policies is not None:
            authority = InvocationAuthority(
                access_id=view.registry_access_id,
                resource=decision.resource,
                surface=SURFACE_OUTER,
                operation=decision.tool.name,
            )
            request_digest = canonical_request_digest(
                {
                    "operation": decision.tool.name,
                    "arguments": call_arguments,
                }
            )
            policy_decision = await self._invocation_policies.begin(
                owner_subject=owner,
                authority=authority,
                invocation_id=invocation_id,
                request_digest=request_digest if invocation_id else "",
                card_revision=view.card_revision,
                authority_revision=(
                    f"connector:{decision.connector.revision}:"
                    f"descriptor:{decision.connector.descriptor_revision}"
                ),
            )
            if not policy_decision.dispatch:
                if policy_decision.replay and policy_decision.invocation is not None:
                    if policy_decision.invocation.state == "completed":
                        if policy_decision.result_is_error:
                            stored = (
                                policy_decision.result
                                if isinstance(policy_decision.result, Mapping)
                                else {}
                            )
                            raise self._policy_error(
                                view=view,
                                decision=decision,
                                policy_decision=policy_decision,
                                invocation_id=invocation_id,
                                reason=str(
                                    stored.get("reason")
                                    or "remote_mcp_call_failed"
                                ),
                            )
                        return policy_decision.result
                raise self._policy_error(
                    view=view,
                    decision=decision,
                    policy_decision=policy_decision,
                    invocation_id=invocation_id,
                )

        try:
            result = await self._service.call_tool(
                connector=decision.connector,
                tool_name=decision.tool.name,
                arguments=call_arguments,
            )
        except Exception as exc:
            if (
                self._invocation_policies is not None
                and authority is not None
                and policy_decision is not None
                and policy_decision.invocation is not None
                and invocation_id
            ):
                await self._invocation_policies.complete(
                    owner_subject=owner,
                    authority=authority,
                    invocation_id=invocation_id,
                    request_digest=request_digest,
                    result={
                        "ok": False,
                        "reason": "remote_mcp_call_failed",
                        "failure_type": type(exc).__name__,
                    },
                    result_is_error=True,
                )
            raise
        if (
            self._invocation_policies is not None
            and authority is not None
            and policy_decision is not None
            and policy_decision.invocation is not None
            and invocation_id
        ):
            await self._invocation_policies.complete(
                owner_subject=owner,
                authority=authority,
                invocation_id=invocation_id,
                request_digest=request_digest,
                result=result,
            )
        return result

    def _policy_error(
        self,
        *,
        view: DelegatedCredentialView,
        decision: RemoteMCPProxyDecision,
        policy_decision: InvocationDecision,
        invocation_id: str,
        reason: str = "",
    ) -> RemoteMCPProxyError:
        policy = (
            policy_decision.policy.to_public_dict()
            if policy_decision.policy is not None
            else {}
        )
        recovery_url = (
            self._recovery_url_builder(view, decision)
            if self._recovery_url_builder is not None
            else ""
        )
        return RemoteMCPProxyError(
            reason or policy_decision.reason,
            resource=decision.resource,
            operation=decision.tool.name,
            connector_id=decision.connector.connector_id,
            proxy_name=decision.tool.proxy_name,
            consent_required=policy_decision.reason
            in {
                "delegated_invocation_id_required",
                "delegated_invocation_limit_exhausted",
            },
            retryable=policy_decision.retryable,
            access_id=view.registry_access_id,
            card_revision=view.card_revision,
            invocation_id=invocation_id,
            policy=policy,
            details={"replay": policy_decision.replay},
            client_id=view.client_id,
            recovery_url=recovery_url,
        )

    @staticmethod
    def _owner(view: DelegatedCredentialView) -> str:
        owner = str(view.grantor_user_id or "").strip()
        if not view.present or not owner:
            raise RemoteMCPProxyError("delegated_identity_missing")
        return owner

    def _error(
        self,
        reason: str,
        view: DelegatedCredentialView,
        connector: RemoteMCPConnector,
        tool: RemoteMCPTool,
        resource: str,
        *,
        consent_required: bool = False,
        invocation_id: str = "",
    ) -> RemoteMCPProxyError:
        decision = RemoteMCPProxyDecision(
            connector=connector,
            tool=tool,
            resource=resource,
        )
        recovery_url = (
            self._grant_url_builder(view, decision, invocation_id)
            if consent_required and self._grant_url_builder is not None
            else ""
        )
        return RemoteMCPProxyError(
            reason,
            resource=resource,
            operation=tool.name,
            connector_id=connector.connector_id,
            proxy_name=tool.proxy_name,
            consent_required=consent_required,
            access_id=view.registry_access_id,
            card_revision=view.card_revision,
            invocation_id=invocation_id,
            client_id=view.client_id,
            recovery_url=recovery_url,
        )


__all__ = [
    "EXTERNAL_MCP_GRANT",
    "RemoteMCPProxy",
    "RemoteMCPProxyDecision",
    "RemoteMCPProxyError",
]

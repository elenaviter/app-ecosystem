"""Descriptor-bound project membership adapter for Connection Hub."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from connection_hub.bundle_operations import (
    BundleOperationResultError,
    normalize_bundle_operation_result,
)
from connection_hub.delegated_credentials.named_service_policy import clean_text
from connection_hub.delegated_credentials.project_agent_card_access import (
    AgentCardAuthorizationError,
    AgentCardDecision,
    RefusingAgentCardAuthorizationPort,
)
from connection_hub.delegated_credentials.project_control_card_access import (
    ControlCardAuthorizationError,
    ProjectControlCardDecision,
    RefusingProjectControlCardAuthorizationPort,
)
from connection_hub.delegated_credentials.project_authorization import (
    ProjectAuthorizationDecision,
    ProjectAuthorizationError,
    ProjectAuthorizationRequest,
    ProjectMembershipConfig,
    ProjectMembershipEvidence,
    ResolverBackedProjectAuthorizationPort,
)
from kdcube_ai_app.apps.chat.sdk.infra.bundle_operations import (
    call_bundle_operation,
)


BundleOperationCaller = Callable[..., Awaitable[Mapping[str, Any]]]


def _refusal_reason(response: Mapping[str, Any]) -> str:
    error = response.get("error")
    if isinstance(error, Mapping):
        return clean_text(error.get("code") or response.get("reason"))
    return clean_text(error or response.get("reason"))


class BundleOperationProjectMembershipResolver:
    """Resolve canonical membership through one request-bound peer operation."""

    def __init__(
        self,
        *,
        bundle_id: str,
        operation: str,
        caller: BundleOperationCaller = call_bundle_operation,
    ) -> None:
        self._bundle_id = clean_text(bundle_id)
        self._operation = clean_text(operation)
        self._caller = caller

    async def resolve_project_membership(
        self,
        *,
        project_ref: str,
        subject: str,
    ) -> ProjectMembershipEvidence | None:
        try:
            response = await self._caller(
                bundle_id=self._bundle_id,
                operation=self._operation,
                data={"project_ref": project_ref, "subject": subject},
            )
        except Exception as exc:
            raise ProjectAuthorizationError(
                "project_membership_provider_unavailable"
            ) from exc
        if not isinstance(response, Mapping):
            raise ProjectAuthorizationError(
                "project_membership_provider_response_invalid"
            )
        try:
            response = normalize_bundle_operation_result(self._operation, response)
        except BundleOperationResultError as exc:
            raise ProjectAuthorizationError(exc.reason) from exc
        if response.get("ok") is not True:
            reason = _refusal_reason(response)
            raise ProjectAuthorizationError(
                reason or "project_membership_provider_refused"
            )
        membership = response.get("membership")
        if membership is None:
            return None
        if not isinstance(membership, Mapping):
            raise ProjectAuthorizationError(
                "project_membership_provider_response_invalid"
            )
        return ProjectMembershipEvidence.build(
            project_ref=membership.get("project_ref"),
            subject=membership.get("subject"),
            role=membership.get("role"),
            delegable_grants=membership.get("delegable_grants", ()),
            evidence=(
                membership.get("evidence")
                if isinstance(membership.get("evidence"), Mapping)
                else {}
            ),
        )


DEFAULT_AGENT_CARD_OPERATION = "project_agent_card_authorize"


class BundleOperationAgentCardAuthorizer:
    """Ask the project host whether this person may open or change an agent's Card (W319).

    Request-bound like the membership question: the host answers for the
    person whose session made the Connection Hub call.
    """

    def __init__(
        self,
        *,
        bundle_id: str,
        operation: str = DEFAULT_AGENT_CARD_OPERATION,
        caller: BundleOperationCaller = call_bundle_operation,
    ) -> None:
        self._bundle_id = clean_text(bundle_id)
        self._operation = clean_text(operation) or DEFAULT_AGENT_CARD_OPERATION
        self._caller = caller

    async def authorize_agent_card(
        self, *, access_id: str, project_ref: str, action: str
    ) -> AgentCardDecision:
        try:
            response = await self._caller(
                bundle_id=self._bundle_id,
                operation=self._operation,
                data={"access_id": access_id, "project_ref": project_ref, "action": action},
            )
        except Exception as exc:
            raise AgentCardAuthorizationError("project_agent_card_provider_unavailable") from exc
        if not isinstance(response, Mapping):
            raise AgentCardAuthorizationError("project_agent_card_provider_response_invalid")
        try:
            response = normalize_bundle_operation_result(self._operation, response)
        except BundleOperationResultError as exc:
            raise AgentCardAuthorizationError(exc.reason) from exc
        if response.get("ok") is not True:
            reason = _refusal_reason(response) or "project_agent_card_provider_refused"
            return AgentCardDecision(
                allowed=False, reason=reason, access_id=access_id, project_ref=project_ref, action=action
            )
        return AgentCardDecision.from_mapping(response.get("decision"))


def descriptor_agent_card_port(
    entrypoint: Any,
    *,
    caller: BundleOperationCaller = call_bundle_operation,
) -> BundleOperationAgentCardAuthorizer | RefusingAgentCardAuthorizationPort:
    """The agent Card question goes to the project membership provider's bundle.

    ``project_membership.provider.agent_card_operation`` names the operation;
    without a provider bundle it fails closed.
    """

    props = getattr(entrypoint, "bundle_props", None)
    raw = props.get("project_membership") if isinstance(props, Mapping) else None
    provider = raw.get("provider") if isinstance(raw, Mapping) else None
    bundle_id = clean_text(provider.get("bundle_id")) if isinstance(provider, Mapping) else ""
    if not bundle_id:
        return RefusingAgentCardAuthorizationPort("project_agent_card_provider_not_configured")
    return BundleOperationAgentCardAuthorizer(
        bundle_id=bundle_id,
        operation=clean_text(provider.get("agent_card_operation")) or DEFAULT_AGENT_CARD_OPERATION,
        caller=caller,
    )


DEFAULT_CONTROL_CARD_OPERATION = "project_control_card_authorize"


class BundleOperationControlCardAuthorizer:
    """Ask the project host whether this person may read or change the project's Control Card (W260).

    Request-bound like the membership question: the host answers for the
    person whose session made the Connection Hub call, and names the Card's
    creator, under whose key the Card is stored.
    """

    def __init__(
        self,
        *,
        bundle_id: str,
        operation: str = DEFAULT_CONTROL_CARD_OPERATION,
        caller: BundleOperationCaller = call_bundle_operation,
    ) -> None:
        self._bundle_id = clean_text(bundle_id)
        self._operation = clean_text(operation) or DEFAULT_CONTROL_CARD_OPERATION
        self._caller = caller

    async def authorize_project_control_card(
        self, *, control_id: str, project_ref: str, action: str, access_id: str = ""
    ) -> ProjectControlCardDecision:
        data = {"control_id": control_id, "project_ref": project_ref, "action": action}
        if clean_text(access_id):
            # The agent Card an attach or detach binds (owner linking vias).
            data["access_id"] = clean_text(access_id)
        try:
            response = await self._caller(
                bundle_id=self._bundle_id,
                operation=self._operation,
                data=data,
            )
        except Exception as exc:
            raise ControlCardAuthorizationError("project_control_card_provider_unavailable") from exc
        if not isinstance(response, Mapping):
            raise ControlCardAuthorizationError("project_control_card_provider_response_invalid")
        try:
            response = normalize_bundle_operation_result(self._operation, response)
        except BundleOperationResultError as exc:
            raise ControlCardAuthorizationError(exc.reason) from exc
        if response.get("ok") is not True:
            reason = _refusal_reason(response) or "project_control_card_provider_refused"
            return ProjectControlCardDecision(
                allowed=False, reason=reason, control_id=control_id, project_ref=project_ref, action=action
            )
        return ProjectControlCardDecision.from_mapping(response.get("decision"))


def descriptor_control_card_port(
    entrypoint: Any,
    *,
    caller: BundleOperationCaller = call_bundle_operation,
) -> BundleOperationControlCardAuthorizer | RefusingProjectControlCardAuthorizationPort:
    """The project Control Card question goes to the project membership provider's bundle.

    ``project_membership.provider.control_card_operation`` names the operation;
    without a provider bundle it fails closed.
    """

    props = getattr(entrypoint, "bundle_props", None)
    raw = props.get("project_membership") if isinstance(props, Mapping) else None
    provider = raw.get("provider") if isinstance(raw, Mapping) else None
    bundle_id = clean_text(provider.get("bundle_id")) if isinstance(provider, Mapping) else ""
    if not bundle_id:
        return RefusingProjectControlCardAuthorizationPort("project_control_card_provider_not_configured")
    return BundleOperationControlCardAuthorizer(
        bundle_id=bundle_id,
        operation=clean_text(provider.get("control_card_operation")) or DEFAULT_CONTROL_CARD_OPERATION,
        caller=caller,
    )


class RefusingProjectAuthorizationPort:
    """Fail closed by a descriptor/configuration reason."""

    def __init__(self, reason: str) -> None:
        self._reason = reason

    async def authorize_project_person_control(
        self,
        request: ProjectAuthorizationRequest,
    ) -> ProjectAuthorizationDecision:
        return ProjectAuthorizationDecision.deny(request, reason=self._reason)


def project_membership_config(entrypoint: Any) -> ProjectMembershipConfig:
    props = getattr(entrypoint, "bundle_props", None)
    raw = props.get("project_membership") if isinstance(props, Mapping) else None
    return ProjectMembershipConfig.from_mapping(raw)


def descriptor_project_authorization_port(
    entrypoint: Any,
    *,
    caller: BundleOperationCaller = call_bundle_operation,
) -> ResolverBackedProjectAuthorizationPort | RefusingProjectAuthorizationPort:
    """Build the live port from the current Connection Hub descriptor."""

    try:
        config = project_membership_config(entrypoint)
    except ProjectAuthorizationError as exc:
        return RefusingProjectAuthorizationPort(exc.reason)
    resolver = (
        BundleOperationProjectMembershipResolver(
            bundle_id=config.provider_bundle_id,
            operation=config.provider_operation,
            caller=caller,
        )
        if config.provider_configured
        else None
    )
    return ResolverBackedProjectAuthorizationPort(
        resolver=resolver,
        administrative_roles=config.administrative_roles,
    )


__all__ = [
    "BundleOperationProjectMembershipResolver",
    "RefusingProjectAuthorizationPort",
    "descriptor_project_authorization_port",
    "project_membership_config",
]

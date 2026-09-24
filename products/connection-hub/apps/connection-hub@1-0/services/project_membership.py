"""Descriptor-bound project membership adapter for Connection Hub."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from connection_hub.delegated_credentials.named_service_policy import clean_text
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
        if response.get("ok") is not True:
            reason = clean_text(response.get("error") or response.get("reason"))
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

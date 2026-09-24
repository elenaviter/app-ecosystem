"""Descriptor-bound invitation binding adapter for Connection Hub."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from connection_hub.bundle_operations import (
    BundleOperationResultError,
    normalize_bundle_operation_result,
)
from connection_hub.delegated_credentials.named_service_policy import clean_text
from connection_hub.delegated_credentials.project_invitation_binding import (
    ProjectInvitationBindingConfig,
    ProjectInvitationBindingError,
    ProjectInvitationBindingEvidence,
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


class BundleOperationProjectInvitationBindingResolver:
    """Resolve the invitation bound to the current provider-verified session."""

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

    async def resolve_project_invitation_binding(
        self,
        *,
        project_ref: str,
        invitation_ref: str,
    ) -> ProjectInvitationBindingEvidence | None:
        try:
            response = await self._caller(
                bundle_id=self._bundle_id,
                operation=self._operation,
                data={
                    "project_ref": project_ref,
                    "invitation_ref": invitation_ref,
                },
            )
        except Exception as exc:
            raise ProjectInvitationBindingError(
                "project_invitation_binding_provider_unavailable"
            ) from exc
        if not isinstance(response, Mapping):
            raise ProjectInvitationBindingError(
                "project_invitation_binding_provider_response_invalid"
            )
        try:
            response = normalize_bundle_operation_result(self._operation, response)
        except BundleOperationResultError as exc:
            raise ProjectInvitationBindingError(exc.reason) from exc
        if response.get("ok") is not True:
            raise ProjectInvitationBindingError(
                _refusal_reason(response)
                or "project_invitation_binding_provider_refused"
            )
        binding = response.get("binding")
        if binding is None:
            return None
        if not isinstance(binding, Mapping):
            raise ProjectInvitationBindingError(
                "project_invitation_binding_provider_response_invalid"
            )
        return ProjectInvitationBindingEvidence.build(
            project_ref=binding.get("project_ref"),
            invitation_ref=binding.get("invitation_ref"),
            control_id=binding.get("control_id"),
            person_subject=binding.get("person_subject"),
            email=binding.get("email"),
        )


class RefusingProjectInvitationBindingResolver:
    """Fail closed with the descriptor/configuration reason."""

    def __init__(self, reason: str) -> None:
        self._reason = reason

    async def resolve_project_invitation_binding(
        self,
        *,
        project_ref: str,
        invitation_ref: str,
    ) -> ProjectInvitationBindingEvidence | None:
        del project_ref, invitation_ref
        raise ProjectInvitationBindingError(self._reason)


def project_invitation_binding_config(
    entrypoint: Any,
) -> ProjectInvitationBindingConfig:
    props = getattr(entrypoint, "bundle_props", None)
    raw = (
        props.get("project_invitation_binding") if isinstance(props, Mapping) else None
    )
    return ProjectInvitationBindingConfig.from_mapping(raw)


def descriptor_project_invitation_binding_resolver(
    entrypoint: Any,
    *,
    caller: BundleOperationCaller = call_bundle_operation,
) -> (
    BundleOperationProjectInvitationBindingResolver
    | RefusingProjectInvitationBindingResolver
):
    """Build the live binding resolver from the current descriptor."""

    try:
        config = project_invitation_binding_config(entrypoint)
    except ProjectInvitationBindingError as exc:
        return RefusingProjectInvitationBindingResolver(exc.reason)
    if not config.provider_configured:
        return RefusingProjectInvitationBindingResolver(
            "project_invitation_binding_provider_missing"
        )
    return BundleOperationProjectInvitationBindingResolver(
        bundle_id=config.provider_bundle_id,
        operation=config.provider_operation,
        caller=caller,
    )


__all__ = [
    "BundleOperationProjectInvitationBindingResolver",
    "RefusingProjectInvitationBindingResolver",
    "descriptor_project_invitation_binding_resolver",
    "project_invitation_binding_config",
]

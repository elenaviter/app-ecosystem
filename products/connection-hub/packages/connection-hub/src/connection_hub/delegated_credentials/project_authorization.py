# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Host-neutral authorization contract for project-held person Control Cards."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from connection_hub.delegated_credentials.named_service_policy import clean_text


PROJECT_PERSON_CONTROL_CREATE = "project.person_control.create"
PROJECT_PERSON_CONTROL_READ = "project.person_control.read"
PROJECT_PERSON_CONTROL_UPDATE = "project.person_control.update"
PROJECT_PERSON_CONTROL_REVOKE = "project.person_control.revoke"
PROJECT_PERSON_CONTROL_OPERATIONS = frozenset(
    {
        PROJECT_PERSON_CONTROL_CREATE,
        PROJECT_PERSON_CONTROL_READ,
        PROJECT_PERSON_CONTROL_UPDATE,
        PROJECT_PERSON_CONTROL_REVOKE,
    }
)


class ProjectAuthorizationError(ValueError):
    """A project authorization request or decision is invalid."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _required(value: Any, reason: str) -> str:
    result = clean_text(value)
    if not result:
        raise ProjectAuthorizationError(reason)
    return result


def _operation(value: Any) -> str:
    operation = _required(value, "project_authorization_operation_missing")
    if operation not in PROJECT_PERSON_CONTROL_OPERATIONS:
        raise ProjectAuthorizationError("project_authorization_operation_invalid")
    return operation


def _grants(values: Any) -> tuple[str, ...]:
    if isinstance(values, str):
        values = (values,)
    if values is None:
        values = ()
    if not isinstance(values, (list, tuple, set, frozenset)):
        raise ProjectAuthorizationError("project_authorization_grants_invalid")
    return tuple(sorted({clean_text(value) for value in values if clean_text(value)}))


@dataclass(frozen=True)
class ProjectAuthorizationRequest:
    """Trusted lifecycle coordinates presented to the project policy host.

    The actor comes from the authenticated platform session. ``project_ref``
    and ``target_subject`` identify the record being managed; they confer no
    authority by themselves. Creator bootstrap is a policy decision made by
    the port for ``PROJECT_PERSON_CONTROL_CREATE``, never a request flag.
    """

    actor_subject: str
    project_ref: str
    target_subject: str
    operation: str
    request_id: str

    @classmethod
    def build(
        cls,
        *,
        actor_subject: Any,
        project_ref: Any,
        target_subject: Any,
        operation: Any,
        request_id: Any,
    ) -> "ProjectAuthorizationRequest":
        return cls(
            actor_subject=_required(
                actor_subject,
                "project_authorization_actor_missing",
            ),
            project_ref=_required(
                project_ref,
                "project_authorization_project_ref_missing",
            ),
            target_subject=_required(
                target_subject,
                "project_authorization_target_missing",
            ),
            operation=_operation(operation),
            request_id=_required(
                request_id,
                "project_authorization_request_id_missing",
            ),
        )


@dataclass(frozen=True)
class ProjectAuthorizationDecision:
    """A policy answer bound to one exact project lifecycle request."""

    allowed: bool
    actor_subject: str
    project_ref: str
    target_subject: str
    operation: str
    request_id: str
    reason: str = ""
    delegable_grants: tuple[str, ...] = ()
    platform_admin: bool = False
    evidence: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def allow(
        cls,
        request: ProjectAuthorizationRequest,
        *,
        delegable_grants: Any = (),
        platform_admin: bool = False,
        evidence: Mapping[str, Any] | None = None,
    ) -> "ProjectAuthorizationDecision":
        return cls(
            allowed=True,
            actor_subject=request.actor_subject,
            project_ref=request.project_ref,
            target_subject=request.target_subject,
            operation=request.operation,
            request_id=request.request_id,
            delegable_grants=_grants(delegable_grants),
            platform_admin=bool(platform_admin),
            evidence=copy.deepcopy(dict(evidence or {})),
        )

    @classmethod
    def deny(
        cls,
        request: ProjectAuthorizationRequest,
        *,
        reason: Any,
        evidence: Mapping[str, Any] | None = None,
    ) -> "ProjectAuthorizationDecision":
        return cls(
            allowed=False,
            actor_subject=request.actor_subject,
            project_ref=request.project_ref,
            target_subject=request.target_subject,
            operation=request.operation,
            request_id=request.request_id,
            reason=_required(reason, "project_authorization_denial_reason_missing"),
            evidence=copy.deepcopy(dict(evidence or {})),
        )

    def validate_for(self, request: ProjectAuthorizationRequest) -> None:
        """Refuse a replayed, broadened, or malformed decision."""

        if not isinstance(self.allowed, bool):
            raise ProjectAuthorizationError("project_authorization_allowed_invalid")
        if clean_text(self.actor_subject) != request.actor_subject:
            raise ProjectAuthorizationError("project_authorization_actor_mismatch")
        if clean_text(self.project_ref) != request.project_ref:
            raise ProjectAuthorizationError("project_authorization_project_ref_mismatch")
        if clean_text(self.target_subject) != request.target_subject:
            raise ProjectAuthorizationError("project_authorization_target_mismatch")
        if _operation(self.operation) != request.operation:
            raise ProjectAuthorizationError("project_authorization_operation_mismatch")
        if clean_text(self.request_id) != request.request_id:
            raise ProjectAuthorizationError("project_authorization_request_id_mismatch")
        if not isinstance(self.platform_admin, bool):
            raise ProjectAuthorizationError("project_authorization_admin_flag_invalid")
        if not isinstance(self.evidence, Mapping):
            raise ProjectAuthorizationError("project_authorization_evidence_invalid")
        if self.allowed:
            _grants(self.delegable_grants)
            if clean_text(self.reason):
                raise ProjectAuthorizationError("project_authorization_allow_reason_invalid")
        elif not clean_text(self.reason):
            raise ProjectAuthorizationError("project_authorization_denial_reason_missing")
        elif self.delegable_grants or self.platform_admin:
            raise ProjectAuthorizationError("project_authorization_denial_authority_invalid")


class ProjectAuthorizationPort(Protocol):
    """Project-owned policy evaluator injected by the application host."""

    async def authorize_project_person_control(
        self,
        request: ProjectAuthorizationRequest,
    ) -> ProjectAuthorizationDecision:
        ...


__all__ = [
    "PROJECT_PERSON_CONTROL_CREATE",
    "PROJECT_PERSON_CONTROL_OPERATIONS",
    "PROJECT_PERSON_CONTROL_READ",
    "PROJECT_PERSON_CONTROL_REVOKE",
    "PROJECT_PERSON_CONTROL_UPDATE",
    "ProjectAuthorizationDecision",
    "ProjectAuthorizationError",
    "ProjectAuthorizationPort",
    "ProjectAuthorizationRequest",
]

# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Host-neutral authorization contract for project-held person Control Cards."""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from connection_hub.delegated_credentials.named_service_policy import clean_text

PROJECT_PERSON_CONTROL_CREATE = "project.person_control.create"
PROJECT_PERSON_CONTROL_READ = "project.person_control.read"
PROJECT_PERSON_CONTROL_UPDATE = "project.person_control.update"
PROJECT_PERSON_CONTROL_REVOKE = "project.person_control.revoke"
PROJECT_PERSON_MY_CARD_SEED = "project.person_my_card.seed"
PROJECT_INVITATION_CONTROL_CREATE = "project.invitation_control.create"
PROJECT_INVITATION_CONTROL_READ = "project.invitation_control.read"
PROJECT_INVITATION_CONTROL_UPDATE = "project.invitation_control.update"
PROJECT_INVITATION_CONTROL_REVOKE = "project.invitation_control.revoke"
PROJECT_INVITATION_CONTROL_OPERATIONS = frozenset(
    {
        PROJECT_INVITATION_CONTROL_CREATE,
        PROJECT_INVITATION_CONTROL_READ,
        PROJECT_INVITATION_CONTROL_UPDATE,
        PROJECT_INVITATION_CONTROL_REVOKE,
    }
)
PROJECT_PERSON_CONTROL_OPERATIONS = frozenset(
    {
        PROJECT_PERSON_CONTROL_CREATE,
        PROJECT_PERSON_CONTROL_READ,
        PROJECT_PERSON_CONTROL_UPDATE,
        PROJECT_PERSON_CONTROL_REVOKE,
        PROJECT_PERSON_MY_CARD_SEED,
        *PROJECT_INVITATION_CONTROL_OPERATIONS,
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
    if any(not isinstance(value, str) for value in values):
        raise ProjectAuthorizationError("project_authorization_grants_invalid")
    return tuple(sorted({clean_text(value) for value in values if clean_text(value)}))


def _roles(values: Iterable[Any] | str | None) -> frozenset[str]:
    source: Iterable[Any]
    if values is None:
        source = ()
    elif isinstance(values, str):
        source = values.replace(",", " ").split()
    elif isinstance(values, (list, tuple, set, frozenset)):
        source = values
    else:
        raise ProjectAuthorizationError("project_administrative_roles_invalid")
    if any(not isinstance(value, str) for value in source):
        raise ProjectAuthorizationError("project_administrative_roles_invalid")
    return frozenset(role for value in source if (role := clean_text(value).lower()))


@dataclass(frozen=True)
class ProjectMembershipConfig:
    """Descriptor-owned membership provider and administrative roles."""

    provider_bundle_id: str = ""
    provider_operation: str = ""
    administrative_roles: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: Any) -> "ProjectMembershipConfig":
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise ProjectAuthorizationError("project_membership_config_invalid")
        provider = value.get("provider")
        if provider is None:
            provider = {}
        if not isinstance(provider, Mapping):
            raise ProjectAuthorizationError("project_membership_provider_invalid")
        bundle_id = clean_text(provider.get("bundle_id"))
        operation = clean_text(provider.get("operation"))
        if bool(bundle_id) != bool(operation):
            raise ProjectAuthorizationError("project_membership_provider_invalid")
        return cls(
            provider_bundle_id=bundle_id,
            provider_operation=operation,
            administrative_roles=tuple(
                sorted(_roles(value.get("administrative_roles")))
            ),
        )

    @property
    def provider_configured(self) -> bool:
        return bool(self.provider_bundle_id and self.provider_operation)


@dataclass(frozen=True)
class ProjectMembershipEvidence:
    """One host-owned project membership answer, with no implied authority."""

    project_ref: str
    subject: str
    role: str
    delegable_grants: tuple[str, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        *,
        project_ref: Any,
        subject: Any,
        role: Any,
        delegable_grants: Any = (),
        evidence: Mapping[str, Any] | None = None,
    ) -> ProjectMembershipEvidence:
        if evidence is not None and not isinstance(evidence, Mapping):
            raise ProjectAuthorizationError("project_membership_evidence_invalid")
        return cls(
            project_ref=_required(
                project_ref,
                "project_membership_project_ref_missing",
            ),
            subject=_required(subject, "project_membership_subject_missing"),
            role=_required(role, "project_membership_role_missing").lower(),
            delegable_grants=_grants(delegable_grants),
            evidence=copy.deepcopy(dict(evidence or {})),
        )

    def validate_for(self, *, project_ref: str, subject: str) -> None:
        if clean_text(self.project_ref) != project_ref:
            raise ProjectAuthorizationError("project_membership_project_ref_mismatch")
        if clean_text(self.subject) != subject:
            raise ProjectAuthorizationError("project_membership_subject_mismatch")
        _required(self.role, "project_membership_role_missing")
        _grants(self.delegable_grants)
        if not isinstance(self.evidence, Mapping):
            raise ProjectAuthorizationError("project_membership_evidence_invalid")

    def to_public_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "project_ref": self.project_ref,
            "subject": self.subject,
            "role": self.role,
        }
        if self.evidence:
            result["evidence"] = copy.deepcopy(dict(self.evidence))
        return result


class ProjectMembershipResolver(Protocol):
    """Application-owned lookup for canonical project membership evidence."""

    async def resolve_project_membership(
        self,
        *,
        project_ref: str,
        subject: str,
    ) -> ProjectMembershipEvidence | None: ...


@dataclass(frozen=True)
class ProjectAuthorizationRequest:
    """Trusted lifecycle coordinates presented to the project policy host.

    The actor comes from the authenticated platform session. ``project_ref``
    and ``target_subject`` identify the person or invitation record being
    managed; they confer no authority by themselves. Creator bootstrap is a
    policy decision made by the port for ``PROJECT_PERSON_CONTROL_CREATE``,
    never a request flag.
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
    ) -> ProjectAuthorizationRequest:
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
    ) -> ProjectAuthorizationDecision:
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
    ) -> ProjectAuthorizationDecision:
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
            raise ProjectAuthorizationError(
                "project_authorization_project_ref_mismatch"
            )
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
                raise ProjectAuthorizationError(
                    "project_authorization_allow_reason_invalid"
                )
        elif not clean_text(self.reason):
            raise ProjectAuthorizationError(
                "project_authorization_denial_reason_missing"
            )
        elif self.delegable_grants or self.platform_admin:
            raise ProjectAuthorizationError(
                "project_authorization_denial_authority_invalid"
            )


class ProjectAuthorizationPort(Protocol):
    """Project-owned policy evaluator injected by the application host."""

    async def authorize_project_person_control(
        self,
        request: ProjectAuthorizationRequest,
    ) -> ProjectAuthorizationDecision: ...


class ResolverBackedProjectAuthorizationPort:
    """Authorize project-held Control Card changes from membership evidence."""

    def __init__(
        self,
        *,
        resolver: ProjectMembershipResolver | None,
        administrative_roles: Iterable[Any] | str,
    ) -> None:
        self._resolver = resolver
        self._administrative_roles = _roles(administrative_roles)

    async def _resolve(
        self,
        *,
        request: ProjectAuthorizationRequest,
        subject: str,
    ) -> ProjectMembershipEvidence | None:
        assert self._resolver is not None
        membership = await self._resolver.resolve_project_membership(
            project_ref=request.project_ref,
            subject=subject,
        )
        if membership is None:
            return None
        if not isinstance(membership, ProjectMembershipEvidence):
            raise ProjectAuthorizationError("project_membership_evidence_invalid")
        membership.validate_for(
            project_ref=request.project_ref,
            subject=subject,
        )
        return membership

    @staticmethod
    def _deny(
        request: ProjectAuthorizationRequest,
        reason: str,
        *,
        evidence: Mapping[str, Any] | None = None,
    ) -> ProjectAuthorizationDecision:
        return ProjectAuthorizationDecision.deny(
            request,
            reason=reason,
            evidence=evidence,
        )

    async def authorize_project_person_control(
        self,
        request: ProjectAuthorizationRequest,
    ) -> ProjectAuthorizationDecision:
        if self._resolver is None:
            return self._deny(request, "project_membership_resolver_missing")
        if not self._administrative_roles:
            return self._deny(request, "project_administrative_roles_missing")

        try:
            actor = await self._resolve(
                request=request,
                subject=request.actor_subject,
            )
        except ProjectAuthorizationError as exc:
            return self._deny(request, exc.reason)
        if actor is None:
            return self._deny(request, "project_actor_membership_missing")
        if actor.role.lower() not in self._administrative_roles:
            # The operator's rule (W260, 2026-09-26): a project admin (an
            # administrative role) does anything to any person's Control Card,
            # their own included; anyone else reads their own and nothing more.
            # A person's Control Card is decided by an admin, in the board's
            # Team > People.
            own = request.target_subject == request.actor_subject
            if own and request.operation == PROJECT_PERSON_CONTROL_READ:
                return ProjectAuthorizationDecision.allow(
                    request,
                    delegable_grants=actor.delegable_grants,
                    evidence={
                        "authorization_source": "project_membership_resolver",
                        "actor_membership": actor.to_public_dict(),
                        "target_membership": actor.to_public_dict(),
                        "own_card": True,
                    },
                )
            return self._deny(
                request,
                (
                    "project_person_control_decided_by_admin"
                    if own and request.operation in {
                        PROJECT_PERSON_CONTROL_UPDATE,
                        PROJECT_PERSON_CONTROL_REVOKE,
                    }
                    else "project_actor_role_not_administrative"
                ),
                evidence={"actor_membership": actor.to_public_dict()},
            )

        target: ProjectMembershipEvidence | None = None
        target_membership_required = request.operation not in {
            PROJECT_PERSON_CONTROL_REVOKE,
            *PROJECT_INVITATION_CONTROL_OPERATIONS,
        }
        if target_membership_required:
            try:
                target = (
                    actor
                    if request.target_subject == request.actor_subject
                    else await self._resolve(
                        request=request,
                        subject=request.target_subject,
                    )
                )
            except ProjectAuthorizationError as exc:
                return self._deny(request, exc.reason)
            if target is None:
                return self._deny(request, "project_target_membership_missing")

        evidence: dict[str, Any] = {
            "authorization_source": "project_membership_resolver",
            "actor_membership": actor.to_public_dict(),
        }
        if target is not None:
            evidence["target_membership"] = target.to_public_dict()
        return ProjectAuthorizationDecision.allow(
            request,
            delegable_grants=actor.delegable_grants,
            evidence=evidence,
        )


__all__ = [
    "PROJECT_INVITATION_CONTROL_CREATE",
    "PROJECT_INVITATION_CONTROL_OPERATIONS",
    "PROJECT_INVITATION_CONTROL_READ",
    "PROJECT_INVITATION_CONTROL_REVOKE",
    "PROJECT_INVITATION_CONTROL_UPDATE",
    "PROJECT_PERSON_CONTROL_CREATE",
    "PROJECT_PERSON_CONTROL_OPERATIONS",
    "PROJECT_PERSON_CONTROL_READ",
    "PROJECT_PERSON_CONTROL_REVOKE",
    "PROJECT_PERSON_CONTROL_UPDATE",
    "PROJECT_PERSON_MY_CARD_SEED",
    "ProjectAuthorizationDecision",
    "ProjectAuthorizationError",
    "ProjectAuthorizationPort",
    "ProjectAuthorizationRequest",
    "ProjectMembershipEvidence",
    "ProjectMembershipConfig",
    "ProjectMembershipResolver",
    "ResolverBackedProjectAuthorizationPort",
]

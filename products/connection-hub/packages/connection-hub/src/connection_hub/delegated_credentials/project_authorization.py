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
# W502: bind an existing person's project Control under the project's Control P.
PROJECT_PERSON_CONTROL_BIND_PROJECT = "project.person_control.bind_project"
# W578: create the project's own application Control P (a brand-new project's genesis plan).
PROJECT_CONTROL_CREATE = "project.control.create"
PROJECT_INVITATION_CONTROL_CREATE = "project.invitation_control.create"
PROJECT_INVITATION_CONTROL_READ = "project.invitation_control.read"
PROJECT_INVITATION_CONTROL_UPDATE = "project.invitation_control.update"
PROJECT_INVITATION_CONTROL_REVOKE = "project.invitation_control.revoke"
# W587 follow-up C (operator, 2026-10-06 15:58): the project host gave no
# answer, because its call failed or its application was still starting (a
# board reload). The resolver names these; they are not refusals, so a Card's
# viewer is never told "a project admin decides this" for them, and a save
# fails closed as retryable instead of forbidden.
PROJECT_MEMBERSHIP_PROVIDER_UNAVAILABLE = "project_membership_provider_unavailable"
PROJECT_MEMBERSHIP_PROVIDER_NOT_READY = "project_membership_provider_not_ready"
PROJECT_BOARD_RESTARTING = "project_board_restarting"


def unanswered_policy_refusal(reason: str, *, error: str) -> dict[str, Any] | None:
    """A retryable 503 for a decision the project host never gave, else None."""

    if reason == PROJECT_MEMBERSHIP_PROVIDER_NOT_READY:
        error = PROJECT_BOARD_RESTARTING
    elif reason != PROJECT_MEMBERSHIP_PROVIDER_UNAVAILABLE:
        return None
    return {"ok": False, "error": error, "reason": reason, "retryable": True, "status": 503}


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
        PROJECT_PERSON_CONTROL_BIND_PROJECT,
        PROJECT_CONTROL_CREATE,
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


def _is_digest(value: Any) -> bool:
    """A plan request digest: exactly 64 lowercase hex characters."""

    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


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
class ProjectControlLocator:
    """W502: the project host's exact record of this project's Control Card P.

    ``control_id`` and ``holder_subject`` come from the host's own stored link
    (Problem Board's control_card_link), never from a request. The Hub binds a
    person's project Control under exactly this Card, or binds nothing.
    """

    control_id: str
    holder_subject: str

    @classmethod
    def from_mapping(cls, raw: Any) -> "ProjectControlLocator | None":
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise ProjectAuthorizationError("project_control_locator_invalid")
        control_id, holder = clean_text(raw.get("control_id")), clean_text(raw.get("holder_subject"))
        if not control_id or not holder or set(raw) - {"control_id", "holder_subject"}:
            raise ProjectAuthorizationError("project_control_locator_invalid")
        return cls(control_id=control_id, holder_subject=holder)

    def to_dict(self) -> dict[str, str]:
        return {"control_id": self.control_id, "holder_subject": self.holder_subject}


@dataclass(frozen=True)
class ProjectMembershipEvidence:
    """One host-owned project membership answer, with no implied authority."""

    project_ref: str
    subject: str
    role: str
    delegable_grants: tuple[str, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)
    # W502: the project's Control Card P, a project-level fact the host
    # answers with the membership; None when the project has none yet.
    project_control: ProjectControlLocator | None = None

    @classmethod
    def build(
        cls,
        *,
        project_ref: Any,
        subject: Any,
        role: Any,
        delegable_grants: Any = (),
        evidence: Mapping[str, Any] | None = None,
        project_control: Any = None,
    ) -> ProjectMembershipEvidence:
        if evidence is not None and not isinstance(evidence, Mapping):
            raise ProjectAuthorizationError("project_membership_evidence_invalid")
        return cls(
            project_control=ProjectControlLocator.from_mapping(project_control),
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

    ``request_digest`` is empty for an ordinary single operation. For one step
    of a lifecycle plan it is that plan's 64-hex request digest, so a decision
    issued for another plan under the same request id cannot answer it (W578
    N1).
    """

    actor_subject: str
    project_ref: str
    target_subject: str
    operation: str
    request_id: str
    request_digest: str = ""

    def __post_init__(self) -> None:
        if self.request_digest != "" and not _is_digest(self.request_digest):
            raise ProjectAuthorizationError(
                "project_authorization_request_digest_invalid"
            )

    @classmethod
    def build(
        cls,
        *,
        actor_subject: Any,
        project_ref: Any,
        target_subject: Any,
        operation: Any,
        request_id: Any,
        request_digest: Any = "",
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
            request_digest=clean_text(request_digest),
        )


_DECISION_REQUIRED = frozenset({"allowed", "actor_subject", "project_ref", "target_subject", "operation",
                                "request_id"})
_DECISION_FIELDS = _DECISION_REQUIRED | {"request_digest", "reason", "delegable_grants", "platform_admin",
                                         "evidence", "project_control"}


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
    # W502: the host's exact project Control Card P for this project, if any.
    project_control: ProjectControlLocator | None = None
    # W578 N1: copied from the request it answers; "" for an ordinary request.
    request_digest: str = ""

    @classmethod
    def allow(
        cls,
        request: ProjectAuthorizationRequest,
        *,
        delegable_grants: Any = (),
        platform_admin: bool = False,
        evidence: Mapping[str, Any] | None = None,
        project_control: ProjectControlLocator | None = None,
    ) -> ProjectAuthorizationDecision:
        return cls(
            project_control=project_control,
            allowed=True,
            actor_subject=request.actor_subject,
            project_ref=request.project_ref,
            target_subject=request.target_subject,
            operation=request.operation,
            request_id=request.request_id,
            request_digest=request.request_digest,
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
            request_digest=request.request_digest,
            reason=_required(reason, "project_authorization_denial_reason_missing"),
            evidence=copy.deepcopy(dict(evidence or {})),
        )

    def to_dict(self) -> dict[str, Any]:
        """The decision's wire mapping (W578: what a project host's plan answer carries per step)."""

        out: dict[str, Any] = {
            "allowed": self.allowed,
            "actor_subject": self.actor_subject,
            "project_ref": self.project_ref,
            "target_subject": self.target_subject,
            "operation": self.operation,
            "request_id": self.request_id,
            "request_digest": self.request_digest,
            "reason": self.reason,
            "delegable_grants": list(self.delegable_grants),
            "platform_admin": self.platform_admin,
            "evidence": copy.deepcopy(dict(self.evidence)),
        }
        if self.project_control is not None:
            out["project_control"] = self.project_control.to_dict()
        return out

    @classmethod
    def from_mapping(cls, raw: Any) -> ProjectAuthorizationDecision:
        """A decision from its wire mapping, refusing unknown, missing or mistyped fields.

        It is not validated against any request here: the caller binds it to
        its own request with ``validate_for`` (a plan envelope does so per step).
        """

        if not isinstance(raw, Mapping) or set(raw) - _DECISION_FIELDS or not _DECISION_REQUIRED <= set(raw):
            raise ProjectAuthorizationError("project_authorization_decision_invalid")
        if type(raw["allowed"]) is not bool or type(raw.get("platform_admin", False)) is not bool:
            raise ProjectAuthorizationError("project_authorization_decision_invalid")
        texts = ("actor_subject", "project_ref", "target_subject", "operation", "request_id",
                 "request_digest", "reason")
        if any(type(raw.get(name, "")) is not str for name in texts):
            raise ProjectAuthorizationError("project_authorization_decision_invalid")
        grants = raw.get("delegable_grants", [])
        evidence = raw.get("evidence", {})
        if not isinstance(grants, list) or not isinstance(evidence, Mapping):
            raise ProjectAuthorizationError("project_authorization_decision_invalid")
        return cls(
            allowed=raw["allowed"],
            actor_subject=raw["actor_subject"],
            project_ref=raw["project_ref"],
            target_subject=raw["target_subject"],
            operation=raw["operation"],
            request_id=raw["request_id"],
            request_digest=raw.get("request_digest", ""),
            reason=raw.get("reason", ""),
            delegable_grants=_grants(grants),
            platform_admin=raw.get("platform_admin", False),
            evidence=copy.deepcopy(dict(evidence)),
            project_control=ProjectControlLocator.from_mapping(raw.get("project_control")),
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
        if clean_text(self.request_digest) != request.request_digest:
            raise ProjectAuthorizationError(
                "project_authorization_request_digest_mismatch"
            )
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


LIFECYCLE_PLAN_AUTHORIZATION_SCHEMA = "connection-hub.lifecycle-plan-authorization.v1"
MAX_LIFECYCLE_PLAN_STEPS = 8


@dataclass(frozen=True)
class LifecyclePlanStep:
    """One step of a lifecycle plan: its local ref, the exact operation and the person it is for."""

    ref: str
    operation: str
    target_subject: str


@dataclass(frozen=True)
class LifecyclePlanAuthorizationRequest:
    """W578: what the project host is asked to authorize for ONE complete plan request.

    Every step belongs to the same authenticated actor, project scope,
    request id and the digest of the complete plan request (CodeApp 23:48).
    The host evaluates each step's exact operation and target; nothing here
    grants anything by itself.
    """

    actor_subject: str
    project_ref: str
    request_id: str
    request_digest: str
    steps: tuple[LifecyclePlanStep, ...]

    def __post_init__(self) -> None:
        for name in ("actor_subject", "project_ref", "request_id"):
            _required(getattr(self, name), f"lifecycle_plan_{name}_missing")
        if not _is_digest(self.request_digest):
            raise ProjectAuthorizationError("lifecycle_plan_request_digest_invalid")
        refs = [step.ref for step in self.steps]
        if (not 1 <= len(self.steps) <= MAX_LIFECYCLE_PLAN_STEPS or len(set(refs)) != len(refs)
                or any(not clean_text(step.ref) or not clean_text(step.target_subject) for step in self.steps)):
            raise ProjectAuthorizationError("lifecycle_plan_steps_invalid")
        for step in self.steps:
            _operation(step.operation)

    def step_request(self, step: LifecyclePlanStep) -> "ProjectAuthorizationRequest":
        """The exact single-step request this step's decision must answer."""
        return ProjectAuthorizationRequest(actor_subject=self.actor_subject, project_ref=self.project_ref,
                                           target_subject=step.target_subject, operation=step.operation,
                                           request_id=self.request_id, request_digest=self.request_digest)


@dataclass(frozen=True)
class LifecyclePlanAuthorization:
    """W578: the host's answer for a complete plan: one exact decision per step, nothing more.

    Not a union of grants and never a broadened singular decision: each step
    keeps its own typed decision (its delegable grants, platform flag and P
    locator), and the planner constructs each candidate under that step's
    bounds only. Output never authorizes itself.
    """

    request: LifecyclePlanAuthorizationRequest
    decisions: tuple[tuple[str, "ProjectAuthorizationDecision"], ...]
    schema: str = LIFECYCLE_PLAN_AUTHORIZATION_SCHEMA

    def __post_init__(self) -> None:
        # Fail closed by construction (EMain F1 on #632): an empty, partial or
        # mismatched envelope never exists, so allowed/decision_for can only be
        # read on one that covers every step of its own request exactly.
        if self.schema != LIFECYCLE_PLAN_AUTHORIZATION_SCHEMA or not isinstance(
                self.request, LifecyclePlanAuthorizationRequest):
            raise ProjectAuthorizationError("lifecycle_plan_authorization_request_mismatch")
        if not isinstance(self.decisions, tuple) or any(
                not isinstance(item, tuple) or len(item) != 2 for item in self.decisions):
            raise ProjectAuthorizationError("lifecycle_plan_authorization_steps_mismatch")
        refs = [ref for ref, _ in self.decisions]
        if not refs or sorted(refs) != sorted(step.ref for step in self.request.steps) or len(set(refs)) != len(refs):
            raise ProjectAuthorizationError("lifecycle_plan_authorization_steps_mismatch")
        by_ref = dict(self.decisions)
        for step in self.request.steps:
            decision = by_ref[step.ref]
            if not isinstance(decision, ProjectAuthorizationDecision):
                raise ProjectAuthorizationError("lifecycle_plan_authorization_decision_invalid")
            decision.validate_for(self.request.step_request(step))

    def validate_for(self, request: LifecyclePlanAuthorizationRequest) -> None:
        """Refuse an envelope for another request (its own steps were checked at construction)."""
        if self.request != request:
            raise ProjectAuthorizationError("lifecycle_plan_authorization_request_mismatch")

    @property
    def allowed(self) -> bool:
        return all(decision.allowed for _, decision in self.decisions)

    def refusal(self) -> str:
        """The first denied step's reason ("" when every step is allowed)."""
        for step in self.request.steps:
            decision = dict(self.decisions)[step.ref]
            if not decision.allowed:
                return clean_text(decision.reason) or "project_authorization_denied"
        return ""

    def decision_for(self, ref: str) -> "ProjectAuthorizationDecision":
        """The exact decision for one step; it must be an allow."""
        decision = dict(self.decisions).get(ref)
        if decision is None:
            raise ProjectAuthorizationError("lifecycle_plan_authorization_step_unknown")
        if not decision.allowed:
            raise ProjectAuthorizationError(clean_text(decision.reason) or "project_authorization_denied")
        return decision


class LifecyclePlanAuthorizationPort(Protocol):
    """The project host's whole-plan policy adapter (PB owns its policy)."""

    async def authorize_lifecycle_plan(
        self, request: LifecyclePlanAuthorizationRequest,
    ) -> LifecyclePlanAuthorization: ...


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
            # A person's Control Card is decided by a project admin, in
            # Connection Hub (the only Card editor).
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
            project_control=actor.project_control,
        )



@dataclass(frozen=True)
class ViewerAuthority:
    """What the signed-in person's own account may delegate, by role (W379).

    A project host answers who may act on a project's Card and what the
    project lets them delegate; it knows no platform roles. The catalog rows
    whose grants are platform roles (the "All platform and application APIs"
    row, resource ``*``) are offered by the signed-in person's own role, as on
    their own Cards, so every project route adds this to the host's answer.
    """

    grants: tuple[str, ...] = ()
    platform_admin: bool = False


def with_viewer_authority(
    decision: "ProjectAuthorizationDecision",
    viewer: ViewerAuthority | None,
) -> "ProjectAuthorizationDecision":
    """The host's decision, bounded also by what the signed-in person's role may delegate."""

    if viewer is None or not decision.allowed:
        return decision
    from dataclasses import replace

    return replace(
        decision,
        delegable_grants=tuple(
            sorted(set(decision.delegable_grants) | {str(g) for g in viewer.grants if str(g)})
        ),
        platform_admin=bool(decision.platform_admin or viewer.platform_admin),
    )

__all__ = [
    "LIFECYCLE_PLAN_AUTHORIZATION_SCHEMA",
    "LifecyclePlanAuthorization",
    "LifecyclePlanAuthorizationPort",
    "LifecyclePlanAuthorizationRequest",
    "LifecyclePlanStep",
    "MAX_LIFECYCLE_PLAN_STEPS",
    "PROJECT_CONTROL_CREATE",
    "ProjectControlLocator",
    "PROJECT_BOARD_RESTARTING",
    "PROJECT_MEMBERSHIP_PROVIDER_NOT_READY",
    "PROJECT_MEMBERSHIP_PROVIDER_UNAVAILABLE",
    "unanswered_policy_refusal",
    "ViewerAuthority",
    "with_viewer_authority",
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

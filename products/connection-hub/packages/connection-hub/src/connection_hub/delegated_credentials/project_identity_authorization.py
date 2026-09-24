# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Authorize a signed-in person's operation in one project.

The edge is identity evidence, not authority by itself. The host resolves its
Card references from authoritative storage and supplies the active catalog.
The evaluator requires catalog AND the project's per-person Control Card AND
the person's My Card. This cross-owner composition does not relax the
same-grantor invariant of ordinary caller-to-Control-Card composition.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from connection_hub.delegated_credentials.cards.identity import CARD_KIND_CONTROL
from connection_hub.delegated_credentials.cards.model import (
    CARD_STATE_ACTIVE,
    CARD_STATE_REVOKED,
    CONTROL_COMPOSITION_AND,
    CardAuthority,
    authority_is_credentialless,
)
from connection_hub.delegated_credentials.catalog.authorization import (
    CAPABILITY_OUTER_OPERATION,
    CAPABILITY_RESOURCE,
    CAPABILITY_RESOURCE_CLAIM,
    ActiveCatalogCapabilities,
    CapabilityRequest,
    card_permits_capability,
)
from connection_hub.delegated_credentials.controls.project_person import (
    PROJECT_PERSON_CONTROL_ISSUER_KIND,
    ProjectPersonControlError,
    ProjectPersonControlIdentity,
)
from connection_hub.delegated_credentials.controls.snapshot import (
    control_snapshot_is_exact,
)

PROJECT_IDENTITY_EDGE_SCHEMA = "connection_hub.project_identity_delegation_edge.v1"
PROJECT_OPERATION_AUTHORIZATION_SCHEMA = (
    "connection_hub.project_operation_authorization.v1"
)

CARD_RESOLUTION_CURRENT = "current"
CARD_RESOLUTION_MISSING = "missing"
CARD_RESOLUTION_UPDATING = "updating"
CARD_RESOLUTION_UNAVAILABLE = "unavailable"
CARD_RESOLUTION_STATES = frozenset(
    {
        CARD_RESOLUTION_CURRENT,
        CARD_RESOLUTION_MISSING,
        CARD_RESOLUTION_UPDATING,
        CARD_RESOLUTION_UNAVAILABLE,
    }
)

BOUNDARY_EDGE = "project_identity_edge"
BOUNDARY_CATALOG = "active_catalog"
BOUNDARY_CONTROL_CARD = "control_card"
BOUNDARY_MY_CARD = "my_card"
ALLOW_REASON = "project_operation_allowed"


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _values(value: Iterable[Any] | str | None) -> tuple[str, ...]:
    source: Iterable[Any]
    if isinstance(value, str):
        source = value.replace(",", " ").split()
    elif isinstance(value, (list, tuple, set, frozenset)):
        source = value
    else:
        source = ()
    return tuple(dict.fromkeys(item for raw in source if (item := _clean(raw))))


def _card_prefix(role: str) -> str:
    return "control_card" if role == BOUNDARY_CONTROL_CARD else "my_card"


@dataclass(frozen=True)
class ProjectIdentityCardReference:
    """Stable coordinates for one Card participating in the edge."""

    access_id: str
    grantor_subject: str
    card_revision: int
    issuer_ref: str = ""
    issuer_kind: str = ""

    def validation_reason(self, role: str) -> str:
        prefix = _card_prefix(role)
        if not _clean(self.access_id):
            return f"{prefix}_reference_missing"
        if not _clean(self.grantor_subject):
            return f"{prefix}_owner_missing"
        if _integer(self.card_revision) <= 0:
            return f"{prefix}_revision_missing"
        return ""

    def to_dict(self) -> dict[str, Any]:
        values = {
            "access_id": _clean(self.access_id),
            "grantor_subject": _clean(self.grantor_subject),
            "card_revision": _integer(self.card_revision),
            "issuer_ref": _clean(self.issuer_ref),
            "issuer_kind": _clean(self.issuer_kind),
        }
        return {key: value for key, value in values.items() if value not in ("", 0)}


@dataclass(frozen=True)
class ProjectIdentityDelegationEdge:
    """Inspectable person-session to project-identity relationship."""

    person_subject: str
    project_ref: str
    project_subject: str
    control_card: ProjectIdentityCardReference
    my_card: ProjectIdentityCardReference
    edge_ref: str = ""

    def validation_reason(self) -> str:
        if not _clean(self.person_subject):
            return "project_session_identity_missing"
        if not _clean(self.project_ref):
            return "project_identity_missing"
        if not _clean(self.project_subject):
            return "project_identity_subject_missing"
        if not isinstance(self.control_card, ProjectIdentityCardReference):
            return "control_card_reference_missing"
        if not isinstance(self.my_card, ProjectIdentityCardReference):
            return "my_card_reference_missing"
        for role, reference in (
            (BOUNDARY_CONTROL_CARD, self.control_card),
            (BOUNDARY_MY_CARD, self.my_card),
        ):
            if reason := reference.validation_reason(role):
                return reason
        if self.control_card.access_id == self.my_card.access_id:
            return "project_identity_cards_not_distinct"
        expected = ProjectPersonControlIdentity.build(
            project_ref=self.project_ref,
            target_subject=self.person_subject,
        )
        if self.project_subject != expected.project_subject:
            return "control_card_project_subject_mismatch"
        if self.control_card.access_id != expected.control_id:
            return "control_card_reference_mismatch"
        if self.control_card.grantor_subject != expected.project_subject:
            return "control_card_owner_mismatch"
        if self.control_card.issuer_ref != expected.project_ref:
            return "control_card_project_mismatch"
        if self.control_card.issuer_kind != PROJECT_PERSON_CONTROL_ISSUER_KIND:
            return "control_card_issuer_mismatch"
        if self.my_card.grantor_subject != self.person_subject:
            return "my_card_owner_mismatch"
        return ""

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema": PROJECT_IDENTITY_EDGE_SCHEMA,
            "person_identity": {"subject": _clean(self.person_subject)},
            "project_identity": {
                "ref": _clean(self.project_ref),
                "subject": _clean(self.project_subject),
            },
            "cards": {
                "control_card": self.control_card.to_dict(),
                "my_card": self.my_card.to_dict(),
            },
        }
        if edge_ref := _clean(self.edge_ref):
            result["edge_ref"] = edge_ref
        return result


@dataclass(frozen=True)
class ProjectOperationRequest:
    """One exact project operation requested by an authenticated session."""

    person_subject: str
    project_ref: str
    resource: str
    operation: str
    required_grants: tuple[str, ...] = ()
    request_resource: str = ""
    surface: str = "application"

    def __post_init__(self) -> None:
        object.__setattr__(self, "required_grants", _values(self.required_grants))

    def validation_reason(self) -> str:
        return next(
            (
                reason
                for condition, reason in (
                    (
                        not _clean(self.person_subject),
                        "project_session_identity_missing",
                    ),
                    (not _clean(self.project_ref), "project_identity_missing"),
                    (not _clean(self.resource), "project_operation_resource_missing"),
                    (not _clean(self.operation), "project_operation_missing"),
                )
                if condition
            ),
            "",
        )

    def capability_requests(self) -> tuple[CapabilityRequest, ...]:
        shared = {
            "resource": _clean(self.resource),
            "request_resource": _clean(self.request_resource or self.resource),
            "surface": _clean(self.surface),
        }
        return (
            CapabilityRequest(kind=CAPABILITY_RESOURCE, **shared),
            CapabilityRequest(
                kind=CAPABILITY_OUTER_OPERATION,
                outer_operation=_clean(self.operation),
                **shared,
            ),
            *(
                CapabilityRequest(
                    kind=CAPABILITY_RESOURCE_CLAIM,
                    claim=grant,
                    **shared,
                )
                for grant in self.required_grants
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        values = {
            "person_subject": _clean(self.person_subject),
            "project_ref": _clean(self.project_ref),
            "resource": _clean(self.resource),
            "request_resource": _clean(self.request_resource),
            "surface": _clean(self.surface),
            "operation": _clean(self.operation),
            "required_grants": list(self.required_grants),
        }
        return {key: value for key, value in values.items() if value not in ("", [])}


@dataclass(frozen=True)
class ProjectCardResolution:
    """Current resolution state for one edge Card reference."""

    state: str = CARD_RESOLUTION_CURRENT
    authority: CardAuthority | None = None
    reason: str = ""

    @classmethod
    def current(cls, authority: CardAuthority) -> ProjectCardResolution:
        return cls(authority=authority)

    @classmethod
    def missing(cls) -> ProjectCardResolution:
        return cls(state=CARD_RESOLUTION_MISSING)

    @classmethod
    def updating(cls, reason: str = "") -> ProjectCardResolution:
        return cls(state=CARD_RESOLUTION_UPDATING, reason=_clean(reason))

    @classmethod
    def unavailable(cls, reason: str = "") -> ProjectCardResolution:
        return cls(state=CARD_RESOLUTION_UNAVAILABLE, reason=_clean(reason))

    def to_public_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"state": _clean(self.state)}
        if self.authority is not None:
            result.update(
                access_id=self.authority.access_id,
                card_revision=self.authority.card_revision,
                card_state=self.authority.state,
            )
        if reason := _clean(self.reason):
            result["resolution_reason"] = reason
        return result


@dataclass(frozen=True)
class ProjectOperationAuthorizationDecision:
    """Secret-free allow or named default-closed denial."""

    allowed: bool
    reason: str
    request: ProjectOperationRequest
    edge: ProjectIdentityDelegationEdge | None = None
    blocking_boundary: str = ""
    blocking_capability: CapabilityRequest | None = None
    retryable: bool = False
    active_catalog_version: str = ""
    control_card: ProjectCardResolution | None = None
    my_card: ProjectCardResolution | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema": PROJECT_OPERATION_AUTHORIZATION_SCHEMA,
            "allowed": self.allowed,
            "reason": _clean(self.reason),
            "retryable": self.retryable,
            "request": self.request.to_dict(),
        }
        optional = {
            "blocking_boundary": _clean(self.blocking_boundary),
            "active_catalog_version": _clean(self.active_catalog_version),
        }
        result.update({key: value for key, value in optional.items() if value})
        if self.edge is not None:
            result["delegation_edge"] = self.edge.to_dict()
        if self.blocking_capability is not None:
            result["blocking_capability"] = self.blocking_capability.path()
        resolved = {
            key: value.to_public_dict()
            for key, value in (
                ("control_card", self.control_card),
                ("my_card", self.my_card),
            )
            if value is not None
        }
        if resolved:
            result["resolved_cards"] = resolved
        if self.details:
            result["details"] = dict(self.details)
        return result


@dataclass(frozen=True)
class _Issue:
    reason: str
    boundary: str
    capability: CapabilityRequest | None = None
    retryable: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)


def _card_issue(
    role: str,
    reference: ProjectIdentityCardReference,
    resolution: ProjectCardResolution | None,
    edge: ProjectIdentityDelegationEdge,
    *,
    now: int,
) -> _Issue | None:
    prefix = _card_prefix(role)
    resolved = resolution or ProjectCardResolution.missing()
    state = _clean(resolved.state) or CARD_RESOLUTION_MISSING
    if state not in CARD_RESOLUTION_STATES:
        return _Issue(
            f"{prefix}_resolution_invalid",
            role,
            details={"resolution_state": state},
        )
    if state != CARD_RESOLUTION_CURRENT:
        details = (
            {"resolution_reason": _clean(resolved.reason)} if resolved.reason else {}
        )
        return _Issue(
            f"{prefix}_{state}",
            role,
            retryable=state in {CARD_RESOLUTION_UPDATING, CARD_RESOLUTION_UNAVAILABLE},
            details=details,
        )

    card = resolved.authority
    if card is None:
        return _Issue(f"{prefix}_missing", role)
    if card.state == CARD_STATE_REVOKED:
        return _Issue(f"{prefix}_revoked", role)
    if card.state != CARD_STATE_ACTIVE:
        return _Issue(f"{prefix}_not_active", role)

    mismatches = (
        (card.access_id != reference.access_id, f"{prefix}_reference_mismatch"),
        (
            card.grantor_subject != reference.grantor_subject,
            f"{prefix}_owner_mismatch",
        ),
        (
            bool(reference.issuer_ref) and card.issuer_ref != reference.issuer_ref,
            f"{prefix}_issuer_mismatch",
        ),
        (
            bool(reference.issuer_kind) and card.issuer_kind != reference.issuer_kind,
            f"{prefix}_issuer_mismatch",
        ),
    )
    if reason := next((reason for condition, reason in mismatches if condition), ""):
        return _Issue(reason, role)
    if _integer(card.card_revision) != _integer(reference.card_revision):
        return _Issue(
            f"{prefix}_revision_mismatch",
            role,
            details={"resolved_card_revision": _integer(card.card_revision)},
        )
    if not authority_is_credentialless(card) and _integer(card.expires_at) <= now:
        return _Issue(f"{prefix}_expired", role)

    if role == BOUNDARY_CONTROL_CARD:
        if card.card_kind != CARD_KIND_CONTROL or not authority_is_credentialless(card):
            return _Issue("control_card_invalid", role)
        if card.composition_mode != CONTROL_COMPOSITION_AND:
            return _Issue("control_card_requires_and", role)
        if not control_snapshot_is_exact(card):
            return _Issue("control_card_exact_snapshot_required", role)
        try:
            identity = ProjectPersonControlIdentity.from_authority(card)
        except ProjectPersonControlError as exc:
            return _Issue(
                "control_card_identity_invalid",
                role,
                details={"identity_reason": exc.reason},
            )
        if identity.project_ref != edge.project_ref:
            return _Issue("control_card_project_mismatch", role)
        if identity.target_subject != edge.person_subject:
            return _Issue("control_card_target_mismatch", role)
        if identity.project_subject != edge.project_subject:
            return _Issue("control_card_project_subject_mismatch", role)
    else:
        binding = card.control_card
        if binding is None:
            return _Issue("my_card_control_binding_missing", role)
        if (
            binding.control_id != edge.control_card.access_id
            or binding.issuer_ref != edge.control_card.issuer_ref
            or (
                bool(edge.control_card.issuer_kind)
                and binding.issuer_kind != edge.control_card.issuer_kind
            )
        ):
            return _Issue("my_card_control_binding_mismatch", role)
    return None


def _capability_reason(boundary: str, capability: CapabilityRequest) -> str:
    suffix = {
        CAPABILITY_RESOURCE: "resource",
        CAPABILITY_RESOURCE_CLAIM: "grant",
    }.get(capability.kind, "operation")
    prefix = {
        BOUNDARY_CATALOG: "active_catalog_excludes_project",
        BOUNDARY_CONTROL_CARD: "control_card_excludes",
        BOUNDARY_MY_CARD: "my_card_excludes",
    }[boundary]
    return f"{prefix}_{suffix}"


def _decision(
    issue: _Issue | None,
    *,
    request: ProjectOperationRequest,
    edge: ProjectIdentityDelegationEdge | None,
    catalog: ActiveCatalogCapabilities | None,
    control_card: ProjectCardResolution | None,
    my_card: ProjectCardResolution | None,
) -> ProjectOperationAuthorizationDecision:
    return ProjectOperationAuthorizationDecision(
        allowed=issue is None,
        reason=issue.reason if issue is not None else ALLOW_REASON,
        request=request,
        edge=edge,
        blocking_boundary=issue.boundary if issue is not None else "",
        blocking_capability=issue.capability if issue is not None else None,
        retryable=issue.retryable if issue is not None else False,
        active_catalog_version=catalog.version if catalog is not None else "",
        control_card=control_card,
        my_card=my_card,
        details=issue.details if issue is not None else {},
    )


def authorize_project_operation(
    *,
    request: ProjectOperationRequest,
    edge: ProjectIdentityDelegationEdge | None,
    control_card: ProjectCardResolution | None,
    my_card: ProjectCardResolution | None,
    catalog: ActiveCatalogCapabilities | None,
    now: int | None = None,
) -> ProjectOperationAuthorizationDecision:
    """Evaluate one operation against current authoritative inputs."""

    issue: _Issue | None = None
    if reason := request.validation_reason():
        issue = _Issue(reason, BOUNDARY_EDGE)
    elif edge is None:
        issue = _Issue("project_identity_edge_missing", BOUNDARY_EDGE)
    elif reason := edge.validation_reason():
        issue = _Issue(reason, BOUNDARY_EDGE)
    elif request.person_subject != edge.person_subject:
        issue = _Issue("project_session_identity_mismatch", BOUNDARY_EDGE)
    elif request.project_ref != edge.project_ref:
        issue = _Issue("project_identity_mismatch", BOUNDARY_EDGE)
    if issue is not None or edge is None:
        return _decision(
            issue,
            request=request,
            edge=edge,
            catalog=catalog,
            control_card=control_card,
            my_card=my_card,
        )

    capabilities = request.capability_requests()
    if catalog is None:
        issue = _Issue("active_catalog_unavailable", BOUNDARY_CATALOG, retryable=True)
    else:
        issue = next(
            (
                _Issue(
                    _capability_reason(BOUNDARY_CATALOG, capability),
                    BOUNDARY_CATALOG,
                    capability=capability,
                )
                for capability in capabilities
                if not catalog.permits(capability)
            ),
            None,
        )
    if issue is not None:
        return _decision(
            issue,
            request=request,
            edge=edge,
            catalog=catalog,
            control_card=control_card,
            my_card=my_card,
        )

    moment = int(time.time()) if now is None else int(now)
    for role, reference, resolution in (
        (BOUNDARY_CONTROL_CARD, edge.control_card, control_card),
        (BOUNDARY_MY_CARD, edge.my_card, my_card),
    ):
        if issue := _card_issue(role, reference, resolution, edge, now=moment):
            return _decision(
                issue,
                request=request,
                edge=edge,
                catalog=catalog,
                control_card=control_card,
                my_card=my_card,
            )

    authorities = (
        (BOUNDARY_CONTROL_CARD, control_card.authority),
        (BOUNDARY_MY_CARD, my_card.authority),
    )
    issue = next(
        (
            _Issue(
                _capability_reason(boundary, capability),
                boundary,
                capability=capability,
            )
            for boundary, authority in authorities
            for capability in capabilities
            if authority is None or not card_permits_capability(authority, capability)
        ),
        None,
    )
    return _decision(
        issue,
        request=request,
        edge=edge,
        catalog=catalog,
        control_card=control_card,
        my_card=my_card,
    )


__all__ = [
    "ALLOW_REASON",
    "BOUNDARY_CATALOG",
    "BOUNDARY_CONTROL_CARD",
    "BOUNDARY_EDGE",
    "BOUNDARY_MY_CARD",
    "CARD_RESOLUTION_CURRENT",
    "CARD_RESOLUTION_MISSING",
    "CARD_RESOLUTION_UNAVAILABLE",
    "CARD_RESOLUTION_UPDATING",
    "PROJECT_IDENTITY_EDGE_SCHEMA",
    "PROJECT_OPERATION_AUTHORIZATION_SCHEMA",
    "ProjectCardResolution",
    "ProjectIdentityCardReference",
    "ProjectIdentityDelegationEdge",
    "ProjectOperationAuthorizationDecision",
    "ProjectOperationRequest",
    "authorize_project_operation",
]

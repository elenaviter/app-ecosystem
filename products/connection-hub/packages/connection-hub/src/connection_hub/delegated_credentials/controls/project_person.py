# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Project-held Control Card identity and immutable mutation evidence."""

from __future__ import annotations

import copy
import dataclasses
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from connection_hub.delegated_credentials.cards.model import (
    CONTROL_COMPOSITION_AND,
    CardAuthority,
)
from connection_hub.delegated_credentials.named_service_policy import clean_text


PROJECT_PERSON_CONTROL_SCHEMA = "connection_hub.project_person_control.v1"
PROJECT_PERSON_CONTROL_PROPERTY = "connection_hub.project_person_control"
PROJECT_PERSON_CONTROL_AUDIT_SCHEMA = "connection_hub.project_person_control.audit.v1"
PROJECT_PERSON_CONTROL_AUDIT_PROVENANCE = "project_person_control_audit"
PROJECT_PERSON_CONTROL_ISSUER_KIND = "project"


class ProjectPersonControlError(ValueError):
    """A project/person Control Card identity or audit value is invalid."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _required(value: Any, reason: str) -> str:
    result = clean_text(value)
    if not result:
        raise ProjectPersonControlError(reason)
    return result


def project_authority_subject(project_ref: Any) -> str:
    """Stable synthetic grantor subject for one project's Card partition."""

    project = _required(project_ref, "project_person_control_project_ref_missing")
    digest = hashlib.sha256(project.encode("utf-8")).hexdigest()
    return f"project-authority:{digest}"


def project_person_control_id(project_ref: Any, target_subject: Any) -> str:
    """Stable Card id for exactly one target person in one project."""

    project = _required(project_ref, "project_person_control_project_ref_missing")
    target = _required(target_subject, "project_person_control_target_missing")
    digest = hashlib.sha256(f"{project}\0{target}".encode("utf-8")).hexdigest()[:24]
    return f"person-control-{digest}"


@dataclass(frozen=True)
class ProjectPersonControlIdentity:
    """The project owner and target person bound to one Control Card."""

    project_ref: str
    target_subject: str
    project_subject: str
    control_id: str

    @classmethod
    def build(
        cls,
        *,
        project_ref: Any,
        target_subject: Any,
    ) -> "ProjectPersonControlIdentity":
        project = _required(project_ref, "project_person_control_project_ref_missing")
        target = _required(target_subject, "project_person_control_target_missing")
        return cls(
            project_ref=project,
            target_subject=target,
            project_subject=project_authority_subject(project),
            control_id=project_person_control_id(project, target),
        )

    @classmethod
    def from_authority(
        cls,
        authority: CardAuthority,
    ) -> "ProjectPersonControlIdentity":
        raw = dict(authority.properties or {}).get(PROJECT_PERSON_CONTROL_PROPERTY)
        if not isinstance(raw, Mapping):
            raise ProjectPersonControlError("project_person_control_marker_missing")
        if clean_text(raw.get("schema")) != PROJECT_PERSON_CONTROL_SCHEMA:
            raise ProjectPersonControlError("project_person_control_schema_invalid")
        identity = cls.build(
            project_ref=raw.get("project_ref"),
            target_subject=raw.get("target_subject"),
        )
        if clean_text(raw.get("project_subject")) != identity.project_subject:
            raise ProjectPersonControlError("project_person_control_project_subject_mismatch")
        if authority.grantor_subject != identity.project_subject:
            raise ProjectPersonControlError("project_person_control_grantor_mismatch")
        if authority.access_id != identity.control_id:
            raise ProjectPersonControlError("project_person_control_id_mismatch")
        if authority.issuer_kind != PROJECT_PERSON_CONTROL_ISSUER_KIND:
            raise ProjectPersonControlError("project_person_control_issuer_kind_mismatch")
        if authority.issuer_ref != identity.project_ref:
            raise ProjectPersonControlError("project_person_control_issuer_ref_mismatch")
        if authority.composition_mode != CONTROL_COMPOSITION_AND:
            raise ProjectPersonControlError("project_person_control_requires_and")
        return identity

    def to_property(self) -> dict[str, str]:
        return {
            "schema": PROJECT_PERSON_CONTROL_SCHEMA,
            "project_ref": self.project_ref,
            "target_subject": self.target_subject,
            "project_subject": self.project_subject,
        }


def _public_properties(authority: CardAuthority) -> dict[str, Any]:
    properties = copy.deepcopy(dict(authority.properties or {}))
    properties.pop(PROJECT_PERSON_CONTROL_PROPERTY, None)
    return properties


def project_person_control_snapshot(authority: CardAuthority) -> dict[str, Any]:
    """The bounded authorization fields whose change must be audited."""

    return {
        "state": authority.state,
        "label": authority.label,
        "composition_mode": authority.composition_mode,
        "resource_grants": {
            resource: list(grants)
            for resource, grants in sorted(authority.resource_grants.items())
        },
        "resource_operations": {
            resource: list(operations)
            for resource, operations in sorted(authority.resource_operations.items())
        },
        "named_service_operations": authority.named_service_operations.to_stored(),
        "account_scope": {
            provider: {
                account_id: list(claims)
                for account_id, claims in sorted(accounts.items())
            }
            for provider, accounts in sorted(authority.account_scope.items())
        },
        "properties": _public_properties(authority),
    }


def project_person_control_diff(
    before: CardAuthority | None,
    after: CardAuthority,
) -> dict[str, dict[str, Any]]:
    """Exact before/after values for fields changed by one revision."""

    previous = project_person_control_snapshot(before) if before is not None else {}
    current = project_person_control_snapshot(after)
    return {
        field: {
            "before": copy.deepcopy(previous.get(field)),
            "after": copy.deepcopy(current.get(field)),
        }
        for field in sorted(set(previous) | set(current))
        if previous.get(field) != current.get(field)
    }


@dataclass(frozen=True)
class ProjectPersonControlAudit:
    """Who changed one project-held Control Card, when, and what changed."""

    action: str
    actor_subject: str
    project_ref: str
    target_subject: str
    request_id: str
    occurred_at: int
    before_revision: int
    after_revision: int
    changes: Mapping[str, Mapping[str, Any]]

    @classmethod
    def build(
        cls,
        *,
        action: Any,
        actor_subject: Any,
        identity: ProjectPersonControlIdentity,
        request_id: Any,
        occurred_at: int,
        before: CardAuthority | None,
        after: CardAuthority,
    ) -> "ProjectPersonControlAudit":
        event_action = _required(action, "project_person_control_audit_action_missing")
        actor = _required(actor_subject, "project_person_control_audit_actor_missing")
        request = _required(request_id, "project_person_control_audit_request_missing")
        timestamp = int(occurred_at)
        if timestamp < 1:
            raise ProjectPersonControlError("project_person_control_audit_time_invalid")
        changes = project_person_control_diff(before, after)
        if not changes:
            raise ProjectPersonControlError("project_person_control_audit_changes_empty")
        return cls(
            action=event_action,
            actor_subject=actor,
            project_ref=identity.project_ref,
            target_subject=identity.target_subject,
            request_id=request,
            occurred_at=timestamp,
            before_revision=int(before.card_revision) if before is not None else 0,
            after_revision=int(after.card_revision),
            changes=changes,
        )

    def to_dict(self) -> dict[str, Any]:
        moment = datetime.fromtimestamp(self.occurred_at, tz=timezone.utc)
        return {
            "schema": PROJECT_PERSON_CONTROL_AUDIT_SCHEMA,
            "action": self.action,
            "actor_subject": self.actor_subject,
            "project_ref": self.project_ref,
            "target_subject": self.target_subject,
            "request_id": self.request_id,
            "occurred_at": moment.isoformat().replace("+00:00", "Z"),
            "occurred_at_epoch": self.occurred_at,
            "before_revision": self.before_revision,
            "after_revision": self.after_revision,
            "changes": copy.deepcopy(dict(self.changes)),
        }


def bind_project_person_control(
    authority: CardAuthority,
    *,
    identity: ProjectPersonControlIdentity,
    audit: ProjectPersonControlAudit | None = None,
) -> CardAuthority:
    """Bind project ownership, target identity, and optional revision audit."""

    properties = copy.deepcopy(dict(authority.properties or {}))
    properties[PROJECT_PERSON_CONTROL_PROPERTY] = identity.to_property()
    provenance = copy.deepcopy(dict(authority.provenance or {}))
    if audit is not None:
        if audit.project_ref != identity.project_ref:
            raise ProjectPersonControlError("project_person_control_audit_project_mismatch")
        if audit.target_subject != identity.target_subject:
            raise ProjectPersonControlError("project_person_control_audit_target_mismatch")
        if audit.after_revision != authority.card_revision:
            raise ProjectPersonControlError("project_person_control_audit_revision_mismatch")
        provenance[PROJECT_PERSON_CONTROL_AUDIT_PROVENANCE] = audit.to_dict()
    result = dataclasses.replace(
        authority,
        access_id=identity.control_id,
        grantor_subject=identity.project_subject,
        issuer_ref=identity.project_ref,
        issuer_kind=PROJECT_PERSON_CONTROL_ISSUER_KIND,
        properties=properties,
        provenance=provenance,
    )
    ProjectPersonControlIdentity.from_authority(result)
    return result


__all__ = [
    "PROJECT_PERSON_CONTROL_AUDIT_PROVENANCE",
    "PROJECT_PERSON_CONTROL_AUDIT_SCHEMA",
    "PROJECT_PERSON_CONTROL_ISSUER_KIND",
    "PROJECT_PERSON_CONTROL_PROPERTY",
    "PROJECT_PERSON_CONTROL_SCHEMA",
    "ProjectPersonControlAudit",
    "ProjectPersonControlError",
    "ProjectPersonControlIdentity",
    "bind_project_person_control",
    "project_authority_subject",
    "project_person_control_diff",
    "project_person_control_id",
    "project_person_control_snapshot",
]

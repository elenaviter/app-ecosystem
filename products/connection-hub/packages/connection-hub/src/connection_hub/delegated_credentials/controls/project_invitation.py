# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Pending project-invitation Control Card identity and mutation evidence."""

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
from connection_hub.delegated_credentials.controls.project_person import (
    project_authority_subject,
)
from connection_hub.delegated_credentials.named_service_policy import clean_text
from connection_hub.delegated_credentials.project_invitation_binding import (
    normalized_email_digest,
)


PROJECT_INVITATION_CONTROL_SCHEMA = "connection_hub.project_invitation_control.v1"
PROJECT_INVITATION_CONTROL_PROPERTY = "connection_hub.project_invitation_control"
PROJECT_INVITATION_CONTROL_AUDIT_SCHEMA = (
    "connection_hub.project_invitation_control.audit.v1"
)
PROJECT_INVITATION_CONTROL_AUDIT_PROVENANCE = "project_invitation_control_audit"
PROJECT_INVITATION_CONTROL_ISSUER_KIND = "project-invitation"


class ProjectInvitationControlError(ValueError):
    """A pending invitation Control Card identity or audit is invalid."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _required(value: Any, reason: str) -> str:
    result = clean_text(value)
    if not result:
        raise ProjectInvitationControlError(reason)
    return result


def project_invitation_control_id(project_ref: Any, invitation_ref: Any) -> str:
    """Stable pending Card id for exactly one invitation in one project."""

    project = _required(project_ref, "project_invitation_control_project_ref_missing")
    invitation = _required(
        invitation_ref,
        "project_invitation_control_invitation_ref_missing",
    )
    digest = hashlib.sha256(f"{project}\0{invitation}".encode("utf-8")).hexdigest()[:24]
    return f"invitation-control-{digest}"


def _email_digest(value: Any) -> str:
    digest = _required(value, "project_invitation_control_email_digest_missing").lower()
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise ProjectInvitationControlError(
            "project_invitation_control_email_digest_invalid"
        )
    return digest


@dataclass(frozen=True)
class ProjectInvitationControlIdentity:
    """Project, invitation, and invited email bound to one pending Card."""

    project_ref: str
    invitation_ref: str
    target_email_digest: str
    project_subject: str
    control_id: str

    @classmethod
    def build(
        cls,
        *,
        project_ref: Any,
        invitation_ref: Any,
        target_email: Any,
    ) -> "ProjectInvitationControlIdentity":
        return cls.from_digest(
            project_ref=project_ref,
            invitation_ref=invitation_ref,
            target_email_digest=normalized_email_digest(target_email),
        )

    @classmethod
    def from_digest(
        cls,
        *,
        project_ref: Any,
        invitation_ref: Any,
        target_email_digest: Any,
    ) -> "ProjectInvitationControlIdentity":
        project = _required(
            project_ref,
            "project_invitation_control_project_ref_missing",
        )
        invitation = _required(
            invitation_ref,
            "project_invitation_control_invitation_ref_missing",
        )
        return cls(
            project_ref=project,
            invitation_ref=invitation,
            target_email_digest=_email_digest(target_email_digest),
            project_subject=project_authority_subject(project),
            control_id=project_invitation_control_id(project, invitation),
        )

    @classmethod
    def from_authority(
        cls,
        authority: CardAuthority,
    ) -> "ProjectInvitationControlIdentity":
        raw = dict(authority.properties or {}).get(PROJECT_INVITATION_CONTROL_PROPERTY)
        if not isinstance(raw, Mapping):
            raise ProjectInvitationControlError(
                "project_invitation_control_marker_missing"
            )
        if clean_text(raw.get("schema")) != PROJECT_INVITATION_CONTROL_SCHEMA:
            raise ProjectInvitationControlError(
                "project_invitation_control_schema_invalid"
            )
        identity = cls.from_digest(
            project_ref=raw.get("project_ref"),
            invitation_ref=raw.get("invitation_ref"),
            target_email_digest=raw.get("target_email_digest"),
        )
        mismatches = (
            (
                clean_text(raw.get("project_subject")) != identity.project_subject,
                "project_invitation_control_project_subject_mismatch",
            ),
            (
                authority.grantor_subject != identity.project_subject,
                "project_invitation_control_grantor_mismatch",
            ),
            (
                authority.access_id != identity.control_id,
                "project_invitation_control_id_mismatch",
            ),
            (
                authority.issuer_kind != PROJECT_INVITATION_CONTROL_ISSUER_KIND,
                "project_invitation_control_issuer_kind_mismatch",
            ),
            (
                authority.issuer_ref != identity.invitation_ref,
                "project_invitation_control_issuer_ref_mismatch",
            ),
            (
                authority.composition_mode != CONTROL_COMPOSITION_AND,
                "project_invitation_control_requires_and",
            ),
        )
        reason = next((reason for condition, reason in mismatches if condition), "")
        if reason:
            raise ProjectInvitationControlError(reason)
        return identity

    def to_property(self) -> dict[str, str]:
        return {
            "schema": PROJECT_INVITATION_CONTROL_SCHEMA,
            "project_ref": self.project_ref,
            "invitation_ref": self.invitation_ref,
            "target_email_digest": self.target_email_digest,
            "project_subject": self.project_subject,
        }


def _public_properties(authority: CardAuthority) -> dict[str, Any]:
    properties = copy.deepcopy(dict(authority.properties or {}))
    properties.pop(PROJECT_INVITATION_CONTROL_PROPERTY, None)
    return properties


def project_invitation_control_snapshot(authority: CardAuthority) -> dict[str, Any]:
    """The pending authorization fields whose changes are audited."""

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


def project_invitation_control_diff(
    before: CardAuthority | None,
    after: CardAuthority,
) -> dict[str, dict[str, Any]]:
    previous = project_invitation_control_snapshot(before) if before is not None else {}
    current = project_invitation_control_snapshot(after)
    return {
        field: {
            "before": copy.deepcopy(previous.get(field)),
            "after": copy.deepcopy(current.get(field)),
        }
        for field in sorted(set(previous) | set(current))
        if previous.get(field) != current.get(field)
    }


@dataclass(frozen=True)
class ProjectInvitationControlAudit:
    """Actor, request, and exact change on a pending invitation Card."""

    action: str
    actor_subject: str
    project_ref: str
    invitation_ref: str
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
        identity: ProjectInvitationControlIdentity,
        request_id: Any,
        occurred_at: int,
        before: CardAuthority | None,
        after: CardAuthority,
    ) -> "ProjectInvitationControlAudit":
        changes = project_invitation_control_diff(before, after)
        if not changes:
            raise ProjectInvitationControlError(
                "project_invitation_control_audit_changes_empty"
            )
        timestamp = int(occurred_at)
        if timestamp < 1:
            raise ProjectInvitationControlError(
                "project_invitation_control_audit_time_invalid"
            )
        return cls(
            action=_required(
                action,
                "project_invitation_control_audit_action_missing",
            ),
            actor_subject=_required(
                actor_subject,
                "project_invitation_control_audit_actor_missing",
            ),
            project_ref=identity.project_ref,
            invitation_ref=identity.invitation_ref,
            request_id=_required(
                request_id,
                "project_invitation_control_audit_request_missing",
            ),
            occurred_at=timestamp,
            before_revision=int(before.card_revision) if before is not None else 0,
            after_revision=int(after.card_revision),
            changes=changes,
        )

    def to_dict(self) -> dict[str, Any]:
        moment = datetime.fromtimestamp(self.occurred_at, tz=timezone.utc)
        return {
            "schema": PROJECT_INVITATION_CONTROL_AUDIT_SCHEMA,
            "action": self.action,
            "actor_subject": self.actor_subject,
            "project_ref": self.project_ref,
            "invitation_ref": self.invitation_ref,
            "request_id": self.request_id,
            "occurred_at": moment.isoformat().replace("+00:00", "Z"),
            "occurred_at_epoch": self.occurred_at,
            "before_revision": self.before_revision,
            "after_revision": self.after_revision,
            "changes": copy.deepcopy(dict(self.changes)),
        }


def bind_project_invitation_control(
    authority: CardAuthority,
    *,
    identity: ProjectInvitationControlIdentity,
    audit: ProjectInvitationControlAudit | None = None,
) -> CardAuthority:
    """Bind pending invitation identity and optional mutation audit."""

    properties = copy.deepcopy(dict(authority.properties or {}))
    properties[PROJECT_INVITATION_CONTROL_PROPERTY] = identity.to_property()
    provenance = copy.deepcopy(dict(authority.provenance or {}))
    if audit is not None:
        if audit.project_ref != identity.project_ref:
            raise ProjectInvitationControlError(
                "project_invitation_control_audit_project_mismatch"
            )
        if audit.invitation_ref != identity.invitation_ref:
            raise ProjectInvitationControlError(
                "project_invitation_control_audit_invitation_mismatch"
            )
        if audit.after_revision != authority.card_revision:
            raise ProjectInvitationControlError(
                "project_invitation_control_audit_revision_mismatch"
            )
        provenance[PROJECT_INVITATION_CONTROL_AUDIT_PROVENANCE] = audit.to_dict()
    result = dataclasses.replace(
        authority,
        access_id=identity.control_id,
        grantor_subject=identity.project_subject,
        issuer_ref=identity.invitation_ref,
        issuer_kind=PROJECT_INVITATION_CONTROL_ISSUER_KIND,
        properties=properties,
        provenance=provenance,
    )
    ProjectInvitationControlIdentity.from_authority(result)
    return result


__all__ = [
    "PROJECT_INVITATION_CONTROL_AUDIT_PROVENANCE",
    "PROJECT_INVITATION_CONTROL_AUDIT_SCHEMA",
    "PROJECT_INVITATION_CONTROL_ISSUER_KIND",
    "PROJECT_INVITATION_CONTROL_PROPERTY",
    "PROJECT_INVITATION_CONTROL_SCHEMA",
    "ProjectInvitationControlAudit",
    "ProjectInvitationControlError",
    "ProjectInvitationControlIdentity",
    "bind_project_invitation_control",
    "project_invitation_control_diff",
    "project_invitation_control_id",
    "project_invitation_control_snapshot",
]

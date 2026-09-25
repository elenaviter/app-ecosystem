# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Evidence and durable marker policy for invitation claims."""

from __future__ import annotations

import copy
from typing import Any, Mapping

from connection_hub.delegated_credentials.project_invitation_binding import (
    PROJECT_INVITATION_BINDING_SCHEMA,
    ProjectInvitationBindingError,
    ProjectInvitationBindingEvidence,
    ProjectInvitationBindingResolver,
)


PROJECT_INVITATION_BINDING_PROVENANCE = "project_invitation_binding"


class ProjectInvitationClaimPolicy:
    """Resolve provider evidence and validate the claim stored on both Cards."""

    def __init__(
        self,
        *,
        binding_resolver: ProjectInvitationBindingResolver | None,
    ) -> None:
        self._binding_resolver = binding_resolver

    @staticmethod
    def new_marker(
        *,
        evidence: ProjectInvitationBindingEvidence,
        target_email_digest: str,
        live_control_id: str,
        request_id: str,
        bound_at: int,
    ) -> dict[str, Any]:
        return {
            "schema": PROJECT_INVITATION_BINDING_SCHEMA,
            "project_ref": evidence.project_ref,
            "invitation_ref": evidence.invitation_ref,
            "pending_control_id": evidence.control_id,
            "control_id": live_control_id,
            "person_subject": evidence.person_subject,
            "target_email_digest": target_email_digest,
            "request_id": request_id,
            "bound_at": bound_at,
        }

    @staticmethod
    def validate_marker(
        marker: Any,
        *,
        evidence: ProjectInvitationBindingEvidence,
        target_email_digest: str,
        live_control_id: str,
    ) -> dict[str, Any]:
        if not isinstance(marker, Mapping):
            raise ProjectInvitationBindingError(
                "project_invitation_binding_marker_invalid"
            )
        expected = {
            "schema": PROJECT_INVITATION_BINDING_SCHEMA,
            "project_ref": evidence.project_ref,
            "invitation_ref": evidence.invitation_ref,
            "pending_control_id": evidence.control_id,
            "control_id": live_control_id,
            "person_subject": evidence.person_subject,
            "target_email_digest": target_email_digest,
        }
        if any(marker.get(key) != value for key, value in expected.items()):
            raise ProjectInvitationBindingError(
                "project_invitation_binding_marker_mismatch"
            )
        if not str(marker.get("request_id") or "").strip():
            raise ProjectInvitationBindingError(
                "project_invitation_binding_marker_invalid"
            )
        if type(marker.get("bound_at")) is not int or marker["bound_at"] < 1:
            raise ProjectInvitationBindingError(
                "project_invitation_binding_marker_invalid"
            )
        return copy.deepcopy(dict(marker))

    async def evidence(
        self,
        *,
        actor_subject: str,
        project_ref: str,
        invitation_ref: str,
        control_id: str,
    ) -> ProjectInvitationBindingEvidence | dict[str, Any]:
        if self._binding_resolver is None:
            return {
                "ok": False,
                "error": "project_invitation_binding_unavailable",
                "reason": "binding_resolver_not_configured",
                "retryable": True,
                "status": 503,
            }
        try:
            evidence = await self._binding_resolver.resolve_project_invitation_binding(
                project_ref=project_ref,
                invitation_ref=invitation_ref,
            )
        except ProjectInvitationBindingError as exc:
            return {
                "ok": False,
                "error": "project_invitation_binding_unavailable",
                "reason": exc.reason,
                "retryable": True,
                "status": 503,
            }
        except Exception:  # noqa: BLE001 - provider is an availability boundary
            return {
                "ok": False,
                "error": "project_invitation_binding_unavailable",
                "reason": "binding_resolver_failed",
                "retryable": True,
                "status": 503,
            }
        if evidence is None:
            return {
                "ok": False,
                "error": "project_invitation_binding_not_found",
                "status": 403,
            }
        if not isinstance(evidence, ProjectInvitationBindingEvidence):
            return {
                "ok": False,
                "error": "project_invitation_binding_invalid",
                "reason": "binding_evidence_type_invalid",
                "status": 503,
                "retryable": True,
            }
        try:
            evidence.validate_for(
                project_ref=str(project_ref).strip(),
                invitation_ref=str(invitation_ref).strip(),
                control_id=str(control_id).strip(),
                person_subject=str(actor_subject).strip(),
            )
        except ProjectInvitationBindingError as exc:
            return {
                "ok": False,
                "error": "project_invitation_binding_invalid",
                "reason": exc.reason,
                "status": 403,
            }
        return evidence


__all__ = [
    "PROJECT_INVITATION_BINDING_PROVENANCE",
    "ProjectInvitationClaimPolicy",
]

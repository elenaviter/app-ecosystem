# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Host-neutral evidence contract for binding a project invitation to a person."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from connection_hub.delegated_credentials.named_service_policy import clean_text


PROJECT_INVITATION_BINDING_SCHEMA = "connection_hub.project_invitation_binding.v1"


class ProjectInvitationBindingError(ValueError):
    """Invitation binding configuration or provider evidence is invalid."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _required(value: Any, reason: str) -> str:
    result = clean_text(value)
    if not result:
        raise ProjectInvitationBindingError(reason)
    return result


def normalized_email(value: Any) -> str:
    """The Problem Board invitation email identity, without preserving casing."""

    return _required(value, "project_invitation_binding_email_missing").lower()


def normalized_email_digest(value: Any) -> str:
    """Stable SHA-256 digest for one normalized invitation email."""

    return hashlib.sha256(normalized_email(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ProjectInvitationBindingConfig:
    """Descriptor-owned provider for current invitation binding evidence."""

    provider_bundle_id: str = ""
    provider_operation: str = ""

    @classmethod
    def from_mapping(cls, value: Any) -> "ProjectInvitationBindingConfig":
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise ProjectInvitationBindingError(
                "project_invitation_binding_config_invalid"
            )
        provider = value.get("provider")
        if provider is None:
            provider = {}
        if not isinstance(provider, Mapping):
            raise ProjectInvitationBindingError(
                "project_invitation_binding_provider_invalid"
            )
        bundle_id = clean_text(provider.get("bundle_id"))
        operation = clean_text(provider.get("operation"))
        if bool(bundle_id) != bool(operation):
            raise ProjectInvitationBindingError(
                "project_invitation_binding_provider_invalid"
            )
        return cls(
            provider_bundle_id=bundle_id,
            provider_operation=operation,
        )

    @property
    def provider_configured(self) -> bool:
        return bool(self.provider_bundle_id and self.provider_operation)


@dataclass(frozen=True)
class ProjectInvitationBindingEvidence:
    """One provider-verified invitation, person, Card, and email binding."""

    project_ref: str
    invitation_ref: str
    control_id: str
    person_subject: str
    email: str

    @classmethod
    def build(
        cls,
        *,
        project_ref: Any,
        invitation_ref: Any,
        control_id: Any,
        person_subject: Any,
        email: Any,
    ) -> "ProjectInvitationBindingEvidence":
        return cls(
            project_ref=_required(
                project_ref,
                "project_invitation_binding_project_ref_missing",
            ),
            invitation_ref=_required(
                invitation_ref,
                "project_invitation_binding_invitation_ref_missing",
            ),
            control_id=_required(
                control_id,
                "project_invitation_binding_control_id_missing",
            ),
            person_subject=_required(
                person_subject,
                "project_invitation_binding_person_subject_missing",
            ),
            email=normalized_email(email),
        )

    @property
    def email_digest(self) -> str:
        return normalized_email_digest(self.email)

    def validate_for(
        self,
        *,
        project_ref: str,
        invitation_ref: str,
        control_id: str,
        person_subject: str,
    ) -> None:
        expected = {
            "project_ref": project_ref,
            "invitation_ref": invitation_ref,
            "control_id": control_id,
            "person_subject": person_subject,
        }
        actual = {
            "project_ref": self.project_ref,
            "invitation_ref": self.invitation_ref,
            "control_id": self.control_id,
            "person_subject": self.person_subject,
        }
        mismatch = next(
            (name for name, value in expected.items() if actual[name] != value),
            "",
        )
        if mismatch:
            raise ProjectInvitationBindingError(
                f"project_invitation_binding_{mismatch}_mismatch"
            )
        normalized_email(self.email)


class ProjectInvitationBindingResolver(Protocol):
    """Application-owned lookup for the invitation bound to this session."""

    async def resolve_project_invitation_binding(
        self,
        *,
        project_ref: str,
        invitation_ref: str,
    ) -> ProjectInvitationBindingEvidence | None: ...


__all__ = [
    "PROJECT_INVITATION_BINDING_SCHEMA",
    "ProjectInvitationBindingConfig",
    "ProjectInvitationBindingError",
    "ProjectInvitationBindingEvidence",
    "ProjectInvitationBindingResolver",
    "normalized_email",
    "normalized_email_digest",
]

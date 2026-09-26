# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Compose a person's Card with the Control Card their project holds for them.

A project person's Control Card is stored under the project's authority
subject, not under the person who owns the My Card bound to it. Ordinary
Control Card composition requires one grantor, so a My Card bound to a
project-held Control Card was resolved under the person's own subject, found
nothing, and failed closed as ``control_card_unresolvable`` (W260, 2026-09-26,
right after the project moved its people to Cards).

This module names where such a Control Card lives and composes the two with
the same selection intersection the My Card seed uses. It applies only when
the binding is exactly the person's derived Control Card on the binding's
project; any other binding keeps the ordinary path.
"""

from __future__ import annotations

from dataclasses import dataclass

from connection_hub.delegated_credentials.cards.model import CARD_STATE_ACTIVE, CardAuthority
from connection_hub.delegated_credentials.controls.effective import (
    ControlCardMismatch,
    intersect_card_authority_selection,
)
from connection_hub.delegated_credentials.controls.project_person import (
    PROJECT_PERSON_CONTROL_ISSUER_KIND,
    ProjectPersonControlError,
    ProjectPersonControlIdentity,
)


@dataclass(frozen=True)
class ProjectHeldControl:
    """Where a card's project-held Control Card is stored."""

    control_id: str
    grantor_subject: str
    project_ref: str


def project_held_control(card: CardAuthority) -> ProjectHeldControl | None:
    """The project-held Control Card this card is bound to, or None for any other binding."""

    binding = card.control_card
    if binding is None or binding.issuer_kind != PROJECT_PERSON_CONTROL_ISSUER_KIND:
        return None
    project_ref = str(binding.issuer_ref or "").strip()
    person = str(card.grantor_subject or "").strip()
    if not project_ref or not person:
        return None
    try:
        identity = ProjectPersonControlIdentity.build(project_ref=project_ref, target_subject=person)
    except ProjectPersonControlError:
        return None
    if identity.control_id != binding.control_id:
        # Not this person's Control Card on that project: the ordinary path decides.
        return None
    return ProjectHeldControl(
        control_id=identity.control_id,
        grantor_subject=identity.project_subject,
        project_ref=identity.project_ref,
    )


def compose_with_project_held_control(card: CardAuthority, control: CardAuthority) -> CardAuthority:
    """The card narrowed by its project-held Control Card; ControlCardMismatch when they do not belong together."""

    held = project_held_control(card)
    if held is None:
        raise ControlCardMismatch("control_card_binding_mismatch")
    try:
        identity = ProjectPersonControlIdentity.from_authority(control)
    except ProjectPersonControlError as exc:
        raise ControlCardMismatch(str(getattr(exc, "reason", "") or exc) or "project_person_control_invalid") from exc
    if identity.control_id != held.control_id or identity.target_subject != card.grantor_subject:
        raise ControlCardMismatch("control_card_binding_mismatch")
    if control.state != CARD_STATE_ACTIVE:
        raise ControlCardMismatch("control_card_not_active")
    return intersect_card_authority_selection(card, control)


__all__ = [
    "ProjectHeldControl",
    "compose_with_project_held_control",
    "project_held_control",
]

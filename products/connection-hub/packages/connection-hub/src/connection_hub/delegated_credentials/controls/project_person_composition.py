# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Compose a person's Card with the Control Card their project holds for them.

A project person's Control Card is stored under the project's authority
subject, not under the person who owns the My Card bound to it. Ordinary
Control Card composition requires one grantor, so a My Card bound to a
project-held Control Card was resolved under the person's own subject, found
nothing, and failed closed as ``control_card_unresolvable`` (W260, 2026-09-26,
right after the project moved its people to Cards).

This module names where such a Control Card lives and validates the exact
cross-owner edge before upstream-owned selection composition. It applies only when
the binding is exactly the person's derived Control Card on the binding's
project; any other binding keeps the ordinary path.
"""

from __future__ import annotations

from dataclasses import dataclass

from connection_hub.delegated_credentials.cards.model import (
    CARD_STATE_ACTIVE,
    CardAuthority,
    authority_is_credentialless,
)
from connection_hub.delegated_credentials.controls.snapshot import control_snapshot_is_exact
from connection_hub.delegated_credentials.controls.effective import (
    ControlCardMismatch,
    compose_card_authority_selection,
)
from connection_hub.delegated_credentials.controls.model import ControlCardError, control_card_id_for_issuer
from connection_hub.delegated_credentials.controls.project_person import (
    PROJECT_PERSON_CONTROL_ISSUER_KIND,
    ProjectPersonControlError,
    ProjectPersonControlIdentity,
)

# W502 C -> P: the project's Control Card is an application-issued Control for
# the project, held by the person who created it (the project host records
# that creator). A person's project Control C is bound under it so the
# project's operation applies to everything below (W579).
PROJECT_CONTROL_ISSUER_KIND = "application"


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


@dataclass(frozen=True)
class ProjectControlParent:
    """The project Control Card a person's project Control C is bound under."""

    control_id: str
    holder_subject: str
    project_ref: str


def project_control_parent(card: CardAuthority) -> ProjectControlParent | None:
    """The exact C -> P edge, or None for any other binding (which the ordinary path decides).

    Admitted only when ``card`` is an exact project-person Control for project
    q, its binding names an application Control for the same q held by a
    recorded holder, and the bound id is exactly the one that holder's
    application Control for q has. Nothing else qualifies, so the generic
    foreign-holder rule still governs every other cross-owner edge.
    """

    binding = card.control_card
    if binding is None or binding.issuer_kind != PROJECT_CONTROL_ISSUER_KIND:
        return None
    holder = str(binding.holder_subject or "").strip()
    if not holder or holder == card.grantor_subject:
        return None
    try:
        identity = ProjectPersonControlIdentity.from_authority(card)
        expected = control_card_id_for_issuer(PROJECT_CONTROL_ISSUER_KIND, identity.project_ref,
                                              grantor_subject=holder)
    except (ProjectPersonControlError, ControlCardError):
        return None
    if binding.issuer_ref != identity.project_ref or binding.control_id != expected:
        return None
    return ProjectControlParent(control_id=expected, holder_subject=holder, project_ref=identity.project_ref)


def compose_with_project_control(card: CardAuthority, control: CardAuthority) -> CardAuthority:
    """Compose the exact C -> P edge with P's own operation (AND or OR), never a weaker check.

    The project host authorized this binding (the Hub binds only the P its
    trusted decision names), so P's operation applies to the whole C/My
    subtree, as for a same-holder parent. Every other guard still holds.
    """

    parent = project_control_parent(card)
    if parent is None:
        raise ControlCardMismatch("control_card_binding_mismatch")
    if control.access_id != parent.control_id or control.grantor_subject != parent.holder_subject:
        raise ControlCardMismatch("control_card_grantor_mismatch")
    if control.issuer_kind != PROJECT_CONTROL_ISSUER_KIND or str(control.issuer_ref or "") != parent.project_ref:
        raise ControlCardMismatch("control_card_issuer_mismatch")
    if not authority_is_credentialless(control):
        raise ControlCardMismatch("control_card_has_credential")
    if not control_snapshot_is_exact(control):
        raise ControlCardMismatch("control_card_exact_snapshot_required")
    card_scope = str(card.identity_scope or "grantor").strip() or "grantor"
    control_scope = str(control.identity_scope or "grantor").strip() or "grantor"
    if card_scope != control_scope:
        raise ControlCardMismatch("control_card_identity_scope_mismatch")
    if control.state != CARD_STATE_ACTIVE:
        raise ControlCardMismatch("control_card_not_active")
    if control.control_card is not None:
        # Root option (a), 2026-10-06: the exact PB-linked P is the project's
        # top authority boundary. Only this qualified edge's P; other
        # application Controls keep their parents.
        raise ControlCardMismatch("project_control_not_root")
    return compose_card_authority_selection(card, control)


def compose_with_project_held_control(card: CardAuthority, control: CardAuthority) -> CardAuthority:
    """Compose an exact person/Control edge; refuse unrelated cross-owner links."""

    held = project_held_control(card)
    if held is None:
        raise ControlCardMismatch("control_card_binding_mismatch")
    try:
        identity = ProjectPersonControlIdentity.from_authority(control)
    except ProjectPersonControlError as exc:
        raise ControlCardMismatch(str(getattr(exc, "reason", "") or exc) or "project_person_control_invalid") from exc
    if identity.control_id != held.control_id or identity.target_subject != card.grantor_subject:
        raise ControlCardMismatch("control_card_binding_mismatch")
    # The guards ordinary composition applies, kept here so every caller of
    # this composition gets them (review on app-ecosystem#162): a Control Card
    # never carries a credential, is an exact snapshot,
    # is issued by the binding's project, and shares the Card's identity scope.
    if not authority_is_credentialless(control):
        raise ControlCardMismatch("control_card_has_credential")
    if not control_snapshot_is_exact(control):
        raise ControlCardMismatch("control_card_exact_snapshot_required")
    if str(control.issuer_ref or "") != held.project_ref:
        raise ControlCardMismatch("control_card_issuer_mismatch")
    card_scope = str(card.identity_scope or "grantor").strip() or "grantor"
    control_scope = str(control.identity_scope or "grantor").strip() or "grantor"
    if card_scope != control_scope:
        raise ControlCardMismatch("control_card_identity_scope_mismatch")
    if control.state != CARD_STATE_ACTIVE:
        raise ControlCardMismatch("control_card_not_active")
    return compose_card_authority_selection(card, control)


__all__ = [
    "PROJECT_CONTROL_ISSUER_KIND",
    "ProjectControlParent",
    "compose_with_project_control",
    "project_control_parent",
    "ProjectHeldControl",
    "compose_with_project_held_control",
    "project_held_control",
]

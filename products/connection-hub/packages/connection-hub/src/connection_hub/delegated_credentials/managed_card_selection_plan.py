# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter
"""Non-writing selection candidates for a project's own Control and its agent Cards.

The person-Control and My reselect (``existing_card_selection_plan``) covers a
project person's Cards. This module covers the two other managed Cards a
project host edits in its own transaction:

- ``project_control``: the project's Control Card P, the application Control
  at ``control_card_id_for_issuer("application", <project>, grantor_subject=<holder>)``;
- ``agent_card``: a Card bound directly under that P (a project agent Card).

The host has already authorized the step for the exact operation and target;
this module only checks the target's identity, resolves the selection against
the active catalog, and builds one group member. It writes nothing, and its
output is never an authorization or a publication decision.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Mapping

from service_foundation.coordination.durable_decision_log import DecisionRefused

from .agent_capability_control import preserve_descriptor_acceptance
from .caller_writer_gate import candidate_shape_refusal
from .card_lifecycle_plan import _with_selection
from .cards.card_group import group_member
from .cards.model import CARD_STATE_ACTIVE, CardAuthority, authority_is_credentialless
from .catalog.descriptors import next_resource_acceptance
from .controls.model import control_card_id_for_issuer
from .existing_card_selection_plan import undisplayed_change
from .controls.project_invitation import (
    PROJECT_INVITATION_CONTROL_ISSUER_KIND, ProjectInvitationControlError, ProjectInvitationControlIdentity,
)
from .project_authorization import (
    PROJECT_AGENT_CARD_UPDATE, PROJECT_CONTROL_UPDATE, PROJECT_INVITATION_CONTROL_UPDATE, ProjectAuthorizationDecision,
)
OPERATIONS = {"project_control": PROJECT_CONTROL_UPDATE, "agent_card": PROJECT_AGENT_CARD_UPDATE,
              "invitation_control": PROJECT_INVITATION_CONTROL_UPDATE}
_SELECTION_FIELDS = frozenset({"resource_grants", "resource_operations", "named_service_operations", "account_scope"})


def _refuse(reason: str) -> DecisionRefused:
    return DecisionRefused(reason)


def _is_project_control(card: CardAuthority, project_ref: str) -> bool:
    return (card.issuer_kind == "application" and card.issuer_ref == project_ref
            and authority_is_credentialless(card)
            and card.access_id == control_card_id_for_issuer("application", project_ref,
                                                              grantor_subject=card.grantor_subject))


def managed_target_kind(original: CardAuthority, *, project_ref: str, kind: str) -> str:
    """The exact managed kind of this Card in this project, or a refusal."""
    if not isinstance(original, CardAuthority) or original.state != CARD_STATE_ACTIVE or original.card_revision < 1:
        raise _refuse("card_plan_update_scope_invalid")
    if kind == "project_control" and _is_project_control(original, project_ref) and original.control_card is None:
        return kind
    if kind == "invitation_control" and original.issuer_kind == PROJECT_INVITATION_CONTROL_ISSUER_KIND             and authority_is_credentialless(original):
        # A pending invitation's Control: its own identity marker names the project.
        try:
            identity = ProjectInvitationControlIdentity.from_authority(original)
        except ProjectInvitationControlError as exc:
            raise _refuse("card_plan_update_scope_invalid") from exc
        if identity.project_ref == project_ref and identity.control_id == original.access_id:
            return kind
        raise _refuse("card_plan_update_scope_invalid")
    binding = original.control_card
    if (kind == "agent_card" and binding is not None and binding.issuer_kind == "application"
            and binding.issuer_ref == project_ref
            and binding.control_id == control_card_id_for_issuer("application", project_ref,
                                                                  grantor_subject=binding.holder_subject)):
        return kind
    raise _refuse("card_plan_update_scope_invalid")


async def build_managed_card_selection_update(
    host: Any, *, original: CardAuthority, kind: str, selection: Mapping[str, Any], active: Any,
    decision: ProjectAuthorizationDecision, project_ref: str, actor_subject: str, request_id: str, now: int,
) -> dict[str, Any]:
    """One exact P or agent-Card update from a host-authorized selection, without a write."""
    if kind not in OPERATIONS or not isinstance(selection, Mapping) or set(selection) - _SELECTION_FIELDS:
        raise _refuse("card_plan_selection_invalid")
    if type(now) is not int or now < 1:
        raise _refuse("card_plan_audit_time_invalid")
    managed_target_kind(original, project_ref=project_ref, kind=kind)
    if (not isinstance(decision, ProjectAuthorizationDecision) or not decision.allowed
            or decision.project_ref != project_ref or decision.actor_subject != actor_subject
            or decision.request_id != request_id or decision.operation != OPERATIONS[kind]
            or decision.target_subject != original.access_id):
        raise _refuse("card_plan_authorization_invalid")
    for field in ("resource_grants", "resource_operations", "account_scope"):
        if field in selection and not isinstance(selection[field], Mapping):
            raise _refuse("card_plan_selection_invalid")
    resolved = await host._resolve_card_authority(
        user={"user_id": original.grantor_subject}, existing=original, active=active,
        resource_grants=selection.get("resource_grants", original.resource_grants),
        resource_operations=selection.get("resource_operations", original.resource_operations),
        operations=(),
        named_service_operations=selection.get("named_service_operations",
                                               original.named_service_operations.to_stored()),
        account_scope=selection.get("account_scope", original.account_scope),
        properties=dict(original.properties or {}),
        _delegable_grants=decision.delegable_grants, _platform_admin=decision.platform_admin,
    )
    if resolved.error is not None:
        raise _refuse(str(resolved.error.get("error") or "card_plan_selection_invalid"))
    if resolved.revoke:
        raise _refuse("card_plan_selection_empty")
    catalog_version = host._version_of(active)
    if not catalog_version:
        raise _refuse("card_plan_catalog_version_missing")
    catalog_config = await host._catalog_config(active, owner_subject=original.grantor_subject)
    computed = next_resource_acceptance(
        resources=resolved.resource_grants,
        row_for=lambda resource: host._configured_resource(resource, config=catalog_config),
        catalog_version=catalog_version, selected_operations=resolved.resource_operations,
        previous=original.resource_acceptance)
    selected = _with_selection(original, resolved=resolved, catalog_version=catalog_version,
        resource_row_for=lambda resource: host._configured_resource(resource, config=catalog_config))
    candidate = dataclasses.replace(selected, card_revision=original.card_revision + 1,
        catalog_version=catalog_version,
        resource_acceptance=preserve_descriptor_acceptance(original.resource_acceptance, computed))
    if candidate.control_card != original.control_card:
        raise _refuse("card_plan_update_scope_invalid")  # an edit never moves a Card in the hierarchy
    before, after = original.to_dict(), candidate.to_dict()
    after["card_revision"] = before["card_revision"]
    if after == before:
        raise _refuse("card_plan_selection_unchanged")
    refusal = candidate_shape_refusal("update", original.to_dict(), candidate.to_dict())
    if refusal is not None:
        raise _refuse(refusal)
    if undisplayed_change(original.to_dict(), candidate.to_dict()) is not None:
        raise _refuse("card_plan_undisplayed_change")
    return {"member": group_member(original=original, candidate=candidate, action="update")}


__all__ = ["OPERATIONS", "PROJECT_AGENT_CARD_UPDATE", "PROJECT_CONTROL_UPDATE",
           "build_managed_card_selection_update", "managed_target_kind"]

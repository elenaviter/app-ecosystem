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
from .cards.identity import CARD_KIND_AGENT, CARD_KIND_AUTOMATION
from .cards.model import CARD_STATE_ACTIVE, CardAuthority, authority_is_credentialless
from .catalog.descriptors import next_resource_acceptance
from .controls.model import control_card_id_for_issuer
from .existing_card_selection_plan import _accepted, undisplayed_change  # W723: one acceptance validation
from .controls.project_invitation import (
    PROJECT_INVITATION_CONTROL_ISSUER_KIND, ProjectInvitationControlError, ProjectInvitationControlIdentity,
)
from .project_authorization import (
    PROJECT_AGENT_CARD_UPDATE, PROJECT_CONTROL_UPDATE, PROJECT_INVITATION_CONTROL_UPDATE, ProjectAuthorizationDecision,
)
OPERATIONS = {"project_control": PROJECT_CONTROL_UPDATE, "agent_card": PROJECT_AGENT_CARD_UPDATE,
              "invitation_control": PROJECT_INVITATION_CONTROL_UPDATE}
_SELECTION_FIELDS = frozenset({"resource_grants", "resource_operations", "named_service_operations", "account_scope", "properties"})


def _refuse(reason: str) -> DecisionRefused:
    return DecisionRefused(reason)


def _is_project_control(card: CardAuthority, project_ref: str) -> bool:
    return (card.issuer_kind == "application" and card.issuer_ref == project_ref
            and authority_is_credentialless(card)
            and card.access_id == control_card_id_for_issuer("application", project_ref,
                                                              grantor_subject=card.grantor_subject))


def is_agent_own_card(card: CardAuthority) -> bool:
    """An agent's own Card (W639's check): a person's C and My Card are bound under P too, but are not this."""
    return (card.card_kind in {CARD_KIND_AGENT, CARD_KIND_AUTOMATION}
            and card.delegate_subject.startswith("integration:")
            and card.delegate_subject != card.grantor_subject
            and card.source in {"oauth", "agent"})


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
    # The resolved holder, as the resolver reads it: attach leaves holder_subject empty when P's holder is
    # the Card's own grantor (an agent owned by P's own holder).
    if (kind == "agent_card" and binding is not None and binding.issuer_kind == "application"
            and binding.issuer_ref == project_ref
            and binding.control_id == control_card_id_for_issuer(
                "application", project_ref, grantor_subject=binding.holder_subject or original.grantor_subject)):
        # W852: the binding row PB sends never decides alone that the target is an agent's Card; the Card
        # this plan already read does, so every agent_card path refuses a person's Card with no extra read.
        if not is_agent_own_card(original):
            raise _refuse("card_plan_agent_card_target_not_agent")
        return kind
    raise _refuse("card_plan_update_scope_invalid")


async def _governed_resources(load_live: Any, original: CardAuthority) -> tuple[str, ...]:
    """The resources the agent's project governs: those its project Control P lists.

    Read through the plan's own live read (once per plan). STAGE fences that exact read
    (``participant_card_version._agent_parent_reads``), so a P edited before COMMIT refuses the save.
    """
    from .cards.store import subject_hash_for

    binding = original.control_card
    holder = str(binding.holder_subject or "") or original.grantor_subject
    project = await load_live(subject_hash_for(holder), binding.control_id) if load_live is not None else None
    if project is None:
        raise _refuse("card_plan_project_control_read_missing")
    return tuple(sorted(dict(project.resource_grants or {})))


def _verified_owner_authority(host: Any, original: CardAuthority, grantor_proof: str | None, *,
                              edit: Mapping[str, Any], actor_subject: str, request_id: str,
                              now: int) -> dict[str, Any] | None:
    """The owner's admission-time identity from a verified proof, or None when no proof was sent.

    A proof is only ever the owner's own: it is refused for any other actor, and checked against the
    stored Card (grantor, id, revision), this plan's own authorized request (the Save's request id, which
    the decision also names), the exact planned edit with its catalog pin, its expiry (the planner's own
    clock, at PLAN and again at STAGE) and the Hub's own key. Nothing beside the token is trusted.
    """
    from .grantor_authority_proof import GrantorProofError, verify_grantor_proof

    if grantor_proof is None:
        return None
    if type(grantor_proof) is not str or actor_subject != original.grantor_subject:
        raise _refuse("card_grantor_proof_invalid")
    try:
        authority = verify_grantor_proof(
            grantor_proof, secret=getattr(host, "_grantor_authority_secret", "") or "", now=now,
            grantor_subject=original.grantor_subject, access_id=original.access_id,
            original_revision=original.card_revision, request_id=request_id, edit=edit)
    except GrantorProofError as exc:
        raise _refuse(exc.reason) from exc
    return authority.identity()


async def build_managed_card_selection_update(
    host: Any, *, original: CardAuthority, kind: str, selection: Mapping[str, Any], active: Any,
    decision: ProjectAuthorizationDecision, project_ref: str, actor_subject: str, request_id: str, now: int,
    accepted_operations: Mapping[str, Any] | None = None, composition_mode: str | None = None,
    label: str | None = None, allow_unchanged: bool = False, external_mcp_family: str | None = None,
    a2a_family: str | None = None,
    invocation_modes: Mapping[str, Any] | None = None, grantor_proof: str | None = None,
    grantor_edit: Mapping[str, Any] | None = None, load_live: Any = None,
) -> dict[str, Any]:
    """One exact P or agent-Card update from a host-authorized selection, without a write (W723: and its acceptance).

    W742: ``composition_mode`` is the project access rule a project Control (P) edit sets, ``and`` or ``or``;
    it changes only that field of P, beside (or instead of) the selection. W735: ``label`` is the Card's new
    display name, changed the same way, never authority.
    """
    if kind not in OPERATIONS or not isinstance(selection, Mapping) or set(selection) - _SELECTION_FIELDS:
        raise _refuse("card_plan_selection_invalid")
    if composition_mode is not None and (kind != "project_control" or composition_mode not in {"and", "or"}):
        raise _refuse("card_plan_selection_invalid")
    # W918: the project's External MCP switch, one field of P, like the access rule.
    if external_mcp_family is not None and (kind != "project_control" or external_mcp_family not in {"on", "off"}):
        raise _refuse("card_plan_selection_invalid")
    # W937: the project's A2A switch, another field of P, the same way.
    if a2a_family is not None and (kind != "project_control" or a2a_family not in {"on", "off"}):
        raise _refuse("card_plan_selection_invalid")
    switch_values = {name: value for name, value in (("external_mcp_family", external_mcp_family),
                                                      ("a2a_family", a2a_family)) if value is not None}
    if type(now) is not int or now < 1:
        raise _refuse("card_plan_audit_time_invalid")
    managed_target_kind(original, project_ref=project_ref, kind=kind)
    if "properties" in selection and selection["properties"] != dict(original.properties or {}):
        raise _refuse("card_plan_undisplayed_change")
    if (not isinstance(decision, ProjectAuthorizationDecision) or not decision.allowed
            or decision.project_ref != project_ref or decision.actor_subject != actor_subject
            or decision.request_id != request_id or decision.operation != OPERATIONS[kind]
            or decision.target_subject != original.access_id):
        raise _refuse("card_plan_authorization_invalid")
    for field in ("resource_grants", "resource_operations", "account_scope"):
        if field in selection and not isinstance(selection[field], Mapping):
            raise _refuse("card_plan_selection_invalid")
    grantor_bound = None
    if kind == "agent_card":
        # W918 H2: the project bounds only what it governs (its own P's resources); every other
        # resource is bounded by the owner's own proven authority, and is refused without it.
        grantor_bound = {"governed": await _governed_resources(load_live, original),
                         "authority": _verified_owner_authority(
                             host, original, grantor_proof, edit=grantor_edit or {}, actor_subject=actor_subject,
                             request_id=request_id, now=now)}
    elif grantor_proof is not None:
        raise _refuse("card_plan_selection_invalid")
    catalog_config = await host._catalog_config(active, owner_subject=original.grantor_subject)
    from .whole_card_save import normalize_selection
    selection = normalize_selection(original, selection, config=catalog_config)
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
        _grantor_bound=grantor_bound,
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
        previous=original.resource_acceptance, accepted_operations=_accepted(accepted_operations))
    selected = _with_selection(original, resolved=resolved, catalog_version=catalog_version,
        resource_row_for=lambda resource: host._configured_resource(resource, config=catalog_config))
    candidate = dataclasses.replace(selected, card_revision=original.card_revision + 1,
        catalog_version=catalog_version,
        resource_acceptance=preserve_descriptor_acceptance(original.resource_acceptance, computed),
        **({"composition_mode": composition_mode} if composition_mode is not None else {}),
        **({"label": label} if label is not None else {}),
        **({"properties": {**dict(selected.properties or {}), **switch_values}} if switch_values else {}))
    if candidate.control_card != original.control_card:
        raise _refuse("card_plan_update_scope_invalid")  # an edit never moves a Card in the hierarchy
    if kind == "agent_card":
        # W918: only the Card's owner adds or changes their custom MCP connectors; anyone may remove one.
        from .owner_only_connectors import custom_mcp_owner_only_refusal
        refusal = custom_mcp_owner_only_refusal(original=original, candidate=candidate, actor_subject=actor_subject,
                                                invocation_modes=invocation_modes,
                                                accepted_operations=accepted_operations)
        if refusal is not None:
            raise _refuse(refusal)
    before, after = original.to_dict(), candidate.to_dict()
    after["card_revision"] = before["card_revision"]
    if after == before and not allow_unchanged:
        # W872: see build_existing_card_selection_update.
        raise _refuse("card_plan_selection_unchanged")
    refusal = candidate_shape_refusal("update", original.to_dict(), candidate.to_dict())
    if refusal is not None:
        raise _refuse(refusal)
    shown = original.to_dict()
    if composition_mode is not None:
        shown["composition_mode"] = candidate.to_dict().get("composition_mode")  # the one rule the person set
    if label is not None:
        shown["label"] = label  # the one display field the person renamed
    if switch_values:
        shown["properties"] = {**dict(shown.get("properties") or {}), **switch_values}
    if undisplayed_change(shown, candidate.to_dict()) is not None:
        raise _refuse("card_plan_undisplayed_change")
    return {"member": group_member(original=original, candidate=candidate, action="update")}


__all__ = ["OPERATIONS", "PROJECT_AGENT_CARD_UPDATE", "PROJECT_CONTROL_UPDATE",
           "build_managed_card_selection_update", "managed_target_kind"]

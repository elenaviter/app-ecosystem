"""Read-only PLAN candidates for a project-bound agent's existing Card.

The project host authorizes each step. This builder still checks the exact
agent, revision, project Control and operation before constructing a candidate.
It never writes a Card or treats a PLAN answer as a business decision.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Mapping

from service_foundation.coordination.durable_decision_log import DecisionRefused

from .agent_capability_control import preserve_descriptor_acceptance
from .caller_writer_gate import candidate_shape_refusal
from .cards.card_group import group_member
from .cards.identity import CARD_KIND_AGENT, CARD_KIND_AUTOMATION
from .cards.model import CARD_STATE_ACTIVE, CardAuthority, ControlCardBinding
from .cards.store import subject_hash_for
from .catalog.descriptors import next_resource_acceptance
from .controls.model import control_card_id_for_issuer
from .oauth.consent import requested_card_selection
from .project_authorization import (
    PROJECT_AGENT_CARD_APPLY_PROFILE, PROJECT_AGENT_CARD_ATTACH,
    PROJECT_AGENT_CARD_DETACH, ProjectAuthorizationDecision,
)

ATTACH_AGENT = "attach_agent"
DETACH_AGENT = "detach_agent"
APPLY_AGENT_PROFILE = "apply_agent_profile"
AGENT_ATTACH_OPERATION = PROJECT_AGENT_CARD_ATTACH
AGENT_DETACH_OPERATION = PROJECT_AGENT_CARD_DETACH
AGENT_PROFILE_OPERATION = PROJECT_AGENT_CARD_APPLY_PROFILE
AGENT_UPDATE_STEPS = {
    ATTACH_AGENT: AGENT_ATTACH_OPERATION,
    DETACH_AGENT: AGENT_DETACH_OPERATION,
    APPLY_AGENT_PROFILE: AGENT_PROFILE_OPERATION,
}
AUDIT_KEY = "project_agent_lifecycle_plan_audit"
AUDIT_SCHEMA = "connection_hub.project_agent_lifecycle_plan_audit.v1"

_COMMON = frozenset({"kind", "target_subject", "access_id", "subject_hash", "original_revision"})
_FIELDS = {
    ATTACH_AGENT: _COMMON | {"parent"},
    DETACH_AGENT: _COMMON,
    APPLY_AGENT_PROFILE: _COMMON | {"profile", "resource"},
}


def _refuse(code: str) -> DecisionRefused:
    return DecisionRefused(code)


def _text(value: Any) -> bool:
    return type(value) is str and 0 < len(value) <= 256 and value == value.strip() and value.isprintable()


def _project_parent(parent: CardAuthority | None, *, project_ref: str,
                    decision: ProjectAuthorizationDecision) -> ControlCardBinding:
    if parent is None or parent.state != CARD_STATE_ACTIVE or parent.control_card is not None:
        raise _refuse("agent_plan_project_control_invalid")
    expected = control_card_id_for_issuer("application", project_ref, grantor_subject=parent.grantor_subject)
    locator = decision.project_control
    if (parent.access_id != expected or parent.issuer_kind != "application" or parent.issuer_ref != project_ref
            or locator is None or locator.control_id != parent.access_id
            or locator.holder_subject != parent.grantor_subject):
        raise _refuse("agent_plan_project_control_invalid")
    return ControlCardBinding(control_id=parent.access_id, issuer_ref=parent.issuer_ref,
        issuer_kind=parent.issuer_kind, issuer_label=parent.issuer_label,
        manage_url=parent.manage_url, control_revision=parent.card_revision,
        holder_subject=parent.grantor_subject)


def _existing_binding(original: CardAuthority, *, project_ref: str,
                      decision: ProjectAuthorizationDecision) -> ControlCardBinding:
    binding = original.control_card
    locator = decision.project_control
    if (binding is None or binding.issuer_kind != "application" or binding.issuer_ref != project_ref
            or locator is None or locator.control_id != binding.control_id
            or locator.holder_subject != binding.holder_subject):
        raise _refuse("agent_plan_project_control_invalid")
    return binding


def _stamp(original: CardAuthority, candidate: CardAuthority, *, kind: str,
           actor_subject: str, project_ref: str, request_id: str, now: int,
           profile: str = "", resource: str = "") -> CardAuthority:
    provenance = dict(candidate.provenance or {})
    provenance[AUDIT_KEY] = {"schema": AUDIT_SCHEMA, "action": kind,
        "actor_subject": actor_subject, "project_ref": project_ref,
        "request_id": request_id, "occurred_at": now,
        "before_revision": original.card_revision, "after_revision": candidate.card_revision,
        "profile": profile, "resource": resource}
    return dataclasses.replace(candidate, provenance=provenance)


async def _profile_candidate(host: Any, *, original: CardAuthority, active: Any,
                             decision: ProjectAuthorizationDecision, profile: str,
                             resource: str) -> CardAuthority:
    if profile not in {"worker", "coordinator"} or not _text(resource):
        raise _refuse("agent_plan_profile_invalid")
    if active is None:
        raise _refuse("agent_plan_catalog_unavailable")
    config = await host._catalog_config(active, owner_subject=original.grantor_subject)
    grants = {key: list(values) for key, values in original.resource_grants.items()}
    operations = {key: list(values) for key, values in original.resource_operations.items()}
    changed = False
    for key in tuple(grants):
        row = host._configured_resource(key, config=config)
        if row is None or resource not in {key, getattr(row, "resource", "")}:
            continue
        declared = {str(item.name).lower(): item for item in (getattr(row, "authorization_profiles", ()) or ())}
        chosen = declared.get(profile)
        if chosen is None:
            raise _refuse("agent_plan_profile_not_declared")
        selection = requested_card_selection([chosen.scope], config=config, resource=key)
        selected_grants = dict(selection.get("resource_grants") or {})
        selected_operations = dict(selection.get("resource_operations") or {})
        if not selected_grants:
            raise _refuse("agent_plan_profile_empty")
        del grants[key]
        operations.pop(key, None)
        for selected_key, values in selected_grants.items():
            grants[selected_key] = list(values)
            operations[selected_key] = list(selected_operations.get(selected_key) or ())
        changed = True
    if not changed:
        raise _refuse("agent_plan_profile_resource_missing")
    resolved = await host._resolve_card_authority(user={"user_id": original.grantor_subject},
        existing=original, active=active, resource_grants=grants,
        resource_operations=operations, operations=(),
        named_service_operations=original.named_service_operations.to_stored(),
        account_scope=original.account_scope, properties=original.properties,
        _delegable_grants=decision.delegable_grants, _platform_admin=decision.platform_admin)
    if resolved.error is not None or resolved.revoke:
        raise _refuse(str((resolved.error or {}).get("error") or "agent_plan_profile_invalid"))
    # Import lazily: card_lifecycle_plan dispatches into this module.
    from .card_lifecycle_plan import _with_selection
    version = host._version_of(active)
    if not _text(version):
        raise _refuse("agent_plan_catalog_unavailable")
    acceptance = next_resource_acceptance(resources=resolved.resource_grants,
        row_for=lambda name: host._configured_resource(name, config=config),
        catalog_version=version, selected_operations=resolved.resource_operations,
        previous=original.resource_acceptance)
    selected = _with_selection(original, resolved=resolved, catalog_version=version,
        resource_row_for=lambda name: host._configured_resource(name, config=config))
    candidate = dataclasses.replace(selected, card_revision=original.card_revision + 1,
        catalog_version=version, resource_acceptance=preserve_descriptor_acceptance(
            original.resource_acceptance, acceptance))
    return candidate


async def build_agent_lifecycle_update(host: Any, *, original: CardAuthority,
        update: Mapping[str, Any], active: Any, decision: ProjectAuthorizationDecision,
        project_ref: str, actor_subject: str, request_id: str, now: int,
        parent: CardAuthority | None = None) -> dict[str, Any]:
    """Construct one exact existing-agent group member for PLAN dispatch.

    ``parent`` is the live project Control P resolved by the generic planner
    for attach. Its revision must also be held as a read by that planner.
    """
    kind = update.get("kind") if isinstance(update, Mapping) else None
    if (kind not in _FIELDS or set(update) != _FIELDS[kind]
            or not isinstance(original, CardAuthority)
            or not isinstance(decision, ProjectAuthorizationDecision)
            or not all(_text(value) for value in (project_ref, actor_subject, request_id))
            or type(now) is not int or now < 1):
        raise _refuse("agent_plan_update_invalid")
    if (not decision.allowed or decision.operation != AGENT_UPDATE_STEPS[kind]
            or decision.project_ref != project_ref or decision.actor_subject != actor_subject
            or decision.target_subject != update["target_subject"]
            or decision.request_id != request_id):
        raise _refuse("agent_plan_authorization_invalid")
    if (original.state != CARD_STATE_ACTIVE or original.card_kind not in {CARD_KIND_AGENT, CARD_KIND_AUTOMATION}
            or not original.delegate_subject.startswith("integration:")
            or original.delegate_subject == original.grantor_subject
            or original.source not in {"oauth", "agent"}
            or update["target_subject"] != original.delegate_subject
            or update["access_id"] != original.access_id
            or update["subject_hash"] != subject_hash_for(original.grantor_subject)
            or type(update["original_revision"]) is not int
            or update["original_revision"] != original.card_revision or original.card_revision < 1):
        raise _refuse("agent_plan_target_invalid")
    if kind == ATTACH_AGENT:
        if original.control_card is not None:
            raise _refuse("agent_plan_already_bound")
        binding = _project_parent(parent, project_ref=project_ref, decision=decision)
        candidate = dataclasses.replace(original, card_revision=original.card_revision + 1,
                                        control_card=binding)
        action = "attach"
    elif kind == DETACH_AGENT:
        _existing_binding(original, project_ref=project_ref, decision=decision)
        candidate = dataclasses.replace(original, card_revision=original.card_revision + 1,
                                        control_card=None)
        action = "detach"
    else:
        _existing_binding(original, project_ref=project_ref, decision=decision)
        candidate = await _profile_candidate(host, original=original, active=active, decision=decision,
            profile=update["profile"], resource=update["resource"])
        action = "update"
    candidate = _stamp(original, candidate, kind=kind, actor_subject=actor_subject,
        project_ref=project_ref, request_id=request_id, now=now,
        profile=update.get("profile", ""), resource=update.get("resource", ""))
    shape = candidate_shape_refusal(action, original.to_dict(), candidate.to_dict())
    if shape is not None:
        raise _refuse(shape)
    return {"member": group_member(original=original, candidate=candidate, action=action)}


__all__ = ["AGENT_ATTACH_OPERATION", "AGENT_DETACH_OPERATION", "AGENT_PROFILE_OPERATION",
    "AGENT_UPDATE_STEPS", "APPLY_AGENT_PROFILE", "ATTACH_AGENT", "DETACH_AGENT",
    "build_agent_lifecycle_update"]

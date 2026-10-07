# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Non-writing selection candidates for existing project-person Cards.

The project host has already authorized the lifecycle-plan step and loaded the
exact original revision. This module only constructs a Card candidate and a
public projection of the resulting group. Its output is never an authorization
or a publication decision.
"""

from __future__ import annotations

import copy
import dataclasses
from typing import Any, Mapping

from service_foundation.coordination.durable_decision_log import DecisionRefused

from .agent_capability_control import preserve_descriptor_acceptance
from .card_lifecycle_plan import _with_selection
from .caller_writer_gate import candidate_shape_refusal
from .cards.card_group import group_member
from .cards.model import CARD_STATE_ACTIVE, CardAuthority, authority_is_credentialless
from .catalog.descriptors import next_resource_acceptance
from .controls.project_person import (
    PROJECT_PERSON_CONTROL_ISSUER_KIND,
    PROJECT_PERSON_CONTROL_PROPERTY,
    ProjectPersonControlAudit,
    ProjectPersonControlError,
    ProjectPersonControlIdentity,
    bind_project_person_control,
    project_person_control_diff,
)
from .controls.snapshot import control_snapshot_refusal, reviewed_control_snapshot_properties
from .project_authorization import PROJECT_PERSON_CONTROL_UPDATE, ProjectAuthorizationDecision
from .project_identity_lifecycle import (
    PROJECT_PERSON_MY_CARD_ISSUER_KIND,
    ProjectIdentityLifecycleError,
    ProjectPersonCardIdentity,
)

_SELECTION_FIELDS = frozenset({
    "resource_grants", "resource_operations", "named_service_operations",
    "account_scope", "properties",
})


def _refuse(reason: str) -> DecisionRefused:
    return DecisionRefused(reason)


def _required(value: Any, reason: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise _refuse(reason)
    return value


def _target_identity(original: CardAuthority, project_ref: str, target_subject: str) -> tuple[str, Any]:
    if original.state != CARD_STATE_ACTIVE or original.card_revision < 1:
        raise _refuse("card_plan_update_scope_invalid")
    if original.issuer_kind == PROJECT_PERSON_CONTROL_ISSUER_KIND:
        try:
            identity = ProjectPersonControlIdentity.from_authority(original)
        except ProjectPersonControlError as exc:
            raise _refuse("card_plan_update_scope_invalid") from exc
        binding = original.control_card
        if (identity.project_ref != project_ref or identity.target_subject != target_subject
                or not authority_is_credentialless(original) or binding is None
                or binding.issuer_kind != "application" or binding.issuer_ref != project_ref):
            raise _refuse("card_plan_update_scope_invalid")
        return "person_control", identity
    if original.issuer_kind == PROJECT_PERSON_MY_CARD_ISSUER_KIND:
        try:
            identity = ProjectPersonCardIdentity.from_my_card(original)
        except ProjectIdentityLifecycleError as exc:
            raise _refuse("card_plan_update_scope_invalid") from exc
        binding = original.control_card
        if (identity.project_ref != project_ref or identity.person_subject != target_subject
                or binding is None or binding.holder_subject != identity.project_subject):
            raise _refuse("card_plan_update_scope_invalid")
        return "my_card", identity
    raise _refuse("card_plan_update_scope_invalid")


async def build_existing_card_selection_update(
    host: Any,
    *,
    original: CardAuthority,
    selection: Mapping[str, Any],
    active: Any,
    decision: ProjectAuthorizationDecision,
    project_ref: str,
    target_subject: str,
    actor_subject: str,
    request_id: str,
    now: int,
) -> dict[str, Any]:
    """Build one exact C/My update from a host-authorized selection, without a write."""
    scope = _required(project_ref, "card_plan_update_scope_invalid")
    target = _required(target_subject, "card_plan_update_scope_invalid")
    actor = _required(actor_subject, "card_plan_actor_invalid")
    _required(request_id, "card_plan_request_id_invalid")
    if type(now) is not int or now < 1:
        raise _refuse("card_plan_audit_time_invalid")
    if (not isinstance(original, CardAuthority) or not isinstance(selection, Mapping)
            or set(selection) - _SELECTION_FIELDS or not isinstance(decision, ProjectAuthorizationDecision)):
        raise _refuse("card_plan_selection_invalid")
    if (not decision.allowed or decision.project_ref != scope
            or decision.target_subject != target or decision.actor_subject != actor
            or decision.request_id != request_id or decision.operation != PROJECT_PERSON_CONTROL_UPDATE):
        raise _refuse("card_plan_authorization_invalid")
    kind, identity = _target_identity(original, scope, target)
    for field in ("resource_grants", "resource_operations", "account_scope", "properties"):
        if field in selection and not isinstance(selection[field], Mapping):
            raise _refuse("card_plan_selection_invalid")
    properties = copy.deepcopy(dict(selection["properties"] if "properties" in selection else original.properties or {}))
    if (kind == "person_control" and properties.get(PROJECT_PERSON_CONTROL_PROPERTY)
            != dict(original.properties or {}).get(PROJECT_PERSON_CONTROL_PROPERTY)):
        raise _refuse("card_plan_update_scope_invalid")
    resolved = await host._resolve_card_authority(
        user={"user_id": original.grantor_subject},
        # The resolver reads the authority fields shared by CardAuthority and
        # AutomationAccessRecord. Avoid loading credential handles to plan.
        existing=original,
        active=active,
        resource_grants=selection.get("resource_grants", original.resource_grants),
        resource_operations=selection.get("resource_operations", original.resource_operations),
        operations=(),
        named_service_operations=selection.get(
            "named_service_operations", original.named_service_operations.to_stored()
        ),
        account_scope=selection.get("account_scope", original.account_scope),
        properties=properties,
        _delegable_grants=decision.delegable_grants,
        _platform_admin=decision.platform_admin,
    )
    if resolved.error is not None:
        raise _refuse(str(resolved.error.get("error") or "card_plan_selection_invalid"))
    if resolved.revoke:
        raise _refuse("card_plan_selection_empty")
    catalog_version = _required(host._version_of(active), "card_plan_catalog_version_missing")
    catalog_config = await host._catalog_config(active, owner_subject=original.grantor_subject)
    computed_acceptance = next_resource_acceptance(
        resources=resolved.resource_grants,
        row_for=lambda resource: host._configured_resource(resource, config=catalog_config),
        catalog_version=catalog_version,
        selected_operations=resolved.resource_operations,
        previous=original.resource_acceptance,
    )
    selected = _with_selection(
        original, resolved=resolved, catalog_version=catalog_version,
        resource_row_for=lambda resource: host._configured_resource(resource, config=catalog_config),
    )
    candidate = dataclasses.replace(
        selected, card_revision=original.card_revision + 1, catalog_version=catalog_version,
        resource_acceptance=preserve_descriptor_acceptance(original.resource_acceptance, computed_acceptance),
    )
    before = original.to_dict()
    after = candidate.to_dict()
    after["card_revision"] = before["card_revision"]
    if after == before:
        raise _refuse("card_plan_selection_unchanged")
    if kind == "person_control":
        refusal = control_snapshot_refusal(candidate)
        if refusal is not None:
            raise _refuse(str(refusal["error"]))
        candidate = dataclasses.replace(candidate, properties=reviewed_control_snapshot_properties(
            candidate.properties, basis_catalog_version=catalog_version,
        ))
        candidate = bind_project_person_control(candidate, identity=identity)
        if not project_person_control_diff(original, candidate):
            raise _refuse("card_plan_selection_unchanged")
        audit = ProjectPersonControlAudit.build(
            action="updated", actor_subject=actor, identity=identity,
            request_id=request_id, occurred_at=now, before=original, after=candidate,
        )
        candidate = bind_project_person_control(candidate, identity=identity, audit=audit)
    shape_refusal = candidate_shape_refusal("update", original.to_dict(), candidate.to_dict())
    if shape_refusal is not None:
        raise _refuse(shape_refusal)
    return {"member": group_member(original=original, candidate=candidate, action="update")}


__all__ = ["build_existing_card_selection_update"]

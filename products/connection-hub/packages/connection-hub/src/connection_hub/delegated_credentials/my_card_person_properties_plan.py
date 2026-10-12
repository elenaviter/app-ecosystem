# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter
"""Closed non-secret person settings on an owner's existing project My Card.

This codec never reads credentials or writes Cards. Audit values come only
from authenticated STAGE evidence, not the browser or a planner wall clock.
"""
from __future__ import annotations

import copy
import dataclasses
import re
from datetime import datetime
from typing import Any, Mapping

from service_foundation.coordination.durable_decision_log import DecisionRefused

from .caller_writer_gate import candidate_shape_refusal
from .cards.card_group import group_member
from .cards.model import CARD_STATE_ACTIVE, CardAuthority
from .project_authorization import PROJECT_PERSON_CONTROL_UPDATE, ProjectAuthorizationDecision
from .project_identity_lifecycle import (
    MY_CARD_COMMIT_EMAIL_PROPERTY, MY_CARD_GITHUB_PROPERTY, MY_CARD_PERSON_PROPERTIES,
    ProjectIdentityLifecycleError, ProjectPersonCardIdentity,
)

SETTINGS_STEP = "set_person_properties"
_EMAIL = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")
_UPDATE_FIELDS = frozenset({"kind", "target_subject", "access_id", "subject_hash",
                            "original_revision", "selection"})


def _invalid() -> ValueError:
    return ValueError("project_person_my_card_property_invalid")


def _text(value: Any, *, empty: bool = False, limit: int = 256) -> bool:
    return (type(value) is str and (empty or bool(value)) and len(value) <= limit
            and value == value.strip() and (not value or value.isprintable()))


def settings_patch(changes: Any, *, internal_audit: bool = False) -> dict[str, Any]:
    """Exactly the two admitted keys; None clears. Strip only known internal audit fields.

    The signed wire uses the default, audit-free shape. The legacy entrypoint
    supplies its own audit fields; those are ignored and rebuilt by STAGE.
    Unknown fields (including credential-shaped values) always refuse.
    """
    if (not isinstance(changes, Mapping) or not changes
            or set(changes) - set(MY_CARD_PERSON_PROPERTIES)):
        raise _invalid()
    out = {}
    for key, value in sorted(changes.items()):
        if value is None:
            out[key] = None
            continue
        if not isinstance(value, Mapping):
            raise _invalid()
        required = {"provider_id", "account_id", "login"} if key == MY_CARD_GITHUB_PROPERTY else {"email"}
        audit = {"linked_at"} if key == MY_CARD_GITHUB_PROPERTY else {"set_by", "set_at"}
        if not required <= set(value) or set(value) - required - (audit if internal_audit else set()):
            raise _invalid()
        if key == MY_CARD_GITHUB_PROPERTY:
            if (not _text(value["provider_id"]) or not _text(value["account_id"])
                    or not _text(value["login"], empty=True)):
                raise _invalid()
        elif not _text(value["email"], limit=254) or not _EMAIL.fullmatch(value["email"]):
            raise _invalid()
        if internal_audit:
            for field in audit & set(value):
                if (field in {"linked_at", "set_at"} and (type(value[field]) is not int or value[field] < 1)
                        or field == "set_by" and not _text(value[field])):
                    raise _invalid()
        out[key] = {field: value[field] for field in sorted(required)}
    return out


def settings_selection(selection: Any) -> dict[str, Any]:
    if not isinstance(selection, Mapping) or set(selection) != {"properties"}:
        raise _invalid()
    return {"properties": settings_patch(selection["properties"])}


def settings_update_valid(update: Any) -> bool:
    try:
        return (isinstance(update, Mapping) and set(update) == _UPDATE_FIELDS
                and update["kind"] == SETTINGS_STEP and bool(settings_selection(update["selection"])))
    except ValueError:
        return False


def settings_display(properties: Any) -> dict[str, Any]:
    """Only the two exact reference/email values, with validated non-secret audits."""
    if not isinstance(properties, Mapping):
        raise _invalid()
    selected = {key: properties[key] for key in MY_CARD_PERSON_PROPERTIES if key in properties}
    if selected:
        settings_patch(selected, internal_audit=True)
    return copy.deepcopy(selected)


def whole_person_properties_patch(original: Mapping[str, Any], submitted: Any) -> dict[str, Any]:
    """A whole Card keeps every non-settings property; only the two owned settings may differ."""
    if not isinstance(submitted, Mapping):
        raise _invalid()
    keep = lambda values: {key: value for key, value in values.items() if key not in MY_CARD_PERSON_PROPERTIES}
    if keep(original) != keep(submitted):
        raise _invalid()
    patch = {}
    for key in MY_CARD_PERSON_PROPERTIES:
        before, after = original.get(key), submitted.get(key)
        if before == after:
            continue
        # Known audit fields in the returned Card are not authority from the
        # browser; STAGE rebuilds them under the signed request's own time.
        value = settings_patch({key: after}, internal_audit=True)[key]
        previous = settings_patch({key: before}, internal_audit=True)[key]
        if previous != value:
            patch[key] = value
        elif before != after:
            raise _invalid()  # changing only a Hub-written audit is not a settings edit
    return patch


def settings_only_change(original: CardAuthority, candidate: CardAuthority) -> bool:
    """All fields outside the two settings and revision must be byte-identical."""
    before, after = original.to_dict(), candidate.to_dict()
    if candidate.card_revision != original.card_revision + 1:
        return False
    after["card_revision"] = before["card_revision"]
    for value in (before, after):
        value["properties"] = {key: item for key, item in value["properties"].items()
                               if key not in MY_CARD_PERSON_PROPERTIES}
    return before == after


def build_person_settings_update(*, original: CardAuthority, selection: Mapping[str, Any],
                                 decision: ProjectAuthorizationDecision, project_ref: str,
                                 target_subject: str, actor_subject: str, request_id: str,
                                 request_digest: str) -> dict[str, Any]:
    """One revision-fenced, settings-only candidate; every other byte is retained."""
    try:
        patch = settings_selection(selection)["properties"]
        identity = ProjectPersonCardIdentity.from_my_card(original)
    except (ValueError, ProjectIdentityLifecycleError):
        raise DecisionRefused("card_plan_person_settings_invalid") from None
    binding = original.control_card
    if (original.state != CARD_STATE_ACTIVE or original.card_revision < 1
            or identity.project_ref != project_ref or identity.person_subject != target_subject
            or actor_subject != target_subject or original.grantor_subject != actor_subject
            or binding is None or binding.holder_subject != identity.project_subject
            or binding.control_id != identity.control_id):
        raise DecisionRefused("card_plan_person_settings_owner_required")
    if (not isinstance(decision, ProjectAuthorizationDecision) or not decision.allowed
            or decision.operation != PROJECT_PERSON_CONTROL_UPDATE or decision.project_ref != project_ref
            or decision.target_subject != target_subject or decision.actor_subject != actor_subject
            or decision.request_id != request_id or decision.request_digest != request_digest):
        raise DecisionRefused("card_plan_authorization_invalid")
    evidence = decision.evidence or {}
    if (evidence.get("source") != "card-version-stage"
            or evidence.get("request_digest") != request_digest):
        raise DecisionRefused("card_plan_person_settings_stage_required")
    try:
        at = datetime.fromisoformat(evidence["person_settings_at"])
        if at.utcoffset() is None:
            raise ValueError
        occurred_at = int(at.timestamp())
        if occurred_at < 1:
            raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError):
        raise DecisionRefused("card_plan_audit_time_invalid") from None
    properties = copy.deepcopy(dict(original.properties or {}))
    for key, value in patch.items():
        if value is None:
            properties.pop(key, None)
        else:
            audit = ({"linked_at": occurred_at} if key == MY_CARD_GITHUB_PROPERTY
                     else {"set_by": actor_subject, "set_at": occurred_at})
            properties[key] = {**value, **audit}
    candidate = dataclasses.replace(original, properties=properties, card_revision=original.card_revision + 1)
    refusal = candidate_shape_refusal("update", original.to_dict(), candidate.to_dict())
    if refusal is not None:
        raise DecisionRefused(refusal)
    # No re-resolution or selection/catalog/acceptance changes are permitted.
    if not settings_only_change(original, candidate):
        raise DecisionRefused("card_plan_undisplayed_change")
    return {"member": group_member(original=original, candidate=candidate, action="update")}

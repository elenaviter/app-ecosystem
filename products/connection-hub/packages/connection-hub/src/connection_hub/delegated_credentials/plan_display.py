# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Pure public display of an existing Card-group proposal and its originals."""

from __future__ import annotations

import copy
from typing import Any, Mapping

from service_foundation.coordination.durable_decision_log import DecisionRefused

from .cards.model import CardAuthority, CardRecordError
from .cards.store import subject_hash_for
from .controls.project_person import PROJECT_PERSON_CONTROL_ISSUER_KIND
from .project_identity_lifecycle import PROJECT_PERSON_MY_CARD_ISSUER_KIND

_DISPLAY_KINDS = {
    "application": "project_control",
    PROJECT_PERSON_CONTROL_ISSUER_KIND: "person_control",
    PROJECT_PERSON_MY_CARD_ISSUER_KIND: "my_card",
}


def _refuse(reason: str) -> DecisionRefused:
    return DecisionRefused(reason)


def _required(value: Any) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise _refuse("card_plan_display_invalid")
    return value


def _selection_value(card: Mapping[str, Any]) -> dict[str, Any]:
    """Only public, operator-relevant selection and its Control parent."""
    return {
        "resource_grants": copy.deepcopy(card["resource_grants"]),
        "resource_operations": copy.deepcopy(card["resource_operations"]),
        "named_service_operations": copy.deepcopy(card.get("named_service_operations")),
        "account_scope": copy.deepcopy(card["account_scope"]),
        "parent": copy.deepcopy(card.get("control_card")),
    }


def plan_display(
    candidate_value: Mapping[str, Any],
    originals: Mapping[tuple[str, str], Mapping[str, Any] | None],
) -> list[dict[str, Any]]:
    """Project a group with a real before image for every member.

    The group stores only candidates. A caller supplies each exact original
    loaded at the member revision, or None for an absent creation slot. A
    missing, stale or replaced original refuses rather than inventing a before
    image. Display adds no authority to the signed private proposal.
    """
    if not isinstance(candidate_value, Mapping) or not isinstance(originals, Mapping):
        raise _refuse("card_plan_display_invalid")
    cards = candidate_value.get("cards")
    if not isinstance(cards, (list, tuple)):
        raise _refuse("card_plan_display_invalid")
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for member in cards:
        if not isinstance(member, Mapping) or not isinstance(member.get("candidate"), Mapping):
            raise _refuse("card_plan_display_invalid")
        subject_hash = _required(member.get("subject_hash"))
        access_id = _required(member.get("access_id"))
        key = (subject_hash, access_id)
        if key in seen or key not in originals:
            raise _refuse("card_plan_display_original_missing")
        seen.add(key)
        before_revision = member.get("original_revision")
        if type(before_revision) is not int or before_revision < 0:
            raise _refuse("card_plan_display_original_revision_changed")
        try:
            candidate = CardAuthority.from_mapping(member["candidate"])
        except (CardRecordError, TypeError, ValueError) as exc:
            raise _refuse("card_plan_display_invalid") from exc
        if (candidate.access_id != access_id
                or subject_hash_for(candidate.grantor_subject) != subject_hash
                or candidate.card_revision != before_revision + 1):
            raise _refuse("card_plan_display_candidate_changed")
        kind = _DISPLAY_KINDS.get(candidate.issuer_kind)
        if kind is None:
            raise _refuse("card_plan_display_kind_invalid")
        original_raw = originals[key]
        if original_raw is None:
            if before_revision != 0 or member.get("original_absent") is not True:
                raise _refuse("card_plan_display_original_revision_changed")
            before = None
        else:
            if before_revision == 0 or member.get("original_absent") is not False:
                raise _refuse("card_plan_display_original_revision_changed")
            try:
                original = CardAuthority.from_mapping(original_raw)
            except (CardRecordError, TypeError, ValueError) as exc:
                raise _refuse("card_plan_display_original_invalid") from exc
            if (original.access_id != access_id
                    or subject_hash_for(original.grantor_subject) != subject_hash
                    or original.card_revision != before_revision):
                raise _refuse("card_plan_display_original_revision_changed")
            before = _selection_value(original.to_dict())
        entries.append({
            "access_id": access_id,
            "subject_hash": subject_hash,
            "kind": kind,
            "action": _required(member.get("action")),
            "original_revision": before_revision,
            "candidate_revision": candidate.card_revision,
            "before": before,
            "after": _selection_value(candidate.to_dict()),
        })
    return sorted(entries, key=lambda entry: (entry["subject_hash"], entry["access_id"]))


__all__ = ["plan_display"]

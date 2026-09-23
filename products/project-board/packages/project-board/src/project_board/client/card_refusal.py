# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""A Card operation refusal that says what to do about it (W262, 2026-09-23).

A worker Card keeps per-operation consent, closed by default, and its stored
operation list is written once, at consent. An operation declared later is
refused with ``work_worker_operation_not_granted`` even when the Card already
holds the permission group the operation falls under, and until tonight the
refusal named neither the group nor the way out: one relay log carried 1131
such refusals and nobody could act on any of them. The operator chose
re-approval over a grant fallback (the Card stays closed by default), so the
refusal has to carry three things wherever a person reads it: the operation,
the permission group it falls under, and the one command that fixes it, with
this worker's real profile.
"""

from __future__ import annotations

from typing import Any, Mapping

from project_board.contract.worker_operation_contract import (
    required_grants_for_operation,
)

# The app's reason when the Card is live and holds the resource, and only the
# operation is missing from the list written at consent.
CARD_LIST_PREDATES_OPERATION = "connection_hub_operation_not_granted"

REFUSAL_CODES = frozenset({"work_worker_operation_not_granted"})

PROFILE_PLACEHOLDER = "<profile>"


def replace_card_command(profile: str) -> str:
    """The one command that gives an existing Card a newly declared operation."""

    name = str(profile or "").strip() or PROFILE_PLACEHOLDER
    return f"pb worker authorize {name} --replace-card"


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(item).strip() for item in value if str(item or "").strip()]
    return []


def actionable_card_refusal(
    code: Any,
    details: Any,
    *,
    profile: str,
) -> dict[str, Any]:
    """The fields a refused Card operation needs before anyone can act on it.

    Empty for any other error. For ``work_worker_operation_not_granted`` with
    the Card-list reason (or no reason, from an older service) it names the
    operation, the permission group (from the refusal when the service sent
    ``required_grants``, else from this client's own operation policy), an
    operation the Card already holds under the same group when there is one,
    and the exact ``--replace-card`` command with the worker's profile.
    """

    if str(code or "").strip() not in REFUSAL_CODES:
        return {}
    detail_map = dict(details) if isinstance(details, Mapping) else {}
    reason = str(detail_map.get("reason") or "").strip()
    if reason and reason != CARD_LIST_PREDATES_OPERATION:
        return {}
    operation = str(detail_map.get("operation") or "").strip()
    if not operation:
        return {}
    group = _strings(detail_map.get("required_grants")) or sorted(
        required_grants_for_operation(operation)
    )
    group_set = set(group)
    held_via = sorted(
        granted
        for granted in _strings(detail_map.get("granted_operations"))
        if granted != operation
        and set(required_grants_for_operation(granted)) == group_set
    )
    if held_via:
        why = (
            "the Card holds this permission group (it grants "
            f"{held_via[0]} under it) but its operation list was written at "
            "consent and predates this operation"
        )
    else:
        why = (
            "the Card's operation list was written at consent and does not "
            "include this operation"
        )
    return {
        "operation": operation,
        "permission_group": group,
        "held_via": held_via,
        "why": why,
        "fix": replace_card_command(profile),
    }


def with_actionable_refusal(
    error: Mapping[str, Any],
    *,
    profile: str,
) -> dict[str, Any]:
    """An error payload with the actionable fields folded into its details.

    Used by the CLI on its way to the renderer, so ``ERROR
    work_worker_operation_not_granted`` prints ``fix = pb worker authorize
    <profile> --replace-card`` under ``details`` instead of only the list the
    Card holds.
    """

    payload = dict(error)
    extra = actionable_card_refusal(
        payload.get("code"), payload.get("details"), profile=profile
    )
    if not extra:
        return payload
    details = dict(payload.get("details") or {})
    for key in ("permission_group", "held_via", "why", "fix"):
        details[key] = extra[key]
    payload["details"] = details
    return payload


__all__ = [
    "CARD_LIST_PREDATES_OPERATION",
    "REFUSAL_CODES",
    "actionable_card_refusal",
    "replace_card_command",
    "with_actionable_refusal",
]

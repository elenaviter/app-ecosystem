# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W661: a role change's Card side as a STAGE update (``role_selection``), EMain 18:16Z.

PB decides roles; the Hub knows Cards. PB sends only a fixed policy delta for its
own resource (grants and operations to add, operations to remove, and for a
removal its declared operation-to-grant map) and no Card content. The Hub reads
the person's current C or My, applies the delta to that one resource, and builds
the candidate through the ordinary reselect builder, so catalog resolution, the
person Control's audit and its snapshot are exactly those of any selection edit.
The Hub learns nothing about roles or admins: it applies the sets it is given.
"""

from __future__ import annotations

from typing import Any, Mapping

from service_foundation.coordination.durable_decision_log import DecisionRefused

from .cards.model import CardAuthority

ROLE_KEYS = frozenset({"kind", "target_subject", "access_id", "subject_hash", "original_revision", "resource",
                       "add_grants", "add_operations", "remove_operations", "operation_grants"})
_MAX_ITEMS = 128  # a list's entries
_MAX_OPERATIONS = 256  # operation_grants: PB declares every operation it has (94 today)


def _names(value: Any) -> bool:
    return (type(value) is list and len(value) <= _MAX_ITEMS and len(set(value)) == len(value)
            and all(type(item) is str and 0 < len(item) <= 256 and item == item.strip() for item in value))


def role_update_shape_valid(raw: Mapping[str, Any]) -> bool:
    """The closed shape: one direction per update (add for a promotion, remove for a demotion)."""
    if set(raw) != ROLE_KEYS or raw.get("original_revision") != 0:
        return False
    if type(raw.get("resource")) is not str or not raw["resource"]:
        return False
    if not all(_names(raw.get(key)) for key in ("add_grants", "add_operations", "remove_operations")):
        return False
    adding, removing = bool(raw["add_grants"] or raw["add_operations"]), bool(raw["remove_operations"])
    grants = raw["operation_grants"]
    if adding == removing:
        return False
    if removing:
        return (isinstance(grants, Mapping) and len(grants) <= _MAX_OPERATIONS
                and all(type(op) is str and op and _names(values) for op, values in grants.items()))
    return grants is None


def role_selection(original: CardAuthority, update: Mapping[str, Any]) -> dict[str, Any] | None:
    """The full selection after the delta, or ``None`` when nothing would change (the Card stays a read)."""
    resource = update["resource"]
    grants = {key: sorted(values) for key, values in (original.resource_grants or {}).items()}
    operations = {key: sorted(values) for key, values in (original.resource_operations or {}).items()}
    before = (list(grants.get(resource, [])), list(operations.get(resource, [])))
    if update["remove_operations"]:
        kept = sorted(set(operations.get(resource, ())) - set(update["remove_operations"]))
        missing = [op for op in kept if op not in update["operation_grants"]]
        if missing:
            raise DecisionRefused("card_plan_role_operation_unknown")  # never guess a grant
        operations[resource] = kept
        grants[resource] = sorted({grant for op in kept for grant in update["operation_grants"][op]})
    else:
        grants[resource] = sorted(set(grants.get(resource, ())) | set(update["add_grants"]))
        operations[resource] = sorted(set(operations.get(resource, ())) | set(update["add_operations"]))
    if (grants[resource], operations[resource]) == before:
        return None
    value = original.to_dict()
    # Every other resource, the named services and the account scope stay as stored.
    return {"resource_grants": grants, "resource_operations": operations,
            "named_service_operations": value["named_service_operations"], "account_scope": value["account_scope"]}


__all__ = ["ROLE_KEYS", "role_selection", "role_update_shape_valid"]

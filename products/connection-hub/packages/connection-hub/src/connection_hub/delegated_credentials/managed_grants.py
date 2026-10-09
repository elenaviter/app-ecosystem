# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Application-managed grants: set by the owning application, never by a Card editor.

A catalog resource row may declare ``managed_grants``, a subset of its ``grants``.
Such a grant is assigned only through the application's own operations, which
write the Card themselves. A client Card create or update that would add or
remove one is refused with ``managed_grant_not_editable``. The Hub does not know
which application or which grant this is; it only compares what the catalog
declares with what the client sends.
"""

from __future__ import annotations

from typing import Any, Mapping

MANAGED_GRANT_NOT_EDITABLE = "managed_grant_not_editable"


def _grants(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value.strip()} if value.strip() else set()
    if isinstance(value, (list, tuple, set, frozenset)):
        return {str(item).strip() for item in value if str(item).strip()}
    return set()


def managed_grant_changes(config: Any, requested: Mapping[str, Any], existing: Mapping[str, Any]) -> list[str]:
    """Every ``resource:grant`` among the catalog's managed grants whose presence differs
    between the requested Card selection and the Card as it stands (``{}`` for a new Card)."""
    changes: list[str] = []
    requested = requested if isinstance(requested, Mapping) else {}
    existing = existing if isinstance(existing, Mapping) else {}
    for resource in sorted({str(key) for key in (*requested, *existing)}):
        row = config.resource_config(resource) if config is not None else None
        managed = _grants(getattr(row, "managed_grants", ())) if row is not None else set()
        if not managed:
            continue
        before = _grants(existing.get(resource)) & managed
        after = _grants(requested.get(resource)) & managed
        changes.extend(f"{resource}:{grant}" for grant in sorted(before ^ after))
    return changes


def managed_operation_changes(config: Any, requested: Mapping[str, Any], existing: Mapping[str, Any]) -> list[str]:
    """Every resource:operation the catalog marks person_card: false (the application decides it for
    a person) whose presence differs between the requested selection and the Card as it stands. Used for a
    person's Control Card only; other Cards keep the whole catalog editable."""
    changes: list[str] = []
    requested = requested if isinstance(requested, Mapping) else {}
    existing = existing if isinstance(existing, Mapping) else {}
    for resource in sorted({str(key) for key in (*requested, *existing)}):
        row = config.resource_config(resource) if config is not None else None
        managed = {getattr(tool, "name", "") for tool in (getattr(row, "tools", ()) or ())
                   if getattr(tool, "person_card", True) is False} if row is not None else set()
        managed.discard("")
        if not managed:
            continue
        before = _grants(existing.get(resource)) & managed
        after = _grants(requested.get(resource)) & managed
        changes.extend(f"{resource}:{operation}" for operation in sorted(before ^ after))
    return changes


def managed_grant_refusal(changes: list[str]) -> dict[str, Any]:
    return {
        "ok": False,
        "error": MANAGED_GRANT_NOT_EDITABLE,
        "grants": changes,
        "status": 409,
        "message": (
            "These permissions are managed by the application and are set only through its own "
            "operations, not by editing the Card."
        ),
    }


__all__ = ["MANAGED_GRANT_NOT_EDITABLE", "managed_grant_changes", "managed_grant_refusal", "managed_operation_changes"]

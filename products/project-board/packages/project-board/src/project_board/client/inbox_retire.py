"""Retire superseded board notices from a worker's own backlog, audited (W563).

A worker that was away can find hundreds of pending messages, and settling
each one meant receiving it, body and all. Most of that backlog is board
notices whose news a later notice or the item itself has already replaced.

Coordinator decision, 2026-10-05 21:19Z: retirement is never by age or by
kind alone, because an ``update`` written by a person or an agent can carry a
GO or a scope decision. Only a typed board notice is eligible, and only with
evidence that it is superseded: a newer notice with the same key in the same
mailbox, or the item's current state. Everything else stays pending with a
named reason. The dry run binds its selection to a digest; apply recomputes
it and settles nothing when it differs, so a change after review is never
retired unseen. Bodies are hashed here and never returned.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

SCHEMA = "problem-board.inbox-retire.v1"

# Board notices come from the control plane as ``update`` mail. Operator mail
# also arrives through the control plane, so the sender alone proves nothing:
# the kind, the absence of a control ref and an exact payload shape decide.
_BOARD_SENDER = "control-plane"
_NOTICE_KIND = "update"

# One payload shape per notice type, as services/control.py writes it
# (Applications problem-board@1-0). A payload with any other key set is not
# that type, and an unknown shape is never guessed.
_REVIEW_SITS_KEYS = frozenset({"identity_ref", "reason", "entered_at", "hold"})
_REVIEW_MOVED_KEYS = frozenset({"identity_ref", "reviewer", "previous_reviewer"})
_RESPONSIBILITY_KEYS = frozenset({"work_ref", "identity_ref", "assignee", "previous_assignee", "item_status"})
_TERMINAL_KEYS = frozenset({
    "notice_kind", "expected_reaction", "item_status", "item_assignee", "work_ref", "identity_ref",
})

# Distinct items read for evidence in one run. Beyond it the remaining
# notices stay pending ("evidence_budget"); a later run continues.
EVIDENCE_ITEM_BUDGET = 100

_TERMINAL_STATUSES = frozenset({"done", "cancelled"})


def notice_type(row: Mapping[str, Any]) -> tuple[str, str] | str:
    """``(type, key)`` for a typed board notice, else the reason it is not one."""

    if row.get("operator"):
        return "operator_mail"
    if str(row.get("sender") or "") != _BOARD_SENDER:
        return "not_a_board_notice"
    kind = str(row.get("kind") or "")
    if kind != _NOTICE_KIND:
        return f"kind_{kind or 'missing'}"
    payload = row.get("payload")
    if not isinstance(payload, Mapping) or "command_ref" in payload:
        return "untyped_notice"
    keys = frozenset(payload)
    if keys == frozenset({"coordinator_announcement"}) and isinstance(payload["coordinator_announcement"], Mapping):
        return "coordinator-announcement", str(row.get("project_ref") or "")
    identity = str(payload.get("identity_ref") or "")
    if not identity:
        return "untyped_notice"
    if keys == _REVIEW_SITS_KEYS:
        return "review-sits", identity
    if keys == _REVIEW_MOVED_KEYS:
        return "review-moved", identity
    if keys == _RESPONSIBILITY_KEYS:
        return "responsibility-changed", identity
    if keys == _TERMINAL_KEYS and payload.get("notice_kind") == "terminal_assignee_information":
        return "terminal-assignee-information", identity
    return "untyped_notice"


# Which newer notices replace an older one with the same key.
_SUPERSEDED_BY = {
    "coordinator-announcement": frozenset({"coordinator-announcement"}),
    "review-sits": frozenset({"review-sits", "review-moved"}),
    "review-moved": frozenset({"review-sits", "review-moved"}),
    "responsibility-changed": frozenset({"responsibility-changed", "terminal-assignee-information"}),
    "terminal-assignee-information": frozenset({"responsibility-changed", "terminal-assignee-information"}),
}


def _order(row: Mapping[str, Any], kind: str) -> tuple[int, str]:
    if kind == "coordinator-announcement":
        announcement = row["payload"]["coordinator_announcement"]
        try:
            revision = int(announcement.get("revision") or 0)
        except (TypeError, ValueError):
            revision = 0
        return revision, str(row.get("created_at") or "")
    return 0, str(row.get("created_at") or "")


def _state_evidence(kind: str, payload: Mapping[str, Any], item: Mapping[str, Any]) -> dict[str, Any] | None:
    """The item's current state when it shows the notice is superseded, else None."""

    status = str(item.get("status") or "").lower()
    evidence = {"item_revision": item.get("revision"), "status": status}
    if kind in {"review-sits", "review-moved"}:
        if status != "review":
            return evidence
        if kind == "review-moved" and str(item.get("reviewer") or "") != str(payload.get("reviewer") or ""):
            return {**evidence, "reviewer": str(item.get("reviewer") or "")}
        return None
    if kind == "responsibility-changed":
        if status in _TERMINAL_STATUSES or str(item.get("assignee") or "") != str(payload.get("assignee") or ""):
            return {**evidence, "assignee": str(item.get("assignee") or "")}
        return None
    if kind == "terminal-assignee-information":
        if (status != str(payload.get("item_status") or "").lower()
                or str(item.get("assignee") or "") != str(payload.get("item_assignee") or "")):
            return {**evidence, "assignee": str(item.get("assignee") or "")}
        return None
    return None


def plan_retirement(
    rows: Sequence[Mapping[str, Any]],
    *,
    read_item: Callable[[str, str], Mapping[str, Any] | None],
) -> dict[str, Any]:
    """Select superseded board notices; every other row is excluded with a reason.

    ``rows`` are this worker's own mail rows with ``message_ref``,
    ``project_ref``, ``kind``, ``sender``, ``created_at``, ``operator``,
    ``leased``, ``payload`` and ``content_hash``. ``read_item(project_ref,
    identity_ref)`` returns the item's current ``status``, ``assignee``,
    ``reviewer`` and ``revision``, or None when it cannot be read.
    """

    typed: list[tuple[Mapping[str, Any], str, str]] = []
    excluded: list[dict[str, str]] = []
    for row in rows:
        ref = str(row.get("message_ref") or "")
        if row.get("leased"):
            excluded.append({"message_ref": ref, "reason": "leased"})
            continue
        found = notice_type(row)
        if isinstance(found, str):
            excluded.append({"message_ref": ref, "reason": found})
            continue
        typed.append((row, *found))

    newest: dict[tuple[str, str, str], tuple[tuple[int, str], Mapping[str, Any], str]] = {}
    for row, kind, key in typed:
        for family in _SUPERSEDED_BY[kind]:
            slot = (str(row.get("project_ref") or ""), family, key)
            order = _order(row, kind)
            if slot not in newest or order > newest[slot][0]:
                newest[slot] = (order, row, kind)

    selected: list[dict[str, Any]] = []
    items: dict[tuple[str, str], Mapping[str, Any] | None] = {}
    for row, kind, key in typed:
        ref = str(row.get("message_ref") or "")
        project_ref = str(row.get("project_ref") or "")
        order = _order(row, kind)
        newer = [
            (entry_order, entry_row)
            for family in _SUPERSEDED_BY[kind]
            for entry_order, entry_row, entry_kind in [newest.get((project_ref, family, key), ((0, ""), {}, ""))]
            if entry_row and entry_kind in _SUPERSEDED_BY[kind] and entry_order > order
            and str(entry_row.get("message_ref") or "") != ref
        ]
        evidence: dict[str, Any] | None = None
        if newer:
            _, later = max(newer, key=lambda entry: entry[0])
            evidence = {"superseded_by": str(later.get("message_ref") or "")}
        elif kind != "coordinator-announcement":
            slot = (project_ref, key)
            if slot not in items:
                if len(items) >= EVIDENCE_ITEM_BUDGET:
                    excluded.append({"message_ref": ref, "reason": "evidence_budget"})
                    continue
                try:
                    items[slot] = read_item(project_ref, key)
                except Exception:  # noqa: BLE001 - any read failure keeps the mail pending
                    items[slot] = None
            item = items[slot]
            if item is None:
                excluded.append({"message_ref": ref, "reason": "evidence_unavailable"})
                continue
            evidence = _state_evidence(kind, row["payload"], item)
        if evidence is None:
            excluded.append({"message_ref": ref, "reason": "still_current"})
            continue
        selected.append({
            "message_ref": ref,
            "project_ref": project_ref,
            "type": kind,
            "key": key,
            "content_hash": str(row.get("content_hash") or ""),
            "evidence": evidence,
        })

    selected.sort(key=lambda entry: entry["message_ref"])
    excluded.sort(key=lambda entry: (entry["reason"], entry["message_ref"]))
    return {
        "schema": SCHEMA,
        "selected": selected,
        "excluded": excluded,
        "digest": selection_digest(selected),
    }


def selection_digest(selected: Sequence[Mapping[str, Any]]) -> str:
    """sha256 over the exact selection: refs, content hashes and evidence."""

    canonical = json.dumps(
        [
            {key: entry[key] for key in ("message_ref", "content_hash", "type", "key", "evidence")}
            for entry in sorted(selected, key=lambda entry: entry["message_ref"])
        ],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def content_hash(row: Mapping[str, Any]) -> str:
    """The message as delivered: ref, kind, sender, subject, payload and body."""

    canonical = json.dumps(
        {key: row.get(key) for key in ("message_ref", "kind", "sender", "subject", "payload", "body")},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def settlement_summary(entry: Mapping[str, Any], retirement_id: str) -> str:
    evidence = entry["evidence"]
    if "superseded_by" in evidence:
        reason = f"superseded by {evidence['superseded_by']}"
    else:
        reason = "item now " + ", ".join(f"{key} {value}" for key, value in sorted(evidence.items()))
    return f"Retired unread: {entry['type']} notice {reason}; retirement {retirement_id}."[:1000]

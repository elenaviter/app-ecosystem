from __future__ import annotations

from typing import Any, Mapping


NOTE_APPEND_KIND = "plan.note.append"
NOTE_VIEW_KIND = "plan.notes.view"
WORK_ACCEPT_KIND = "plan.item.accept"
WORK_CANCEL_KIND = "plan.item.cancel"
WORK_RETAG_KIND = "plan.item.retag"
PLAN_HOST_CONTROL_KINDS = frozenset(
    {NOTE_APPEND_KIND, NOTE_VIEW_KIND, WORK_ACCEPT_KIND, WORK_CANCEL_KIND, WORK_RETAG_KIND}
)
# How a person labels work is their own vocabulary, so the only limits are the
# ones that keep the label a label.
MAX_LABELS_PER_ITEM = 24
MAX_LABEL_LENGTH = 64
MAX_NOTE_PAGE = 50
NOTE_VIEW_TTL_SECONDS = 15 * 60


def command_receipt(row: Mapping[str, Any]) -> dict[str, Any]:
    raw_state = str(row.get("state") or "pending")
    if raw_state == "acknowledged":
        state = "applied"
    elif raw_state in {"pending", "leased"}:
        state = "queued"
    else:
        state = "refused"
    result = (
        dict(row.get("result_payload") or {})
        if isinstance(row.get("result_payload"), Mapping)
        else {}
    )
    payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
    expected_revision = result.get(
        "expected_revision", payload.get("expected_revision")
    )
    return {
        "command_ref": str(row.get("command_ref") or ""),
        "state": state,
        "operation": str(result.get("operation") or row.get("kind") or ""),
        "project_ref": str(row.get("project_ref") or ""),
        "work_ref": str(row.get("work_ref") or ""),
        "expected_revision": expected_revision,
        "observed_revision": result.get("observed_revision"),
        "result_ref": str(result.get("result_ref") or row.get("result_ref") or ""),
        "summary": str(result.get("summary") or row.get("result_summary") or ""),
        "error": dict(result.get("error") or {})
        if isinstance(result.get("error"), Mapping)
        else {},
        "created_at": str(row.get("created_at") or ""),
        "updated_at": str(row.get("updated_at") or ""),
        "replayed": bool(row.get("replayed")),
    }


__all__ = [
    "MAX_NOTE_PAGE",
    "NOTE_APPEND_KIND",
    "NOTE_VIEW_KIND",
    "NOTE_VIEW_TTL_SECONDS",
    "PLAN_HOST_CONTROL_KINDS",
    "WORK_ACCEPT_KIND",
    "WORK_CANCEL_KIND",
    "command_receipt",
]

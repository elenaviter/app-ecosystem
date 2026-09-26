from __future__ import annotations

import time
from typing import Any, Mapping, Sequence

from ..contract.errors import DomainError


DEFAULT_OUTBOX_WAIT_SECONDS = 120.0
OUTBOX_TERMINAL_STATES = frozenset({"sent", "refused", "ignored"})


def await_outbox_outcome(
    field: Any,
    outbox_id: str,
    *,
    wait_seconds: float,
) -> dict[str, Any] | None:
    """Return once the relay records a terminal row, or at the deadline."""

    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    while True:
        row = field.read_outbox_record(outbox_id)
        if row is not None and str(row.get("state") or "") in OUTBOX_TERMINAL_STATES:
            return row
        if time.monotonic() >= deadline:
            return row
        time.sleep(0.5)


def assignment_report_outcome(
    *,
    queued: Mapping[str, Any],
    row: Mapping[str, Any] | None,
    status_command: Sequence[str],
) -> dict[str, Any]:
    """Interpret the relay's answer without treating a queued intent as success."""

    outbox_state = str((row or {}).get("state") or "unknown")
    remote_disposition = str((row or {}).get("remote_disposition") or "")
    base = {
        "outbox_id": str(queued.get("outbox_id") or ""),
        "assignment_ref": str(queued.get("assignment_ref") or ""),
        "ownership_version": int(queued.get("ownership_version") or 0),
        "report_state": str(queued.get("state") or ""),
        "requested_source_event_ref": str(queued.get("source_event_ref") or ""),
        "replayed": bool(queued.get("replayed")),
    }
    if outbox_state == "sent":
        return {
            **dict(queued),
            "outbox_state": "sent",
            "delivery_status": "accepted",
            "remote_disposition": remote_disposition or "accepted",
            "settled_at": str((row or {}).get("settled_at") or ""),
            "outcome_confirmed": True,
            "applied": True,
        }

    if outbox_state in {"refused", "ignored"}:
        remote = (row or {}).get("remote_result")
        error = (
            dict(remote.get("error"))
            if isinstance(remote, Mapping)
            and isinstance(remote.get("error"), Mapping)
            else {}
        )
        code = str(error.get("code") or "").strip()
        if not code and remote_disposition:
            code = remote_disposition.split(maxsplit=1)[0]
        code = code or "field_assignment_report_refused"
        message = str(error.get("message") or "").strip() or (
            "The service refused this assignment report."
        )
        details = (
            dict(error.get("details"))
            if isinstance(error.get("details"), Mapping)
            else {}
        )
        details.setdefault(
            "source_event_ref", str(queued.get("source_event_ref") or "")
        )
        details.update(
            {
                **base,
                "outbox_state": outbox_state,
                "delivery_status": "refused",
                "remote_disposition": remote_disposition,
                "outcome_confirmed": True,
                "applied": False,
            }
        )
        raise DomainError(code, message, status=409, details=details)

    details: dict[str, Any] = {
        **base,
        "outbox_state": outbox_state,
        "delivery_status": "outcome_unknown",
        "outcome_confirmed": False,
        "may_have_applied": True,
        "source_event_ref": str(queued.get("source_event_ref") or ""),
        "required_action": (
            "Read this exact outbox result. If it remains unknown, retry the "
            "same report unchanged so its idempotency identity is preserved."
        ),
    }
    if status_command:
        details["status_command"] = list(status_command)
    raise DomainError(
        "field_assignment_report_outcome_unknown",
        (
            "The assignment report outcome is unknown. The relay may have "
            "timed out after the service applied it."
        ),
        status=504,
        details=details,
    )


def submit_assignment_report(
    field: Any,
    project_id: str,
    *,
    worker_name: str,
    assignment_ref: str,
    ownership_version: int,
    state: str,
    summary: str,
    result_ref: str,
    source_event_ref: str,
    review_look_at: str | None,
    review_could_not_verify: str | None,
    wait_seconds: float,
    review_reviewer: str | None = None,
    review_merged: str | None = None,
    review_deploy: str | None = None,
    review_nothing_to_deploy: bool = False,
    scope: str = "",
    status_command_prefix: Sequence[str] = (),
) -> dict[str, Any]:
    """Queue one immutable assignment report and read its authoritative result."""

    queued = field.enqueue_assignment_report(
        project_id,
        worker_name=worker_name,
        assignment_ref=assignment_ref,
        ownership_version=ownership_version,
        state=state,
        summary=summary,
        result_ref=result_ref,
        source_event_ref=source_event_ref,
        review_look_at=review_look_at,
        review_could_not_verify=review_could_not_verify,
        review_reviewer=review_reviewer,
        review_merged=review_merged,
        review_deploy=review_deploy,
        review_nothing_to_deploy=review_nothing_to_deploy,
        scope=scope,
    )
    outbox_id = str(queued.get("outbox_id") or "")
    row = await_outbox_outcome(
        field,
        outbox_id,
        wait_seconds=wait_seconds,
    )
    status_command = (
        [*status_command_prefix, "--outbox-id", outbox_id]
        if status_command_prefix
        else []
    )
    return assignment_report_outcome(
        queued=queued,
        row=row,
        status_command=status_command,
    )


__all__ = [
    "DEFAULT_OUTBOX_WAIT_SECONDS",
    "OUTBOX_TERMINAL_STATES",
    "assignment_report_outcome",
    "await_outbox_outcome",
    "submit_assignment_report",
]

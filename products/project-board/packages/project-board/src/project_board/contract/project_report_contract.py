"""What a project status report is, for the machine that submits one and the
service that stores it.

One module holds both halves on purpose. On 2026-09-16 the client was simplified
to send the author's text, its files, and the work it points at, and the server
kept requiring twelve fields. Every report was refused for two days, and nothing
said so, because the two halves lived in two files that nothing compared.

The split is by where the facts are:

    submission   what only the author has: the summary, the work it points at,
                 the files kept with it, and anything the author could not see
    document     everything else, composed by the service from rows it owns

Work items live in PostgreSQL, so the service is the machine that holds the
history. A report is a delta: the items whose status moved since the predecessor
report, newest move first, capped. It is never a census, and no path that builds
one reads a plan, whatever the item count.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from .errors import DomainError

REPORT_SUBMISSION_SCHEMA = "problem-board.project-report-submission.v1"
REPORT_DOCUMENT_SCHEMA = "problem-board.project-report.v2"

# The cap is part of the contract and belongs to the service: it bounds a SQL
# LIMIT and the stored document. A cap the caller could raise would let a caller
# ask for a census again. Operator ruling, 2026-09-17: "capped to 20 max".
MAX_REPORT_CHANGED_ROWS = 20
MAX_REPORT_NOT_SEEN_ROWS = 50
MAX_REPORT_WORK_REFS = 50
MAX_REPORT_ATTACHMENTS = 20
MAX_REPORT_SUMMARY_BYTES = 64 * 1024

SUBMISSION_FIELDS = ("summary", "work_refs", "attachments", "not_seen")
REQUIRED_SUBMISSION_FIELDS = ("summary",)

# The pre-2026-09-17 client sent a whole document. These names are accepted and
# dropped so a relay with such a row still in its outbox can deliver it. Their
# values are never used: the service owns every one of them.
LEGACY_DOCUMENT_FIELDS = frozenset(
    {
        "schema",
        "report_ref",
        "project_ref",
        "project_title",
        "requested_by",
        "created_at",
        "created_by",
        "created_by_alias",
        "since_report_ref",
        "counts",
        "changed",
        "blocked",
        "stalled",
        "awaiting_operator",
        "item_total",
    }
)

_PRIVATE_KEY_SUFFIXES = ("_token", "_secret", "_credential", "_password")
_WORK_KEY = re.compile(r"^W\d{1,9}$", re.IGNORECASE)


def _invalid(field: str, message: str, **details: Any) -> DomainError:
    return DomainError(
        "work_project_report_submission_invalid",
        message,
        details={"field": field, **details},
    )


def _bounded_text(value: Any, *, field: str, maximum: int, required: bool = False) -> str:
    text = "" if value is None else str(value)
    if required and not text.strip():
        raise _invalid(field, f"{field} is required.")
    if len(text.encode("utf-8")) > maximum:
        raise _invalid(field, f"{field} is too large.", maximum_bytes=maximum)
    return text


def _sequence(value: Any, *, field: str, maximum: int) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise _invalid(field, f"{field} must be an array.")
    if len(value) > maximum:
        raise _invalid(field, f"{field} contains too many entries.", maximum_rows=maximum)
    return list(value)


def work_refs_in(text: str) -> list[str]:
    """The work items a summary names, in the order the author named them."""

    seen: list[str] = []
    for match in re.finditer(r"\bW\d+\b", text or ""):
        key = match.group(0).upper()
        if key not in seen:
            seen.append(key)
    return seen[:MAX_REPORT_WORK_REFS]


def validate_submission(value: Any) -> dict[str, Any]:
    """Normalize one submission, or refuse it naming the field.

    The client calls this before it queues anything and the service calls it on
    arrival, so a submission the service would refuse cannot be queued in the
    first place.
    """

    if not isinstance(value, Mapping):
        raise _invalid("document", "The report submission must be an object.")
    unknown = sorted(
        str(key)
        for key in value
        if key not in SUBMISSION_FIELDS and key not in LEGACY_DOCUMENT_FIELDS
    )
    if unknown:
        raise _invalid(
            "document",
            "The report submission carries fields the contract does not define.",
            unknown=unknown,
            accepted=list(SUBMISSION_FIELDS),
        )
    missing = [name for name in REQUIRED_SUBMISSION_FIELDS if name not in value]
    if missing:
        raise DomainError(
            "work_project_report_incomplete",
            "The report submission is missing required fields.",
            details={"missing": missing, "accepted": list(SUBMISSION_FIELDS)},
        )
    summary = _bounded_text(
        value.get("summary"),
        field="summary",
        maximum=MAX_REPORT_SUMMARY_BYTES,
        required=True,
    )
    work_refs: list[str] = []
    for index, item in enumerate(
        _sequence(value.get("work_refs"), field="work_refs", maximum=MAX_REPORT_WORK_REFS)
    ):
        ref = _bounded_text(item, field=f"work_refs[{index}]", maximum=512).strip()
        if not ref:
            continue
        if not (_WORK_KEY.match(ref) or ref.startswith("work:plan:node:")):
            raise _invalid(
                f"work_refs[{index}]",
                "A work ref is an item key such as W12 or a work:plan:node reference.",
            )
        ref = ref.upper() if _WORK_KEY.match(ref) else ref
        if ref not in work_refs:
            work_refs.append(ref)
    attachments: list[dict[str, Any]] = []
    for index, item in enumerate(
        _sequence(
            value.get("attachments"), field="attachments", maximum=MAX_REPORT_ATTACHMENTS
        )
    ):
        if not isinstance(item, Mapping):
            raise _invalid(f"attachments[{index}]", "Every attachment is an object.")
        attachments.append(
            {
                "filename": _bounded_text(
                    item.get("filename"),
                    field=f"attachments[{index}].filename",
                    maximum=512,
                    required=True,
                ),
                "mime": _bounded_text(
                    item.get("mime") or "application/octet-stream",
                    field=f"attachments[{index}].mime",
                    maximum=255,
                ),
                "size": max(0, int(item.get("size") or 0)),
                "sha256": _bounded_text(
                    item.get("sha256"), field=f"attachments[{index}].sha256", maximum=128
                ),
            }
        )
    return {
        "schema": REPORT_SUBMISSION_SCHEMA,
        "summary": summary,
        "work_refs": work_refs,
        "attachments": attachments,
        "not_seen": _not_seen_rows(value.get("not_seen")),
    }


def build_submission(
    *,
    summary: str,
    work_refs: Sequence[str] | None = None,
    attachments: Sequence[Mapping[str, Any]] = (),
    not_seen: Sequence[Any] = (),
) -> dict[str, Any]:
    """What the reporting machine sends. Validated here, before anything queues."""

    return validate_submission(
        {
            "summary": summary,
            "work_refs": list(work_refs) if work_refs is not None else work_refs_in(summary),
            "attachments": [dict(item) for item in attachments],
            "not_seen": list(not_seen),
        }
    )


def _not_seen_rows(value: Any) -> list[dict[str, str]]:
    """What the author could not see. `stated_by` is set here and never taken
    from the caller, so an author's row cannot pose as the service's."""

    rows: list[dict[str, str]] = []
    for index, item in enumerate(
        _sequence(value, field="not_seen", maximum=MAX_REPORT_NOT_SEEN_ROWS)
    ):
        if isinstance(item, Mapping):
            row: dict[str, str] = {}
            for key, part in item.items():
                name = _bounded_text(
                    key, field=f"not_seen[{index}].key", maximum=128, required=True
                )
                if name.lower().endswith(_PRIVATE_KEY_SUFFIXES):
                    raise DomainError(
                        "work_project_report_contains_private_data",
                        "The report contains a field reserved for private machine data.",
                        details={"field": f"not_seen[{index}].{name}"},
                    )
                row[name] = _bounded_text(
                    part, field=f"not_seen[{index}].{name}", maximum=2000
                )
        else:
            text = _bounded_text(item, field=f"not_seen[{index}]", maximum=2000).strip()
            if not text:
                continue
            row = {"what": text}
        row["stated_by"] = "author"
        rows.append(row)
    return rows


def _natural_key(item_key: str) -> tuple[str, int]:
    match = re.match(r"^([A-Za-z]*)(\d+)$", item_key)
    return (match.group(1).upper(), int(match.group(2))) if match else (item_key.upper(), 0)


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value or "")


REASON_MOVED = "moved"
REASON_BLOCKED = "blocked"
REASON_CANCELLED_DEPENDENCY = "cancelled_dependency"
REASON_MENTIONED = "mentioned"
REASON_DEPENDENCY_OF_MOVED = "dependency_of_moved"


def delta_sections(delta: Mapping[str, Any]) -> dict[str, Any]:
    """The report's work sections, from one `project_report_delta` result.

    One SQL statement selected these rows and said why each is there. Nothing
    is looked up again here: `attention` is that list as it came, newest first,
    and `changed` and `blocked` are the same rows read by reason, in the shape
    the board already renders.
    """

    attention: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for item in delta.get("items") or []:
        reasons = [str(reason) for reason in item.get("reasons") or []]
        key = str(item.get("item_key") or "")
        title = str(item.get("title") or "")
        status = str(item.get("status") or "")
        previous = str(item.get("previous_status") or "")
        of_items = [str(value) for value in item.get("of_items") or [] if str(value)]
        row: dict[str, Any] = {
            "item": key,
            "title": title,
            "status": status,
            "reasons": reasons,
            "at": _iso(item.get("at")),
            "work_ref": str(item.get("item_ref") or ""),
        }
        if str(item.get("assignee") or ""):
            row["assignee"] = str(item.get("assignee"))
        if of_items:
            row["of_items"] = of_items
        if status == "cancelled":
            # Cancelled work is its own outcome and must not read as finished.
            row["cancelled"] = True
        if REASON_MOVED in reasons:
            move = {
                "item": key,
                "title": title,
                # No previous status: the item entered the plan inside the window.
                "from": previous or "new",
                "to": status,
                "moved_at": _iso(item.get("status_changed_at")),
            }
            if status == "cancelled":
                move["cancelled"] = True
            changed.append(move)
            row["from"] = move["from"]
        if REASON_BLOCKED in reasons:
            reason_text = str(item.get("detail") or "").strip() or (
                "The assignee reported blocked and gave no reason."
            )
            blocked.append(
                {
                    "item": key,
                    "title": title,
                    "reason": reason_text,
                    "worker": str(item.get("detail_worker") or ""),
                }
            )
            row["blocked_reason"] = reason_text
        if REASON_CANCELLED_DEPENDENCY in reasons:
            reason_text = "Depends on cancelled work: " + ", ".join(of_items or ["unknown"])
            blocked.append({"item": key, "title": title, "reason": reason_text, "worker": ""})
            row.setdefault("blocked_reason", reason_text)
        attention.append(row)
    # `attention` keeps the statement's order, where an item the author named or
    # a fresh blocked report can lead. `changed` answers a narrower question,
    # what moved, so it is ordered by the move itself, most recent first.
    # Two stable passes: item key breaks a tie, the move time decides.
    changed.sort(key=lambda move: _natural_key(str(move.get("item") or "")))
    changed.sort(key=lambda move: str(move.get("moved_at") or ""), reverse=True)
    moved_total = delta.get("moved_total")
    return {
        "attention": attention,
        "changed": changed,
        "blocked": blocked,
        "delta_window": {
            "order": "most_recent_first",
            "cap": int(delta.get("limit") or MAX_REPORT_CHANGED_ROWS),
            "shown": len(attention),
            "more": bool(delta.get("more")),
            "moved_total": int(moved_total) if moved_total is not None else None,
            # `from` is the status before the item's latest move. An item that
            # moved twice inside one window shows its last hop.
            "from_means": "status_before_latest_move",
            "reasons": [
                REASON_MOVED,
                REASON_BLOCKED,
                REASON_CANCELLED_DEPENDENCY,
                REASON_MENTIONED,
                REASON_DEPENDENCY_OF_MOVED,
            ],
        },
    }


def unreachable_workers(team: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Attending workers the service could not vouch for when it composed."""

    rows: list[dict[str, str]] = []
    for worker in team:
        if str(worker.get("pool_status") or "") != "active":
            continue
        availability = str(worker.get("availability") or "")
        heartbeat = _iso(worker.get("heartbeat_at"))
        if availability in {"", "available"} and heartbeat:
            continue
        rows.append(
            {
                "stated_by": "service",
                "worker": str(worker.get("worker_alias") or worker.get("worker_name") or ""),
                "availability": availability or "unknown",
                "last_heartbeat_at": heartbeat,
            }
        )
    return rows[:MAX_REPORT_NOT_SEEN_ROWS]


def compose_document(
    *,
    request: Mapping[str, Any],
    worker: Mapping[str, Any],
    submission: Mapping[str, Any],
    attachments: Sequence[Mapping[str, Any]],
    counts: Mapping[str, int],
    delta: Mapping[str, Any],
    team: Sequence[Mapping[str, Any]],
    created_at: str,
) -> dict[str, Any]:
    """The immutable document. Pure: every row it needs is handed in."""

    clean_counts = {str(key).lower(): int(value) for key, value in counts.items()}
    clean_counts.setdefault("cancelled", 0)
    document: dict[str, Any] = {
        "schema": REPORT_DOCUMENT_SCHEMA,
        "report_ref": str(request.get("report_ref") or ""),
        "project_ref": str(request.get("project_ref") or ""),
        "requested_by": {
            "principal_key": str(request.get("requester_principal_key") or ""),
            "user_id": str(request.get("requested_by_user_id") or ""),
            "label": str(request.get("requested_by_label") or ""),
        },
        "ask": str(request.get("ask") or ""),
        "created_at": created_at,
        "created_by": {
            "worker_name": str(worker.get("worker_name") or ""),
            "worker_alias": str(worker.get("worker_alias") or ""),
        },
        # Selected when the request was created, and read back from that row.
        "since_report_ref": str(request.get("since_report_ref") or ""),
        "since_published_at": str(request.get("since_published_at") or ""),
        "counts": dict(sorted(clean_counts.items())),
    }
    document.update(delta_sections(delta))
    document["not_seen"] = (
        unreachable_workers(team) + list(submission.get("not_seen") or [])
    )[:MAX_REPORT_NOT_SEEN_ROWS]
    document["summary"] = str(submission.get("summary") or "")
    document["work_refs"] = list(submission.get("work_refs") or [])
    document["attachments"] = [
        {
            "filename": str(item.get("filename") or ""),
            "mime": str(item.get("mime") or "application/octet-stream"),
            "size": int(item.get("size") or 0),
            "file_ref": str(item.get("file_ref") or ""),
        }
        for item in attachments
    ]
    return document


def same_submission(stored: Mapping[str, Any], submission: Mapping[str, Any]) -> bool:
    """Whether a retry carries what the stored report was published from.

    Only the author's fields are compared. The composed sections are a function
    of when they were computed, so comparing them would refuse every honest
    retry of a report that already published.
    """

    author_not_seen = [
        row
        for row in stored.get("not_seen") or []
        if isinstance(row, Mapping) and row.get("stated_by") == "author"
    ]
    stated_files = sorted(
        str(item.get("filename") or "") for item in submission.get("attachments") or []
    )
    stored_files = sorted(
        str(item.get("filename") or "") for item in stored.get("attachments") or []
    )
    return (
        str(stored.get("summary") or "") == str(submission.get("summary") or "")
        and list(stored.get("work_refs") or []) == list(submission.get("work_refs") or [])
        and author_not_seen == list(submission.get("not_seen") or [])
        # Files travel as staged refs beside the submission. A retry that lists
        # its files must list the same ones; one that lists none states nothing.
        and (not stated_files or stated_files == stored_files)
    )


__all__ = [
    "LEGACY_DOCUMENT_FIELDS",
    "MAX_REPORT_CHANGED_ROWS",
    "MAX_REPORT_SUMMARY_BYTES",
    "REPORT_DOCUMENT_SCHEMA",
    "REPORT_SUBMISSION_SCHEMA",
    "REQUIRED_SUBMISSION_FIELDS",
    "SUBMISSION_FIELDS",
    "build_submission",
    "compose_document",
    "delta_sections",
    "same_submission",
    "unreachable_workers",
    "validate_submission",
    "work_refs_in",
]

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from ..contract.errors import DomainError
from ..contract.refs import parse_ref
from ..contract.scoped_collection import CollectionError, ScopedKeysetCursor
from .io import atomic_write_json, bounded_text, component, exclusive_lock, read_json, utc_now


QUARANTINE_PAGE_SCHEMA = "problem-board.worker-quarantine.v1"


def _scopes(field: Any, worker_name: str) -> list[tuple[str, str, Path]]:
    clean_worker = component(worker_name, field="worker_name").lower()
    scopes = [("@direct", "", field._mail_root("", clean_worker))]
    projects = field.control / "projects"
    if projects.is_dir():
        scopes.extend(
            (f"project:{path.name}", path.name, field._mail_root(path.name, clean_worker))
            for path in sorted(projects.iterdir())
            if path.is_dir() and (path / "mail" / clean_worker / "quarantine").is_dir()
        )
    return scopes


def quarantine_summary(field: Any, worker_name: str) -> dict[str, Any]:
    count = sum(
        1
        for _, _, root in _scopes(field, worker_name)
        for _ in (root / "quarantine").glob("*.json")
    )
    return {
        "count": count,
        "list_command": ["pb", "worker", "quarantine", "list"],
    }


def _reason(row: Mapping[str, Any]) -> dict[str, Any]:
    failure = row.get("receive_failure")
    failure = failure if isinstance(failure, Mapping) else {}
    error = failure.get("error")
    error = error if isinstance(error, Mapping) else {}
    recorded = row.get("quarantine_reason")
    if isinstance(recorded, Mapping):
        return dict(recorded)
    return {
        "code": str(error.get("code") or "unknown"),
        "message": str(error.get("message") or "Receive transformation failed."),
        "attempts": int(failure.get("attempts") or 0),
        "details": dict(error.get("details") or {}),
    }


def list_quarantine(
    field: Any, worker_name: str, *, cursor: str = "", limit: int = 20
) -> dict[str, Any]:
    clean_worker = component(worker_name, field="worker_name").lower()
    codec = ScopedKeysetCursor(
        scope={"worker_name": clean_worker},
        query={"collection": "quarantined_mail"},
        key_fields=("mailbox", "message_ref"),
    )
    boundary = None
    if cursor:
        try:
            boundary = codec.decode(bounded_text(cursor, field="cursor", maximum=4000))
        except CollectionError as exc:
            raise DomainError(exc.code, exc.message) from exc
    rows = []
    total = 0
    for scope, project_id, root in _scopes(field, clean_worker):
        for path in sorted((root / "quarantine").glob("*.json")):
            total += 1
            row = read_json(path)
            message_ref = str(row.get("message_ref") or "")
            if boundary is not None and (scope, message_ref) <= tuple(boundary):
                continue
            project_ref = str(row.get("project_ref") or "")
            rows.append({
                "_scope": scope,
                "project_ref": project_ref,
                "message_ref": message_ref,
                "subject": str(row.get("subject") or ""),
                "sender": str(row.get("sender") or ""),
                "quarantined_at": str(row.get("quarantined_at") or ""),
                "reason": _reason(row),
                "read_command": [
                    "pb", "worker", "quarantine", "read",
                    *(["--project-ref", project_ref] if project_id else []),
                    "--message-ref", message_ref,
                ],
            })
    rows.sort(key=lambda row: (row["_scope"], row["message_ref"]))
    page_limit = max(1, min(int(limit), 100))
    page = rows[:page_limit]
    next_cursor = ""
    if len(rows) > page_limit:
        next_cursor = codec.encode((page[-1]["_scope"], page[-1]["message_ref"]))
    return {
        "schema": QUARANTINE_PAGE_SCHEMA,
        "worker_name": clean_worker,
        "total": total,
        "count": len(page),
        "items": [{key: value for key, value in row.items() if key != "_scope"} for row in page],
        "next_cursor": next_cursor,
    }


def _source(field: Any, worker_name: str, project_ref: str, message_ref: str) -> tuple[str, Path]:
    parsed_mail = parse_ref(message_ref)
    if parsed_mail.kind != "mail":
        raise DomainError("field_mail_ref_invalid", "Expected a work:mail reference.")
    project_id = ""
    if project_ref:
        parsed_project = parse_ref(project_ref)
        if parsed_project.kind != "project":
            raise DomainError("field_project_ref_invalid", "Expected a work:project reference.")
        project_id = parsed_project.object_id
    root = field._mail_root(project_id, worker_name)
    return project_id, root / "quarantine" / f"{component(parsed_mail.object_id)}.json"


def _held_row(source: Path, message_ref: str, project_ref: str) -> dict[str, Any]:
    row = read_json(source, required=False)
    if not row or str(row.get("message_ref") or "") != message_ref or str(row.get("project_ref") or "") != project_ref:
        raise DomainError(
            "field_mail_quarantine_not_found",
            "This message is not held in the specified worker mailbox.",
            status=404,
            details={"message_ref": message_ref, "project_ref": project_ref},
        )
    return row


def read_quarantine(field: Any, worker_name: str, *, project_ref: str, message_ref: str) -> dict[str, Any]:
    project_id, source = _source(field, worker_name, project_ref, message_ref)
    with exclusive_lock(field._mail_root(project_id, worker_name) / ".mail.lock"):
        row = _held_row(source, message_ref, project_ref)
    return {
        "schema": "problem-board.worker-quarantine-read.v1",
        "message": row,
        "reason": _reason(row),
        "expected_quarantined_at": str(row.get("quarantined_at") or ""),
    }


def settle_quarantine(
    field: Any,
    worker_name: str,
    *,
    project_ref: str,
    message_ref: str,
    expected_quarantined_at: str,
    action: str,
    reason: str = "",
) -> dict[str, Any]:
    if action not in {"release", "discard"}:
        raise DomainError("field_mail_quarantine_action_invalid", "Expected release or discard.")
    project_id, source = _source(field, worker_name, project_ref, message_ref)
    worker = field.read_worker(worker_name)
    if action == "release" and project_ref and project_ref not in (worker.get("attended_project_refs") or []):
        raise DomainError(
            "field_mail_project_not_attended",
            "Rejoin this project before releasing its mail to the active inbox.",
            status=409,
        )
    root = field._mail_root(project_id, worker_name)
    with exclusive_lock(root / ".mail.lock"):
        row = _held_row(source, message_ref, project_ref)
        held_at = str(row.get("quarantined_at") or "")
        if held_at != expected_quarantined_at:
            raise DomainError(
                "field_mail_quarantine_changed",
                "The held message changed; read it again before taking action.",
                status=409,
                details={"expected_quarantined_at": expected_quarantined_at, "current_quarantined_at": held_at},
            )
        destination = root / ("inbox" if action == "release" else "processed") / source.name
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if destination.exists():
            raise DomainError("field_mail_destination_exists", "A mail record already exists at the destination.", status=409)
        now = utc_now()
        if action == "release":
            history = list(row.get("quarantine_history") or [])
            history.append({"quarantined_at": held_at, "reason": _reason(row)})
            row["quarantine_history"] = history[-10:]
            row["quarantine_history_count"] = int(row.get("quarantine_history_count") or 0) + 1
            row.pop("receive_failure", None)
            row.pop("quarantine_reason", None)
            row.pop("quarantined_at", None)
            row["state"] = "pending"
            row["delivery_status"] = "released_from_quarantine"
        else:
            row["state"] = "discarded"
            row["delivery_status"] = "discarded_from_quarantine"
            row["discarded_at"] = now
            row["discard_reason"] = bounded_text(reason, field="reason", maximum=2000, required=True)
        row["updated_at"] = now
        atomic_write_json(source, row)
        os.replace(source, destination)
    return {
        "schema": "problem-board.worker-quarantine-action.v1",
        "action": action,
        "message_ref": message_ref,
        "project_ref": project_ref,
        "state": row["state"],
        "at": now,
    }

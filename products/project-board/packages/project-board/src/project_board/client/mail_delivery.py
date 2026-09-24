from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from .io import atomic_write_json, read_json, utc_now


ACTIVE_MAILBOX_STATES = ("inbox", "leased")
ALL_MAILBOX_STATES = ("inbox", "leased", "processed", "quarantine")


def delivery_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    """Project one outbox delivery without returning its retained full body."""

    payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
    body = str(payload.get("body") or "")
    failure = row.get("delivery_failure") if isinstance(row.get("delivery_failure"), Mapping) else None
    return {
        "outbox_id": str(row.get("outbox_id") or ""),
        "project_ref": str(row.get("project_ref") or ""),
        "state": str(row.get("state") or ""),
        # A sent row keeps these beside the dropped body (W304 finding 38).
        "recipient": str(payload.get("recipient") or row.get("recipient") or ""),
        "kind": str(payload.get("kind") or row.get("kind_sent") or ""),
        "subject": str(payload.get("subject") or row.get("subject") or ""),
        "source_message_ref": str(payload.get("source_message_ref") or row.get("source_message_ref") or ""),
        **({"delivery_failure": dict(failure)} if failure is not None else {}),
        "remote_disposition": str(row.get("remote_disposition") or ""),
        "created_at": str(row.get("created_at") or ""),
        "settled_at": str(row.get("settled_at") or ""),
        "body_bytes": len(body.encode("utf-8")),
        "body_preview": body[:240],
        "replayed_at": str(row.get("replayed_at") or ""),
        "replayed_to": str(row.get("replayed_to") or ""),
        "replayed_kind": str(row.get("replayed_kind") or ""),
        "replay_message_ref": str(row.get("replay_message_ref") or ""),
        "replay_outbox_id": str(row.get("replay_outbox_id") or ""),
    }


def archive_mailbox_messages(
    source_root: Path,
    destination_root: Path,
    *,
    states: Sequence[str],
    disposition: str,
    reason: str,
    details: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Move mailbox records into an inspectable undeliverable archive."""

    moved: list[dict[str, Any]] = []
    destination_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for state in states:
        folder = source_root / state
        for source in sorted(folder.glob("*.json")):
            row = read_json(source)
            now = utc_now()
            row.update(
                mailbox_state="undeliverable",
                previous_mailbox_state=state,
                state=disposition,
                delivery_status=disposition,
                undeliverable_at=now,
                updated_at=now,
                recipient_failure={
                    "code": disposition,
                    "reason": str(reason or ""),
                    "details": dict(details or {}),
                    "notify_sender": state in ACTIVE_MAILBOX_STATES,
                },
            )
            row.pop("lease", None)
            atomic_write_json(source, row)
            destination = destination_root / source.name
            if destination.exists():
                source.unlink(missing_ok=True)
                row = read_json(destination)
            else:
                os.replace(source, destination)
            moved.append(row)
    return moved


__all__ = [
    "ACTIVE_MAILBOX_STATES",
    "ALL_MAILBOX_STATES",
    "archive_mailbox_messages",
    "delivery_summary",
]

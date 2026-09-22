from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from ..contract.errors import DomainError
from ..contract.mailbox_reconciliation_contract import (
    MAILBOX_RECONCILIATION_RETENTION_DAYS,
    normalize_receipt,
)
from ..contract.mailbox_reconciliation_publication import publication_batches
from .io import atomic_write_json, content_hash, exclusive_lock, read_json, utc_now


LOCAL_RECEIPT_RECORD_SCHEMA = (
    "problem-board.local-mailbox-reconciliation-receipt.v1"
)
OUTBOX_KIND = "mail.reconciliation.publish"
_RECEIPT_TIMESTAMP = re.compile(
    r"_(?P<stamp>\d{8}T\d{6}Z)(?:_[0-9a-fA-F]{4})?\.json$"
)
# One host relay process owns each worker. A failed record clears this marker,
# and a process restart clears all markers, so both crash boundaries re-audit.
_RECOVERY_STATE_LOCK = threading.Lock()
_RECOVERY_LOCKS: dict[tuple[str, str], Any] = {}
_RECOVERY_COMPLETED: set[tuple[str, str]] = set()


def record_receipt(
    field: Any,
    project_id: str,
    *,
    worker_name: str,
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist one complete run before creating its retryable publications."""

    normalized = normalize_receipt(receipt)
    clean_worker = _require_reporter(normalized, worker_name)
    path = _receipt_path(field, project_id, normalized["receipt_id"])
    recovery_key, recovery_lock = _recovery_state(
        field, project_id, worker_name=clean_worker
    )
    with recovery_lock:
        _mark_recovery_incomplete(recovery_key)
        created = False
        with exclusive_lock(field._project_lock(project_id)):
            existing = read_json(path, required=False)
            if existing:
                stored = normalize_receipt(existing.get("receipt") or {})
                if stored["content_hash"] != normalized["content_hash"]:
                    raise DomainError(
                        "field_mail_reconciliation_idempotency_conflict",
                        "The reconciliation receipt identity has different content.",
                        status=409,
                    )
            else:
                created = True
                existing = {
                    "schema": LOCAL_RECEIPT_RECORD_SCHEMA,
                    "receipt": normalized,
                    "publication": {},
                    "created_at": utc_now(),
                }
                atomic_write_json(path, existing)
        if _publication_is_complete(existing):
            queued = existing
        else:
            queued = ensure_publication(
                field,
                project_id,
                worker_name=clean_worker,
                record_path=path,
                discover_existing=not created,
            )
        prune_published_receipts(field, project_id)
        _mark_recovery_complete(recovery_key)
        return queued


def recover_unpublished_receipts(
    field: Any,
    project_id: str,
    *,
    worker_name: str,
    force: bool = False,
) -> dict[str, int]:
    """Repair the receipt-write/outbox-write crash boundary idempotently."""

    root = _receipt_root(field, project_id)
    clean_worker = str(worker_name or "").strip().lower()
    recovery_key, recovery_lock = _recovery_state(
        field, project_id, worker_name=clean_worker
    )
    with recovery_lock:
        if not force and _recovery_is_complete(recovery_key):
            return {"receipts_recovered": 0, "publication_batches": 0}
        recovered = 0
        batches = 0
        for path in sorted(root.glob("*.json")):
            record = read_json(path, required=False)
            if not record:
                continue
            receipt = normalize_receipt(record.get("receipt") or {})
            if receipt["reporter_worker_name"] != clean_worker:
                continue
            if _publication_is_complete(record):
                continue
            result = ensure_publication(
                field,
                project_id,
                worker_name=clean_worker,
                record_path=path,
            )
            recovered += 1
            batches += len(result.get("publication", {}).get("outbox_ids") or [])
        prune_published_receipts(field, project_id)
        _mark_recovery_complete(recovery_key)
        return {"receipts_recovered": recovered, "publication_batches": batches}


def ensure_publication(
    field: Any,
    project_id: str,
    *,
    worker_name: str,
    record_path: Path,
    discover_existing: bool = True,
) -> dict[str, Any]:
    record = read_json(record_path)
    receipt = normalize_receipt(record.get("receipt") or {})
    _require_reporter(receipt, worker_name)
    publications = publication_batches(receipt)
    root = field.control / "outbox"
    outbox_ids: list[str] = []
    with exclusive_lock(root / ".outbox.lock"):
        existing = (
            _publication_outbox_rows(root, receipt["receipt_ref"])
            if discover_existing
            else {}
        )
        for publication in publications:
            batch_index = int(publication["batch_index"])
            publication_hash = content_hash(publication)
            row = existing.get(batch_index)
            if not row:
                outbox_id = _publication_outbox_id(publication_hash)
                row = _outbox_row(root, outbox_id)
            if row:
                _require_publication_row(
                    row,
                    receipt_ref=receipt["receipt_ref"],
                    batch_index=batch_index,
                    publication_hash=publication_hash,
                )
                outbox_ids.append(str(row.get("outbox_id") or ""))
                continue
            row = {
                "schema": "problem-board.service-outbox.v1",
                "outbox_id": outbox_id,
                "kind": OUTBOX_KIND,
                "worker_name": receipt["reporter_worker_name"],
                "project_ref": receipt["project_ref"],
                "content_hash": publication_hash,
                "payload": publication,
                "state": "pending",
                "created_at": utc_now(),
                "retry_count": 0,
                "next_attempt_at": "",
            }
            atomic_write_json(root / "pending" / f"{outbox_id}.json", row)
            outbox_ids.append(outbox_id)

    with exclusive_lock(field._project_lock(project_id)):
        current = read_json(record_path)
        current["publication"] = {
            "kind": OUTBOX_KIND,
            "outbox_ids": outbox_ids,
            "batch_count": len(publications),
            "queued_at": str(
                (current.get("publication") or {}).get("queued_at") or utc_now()
            ),
        }
        atomic_write_json(record_path, current)
        return current


def prune_published_receipts(field: Any, project_id: str) -> int:
    """Apply local retention only after every publication batch was sent."""

    cutoff = datetime.now(timezone.utc) - timedelta(
        days=MAILBOX_RECONCILIATION_RETENTION_DAYS
    )
    removed = 0
    root = _receipt_root(field, project_id)
    outbox_root = field.control / "outbox"
    with exclusive_lock(field._project_lock(project_id)):
        for path in sorted(root.glob("*.json")):
            if _receipt_filename_is_recent(path, cutoff=cutoff):
                continue
            record = read_json(path, required=False)
            receipt = dict(record.get("receipt") or {})
            try:
                completed_at = datetime.fromisoformat(
                    str(receipt.get("completed_at") or "").replace("Z", "+00:00")
                )
            except ValueError:
                continue
            if completed_at.tzinfo is None:
                completed_at = completed_at.replace(tzinfo=timezone.utc)
            publication = dict(record.get("publication") or {})
            outbox_ids = [
                str(value) for value in publication.get("outbox_ids") or [] if value
            ]
            if completed_at >= cutoff or not outbox_ids:
                continue
            if not all(
                _outbox_is_sent(outbox_root, outbox_id) for outbox_id in outbox_ids
            ):
                continue
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def _publication_is_complete(record: Mapping[str, Any]) -> bool:
    publication = dict(record.get("publication") or {})
    expected = int(publication.get("batch_count") or 0)
    outbox_ids = [
        str(value) for value in publication.get("outbox_ids") or [] if value
    ]
    return expected > 0 and len(outbox_ids) == expected


def _publication_outbox_id(publication_hash: str) -> str:
    return f"outbox_mailrecon_{publication_hash}"


def _outbox_row(outbox_root: Path, outbox_id: str) -> dict[str, Any]:
    for state in ("pending", "leased", "sent"):
        row = read_json(outbox_root / state / f"{outbox_id}.json", required=False)
        if row:
            return row
    return {}


def _require_publication_row(
    row: Mapping[str, Any],
    *,
    receipt_ref: str,
    batch_index: int,
    publication_hash: str,
) -> None:
    payload = dict(row.get("payload") or {})
    header = dict(payload.get("receipt") or {})
    if (
        row.get("kind") == OUTBOX_KIND
        and header.get("receipt_ref") == receipt_ref
        and int(payload.get("batch_index") or 0) == batch_index
        and str(row.get("content_hash") or "") == publication_hash
    ):
        return
    raise DomainError(
        "field_mail_reconciliation_idempotency_conflict",
        "A reconciliation publication batch has different content.",
        status=409,
        details={"receipt_ref": receipt_ref, "batch_index": batch_index},
    )


def _receipt_filename_is_recent(path: Path, *, cutoff: datetime) -> bool:
    match = _RECEIPT_TIMESTAMP.search(path.name)
    if not match:
        return False
    created_at = datetime.strptime(match.group("stamp"), "%Y%m%dT%H%M%SZ").replace(
        tzinfo=timezone.utc
    )
    return created_at >= cutoff


def _recovery_state(
    field: Any, project_id: str, *, worker_name: str
) -> tuple[tuple[str, str], Any]:
    key = (str(_receipt_root(field, project_id).resolve()), worker_name)
    with _RECOVERY_STATE_LOCK:
        lock = _RECOVERY_LOCKS.setdefault(key, threading.Lock())
    return key, lock


def _recovery_is_complete(key: tuple[str, str]) -> bool:
    with _RECOVERY_STATE_LOCK:
        return key in _RECOVERY_COMPLETED


def _mark_recovery_incomplete(key: tuple[str, str]) -> None:
    with _RECOVERY_STATE_LOCK:
        _RECOVERY_COMPLETED.discard(key)


def _mark_recovery_complete(key: tuple[str, str]) -> None:
    with _RECOVERY_STATE_LOCK:
        _RECOVERY_COMPLETED.add(key)


def _publication_outbox_rows(
    outbox_root: Path, receipt_ref: str
) -> dict[int, dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    for state in ("pending", "leased", "sent"):
        for path in sorted((outbox_root / state).glob("*.json")):
            row = read_json(path, required=False)
            payload = dict(row.get("payload") or {})
            header = dict(payload.get("receipt") or {})
            if row.get("kind") != OUTBOX_KIND or header.get("receipt_ref") != receipt_ref:
                continue
            index = int(payload.get("batch_index") or 0)
            prior = rows.get(index)
            if prior and str(prior.get("content_hash") or "") != str(
                row.get("content_hash") or ""
            ):
                raise DomainError(
                    "field_mail_reconciliation_idempotency_conflict",
                    "Duplicate reconciliation batches carry different content.",
                    status=409,
                    details={"receipt_ref": receipt_ref, "batch_index": index},
                )
            rows[index] = row
    return rows


def _outbox_is_sent(outbox_root: Path, outbox_id: str) -> bool:
    path = outbox_root / "sent" / f"{outbox_id}.json"
    if not path.is_file():
        return False
    return str(read_json(path).get("state") or "") in {"sent", "ignored"}


def _require_reporter(receipt: Mapping[str, Any], worker_name: str) -> str:
    clean_worker = str(worker_name or "").strip().lower()
    if str(receipt.get("reporter_worker_name") or "") != clean_worker:
        raise DomainError(
            "field_mail_reconciliation_reporter_mismatch",
            "Only the worker that recorded a reconciliation receipt may publish it.",
            status=403,
            details={
                "reporter_worker_name": str(
                    receipt.get("reporter_worker_name") or ""
                ),
                "worker_name": clean_worker,
            },
        )
    return clean_worker


def _receipt_root(field: Any, project_id: str) -> Path:
    return field._project_dir(project_id) / "mail" / "reconciliation-receipts"


def _receipt_path(field: Any, project_id: str, receipt_id: str) -> Path:
    return _receipt_root(field, project_id) / f"{receipt_id}.json"


__all__ = [
    "LOCAL_RECEIPT_RECORD_SCHEMA",
    "OUTBOX_KIND",
    "prune_published_receipts",
    "record_receipt",
    "recover_unpublished_receipts",
]

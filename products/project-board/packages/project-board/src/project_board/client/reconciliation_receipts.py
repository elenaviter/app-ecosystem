"""Local mailbox reconciliation receipts, kept only for runs that carry information.

A reconciliation run examines every mailbox of a project and archives mail no
one can read. Until W287 every run wrote a receipt and queued it for the
service, whether it archived anything or not: dev-main held 51,855 receipts,
every one empty, and the relay re-read all of them after each restart. The
rules this module follows are in ``products/project-board/docs/storage-and-retention.md``,
section "Relay Local State" (LS1 to LS5).

Layout, per project and reporting worker (the agent)::

    mail-reconciliation/<agent>/marker.json
        the last run, overwritten in place (LS1)
    mail-reconciliation/<agent>/pending/<receipt_id>.json
        receipts whose publication is not yet terminal (LS3: recovery reads only this)
    mail-reconciliation/<agent>/<yyyy>/<mm>/<dd>/<hh>/<started>_<completed>_<publication>_<receipt_id>.json
        finished receipts, in the hour the run started; retention drops whole
        hour folders by name, without opening a file (LS2)
"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from ..contract.errors import DomainError
from ..contract.mailbox_reconciliation_contract import (
    MAILBOX_RECONCILIATION_RETENTION_DAYS,
    normalize_receipt,
)
from .io import atomic_write_json, exclusive_lock, read_json, utc_now
from .local_store import PartitionedStore
from .reconciliation_publication import (
    PRUNABLE_PUBLICATION_STATES,
    TERMINAL_PUBLICATION_STATES,
    PUBLICATION_MISSING,
    publication_is_queued,
    publication_state,
    queue_publication,
)


logger = logging.getLogger(__name__)

LOCAL_RECEIPT_RECORD_SCHEMA = "problem-board.local-mailbox-reconciliation-receipt.v1"
LOCAL_MARKER_SCHEMA = "problem-board.local-mailbox-reconciliation-marker.v1"
STORE = "mail-reconciliation"
PENDING = "pending"
MARKER = "marker.json"
MAX_BYTES_PER_AGENT = 50 * 1024 * 1024
MAX_RECORDS_PER_AGENT = 50_000


def receipt_carries_information(receipt: Mapping[str, Any]) -> bool:
    """A run carries information when it archived mail or hit a failure."""

    return bool(
        int(receipt.get("archived_count") or 0)
        or receipt.get("archived_mailboxes")
        or receipt.get("failure_notices")
        or receipt.get("report_failures")
    )


def record_receipt(
    field: Any,
    project_id: str,
    *,
    worker_name: str,
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Keep one run: the marker always, a receipt only when it carries information.

    Returns the stored receipt record, or for a run that changed nothing a
    record with ``stored`` false and no file behind it.
    """

    normalized = normalize_receipt(receipt)
    clean_worker = _require_reporter(normalized, worker_name)
    if not receipt_carries_information(normalized):
        # LS1: a run that changed nothing leaves no receipt, only the marker.
        _write_marker(field, project_id, clean_worker, normalized, stored=False)
        return {"schema": LOCAL_RECEIPT_RECORD_SCHEMA, "receipt": normalized, "publication": {}, "stored": False}
    path = pending_path(field, project_id, clean_worker, normalized["receipt_id"])
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
            existing = {
                "schema": LOCAL_RECEIPT_RECORD_SCHEMA,
                "receipt": normalized,
                "publication": {},
                "created_at": utc_now(),
            }
            atomic_write_json(path, existing)
    if not publication_is_queued(existing):
        queue_publication(field, project_id, worker_name=clean_worker, record_path=path)
    record = settle_if_terminal(field, project_id, worker_name=clean_worker, path=path)
    _write_marker(field, project_id, clean_worker, normalized, stored=True)
    return {**record, "stored": True}


def recover_unpublished_receipts(
    field: Any,
    project_id: str,
    *,
    worker_name: str,
) -> dict[str, int]:
    """Finish every receipt this worker left in ``pending/``, and nothing else.

    LS3 and LS4: the work is proportional to the receipts still in flight, and
    no process-memory flag decides whether it runs, so a restart costs the
    same as any other cycle.
    """

    clean_worker = str(worker_name or "").strip().lower()
    history = PartitionedStore(store_root(field, project_id), store=STORE)
    root = history.agent_root(clean_worker) / PENDING
    paths = sorted(root.glob("*.json")) if root.is_dir() else []
    recovered = batches = settled = 0
    with history.reading("startup_recovery") as read:
        read.opened_pending(clean_worker, len(paths))
        for path in paths:
            record = read_json(path, required=False)
            if not record:
                continue
            receipt = normalize_receipt(record.get("receipt") or {})
            if receipt["reporter_worker_name"] != clean_worker:
                continue
            if not publication_is_queued(record) or publication_state(field, record) == PUBLICATION_MISSING:
                record = queue_publication(field, project_id, worker_name=clean_worker, record_path=path)
                recovered += 1
                batches += len((record.get("publication") or {}).get("outbox_ids") or [])
            if str(settle_if_terminal(field, project_id, worker_name=clean_worker, path=path).get("publication", {}).get("state") or "") in TERMINAL_PUBLICATION_STATES:
                settled += 1
    return {"receipts_recovered": recovered, "publication_batches": batches, "receipts_settled": settled}


def settle_if_terminal(
    field: Any,
    project_id: str,
    *,
    worker_name: str,
    path: Path,
) -> dict[str, Any]:
    """Move a receipt out of ``pending/`` once its publication is terminal."""

    record = read_json(path, required=False)
    if not record:
        return {}
    state = publication_state(field, record)
    if state not in TERMINAL_PUBLICATION_STATES:
        return record
    receipt = normalize_receipt(record.get("receipt") or {})
    target = partition_path(field, project_id, worker_name, receipt, publication=state)
    with exclusive_lock(field._project_lock(project_id)):
        current = read_json(path, required=False)
        if not current:
            return read_json(target, required=False)
        current["publication"] = {**dict(current.get("publication") or {}), "state": state}
        current["settled_at"] = utc_now()
        atomic_write_json(target, current)
        path.unlink(missing_ok=True)
        return current


def apply_receipt_retention(
    field: Any,
    project_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """Remove hour folders past retention whose receipts were all published.

    LS2: the decision reads folder and file names only. A folder holding a
    refused receipt is kept whole, because that evidence never reached the
    service (W287 acceptance 1).
    """

    current = now or datetime.now(timezone.utc)
    cutoff = current - timedelta(days=MAILBOX_RECONCILIATION_RETENTION_DAYS)
    root = store_root(field, project_id)
    totals = {
        "partitions_removed": 0,
        "receipts_removed": 0,
        "receipts_kept_refused": 0,
        "size_bound_warnings": 0,
    }
    if not root.is_dir():
        return totals
    history = PartitionedStore(root, store=STORE)
    for agent_dir in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        inventory: list[tuple[Path, datetime, list[str], int]] = []
        with history.reading("retention") as read:
            for hour_dir, hour_start in _hour_partitions(agent_dir):
                names = [name for name in os.listdir(hour_dir) if name.endswith(".json")]
                read.opened(
                    agent_dir.name,
                    hour_start.strftime("%Y-%m-%dT%H"),
                    len(names),
                )
                size = 0
                for name in names:
                    try:
                        size += (hour_dir / name).stat().st_size
                    except FileNotFoundError:
                        continue
                inventory.append((hour_dir, hour_start, names, size))
                if hour_start + timedelta(hours=1) > cutoff:
                    continue
                if all(_publication_of(name) in PRUNABLE_PUBLICATION_STATES for name in names):
                    shutil.rmtree(hour_dir)
                    totals["partitions_removed"] += 1
                    totals["receipts_removed"] += len(names)
                    read.removed_from(agent_dir.name, len(names))
                else:
                    totals["receipts_kept_refused"] += sum(
                        1 for name in names if _publication_of(name) not in PRUNABLE_PUBLICATION_STATES
                    )
            retained = [item for item in inventory if item[0].is_dir()]
            retained_records = sum(len(item[2]) for item in retained)
            retained_bytes = sum(item[3] for item in retained)
            for hour_dir, hour_start, names, size in retained:
                if (
                    retained_records <= MAX_RECORDS_PER_AGENT
                    and retained_bytes <= MAX_BYTES_PER_AGENT
                ):
                    break
                # Refused evidence has not reached the service. Keep it and make
                # the pressure visible rather than silently deleting it.
                if not all(
                    _publication_of(name) in PRUNABLE_PUBLICATION_STATES
                    for name in names
                ):
                    continue
                shutil.rmtree(hour_dir)
                retained_records -= len(names)
                retained_bytes -= size
                totals["partitions_removed"] += 1
                totals["receipts_removed"] += len(names)
                read.removed_from(agent_dir.name, len(names))
        if (
            retained_records > MAX_RECORDS_PER_AGENT
            or retained_bytes > MAX_BYTES_PER_AGENT
        ):
            totals["size_bound_warnings"] += 1
            logger.error(
                "relay store bound reached worker=%s store=%s records=%d bytes=%d "
                "record_limit=%d byte_limit=%d reason=refused-evidence-retained",
                agent_dir.name,
                STORE,
                retained_records,
                retained_bytes,
                MAX_RECORDS_PER_AGENT,
                MAX_BYTES_PER_AGENT,
            )
        _remove_empty_date_folders(agent_dir)
    return totals


def read_marker(field: Any, project_id: str, worker_name: str) -> dict[str, Any]:
    return dict(read_json(agent_root(field, project_id, worker_name) / MARKER, required=False) or {})


def store_root(field: Any, project_id: str) -> Path:
    return field._project_dir(project_id) / STORE


def agent_root(field: Any, project_id: str, worker_name: str) -> Path:
    return PartitionedStore(store_root(field, project_id), store=STORE).agent_root(
        worker_name
    )


def pending_path(field: Any, project_id: str, worker_name: str, receipt_id: str) -> Path:
    return agent_root(field, project_id, worker_name) / PENDING / f"{receipt_id}.json"


def partition_path(
    field: Any,
    project_id: str,
    worker_name: str,
    receipt: Mapping[str, Any],
    *,
    publication: str,
) -> Path:
    started = _parse(receipt.get("started_at"))
    completed = _parse(receipt.get("completed_at")) or started
    hour = started.strftime("%Y/%m/%d/%H").split("/")
    name = f"{_stamp(started)}_{_stamp(completed)}_{publication}_{receipt['receipt_id']}.json"
    return agent_root(field, project_id, worker_name).joinpath(*hour, name)


def _write_marker(
    field: Any,
    project_id: str,
    worker_name: str,
    receipt: Mapping[str, Any],
    *,
    stored: bool,
) -> None:
    path = agent_root(field, project_id, worker_name) / MARKER
    examined = dict(receipt.get("examined") or {})
    with exclusive_lock(field._project_lock(project_id)):
        marker = dict(read_json(path, required=False) or {})
        marker.update(
            schema=LOCAL_MARKER_SCHEMA,
            worker_name=worker_name,
            project_ref=str(receipt.get("project_ref") or ""),
            last_run_started_at=str(receipt.get("started_at") or ""),
            last_run_completed_at=str(receipt.get("completed_at") or ""),
            last_run_examined={
                "recipient_directory_entries": int(examined.get("recipient_directory_entries") or 0),
                "undeliverable_records": int(examined.get("undeliverable_records") or 0),
                "mailboxes": len(examined.get("mailboxes") or []),
            },
            runs_total=int(marker.get("runs_total") or 0) + 1,
            runs_since_receipt=0 if stored else int(marker.get("runs_since_receipt") or 0) + 1,
            updated_at=utc_now(),
        )
        if stored:
            marker["last_receipt_ref"] = str(receipt.get("receipt_ref") or "")
            marker["last_receipt_at"] = str(receipt.get("completed_at") or "")
        atomic_write_json(path, marker)


def _hour_partitions(agent_dir: Path):
    for year in _numbered(agent_dir, 4):
        for month in _numbered(year, 2):
            for day in _numbered(month, 2):
                for hour in _numbered(day, 2):
                    try:
                        start = datetime(
                            int(year.name), int(month.name), int(day.name), int(hour.name),
                            tzinfo=timezone.utc,
                        )
                    except ValueError:
                        continue
                    yield hour, start


def _numbered(parent: Path, width: int) -> list[Path]:
    return sorted(
        child for child in parent.iterdir()
        if child.is_dir() and len(child.name) == width and child.name.isdigit()
    )


def _remove_empty_date_folders(agent_dir: Path) -> None:
    for year in _numbered(agent_dir, 4):
        for month in _numbered(year, 2):
            for day in _numbered(month, 2):
                _rmdir_if_empty(day)
            _rmdir_if_empty(month)
        _rmdir_if_empty(year)


def _rmdir_if_empty(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass


def _publication_of(name: str) -> str:
    parts = name.split("_")
    return parts[2] if len(parts) > 3 else ""


def _parse(value: Any) -> datetime:
    text = str(value or "").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return datetime.now(timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _require_reporter(receipt: Mapping[str, Any], worker_name: str) -> str:
    clean_worker = str(worker_name or "").strip().lower()
    if str(receipt.get("reporter_worker_name") or "") != clean_worker:
        raise DomainError(
            "field_mail_reconciliation_reporter_mismatch",
            "Only the worker that recorded a reconciliation receipt may publish it.",
            status=403,
            details={
                "reporter_worker_name": str(receipt.get("reporter_worker_name") or ""),
                "worker_name": clean_worker,
            },
        )
    return clean_worker


__all__ = [
    "LOCAL_MARKER_SCHEMA",
    "LOCAL_RECEIPT_RECORD_SCHEMA",
    "MAX_BYTES_PER_AGENT",
    "MAX_RECORDS_PER_AGENT",
    "STORE",
    "agent_root",
    "apply_receipt_retention",
    "partition_path",
    "pending_path",
    "read_marker",
    "receipt_carries_information",
    "record_receipt",
    "recover_unpublished_receipts",
    "settle_if_terminal",
    "store_root",
]

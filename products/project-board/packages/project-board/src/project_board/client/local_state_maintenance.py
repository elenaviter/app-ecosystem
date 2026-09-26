"""Relay-local housekeeping that runs beside the relay cycle, never inside it.

Everything here can cost time in proportion to history, so the relay runs it
in a worker thread on its own schedule (rule LS5 in
``products/project-board/docs/storage-and-retention.md``, "Relay Local State"). Whether
retention is due is read from a file in the field, not from process memory, so
a restart neither repeats it nor skips it (LS4).

Three jobs:

- the one-time cleanup of the flat ``mail/reconciliation-receipts/`` directory
  that every relay before W287 filled with one receipt per run: empty receipts
  and their publication rows are deleted, receipts that carry information move
  into the per-agent layout of :mod:`reconciliation_receipts`;
- receipt retention by hour folder (:func:`reconciliation_receipts.apply_receipt_retention`);
- outbox retention for settled rows (``sent/`` and ``refused/``);
- advancing each clean setup read root to its integration ref (W262,
  :mod:`read_roots`).
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..contract.errors import DomainError
from ..contract.mailbox_reconciliation_contract import normalize_receipt
from .assignment_store import AssignmentStore
from .io import atomic_write_json, exclusive_lock, parse_utc, read_json, utc_now
from .local_store import PartitionedStore, agent_component
from .outbox_layout import OUTBOX_TERMINAL_FOLDERS
from .outbox_store import (
    OUTBOX_TERMINAL_MAX_BYTES_PER_AGENT,
    OUTBOX_TERMINAL_MAX_RECORDS_PER_AGENT,
    OUTBOX_TERMINAL_RETENTION_DAYS,
    OutboxStore,
)
from .operation_store import OperationStore
from .read_roots import advance_read_roots
from .reconciliation_replay import replay_refused_receipts
from .reconciliation_receipts import (
    apply_receipt_retention,
    pending_path,
    receipt_carries_information,
    settle_if_terminal,
    store_root,
)


logger = logging.getLogger(__name__)

MAINTENANCE_STATE = "local-state-maintenance.json"
MAINTENANCE_STATE_SCHEMA = "problem-board.local-state-maintenance.v1"
RETENTION_INTERVAL_SECONDS = 3600
LEGACY_RECEIPTS = ("mail", "reconciliation-receipts")
LEGACY_CLAIMED = ".legacy-claimed"
LEGACY_UNREADABLE = ".legacy-unreadable"
LEGACY_PROGRESS = ".legacy-cleanup.json"
LEGACY_BATCH_SIZE = 1000
SESSION_RETENTION_DAYS = 90
SESSION_MAX_RECORDS_PER_WORKER = 1000


def run_local_state_maintenance(
    field: Any,
    *,
    now: datetime | None = None,
    repositories: Any = None,
    read_roots_state: Path | None = None,
) -> dict[str, Any]:
    """One maintenance pass over every project in this field.

    With the host's ``repositories`` map and a state file, it also advances
    each clean setup read root to its integration ref (W262), at most every
    five minutes per alias.
    """

    current = now or datetime.now(timezone.utc)
    summary: dict[str, Any] = {"legacy_receipts": {}, "retention": None}
    summary["read_roots"] = (
        advance_read_roots(repositories, read_roots_state, now=current)
        if repositories is not None and read_roots_state is not None
        else None
    )
    summary["flat_events"] = {}
    summary["refused_receipts_replay"] = {}
    summary["flat_assignments"] = {}
    summary["flat_scope_leases"] = {}
    summary["flat_mail_history"] = field._mail_history().migrate_legacy()
    summary["flat_idempotency"] = field._idempotency().migrate_legacy()
    summary["flat_journal_receipts"] = field._journal_receipts().migrate_legacy()
    summary["flat_operations"] = OperationStore(
        field.control / "operations" / "journal-index"
    ).migrate_legacy()
    for project_id in _project_ids(field):
        summary["legacy_receipts"][project_id] = cleanup_legacy_receipts(field, project_id)
        summary["flat_events"][project_id] = migrate_flat_events(field, project_id)
        summary["refused_receipts_replay"][project_id] = replay_refused_receipts(
            field, project_id, now=current
        )
        with exclusive_lock(field._project_lock(project_id)):
            summary["flat_assignments"][project_id] = AssignmentStore(
                field._project_dir(project_id) / "assignments"
            ).migrate_legacy()
        with exclusive_lock(field._scope_leases(project_id).lock):
            summary["flat_scope_leases"][project_id] = field._scope_leases(
                project_id
            ).migrate_legacy()
    # After the receipt cleanup, so empty receipts' rows are deleted, not moved.
    summary["flat_outbox"] = migrate_flat_outbox(field)
    state_path = field.control / MAINTENANCE_STATE
    state = dict(read_json(state_path, required=False) or {})
    last = parse_utc(str(state.get("retention_ran_at") or "")) if state.get("retention_ran_at") else None
    if last is None or (current - last).total_seconds() >= RETENTION_INTERVAL_SECONDS:
        event_cutoff = current - timedelta(days=int(field.EVENT_RETENTION_DAYS))
        retention: dict[str, Any] = {
            "receipts": {
                project_id: apply_receipt_retention(field, project_id, now=current)
                for project_id in _project_ids(field)
            },
            "events": {
                project_id: field._events(project_id).expire(
                    cutoff=event_cutoff,
                    max_bytes_per_agent=int(field.EVENT_MAX_BYTES_PER_AGENT),
                    max_records_per_agent=int(field.EVENT_MAX_RECORDS_PER_AGENT),
                )
                for project_id in _project_ids(field)
            },
            "outbox": apply_outbox_retention(field, now=current),
            "sessions": apply_session_retention(field, now=current),
            "keyed": expire_keyed_stores(field, now=current),
        }
        state.update(
            schema=MAINTENANCE_STATE_SCHEMA,
            retention_ran_at=current.strftime("%Y-%m-%dT%H:%M:%SZ"),
            last_retention=retention,
        )
        atomic_write_json(state_path, state)
        summary["retention"] = retention
    return summary


def cleanup_legacy_receipts(
    field: Any,
    project_id: str,
    *,
    batch_size: int = LEGACY_BATCH_SIZE,
) -> dict[str, Any]:
    """Empty the flat pre-W287 receipt directory, once, with counts per agent.

    Each file is claimed by renaming it into ``.legacy-claimed/`` before it is
    handled, so two relays on one host never handle the same receipt, and a
    crash leaves the file where the next pass finds it. A receipt whose
    publication row is still leased by a delivery in flight stays claimed until
    a later pass.
    """

    legacy = field._project_dir(project_id).joinpath(*LEGACY_RECEIPTS)
    root = store_root(field, project_id)
    claimed_root = root / LEGACY_CLAIMED
    progress_path = root / LEGACY_PROGRESS
    progress = dict(read_json(progress_path, required=False) or {})
    base_agents = {
        agent: dict(values) for agent, values in dict(progress.get("agents") or {}).items()
    }
    if progress.get("state") == "complete" and not legacy.exists() and not claimed_root.exists():
        return {"state": "complete"}
    if not legacy.exists() and not claimed_root.exists():
        return {"state": "absent"}
    started = time.monotonic()
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    claimed_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    budget = max(1, int(batch_size))
    claimed = sorted(claimed_root.glob("*.json"))[:budget]
    for path in claimed:
        _handle_legacy_receipt(field, project_id, path, counts)
    budget -= len(claimed)
    if budget > 0 and legacy.is_dir():
        batch: list[str] = []
        with os.scandir(legacy) as entries:
            for entry in entries:
                if entry.name.endswith(".json") and entry.is_file(follow_symlinks=False):
                    batch.append(entry.name)
                    if len(batch) >= budget:
                        break
        for name in batch:
            claimed = claimed_root / name
            try:
                os.replace(legacy / name, claimed)
            except FileNotFoundError:
                continue
            _handle_legacy_receipt(field, project_id, claimed, counts)
        _record_progress(progress_path, progress, base_agents, counts, state="running")
    remaining = sum(1 for _ in claimed_root.glob("*.json"))
    legacy_remaining = any(legacy.glob("*.json")) if legacy.is_dir() else False
    for directory in (legacy, claimed_root):
        try:
            directory.rmdir()
        except OSError:
            pass
    state = "complete" if not legacy_remaining and remaining == 0 else "running"
    _record_progress(progress_path, progress, base_agents, counts, state=state)
    elapsed = int((time.monotonic() - started) * 1000)
    for agent, values in sorted(counts.items()):
        logger.info(
            "relay store migrated worker=%s store=reconciliation-receipts deleted=%d moved=%d "
            "outbox_rows_deleted=%d deferred=%d unreadable=%d ms=%d",
            agent, values["deleted"], values["moved"], values["outbox_rows_deleted"],
            values["deferred"], values["unreadable"], elapsed,
        )
    return {"state": state, "claimed_remaining": remaining, "agents": {k: dict(v) for k, v in counts.items()}}


def migrate_flat_events(
    field: Any,
    project_id: str,
    *,
    batch_size: int = LEGACY_BATCH_SIZE,
) -> dict[str, Any]:
    """Move pre-W287 flat ``events/<id>.json`` files into agent and hour folders.

    Each file moves under the project lock, so an event written meanwhile and
    a moved one never share a path. Counts per agent go to the log.
    """

    legacy = field._project_dir(project_id) / "events"
    if not legacy.is_dir():
        return {"state": "absent"}
    events = field._events(project_id)
    started = time.monotonic()
    moved: dict[str, int] = defaultdict(int)
    unreadable = 0
    batch: list[str] = []
    with os.scandir(legacy) as entries:
        for entry in entries:
            if entry.name.endswith(".json") and entry.is_file(follow_symlinks=False):
                batch.append(entry.name)
                if len(batch) >= batch_size:
                    break
    if batch:
        with exclusive_lock(field._project_lock(project_id)):
            for name in batch:
                path = legacy / name
                if not path.exists():
                    continue
                row = _readable(path)
                if not row:
                    # Kept for a person, never deleted unread (review on W287 2b).
                    _quarantine(path, legacy / LEGACY_UNREADABLE)
                    unreadable += 1
                    continue
                actor = str(row.get("actor") or row.get("worker_name") or "-")
                event_id = str(row.get("event_id") or name[:-5])
                try:
                    created = parse_utc(str(row.get("created_at") or ""))
                except (DomainError, TypeError, ValueError):
                    created = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                events.write(actor, event_id, created, row, slug=str(row.get("event_ref") or row.get("kind") or "").rsplit(":", 1)[-1])
                path.unlink()
                moved[agent_component(actor)] += 1
    remaining = any(
        path for path in legacy.glob("*.json") if not path.name.startswith(".")
    )
    state = "running" if remaining else "complete"
    elapsed = int((time.monotonic() - started) * 1000)
    for agent in sorted(set(moved) | set(events.agents())):
        marker = events.agent_root(agent) / ".migration.json"
        prior = dict(read_json(marker, required=False) or {})
        atomic_write_json(
            marker,
            {
                "schema": "problem-board.event-store-migration.v1",
                "state": state,
                "moved": int(prior.get("moved") or 0) + moved.get(agent, 0),
                "updated_at": utc_now(),
            },
        )
        if moved.get(agent):
            logger.info(
                "relay store migrated worker=%s store=events moved=%d unreadable=0 ms=%d",
                agent,
                moved[agent],
                elapsed,
            )
    if unreadable:
        logger.warning(
            "relay store migrated worker=- store=events moved=0 unreadable=%d ms=%d",
            unreadable,
            elapsed,
        )
    return {"state": state, "moved": dict(moved), "unreadable": unreadable}


def apply_outbox_retention(field: Any, *, now: datetime | None = None) -> dict[str, int]:
    """Remove settled outbox rows older than the retention window, per agent.

    Settled rows live in hour folders per project and agent, so retention drops
    whole folders by name and logs one line per agent (W287 2b). Rows still in
    the flat pre-2b folders expire by file modification time, which the relay
    set when it settled them. Attachment folders expire by age once no row in
    flight names them. Pending and leased rows are never touched.
    """

    current = now or datetime.now(timezone.utc)
    cutoff_dt = current - timedelta(days=OUTBOX_TERMINAL_RETENTION_DAYS)
    cutoff = cutoff_dt.timestamp()
    outbox = OutboxStore(field.control)
    totals: dict[str, int] = {"partitions": 0, "records": 0, "flat": 0, "attachments": 0}
    projects = field.control / "projects"
    project_refs = [
        "work:project:" + project_dir.name
        for project_dir in (sorted(projects.glob("*")) if projects.is_dir() else ())
        if (project_dir / "outbox").is_dir()
    ] + [""]
    for project_ref in project_refs:
        if not outbox.project_root(project_ref).is_dir():
            continue
        with exclusive_lock(outbox.lock):
            removed = outbox.partitioned(project_ref).expire(
                cutoff=cutoff_dt,
                max_bytes_per_agent=OUTBOX_TERMINAL_MAX_BYTES_PER_AGENT,
                max_records_per_agent=OUTBOX_TERMINAL_MAX_RECORDS_PER_AGENT,
            )
        totals["partitions"] += removed["partitions"]
        totals["records"] += removed["records"]
        for _ref, agent_root in outbox.agent_roots(project_ref=project_ref):
            totals["attachments"] += _expire_attachments(outbox, agent_root / "attachments", cutoff)
    root = outbox.legacy_root
    started = time.monotonic()
    examined = 0
    for folder in OUTBOX_TERMINAL_FOLDERS:
        directory = root / folder
        if not directory.is_dir():
            continue
        expired: list[Path] = []
        with os.scandir(directory) as entries:
            for entry in entries:
                if not entry.name.endswith(".json"):
                    continue
                examined += 1
                try:
                    if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                        expired.append(Path(entry.path))
                except FileNotFoundError:
                    continue
        for start in range(0, len(expired), 500):
            with exclusive_lock(outbox.lock):
                for path in expired[start:start + 500]:
                    path.unlink(missing_ok=True)
                    totals["flat"] += 1
    totals["attachments"] += _expire_attachments(outbox, root / "attachments", cutoff)
    if examined:
        logger.info(
            "relay store read worker=* store=outbox-flat op=retention range=..%s partitions=%d records=%d ms=%d removed=%d",
            cutoff_dt.strftime("%Y-%m-%dT%H"), len(OUTBOX_TERMINAL_FOLDERS), examined,
            int((time.monotonic() - started) * 1000), totals["flat"],
        )
    return totals


def apply_session_retention(field: Any, *, now: datetime | None = None) -> dict[str, int]:
    """Bound each worker's current/detached session map.

    Session files are mutable current-state rows rather than append-only
    history, so they stay keyed directly by session id. Detached rows expire;
    active rows are never removed. The count cap removes the oldest detached
    rows first and emits a warning if active rows alone exceed it (LS2).
    """

    current = now or datetime.now(timezone.utc)
    cutoff = current - timedelta(days=SESSION_RETENTION_DAYS)
    totals = {"removed": 0, "warnings": 0}
    projects = field.control / "projects"
    for project in sorted(projects.iterdir()) if projects.is_dir() else ():
        sessions = project / "sessions"
        session_store = PartitionedStore(sessions, store="sessions")
        for worker in sorted(sessions.iterdir()) if sessions.is_dir() else ():
            if not worker.is_dir():
                continue
            rows: list[tuple[Path, dict[str, Any], datetime]] = []
            paths = sorted(worker.glob("*.json"))
            with session_store.reading("retention") as read:
                read.opened_pending(worker.name, len(paths))
                for path in paths:
                    row = _readable(path)
                    if not row:
                        continue
                    text = str(row.get("detached_at") or row.get("heartbeat_at") or "")
                    try:
                        when = parse_utc(text)
                    except (DomainError, TypeError, ValueError):
                        when = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                    rows.append((path, row, when))
            removed = 0
            for path, row, when in rows:
                if str(row.get("state") or "") == "detached" and when <= cutoff:
                    path.unlink(missing_ok=True)
                    removed += 1
            retained = [
                item for item in rows if item[0].is_file()
            ]
            detached = sorted(
                (
                    item
                    for item in retained
                    if str(item[1].get("state") or "") == "detached"
                ),
                key=lambda item: item[2],
            )
            while len(retained) > SESSION_MAX_RECORDS_PER_WORKER and detached:
                path, _row, _when = detached.pop(0)
                path.unlink(missing_ok=True)
                retained = [item for item in retained if item[0] != path]
                removed += 1
            warning = int(len(retained) > SESSION_MAX_RECORDS_PER_WORKER)
            totals["removed"] += removed
            totals["warnings"] += warning
            if warning:
                logger.error(
                    "relay store bound reached worker=%s store=sessions records=%d "
                    "record_limit=%d reason=active-sessions-retained",
                    worker.name,
                    len(retained),
                    SESSION_MAX_RECORDS_PER_WORKER,
                )
    return totals


def _expire_attachments(outbox: OutboxStore, directory: Path, cutoff: float) -> int:
    """Remove attachment folders older than ``cutoff`` whose row is not in flight."""

    removed = 0
    if not directory.is_dir():
        return removed
    for child in sorted(directory.iterdir()):
        try:
            if not child.is_dir() or child.stat().st_mtime >= cutoff:
                continue
        except FileNotFoundError:
            continue
        with exclusive_lock(outbox.lock):
            found = outbox.find(child.name)
            if found is not None and found[1] in {"pending", "leased"}:
                continue
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
    return removed


def migrate_flat_outbox(field: Any, *, batch_size: int = LEGACY_BATCH_SIZE) -> dict[str, Any]:
    """Move pre-2b rows from ``outbox/<folder>/`` into the per-agent layout.

    In-flight rows move to their agent's ``pending/`` or ``leased/`` with their
    lease intact; settled rows move into the hour they were created. Attachment
    folders stay where they are, because rows name them by absolute path, and
    expire by age. Counts per agent go to the log.
    """

    outbox = OutboxStore(field.control)
    root = outbox.legacy_root
    unreadable_root = root / LEGACY_UNREADABLE
    started = time.monotonic()
    moved: dict[str, int] = defaultdict(int)
    unreadable: dict[str, int] = defaultdict(int)
    marker_counts: dict[tuple[str, str], int] = defaultdict(int)
    for folder in ("pending", "leased", "sent", "refused"):
        directory = root / folder
        if directory.is_dir():
            with os.scandir(directory) as entries:
                batch = [
                    entry.name for entry in entries
                    if entry.name.endswith(".json") and entry.is_file(follow_symlinks=False)
                ][:batch_size]
            with exclusive_lock(outbox.lock):
                for name in batch:
                    path = directory / name
                    if not path.exists():
                        continue
                    row = _readable(path)
                    if not row.get("outbox_id"):
                        _quarantine(path, unreadable_root)
                        unreadable[str(row.get("worker_name") or "-").lower()] += 1
                        continue
                    if folder in ("pending", "leased"):
                        outbox.move_in_flight(path, row, folder)
                    else:
                        outbox.settle(path, row)
                    agent = agent_component(str(row.get("worker_name") or "-"))
                    project_ref = str(row.get("project_ref") or "")
                    moved[agent] += 1
                    marker_counts[(project_ref, agent)] += 1
    remaining = any(
        path
        for folder in ("pending", "leased", "sent", "refused")
        for path in (root / folder).glob("*.json")
    )
    state = "running" if remaining else "complete"
    if state == "complete":
        for project_ref, agent_root in outbox.agent_roots():
            if (agent_root / ".migration.json").is_file():
                marker_counts.setdefault((project_ref, agent_root.name), 0)
    for (project_ref, agent), count in marker_counts.items():
        marker = outbox.agent_root(project_ref, agent) / ".migration.json"
        prior = dict(read_json(marker, required=False) or {})
        atomic_write_json(
            marker,
            {
                "schema": "problem-board.outbox-store-migration.v1",
                "state": state,
                "moved": int(prior.get("moved") or 0) + count,
                "updated_at": utc_now(),
            },
        )
    elapsed = int((time.monotonic() - started) * 1000)
    for agent in sorted(set(moved) | set(unreadable)):
        logger.info(
            "relay store migrated worker=%s store=outbox moved=%d unreadable=%d ms=%d",
            agent, moved.get(agent, 0), unreadable.get(agent, 0), elapsed,
        )
    return {"state": state, "moved": dict(moved), "unreadable": dict(unreadable)}


# Stores read only by key (a message id, a content hash, a lease id), never
# listed: they cost no cycle time (LS3), so what they need is a bound (LS2).
# Age is the file's modification time, so no record is opened.
KEYED_STORE_RETENTION_DAYS = 30
JOURNAL_RECEIPT_RETENTION_DAYS = 90
_MAILBOX_SKIP = {"reconciliation-receipts", "ignored", "undeliverable"}


def keyed_stores(field: Any) -> list[tuple[str, str, Path, int]]:
    """``(store, agent, directory, retention_days)`` for every keyed store."""

    control = field.control
    found: list[tuple[str, str, Path, int]] = []
    workers = control / "workers"
    for worker in sorted(workers.glob("*")) if workers.is_dir() else ():
        if worker.is_dir():
            found.append(("handled", worker.name, worker / "handled", KEYED_STORE_RETENTION_DAYS))
            found.append(("idempotency-mail", worker.name, worker / "idempotency" / "mail", KEYED_STORE_RETENTION_DAYS))
            found.append(("idempotency-events", worker.name, worker / "idempotency" / "events", KEYED_STORE_RETENTION_DAYS))
            # Mail sent outside any project lives under the worker.
            found.append(("mail-processed", worker.name, worker / "mail" / "processed", KEYED_STORE_RETENTION_DAYS))
            found.append(("mail-by-control", worker.name, worker / "mail" / "by-control", KEYED_STORE_RETENTION_DAYS))
    responses = control / "operator-responses"
    for worker in sorted(responses.glob("*")) if responses.is_dir() else ():
        if worker.is_dir():
            found.append(("operator-responses", worker.name, worker, KEYED_STORE_RETENTION_DAYS))
    projects = control / "projects"
    for project in sorted(projects.glob("*")) if projects.is_dir() else ():
        if not (project / "project.json").is_file():
            continue
        for kind in ("mail", "assignment-report", "project-report"):
            found.append((f"idempotency-{kind}", "-", project / "idempotency" / kind, KEYED_STORE_RETENTION_DAYS))
        mail = project / "mail"
        for mailbox in sorted(mail.glob("*")) if mail.is_dir() else ():
            if mailbox.is_dir() and mailbox.name not in _MAILBOX_SKIP:
                found.append(("mail-processed", mailbox.name, mailbox / "processed", KEYED_STORE_RETENTION_DAYS))
                found.append(("mail-by-control", mailbox.name, mailbox / "by-control", KEYED_STORE_RETENTION_DAYS))
        undeliverable = mail / "undeliverable"
        for address in sorted(undeliverable.glob("*")) if undeliverable.is_dir() else ():
            if address.is_dir():
                found.append(("mail-undeliverable", address.name, address, KEYED_STORE_RETENTION_DAYS))
        found.append(("scope-leases-settled", "-", project / "scope-leases" / "settled", KEYED_STORE_RETENTION_DAYS))
        found.append(("journal-receipts", "-", project / "journals", JOURNAL_RECEIPT_RETENTION_DAYS))
    return found


def expire_keyed_stores(field: Any, *, now: datetime | None = None) -> dict[str, int]:
    """Remove keyed records older than their store's retention, by file age.

    A lock file of a journal-index operation goes only once its operation
    record is gone, so a lock never disappears under a running operation.
    """

    current = now or datetime.now(timezone.utc)
    totals: dict[str, int] = defaultdict(int)
    for history in field._mail_history().stores():
        removed = history.expire(now=current)
        totals[history.store] += removed["records"]
    for history in field._idempotency().stores():
        removed = history.expire(now=current)
        totals[history.store] += removed["records"]
    for history in field._journal_receipts().stores():
        removed = history.expire(now=current)
        totals[history.store] += removed["records"]
    for project_id in _project_ids(field):
        for history in field._scope_leases(project_id).stores():
            removed = history.expire(now=current)
            totals[history.store] += removed["records"]
    operation_retention = OperationStore(
        field.control / "operations" / "journal-index"
    ).expire(now=current)
    totals["journal-index-operations"] += operation_retention["records"]
    totals["journal-index-operation-pointers"] += operation_retention["pointers"]
    totals["journal-index-locks"] += operation_retention["locks"]
    for store, agent, directory, days in keyed_stores(field):
        if not directory.is_dir():
            continue
        cutoff = (current - timedelta(days=days)).timestamp()
        started = time.monotonic()
        examined = removed = 0
        with os.scandir(directory) as entries:
            for entry in entries:
                if not entry.name.endswith(".json") or not entry.is_file(follow_symlinks=False):
                    continue
                examined += 1
                try:
                    if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                        os.unlink(entry.path)
                        removed += 1
                except FileNotFoundError:
                    continue
        totals[store] += removed
        if removed:
            logger.info(
                "relay store read worker=%s store=%s op=retention range=..%s partitions=1 records=%d ms=%d removed=%d",
                agent, store, (current - timedelta(days=days)).strftime("%Y-%m-%dT%H"), examined,
                int((time.monotonic() - started) * 1000), removed,
            )
    return dict(totals)


def _handle_legacy_receipt(
    field: Any,
    project_id: str,
    path: Path,
    counts: dict[str, dict[str, int]],
) -> None:
    record = _readable(path)
    try:
        receipt = normalize_receipt((record or {}).get("receipt") or {})
    except (DomainError, ValueError, TypeError, KeyError):
        receipt = {}
    agent = str(receipt.get("reporter_worker_name") or "").strip().lower()
    if not record or not agent:
        unreadable = store_root(field, project_id) / LEGACY_UNREADABLE
        unreadable.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.replace(path, unreadable / path.name)
        counts[agent or "-"]["unreadable"] += 1
        return
    outbox_ids = [
        str(value)
        for value in (record.get("publication") or {}).get("outbox_ids") or []
        if value
    ]
    if receipt_carries_information(receipt):
        target = pending_path(field, project_id, agent, receipt["receipt_id"])
        if not target.exists():
            atomic_write_json(target, record)
        path.unlink(missing_ok=True)
        settle_if_terminal(field, project_id, worker_name=agent, path=target)
        counts[agent]["moved"] += 1
        return
    outbox = OutboxStore(field.control)
    with exclusive_lock(outbox.lock):
        # A row may still be in the flat pre-2b folders or already in the
        # per-agent layout; find() looks in both by id.
        found = [outbox.find(outbox_id, worker_name=agent) for outbox_id in outbox_ids]
        if any(item is not None and item[1] == "leased" for item in found):
            counts[agent]["deferred"] += 1
            return
        for item in found:
            if item is not None and item[0].exists():
                item[0].unlink()
                counts[agent]["outbox_rows_deleted"] += 1
    path.unlink(missing_ok=True)
    counts[agent]["deleted"] += 1


def _record_progress(
    path: Path,
    progress: dict[str, Any],
    base_agents: dict[str, dict[str, Any]],
    counts: dict[str, dict[str, int]],
    *,
    state: str,
) -> None:
    # Totals across passes: what earlier passes recorded plus this pass so far.
    agents = {agent: dict(values) for agent, values in base_agents.items()}
    for agent, values in counts.items():
        merged = dict(agents.get(agent) or {})
        for key, value in values.items():
            merged[key] = int(merged.get(key) or 0) + int(value)
        agents[agent] = merged
    progress.update(
        schema="problem-board.legacy-reconciliation-cleanup.v1",
        state=state,
        agents=agents,
        started_at=str(progress.get("started_at") or utc_now()),
        updated_at=utc_now(),
    )
    if state == "complete":
        progress["completed_at"] = utc_now()
    atomic_write_json(path, progress)


def _readable(path: Path) -> dict[str, Any]:
    """A record, or ``{}`` when the file is gone, empty or not valid JSON."""

    try:
        return dict(read_json(path, required=False) or {})
    except DomainError:
        return {}


def _quarantine(path: Path, root: Path) -> None:
    """Move a record a migration cannot read out of its way, kept for a person.

    Leaving it in place would make every later batch meet it again: with a
    batch's worth of them the migration never ends (review on W287 2b).
    """

    target = root / path.parent.name
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.replace(path, target / path.name)


def _project_ids(field: Any) -> list[str]:
    projects = field.control / "projects"
    if not projects.is_dir():
        return []
    return sorted(
        child.name for child in projects.iterdir()
        if child.is_dir() and (child / "project.json").is_file()
    )


__all__ = [
    "OUTBOX_TERMINAL_RETENTION_DAYS",
    "RETENTION_INTERVAL_SECONDS",
    "SESSION_MAX_RECORDS_PER_WORKER",
    "SESSION_RETENTION_DAYS",
    "apply_outbox_retention",
    "apply_session_retention",
    "cleanup_legacy_receipts",
    "expire_keyed_stores",
    "keyed_stores",
    "migrate_flat_outbox",
    "migrate_flat_events",
    "run_local_state_maintenance",
]

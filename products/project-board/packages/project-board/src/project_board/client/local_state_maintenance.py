"""Relay-local housekeeping that runs beside the relay cycle, never inside it.

Everything here can cost time in proportion to history, so the relay runs it
in a worker thread on its own schedule (rule LS5 in
``docs/project-board/storage-and-retention.md``, "Relay Local State"). Whether
retention is due is read from a file in the field, not from process memory, so
a restart neither repeats it nor skips it (LS4).

Three jobs:

- the one-time cleanup of the flat ``mail/reconciliation-receipts/`` directory
  that every relay before W287 filled with one receipt per run: empty receipts
  and their publication rows are deleted, receipts that carry information move
  into the per-agent layout of :mod:`reconciliation_receipts`;
- receipt retention by hour folder (:func:`reconciliation_receipts.apply_receipt_retention`);
- outbox retention for settled rows (``sent/`` and ``refused/``).
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..contract.errors import DomainError
from ..contract.mailbox_reconciliation_contract import normalize_receipt
from .io import atomic_write_json, exclusive_lock, parse_utc, read_json, utc_now
from .outbox_layout import OUTBOX_FOLDERS, OUTBOX_TERMINAL_FOLDERS
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
OUTBOX_TERMINAL_RETENTION_DAYS = 30
LEGACY_RECEIPTS = ("mail", "reconciliation-receipts")
LEGACY_CLAIMED = ".legacy-claimed"
LEGACY_UNREADABLE = ".legacy-unreadable"
LEGACY_PROGRESS = ".legacy-cleanup.json"
LEGACY_BATCH_SIZE = 1000


def run_local_state_maintenance(field: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """One maintenance pass over every project in this field."""

    current = now or datetime.now(timezone.utc)
    summary: dict[str, Any] = {"legacy_receipts": {}, "retention": None}
    for project_id in _project_ids(field):
        summary["legacy_receipts"][project_id] = cleanup_legacy_receipts(field, project_id)
    state_path = field.control / MAINTENANCE_STATE
    state = dict(read_json(state_path, required=False) or {})
    last = parse_utc(str(state.get("retention_ran_at") or "")) if state.get("retention_ran_at") else None
    if last is None or (current - last).total_seconds() >= RETENTION_INTERVAL_SECONDS:
        retention: dict[str, Any] = {
            "receipts": {
                project_id: apply_receipt_retention(field, project_id, now=current)
                for project_id in _project_ids(field)
            },
            "outbox": apply_outbox_retention(field, now=current),
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
    for path in sorted(claimed_root.glob("*.json")):
        _handle_legacy_receipt(field, project_id, path, counts)
    while legacy.is_dir():
        batch: list[str] = []
        with os.scandir(legacy) as entries:
            for entry in entries:
                if entry.name.endswith(".json") and entry.is_file(follow_symlinks=False):
                    batch.append(entry.name)
                    if len(batch) >= batch_size:
                        break
        if not batch:
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
    for directory in (legacy, claimed_root):
        try:
            directory.rmdir()
        except OSError:
            pass
    state = "complete" if not legacy.exists() and remaining == 0 else "running"
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


def apply_outbox_retention(field: Any, *, now: datetime | None = None) -> dict[str, int]:
    """Remove settled outbox rows older than the retention window.

    Age is the file's modification time, which the relay sets when it settles
    the row, so no row is opened. Pending and leased rows are never touched.
    """

    current = now or datetime.now(timezone.utc)
    cutoff = (current - timedelta(days=OUTBOX_TERMINAL_RETENTION_DAYS)).timestamp()
    root = field.control / "outbox"
    totals: dict[str, int] = {}
    started = time.monotonic()
    examined = 0
    for folder in OUTBOX_TERMINAL_FOLDERS:
        directory = root / folder
        removed = 0
        if directory.is_dir():
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
                with exclusive_lock(root / ".outbox.lock"):
                    for path in expired[start:start + 500]:
                        path.unlink(missing_ok=True)
                        removed += 1
        totals[folder] = removed
    logger.info(
        "relay store read worker=* store=outbox op=retention range=..%s partitions=%d records=%d ms=%d removed=%d",
        datetime.fromtimestamp(cutoff, timezone.utc).strftime("%Y-%m-%dT%H"),
        len(OUTBOX_TERMINAL_FOLDERS), examined, int((time.monotonic() - started) * 1000),
        sum(totals.values()),
    )
    return totals


def _handle_legacy_receipt(
    field: Any,
    project_id: str,
    path: Path,
    counts: dict[str, dict[str, int]],
) -> None:
    record = read_json(path, required=False)
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
    root = field.control / "outbox"
    with exclusive_lock(root / ".outbox.lock"):
        if any((root / "leased" / f"{outbox_id}.json").exists() for outbox_id in outbox_ids):
            counts[agent]["deferred"] += 1
            return
        for outbox_id in outbox_ids:
            for folder in OUTBOX_FOLDERS:
                row_path = root / folder / f"{outbox_id}.json"
                if row_path.exists():
                    row_path.unlink()
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
    "apply_outbox_retention",
    "cleanup_legacy_receipts",
    "run_local_state_maintenance",
]

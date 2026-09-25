"""Active source-scope leases and partitioned terminal history (W287)."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Iterator, Mapping

from ..contract.errors import DomainError
from .history_migration import migrate_flat_history
from .io import atomic_write_json, read_json, utc_now
from .keyed_history import KeyedHistoryStore
from .local_store import PartitionedStore, agent_component


logger = logging.getLogger(__name__)

STORE = "scope-leases"
RETENTION_DAYS = 30
MAX_BYTES_PER_AGENT = 50 * 1024 * 1024
MAX_RECORDS_PER_AGENT = 50_000
MIGRATION_SCHEMA = "problem-board.scope-lease-migration.v1"


class ScopeLeaseStore:
    """Keep only active leases in ``pending/`` and partition settled leases."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.reads = PartitionedStore(self.root, store=STORE)
        self.history = KeyedHistoryStore(
            self.root,
            store=STORE,
            retention_days=RETENTION_DAYS,
            max_bytes_per_agent=MAX_BYTES_PER_AGENT,
            max_records_per_agent=MAX_RECORDS_PER_AGENT,
        )

    @property
    def lock(self) -> Path:
        return self.root / ".scope-leases.lock"

    def pending_path(self, worker_name: str, lease_id: str) -> Path:
        return self.root / agent_component(worker_name) / "pending" / f"{lease_id}.json"

    def write_active(self, row: Mapping[str, Any]) -> Path:
        worker = str(row.get("worker_name") or "").strip()
        lease_id = str(row.get("lease_id") or "").strip()
        if not worker or not lease_id:
            raise ValueError("an active scope lease needs worker_name and lease_id")
        path = self.pending_path(worker, lease_id)
        atomic_write_json(path, row)
        (self.root / "active" / f"{lease_id}.json").unlink(missing_ok=True)
        return path

    def active(self) -> list[tuple[Path, dict[str, Any]]]:
        rows: list[tuple[Path, dict[str, Any]]] = []
        with self.reads.reading("pending") as read:
            for agent in self.reads.agents():
                directory = self.root / agent / "pending"
                paths = sorted(directory.glob("*.json")) if directory.is_dir() else []
                read.opened_pending(agent, len(paths))
                for path in paths:
                    row = read_json(path, required=False)
                    if isinstance(row, Mapping) and row:
                        rows.append((path, dict(row)))
        # Active rows are in-flight state. Reading this legacy folder remains
        # proportional to current work while the synchronous migration finishes.
        legacy = self.root / "active"
        for path in sorted(legacy.glob("*.json")) if legacy.is_dir() else ():
            row = read_json(path, required=False)
            if isinstance(row, Mapping) and row:
                rows.append((path, dict(row)))
        return rows

    def read_active(self, worker_name: str, lease_id: str) -> tuple[Path, dict[str, Any]] | None:
        path = self.pending_path(worker_name, lease_id)
        with self.reads.reading("lookup") as read:
            read.opened_pending(agent_component(worker_name), int(path.is_file()))
        if not path.is_file():
            legacy = self.root / "active" / f"{lease_id}.json"
            path = legacy if legacy.is_file() else path
        row = read_json(path, required=False)
        return (path, dict(row)) if isinstance(row, Mapping) and row else None

    def settle(self, source: Path, row: Mapping[str, Any]) -> Path:
        worker = str(row.get("worker_name") or "-")
        lease_id = str(row.get("lease_id") or "").strip()
        if not lease_id:
            raise ValueError("a settled scope lease needs lease_id")
        path = self.history.write(
            agent=worker,
            record_id=lease_id,
            row=row,
            slug=str(row.get("state") or "settled"),
        )
        source.unlink(missing_ok=True)
        (self.root / "settled" / f"{lease_id}.json").unlink(missing_ok=True)
        return path

    def migrate_legacy(self, *, batch_size: int = 1000) -> dict[str, Any]:
        active = self._migrate_active(batch_size=batch_size)
        settled = migrate_flat_history(
            history=self.history,
            legacy=self.root / "settled",
            agent_for=lambda row, _path: str(row.get("worker_name") or "-"),
            batch_size=batch_size,
        )
        return {"active": active, "settled": settled}

    def _migrate_active(self, *, batch_size: int) -> dict[str, Any]:
        legacy = self.root / "active"
        if not legacy.is_dir():
            return {"state": "absent", "moved": {}, "unreadable": 0}
        moved: dict[str, int] = {}
        unreadable = 0
        paths = list(sorted(legacy.glob("*.json")))[: max(1, int(batch_size))]
        for path in paths:
            try:
                row = read_json(path, required=False)
            except DomainError:
                row = None
            if not isinstance(row, Mapping) or not row.get("worker_name") or not row.get("lease_id"):
                quarantine = self.root / ".legacy-unreadable" / "active"
                quarantine.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.replace(path, quarantine / path.name)
                unreadable += 1
                continue
            agent = agent_component(str(row["worker_name"]))
            self.write_active(row)
            moved[agent] = moved.get(agent, 0) + 1
        remaining = any(legacy.glob("*.json"))
        state = "running" if remaining else "complete"
        for agent, count in moved.items():
            marker = self.root / agent / ".migration.json"
            prior = dict(read_json(marker, required=False) or {})
            atomic_write_json(
                marker,
                {
                    "schema": MIGRATION_SCHEMA,
                    "state": state,
                    "active_moved": int(prior.get("active_moved") or 0) + count,
                    "updated_at": utc_now(),
                },
            )
            logger.info(
                "relay store migrated worker=%s store=%s-active moved=%d unreadable=0",
                agent,
                STORE,
                count,
            )
        return {"state": state, "moved": moved, "unreadable": unreadable}

    def stores(self) -> Iterator[KeyedHistoryStore]:
        if self.root.is_dir():
            yield self.history


__all__ = [
    "MAX_BYTES_PER_AGENT",
    "MAX_RECORDS_PER_AGENT",
    "RETENTION_DAYS",
    "STORE",
    "ScopeLeaseStore",
]

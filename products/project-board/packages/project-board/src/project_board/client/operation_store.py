"""Pending and terminal local workflow operations, with exact step indexes."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Mapping

from ..contract.errors import DomainError
from .io import atomic_write_json, content_hash, read_json, utc_now
from .keyed_history import KeyedHistoryStore


logger = logging.getLogger(__name__)

OPERATION_STORE = "journal-index-operations"
POINTER_STORE = "journal-index-operation-pointers"
RETENTION_DAYS = 30
MAX_BYTES = 50 * 1024 * 1024
MAX_RECORDS = 25_000
PROJECT_AGENT = "-"
MIGRATION_SCHEMA = "problem-board.operation-store-migration.v1"


class OperationStore:
    """Crash-resumable operations in pending/, completed rows by hour."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.history = KeyedHistoryStore(
            self.root,
            store=OPERATION_STORE,
            retention_days=RETENTION_DAYS,
            max_bytes_per_agent=MAX_BYTES,
            max_records_per_agent=MAX_RECORDS,
        )
        self.pointers = KeyedHistoryStore(
            self.root / "by-step",
            store=POINTER_STORE,
            retention_days=RETENTION_DAYS,
            max_bytes_per_agent=MAX_BYTES,
            max_records_per_agent=MAX_RECORDS,
        )

    def pending_path(self, operation_id: str) -> Path:
        return self.root / PROJECT_AGENT / "pending" / f"{operation_id}.json"

    def legacy_path(self, operation_id: str) -> Path:
        return self.root / f"{operation_id}.json"

    def find(self, operation_id: str) -> Path | None:
        pending = self.pending_path(operation_id)
        if pending.is_file():
            return pending
        return self.history.find(
            agent=PROJECT_AGENT,
            record_id=operation_id,
            legacy_paths=[self.legacy_path(operation_id)],
        )

    def read(self, operation_id: str, *, required: bool = True) -> dict[str, Any] | None:
        path = self.find(operation_id)
        if path is None:
            if required:
                return read_json(self.pending_path(operation_id))
            return None
        row = read_json(path, required=required)
        return dict(row) if isinstance(row, Mapping) and row else None

    def write(self, row: Mapping[str, Any]) -> Path:
        operation_id = str(row.get("operation_id") or "").strip()
        if not operation_id:
            raise ValueError("an operation needs operation_id")
        if str(row.get("state") or "") == "completed":
            path = self.history.write(
                agent=PROJECT_AGENT,
                record_id=operation_id,
                row=row,
                slug=str(row.get("kind") or "operation"),
            )
            self.pending_path(operation_id).unlink(missing_ok=True)
        else:
            path = self.pending_path(operation_id)
            atomic_write_json(path, row)
        self.legacy_path(operation_id).unlink(missing_ok=True)
        self._index_steps(row)
        return path

    def find_by_step_value(
        self,
        step_name: str,
        field: str,
        value: str,
    ) -> dict[str, Any] | None:
        token = self.pointer_token(step_name, field, value)
        pointer = self.pointers.read(
            agent=PROJECT_AGENT,
            record_id=token,
        )
        if not pointer:
            return None
        return self.read(str(pointer.get("operation_id") or ""), required=False)

    @staticmethod
    def pointer_token(step_name: str, field: str, value: str) -> str:
        return content_hash(
            {"step": str(step_name), "field": str(field), "value": str(value)}
        )

    def _index_steps(self, row: Mapping[str, Any]) -> None:
        operation_id = str(row.get("operation_id") or "")
        for step in row.get("steps") or []:
            if not isinstance(step, Mapping):
                continue
            name = str(step.get("name") or "")
            for container_name in ("intent", "result"):
                container = step.get(container_name)
                if not isinstance(container, Mapping):
                    continue
                for field, value in container.items():
                    if value is None or isinstance(value, (Mapping, list, tuple, set)):
                        continue
                    token = self.pointer_token(name, str(field), str(value))
                    self.pointers.write(
                        agent=PROJECT_AGENT,
                        record_id=token,
                        row={
                            "operation_id": operation_id,
                            "step": name,
                            "field": str(field),
                            "value": str(value),
                            "created_at": str(row.get("created_at") or utc_now()),
                            "updated_at": utc_now(),
                        },
                        slug=f"{name}-{field}",
                    )

    def migrate_legacy(self, *, batch_size: int = 1000) -> dict[str, Any]:
        paths = [
            path
            for path in sorted(self.root.glob("*.json"))
            if not path.name.startswith(".")
        ][: max(1, int(batch_size))]
        moved = unreadable = 0
        for path in paths:
            try:
                row = read_json(path, required=False)
            except DomainError:
                row = None
            if not isinstance(row, Mapping) or not row.get("operation_id"):
                quarantine = self.root / ".legacy-unreadable"
                quarantine.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.replace(path, quarantine / path.name)
                unreadable += 1
                continue
            self.write(row)
            path.unlink(missing_ok=True)
            moved += 1
        remaining = any(
            path for path in self.root.glob("*.json") if not path.name.startswith(".")
        )
        state = "running" if remaining else "complete"
        marker = self.root / PROJECT_AGENT / ".migration.json"
        prior = dict(read_json(marker, required=False) or {})
        atomic_write_json(
            marker,
            {
                "schema": MIGRATION_SCHEMA,
                "state": state,
                "moved": int(prior.get("moved") or 0) + moved,
                "unreadable": int(prior.get("unreadable") or 0) + unreadable,
                "updated_at": utc_now(),
            },
        )
        if moved or unreadable:
            logger.info(
                "relay store migrated worker=- store=%s moved=%d unreadable=%d",
                OPERATION_STORE,
                moved,
                unreadable,
            )
        return {"state": state, "moved": moved, "unreadable": unreadable}

    def expire(self, *, now=None) -> dict[str, int]:
        operations = self.history.expire(now=now)
        pointers = self.pointers.expire(now=now)
        locks = self._expire_orphan_locks()
        return {
            "records": operations["records"],
            "partitions": operations["partitions"],
            "pointers": pointers["records"],
            "locks": locks,
        }

    def _expire_orphan_locks(self) -> int:
        removed = 0
        locks = self.root / "locks"
        for lock in sorted(locks.iterdir()) if locks.is_dir() else ():
            operation_id = lock.name.split(".", 1)[0]
            if self.find(operation_id) is None:
                lock.unlink(missing_ok=True)
                removed += 1
        return removed


__all__ = [
    "MAX_BYTES",
    "MAX_RECORDS",
    "OPERATION_STORE",
    "OperationStore",
    "POINTER_STORE",
    "RETENTION_DAYS",
]

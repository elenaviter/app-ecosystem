"""Active assignment projections, partitioned by worker (W287).

Assignments in the local field are an in-flight projection of server-owned
assignment rows.  Only active projections are retained, so they live under
``assignments/<worker>/pending`` and every cycle reads pending work only.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping

from ..contract.errors import DomainError
from .io import atomic_write_json, read_json
from .local_store import PartitionedStore, agent_component


logger = logging.getLogger(__name__)
MIGRATION_SCHEMA = "problem-board.assignment-store-migration.v1"


class AssignmentStore:
    """The active assignment projection for one project."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.reads = PartitionedStore(self.root, store="assignments")

    def pending_dir(self, worker_name: str) -> Path:
        return self.root / agent_component(worker_name) / "pending"

    def pending_path(self, worker_name: str, assignment_id: str) -> Path:
        return self.pending_dir(worker_name) / f"{assignment_id}.json"

    def legacy_path(self, assignment_id: str) -> Path:
        return self.root / f"{assignment_id}.json"

    def find(self, assignment_id: str, *, worker_name: str = "") -> Path | None:
        """Find one active projection without reading terminal history."""

        if worker_name:
            path = self.pending_path(worker_name, assignment_id)
            with self.reads.reading("lookup", key=assignment_id) as read:
                read.opened_pending(agent_component(worker_name), int(path.is_file()))
            if path.is_file():
                return path
        else:
            with self.reads.reading("lookup", key=assignment_id) as read:
                for agent in self.reads.agents():
                    path = self.root / agent / "pending" / f"{assignment_id}.json"
                    read.opened_pending(agent, int(path.is_file()))
                    if path.is_file():
                        return path
        legacy = self.legacy_path(assignment_id)
        return legacy if legacy.is_file() else None

    def read(self, assignment_id: str, *, worker_name: str = "") -> dict[str, Any] | None:
        path = self.find(assignment_id, worker_name=worker_name)
        row = read_json(path, required=False) if path is not None else None
        return dict(row) if isinstance(row, Mapping) and row else None

    def write(self, row: Mapping[str, Any]) -> Path:
        assignment_id = str(row.get("assignment_id") or "").strip()
        worker_name = str(row.get("worker_name") or "").strip()
        if not assignment_id or not worker_name:
            raise ValueError("an active assignment needs assignment_id and worker_name")
        path = self.pending_path(worker_name, assignment_id)
        atomic_write_json(path, row)
        legacy = self.legacy_path(assignment_id)
        if legacy != path:
            legacy.unlink(missing_ok=True)
        return path

    def remove(self, assignment_id: str, *, worker_name: str = "") -> bool:
        path = self.find(assignment_id, worker_name=worker_name)
        if path is None:
            return False
        path.unlink(missing_ok=True)
        return True

    def list_pending(self, *, worker_name: str = "") -> list[dict[str, Any]]:
        agents = [agent_component(worker_name)] if worker_name else self.reads.agents()
        rows: list[dict[str, Any]] = []
        with self.reads.reading("pending") as read:
            for agent in agents:
                directory = self.root / agent / "pending"
                paths = sorted(directory.glob("*.json")) if directory.is_dir() else []
                read.opened_pending(agent, len(paths))
                for path in paths:
                    row = read_json(path, required=False)
                    if isinstance(row, Mapping) and row:
                        rows.append(dict(row))
        # Exact-name fallback while background migration is incomplete.  This
        # is active work only; there is no terminal assignment history here.
        for path in sorted(self.root.glob("*.json")):
            if path.name.startswith("."):
                continue
            row = read_json(path, required=False)
            if not isinstance(row, Mapping) or not row:
                continue
            if worker_name and agent_component(str(row.get("worker_name") or "")) != agents[0]:
                continue
            rows.append(dict(row))
        return rows

    def migrate_legacy(self, *, batch_size: int = 1000) -> dict[str, Any]:
        """Move flat active projections in bounded batches, with durable counts."""

        moved: dict[str, int] = defaultdict(int)
        unreadable = 0
        paths = [
            path
            for path in sorted(self.root.glob("*.json"))
            if not path.name.startswith(".")
        ][: max(1, int(batch_size))]
        quarantine = self.root / ".legacy-unreadable" / "assignments"
        for path in paths:
            try:
                row = read_json(path, required=False)
            except DomainError:
                row = None
            if not isinstance(row, Mapping) or not row.get("assignment_id") or not row.get("worker_name"):
                quarantine.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.replace(path, quarantine / path.name)
                unreadable += 1
                continue
            agent = agent_component(str(row["worker_name"]))
            self.write(row)
            moved[agent] += 1
        remaining = any(
            path for path in self.root.glob("*.json") if not path.name.startswith(".")
        )
        state = "running" if remaining else "complete"
        agents = set(moved) | set(self.reads.agents())
        for agent in sorted(agents):
            marker = self.root / agent / ".migration.json"
            prior = dict(read_json(marker, required=False) or {})
            atomic_write_json(
                marker,
                {
                    "schema": MIGRATION_SCHEMA,
                    "state": state,
                    "moved": int(prior.get("moved") or 0) + moved.get(agent, 0),
                    "unreadable": int(prior.get("unreadable") or 0),
                },
            )
            if moved.get(agent):
                logger.info(
                    "relay store migrated worker=%s store=assignments moved=%d unreadable=0",
                    agent,
                    moved[agent],
                )
        if unreadable:
            logger.warning(
                "relay store migrated worker=- store=assignments moved=0 unreadable=%d",
                unreadable,
            )
        return {"state": state, "moved": dict(moved), "unreadable": unreadable}


__all__ = ["AssignmentStore", "MIGRATION_SCHEMA"]

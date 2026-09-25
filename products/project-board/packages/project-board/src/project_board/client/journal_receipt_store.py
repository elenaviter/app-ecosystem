"""Partitioned local receipts for indexed work-journal entries (W287)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, Mapping

from .history_migration import migrate_flat_history
from .keyed_history import KeyedHistoryStore


JOURNAL_RECEIPT_RETENTION_DAYS = 90
JOURNAL_RECEIPT_MAX_BYTES = 100 * 1024 * 1024
JOURNAL_RECEIPT_MAX_RECORDS = 50_000
STORE = "journal-receipts"
PROJECT_AGENT = "-"


class JournalReceiptStore:
    """Receipts keyed by entry id, matching the read API's lookup dimension."""

    def __init__(self, control: str | Path) -> None:
        self.control = Path(control)

    def history(self, project_id: str) -> KeyedHistoryStore:
        return KeyedHistoryStore(
            self.control / "projects" / project_id / STORE,
            store=STORE,
            retention_days=JOURNAL_RECEIPT_RETENTION_DAYS,
            max_bytes_per_agent=JOURNAL_RECEIPT_MAX_BYTES,
            max_records_per_agent=JOURNAL_RECEIPT_MAX_RECORDS,
        )

    def legacy_path(self, project_id: str, entry_id: str) -> Path:
        return self.control / "projects" / project_id / "journals" / f"{entry_id}.json"

    def read(self, project_id: str, entry_id: str) -> dict[str, Any] | None:
        return self.history(project_id).read(
            agent=PROJECT_AGENT,
            record_id=entry_id,
            legacy_paths=[self.legacy_path(project_id, entry_id)],
        )

    def write(
        self,
        project_id: str,
        entry_id: str,
        row: Mapping[str, Any],
    ) -> Path:
        path = self.history(project_id).write(
            agent=PROJECT_AGENT,
            record_id=entry_id,
            row=row,
            slug="journal-receipt",
        )
        self.legacy_path(project_id, entry_id).unlink(missing_ok=True)
        return path

    def list(
        self,
        project_id: str,
        *,
        work_ref: str = "",
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        rows = self.history(project_id).newest(
            limit=(
                JOURNAL_RECEIPT_MAX_RECORDS
                if limit is None
                else max(1, int(limit))
            ),
            agents=[PROJECT_AGENT],
            predicate=(
                (lambda row: str(row.get("work_ref") or "") == work_ref)
                if work_ref
                else None
            ),
            op="list",
        )
        return sorted(rows, key=lambda row: str(row.get("created_at") or ""))

    def migrate_legacy(self, *, batch_size: int = 1000) -> dict[str, Any]:
        results: dict[str, Any] = {}
        projects = self.control / "projects"
        for project in sorted(projects.iterdir()) if projects.is_dir() else ():
            if not project.is_dir() or not (project / "project.json").is_file():
                continue
            results[project.name] = migrate_flat_history(
                history=self.history(project.name),
                legacy=project / "journals",
                agent_for=lambda _row, _path: PROJECT_AGENT,
                batch_size=batch_size,
            )
        return results

    def stores(self) -> Iterator[KeyedHistoryStore]:
        projects = self.control / "projects"
        for project in sorted(projects.iterdir()) if projects.is_dir() else ():
            if (project / STORE).is_dir():
                yield self.history(project.name)


__all__ = [
    "JOURNAL_RECEIPT_MAX_BYTES",
    "JOURNAL_RECEIPT_MAX_RECORDS",
    "JOURNAL_RECEIPT_RETENTION_DAYS",
    "JournalReceiptStore",
    "PROJECT_AGENT",
    "STORE",
]

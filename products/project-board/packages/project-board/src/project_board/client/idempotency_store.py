"""Partitioned idempotency receipts for relay-local effects (W287)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .history_migration import migrate_flat_history
from .io import content_hash
from .keyed_history import KeyedHistoryStore
from .local_store import agent_component


IDEMPOTENCY_RETENTION_DAYS = 30
IDEMPOTENCY_MAX_BYTES_PER_AGENT = 50 * 1024 * 1024
IDEMPOTENCY_MAX_RECORDS_PER_AGENT = 50_000
IDEMPOTENCY_FAMILIES = frozenset(
    {
        "assignment-report-idempotency",
        "event-idempotency",
        "project-report-idempotency",
    }
)


class IdempotencyStore:
    """Exact-key receipts, partitioned by project, family, agent and hour."""

    def __init__(self, control: str | Path) -> None:
        self.control = Path(control)

    def root(self, project_id: str, family: str) -> Path:
        base = (
            self.control / "projects" / project_id
            if str(project_id or "").strip()
            else self.control / "unscoped"
        )
        return base / family

    def family(self, project_id: str, family: str) -> KeyedHistoryStore:
        return KeyedHistoryStore(
            self.root(project_id, family),
            store=family,
            retention_days=IDEMPOTENCY_RETENTION_DAYS,
            max_bytes_per_agent=IDEMPOTENCY_MAX_BYTES_PER_AGENT,
            max_records_per_agent=IDEMPOTENCY_MAX_RECORDS_PER_AGENT,
        )

    @staticmethod
    def event_token(worker_name: str, key: str) -> str:
        clean = agent_component(worker_name)
        return content_hash({"worker": clean, "key": str(key)})

    @staticmethod
    def assignment_token(
        assignment_id: str,
        ownership_version: int,
        report_key: str = "",
    ) -> str:
        identity: dict[str, Any] = {
            "assignment_id": str(assignment_id),
            "ownership_version": int(ownership_version),
        }
        if report_key:
            identity["report_key"] = str(report_key)
        return content_hash(identity)

    def read(
        self,
        *,
        project_id: str,
        family: str,
        agent: str,
        record_id: str,
        legacy_record_ids: Sequence[str] = (),
    ) -> dict[str, Any] | None:
        return self.family(project_id, family).read(
            agent=agent,
            record_id=record_id,
            fallback_agents=self._fallback_agents(agent),
            legacy_paths=self._legacy_paths(
                project_id,
                family,
                agent,
                (record_id, *legacy_record_ids),
            ),
        )

    def find(
        self,
        *,
        project_id: str,
        family: str,
        agent: str,
        record_id: str,
        legacy_record_ids: Sequence[str] = (),
    ) -> Path | None:
        return self.family(project_id, family).find(
            agent=agent,
            record_id=record_id,
            fallback_agents=self._fallback_agents(agent),
            legacy_paths=self._legacy_paths(
                project_id,
                family,
                agent,
                (record_id, *legacy_record_ids),
            ),
        )

    def write(
        self,
        *,
        project_id: str,
        family: str,
        agent: str,
        record_id: str,
        row: Mapping[str, Any],
    ) -> Path:
        history = self.family(project_id, family)
        for fallback in self._fallback_agents(agent):
            history.remove(agent=fallback, record_id=record_id)
        path = history.write(
            agent=agent,
            record_id=record_id,
            row=row,
            slug=family.removesuffix("-idempotency"),
        )
        for legacy in self._legacy_paths(
            project_id,
            family,
            agent,
            (record_id,),
        ):
            legacy.unlink(missing_ok=True)
        return path

    def remove(
        self,
        *,
        project_id: str,
        family: str,
        agent: str,
        record_id: str,
    ) -> bool:
        return self.family(project_id, family).remove(
            agent=agent,
            record_id=record_id,
            fallback_agents=self._fallback_agents(agent),
            legacy_paths=self._legacy_paths(
                project_id,
                family,
                agent,
                (record_id,),
            ),
        )

    def stores(self) -> Iterator[KeyedHistoryStore]:
        roots = [self.control / "unscoped"]
        projects = self.control / "projects"
        if projects.is_dir():
            roots.extend(
                project
                for project in sorted(projects.iterdir())
                if project.is_dir() and (project / "project.json").is_file()
            )
        for root in roots:
            for family in sorted(IDEMPOTENCY_FAMILIES):
                if (root / family).is_dir():
                    yield KeyedHistoryStore(
                        root / family,
                        store=family,
                        retention_days=IDEMPOTENCY_RETENTION_DAYS,
                        max_bytes_per_agent=IDEMPOTENCY_MAX_BYTES_PER_AGENT,
                        max_records_per_agent=IDEMPOTENCY_MAX_RECORDS_PER_AGENT,
                    )

    def migrate_legacy(self, *, batch_size: int = 1000) -> dict[str, Any]:
        results: dict[str, Any] = {}
        workers = self.control / "workers"
        for worker in sorted(workers.iterdir()) if workers.is_dir() else ():
            if not worker.is_dir():
                continue
            key = f"unscoped:{worker.name}:event-idempotency"
            results[key] = migrate_flat_history(
                history=self.family("", "event-idempotency"),
                legacy=worker / "idempotency" / "events",
                agent_for=lambda row, _path, fallback=worker.name: str(
                    row.get("worker_name") or fallback
                ),
                batch_size=batch_size,
            )

        projects = self.control / "projects"
        for project in sorted(projects.iterdir()) if projects.is_dir() else ():
            if not project.is_dir() or not (project / "project.json").is_file():
                continue
            for family, legacy_name in (
                ("assignment-report-idempotency", "assignment-report"),
                ("project-report-idempotency", "project-report"),
            ):
                key = f"{project.name}:-:{family}"
                results[key] = migrate_flat_history(
                    history=self.family(project.name, family),
                    legacy=project / "idempotency" / legacy_name,
                    agent_for=lambda row, _path: str(row.get("worker_name") or "-"),
                    record_id_for=(
                        self._assignment_record_id
                        if family == "assignment-report-idempotency"
                        else None
                    ),
                    batch_size=batch_size,
                )
        return results

    @classmethod
    def _assignment_record_id(cls, row: Mapping[str, Any], path: Path) -> str:
        assignment_id = str(row.get("assignment_id") or "").strip()
        version = int(row.get("ownership_version") or 0)
        report_key = str(row.get("report_key") or "").strip()
        if not assignment_id or version < 1:
            return path.stem
        return cls.assignment_token(assignment_id, version, report_key)

    @staticmethod
    def _fallback_agents(agent: str) -> tuple[str, ...]:
        return ("-",) if agent_component(agent) != "-" else ()

    def _legacy_paths(
        self,
        project_id: str,
        family: str,
        agent: str,
        record_ids: Sequence[str],
    ) -> list[Path]:
        if family == "event-idempotency":
            root = (
                self.control
                / "workers"
                / agent_component(agent)
                / "idempotency"
                / "events"
            )
        elif family == "assignment-report-idempotency":
            root = (
                self.control
                / "projects"
                / project_id
                / "idempotency"
                / "assignment-report"
            )
        elif family == "project-report-idempotency":
            root = (
                self.control
                / "projects"
                / project_id
                / "idempotency"
                / "project-report"
            )
        else:
            raise ValueError(f"unknown idempotency family: {family}")
        return [root / f"{record_id}.json" for record_id in dict.fromkeys(record_ids)]


__all__ = [
    "IDEMPOTENCY_FAMILIES",
    "IDEMPOTENCY_MAX_BYTES_PER_AGENT",
    "IDEMPOTENCY_MAX_RECORDS_PER_AGENT",
    "IDEMPOTENCY_RETENTION_DAYS",
    "IdempotencyStore",
]

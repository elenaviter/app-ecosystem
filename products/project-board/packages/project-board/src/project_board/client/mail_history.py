"""Partitioned terminal mail and mail-adjacent relay records (W287)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, Mapping

from .history_migration import migrate_flat_history
from .io import atomic_write_json, read_json
from .keyed_history import KeyedHistoryStore
from .local_store import agent_component


MAIL_HISTORY_RETENTION_DAYS = 30
MAIL_HISTORY_MAX_BYTES_PER_AGENT = 100 * 1024 * 1024
MAIL_HISTORY_MAX_RECORDS_PER_AGENT = 50_000
MAIL_HISTORY_FAMILIES = frozenset(
    {
        "handled",
        "mail-by-control",
        "mail-idempotency",
        "mail-ignored",
        "mail-processed",
        "mail-undeliverable",
        "operator-responses",
    }
)


class MailHistoryStore:
    """Own terminal mailbox records; live inbox and lease folders stay separate."""

    def __init__(self, control: str | Path) -> None:
        self.control = Path(control)

    def project_root(self, project_id: str) -> Path:
        return (
            self.control / "projects" / project_id
            if str(project_id or "").strip()
            else self.control / "unscoped"
        )

    def family(self, project_id: str, family: str, *, retention_days: int = MAIL_HISTORY_RETENTION_DAYS) -> KeyedHistoryStore:
        return KeyedHistoryStore(
            self.project_root(project_id) / family,
            store=family,
            retention_days=retention_days,
            max_bytes_per_agent=MAIL_HISTORY_MAX_BYTES_PER_AGENT,
            max_records_per_agent=MAIL_HISTORY_MAX_RECORDS_PER_AGENT,
        )

    def legacy_mail_root(self, project_id: str, worker_name: str) -> Path:
        agent = agent_component(worker_name)
        if str(project_id or "").strip():
            return self.control / "projects" / project_id / "mail" / agent
        return self.control / "workers" / agent / "mail"

    def legacy_path(
        self,
        *,
        project_id: str,
        family: str,
        agent: str,
        record_id: str,
    ) -> Path:
        name = f"{record_id}.json"
        if family == "mail-processed":
            return self.legacy_mail_root(project_id, agent) / "processed" / name
        if family == "mail-ignored":
            if str(project_id or "").strip():
                return self.control / "projects" / project_id / "mail" / "ignored" / name
            return self.legacy_mail_root("", agent) / "ignored" / name
        if family == "mail-undeliverable":
            if str(project_id or "").strip():
                return self.control / "projects" / project_id / "mail" / "undeliverable" / agent_component(agent) / name
            return self.control / "undeliverable-mail" / agent_component(agent) / name
        if family == "mail-by-control":
            return self.legacy_mail_root(project_id, agent) / "by-control" / name
        if family == "mail-idempotency":
            if str(project_id or "").strip():
                return self.control / "projects" / project_id / "idempotency" / "mail" / name
            return self.control / "workers" / agent_component(agent) / "idempotency" / "mail" / name
        if family == "operator-responses":
            return self.control / "operator-responses" / agent_component(agent) / name
        if family == "handled":
            return self.control / "workers" / agent_component(agent) / "handled" / name
        raise ValueError(f"unknown mail history family: {family}")

    def read(
        self,
        *,
        project_id: str,
        family: str,
        agent: str,
        record_id: str,
    ) -> dict[str, Any] | None:
        return self.family(project_id, family).read(
            agent=agent,
            record_id=record_id,
            fallback_agents=self._fallback_agents(family, agent),
            legacy_paths=[
                self.legacy_path(
                    project_id=project_id,
                    family=family,
                    agent=agent,
                    record_id=record_id,
                )
            ],
        )

    def find(
        self,
        *,
        project_id: str,
        family: str,
        agent: str,
        record_id: str,
    ) -> Path | None:
        return self.family(project_id, family).find(
            agent=agent,
            record_id=record_id,
            fallback_agents=self._fallback_agents(family, agent),
            legacy_paths=[
                self.legacy_path(
                    project_id=project_id,
                    family=family,
                    agent=agent,
                    record_id=record_id,
                )
            ],
        )

    def write(
        self,
        *,
        project_id: str,
        family: str,
        agent: str,
        record_id: str,
        row: Mapping[str, Any],
        slug: str = "",
    ) -> Path:
        history = self.family(project_id, family)
        for fallback in self._fallback_agents(family, agent):
            history.remove(agent=fallback, record_id=record_id)
        path = history.write(
            agent=agent,
            record_id=record_id,
            row=row,
            slug=slug,
        )
        legacy = self.legacy_path(
            project_id=project_id,
            family=family,
            agent=agent,
            record_id=record_id,
        )
        if legacy != path:
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
            fallback_agents=self._fallback_agents(family, agent),
            legacy_paths=[
                self.legacy_path(
                    project_id=project_id,
                    family=family,
                    agent=agent,
                    record_id=record_id,
                )
            ],
        )

    def pending_path(
        self,
        *,
        project_id: str,
        family: str,
        agent: str,
        record_id: str,
    ) -> Path:
        return (
            self.project_root(project_id)
            / family
            / agent_component(agent)
            / "pending"
            / f"{record_id}.json"
        )

    def write_pending(
        self,
        *,
        project_id: str,
        family: str,
        agent: str,
        record_id: str,
        row: Mapping[str, Any],
    ) -> Path:
        path = self.pending_path(
            project_id=project_id,
            family=family,
            agent=agent,
            record_id=record_id,
        )
        atomic_write_json(path, row)
        return path

    def migrate_pending(
        self,
        *,
        project_id: str,
        family: str,
        agent: str,
        record_id: str,
        row: Mapping[str, Any],
    ) -> Path:
        """Move a legacy in-flight row without replacing newer pending state.

        A prior migration pass may have written the target and crashed before
        removing its legacy source. The target can then advance independently;
        replaying migration must preserve that newer state.
        """

        path = self.pending_path(
            project_id=project_id,
            family=family,
            agent=agent,
            record_id=record_id,
        )
        if not path.is_file():
            atomic_write_json(path, row)
        return path

    def pending(
        self,
        *,
        project_id: str,
        family: str,
        agents: list[str] | None = None,
    ) -> list[tuple[Path, dict[str, Any]]]:
        history = self.family(project_id, family)
        chosen = (
            [agent_component(agent) for agent in agents]
            if agents is not None
            else history.partitioned.agents()
        )
        rows: list[tuple[Path, dict[str, Any]]] = []
        with history.partitioned.reading("pending") as read:
            for agent in chosen:
                directory = history.partitioned.agent_root(agent) / "pending"
                paths = sorted(directory.glob("*.json")) if directory.is_dir() else []
                read.opened_pending(agent, len(paths))
                for path in paths:
                    row = read_json(path, required=False)
                    if isinstance(row, Mapping) and row:
                        rows.append((path, dict(row)))
        return rows

    def settle_pending(
        self,
        *,
        project_id: str,
        family: str,
        agent: str,
        record_id: str,
        row: Mapping[str, Any],
        source: Path,
        slug: str = "",
    ) -> Path:
        path = self.write(
            project_id=project_id,
            family=family,
            agent=agent,
            record_id=record_id,
            row=row,
            slug=slug,
        )
        source.unlink(missing_ok=True)
        return path

    def stores(self) -> Iterator[KeyedHistoryStore]:
        """Every partitioned mail-history family currently present."""

        roots = [self.control / "unscoped"]
        projects = self.control / "projects"
        if projects.is_dir():
            roots.extend(
                project
                for project in sorted(projects.iterdir())
                if project.is_dir() and (project / "project.json").is_file()
            )
        for root in roots:
            for family in sorted(MAIL_HISTORY_FAMILIES):
                if (root / family).is_dir():
                    yield KeyedHistoryStore(
                        root / family,
                        store=family,
                        retention_days=MAIL_HISTORY_RETENTION_DAYS,
                        max_bytes_per_agent=MAIL_HISTORY_MAX_BYTES_PER_AGENT,
                        max_records_per_agent=MAIL_HISTORY_MAX_RECORDS_PER_AGENT,
                    )

    def migrate_legacy(self, *, batch_size: int = 1000) -> dict[str, Any]:
        """Move every pre-W287 mail-history directory in bounded batches."""

        results: dict[str, Any] = {}
        for key, project_id, family, agent, legacy in self._legacy_sources():
            results[key] = migrate_flat_history(
                history=self.family(project_id, family),
                legacy=legacy,
                agent_for=lambda row, _path, fallback=agent, kind=family: self._row_agent(
                    kind, row, fallback
                ),
                slug_for=lambda row, _path, kind=family: str(
                    row.get("state")
                    or row.get("delivery_status")
                    or kind
                ),
                batch_size=batch_size,
            )
        return results

    @staticmethod
    def _fallback_agents(family: str, agent: str) -> tuple[str, ...]:
        if family == "mail-idempotency" and agent_component(agent) != "-":
            return ("-",)
        return ()

    @staticmethod
    def _row_agent(family: str, row: Mapping[str, Any], fallback: str) -> str:
        fields = {
            "handled": ("worker_name",),
            "mail-by-control": ("recipient", "worker_name"),
            "mail-idempotency": ("sender", "worker_name"),
            "mail-ignored": ("recipient", "worker_name"),
            "mail-processed": ("recipient", "worker_name"),
            "mail-undeliverable": ("recipient", "worker_name"),
            "operator-responses": ("worker_name", "recipient"),
        }.get(family, ())
        for field in fields:
            value = str(row.get(field) or "").strip()
            if value:
                return value
        return fallback or "-"

    def _legacy_sources(self) -> Iterator[tuple[str, str, str, str, Path]]:
        workers = self.control / "workers"
        for worker in sorted(workers.iterdir()) if workers.is_dir() else ():
            if not worker.is_dir():
                continue
            agent = worker.name
            yield f"unscoped:{agent}:handled", "", "handled", agent, worker / "handled"
            yield (
                f"unscoped:{agent}:mail-idempotency",
                "",
                "mail-idempotency",
                agent,
                worker / "idempotency" / "mail",
            )
            for family, folder in (
                ("mail-processed", "processed"),
                ("mail-by-control", "by-control"),
                ("mail-ignored", "ignored"),
            ):
                yield (
                    f"unscoped:{agent}:{family}",
                    "",
                    family,
                    agent,
                    worker / "mail" / folder,
                )

        responses = self.control / "operator-responses"
        for worker in sorted(responses.iterdir()) if responses.is_dir() else ():
            if worker.is_dir():
                yield (
                    f"unscoped:{worker.name}:operator-responses",
                    "",
                    "operator-responses",
                    worker.name,
                    worker,
                )
        undeliverable = self.control / "undeliverable-mail"
        for recipient in sorted(undeliverable.iterdir()) if undeliverable.is_dir() else ():
            if recipient.is_dir():
                yield (
                    f"unscoped:{recipient.name}:mail-undeliverable",
                    "",
                    "mail-undeliverable",
                    recipient.name,
                    recipient,
                )

        projects = self.control / "projects"
        for project in sorted(projects.iterdir()) if projects.is_dir() else ():
            if not project.is_dir() or not (project / "project.json").is_file():
                continue
            project_id = project.name
            yield (
                f"{project_id}:-:mail-idempotency",
                project_id,
                "mail-idempotency",
                "-",
                project / "idempotency" / "mail",
            )
            mail = project / "mail"
            for mailbox in sorted(mail.iterdir()) if mail.is_dir() else ():
                if not mailbox.is_dir() or mailbox.name in {"ignored", "undeliverable"}:
                    continue
                for family, folder in (
                    ("mail-processed", "processed"),
                    ("mail-by-control", "by-control"),
                ):
                    yield (
                        f"{project_id}:{mailbox.name}:{family}",
                        project_id,
                        family,
                        mailbox.name,
                        mailbox / folder,
                    )
            yield (
                f"{project_id}:-:mail-ignored",
                project_id,
                "mail-ignored",
                "-",
                mail / "ignored",
            )
            # Project undeliverable rows still missing their sender notice are
            # in-flight work. Reconciliation moves the legacy folders into the
            # new pending layout before history migration can classify them as
            # terminal.


__all__ = [
    "MAIL_HISTORY_FAMILIES",
    "MAIL_HISTORY_MAX_BYTES_PER_AGENT",
    "MAIL_HISTORY_MAX_RECORDS_PER_AGENT",
    "MAIL_HISTORY_RETENTION_DAYS",
    "MailHistoryStore",
]

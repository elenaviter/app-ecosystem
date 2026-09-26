"""The relay outbox, per project and agent, read by range (W287 2b).

Layout, under the field's ``projects/<project>/outbox/<agent>/`` (a row that
belongs to no project, such as mail to the operator outside any project, lives
under ``unscoped/outbox/<agent>/``, never among the projects)::

    pending/<outbox_id>.json                  queued, not yet claimed
    leased/<outbox_id>.json                   claimed by a relay, in flight
    attachments/<outbox_id>/                  a row's files; the row's payload
                                              names them by absolute path, so
                                              they stay here and expire by age
    <yyyy>/<mm>/<dd>/<hh>/<name>.json         settled, in the hour it was created

``<agent>`` is the row's ``worker_name`` (``-`` for a project-level row).
A settled row's name is ``<created>_<outbox_id>__<state>-<kind>`` (or the id
itself when the id carries its own time), so the outcome is read from the
name without opening the row, and retention drops whole hour folders
(rules LS2 and LS3 in ``products/project-board/docs/storage-and-retention.md``).

Rows from before W287 2b stay readable in the flat ``outbox/<folder>/``
directories by exact name until housekeeping moves them. Nothing lists them.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from ..contract.errors import DomainError
from .io import atomic_write_json, parse_utc, read_json
from .local_wake import notify_relay_outbox
from .local_store import PartitionedStore, agent_component, slug_component
from .outbox_layout import OUTBOX_FOLDERS, OUTBOX_IN_FLIGHT_FOLDERS


OUTBOX_TERMINAL_RETENTION_DAYS = 30
OUTBOX_TERMINAL_MAX_BYTES_PER_AGENT = 200 * 1024 * 1024
OUTBOX_TERMINAL_MAX_RECORDS_PER_AGENT = 100_000
ATTACHMENTS = "attachments"
_PROJECT_PREFIX = "work:project:"


def project_id_of(project_ref: str) -> str:
    text = str(project_ref or "")
    return text[len(_PROJECT_PREFIX):] if text.startswith(_PROJECT_PREFIX) and len(text) > len(_PROJECT_PREFIX) else ""


def state_of_name(name: str) -> str:
    """The settled state named in a row's file name, ``""`` for an in-flight name."""

    if "__" not in name:
        return ""
    return name.rsplit("__", 1)[1].split("-", 1)[0]


class OutboxStore:
    """Paths and lookups for the outbox. The caller holds :attr:`lock`."""

    def __init__(self, control: Path) -> None:
        self.control = Path(control)

    @property
    def lock(self) -> Path:
        return self.control / "outbox" / ".outbox.lock"

    @property
    def legacy_root(self) -> Path:
        return self.control / "outbox"

    # -- roots ---------------------------------------------------------------

    def project_root(self, project_ref: str) -> Path:
        project_id = project_id_of(project_ref)
        if not project_id:
            return self.control / "unscoped" / "outbox"
        return self.control / "projects" / project_id / "outbox"

    def agent_root(self, project_ref: str, worker_name: str) -> Path:
        return self.project_root(project_ref) / agent_component(worker_name)

    def partitioned(self, project_ref: str) -> PartitionedStore:
        return PartitionedStore(self.project_root(project_ref), store="outbox")

    def agent_roots(self, *, worker_name: str = "", project_ref: str = "") -> list[tuple[str, Path]]:
        """``(project_ref, agent_root)`` for the agents a caller may read.

        With a worker name, that worker's folder and the project-level ``-``
        folder; with a project, only that project.
        """

        projects = (
            [project_ref]
            if project_ref
            else [
                _PROJECT_PREFIX + child.name
                for child in sorted((self.control / "projects").glob("*"))
                if (child / "outbox").is_dir()
            ]
            + [""]
        )
        found: list[tuple[str, Path]] = []
        for ref in projects:
            root = self.project_root(ref)
            if not root.is_dir():
                continue
            if worker_name:
                names = {agent_component(worker_name), "-"}
                found.extend((ref, root / name) for name in sorted(names) if (root / name).is_dir())
            else:
                found.extend((ref, child) for child in sorted(root.iterdir()) if child.is_dir() and not child.name.startswith("."))
        return found

    # -- in flight -----------------------------------------------------------

    def in_flight_path(self, row: Mapping[str, Any], folder: str) -> Path:
        return self.agent_root(str(row.get("project_ref") or ""), str(row.get("worker_name") or "")) / folder / f"{row['outbox_id']}.json"

    def write_pending(self, row: Mapping[str, Any]) -> Path:
        path = self.in_flight_path(row, "pending")
        atomic_write_json(path, row)
        # The row is the authority. This best-effort hint only lets the relay
        # probe immediately instead of waiting for its next project cycle.
        notify_relay_outbox(self.control.parent)
        return path

    def in_flight(self, folder: str, *, worker_name: str = "", project_ref: str = "") -> Iterator[Path]:
        for ref, root in self.agent_roots(worker_name=worker_name, project_ref=project_ref):
            directory = root / folder
            paths = sorted(directory.glob("*.json")) if directory.is_dir() else []
            with self.partitioned(ref).reading(f"{folder}-list") as read:
                read.opened_pending(root.name, len(paths))
            yield from paths
        legacy = self.legacy_root / folder
        if legacy.is_dir():
            yield from sorted(legacy.glob("*.json"))

    def ready_rows(
        self,
        *,
        worker_names: Sequence[str] = (),
        now: datetime | None = None,
    ) -> Iterator[tuple[Path, dict[str, Any]]]:
        """Yield pending rows whose retry time has arrived.

        A worker-scoped scan includes project-level rows because an active
        worker attending that project may carry them. Paths are deduplicated:
        the same project-level or legacy row is visible from every worker
        scope.
        """

        names = {str(name or "").strip().lower() for name in worker_names}
        names.discard("")
        paths: set[Path] = set()
        if names:
            for name in names:
                paths.update(self.in_flight("pending", worker_name=name))
        else:
            paths.update(self.in_flight("pending"))
        now_dt = now or datetime.now(timezone.utc)
        for path in sorted(paths):
            try:
                row = read_json(path)
            except DomainError:
                # An unreadable pending row is still work for the ordinary
                # cycle to diagnose. Its path remains part of the signature.
                yield path, {}
                continue
            candidate_worker = str(row.get("worker_name") or "").lower()
            if names and candidate_worker and candidate_worker not in names:
                continue
            next_attempt_at = str(row.get("next_attempt_at") or "")
            if next_attempt_at:
                try:
                    if parse_utc(next_attempt_at) > now_dt:
                        continue
                except DomainError:
                    # The claimant owns validation and the resulting error.
                    # Treat malformed pending state as ready so it is visible.
                    pass
            yield path, row

    def has_ready_work(self, *, worker_names: Sequence[str] = ()) -> bool:
        return next(self.ready_rows(worker_names=worker_names), None) is not None

    def ready_signature(self, *, worker_names: Sequence[str] = ()) -> tuple:
        """A stable signature of ready pending rows for the relay wait loop."""

        signature: list[tuple[str, int, int]] = []
        for path, _row in self.ready_rows(worker_names=worker_names):
            try:
                stat = path.stat()
                relative = str(path.relative_to(self.control))
            except (OSError, ValueError):
                continue
            signature.append((relative, stat.st_mtime_ns, stat.st_size))
        return tuple(signature)

    def ready_project_refs(
        self,
        *,
        worker_name: str,
        limit: int = 20,
    ) -> list[str]:
        """Worker-owned project scopes ready for its Card channel."""

        clean_worker = str(worker_name or "").strip().lower()
        if not clean_worker:
            return []
        refs: set[str] = set()
        for _path, row in self.ready_rows(worker_names=[clean_worker]):
            candidate_worker = str(row.get("worker_name") or "").lower()
            # A project-level row has no worker owner. It stays on the normal
            # attendance path, where project linkage chooses its Card.
            if candidate_worker != clean_worker:
                continue
            refs.add(str(row.get("project_ref") or ""))
            if len(refs) >= max(1, int(limit)):
                break
        return sorted(refs)

    def attachments_dir(self, row: Mapping[str, Any]) -> Path:
        """Where a row's attachment files are written."""

        outbox_id = str(row["outbox_id"])
        current = self.agent_root(str(row.get("project_ref") or ""), str(row.get("worker_name") or "")) / ATTACHMENTS / outbox_id
        legacy = self.legacy_root / ATTACHMENTS / outbox_id
        return legacy if legacy.is_dir() and not current.is_dir() else current

    # -- settle --------------------------------------------------------------

    def settle(self, source: Path, row: Mapping[str, Any]) -> Path:
        """Move a row out of flight into its hour folder."""

        state = str(row.get("state") or "")
        created = _created(row)
        store = self.partitioned(str(row.get("project_ref") or ""))
        path = store.write(
            str(row.get("worker_name") or ""),
            str(row["outbox_id"]),
            created,
            row,
            slug=f"{state}-{slug_component(str(row.get('kind') or ''))}",
        )
        source.unlink(missing_ok=True)
        return path

    def move_in_flight(self, source: Path, row: Mapping[str, Any], folder: str) -> Path:
        destination = self.in_flight_path(row, folder)
        atomic_write_json(destination, row)
        if source != destination:
            source.unlink(missing_ok=True)
        return destination

    # -- lookups -------------------------------------------------------------

    def find(self, outbox_id: str, *, worker_name: str = "", project_ref: str = "") -> tuple[Path, str] | None:
        """``(path, folder)`` of one row by id, never listing the history.

        ``folder`` is ``pending``, ``leased``, or the settled state
        (``sent``, ``ignored``, ``refused``).
        """

        roots = self.agent_roots(worker_name=worker_name, project_ref=project_ref)
        for folder in OUTBOX_IN_FLIGHT_FOLDERS:
            for _ref, root in roots:
                path = root / folder / f"{outbox_id}.json"
                if path.is_file():
                    return path, folder
        by_project: dict[str, list[str]] = {}
        for ref, root in roots:
            by_project.setdefault(ref, []).append(root.name)
        for ref, agents in by_project.items():
            path = self.partitioned(ref).find(outbox_id, agents=agents, within_days=OUTBOX_TERMINAL_RETENTION_DAYS)
            if path is not None:
                return path, state_of_name(path.name) or str(read_json(path).get("state") or "")
        for folder in OUTBOX_FOLDERS:
            path = self.legacy_root / folder / f"{outbox_id}.json"
            if path.is_file():
                if folder in OUTBOX_IN_FLIGHT_FOLDERS:
                    return path, folder
                # A settled folder: the row names sent, ignored or refused.
                # Anything else inside is not a settled state, so the folder decides.
                state = str(read_json(path).get("state") or "")
                return path, state if state in {"sent", "ignored", "refused"} else folder
        return None

    def read(self, outbox_id: str, **scope: str) -> dict[str, Any] | None:
        found = self.find(outbox_id, **scope)
        return dict(read_json(found[0])) if found else None

    def settled_paths(self, *, project_ref: str, op: str) -> Iterator[Path]:
        """Every settled row of one project, oldest first: a full walk.

        For one-time tools (the plan cutover) only. The walk is logged per
        agent like any read, so its range is visible.
        """

        store = self.partitioned(project_ref)
        with store.reading(op) as read:
            for agent in store.agents():
                hours = list(store.hours_newest_first(agent))
                for hour, start in reversed(hours):
                    names = sorted(n for n in os.listdir(hour) if n.endswith(".json"))
                    read.opened(agent, start.strftime("%Y-%m-%dT%H"), len(names))
                    yield from (hour / name for name in names)
        for folder in ("sent", "refused"):
            legacy = self.legacy_root / folder
            if legacy.is_dir():
                yield from sorted(legacy.glob("*.json"))

    def settled_newest(
        self,
        *,
        op: str,
        limit: int,
        worker_name: str = "",
        project_ref: str = "",
        predicate: Any = None,
    ) -> list[dict[str, Any]]:
        """The newest settled rows, reading only the newest hours needed."""

        by_project: dict[str, list[str]] = {}
        for ref, root in self.agent_roots(worker_name=worker_name, project_ref=project_ref):
            by_project.setdefault(ref, []).append(root.name)
        rows: list[dict[str, Any]] = []
        for ref, agents in by_project.items():
            rows.extend(self.partitioned(ref).newest(op=op, limit=limit, agents=agents, predicate=predicate))
        for folder in ("sent", "refused"):
            legacy = self.legacy_root / folder
            if legacy.is_dir():
                for path in sorted(legacy.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
                    row = read_json(path, required=False)
                    if row and (predicate is None or predicate(row)):
                        rows.append(dict(row))
        rows.sort(key=lambda row: str(row.get("settled_at") or row.get("created_at") or ""), reverse=True)
        return rows[:limit]


def _created(row: Mapping[str, Any]) -> datetime:
    text = str(row.get("created_at") or "").replace("Z", "+00:00")
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return datetime.now(timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


__all__ = [
    "ATTACHMENTS",
    "OUTBOX_TERMINAL_RETENTION_DAYS",
    "OUTBOX_TERMINAL_MAX_BYTES_PER_AGENT",
    "OUTBOX_TERMINAL_MAX_RECORDS_PER_AGENT",
    "OutboxStore",
    "project_id_of",
    "state_of_name",
]

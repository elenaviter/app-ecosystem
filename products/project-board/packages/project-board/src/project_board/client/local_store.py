"""One partitioned relay-local store, and the only way to read it (W287).

Layout, per project and agent (the worker or person a record belongs to)::

    <project>/<store>/<agent>/<yyyy>/<mm>/<dd>/<hh>/<record_id>.json
        a generated id is <created>_<key>__<slug> (:func:`new_record_id`): it
        says when the record was made and what it is, so its hour folder is
        computed from the id and a listing reads without opening a file
    <project>/<store>/<agent>/<yyyy>/<mm>/<dd>/<hh>/<created>_<key>__<slug>.json
        a caller-chosen key (a content hash that deduplicates) cannot carry a
        time, so its name is prefixed with the creation stamp
    <project>/<store>/<agent>/<yyyy>/<mm>/<dd>/ids      append-only "<record_id> <hh>" lookup entries

The file name starts with the record's UTC creation stamp, so a listing sorts
by time and retention removes whole hour folders without opening a file (rule
LS2 in ``docs/project-board/storage-and-retention.md``). A lookup by id that
cannot compute its folder reads the day ``ids`` files newest first, inside the
retention window only (LS3).

Housekeeping rebuilds each day index from retained record names. That removes
stale and duplicate append entries away from the relay delivery path.

Every read goes through :meth:`PartitionedStore.reading`, which records the
hour folders it opened and logs one line per agent::

    relay store read worker=<agent> store=<store> op=<op> range=<first>..<last> partitions=<n> records=<n> ms=<n>

The last summary per agent and store is kept for the relay heartbeat.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .io import atomic_write_json, exclusive_lock, read_json


logger = logging.getLogger(__name__)

_AGENT_UNSAFE = re.compile(r"[^A-Za-z0-9._:@+-]")
_SLUG_UNSAFE = re.compile(r"[^a-z0-9]+")
SLUG_SEPARATOR = "__"
SLUG_MAX = 60
# Microseconds, because records written in the same second must still sort in
# the order they were made: a stamp in seconds put five events of one second
# in random-id order and ``newest`` returned the wrong ones.
_STAMP = "%Y%m%dT%H%M%S.%fZ"
_STAMP_SECONDS = "%Y%m%dT%H%M%SZ"
IDS_FILE = "ids"
_LAST_READS_LOCK = threading.Lock()
_LAST_READS: dict[tuple[str, str], dict[str, Any]] = {}


def agent_component(agent: str) -> str:
    """The folder name for an agent: lower case, filesystem-safe, never empty."""

    text = _AGENT_UNSAFE.sub("-", str(agent or "").strip().lower())[:160]
    return text or "-"


def slug_component(text: str) -> str:
    """A short readable name: lower case letters, digits and single hyphens."""

    return _SLUG_UNSAFE.sub("-", str(text or "").lower()).strip("-")[:SLUG_MAX].rstrip("-")


def new_record_id(created: datetime, *, slug: str = "") -> str:
    """A generated id: ``<created>_<key>__<slug>``, time first so ids sort by time."""

    readable = slug_component(slug)[:40].rstrip("-")
    key = f"{stamp(created)}_{uuid.uuid4().hex[:12]}"
    return f"{key}{SLUG_SEPARATOR}{readable}" if readable else key


def stamp_of_id(record_id: str) -> datetime | None:
    """The creation time a generated id carries, or None for a caller-chosen key."""

    return parse_stamp(str(record_id or "").split("_", 1)[0])


def record_id_of(name: str) -> str:
    """The key inside ``<created>_<key>[__<slug>].json``, for the day index."""

    stem = name[:-5] if name.endswith(".json") else name
    rest = stem.split("_", 1)[1] if "_" in stem else ""
    return rest.split(SLUG_SEPARATOR, 1)[0]


# Reads a relay makes on every cycle or wake, of in-flight folders or one id.
# At INFO they flood the rotating relay log: lookups did on 2026-09-24, and
# the outbox's in-flight listings did at the W287 switch (about 1,100 lines a
# minute on spark1, 2026-09-26). They log at DEBUG; their last summary still
# reaches the heartbeat. Listings of history, recovery and retention stay INFO.
PER_CYCLE_OPS = frozenset({"lookup", "pending", "pending-list", "leased-list", "lease-recovery"})


def _project_of(root: Path) -> str:
    """The project a store root lies under (``.../projects/<id>/...``), or ""."""

    parts = root.parts
    for index in range(len(parts) - 2, -1, -1):
        if parts[index] == "projects":
            return parts[index + 1]
    return ""


def stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime(_STAMP)


def parse_stamp(text: str) -> datetime | None:
    for pattern in (_STAMP, _STAMP_SECONDS):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def last_read_summaries(agent: str) -> dict[str, dict[str, Any]]:
    """The last read of each store for one agent, for the relay heartbeat."""

    key = agent_component(agent)
    with _LAST_READS_LOCK:
        return {store: dict(summary) for (who, store), summary in _LAST_READS.items() if who == key}


@dataclass
class ReadRecord:
    """What one read opened, per agent."""

    store: str
    op: str
    hours: dict[str, list[str]] = dataclass_field(default_factory=dict)
    records: dict[str, int] = dataclass_field(default_factory=dict)
    pending_only: set[str] = dataclass_field(default_factory=set)
    removed: dict[str, int] = dataclass_field(default_factory=dict)
    key: str = ""

    def opened(self, agent: str, hour: str, records: int) -> None:
        self.hours.setdefault(agent, []).append(hour)
        self.records[agent] = self.records.get(agent, 0) + int(records)

    def opened_pending(self, agent: str, records: int) -> None:
        self.pending_only.add(agent)
        self.records[agent] = self.records.get(agent, 0) + int(records)

    def removed_from(self, agent: str, records: int) -> None:
        """Count records a retention pass removed, for its log line."""
        self.removed[agent] = self.removed.get(agent, 0) + int(records)

    def partitions(self, agent: str) -> list[str]:
        return list(self.hours.get(agent, []))


class PartitionedStore:
    """Records of one kind for one project, partitioned by agent and hour."""

    def __init__(self, root: Path, *, store: str, project: str = "") -> None:
        self.root = Path(root)
        self.store = store
        self.project = project or _project_of(self.root)

    @property
    def lock(self) -> Path:
        """The mutation lock shared by writers and retention for this store."""

        return self.root / ".partitioned-store.lock"

    # -- paths ---------------------------------------------------------------

    def agent_root(self, agent: str) -> Path:
        return self.root / agent_component(agent)

    def hour_dir(self, agent: str, created: datetime) -> Path:
        value = created.astimezone(timezone.utc)
        return self.agent_root(agent).joinpath(
            f"{value.year:04d}", f"{value.month:02d}", f"{value.day:02d}", f"{value.hour:02d}"
        )

    def record_path(self, agent: str, created: datetime, record_id: str, *, slug: str = "") -> Path:
        carried = stamp_of_id(record_id)
        if carried is not None:
            # The id already says when and what: it is the file name.
            return self.hour_dir(agent, carried) / f"{record_id}.json"
        name = f"{stamp(created)}_{record_id}"
        readable = slug_component(slug)
        if readable:
            name += f"{SLUG_SEPARATOR}{readable}"
        return self.hour_dir(agent, created) / f"{name}.json"

    def agents(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            child.name for child in self.root.iterdir()
            if child.is_dir() and not child.name.startswith(".")
        )

    # -- writes --------------------------------------------------------------

    def write(
        self,
        agent: str,
        record_id: str,
        created: datetime,
        row: Mapping[str, Any],
        *,
        slug: str = "",
    ) -> Path:
        """Write one record and its day index entry as one retention-safe mutation."""

        with exclusive_lock(self.lock):
            return self._write_unlocked(agent, record_id, created, row, slug=slug)

    def replace(
        self,
        agent: str,
        record_id: str,
        created: datetime,
        row: Mapping[str, Any],
        *,
        within_days: int,
        slug: str = "",
    ) -> Path:
        """Replace one stable-key record under the store mutation lock."""

        with exclusive_lock(self.lock):
            prior = self.find(
                record_id,
                agents=[agent],
                within_days=within_days,
            )
            if prior is not None:
                prior.unlink(missing_ok=True)
            return self._write_unlocked(agent, record_id, created, row, slug=slug)

    def _write_unlocked(
        self,
        agent: str,
        record_id: str,
        created: datetime,
        row: Mapping[str, Any],
        *,
        slug: str = "",
    ) -> Path:
        """Write a record while :attr:`lock` is held."""

        path = self.record_path(agent, created, record_id, slug=slug)
        atomic_write_json(path, row)
        ids = path.parent.parent / IDS_FILE
        with open(ids, "a", encoding="utf-8") as handle:
            handle.write(f"{record_id} {path.parent.name}\n")
        return path

    # -- reads ---------------------------------------------------------------

    @contextmanager
    def reading(self, op: str, *, key: str = "") -> Iterator[ReadRecord]:
        """Record what one read opens, then log it per agent.

        A read that walks partitions (a listing, retention, recovery) logs at
        INFO: its range is what the operator asked to see. A lookup by id opens
        one folder by construction and runs once per record a caller touches;
        on 2026-09-24 the legacy cleanup made 50,000 of them in an hour and
        each wrote an INFO line, flooding the rotating relay log. Lookups log
        at DEBUG, and their last summary still reaches the heartbeat.

        The line names the store's other dimensions (W287 partition keys): the
        project whose tree was read, the key a lookup asked for, and for
        retention the records it removed.
        """
        record = ReadRecord(store=self.store, op=op, key=key)
        started = time.monotonic()
        try:
            yield record
        finally:
            elapsed = int((time.monotonic() - started) * 1000)
            for agent in sorted(set(record.hours) | record.pending_only):
                hours = sorted(record.hours.get(agent, []))
                span = f"{hours[0]}..{hours[-1]}" if hours else "pending/"
                summary = {
                    "store": self.store,
                    "op": op,
                    "range": span,
                    "partitions": len(hours) or 1,
                    "records": record.records.get(agent, 0),
                    "ms": elapsed,
                    "at": stamp(datetime.now(timezone.utc)),
                }
                message = "relay store read worker=%s store=%s op=%s range=%s partitions=%d records=%d ms=%d"
                args: list[Any] = [agent, self.store, op, span, summary["partitions"], summary["records"], elapsed]
                if self.project:
                    summary["project"] = self.project
                    message += " project=%s"
                    args.append(self.project)
                if record.key:
                    summary["key"] = record.key
                    message += " key=%s"
                    args.append(record.key)
                if op == "retention":
                    summary["removed"] = record.removed.get(agent, 0)
                    message += " removed=%d"
                    args.append(summary["removed"])
                with _LAST_READS_LOCK:
                    _LAST_READS[(agent, self.store)] = summary
                logger.log(logging.DEBUG if op in PER_CYCLE_OPS else logging.INFO, message, *args)

    def hours_newest_first(self, agent: str, *, not_before: datetime | None = None) -> Iterator[tuple[Path, datetime]]:
        """Hour folders of one agent, newest first, by folder name only."""

        root = self.agent_root(agent)
        for year in _numbered(root, 4, reverse=True):
            for month in _numbered(year, 2, reverse=True):
                for day in _numbered(month, 2, reverse=True):
                    for hour in _numbered(day, 2, reverse=True):
                        try:
                            start = datetime(int(year.name), int(month.name), int(day.name), int(hour.name), tzinfo=timezone.utc)
                        except ValueError:
                            continue
                        if not_before is not None and start + timedelta(hours=1) <= not_before:
                            return
                        yield hour, start

    def newest(
        self,
        *,
        op: str,
        limit: int,
        agents: list[str] | None = None,
        predicate: Callable[[Mapping[str, Any]], bool] | None = None,
        not_before: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """The newest ``limit`` records across agents, reading folders newest first."""

        chosen = [agent_component(a) for a in agents] if agents is not None else self.agents()
        found: list[tuple[str, dict[str, Any]]] = []
        with self.reading(op) as read:
            cursors = {agent: self.hours_newest_first(agent, not_before=not_before) for agent in chosen}
            heads: dict[str, tuple[Path, datetime] | None] = {agent: next(cursor, None) for agent, cursor in cursors.items()}
            while len(found) < limit:
                live = [(start, agent) for agent, head in heads.items() if head is not None for start in [head[1]]]
                if not live:
                    break
                newest_start = max(start for start, _agent in live)
                # An hour is one merge boundary. Reading only one agent at an
                # equal-hour tie can satisfy ``limit`` with an older row while
                # a newer row from another agent in that same hour remains
                # unopened.
                for agent in sorted(
                    agent for start, agent in live if start == newest_start
                ):
                    hour, start = heads[agent]  # type: ignore[misc]
                    names = sorted(
                        (n for n in os.listdir(hour) if n.endswith(".json")),
                        reverse=True,
                    )
                    read.opened(
                        agent, start.strftime("%Y-%m-%dT%H"), len(names)
                    )
                    for name in names:
                        row = read_json(hour / name, required=False)
                        if row and (predicate is None or predicate(row)):
                            found.append((name, dict(row)))
                    heads[agent] = next(cursors[agent], None)
        found.sort(key=lambda item: item[0], reverse=True)
        return [row for _name, row in found[:limit]]

    def latest_stamp(self, agent: str, *, read: ReadRecord | None = None) -> datetime | None:
        """When the agent's newest record was created, from names alone."""

        for hour, start in self.hours_newest_first(agent):
            names = sorted((n for n in os.listdir(hour) if n.endswith(".json")), reverse=True)
            if read is not None:
                read.opened(agent_component(agent), start.strftime("%Y-%m-%dT%H"), 0)
            if names:
                return parse_stamp(names[0].split("_", 1)[0])
        return None

    def find(self, record_id: str, *, agents: list[str] | None = None, within_days: int, op: str = "lookup") -> Path | None:
        """Locate a record by id through the day ``ids`` files, newest day first."""

        chosen = [agent_component(a) for a in agents] if agents is not None else self.agents()
        carried = stamp_of_id(record_id)
        if carried is not None:
            # A generated id names its own hour folder: one stat per agent.
            with self.reading(op, key=record_id) as read:
                for agent in chosen:
                    path = self.hour_dir(agent, carried) / f"{record_id}.json"
                    read.opened(agent, carried.strftime("%Y-%m-%dT%H"), 1)
                    if path.is_file():
                        return path
            return None
        cutoff = datetime.now(timezone.utc) - timedelta(days=within_days)
        with self.reading(op, key=record_id) as read:
            for agent in chosen:
                root = self.agent_root(agent)
                for year in _numbered(root, 4, reverse=True):
                    for month in _numbered(year, 2, reverse=True):
                        for day in _numbered(month, 2, reverse=True):
                            try:
                                day_end = datetime(int(year.name), int(month.name), int(day.name), tzinfo=timezone.utc) + timedelta(days=1)
                            except ValueError:
                                continue
                            if day_end <= cutoff:
                                break
                            ids = day / IDS_FILE
                            if not ids.is_file():
                                continue
                            read.opened(agent, f"{year.name}-{month.name}-{day.name}", 1)
                            for line in reversed(ids.read_text(encoding="utf-8").splitlines()):
                                parts = line.split()
                                if len(parts) == 2 and parts[0] == record_id:
                                    folder = day / parts[1]
                                    for name in os.listdir(folder) if folder.is_dir() else ():
                                        if name.endswith(".json") and record_id_of(name) == record_id:
                                            return folder / name
                                    # A rewrite or removal can leave an older
                                    # append behind until background retention
                                    # compacts the day. Reads never turn that
                                    # retained history into foreground work.
                                    continue
        return None

    # -- retention -----------------------------------------------------------

    def expire(
        self,
        *,
        cutoff: datetime,
        max_bytes_per_agent: int = 0,
        max_records_per_agent: int = 0,
    ) -> dict[str, int]:
        """Enforce age and size bounds by removing whole hour folders.

        Retention inspects folder names and file metadata only. It never opens
        a record body. Once records older than ``cutoff`` are gone, the oldest
        remaining hour folders are removed until both optional per-agent
        bounds hold (W287, LS2).
        """

        with exclusive_lock(self.lock):
            return self._expire_unlocked(
                cutoff=cutoff,
                max_bytes_per_agent=max_bytes_per_agent,
                max_records_per_agent=max_records_per_agent,
            )

    def _expire_unlocked(
        self,
        *,
        cutoff: datetime,
        max_bytes_per_agent: int,
        max_records_per_agent: int,
    ) -> dict[str, int]:
        """Apply retention while :attr:`lock` excludes writers."""

        removed = {"partitions": 0, "records": 0}
        byte_limit = max(0, int(max_bytes_per_agent))
        record_limit = max(0, int(max_records_per_agent))
        for agent in self.agents():
            inventory: list[tuple[Path, datetime, list[str], int]] = []
            with self.reading("retention") as read:
                for hour, start in reversed(list(self.hours_newest_first(agent))):
                    names = [name for name in os.listdir(hour) if name.endswith(".json")]
                    size = 0
                    for name in names:
                        try:
                            size += (hour / name).stat().st_size
                        except FileNotFoundError:
                            continue
                    read.opened(agent, start.strftime("%Y-%m-%dT%H"), len(names))
                    inventory.append((hour, start, names, size))

                retained: list[tuple[Path, datetime, list[str], int]] = []
                for hour, start, names, size in inventory:
                    if start + timedelta(hours=1) <= cutoff:
                        shutil.rmtree(hour)
                        removed["partitions"] += 1
                        removed["records"] += len(names)
                        read.removed_from(agent, len(names))
                        continue
                    retained.append((hour, start, names, size))

                _rebuild_day_indexes(self.agent_root(agent), inventory)
                _prune_empty_days(self.agent_root(agent))
                retained = [item for item in retained if item[0].is_dir()]
                total_records = sum(len(names) for _hour, _start, names, _size in retained)
                total_bytes = sum(size for _hour, _start, _names, size in retained)
                total_bytes += _index_bytes(self.agent_root(agent))

                while retained and (
                    (record_limit and total_records > record_limit)
                    or (byte_limit and total_bytes > byte_limit)
                ):
                    hour, _start, names, size = retained.pop(0)
                    shutil.rmtree(hour)
                    total_records -= len(names)
                    removed["partitions"] += 1
                    removed["records"] += len(names)
                    read.removed_from(agent, len(names))
                    if hour.parent.is_dir() and _numbered(hour.parent, 2):
                        rebuild_day_index(
                            hour.parent,
                            store=self.store,
                            agent=agent,
                            announce=False,
                        )
                    _prune_empty_days(self.agent_root(agent))
                    total_bytes = sum(
                        retained_size
                        for _retained_hour, _retained_start, _retained_names, retained_size in retained
                    ) + _index_bytes(self.agent_root(agent))
        return removed


def rebuild_day_index(
    day: Path,
    *,
    store: str = "",
    agent: str = "",
    announce: bool = True,
) -> int:
    """Rewrite a day's ``ids`` file from the record names in its hour folders."""

    entries: dict[str, str] = {}
    for hour in _numbered(day, 2):
        for name in sorted(os.listdir(hour)):
            if not name.endswith(".json") or "_" not in name:
                continue
            record_id = record_id_of(name)
            if record_id:
                entries[record_id] = hour.name
    lines = [
        f"{record_id} {hour}"
        for record_id, hour in sorted(
            entries.items(), key=lambda item: (item[1], item[0])
        )
    ]
    atomic_write_text(day / IDS_FILE, "".join(f"{line}\n" for line in lines))
    if announce:
        logger.warning(
            "relay store index rebuilt worker=%s store=%s day=%s records=%d",
            agent or day.parent.parent.parent.name,
            store,
            "-".join((day.parent.parent.name, day.parent.name, day.name)),
            len(lines),
        )
    return len(lines)


def atomic_write_text(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _index_bytes(agent_root: Path) -> int:
    """Bytes occupied by retained lookup indexes for one agent."""

    total = 0
    for year in _numbered(agent_root, 4):
        for month in _numbered(year, 2):
            for day in _numbered(month, 2):
                try:
                    total += (day / IDS_FILE).stat().st_size
                except FileNotFoundError:
                    continue
    return total


def _numbered(parent: Path, width: int, *, reverse: bool = False) -> list[Path]:
    if not parent.is_dir():
        return []
    return sorted(
        (child for child in parent.iterdir() if child.is_dir() and len(child.name) == width and child.name.isdigit()),
        key=lambda child: child.name,
        reverse=reverse,
    )


def _prune_empty_days(agent_root: Path) -> None:
    for year in _numbered(agent_root, 4):
        for month in _numbered(year, 2):
            for day in _numbered(month, 2):
                if not _numbered(day, 2):
                    (day / IDS_FILE).unlink(missing_ok=True)
                    _rmdir(day)
            _rmdir(month)
        _rmdir(year)


def _rebuild_day_indexes(
    agent_root: Path,
    inventory: list[tuple[Path, datetime, list[str], int]],
) -> None:
    """Compact every inventoried day before its index bytes enforce retention."""

    days = {hour.parent for hour, _start, _names, _size in inventory}
    for day in sorted(days):
        if day.is_dir() and _numbered(day, 2):
            rebuild_day_index(
                day,
                agent=agent_root.name,
                announce=False,
            )


def _rmdir(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass


__all__ = [
    "IDS_FILE",
    "PartitionedStore",
    "ReadRecord",
    "agent_component",
    "last_read_summaries",
    "parse_stamp",
    "rebuild_day_index",
    "new_record_id",
    "record_id_of",
    "stamp_of_id",
    "slug_component",
    "stamp",
]

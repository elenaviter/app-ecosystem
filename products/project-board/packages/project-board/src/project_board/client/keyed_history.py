"""Time-partitioned relay records addressed by a stable key.

These stores hold terminal history such as handling markers, idempotency
receipts and journal receipts.  Callers address one record by key; history is
partitioned by agent and creation hour so lookup and retention never require a
flat-directory scan (W287, rules LS2 and LS3).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .io import read_json
from .local_store import PartitionedStore


DEFAULT_MAX_BYTES_PER_AGENT = 50 * 1024 * 1024
DEFAULT_MAX_RECORDS_PER_AGENT = 50_000


def record_time(
    row: Mapping[str, Any],
    *,
    fields: Sequence[str] = (
        "created_at",
        "recorded_at",
        "received_at",
        "leased_at",
        "started_at",
        "settled_at",
        "updated_at",
    ),
    fallback: datetime | None = None,
) -> datetime:
    """Return one UTC record time without making callers duplicate parsing."""

    for field in fields:
        text = str(row.get(field) or "").strip()
        if not text:
            continue
        try:
            value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            continue
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    value = fallback or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class KeyedHistoryStore:
    """A :class:`PartitionedStore` with exact-key and legacy fallbacks.

    Legacy paths are always supplied explicitly and checked by exact name.
    They are never listed during a normal read; background migration is the
    only code allowed to enumerate them.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        store: str,
        retention_days: int,
        max_bytes_per_agent: int = DEFAULT_MAX_BYTES_PER_AGENT,
        max_records_per_agent: int = DEFAULT_MAX_RECORDS_PER_AGENT,
    ) -> None:
        self.partitioned = PartitionedStore(Path(root), store=store)
        self.store = store
        self.retention_days = max(1, int(retention_days))
        self.max_bytes_per_agent = max(1, int(max_bytes_per_agent))
        self.max_records_per_agent = max(1, int(max_records_per_agent))

    @property
    def root(self) -> Path:
        return self.partitioned.root

    def write(
        self,
        *,
        agent: str,
        record_id: str,
        row: Mapping[str, Any],
        created: datetime | None = None,
        slug: str = "",
    ) -> Path:
        prior = self.partitioned.find(
            record_id,
            agents=[agent],
            within_days=self.retention_days,
        )
        if prior is not None:
            prior.unlink(missing_ok=True)
        return self.partitioned.write(
            agent,
            record_id,
            created or record_time(row),
            row,
            slug=slug,
        )

    def find(
        self,
        *,
        agent: str,
        record_id: str,
        fallback_agents: Sequence[str] = (),
        legacy_paths: Iterable[Path] = (),
        op: str = "lookup",
    ) -> Path | None:
        path = self.partitioned.find(
            record_id,
            agents=list(dict.fromkeys((agent, *fallback_agents))),
            within_days=self.retention_days,
            op=op,
        )
        if path is not None:
            return path
        for legacy in legacy_paths:
            candidate = Path(legacy)
            if candidate.is_file():
                return candidate
        return None

    def read(
        self,
        *,
        agent: str,
        record_id: str,
        fallback_agents: Sequence[str] = (),
        legacy_paths: Iterable[Path] = (),
        op: str = "lookup",
    ) -> dict[str, Any] | None:
        path = self.find(
            agent=agent,
            record_id=record_id,
            fallback_agents=fallback_agents,
            legacy_paths=legacy_paths,
            op=op,
        )
        if path is None:
            return None
        row = read_json(path, required=False)
        return dict(row) if isinstance(row, Mapping) and row else None

    def remove(
        self,
        *,
        agent: str,
        record_id: str,
        fallback_agents: Sequence[str] = (),
        legacy_paths: Iterable[Path] = (),
    ) -> bool:
        path = self.find(
            agent=agent,
            record_id=record_id,
            fallback_agents=fallback_agents,
            legacy_paths=legacy_paths,
        )
        if path is None:
            return False
        path.unlink(missing_ok=True)
        return True

    def newest(
        self,
        *,
        limit: int,
        agents: list[str] | None = None,
        predicate: Any = None,
        op: str = "list",
    ) -> list[dict[str, Any]]:
        return self.partitioned.newest(
            op=op,
            limit=max(1, int(limit)),
            agents=agents,
            predicate=predicate,
        )

    def paths(
        self,
        *,
        op: str,
        agents: list[str] | None = None,
        newest_first: bool = False,
    ) -> list[Path]:
        """Return retained paths, logging every hour opened per agent.

        This is for explicit history operations such as archiving a retired
        mailbox. Startup, reconciliation and keyed lookup do not call it.
        """

        chosen = agents if agents is not None else self.partitioned.agents()
        found: list[Path] = []
        with self.partitioned.reading(op) as read:
            for agent in chosen:
                hours = list(self.partitioned.hours_newest_first(agent))
                if not newest_first:
                    hours.reverse()
                for hour, start in hours:
                    names = sorted(
                        (name for name in os.listdir(hour) if name.endswith(".json")),
                        reverse=newest_first,
                    )
                    read.opened(agent, start.strftime("%Y-%m-%dT%H"), len(names))
                    found.extend(hour / name for name in names)
        return found

    def expire(self, *, now: datetime | None = None) -> dict[str, int]:
        current = now or datetime.now(timezone.utc)
        return self.partitioned.expire(
            cutoff=current - timedelta(days=self.retention_days),
            max_bytes_per_agent=self.max_bytes_per_agent,
            max_records_per_agent=self.max_records_per_agent,
        )


__all__ = [
    "DEFAULT_MAX_BYTES_PER_AGENT",
    "DEFAULT_MAX_RECORDS_PER_AGENT",
    "KeyedHistoryStore",
    "record_time",
]

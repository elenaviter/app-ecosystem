"""Bounded, resumable migration of flat relay history into hour partitions."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from ..contract.errors import DomainError
from .io import atomic_write_json, read_json, utc_now
from .keyed_history import KeyedHistoryStore, record_time
from .local_store import agent_component


logger = logging.getLogger(__name__)

MIGRATION_SCHEMA = "problem-board.flat-history-migration.v1"
DEFAULT_BATCH_SIZE = 1000
AgentResolver = Callable[[Mapping[str, Any], Path], str]
IdResolver = Callable[[Mapping[str, Any], Path], str]
SlugResolver = Callable[[Mapping[str, Any], Path], str]


def migrate_flat_history(
    *,
    history: KeyedHistoryStore,
    legacy: Path,
    agent_for: AgentResolver,
    record_id_for: IdResolver | None = None,
    slug_for: SlugResolver | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict[str, Any]:
    """Move one flat directory in a bounded batch.

    A moved file is the cursor, so a crash resumes from the files that remain.
    Per-agent marker files retain cumulative counts and completion state. Bad
    input is moved aside for a person and cannot stall every later pass.
    """

    if not legacy.is_dir():
        return {"state": "absent", "moved": {}, "unreadable": 0}

    started = time.monotonic()
    moved: dict[str, int] = {}
    unreadable = 0
    candidates = [
        path
        for path in sorted(legacy.glob("*.json"))
        if not path.name.startswith(".")
    ][: max(1, int(batch_size))]
    for path in candidates:
        try:
            value = read_json(path, required=False)
        except DomainError:
            value = None
        if not isinstance(value, Mapping) or not value:
            _quarantine(path, legacy / ".legacy-unreadable")
            unreadable += 1
            continue
        row = dict(value)
        try:
            agent = agent_component(agent_for(row, path))
            record_id = str(
                record_id_for(row, path) if record_id_for is not None else path.stem
            ).strip()
            if not record_id:
                raise ValueError("history record id is empty")
            created = record_time(
                row,
                fallback=_mtime(path),
            )
            slug = str(slug_for(row, path) if slug_for is not None else "")
        except (TypeError, ValueError):
            _quarantine(path, legacy / ".legacy-unreadable")
            unreadable += 1
            continue
        history.write(
            agent=agent,
            record_id=record_id,
            row=row,
            created=created,
            slug=slug,
        )
        path.unlink(missing_ok=True)
        moved[agent] = moved.get(agent, 0) + 1

    remaining = any(
        path for path in legacy.glob("*.json") if not path.name.startswith(".")
    )
    state = "running" if remaining else "complete"
    agents = set(moved)
    agents.update(history.partitioned.agents())
    elapsed = int((time.monotonic() - started) * 1000)
    for agent in sorted(agents):
        marker = history.partitioned.agent_root(agent) / ".migration.json"
        prior = dict(read_json(marker, required=False) or {})
        record = {
            "schema": MIGRATION_SCHEMA,
            "state": state,
            "moved": int(prior.get("moved") or 0) + moved.get(agent, 0),
            "unreadable": int(prior.get("unreadable") or 0),
            "updated_at": utc_now(),
        }
        if state == "complete":
            record["completed_at"] = utc_now()
        atomic_write_json(marker, record)
        if moved.get(agent):
            logger.info(
                "relay store migrated worker=%s store=%s moved=%d unreadable=0 ms=%d",
                agent,
                history.store,
                moved[agent],
                elapsed,
            )
    if unreadable:
        logger.warning(
            "relay store migrated worker=- store=%s moved=0 unreadable=%d ms=%d",
            history.store,
            unreadable,
            elapsed,
        )
    return {"state": state, "moved": moved, "unreadable": unreadable}


def _mtime(path: Path):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)


def _quarantine(path: Path, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.replace(path, root / path.name)


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "MIGRATION_SCHEMA",
    "migrate_flat_history",
]

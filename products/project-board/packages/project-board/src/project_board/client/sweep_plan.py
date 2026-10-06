# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""The sweep's dry run, recorded so --apply removes only what it listed (W423).

A dry run writes, per removable tree and per removable scratch run, a
fingerprint of what it judged: a tree's head, status and ignored files, a run's
file hashes and closing record. ``--apply`` judges everything again from disk
and removes an entry only when the dry run listed it with the same fingerprint.
Anything that changed between the two, or that the dry run did not list, is
kept. The plan and the per-pin record live in the agent's own workspace.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..contract.errors import DomainError
from .io import atomic_write_json, exclusive_lock, read_json

STATE_FOLDER = Path(".problem-board")
PLAN_NAME = "sweep-plan.json"
# W547: the counts of the latest sweep, for the agent's heartbeat to publish.
SUMMARY_NAME = "sweep-summary.json"
SUMMARY_SCHEMA = "problem-board.sweep-summary.v1"
SUMMARY_COUNTS = ("trees", "unregistered", "orphan", "ended_but_kept", "would_remove", "scratch_kept")
PINS_NAME = "tree-pins.json"
GENERATED_NAME = "tree-generated.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def plan_path(workspace: Path | str) -> Path:
    return Path(workspace).expanduser() / STATE_FOLDER / PLAN_NAME


def write_plan(workspace: Path | str, *, trees: Mapping[str, str], runs: Mapping[str, str]) -> Path:
    """Record what the dry run found removable: path -> fingerprint, per kind."""

    path = plan_path(workspace)
    atomic_write_json(path, {"written_at": _now_iso(), "trees": dict(trees), "runs": dict(runs)})
    return path


def read_plan(workspace: Path | str) -> dict[str, dict[str, str]]:
    """The last dry run's record; empty when there is none or it cannot be read."""

    try:
        value = read_json(plan_path(workspace), required=False)
    except DomainError:
        return {"trees": {}, "runs": {}}
    return {
        "trees": {str(k): str(v) for k, v in (value.get("trees") or {}).items()},
        "runs": {str(k): str(v) for k, v in (value.get("runs") or {}).items()},
    }


def summary_path(workspace: Path | str) -> Path:
    return Path(workspace).expanduser() / STATE_FOLDER / SUMMARY_NAME


def sweep_counts(trees: Any, runs: Any) -> dict[str, int]:
    """Counts only (W547): never a path, a name or a file list leaves the workspace.

    ``trees`` must be the full inventory, before an automatic sweep narrows it
    to ended jobs, or the unregistered folders it exists to report would vanish.
    """

    worktrees = [tree for tree in trees if tree.kind != "clone"]
    return {
        "trees": len(worktrees),
        "unregistered": sum(1 for tree in worktrees if tree.kind == "unregistered"),
        "orphan": sum(1 for tree in worktrees if tree.kind == "orphan"),
        "ended_but_kept": sum(1 for tree in worktrees if tree.ended and not tree.removable),
        "would_remove": sum(1 for tree in worktrees if tree.removable),
        "scratch_kept": sum(1 for run in runs if not run.removable),
    }


def write_summary(workspace: Path | str, counts: Mapping[str, int]) -> Path:
    """Record the latest sweep's counts with the time it judged them."""

    path = summary_path(workspace)
    atomic_write_json(path, {
        "schema": SUMMARY_SCHEMA,
        "observed_at": _now_iso(),
        **{name: max(0, int(counts.get(name) or 0)) for name in SUMMARY_COUNTS},
    })
    return path


def read_summary(workspace: Path | str) -> dict[str, Any] | None:
    """The latest sweep's counts, or None when there is none or it cannot be read.

    No summary means no sweep yet, which is reported as unknown, never as zero.
    """

    try:
        value = read_json(summary_path(workspace), required=False)
    except DomainError:
        return None
    if not isinstance(value, Mapping) or value.get("schema") != SUMMARY_SCHEMA:
        return None
    observed = value.get("observed_at")
    counts = {name: value.get(name) for name in SUMMARY_COUNTS}
    if not isinstance(observed, str) or not observed or not all(
        type(count) is int and count >= 0 for count in counts.values()
    ):
        return None
    return {"observed_at": observed, **counts}


def _pins_path(workspace: Path | str) -> Path:
    return Path(workspace).expanduser() / STATE_FOLDER / PINS_NAME


def read_pins(workspace: Path | str) -> dict[str, list[str]]:
    """Tree path -> the consumers (a review, a release) that still need the tree."""

    try:
        value = read_json(_pins_path(workspace), required=False)
    except DomainError:
        # An unreadable pin record pins nothing it can name, so the sweep says so.
        return {"*": ["pin record unreadable"]}
    return {str(path): [str(ref) for ref in refs] for path, refs in (value.get("pins") or {}).items() if refs}


def set_pin(workspace: Path | str, tree: Path | str, consumer: str, *, pinned: bool) -> dict[str, list[str]]:
    """Add or remove one consumer of one tree in this workspace."""

    consumer = str(consumer or "").strip()
    if not consumer:
        raise DomainError("workspace_pin_consumer_required", "Name the consumer, for example a review or release ref.")
    target = Path(tree).expanduser()
    if not target.is_dir():
        raise DomainError("workspace_pin_path_missing", f"{tree} is not a directory on this host.")
    key = str(target.resolve())
    path = _pins_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with exclusive_lock(path.with_suffix(".lock")):
        try:
            value = read_json(path, required=False)
        except DomainError as exc:
            raise DomainError("workspace_pin_record_unreadable", f"{path} cannot be read; fix it by hand first.") from exc
        pins: dict[str, list[str]] = {str(k): list(v) for k, v in (value.get("pins") or {}).items()}
        refs = [ref for ref in pins.get(key, []) if ref != consumer]
        if pinned:
            refs.append(consumer)
        if refs:
            pins[key] = refs
        else:
            pins.pop(key, None)
        atomic_write_json(path, {"updated_at": _now_iso(), "pins": pins})
    return pins


def _generated_path(workspace: Path | str) -> Path:
    return Path(workspace).expanduser() / STATE_FOLDER / GENERATED_NAME


def read_generated(workspace: Path | str) -> dict[str, dict[str, str]]:
    """Tree path -> {ignored relative path: the command that makes it again}.

    Only what an owner declared counts: a folder name (build, dist, a cache)
    never proves that what sits in it is regenerable. Unreadable means none.
    """

    try:
        value = read_json(_generated_path(workspace), required=False)
    except DomainError:
        return {}
    return {
        str(tree): {str(rel): str(command) for rel, command in (entries or {}).items() if str(command).strip()}
        for tree, entries in (value.get("generated") or {}).items()
    }


def declare_generated(workspace: Path | str, tree: Path | str, relative: str, command: str) -> dict[str, dict[str, str]]:
    """Declare one ignored path in one tree regenerable, naming the command that makes it."""

    relative = str(relative or "").strip().strip("/")
    command = str(command or "").strip()
    if not relative or relative.startswith("..") or Path(relative).is_absolute():
        raise DomainError("workspace_generated_path_invalid", "--generated is a path relative to the tree.")
    if not command:
        raise DomainError("workspace_generated_command_required", "--generated-by names the command that makes it again.")
    target = Path(tree).expanduser()
    if not target.is_dir():
        raise DomainError("workspace_pin_path_missing", f"{tree} is not a directory on this host.")
    key = str(target.resolve())
    path = _generated_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with exclusive_lock(path.with_suffix(".lock")):
        try:
            value = read_json(path, required=False)
        except DomainError as exc:
            raise DomainError("workspace_generated_record_unreadable", f"{path} cannot be read; fix it by hand first.") from exc
        generated = {str(k): dict(v) for k, v in (value.get("generated") or {}).items()}
        generated.setdefault(key, {})[relative] = command
        atomic_write_json(path, {"updated_at": _now_iso(), "generated": generated})
    return generated


__all__ = [
    "declare_generated", "plan_path", "read_generated", "read_pins", "read_plan", "set_pin", "write_plan",
]

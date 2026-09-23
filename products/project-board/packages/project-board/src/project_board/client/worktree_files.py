# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""The tracked files a worker is changing, read from its declared worktree (W278 part B).

Team decision 2026-09-23 (P4a): observed files in flight are a derived signal,
not the contract and not the handoff. Each host's relay reads the worktree the
worker declared on that host, publishes tracked paths only (what changed since
the assignment's base commit, plus what is modified or staged now), never
untracked names or contents, and republishes only when the set changes. The
pushed branch is the source contract across machines, and the worker's one
intended-scope line (P4b) is its statement of intent.

Two git commands, both read-only and bounded:

    git --no-optional-locks diff --name-only -z <base_commit>...HEAD
    git --no-optional-locks status --porcelain -z --untracked-files=no

The first is what changed since the base, the second what is modified or
staged now, tracked only. ``--no-optional-locks`` keeps a background reader
from ever taking the worker's index lock, ``-z`` keeps unusual file names
intact.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Iterable, Mapping

from .io import content_hash, utc_now

MAX_OBSERVED_PATHS = 200
GIT_TIMEOUT_SECONDS = 10


def _git(path: Path, *args: str) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            # A background reader never takes the worker's .git/index.lock:
            # git status would otherwise refresh the index under a commit in
            # progress and the worker would see "index.lock: File exists".
            ["git", "--no-optional-locks", "-C", str(path), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return completed.returncode, (completed.stdout if completed.returncode == 0 else completed.stderr)


def worktree_root(path: Path | str) -> Path | None:
    """The git worktree root at ``path``, or None when it is not one."""

    candidate = Path(path).expanduser()
    if not candidate.is_dir():
        return None
    code, out = _git(candidate, "rev-parse", "--show-toplevel")
    if code != 0 or not out.strip():
        return None
    return Path(out.strip())


def _status_paths(text: str) -> list[str]:
    """Paths from ``git status --porcelain -z --untracked-files=no``.

    Fields are NUL-separated. Each entry is two status letters, a space and
    the path. A rename or copy (``R`` or ``C`` in either column) is followed by
    one more field, the original path, which is skipped: the path that exists
    now is the one in flight. Nothing is C-quoted with ``-z``, so a name with
    spaces or quotes comes through as it is.
    """

    fields = text.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        if path:
            paths.append(path)
        if "R" in status or "C" in status:
            index += 1  # the original name, not in flight
    return paths


def _diff_paths(text: str) -> list[str]:
    """Paths from ``git diff --name-only -z``: NUL-separated, unquoted."""

    return [field for field in text.split("\0") if field]


def observe_worktree(
    path: Path | str,
    *,
    base_commit: str = "",
    limit: int = MAX_OBSERVED_PATHS,
) -> dict[str, Any]:
    """What is changing in one worktree, as tracked paths, bounded.

    Returns ``paths`` (sorted, at most ``limit``), ``path_count`` (the total
    before the cut), ``truncated`` (how many were cut), ``head_commit``,
    ``observed_at``, and ``error`` when the worktree could not be read. An
    unknown or unreachable base commit leaves the committed leg out and says so
    in ``error`` without failing the observation: the working tree still tells
    what is modified now.
    """

    root = worktree_root(path)
    if root is None:
        return {
            "paths": [],
            "path_count": 0,
            "truncated": 0,
            "head_commit": "",
            "observed_at": utc_now(),
            "error": "not_a_worktree",
        }
    errors: list[str] = []
    found: set[str] = set()
    code, head = _git(root, "rev-parse", "HEAD")
    head_commit = head.strip() if code == 0 else ""
    base = str(base_commit or "").strip()
    if base:
        code, out = _git(root, "diff", "--name-only", "-z", f"{base}...HEAD")
        if code == 0:
            found.update(_diff_paths(out))
        else:
            errors.append("base_commit_unreachable")
    code, out = _git(root, "status", "--porcelain", "-z", "--untracked-files=no")
    if code == 0:
        found.update(_status_paths(out))
    else:
        errors.append("status_unreadable")
    ordered = sorted(found)
    cut = max(0, len(ordered) - max(1, int(limit)))
    return {
        "paths": ordered[: max(1, int(limit))] if ordered else [],
        "path_count": len(ordered),
        "truncated": cut,
        "head_commit": head_commit,
        "observed_at": utc_now(),
        "error": ",".join(errors),
    }


ACTIVE_OBSERVED_ASSIGNMENT_STATES = frozenset({"routing", "assigned", "working", "blocked"})


def observe_assignments(
    workspaces: Iterable[Mapping[str, Any]],
    assignments: Iterable[Mapping[str, Any]],
    *,
    worker_name: str,
    limit: int = MAX_OBSERVED_PATHS,
    observe: Any = None,
) -> list[dict[str, Any]]:
    """One observation per declared worktree whose assignment this worker still holds.

    ``assignments`` are the field's assignment records (with ``sources`` from
    W278 part A); the base commit for a repository comes from there. A
    declaration for an assignment that ended, or that belongs to another
    worker, yields nothing. ``observe`` replaces the git reader, which is how the
    relay keeps one observation per worktree for an interval instead of running
    git on every cycle.
    """

    mine = str(worker_name or "").lower()
    active = {
        str(row.get("assignment_ref") or ""): row
        for row in assignments
        if str(row.get("worker_name") or "").lower() == mine
        and str(row.get("state") or "") in ACTIVE_OBSERVED_ASSIGNMENT_STATES
    }
    observations: list[dict[str, Any]] = []
    for workspace in workspaces:
        assignment = active.get(str(workspace.get("assignment_ref") or ""))
        if assignment is None:
            continue
        repository_ref = str(workspace.get("repository_ref") or "")
        base_commit = next(
            (
                str(source.get("base_commit") or "")
                for source in (assignment.get("sources") or [])
                if isinstance(source, Mapping)
                and str(source.get("repository_ref") or "") == repository_ref
            ),
            "",
        )
        observer = observe or observe_worktree
        seen = observer(str(workspace.get("path") or ""), base_commit=base_commit, limit=limit)
        observations.append(
            {
                "assignment_ref": str(workspace.get("assignment_ref") or ""),
                "repository_ref": repository_ref,
                "paths": list(seen.get("paths") or []),
                "path_count": int(seen.get("path_count") or 0),
                "truncated": int(seen.get("truncated") or 0),
                "head_commit": str(seen.get("head_commit") or ""),
                "observed_at": str(seen.get("observed_at") or ""),
                "error": str(seen.get("error") or ""),
            }
        )
    return observations


OBSERVE_INTERVAL_SECONDS = 30.0


class WorktreeObserverCache:
    """One observation per worktree per interval (review on #43).

    The relay cycles every few seconds and a declared worktree can be as large
    as kdcube-ai-app, so git runs at most once per interval per path unless the
    caller asks for a fresh read (a forced heartbeat).
    """

    def __init__(self, *, interval_seconds: float = OBSERVE_INTERVAL_SECONDS, clock: Any = None) -> None:
        import time

        self.interval_seconds = float(interval_seconds)
        self._clock = clock or time.monotonic
        self._last: dict[str, tuple[float, dict[str, Any]]] = {}

    def __call__(self, path: str, *, base_commit: str = "", limit: int = MAX_OBSERVED_PATHS, fresh: bool = False) -> dict[str, Any]:
        key = f"{path}\n{base_commit}\n{int(limit)}"
        now = self._clock()
        cached = self._last.get(key)
        if cached is not None and not fresh and now - cached[0] < self.interval_seconds:
            return dict(cached[1])
        seen = observe_worktree(path, base_commit=base_commit, limit=limit)
        self._last[key] = (now, dict(seen))
        return seen

    def forget(self, path: str = "") -> None:
        if not path:
            self._last.clear()
            return
        for key in [k for k in self._last if k.split("\n", 1)[0] == path]:
            self._last.pop(key, None)


def observations_signature(observations: Iterable[Mapping[str, Any]]) -> str:
    """What "the set changed" means across assignments: paths, heads, errors, not the clock."""

    return content_hash(
        [
            {
                key: value.get(key)
                for key in ("assignment_ref", "repository_ref", "paths", "truncated", "head_commit", "error")
            }
            for value in observations
        ]
    )


def observation_signature(observation: Mapping[str, Any]) -> str:
    """What "the set changed" means: the paths, the head and the error, not the clock."""

    return content_hash(
        {
            "paths": list(observation.get("paths") or []),
            "truncated": int(observation.get("truncated") or 0),
            "head_commit": str(observation.get("head_commit") or ""),
            "error": str(observation.get("error") or ""),
        }
    )


__all__ = [
    "ACTIVE_OBSERVED_ASSIGNMENT_STATES",
    "MAX_OBSERVED_PATHS",
    "OBSERVE_INTERVAL_SECONDS",
    "WorktreeObserverCache",
    "observations_signature",
    "observe_assignments",
    "observation_signature",
    "observe_worktree",
    "worktree_root",
]

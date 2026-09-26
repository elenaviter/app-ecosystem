"""Read roots: where a host reads a project's setup from (W262).

A repository map entry may name, beside its ``root`` (the checkout agents and
the journal workspace write through), a ``read_root``: a dedicated detached
worktree of the same repository that nobody edits. ``pb worker context`` reads
the project's setup, facts, environment, instructions and runtime profiles
from it, so a working checkout that cannot fast-forward over its owner's edits
never makes the setup stale.

The relay's housekeeping advances each read root to its integration ref
(``origin/main`` by default) when the tree is clean, at most every
``ADVANCE_INTERVAL_SECONDS`` per alias, and names a failure once per change.
``context()`` itself uses local refs only: a lag is named in
``project_setup_issues``, never fatal.

Every git call here takes list arguments, no shell, and a timeout, and never
raises.
"""

from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .io import atomic_write_json, exclusive_lock, parse_utc, read_json


logger = logging.getLogger(__name__)

DEFAULT_READ_REF = "origin/main"
READ_ROOTS_STATE = "read-roots.json"
READ_ROOTS_STATE_SCHEMA = "problem-board.read-roots.v1"
ADVANCE_INTERVAL_SECONDS = 300
CONTEXT_GIT_TIMEOUT_SECONDS = 5.0
HOUSEKEEPING_GIT_TIMEOUT_SECONDS = 30.0


def git(
    directory: Path | str, *args: str, timeout: float = CONTEXT_GIT_TIMEOUT_SECONDS
) -> tuple[int, str]:
    """``git -C <directory> <args>``: (return code, stripped output); -1 on failure to run."""

    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        completed = subprocess.run(
            ["git", "-C", str(directory), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return -1, type(exc).__name__
    output = (completed.stdout or "").strip()
    if completed.returncode != 0:
        output = (completed.stderr or completed.stdout or "").strip().splitlines()
        output = output[-1] if output else f"exit {completed.returncode}"
    return completed.returncode, output


def head_commit(directory: Path | str | None) -> str:
    """The HEAD commit of the git tree at ``directory``, or "" when it is not one."""

    if not directory or not Path(directory).is_dir():
        return ""
    code, output = git(directory, "rev-parse", "--verify", "--quiet", "HEAD")
    return output if code == 0 else ""


def ref_commit(directory: Path | str, ref: str) -> str:
    code, output = git(directory, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return output if code == 0 else ""


def split_read_ref(read_ref: str) -> tuple[str, str]:
    """``origin/main`` -> (``origin``, ``main``)."""

    remote, _, branch = read_ref.partition("/")
    return remote, branch


def read_root_lag(alias: str, read_root: Path, read_ref: str) -> str:
    """One issue line when ``read_root`` is behind ``read_ref``, else "".

    Local refs only: no fetch happens here.
    """

    if not read_root.is_dir():
        return (
            f"read root of {alias} at {read_root} is missing; "
            f"the setup was read from its root checkout instead"
        )
    head = head_commit(read_root)
    if not head:
        return f"read root of {alias} at {read_root} is not a git tree"
    target = ref_commit(read_root, read_ref)
    if not target:
        return (
            f"read root of {alias} at {read_root} (commit {head}) "
            f"has no {read_ref} to compare with"
        )
    if head == target:
        return ""
    code, _ = git(read_root, "merge-base", "--is-ancestor", head, target)
    if code == 0:
        count_code, count = git(read_root, "rev-list", "--count", f"{head}..{target}")
        behind = f"{count} commits " if count_code == 0 and count.isdigit() else ""
        return (
            f"read root of {alias} at {read_root} is at commit {head}, "
            f"{behind}behind {read_ref} at commit {target}"
        )
    code, _ = git(read_root, "merge-base", "--is-ancestor", target, head)
    if code == 0:
        return ""  # ahead of the ref: nothing newer to read
    return (
        f"read root of {alias} at {read_root} is at commit {head}, "
        f"which is not on {read_ref} at commit {target}"
    )


def _now_text(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _advance_one(alias: str, read_root: Path, read_ref: str) -> tuple[str, str]:
    """(outcome, reason) for one read root: advanced, unchanged, skipped_dirty or failed."""

    timeout = HOUSEKEEPING_GIT_TIMEOUT_SECONDS
    if not read_root.is_dir():
        return "failed", "read root is missing"
    remote, branch = split_read_ref(read_ref)
    if not remote or not branch:
        return "failed", f"read_ref {read_ref!r} is not <remote>/<branch>"
    code, output = git(read_root, "fetch", "--quiet", remote, branch, timeout=timeout)
    if code != 0:
        return "failed", f"fetch {remote} {branch} failed: {output}"
    code, output = git(read_root, "status", "--porcelain", timeout=timeout)
    if code != 0:
        return "failed", f"status failed: {output}"
    if output:
        return "skipped_dirty", "the read root has uncommitted changes"
    head = head_commit(read_root)
    target = ref_commit(read_root, read_ref)
    if not target:
        return "failed", f"{read_ref} does not resolve after fetch"
    if head == target:
        return "unchanged", ""
    code, output = git(
        read_root, "checkout", "--quiet", "--detach", read_ref, timeout=timeout
    )
    if code != 0:
        return "failed", f"checkout {read_ref} failed: {output}"
    return "advanced", ""


def advance_read_roots(
    repositories: Any,
    state_path: Path,
    *,
    now: datetime | None = None,
    interval_seconds: int = ADVANCE_INTERVAL_SECONDS,
) -> dict[str, Any]:
    """Advance every clean read root of ``repositories`` to its read ref.

    At most once per ``interval_seconds`` per alias, recorded in
    ``state_path`` so a restart neither repeats nor skips it. Never raises.
    """

    current = now or datetime.now(timezone.utc)
    summary: dict[str, Any] = {
        "advanced": 0,
        "unchanged": 0,
        "skipped_dirty": 0,
        "failed": 0,
        "not_due": 0,
    }
    read_roots: Mapping[str, Path] = getattr(repositories, "read_roots", {}) or {}
    if not read_roots:
        return summary
    read_refs: Mapping[str, str] = getattr(repositories, "read_refs", {}) or {}
    try:
        with exclusive_lock(state_path.with_suffix(".lock")):
            state = dict(read_json(state_path, required=False) or {})
            aliases = dict(state.get("aliases") or {})
            for alias, read_root in sorted(read_roots.items()):
                read_ref = read_refs.get(alias) or DEFAULT_READ_REF
                previous = dict(aliases.get(alias) or {})
                checked = (
                    parse_utc(str(previous.get("checked_at")))
                    if previous.get("checked_at")
                    else None
                )
                if (
                    checked is not None
                    and previous.get("read_root") == str(read_root)
                    and 0 <= (current - checked).total_seconds() < interval_seconds
                ):
                    summary["not_due"] += 1
                    continue
                try:
                    outcome, reason = _advance_one(alias, Path(read_root), read_ref)
                except Exception as exc:  # noqa: BLE001 - housekeeping never raises
                    outcome, reason = "failed", type(exc).__name__
                summary[outcome] += 1
                condition = (outcome if outcome in {"skipped_dirty", "failed"} else "ok", reason)
                if condition != (previous.get("condition"), previous.get("reason")) and condition[0] != "ok":
                    logger.warning(
                        "Problem Board read root not advanced alias=%s read_root=%s reason=%s",
                        alias,
                        read_root,
                        reason or outcome,
                    )
                aliases[alias] = {
                    "read_root": str(read_root),
                    "read_ref": read_ref,
                    "checked_at": _now_text(current),
                    "outcome": outcome,
                    "condition": condition[0],
                    "reason": reason,
                    "commit": head_commit(read_root),
                }
            state.update(schema=READ_ROOTS_STATE_SCHEMA, aliases=aliases)
            atomic_write_json(state_path, state)
    except Exception:  # noqa: BLE001 - housekeeping never stops the relay
        logger.warning(
            "Problem Board read-root housekeeping failed state=%s", state_path, exc_info=True
        )
    return summary


__all__ = [
    "ADVANCE_INTERVAL_SECONDS",
    "DEFAULT_READ_REF",
    "READ_ROOTS_STATE",
    "advance_read_roots",
    "head_commit",
    "read_root_lag",
]

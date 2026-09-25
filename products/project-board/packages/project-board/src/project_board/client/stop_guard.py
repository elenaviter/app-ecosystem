"""A Claude Code turn never ends without this session's watch running (W182).

A Claude Code worker hears the board through one ``pb worker watch`` that the
harness ends after 30 minutes. Re-arming it is the model's obligation, and on
2026-09-18 two workers skipped it and went silent for hours. The Stop hook
makes the obligation independent of the model noticing: when a worker's turn
ends with no watch running, the stop is blocked once with the exact commands
that re-arm it, so a missed re-arm costs one turn.

The hook acts only for a session enrolled as a Problem Board worker and
attending (not detached). It never blocks when anything here fails: a guard
that can trap a session is worse than the silence it prevents.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any, Callable, Mapping

from ..contract.errors import DomainError

# A listener in these states owes no watch.
_NOT_OBLIGED = {"detached", "retired", "never_listened"}


def watch_process_alive(pid: int) -> bool:
    """Whether ``pid`` is a running ``pb worker watch``, not a reused pid."""

    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    try:
        command = subprocess.run(
            ["ps", "-ww", "-o", "command=", "-p", str(pid)],
            check=False, capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "worker watch" in command


def rearm_reason(runtime_session_id: str) -> str:
    """The block message: what is missing and the exact commands that fix it."""

    session = runtime_session_id
    return (
        "The Problem Board watch for this session is not running, so board mail cannot wake it. "
        "Before ending this turn, re-arm it: start the Monitor tool with the command "
        f"`exec pb worker watch --runtime-kind claude-code --runtime-session-id {session} 2>&1` "
        "and timeout_ms 1800000, then run "
        f"`pb worker receive --runtime-kind claude-code --runtime-session-id {session} --format brief` "
        "and handle and settle what it returns."
    )


def stop_guard_decision(
    payload: Mapping[str, Any],
    *,
    field: Any,
    worker_name: str,
    process_alive: Callable[[int], bool] = watch_process_alive,
) -> dict[str, str] | None:
    """The Stop hook's answer: ``None`` lets the turn end, a block names the fix.

    ``stop_hook_active`` means this stop already follows a block, so the turn
    ends: the hook blocks at most once per turn and cannot trap a model that
    cannot re-arm.
    """

    if payload.get("stop_hook_active"):
        return None
    try:
        worker = field.read_worker(worker_name)
    except DomainError as exc:
        if exc.code == "field_record_not_found":
            return None
        raise
    stable = str(worker.get("worker_name") or worker_name)
    if str(field.worker_reachability(stable).get("state") or "") in _NOT_OBLIGED:
        return None
    attachment = field.read_watch_attachment(stable) or {}
    if process_alive(int(attachment.get("pid") or 0)):
        return None
    session = str(payload.get("session_id") or worker.get("runtime_session_id") or "")
    return {"decision": "block", "reason": rearm_reason(session)}

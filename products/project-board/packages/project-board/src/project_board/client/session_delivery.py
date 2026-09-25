from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .codex_queue import (
    CodexQueueProtocolError,
    PROBLEM_BOARD_WAKE_PREFIX,
    reconcile_problem_board_wakes,
)
from .host_config import WorkerChannelConfig


CODEX_QUEUE_ADAPTER = "codex-queue"
SESSION_WATCH_ADAPTER = "session-owned-watch"
_QUEUE_SUCCESS = re.compile(
    r"^Queued message (?P<submission_id>\S+) for thread (?P<thread_id>\S+)\.$"
)


def delivery_adapter(runtime_kind: str) -> str:
    return CODEX_QUEUE_ADAPTER if runtime_kind == "codex" else SESSION_WATCH_ADAPTER


def _codex_executable() -> Path | None:
    candidates = [
        "/usr/local/bin/codex",
        "/opt/homebrew/bin/codex",
        str(Path.home() / ".local" / "node" / "bin" / "codex"),
        str(Path.home() / ".local" / "bin" / "codex"),
        shutil.which("codex"),
    ]
    for value in candidates:
        if not value:
            continue
        path = Path(value).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


def _command_environment(executable: Path) -> dict[str, str]:
    """Give an npm-installed Codex shim its sibling runtime under launchd."""

    environment = os.environ.copy()
    path_entries = [str(executable.parent)]
    for candidate in (Path("/usr/local/bin"), Path("/opt/homebrew/bin")):
        if (candidate / "node").is_file():
            path_entries.append(str(candidate))
    path_entries.extend(
        value for value in environment.get("PATH", "").split(os.pathsep) if value
    )
    environment["PATH"] = os.pathsep.join(dict.fromkeys(path_entries))
    return environment


def notify_agent_session(
    channel: WorkerChannelConfig,
    *,
    event_kind: str,
    wake_id: str = "",
    wake_provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Queue a standard inbox-check instruction; never start a model."""

    adapter = delivery_adapter(channel.runtime_kind)
    if event_kind != "input.available":
        return {
            "adapter": adapter,
            "state": "awaiting_input" if adapter == CODEX_QUEUE_ADAPTER else "session_owned",
            "event_kind": event_kind,
            "delivered": False,
            "reason": "status_event_does_not_wake_model",
        }
    if adapter != CODEX_QUEUE_ADAPTER:
        return {
            "adapter": adapter,
            "state": "session_owned",
            "event_kind": event_kind,
            "delivered": False,
            "reason": "runtime_has_no_supported_local_queue",
        }
    executable = _codex_executable()
    if executable is None:
        return {
            "adapter": adapter,
            "state": "unavailable",
            "event_kind": event_kind,
            "delivered": False,
            "reason": "codex_command_not_found",
        }
    receive_command = "pb worker receive"
    if wake_id:
        receive_command = f"{receive_command} --wake-id {wake_id}"
    provenance = dict(wake_provenance or {})
    provenance_line = (
        "Wake provenance: "
        f"id={wake_id or '(legacy)'}; "
        f"created_at={provenance.get('created_at') or '(unknown)'}; "
        f"first_attempt_at={provenance.get('first_attempt_at') or '(unknown)'}; "
        f"attempt={provenance.get('attempt') or 1}; "
        f"host={provenance.get('host_id') or '(unknown)'}; "
        f"relay={provenance.get('relay_id') or '(unknown)'}; "
        f"process={provenance.get('process_id') or '(unknown)'}; "
        f"attempted_at={provenance.get('attempted_at') or '(unknown)'}"
    )
    message = (
        f"{PROBLEM_BOARD_WAKE_PREFIX} "
        "Problem Board has addressed input for this already-running session. "
        f"{provenance_line}. "
        f"Run `{receive_command}` now, handle each returned item, and settle every "
        "lease exactly once. This instruction contains no task body; read it only "
        "from your machine-local worker inbox."
    )
    try:
        completed = subprocess.run(
            [
                str(executable),
                "queue",
                "--thread",
                channel.runtime_session_id,
                "--message",
                message,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
            env=_command_environment(executable),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "adapter": adapter,
            "state": "unreachable",
            "event_kind": event_kind,
            "delivered": False,
            "reason": type(exc).__name__,
        }
    if completed.returncode != 0:
        return {
            "adapter": adapter,
            "state": "unreachable",
            "event_kind": event_kind,
            "delivered": False,
            "reason": "codex_queue_failed",
            "returncode": completed.returncode,
        }
    result = {
        "adapter": adapter,
        "state": "attached",
        "event_kind": event_kind,
        "delivered": True,
        "wake_provenance": provenance,
    }
    stdout = str(getattr(completed, "stdout", "") or "").strip()
    match = _QUEUE_SUCCESS.fullmatch(stdout)
    if match and match.group("thread_id") == channel.runtime_session_id:
        result["queued_submission_id"] = match.group("submission_id")
        result["queue_identity_state"] = "recorded"
    else:
        # Queue acceptance is still authoritative. Reconciliation can recover
        # the identity from the wake marker if this Codex version changes its
        # human-readable success line.
        result["queue_identity_state"] = "recoverable"
    return result


def reconcile_agent_session_queue(
    channel: WorkerChannelConfig,
    *,
    expected_wake_id: str = "",
    expected_submission_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Reconcile PB's wake record with the native Codex queue."""

    adapter = delivery_adapter(channel.runtime_kind)
    if adapter != CODEX_QUEUE_ADAPTER:
        return {
            "adapter": adapter,
            "state": "session_owned",
            "reconciled": True,
            "expected_wake_id": expected_wake_id,
        }
    executable = _codex_executable()
    if executable is None:
        return {
            "adapter": adapter,
            "state": "unavailable",
            "reconciled": False,
            "expected_wake_id": expected_wake_id,
            "reason": "codex_command_not_found",
        }
    try:
        return reconcile_problem_board_wakes(
            executable,
            environment=_command_environment(executable),
            thread_id=channel.runtime_session_id,
            expected_wake_id=expected_wake_id,
            expected_submission_ids=expected_submission_ids,
        )
    except CodexQueueProtocolError as exc:
        return {
            "adapter": adapter,
            "state": "unreachable",
            "reconciled": False,
            "expected_wake_id": expected_wake_id,
            "reason": exc.reason,
            "detail": exc.detail,
        }


__all__ = [
    "CODEX_QUEUE_ADAPTER",
    "SESSION_WATCH_ADAPTER",
    "delivery_adapter",
    "notify_agent_session",
    "reconcile_agent_session_queue",
]

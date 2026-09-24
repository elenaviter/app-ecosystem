# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""An agent's usage-limit state, read from the runtime itself (W26).

Silence already has a meaning on the board, unreachable, and using it for
"out of tokens" too made both useless. So the limit state is never inferred
from silence or from the shape of an error. It is read from what each runtime
writes about itself:

- **Codex** appends ``token_count`` events to the session's rollout file
  (``~/.codex/sessions/YYYY/MM/DD/rollout-<started>-<session-id>.jsonl``)
  whose ``payload.rate_limits`` carries the ``primary`` and ``secondary``
  windows (``used_percent``, ``window_minutes``, ``resets_at`` in epoch
  seconds), ``credits``, ``plan_type`` and ``rate_limit_reached_type``.
  Verified on codex-cli 0.151 and 0.154 files on 2026-09-23.
- **Claude Code** hands its status line command a JSON whose ``rate_limits``
  has ``five_hour`` and ``seven_day`` (``used_percentage``, ``resets_at`` in
  epoch seconds), and fires the ``StopFailure`` hook with matcher
  ``rate_limit`` when a turn ends on one (code.claude.com docs, statusline and
  hooks, 2.1.281). Both are commands the user's settings run, so a
  ``pb worker`` command receives them and records the state in the field.

The state is one small record on the session row, projected in the relay
heartbeat and shown on the worker's card. It clears when the runtime reports a
lower value or the reset time passes.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

CODEX_SESSIONS_ROOT = Path("~/.codex/sessions")
CODEX_TAIL_BYTES = 256 * 1024

SOURCE_CODEX_ROLLOUT = "codex-rollout"
SOURCE_CLAUDE_STATUSLINE = "claude-code-statusline"
SOURCE_CLAUDE_STOP_FAILURE = "claude-code-stop-failure"

KIND_OK = "ok"
KIND_RATE_LIMITED = "rate_limited"
KIND_OUT_OF_TOKENS = "out_of_tokens"
KIND_UNKNOWN = "unknown"
# The session ended on an error that is neither a usage limit nor credits
# (authentication, an outage). ``reached`` names it, so the board can tell
# credits, limits, auth and outage apart (W26 acceptance 9).
KIND_STOPPED = "stopped"

LIMIT_STATE_KINDS = frozenset(
    {KIND_OK, KIND_RATE_LIMITED, KIND_OUT_OF_TOKENS, KIND_STOPPED, KIND_UNKNOWN}
)

# Claude Code ``StopFailure`` matcher values (code.claude.com hooks) that mean
# the session cannot continue. The procedure's settings snippet matches every
# one, and each maps to a kind below.
STOP_FAILURE_RATE_LIMITED = frozenset({"rate_limit"})
STOP_FAILURE_OUT_OF_TOKENS = frozenset({"billing_error", "account_on_hold"})
STOP_FAILURE_ERRORS: tuple[str, ...] = (
    "rate_limit",
    "billing_error",
    "account_on_hold",
    "authentication_failed",
    "oauth_org_not_allowed",
    "overloaded",
    "server_error",
)
_STOP_FAILURE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _utc(value: Any) -> str:
    """Epoch seconds or an ISO string as UTC ``YYYY-MM-DDTHH:MM:SSZ``, else empty."""

    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)):
        try:
            return (
                datetime.fromtimestamp(float(value), tz=timezone.utc)
                .replace(microsecond=0)
                .strftime("%Y-%m-%dT%H:%M:%SZ")
            )
        except (OverflowError, OSError, ValueError):
            return ""
    text = str(value).strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _percent(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def unknown_state(source: str, *, observed_at: str = "") -> dict[str, Any]:
    return {
        "kind": KIND_UNKNOWN,
        "source": source,
        "windows": [],
        "reached": "",
        "resets_at": "",
        "observed_at": _utc(observed_at),
    }


# --------------------------------------------------------------------------- Codex


def codex_rollout_path(
    session_id: str,
    *,
    sessions_root: Path | str | None = None,
) -> Path | None:
    """The rollout file of one Codex session, or None when none is on this host.

    Rollouts live three directories deep (year, month, day) and the file name
    ends with the session id, so the search is bounded to that layout.
    """

    clean = str(session_id or "").strip()
    if not clean:
        return None
    root = Path(sessions_root or CODEX_SESSIONS_ROOT).expanduser()
    if not root.is_dir():
        return None
    suffix = f"-{clean}.jsonl"
    newest: Path | None = None
    newest_mtime = -1.0
    for candidate in root.glob(f"*/*/*/rollout-*{suffix}"):
        try:
            mtime = candidate.stat().st_mtime
        except OSError:
            continue
        if mtime > newest_mtime:
            newest, newest_mtime = candidate, mtime
    return newest


def _tail_lines(path: Path, *, tail_bytes: int) -> Iterable[str]:
    """The last lines of a file, newest first, without reading the whole file."""

    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        start = max(0, size - tail_bytes)
        handle.seek(start)
        chunk = handle.read()
    text = chunk.decode("utf-8", errors="replace")
    lines = text.split("\n")
    if start > 0 and lines:
        lines = lines[1:]  # the first piece is a cut line
    for line in reversed(lines):
        if line.strip():
            yield line


def read_codex_rate_limits(
    path: Path | str,
    *,
    tail_bytes: int = CODEX_TAIL_BYTES,
) -> tuple[str, dict[str, Any]] | None:
    """The newest ``token_count.rate_limits`` in a rollout, with its timestamp."""

    file = Path(path)
    try:
        for line in _tail_lines(file, tail_bytes=tail_bytes):
            if '"token_count"' not in line or '"rate_limits"' not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            payload = record.get("payload") if isinstance(record, Mapping) else None
            if not isinstance(payload, Mapping) or payload.get("type") != "token_count":
                continue
            limits = payload.get("rate_limits")
            if isinstance(limits, Mapping):
                return str(record.get("timestamp") or ""), dict(limits)
    except OSError:
        return None
    return None


def limit_state_from_codex(
    rate_limits: Mapping[str, Any] | None,
    *,
    observed_at: str = "",
) -> dict[str, Any]:
    """Codex's own rate-limit snapshot as the board's limit state.

    ``rate_limit_reached_type`` names the window Codex says is exhausted. A
    window at or past 100 percent says the same without the name. Credits
    reaching a spend control is out of tokens rather than a window.
    """

    if not isinstance(rate_limits, Mapping):
        return unknown_state(SOURCE_CODEX_ROLLOUT, observed_at=observed_at)
    windows: list[dict[str, Any]] = []
    for name in ("primary", "secondary"):
        window = rate_limits.get(name)
        if not isinstance(window, Mapping):
            continue
        used = _percent(window.get("used_percent"))
        windows.append(
            {
                "name": name,
                "used_percent": used,
                "window_minutes": window.get("window_minutes"),
                "resets_at": _utc(window.get("resets_at")),
            }
        )
    reached = str(rate_limits.get("rate_limit_reached_type") or "").strip()
    exhausted = [
        window for window in windows
        if window["used_percent"] is not None and window["used_percent"] >= 100
    ]
    kind = KIND_OK
    resets_at = ""
    if rate_limits.get("spend_control_reached"):
        kind = KIND_OUT_OF_TOKENS
        reached = reached or "credits"
    elif reached or exhausted:
        kind = KIND_RATE_LIMITED
        named = next((w for w in windows if w["name"] == reached), None)
        chosen = named or (exhausted[0] if exhausted else None)
        if chosen is not None:
            reached = reached or chosen["name"]
            resets_at = chosen["resets_at"]
    if kind == KIND_RATE_LIMITED and not resets_at:
        resets_at = min(
            (w["resets_at"] for w in windows if w["resets_at"]), default=""
        )
    return {
        "kind": kind,
        "source": SOURCE_CODEX_ROLLOUT,
        "windows": windows,
        "reached": reached,
        "resets_at": resets_at,
        "observed_at": _utc(observed_at),
        "plan_type": str(rate_limits.get("plan_type") or ""),
    }


def codex_limit_state(
    session_id: str,
    *,
    sessions_root: Path | str | None = None,
) -> dict[str, Any] | None:
    """The limit state of one Codex session on this host, or None without a rollout."""

    path = codex_rollout_path(session_id, sessions_root=sessions_root)
    if path is None:
        return None
    found = read_codex_rate_limits(path)
    if found is None:
        return unknown_state(SOURCE_CODEX_ROLLOUT)
    timestamp, limits = found
    return limit_state_from_codex(limits, observed_at=timestamp)


# --------------------------------------------------------------------------- Claude Code


def limit_state_from_claude_statusline(
    payload: Mapping[str, Any] | None,
    *,
    observed_at: str = "",
) -> dict[str, Any]:
    """The status line JSON Claude Code hands its command, as the limit state.

    ``rate_limits`` is absent before the first API response and each window is
    dropped once its reset passes, so an absent object means "nothing to
    report", not "unlimited".
    """

    limits = payload.get("rate_limits") if isinstance(payload, Mapping) else None
    if not isinstance(limits, Mapping):
        return unknown_state(SOURCE_CLAUDE_STATUSLINE, observed_at=observed_at)
    windows: list[dict[str, Any]] = []
    for name in ("five_hour", "seven_day", "spend_limit"):
        window = limits.get(name)
        if not isinstance(window, Mapping):
            continue
        windows.append(
            {
                "name": name,
                "used_percent": _percent(window.get("used_percentage")),
                "window_minutes": {"five_hour": 300, "seven_day": 10080}.get(name),
                "resets_at": _utc(window.get("resets_at")),
            }
        )
    if not windows:
        # An empty object says nothing about any window: not reported, not fine.
        return unknown_state(SOURCE_CLAUDE_STATUSLINE, observed_at=observed_at)
    exhausted = [
        window for window in windows
        if window["used_percent"] is not None and window["used_percent"] >= 100
    ]
    kind = KIND_RATE_LIMITED if exhausted else KIND_OK
    reached = exhausted[0]["name"] if exhausted else ""
    return {
        "kind": kind,
        "source": SOURCE_CLAUDE_STATUSLINE,
        "windows": windows,
        "reached": reached,
        "resets_at": exhausted[0]["resets_at"] if exhausted else "",
        "observed_at": _utc(observed_at),
    }


def limit_state_from_claude_stop_failure(
    payload: Mapping[str, Any] | None,
    *,
    observed_at: str = "",
) -> dict[str, Any]:
    """A ``StopFailure`` hook payload: the turn ended on this error, now.

    ``rate_limit`` is rate limited, ``billing_error`` and ``account_on_hold``
    are out of tokens (out of credits: operator, 2026-09-23 23:05Z, "we had to
    know about this but we weren't"), and any other error is ``stopped`` under
    its own name. Only a payload with no error name reads ``unknown``. The hook
    carries no reset time, so ``resets_at`` stays empty until the status line
    reports a window.
    """

    error = ""
    if isinstance(payload, Mapping):
        error = str(payload.get("error") or payload.get("matcher") or "").strip().lower()
    if not _STOP_FAILURE_NAME.fullmatch(error):
        return unknown_state(SOURCE_CLAUDE_STOP_FAILURE, observed_at=observed_at)
    if error in STOP_FAILURE_RATE_LIMITED:
        kind = KIND_RATE_LIMITED
    elif error in STOP_FAILURE_OUT_OF_TOKENS:
        kind = KIND_OUT_OF_TOKENS
    else:
        kind = KIND_STOPPED
    return {
        "kind": kind,
        "source": SOURCE_CLAUDE_STOP_FAILURE,
        "windows": [],
        "reached": error,
        "resets_at": "",
        "observed_at": _utc(observed_at),
    }


# --------------------------------------------------------------------------- clearing


def limit_state_at(state: Mapping[str, Any] | None, *, now: str) -> dict[str, Any] | None:
    """The state as it stands at ``now``: a limit whose reset has passed reads ok.

    Kept separate from the readers so a stale rollout (an agent that stopped
    writing) still clears on the board when its window turns over.
    """

    if not isinstance(state, Mapping):
        return None
    current = dict(state)
    resets_at = str(current.get("resets_at") or "")
    if current.get("kind") in (KIND_RATE_LIMITED, KIND_OUT_OF_TOKENS) and resets_at:
        if _utc(now) and resets_at <= _utc(now):
            current["kind"] = KIND_OK
            current["cleared_at"] = resets_at
    return current


def wake_deferred_until(state: Mapping[str, Any] | None, *, now: str) -> str:
    """Until when a session wake waits, or empty when it does not wait.

    A wake to an agent that is out of tokens or rate limited only piles up
    turns it cannot take. It waits for the runtime's own reset time, and only
    when the runtime named one: a limit without a reset time is not a reason to
    withhold mail, because nobody could say when to stop withholding it.
    """

    current = limit_state_at(state, now=now)
    if not current or current.get("kind") not in (KIND_RATE_LIMITED, KIND_OUT_OF_TOKENS):
        return ""
    resets_at = str(current.get("resets_at") or "")
    return resets_at if resets_at and resets_at > _utc(now) else ""


# Claude Code names its windows; Codex reports minutes. Both read the same way.
_WINDOW_NAME_LABELS = {
    "five_hour": "5 h",
    "seven_day": "week",
    "seven_day_opus": "week (Opus)",
    "seven_day_sonnet": "week (Sonnet)",
}


def window_length_label(window: Mapping[str, Any]) -> str:
    """A window by its length, the way the operator reads it: `5 h`, `week`.

    The operator could not tell a rolling window from a weekly one while the
    board showed only a name or one number (W26, 2026-09-24).
    """

    try:
        minutes = int(window.get("window_minutes") or 0)
    except (TypeError, ValueError):
        minutes = 0
    if minutes > 0:
        if minutes % 10080 == 0:
            return "week" if minutes == 10080 else f"{minutes // 10080} weeks"
        if minutes >= 1440 and minutes % 1440 == 0:
            return f"{minutes // 1440} d"
        if minutes >= 60:
            return f"{round(minutes / 60)} h"
        return f"{minutes} min"
    name = str(window.get("name") or "")
    return _WINDOW_NAME_LABELS.get(name, name or "window")


def limit_windows_line(state: Mapping[str, Any] | None) -> str:
    """Every reported window with its length and use, nearest its limit first.

    `week 65% · 5 h 23%` for Claude Code, `week 20%` for Codex, which reports
    its weekly window as `primary`.
    """

    if not isinstance(state, Mapping):
        return ""
    windows = [
        window
        for window in state.get("windows") or []
        if isinstance(window, Mapping) and window.get("used_percent") is not None
    ]
    windows.sort(key=lambda window: -float(window["used_percent"]))
    return " · ".join(
        f"{window_length_label(window)} {round(float(window['used_percent']))}%"
        for window in windows
    )


def _reached_label(state: Mapping[str, Any]) -> str:
    reached = str(state.get("reached") or "")
    for window in state.get("windows") or []:
        if isinstance(window, Mapping) and str(window.get("name") or "") == reached:
            return window_length_label(window)
    return _WINDOW_NAME_LABELS.get(reached, reached)


def limit_state_line(state: Mapping[str, Any] | None) -> str:
    """One short line for a status bar: what the limit is and when it resets."""

    if not isinstance(state, Mapping) or not state:
        return "limit unknown"
    kind = str(state.get("kind") or KIND_UNKNOWN)
    resets = str(state.get("resets_at") or "")
    when = f", resets {resets[11:16]}Z" if len(resets) >= 16 else ""
    reached = str(state.get("reached") or "")
    if kind == KIND_OUT_OF_TOKENS:
        detail = " (" + reached + ")" if reached in STOP_FAILURE_OUT_OF_TOKENS else ""
        return "out of tokens" + detail + when
    if kind == KIND_STOPPED:
        return "stopped (" + (reached or "error") + ")"
    if kind == KIND_RATE_LIMITED:
        window = _reached_label(state)
        detail = " (" + window + ")" if window else ""
        return "rate limited" + detail + when
    if kind == KIND_OK:
        windows = limit_windows_line(state)
        return "usage ok (" + windows + ")" if windows else "usage ok"
    return "limit unknown"


def session_with_limit_state(
    session: Mapping[str, Any],
    *,
    runtime_kind: str,
    runtime_session_id: str,
    now: str,
    sessions_root: Path | str | None = None,
    recorded: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The listener session row with the runtime's own limit state on it.

    Codex is read from its rollout on this host. Claude Code has no file of its
    own, so its state is what ``pb worker limit-state`` recorded from the status
    line or the ``StopFailure`` hook (``recorded``). A runtime that reports
    nothing leaves the row without the field, which the board reads as "not
    reported", never as "fine".
    """

    row = dict(session)
    state: Mapping[str, Any] | None = None
    kind = str(runtime_kind or "").strip().lower()
    if kind == "codex":
        try:
            state = codex_limit_state(runtime_session_id, sessions_root=sessions_root)
        except Exception:  # noqa: BLE001 - a rollout that cannot be read is no state, not a failed cycle
            state = None
    elif isinstance(recorded, Mapping) and recorded:
        state = recorded
    current = limit_state_at(state, now=now)
    if current is not None:
        row["limit_state"] = current
    else:
        row.pop("limit_state", None)
    return row


__all__ = [
    "CODEX_SESSIONS_ROOT",
    "KIND_OK",
    "KIND_OUT_OF_TOKENS",
    "KIND_RATE_LIMITED",
    "KIND_UNKNOWN",
    "LIMIT_STATE_KINDS",
    "SOURCE_CLAUDE_STATUSLINE",
    "SOURCE_CLAUDE_STOP_FAILURE",
    "SOURCE_CODEX_ROLLOUT",
    "codex_limit_state",
    "codex_rollout_path",
    "limit_state_at",
    "limit_state_from_claude_statusline",
    "limit_state_from_claude_stop_failure",
    "limit_state_from_codex",
    "limit_state_line",
    "read_codex_rate_limits",
    "session_with_limit_state",
    "unknown_state",
    "wake_deferred_until",
]

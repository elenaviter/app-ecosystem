# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""The model and reasoning effort an agent runs with, read from the runtime (W327).

The same collection path as the usage-limit state (W26), and the same rule:
the value is what the runtime writes about itself, never inferred.

- **Codex** appends a ``turn_context`` record to the session's rollout file
  at every turn; its ``payload`` names the ``model`` and the reasoning
  ``effort`` the turn runs with, and repeats both under
  ``collaboration_mode.settings`` (``model``, ``reasoning_effort``).
  Verified on a codex-cli 0.154.0 rollout on dev-main, 2026-09-25.
- **Claude Code** hands its status line command a JSON with ``model``
  (``id``, ``display_name``), ``effort.level`` only for a model that supports
  effort, and ``thinking.enabled`` (2.1.282, read from the shipped binary on
  2026-09-25). ``pb worker limit-state`` receives it and records the model here.

A value the runtime did not give stays empty, and the board reads that as "not
reported". The record rides the listener session next to ``limit_state``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .limit_state import (
    CODEX_TAIL_BYTES,
    SOURCE_CLAUDE_STATUSLINE,
    SOURCE_CODEX_ROLLOUT,
    _tail_lines,
    _utc,
    codex_rollout_path,
)

MODEL_TEXT_LIMIT = 128
EFFORT_TEXT_LIMIT = 32

# The fields that say what the agent runs with. ``observed_at`` and
# ``recorded_at`` are when, not what, so a change is judged on these only.
RUNTIME_MODEL_FIELDS = ("model", "model_display", "effort", "thinking", "source")


def _text(value: Any, maximum: int) -> str:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    return str(value).strip()[:maximum]


def _runtime_model(
    *,
    model: Any,
    model_display: Any = "",
    effort: Any = "",
    thinking: Any = None,
    source: str,
    observed_at: str = "",
) -> dict[str, Any] | None:
    record = {
        "model": _text(model, MODEL_TEXT_LIMIT),
        "model_display": _text(model_display, MODEL_TEXT_LIMIT),
        "effort": _text(effort, EFFORT_TEXT_LIMIT).lower(),
        "thinking": thinking if isinstance(thinking, bool) else None,
        "source": source,
        "observed_at": _utc(observed_at),
    }
    if not (record["model"] or record["model_display"] or record["effort"]):
        return None
    return record


# --------------------------------------------------------------------------- Codex


def read_codex_turn_context(
    path: Path | str,
    *,
    tail_bytes: int = CODEX_TAIL_BYTES,
) -> tuple[str, dict[str, Any]] | None:
    """The newest ``turn_context`` payload in a rollout, with its timestamp."""

    file = Path(path)
    try:
        for line in _tail_lines(file, tail_bytes=tail_bytes):
            if '"turn_context"' not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, Mapping) or record.get("type") != "turn_context":
                continue
            payload = record.get("payload")
            if isinstance(payload, Mapping):
                return str(record.get("timestamp") or ""), dict(payload)
    except OSError:
        return None
    return None


def runtime_model_from_codex(
    turn_context: Mapping[str, Any] | None,
    *,
    observed_at: str = "",
) -> dict[str, Any] | None:
    """A Codex ``turn_context`` payload as the model record.

    ``payload.effort`` and ``payload.model`` first; the collaboration mode's
    ``settings.reasoning_effort`` and ``settings.model`` (and a top-level
    ``reasoning_effort``) are read when those are missing, so a rename in
    either direction still reports.
    """

    if not isinstance(turn_context, Mapping):
        return None
    mode = turn_context.get("collaboration_mode")
    settings = mode.get("settings") if isinstance(mode, Mapping) else None
    settings = settings if isinstance(settings, Mapping) else {}
    effort = turn_context.get("effort")
    for fallback in (turn_context.get("reasoning_effort"), settings.get("reasoning_effort")):
        if effort not in (None, ""):
            break
        effort = fallback
    model = turn_context.get("model") or settings.get("model")
    return _runtime_model(
        model=model,
        effort=effort,
        source=SOURCE_CODEX_ROLLOUT,
        observed_at=observed_at,
    )


def codex_runtime_model(
    session_id: str,
    *,
    sessions_root: Path | str | None = None,
) -> dict[str, Any] | None:
    """The model record of one Codex session on this host, or None."""

    path = codex_rollout_path(session_id, sessions_root=sessions_root)
    if path is None:
        return None
    found = read_codex_turn_context(path)
    if found is None:
        return None
    timestamp, payload = found
    return runtime_model_from_codex(payload, observed_at=timestamp)


# --------------------------------------------------------------------------- Claude Code


def runtime_model_from_claude_statusline(
    payload: Mapping[str, Any] | None,
    *,
    observed_at: str = "",
) -> dict[str, Any] | None:
    """The status line JSON Claude Code hands its command, as the model record.

    ``effort`` is present only for a model that supports it; without it the
    effort stays empty ("not reported"), never a guessed default.
    """

    if not isinstance(payload, Mapping):
        return None
    model = payload.get("model")
    model_id: Any = ""
    display: Any = ""
    if isinstance(model, Mapping):
        model_id, display = model.get("id"), model.get("display_name")
    elif isinstance(model, str):
        model_id = model
    effort = payload.get("effort")
    level = effort.get("level") if isinstance(effort, Mapping) else ""
    thinking = payload.get("thinking")
    enabled = thinking.get("enabled") if isinstance(thinking, Mapping) else None
    return _runtime_model(
        model=model_id,
        model_display=display,
        effort=level,
        thinking=enabled,
        source=SOURCE_CLAUDE_STATUSLINE,
        observed_at=observed_at,
    )


# --------------------------------------------------------------------------- session


def runtime_model_changed(
    current: Mapping[str, Any] | None, incoming: Mapping[str, Any] | None
) -> bool:
    current = current if isinstance(current, Mapping) else {}
    incoming = incoming if isinstance(incoming, Mapping) else {}
    return any(current.get(key) != incoming.get(key) for key in RUNTIME_MODEL_FIELDS)


def runtime_model_line(record: Mapping[str, Any] | None) -> str:
    """``claude-opus-5-5 · effort high``, with "not reported" for a missing part."""

    if not isinstance(record, Mapping) or not record:
        return "model not reported"
    model = str(record.get("model_display") or record.get("model") or "not reported")
    effort = str(record.get("effort") or "not reported")
    return f"{model} · effort {effort}"


def session_with_runtime_model(
    session: Mapping[str, Any],
    *,
    runtime_kind: str,
    runtime_session_id: str,
    sessions_root: Path | str | None = None,
    recorded: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The listener session row with the runtime's own model record on it.

    Codex is read from its rollout on this host; Claude Code's record is what
    ``pb worker limit-state`` recorded from the status line. Nothing reported
    leaves the row without the field.
    """

    row = dict(session)
    record: Mapping[str, Any] | None = None
    kind = str(runtime_kind or "").strip().lower()
    if kind == "codex":
        try:
            record = codex_runtime_model(runtime_session_id, sessions_root=sessions_root)
        except Exception:  # noqa: BLE001 - an unreadable rollout is no record, not a failed cycle
            record = None
    elif isinstance(recorded, Mapping) and recorded:
        record = {key: value for key, value in recorded.items() if key != "recorded_at"}
    if record:
        row["runtime_model"] = dict(record)
    else:
        row.pop("runtime_model", None)
    return row


__all__ = [
    "RUNTIME_MODEL_FIELDS",
    "codex_runtime_model",
    "read_codex_turn_context",
    "runtime_model_changed",
    "runtime_model_from_claude_statusline",
    "runtime_model_from_codex",
    "runtime_model_line",
    "session_with_runtime_model",
]

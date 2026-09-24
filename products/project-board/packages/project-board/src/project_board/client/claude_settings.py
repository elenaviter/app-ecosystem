"""The Claude Code settings a worker host needs, merged into the user's own settings (W304 finding 45).

A Claude Code worker reports its usage limit and keeps its inbox watch
running through three entries in ``~/.claude/settings.json``: the
``statusLine`` command and the ``StopFailure`` hook hand the runtime's limit
state to ``pb worker limit-state``, and the ``Stop`` hook runs
``pb worker stop-guard``. On 2026-09-24 spark1 had none of them, because the
procedure only described them, and its agents' cards said "limit not
reported". ``pb procedure install --target claude-code`` now merges them.

The merge keeps every other key and every existing hook, adds only what is
missing, writes nothing when nothing is missing, and keeps a copy of the
previous file before a write. A status line the user already runs for
something else is theirs: it is left as it is and reported, with what to do.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..contract.errors import DomainError
from .io import atomic_write_json
from .limit_state import STOP_FAILURE_ERRORS

SETTINGS_RELATIVE_PATH = Path(".claude") / "settings.json"


def pb_command() -> str:
    """This host's own ``pb``, by absolute path when the shell can find it."""

    return shutil.which("pb") or "pb"


def _command_hook(command: str) -> dict[str, Any]:
    return {"type": "command", "command": command}


def _has_command(groups: Any, command: str) -> bool:
    for group in groups if isinstance(groups, list) else []:
        for hook in (group.get("hooks") if isinstance(group, Mapping) else None) or []:
            if isinstance(hook, Mapping) and str(hook.get("command") or "") == command:
                return True
    return False


def merge_claude_code_settings(home: str | Path, *, pb: str | None = None) -> dict[str, Any]:
    """Add the worker's status line and hooks to ``<home>/.claude/settings.json``, keeping the rest."""

    pb_path = pb or pb_command()
    path = Path(home) / SETTINGS_RELATIVE_PATH
    existed = path.exists()
    try:
        settings = json.loads(path.read_text(encoding="utf-8")) if existed else {}
    except json.JSONDecodeError as exc:
        raise DomainError(
            "work_claude_settings_invalid",
            f"{path} is not valid JSON, so it was left unchanged: {exc}.",
            status=409,
            details={"path": str(path)},
        ) from exc
    if not isinstance(settings, dict):
        raise DomainError(
            "work_claude_settings_invalid",
            f"{path} does not hold a JSON object, so it was left unchanged.",
            status=409,
            details={"path": str(path)},
        )

    added: list[str] = []
    kept: list[str] = []
    notes: list[str] = []

    status_command = f"{pb_path} worker limit-state"
    current = settings.get("statusLine")
    current_command = str(current.get("command") or "") if isinstance(current, Mapping) else ""
    if current is None:
        settings["statusLine"] = _command_hook(status_command)
        added.append("statusLine")
    elif current_command.endswith("worker limit-state"):
        kept.append("statusLine")
    else:
        kept.append("statusLine")
        notes.append(
            f"statusLine already runs {current_command or 'another command'}; it was left as it is. "
            f"Pipe the same JSON into `{status_command}` from it, or usage is not reported."
        )

    hooks = settings.get("hooks")
    if hooks is None:
        hooks = settings["hooks"] = {}
    if not isinstance(hooks, dict):
        raise DomainError(
            "work_claude_settings_invalid",
            f"{path} has a hooks value that is not an object, so it was left unchanged.",
            status=409,
            details={"path": str(path)},
        )
    wanted = {
        "StopFailure": {
            "matcher": "|".join(STOP_FAILURE_ERRORS),
            "hooks": [_command_hook(f"{pb_path} worker limit-state --source stop-failure")],
        },
        "Stop": {"hooks": [_command_hook(f"{pb_path} worker stop-guard")]},
    }
    for event, group in wanted.items():
        command = group["hooks"][0]["command"]
        groups = hooks.get(event)
        if _has_command(groups, command) or _has_command(
            groups, command.replace(f"{pb_path} ", "pb ", 1)
        ):
            kept.append(f"hooks.{event}")
            continue
        hooks[event] = [*(groups if isinstance(groups, list) else []), group]
        added.append(f"hooks.{event}")

    backup = ""
    if added:
        path.parent.mkdir(parents=True, exist_ok=True)
        if existed:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup_path = path.with_name(f"{path.name}.bak-{stamp}")
            shutil.copy2(path, backup_path)
            backup = str(backup_path)
        atomic_write_json(path, settings)
    return {
        "path": str(path),
        "changed": bool(added),
        "added": added,
        "kept": kept,
        "backup": backup,
        "notes": notes,
        "undo": (
            f"cp {backup} {path}"
            if backup
            else (f"remove {', '.join(added)} from {path}" if added else "")
        ),
    }


__all__ = ["SETTINGS_RELATIVE_PATH", "merge_claude_code_settings", "pb_command"]

"""The Claude Code settings a worker host needs, merged into the user's own settings (W304 finding 45).

A Claude Code worker reports its usage limit and keeps its inbox watch
running through three entries in ``~/.claude/settings.json``: the
``statusLine`` command and the ``StopFailure`` hook hand the runtime's limit
state to ``pb worker limit-state``, and the ``Stop`` hook runs
``pb worker stop-guard``. On 2026-09-24 spark1 had none of them, because the
procedure only described them, and its agents' cards said "limit not
reported". ``pb procedure install --target claude-code`` now merges them.

The merge keeps every other key and every other hook, and writes nothing
when the file already holds the current entries. An older Problem Board entry
is brought up to date in place: a bare ``pb`` becomes this host's full path,
and a ``StopFailure`` matcher written when it covered only ``rate_limit``
gains every other stopping error. A status line the user runs for something
else is theirs: it is left as it is and reported, with what to do. A
symlinked settings file stays a symlink, and its target is what changes.
"""

from __future__ import annotations

import copy
import json
import os
import shlex
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from ..contract.errors import DomainError
from .io import atomic_write_json
from .limit_state import STOP_FAILURE_ERRORS

SETTINGS_RELATIVE_PATH = Path(".claude") / "settings.json"
STATUS_ARGS = ("worker", "limit-state")
STOP_FAILURE_ARGS = ("worker", "limit-state", "--source", "stop-failure")
STOP_ARGS = ("worker", "stop-guard")
LAUNCHER_READ_LIMIT = 4096
# The stable ``pb`` that re-executed a selected release (W304, fable-pub's
# trace): in the release process argv[0] is the release's code_entrypoint.py,
# which names no executable, so the entry point hands its own path down here.
INVOKED_PB_ENV = "PROBLEM_BOARD_INVOKED_PB"


def _executable(candidate: str | None) -> str | None:
    if not candidate:
        return None
    if os.sep not in candidate:
        candidate = shutil.which(candidate)
        if not candidate:
            return None
    path = os.path.abspath(candidate)
    if path.endswith(".py") or not os.path.isfile(path) or not os.access(path, os.X_OK):
        return None
    return path


def _launches(launcher: str, running: str) -> bool:
    """Whether ``launcher`` is the running program or a launcher script that execs it."""

    if os.path.realpath(launcher) == os.path.realpath(running):
        return True
    try:
        with open(launcher, "rb") as handle:
            head = handle.read(LAUNCHER_READ_LIMIT)
    except OSError:
        return False
    return running.encode() in head or os.path.realpath(running).encode() in head


def pb_command(argv0: str | None = None, *, which: Callable[[str], str | None] = shutil.which) -> str:
    """The full path of the ``pb`` running this install, as the hooks will call it.

    The ``pb`` on ``PATH`` is used when it runs this same program (the host's
    launcher in ``~/.local/bin`` execs the client's own ``pb``). Otherwise the
    running program's own path is used, so a hook never names another ``pb``.
    """

    running = _executable(sys.argv[0] if argv0 is None else argv0)
    if running is None and argv0 is None:
        # A selected release runs as a .py script; the pb that launched it
        # recorded its own path before the exec.
        running = _executable(os.environ.get(INVOKED_PB_ENV))
    on_path = _executable(which("pb") or "")
    if running and on_path and _launches(on_path, running):
        return on_path
    if running:
        return running
    raise DomainError(
        "work_claude_settings_pb_unresolved",
        "The Claude Code hooks need this host's pb by its full path, and this install was not run "
        "through a pb executable. Run `pb procedure install --target claude-code`.",
        status=409,
        details={"argv0": sys.argv[0] if argv0 is None else argv0},
    )


def _command(pb: str, args: tuple[str, ...]) -> str:
    return " ".join([shlex.quote(pb), *args])


def _is_pb_command(command: Any, args: tuple[str, ...]) -> bool:
    """A command this merge owns: any ``pb`` (bare or by path) with exactly these arguments."""

    try:
        words = shlex.split(str(command or ""))
    except ValueError:
        return False
    return bool(words) and os.path.basename(words[0]) == "pb" and tuple(words[1:]) == args


def _matcher(previous: Any) -> str:
    """Every stopping error, plus any the user added to the old Problem Board matcher."""

    if previous in ("", "*"):
        return previous
    extra = [word for word in str(previous or "").split("|") if word and word not in STOP_FAILURE_ERRORS]
    return "|".join([*STOP_FAILURE_ERRORS, *extra])


def _merge_event(groups: Any, *, command: str, args: tuple[str, ...], matcher: bool) -> tuple[list[Any], bool]:
    """The event's groups with exactly one current Problem Board group; whether one existed before.

    Other hooks stay where they are. A group that held only the Problem Board
    hook is replaced in place, so a second run leaves the order unchanged.
    """

    result: list[Any] = []
    placed = False
    owned = False
    for group in groups if isinstance(groups, list) else []:
        hooks = group.get("hooks") if isinstance(group, Mapping) else None
        if not isinstance(hooks, list):
            result.append(group)
            continue
        ours = [hook for hook in hooks if isinstance(hook, Mapping) and _is_pb_command(hook.get("command"), args)]
        if not ours:
            result.append(group)
            continue
        owned = True
        others = [hook for hook in hooks if hook not in ours]
        if others:
            result.append({**group, "hooks": others})
            continue
        if placed:
            continue
        current: dict[str, Any] = {key: value for key, value in group.items() if key != "hooks"}
        if matcher:
            current["matcher"] = _matcher(group.get("matcher"))
        current["hooks"] = [{**ours[0], "type": "command", "command": command}]
        result.append(current)
        placed = True
    if not placed:
        group: dict[str, Any] = {"matcher": "|".join(STOP_FAILURE_ERRORS)} if matcher else {}
        group["hooks"] = [{"type": "command", "command": command}]
        result.append(group)
    return result, owned


def _private_backup(path: Path, content: bytes) -> Path:
    """A new ``settings.json.bak-<UTC time>`` readable by the user only; an earlier backup is never replaced."""

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for attempt in range(1000):
        backup = path.with_name(f"{path.name}.bak-{stamp}" + (f".{attempt}" if attempt else ""))
        try:
            descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        return backup
    raise DomainError(
        "work_claude_settings_backup_unavailable",
        f"No free backup name beside {path}, so it was left unchanged.",
        status=409,
        details={"path": str(path)},
    )


def _invalid(path: Path, reason: str) -> DomainError:
    return DomainError(
        "work_claude_settings_invalid",
        f"{path} {reason}, so it was left unchanged.",
        status=409,
        details={"path": str(path)},
    )


def merge_claude_code_settings(home: str | Path, *, pb: str | None = None) -> dict[str, Any]:
    """Add or update the worker's status line and hooks in ``<home>/.claude/settings.json``, keeping the rest."""

    pb_path = pb or pb_command()
    path = Path(home) / SETTINGS_RELATIVE_PATH
    target = path
    if path.is_symlink():
        target = Path(os.path.realpath(path))
        if not target.exists():
            raise _invalid(path, f"is a symlink to {target}, which does not exist")
    existed = target.exists()
    try:
        original = json.loads(target.read_text(encoding="utf-8")) if existed else {}
    except json.JSONDecodeError as exc:
        raise _invalid(path, f"is not valid JSON ({exc})") from exc
    if not isinstance(original, dict):
        raise _invalid(path, "does not hold a JSON object")
    if not isinstance(original.get("hooks") or {}, dict):
        raise _invalid(path, "has a hooks value that is not an object")

    settings = copy.deepcopy(original)
    added: list[str] = []
    updated: list[str] = []
    kept: list[str] = []
    notes: list[str] = []

    status_command = _command(pb_path, STATUS_ARGS)
    current = settings.get("statusLine")
    current_command = current.get("command") if isinstance(current, Mapping) else None
    if current is None:
        settings["statusLine"] = {"type": "command", "command": status_command}
        added.append("statusLine")
    elif current_command == status_command:
        kept.append("statusLine")
    elif _is_pb_command(current_command, STATUS_ARGS):
        settings["statusLine"] = {**current, "type": "command", "command": status_command}
        updated.append("statusLine")
    else:
        kept.append("statusLine")
        notes.append(
            f"statusLine already runs {current_command or 'another command'}; it was left as it is. "
            f"Pipe the same JSON into `{status_command}` from it, or usage is not reported."
        )

    if settings.get("hooks") is None:
        settings["hooks"] = {}
    hooks = settings["hooks"]
    for event, args, matcher in (
        ("StopFailure", STOP_FAILURE_ARGS, True),
        ("Stop", STOP_ARGS, False),
    ):
        merged, owned = _merge_event(hooks.get(event), command=_command(pb_path, args), args=args, matcher=matcher)
        name = f"hooks.{event}"
        if merged == hooks.get(event):
            kept.append(name)
            continue
        hooks[event] = merged
        (updated if owned else added).append(name)

    changed = settings != original
    backup = ""
    if changed:
        if existed:
            backup = str(_private_backup(path, target.read_bytes()))
        atomic_write_json(target, settings)
    return {
        "path": str(path),
        "target": str(target) if target != path else "",
        "changed": changed,
        "added": added,
        "updated": updated,
        "kept": kept,
        "backup": backup,
        "notes": notes,
        "undo": (
            f"cp {shlex.quote(backup)} {shlex.quote(str(target))}"
            if backup
            else (f"remove {', '.join(added)} from {path}" if added else "")
        ),
    }


__all__ = ["SETTINGS_RELATIVE_PATH", "merge_claude_code_settings", "pb_command"]

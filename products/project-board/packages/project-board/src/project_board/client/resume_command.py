"""The resume command the agent's card shows (W304 finding 29).

The command is the agent's real start line with the resume switch added:
captured at `pb worker listen` (runtime_launch.py) as the argv and working
directory of the `claude` or `codex` process, and the tmux session it runs in.
Without a capture (an older pb, or the process was not found) it is the
documented start line from the README and the Connect panel, marked
"reconstructed" on its first line. It is never the host's approved-roots list:
until 2026-09-28 every card showed that list, the same for every agent, and
not the way any of them runs.

The command is returned to the person and never executed. The board checks
that it contains `claude --resume <id>` or `codex resume <id>`.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..contract.errors import DomainError

RECONSTRUCTED = "# reconstructed: the start line of this session was not captured"
DOCUMENTED_FLAGS = {
    "claude-code": ["--add-dir", "~/.kdcube", "--dangerously-skip-permissions", "--disallowedTools", "AskUserQuestion"],
    "codex": ["--sandbox", "danger-full-access", "--ask-for-approval", "never", "--search"],
}


def _path(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if any(character in text for character in ("\x00", "\n", "\r")):
        raise DomainError(
            "work_session_resume_path_invalid",
            f"{field} contains unsupported control characters.",
        )
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise DomainError(
            "work_session_resume_path_invalid",
            f"{field} must be an absolute LOCAL path.",
        )
    return str(path.resolve())


def _quote(token: str) -> str:
    # "~/" stays unquoted so the shell expands it, as in the documented line.
    if token == "~" or token.startswith("~/"):
        return "~" + shlex.quote(token[1:]) if token != "~" else "~"
    return shlex.quote(token)


def _line(argv: Sequence[str]) -> str:
    return " ".join(_quote(token) for token in argv)


def _resume_argv(runtime: str, session_id: str, flags: Sequence[str], cwd: str) -> tuple[list[str], str]:
    """(argv, cwd for a leading cd) of the resume line for this runtime."""

    if runtime == "codex":
        argv = ["codex", "resume", session_id, *flags]
        # codex takes its folder with -C; a line without it starts from cwd.
        return argv, "" if ("-C" in flags or "--cd" in flags) else cwd
    return ["claude", "--resume", session_id, *flags], cwd


def build_session_resume_command(
    *,
    runtime_kind: str,
    runtime_session_id: str,
    working_directory: str = "",
    launch: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the visible command from the captured start line, else the documented one."""

    runtime = str(runtime_kind or "").strip().lower()
    session_id = str(runtime_session_id or "").strip()
    if not session_id:
        raise DomainError(
            "work_session_resume_id_required",
            "The worker has no native resumable session id.",
        )
    if any(character in session_id for character in ("\x00", "\n", "\r")):
        raise DomainError(
            "work_session_resume_id_invalid",
            "The native resumable session id contains unsupported characters.",
        )
    if runtime not in DOCUMENTED_FLAGS:
        raise DomainError(
            "work_session_resume_runtime_unsupported",
            "This worker runtime has no Problem Board resume-command adapter.",
            details={"runtime_kind": runtime},
        )

    captured = dict(launch or {})
    captured_argv = [str(item) for item in captured.get("argv") or [] if isinstance(item, str) and item]
    source = "captured" if captured_argv else "reconstructed"
    if captured_argv:
        # argv[0] is the program; the capture already dropped any earlier resume switch.
        flags = captured_argv[2:] if runtime == "codex" and captured_argv[1:2] == ["resume"] else captured_argv[1:]
        # The directory as the process reported it: resolving it would name
        # the same folder another way (macOS: /home -> /System/Volumes/Data/home).
        cwd = str(captured.get("cwd") or "").strip()
        if cwd and (not cwd.startswith("/") or any(c in cwd for c in ("\x00", "\n", "\r"))):
            cwd = ""
    else:
        flags = list(DOCUMENTED_FLAGS[runtime])
        cwd = _path(working_directory, field="worker.working_directory")
        if runtime == "codex" and cwd:
            flags = ["-C", cwd, *flags]
    argv, cd = _resume_argv(runtime, session_id, flags, cwd)
    line = _line(argv)
    if cd:
        line = f"cd {shlex.quote(cd)} && {line}"
    tmux_session = str(captured.get("tmux_session") or "").strip()
    if tmux_session and not any(c in tmux_session for c in ("\x00", "\n", "\r")):
        command = (
            f"tmux new-session -d -s {shlex.quote(tmux_session)} {shlex.quote(line)}\n"
            f"tmux attach -t {shlex.quote(tmux_session)}"
        )
    else:
        command = line
    if source == "reconstructed":
        command = f"{RECONSTRUCTED}\n{command}"
    return {
        "runtime_kind": runtime,
        "runtime_session_id": session_id,
        "source": source,
        "working_directory_recorded": bool(cwd),
        "tmux_session": tmux_session,
        "argv": argv,
        "command": command,
    }


__all__ = ["RECONSTRUCTED", "build_session_resume_command"]

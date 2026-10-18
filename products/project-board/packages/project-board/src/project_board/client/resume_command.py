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
that it resumes exactly this session (`claude --resume <id>` or
`codex resume <id>`).

W763: a Claude session bound to an account profile resumes under that
profile. The line then names the profile's configuration directory and the
absolute Claude executable through `env CLAUDE_CONFIG_DIR=<dir> <exe>`, so a
shell function or alias called `claude` cannot replace either, and the same
inner line works unchanged inside tmux.

W913: a session whose host config is a named KDCube profile (the `pb setup`
target id; two boards on one machine, W909) resumes on that profile's board:
the line starts `env PB_PROFILE=<name> ...`, for Codex too. Operator,
2026-10-18: "if the user is admin we can show him the instruction with the
specification of the profile. otherwise regular single profle instruction".
The board removes the word for a viewer who is not a platform super-admin.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..contract.errors import DomainError

RECONSTRUCTED = "# reconstructed: the start line of this session was not captured"
PB_PROFILE_ENV = "PB_PROFILE="
# The pb setup target id rule (io.component), so the word needs no quoting.
KDCUBE_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
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


def _resume_argv(
    runtime: str,
    session_id: str,
    flags: Sequence[str],
    cwd: str,
    profile: Mapping[str, str] | None = None,
    kdcube_profile: str = "",
) -> tuple[list[str], str]:
    """(argv, cwd for a leading cd) of the resume line for this runtime."""

    # W913: a session of a named KDCube profile resumes on that profile's board.
    # The board shows this word only to a platform super-admin.
    board = [f"{PB_PROFILE_ENV}{kdcube_profile}"] if kdcube_profile else []
    if runtime == "codex":
        argv = [*(["env", *board] if board else []), "codex", "resume", session_id, *flags]
        # codex takes its folder with -C; a line without it starts from cwd.
        return argv, "" if ("-C" in flags or "--cd" in flags) else cwd
    # W763: every Claude line names its account folder through env, so neither
    # a shell function or alias called claude nor a CLAUDE_CONFIG_DIR the
    # terminal already has can move the session to another account.
    profile = profile or {}
    if not profile.get("config_dir"):
        # Operator, 2026-10-12: "we re not going to work wit unscoped claude agents."
        raise DomainError(
            "work_session_resume_profile_required",
            "This Claude agent has no account profile yet. Move it first: "
            "pb worker claude-profile bind, then migrate; its resume line follows.",
            status=409,
        )
    executable = profile.get("executable") or "claude"
    return ["env", *board, f"CLAUDE_CONFIG_DIR={profile['config_dir']}", executable, "--resume", session_id, *flags], cwd


def _profile(value: Mapping[str, Any] | None) -> dict[str, str]:
    """The validated folder and program a Claude resume line names (W763).

    An empty folder is the default one; an empty program is `claude` found
    by `env` on PATH, which no shell function or alias can replace.
    """

    value = value or {}
    # As recorded, not resolved: resolving names the same folder another way
    # (macOS: /home -> /System/Volumes/Data/home).
    config_dir = str(value.get("config_dir") or "").strip()
    if config_dir and (not config_dir.startswith("/") or any(c in config_dir for c in ("\x00", "\n", "\r"))):
        raise DomainError(
            "work_session_resume_path_invalid",
            "profile.config_dir must be an absolute LOCAL path.",
        )
    executable = str(value.get("executable") or "").strip()
    if executable and (
        not executable.startswith("/")
        or any(c in executable for c in ("\x00", "\n", "\r"))
        or Path(executable).name != "claude"
    ):
        raise DomainError(
            "work_session_resume_executable_invalid",
            "The Claude executable must be an absolute path to a program named claude.",
        )
    return {"config_dir": config_dir, "executable": executable}


def build_session_resume_command(
    *,
    runtime_kind: str,
    runtime_session_id: str,
    working_directory: str = "",
    launch: Mapping[str, Any] | None = None,
    profile: Mapping[str, Any] | None = None,
    kdcube_profile: str = "",
) -> dict[str, Any]:
    """Build the visible command from the captured start line, else the documented one.

    ``profile`` ({config_dir, executable}) is the Claude account profile the
    session is bound to (W763); without it the line is the bare `claude` one.
    ``kdcube_profile`` is the KDCube profile of the session's host config
    (W913); the line then starts with ``env PB_PROFILE=<name>``.
    """

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
            "This worker runtime has no Project Board resume-command adapter.",
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
    bound = _profile(profile) if runtime == "claude-code" else {}
    board = str(kdcube_profile or "").strip()
    if board and not KDCUBE_PROFILE_RE.fullmatch(board):
        raise DomainError(
            "work_session_resume_kdcube_profile_invalid",
            "The KDCube profile name must contain only letters, digits, dot, underscore, or hyphen.",
        )
    argv, cd = _resume_argv(runtime, session_id, flags, cwd, bound, board)
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
        "profile_dir": bound.get("config_dir", ""),
        "kdcube_profile": board,
    }


__all__ = ["RECONSTRUCTED", "build_session_resume_command"]

"""How this agent's runtime was actually started, for the card's resume command (W304 finding 29).

Operator, 2026-09-28: the card's resume command showed `claude --resume <id>`
with every approved host root as --add-dir, the same list for every agent,
while the agents run `claude --add-dir ~/.kdcube --dangerously-skip-permissions
--disallowedTools AskUserQuestion` from their workspace. `pb worker listen`
now records the real start line: the argv and working directory of the
`claude` or `codex` process that owns this session, found by walking up from
pb's parent process, and the tmux session it runs in. It stays in the host
config; the relay builds the resume command from it.

Nothing secret is kept: a value after a token, secret, key or password flag, a
`name=value` of that kind, and any long token-like argument are dropped. An
earlier resume switch is dropped too, so a resumed session's line resumes once.
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .agent_session import RUNTIME_SESSION_ENV

MAX_ARGS = 64
MAX_ARG_BYTES = 512
MAX_CWD_BYTES = 1024
MAX_WALK = 16
RUNTIME_NAMES = {"claude-code": "claude", "codex": "codex"}
_SECRET_NAME = re.compile(r"(token|secret|key|password|passwd|credential)", re.IGNORECASE)
# A bare credential: 32+ base64/hex-like characters with letters and digits,
# no dot (paths, hosts and config keys carry one), optional "=" padding.
_TOKEN_LIKE = re.compile(r"^(?=.*[A-Za-z])(?=.*[0-9])[A-Za-z0-9+/_-]{32,}={0,2}$")

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str] | None"]


def _run(argv: Sequence[str]) -> "subprocess.CompletedProcess[str] | None":
    try:
        return subprocess.run(
            list(argv), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=3, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


class Probes:
    """What capture reads from the machine; each part is replaceable in tests."""

    def __init__(self, *, run: Runner = _run, proc: Path = Path("/proc"), env: Mapping[str, str] | None = None) -> None:
        self.run = run
        self.proc = proc
        self.env = dict(os.environ if env is None else env)

    def parent(self, pid: int) -> int:
        stat = self.proc / str(pid) / "stat"
        if stat.is_file():
            try:
                # "pid (comm) state ppid ...": comm may hold spaces, so split after ")".
                return int(stat.read_text().rsplit(")", 1)[1].split()[1])
            except (OSError, ValueError, IndexError):
                return 0
        result = self.run(["ps", "-o", "ppid=", "-p", str(pid)])
        try:
            return int((result.stdout if result else "").strip() or 0)
        except ValueError:
            return 0

    def argv(self, pid: int) -> list[str]:
        cmdline = self.proc / str(pid) / "cmdline"
        if cmdline.is_file():
            try:
                return [part for part in cmdline.read_bytes().decode("utf-8", "replace").split("\x00") if part]
            except OSError:
                return []
        result = self.run(["ps", "-ww", "-o", "args=", "-p", str(pid)])
        # macOS ps joins the arguments with spaces: an argument holding a space
        # is split. Start lines rarely have one; the working directory is read apart.
        return (result.stdout if result else "").split()

    def cwd(self, pid: int) -> str:
        link = self.proc / str(pid) / "cwd"
        if link.exists():
            try:
                return os.readlink(link)
            except OSError:
                return ""
        result = self.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"])
        for line in (result.stdout if result else "").splitlines():
            if line.startswith("n/"):
                return line[1:]
        return ""

    def tmux_session(self) -> str:
        if not self.env.get("TMUX"):
            return ""
        result = self.run(["tmux", "display-message", "-p", "#S"])
        return (result.stdout if result and result.returncode == 0 else "").strip()[:128]

    def process(self, pid: int) -> tuple[str, str]:
        """Local start identity and state. A failed inspection is unknown."""

        try:
            if sys.platform.startswith("linux"):
                values = (self.proc / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
                boot_id = (self.proc / "sys/kernel/random/boot_id").read_text().strip()
                token = f"{boot_id}:{int(values[19])}"
                state = values[0]
                if boot_id:
                    return token, (
                        "absent" if state in {"Z", "X", "x"}
                        else "running" if state in {"R", "S", "D", "I"}
                        else "unknown"
                    )
            elif sys.platform == "darwin":
                return _darwin_process(pid)
        except (ValueError, IndexError):
            pass
        except OSError:
            return _missing_process(pid)
        return "", "unknown"


def _missing_process(pid: int) -> tuple[str, str]:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "", "absent"
    except OSError:
        pass
    return "", "unknown"


class _DarwinProcessInfo(ctypes.Structure):
    # proc_bsdinfo in the macOS SDK's sys/proc_info.h. Start time has
    # microsecond resolution, unlike the formatted time from ps.
    _fields_ = [
        ("flags", ctypes.c_uint32),
        ("status", ctypes.c_uint32),
        ("pid_fields", ctypes.c_uint32 * 10),
        ("names", ctypes.c_char * 48),
        ("terminal_fields", ctypes.c_uint32 * 6),
        ("start_seconds", ctypes.c_uint64),
        ("start_microseconds", ctypes.c_uint64),
    ]


def _darwin_process(pid: int) -> tuple[str, str]:
    info = _DarwinProcessInfo()
    proc_pidinfo = ctypes.CDLL("/usr/lib/libproc.dylib").proc_pidinfo
    proc_pidinfo.argtypes = (
        ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int,
    )
    size = ctypes.sizeof(info)
    if proc_pidinfo(pid, 3, 0, ctypes.byref(info), size) != size or not info.start_seconds:
        return _missing_process(pid)
    token = f"{info.start_seconds}:{info.start_microseconds}"
    if info.status == 5 or info.flags & 4:  # SZOMB or PROC_FLAG_INEXIT
        return token, "absent"
    if info.status in {1, 4}:  # SIDL or SSTOP
        return token, "unknown"
    return token, "running" if info.status in {2, 3} else "unknown"


def _is_runtime(argv: Sequence[str], name: str) -> bool:
    """The process is the runtime: its program is `name`, or node running a `name` script."""

    heads = [Path(part).name for part in argv[:2]]
    return bool(heads) and (heads[0] == name or (heads[0] in {"node", "nodejs"} and len(heads) > 1 and heads[1] in {name, f"{name}.js", "cli.js"}))


def _without_node(argv: list[str], name: str) -> list[str]:
    """`node …/claude --flags` reads as `claude --flags`: the start line a person types."""

    if argv and Path(argv[0]).name in {"node", "nodejs"} and len(argv) > 1:
        return [name, *argv[2:]]
    return argv


def sanitize(argv: Sequence[str], runtime_kind: str) -> list[str]:
    """Drop earlier resume switches and anything secret; keep the rest as started."""

    name = RUNTIME_NAMES.get(runtime_kind, "")
    args = list(argv)
    kept: list[str] = [args[0]] if args else []
    rest = args[1:]
    if runtime_kind == "codex" and rest[:1] == ["resume"]:
        rest = rest[1:]
        if rest and not rest[0].startswith("-"):
            rest = rest[1:]
    skip_next = False
    for index, arg in enumerate(rest):
        if skip_next:
            skip_next = False
            continue
        if runtime_kind == "claude-code" and arg in {"--resume", "-r", "--session-id"}:
            nxt = rest[index + 1] if index + 1 < len(rest) else ""
            skip_next = bool(nxt) and not nxt.startswith("-")
            continue
        if runtime_kind == "claude-code" and (arg.startswith("--resume=") or arg in {"--continue", "-c"}):
            continue
        if runtime_kind == "codex" and arg == "--last":
            continue
        flag, _, value = arg.partition("=")
        if flag.startswith("-") and _SECRET_NAME.search(flag):
            skip_next = not value  # "--api-key X": drop X too; "--api-key=X": one item
            continue
        if not arg.startswith("-") and "=" in arg and _SECRET_NAME.search(flag):
            continue
        if _TOKEN_LIKE.match(arg) and not arg.startswith(("/", "~", ".")):
            continue
        kept.append(arg)
    if name and kept:
        kept[0] = kept[0] if Path(kept[0]).name == name else name
    return [item.encode("utf-8")[:MAX_ARG_BYTES].decode("utf-8", "ignore") for item in kept[:MAX_ARGS]]


def capture(runtime_kind: str, *, probes: Probes | None = None, start_pid: int | None = None) -> dict[str, Any]:
    """The runtime's start line as {argv, cwd, tmux_session, captured_at}; {} when it is not found."""

    name = RUNTIME_NAMES.get(str(runtime_kind or "").strip().lower())
    if not name:
        return {}
    probes = probes or Probes()
    pid = start_pid if start_pid is not None else os.getppid()
    for _ in range(MAX_WALK):
        if pid <= 1:
            return {}
        argv = probes.argv(pid)
        if _is_runtime(argv, name):
            cwd = probes.cwd(pid)
            return {
                "argv": sanitize(_without_node(argv, name), runtime_kind),
                "cwd": cwd if cwd.startswith("/") and len(cwd.encode("utf-8")) <= MAX_CWD_BYTES else "",
                "tmux_session": probes.tmux_session(),
                "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        pid = probes.parent(pid)
    return {}


def _selected_native_id(runtime_kind: str, argv: Sequence[str]) -> str:
    if runtime_kind == "codex":
        return argv[2] if len(argv) > 2 and argv[1] == "resume" and not argv[2].startswith("-") else ""
    for position, word in enumerate(argv[1:], start=1):
        flag, equal, inline = word.partition("=")
        if flag in {"--session-id", "--resume", "-r"}:
            if equal:
                return inline
            if position + 1 < len(argv) and not argv[position + 1].startswith("-"):
                return argv[position + 1]
    return ""


def capture_session_binding(
    runtime_kind: str, session_id: str, *, probes: Probes | None = None, start_pid: int | None = None,
) -> dict[str, Any]:
    """Bind the calling native session to a dedicated local runtime process."""

    binding = {
        "session_id": session_id,
        "bound_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
    }
    name = RUNTIME_NAMES.get(runtime_kind)
    if not name:
        return binding
    probes = probes or Probes()
    inherited = {value for key in RUNTIME_SESSION_ENV[runtime_kind] if (value := probes.env.get(key))}
    if inherited and inherited != {session_id}:
        return binding
    pid = start_pid if start_pid is not None else os.getppid()
    for _ in range(MAX_WALK):
        if pid <= 1:
            break
        token, state = probes.process(pid)
        argv = probes.argv(pid)
        if not _is_runtime(argv, name):
            pid = probes.parent(pid)
            continue
        if Path(argv[0]).name in {"node", "nodejs"} and Path(argv[1]).name == "cli.js":
            package = "@openai" if runtime_kind == "codex" else "@anthropic-ai"
            if package not in Path(argv[1]).parts:
                pid = probes.parent(pid)
                continue
        command = _without_node(argv, name)
        # A shared server can keep running after the addressed thread closes.
        if runtime_kind == "codex" and any(arg in {"app-server", "mcp-server", "exec", "daemon"} for arg in command[1:]):
            break
        selected = _selected_native_id(runtime_kind, command)
        if (selected and selected != session_id) or (not inherited and not selected):
            break
        if token and state == "running" and probes.process(pid) == (token, "running"):
            binding.update(pid=pid, start=token)
        break
    return binding


def observe_session(binding: Mapping[str, Any], *, probes: Probes | None = None) -> str:
    pid, original = binding.get("pid"), binding.get("start")
    if type(pid) is not int or pid <= 1 or not isinstance(original, str) or not original:
        return "unknown"
    observed, state = (probes or Probes()).process(pid)
    if observed and observed != original:
        return "absent"
    return state


__all__ = ["Probes", "capture", "capture_session_binding", "observe_session", "sanitize"]

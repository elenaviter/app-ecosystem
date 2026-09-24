"""The watch guard's kill step keeps the newest watch when run from a shell wrapper (W304 finding 33).

On 2026-09-24 claude-ops ran the guard from claude-code-wake.md. Claude Code
runs every Bash command in a `bash -c` wrapper whose command line holds the
pgrep pattern, so the wrapper matched too, was the youngest match, and was the
one kept: both real watches ended and none was left. These tests run the exact
command from the reference against stand-in watch processes whose command
lines carry the same words, with a younger shell whose command line holds the
pattern. Linux pgrep lists the wrapper that runs the command. macOS pgrep
leaves out its own ancestors, so the stand-in shell plays the wrapper there.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid

import pytest

from project_board.client.procedures import source_package_path

pytestmark = pytest.mark.skipif(
    not (shutil.which("pgrep") and shutil.which("ps") and shutil.which("bash")),
    reason="needs pgrep, ps and bash",
)


def _kill_step(session_id: str) -> str:
    wake = (source_package_path() / "references" / "claude-code-wake.md").read_text(encoding="utf-8")
    match = re.search(r"`(for p in \$\(ps .*?done)`", wake, re.S)
    assert match, "the guard prompt names its kill command"
    command = " ".join(line.strip() for line in match.group(1).splitlines())
    return command.replace("<id>", session_id)


def _watch(session_id: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", "worker", "watch",
         "--runtime-kind", "claude-code", "--runtime-session-id", session_id]
    )


def _shell_holding_the_pattern(session_id: str) -> subprocess.Popen:
    # Younger than every watch and matched by the pattern, as the wrapper is on Linux.
    return subprocess.Popen(["bash", "-c", "sleep 60; true", f"worker watch.*{session_id}"])


def _run_in_wrapper(command: str) -> None:
    # Claude Code evaluates each command inside a shell that runs more after
    # it, so that shell stays alive, younger than every watch, with the
    # pattern in its own command line. `; true` keeps bash from exec-ing away.
    subprocess.run(["bash", "-c", f"sleep 1.1; eval {shlex.quote(command)}; true"], check=False, timeout=30)


def test_the_newest_watch_survives_and_the_older_one_ends():
    session = f"guard-{uuid.uuid4()}"
    older = _watch(session)
    time.sleep(1.1)
    newer = _watch(session)
    time.sleep(1.1)
    shell = _shell_holding_the_pattern(session)
    try:
        _run_in_wrapper(_kill_step(session))
        time.sleep(0.3)
        assert older.poll() is not None
        assert newer.poll() is None
    finally:
        for process in (older, newer, shell):
            if process.poll() is None:
                process.kill()


def test_a_single_watch_is_never_ended():
    session = f"guard-{uuid.uuid4()}"
    only = _watch(session)
    time.sleep(1.1)
    shell = _shell_holding_the_pattern(session)
    try:
        _run_in_wrapper(_kill_step(session))
        time.sleep(0.3)
        assert only.poll() is None
    finally:
        only.kill()
        shell.kill()

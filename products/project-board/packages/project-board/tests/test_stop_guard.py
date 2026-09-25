"""A Claude Code worker's turn never ends without its watch running (W182).

The Stop hook blocks an attending worker's stop once when no ``pb worker
watch`` runs for it, and names the exact commands that re-arm it. Everything
else ends normally: another session, a detached worker, a stop that already
followed a block, and any failure inside the hook.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from project_board.client import cli
from project_board.client.stop_guard import stop_guard_decision, watch_process_alive
from project_board.client.store import SharedFieldStore
from project_board.contract.errors import DomainError

SESSION = "0247bb87-0b0f-4009-aadb-e2df67c509fe"
WORKER = f"claude-code-{SESSION}"


class _Field:
    def __init__(self, *, state: str = "not_listening", pid: int = 0, known: bool = True) -> None:
        self.state = state
        self.pid = pid
        self.known = known

    def read_worker(self, worker_name):
        if not self.known:
            raise DomainError("field_record_not_found", "no such worker", status=404)
        return {"worker_name": WORKER, "runtime_session_id": SESSION}

    def worker_reachability(self, worker_name):
        return {"state": self.state}

    def read_watch_attachment(self, worker_name):
        return {"pid": self.pid} if self.pid else None


def _decide(field, payload=None, alive=lambda pid: pid == 4242):
    return stop_guard_decision(
        {"session_id": SESSION, **(payload or {})}, field=field, worker_name=WORKER, process_alive=alive
    )


def test_an_attending_worker_without_a_watch_is_told_exactly_how_to_rearm():
    decision = _decide(_Field(state="listening"))

    assert decision["decision"] == "block"
    reason = decision["reason"]
    assert f"`exec pb worker watch --runtime-kind claude-code --runtime-session-id {SESSION} 2>&1`" in reason
    assert "timeout_ms 1800000" in reason
    assert f"`pb worker receive --runtime-kind claude-code --runtime-session-id {SESSION} --format brief`" in reason


def test_a_running_watch_lets_the_turn_end():
    assert _decide(_Field(state="listening", pid=4242)) is None


def test_a_dead_watch_pid_is_no_watch():
    assert _decide(_Field(state="listening", pid=999))["decision"] == "block"


@pytest.mark.parametrize("state", ["detached", "retired", "never_listened"])
def test_a_worker_that_owes_no_watch_is_not_blocked(state):
    assert _decide(_Field(state=state)) is None


def test_it_blocks_at_most_once_per_turn():
    assert _decide(_Field(), {"stop_hook_active": True}) is None


def test_a_session_that_is_not_a_worker_is_untouched():
    assert _decide(_Field(known=False)) is None


def _run_hook(monkeypatch, capsys, stdin: str, config: Path | None = None) -> tuple[int, str]:
    args = cli.build_parser().parse_args(
        ["worker", "stop-guard", *(["--config", str(config)] if config else [])]
    )
    code = cli._stop_guard_command(args, stdin=io.StringIO(stdin))
    return code, capsys.readouterr().out


def test_the_hook_never_blocks_on_its_own_failure(monkeypatch, capsys, tmp_path):
    assert _run_hook(monkeypatch, capsys, "not json") == (0, "")
    assert _run_hook(monkeypatch, capsys, "") == (0, "")
    missing = tmp_path / "no-such-config.json"
    assert _run_hook(monkeypatch, capsys, json.dumps({"session_id": SESSION}), missing) == (0, "")


def test_the_watch_attachment_names_the_newest_watch(tmp_path):
    field = SharedFieldStore(tmp_path / "field")
    field.initialize(field_id="test-field")
    assert field.read_watch_attachment(WORKER) is None
    field.record_watch_attachment(WORKER, pid=100, runtime_session_id=SESSION)
    field.record_watch_attachment(WORKER, pid=200, runtime_session_id=SESSION)
    assert field.read_watch_attachment(WORKER)["pid"] == 200


def test_only_a_live_watch_process_counts():
    assert not watch_process_alive(0)
    assert not watch_process_alive(os.getpid()), "a live process that is not a watch"
    watch = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "worker", "watch"])
    try:
        assert watch_process_alive(watch.pid)
    finally:
        watch.kill()
        watch.wait()
    assert not watch_process_alive(watch.pid)


def test_watch_detection_reads_past_an_eighty_column_command(monkeypatch):
    full_command = f"{sys.executable} " + ("long-prefix/" * 8) + " worker watch"
    assert "worker watch" not in full_command[:80]

    monkeypatch.setattr(os, "kill", lambda pid, signal: None)

    def run_ps(command, **kwargs):
        stdout = full_command if "-ww" in command else full_command[:80]
        return SimpleNamespace(stdout=stdout)

    monkeypatch.setattr(subprocess, "run", run_ps)

    assert watch_process_alive(4242)

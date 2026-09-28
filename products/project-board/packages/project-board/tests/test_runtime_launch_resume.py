"""The card's resume command is the agent's real start line (W304 finding 29).

Operator, 2026-09-28: the card showed `claude --resume <id>` with every approved
host root as --add-dir, the same for every agent, while the agents run
`claude --add-dir ~/.kdcube --dangerously-skip-permissions --disallowedTools
AskUserQuestion` from their workspace. `pb worker listen` now captures the
start line of the runtime process; the relay builds the resume command from
it, in tmux when the agent runs in tmux, and without a capture shows the
documented line marked "reconstructed", never the host-roots list.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from project_board.client import host_config
from project_board.client.resume_command import RECONSTRUCTED, build_session_resume_command
from project_board.client.runtime_launch import Probes, capture, sanitize

SESSION = "1815da0e-8302-499b-b371-ba3a3a7fe17c"


class FakeProbes(Probes):
    """A process tree: pid -> (parent, argv, cwd)."""

    def __init__(self, tree, *, tmux: str = "") -> None:
        super().__init__(env={"TMUX": "/tmp/tmux-501/default,1,0"} if tmux else {})
        self.tree = tree
        self.tmux = tmux

    def parent(self, pid):
        return self.tree.get(pid, (0, [], ""))[0]

    def argv(self, pid):
        return list(self.tree.get(pid, (0, [], ""))[1])

    def cwd(self, pid):
        return self.tree.get(pid, (0, [], ""))[2]

    def tmux_session(self):
        return self.tmux


CLAUDE_LINE = ["claude", "--add-dir", "/home/ana/.kdcube", "--dangerously-skip-permissions", "--disallowedTools", "AskUserQuestion"]


def test_listen_finds_the_runtime_above_pb_and_keeps_its_real_line():
    tree = {
        300: (200, ["/usr/bin/python3", "/home/ana/.local/bin/pb", "worker", "listen"], "/home/ana/ws/ana@mint"),
        200: (100, ["/bin/zsh", "-c", "pb worker listen"], "/home/ana/ws/ana@mint"),
        100: (1, CLAUDE_LINE, "/home/ana/.kdcube/pb/workspaces/ana@mint"),
    }
    launch = capture("claude-code", probes=FakeProbes(tree, tmux="ana@mint"), start_pid=300)
    assert launch["argv"] == CLAUDE_LINE
    assert launch["cwd"] == "/home/ana/.kdcube/pb/workspaces/ana@mint"
    assert launch["tmux_session"] == "ana@mint" and launch["captured_at"].endswith("Z")
    # No runtime above pb: nothing is captured.
    assert capture("claude-code", probes=FakeProbes({300: (1, ["bash"], "/")}), start_pid=300) == {}
    # node running the claude script reads as `claude`.
    node = {100: (1, ["/usr/local/bin/node", "/usr/local/lib/node_modules/.bin/claude", "--add-dir", "/x"], "/w")}
    assert capture("claude-code", probes=FakeProbes(node), start_pid=100)["argv"] == ["claude", "--add-dir", "/x"]


@pytest.mark.parametrize(
    ("runtime", "argv", "kept"),
    [
        ("claude-code", ["claude", "--resume", "old-id", "--add-dir", "/k"], ["claude", "--add-dir", "/k"]),
        ("claude-code", ["claude", "-r", "old-id", "--continue", "-c", "--resume=x", "--model", "opus"], ["claude", "--model", "opus"]),
        ("codex", ["codex", "resume", "old-id", "-C", "/w", "--search"], ["codex", "-C", "/w", "--search"]),
        ("codex", ["codex", "resume", "--last", "-c", "sandbox_workspace_write.network_access=true"],
         ["codex", "-c", "sandbox_workspace_write.network_access=true"]),
        ("claude-code", ["claude", "--api-key", "sk-live-1", "--token=abc", "GITHUB_TOKEN=ghp_x", "--add-dir", "/k"], ["claude", "--add-dir", "/k"]),
        # argv[0] stays as the process shows it; the resume line always names `claude`.
        ("claude-code", ["/opt/bin/claude", "ghp_0123456789abcdefABCDEF0123456789abcd", "--add-dir", "/k"], ["/opt/bin/claude", "--add-dir", "/k"]),
    ],
)
def test_earlier_resume_switches_and_secrets_are_dropped(runtime, argv, kept):
    assert sanitize(argv, runtime) == kept


def test_every_value_is_bounded():
    long = ["claude", *[f"--flag{i}" for i in range(100)], "--x", "y" * 2000]
    kept = sanitize(long, "claude-code")
    assert len(kept) == 64 and all(len(item.encode()) <= 512 for item in kept)


def test_resume_is_the_captured_line_with_the_switch_added():
    launch = {"argv": CLAUDE_LINE, "cwd": "/home/ana/.kdcube/pb/workspaces/ana@mint", "tmux_session": ""}
    built = build_session_resume_command(runtime_kind="claude-code", runtime_session_id=SESSION, launch=launch)
    assert built["source"] == "captured"
    assert built["command"] == (
        f"cd /home/ana/.kdcube/pb/workspaces/ana@mint && claude --resume {SESSION} "
        "--add-dir /home/ana/.kdcube --dangerously-skip-permissions --disallowedTools AskUserQuestion"
    )
    assert f"claude --resume {SESSION}" in built["command"]  # the board's check

    codex = build_session_resume_command(
        runtime_kind="codex", runtime_session_id=SESSION,
        launch={"argv": ["codex", "-C", "/w", "--sandbox", "danger-full-access"], "cwd": "/w"},
    )
    assert codex["command"] == f"codex resume {SESSION} -C /w --sandbox danger-full-access"
    no_c = build_session_resume_command(runtime_kind="codex", runtime_session_id=SESSION, launch={"argv": ["codex", "--search"], "cwd": "/w"})
    assert no_c["command"] == f"cd /w && codex resume {SESSION} --search"


def test_in_tmux_the_command_starts_the_same_tmux_session():
    launch = {"argv": CLAUDE_LINE, "cwd": "/home/ana/ws", "tmux_session": "ana@mint"}
    built = build_session_resume_command(runtime_kind="claude-code", runtime_session_id=SESSION, launch=launch)
    first, second = built["command"].split("\n")
    assert first.startswith("tmux new-session -d -s ana@mint 'cd /home/ana/ws && claude --resume ")
    assert second == "tmux attach -t ana@mint"
    assert f"claude --resume {SESSION}" in built["command"]


def test_without_a_capture_the_documented_line_is_marked_reconstructed_and_no_roots_are_listed():
    claude = build_session_resume_command(runtime_kind="claude-code", runtime_session_id=SESSION, working_directory="/tmp")
    assert claude["source"] == "reconstructed"
    assert claude["command"].split("\n")[0] == RECONSTRUCTED
    assert claude["command"].split("\n")[1] == (
        f"cd {Path('/tmp').resolve()} && claude --resume {SESSION} --add-dir ~/.kdcube --dangerously-skip-permissions --disallowedTools AskUserQuestion"
    )
    codex = build_session_resume_command(runtime_kind="codex", runtime_session_id=SESSION, working_directory="/tmp")
    assert codex["command"].split("\n")[1] == (
        f"codex resume {SESSION} -C {Path('/tmp').resolve()} --sandbox danger-full-access --ask-for-approval never --search"
    )
    for built in (claude, codex):
        assert built["command"].count("--add-dir") <= 1


def test_listen_keeps_the_capture_on_the_channel_and_a_later_listen_without_one_keeps_it(tmp_path):
    from test_attendance_materializes_project import _fresh_host

    host, identity, field, config = _fresh_host(tmp_path)
    launch = {"argv": CLAUDE_LINE, "cwd": str(tmp_path), "tmux_session": "", "captured_at": "2026-09-28T10:00:00Z"}
    host_config.enroll_worker_channel(host.path, identity=identity, runtime_launch=launch)
    assert dict(host_config.HostRelayConfig.load(host.path).worker(identity).runtime_launch) == launch
    host_config.enroll_worker_channel(host.path, identity=identity, runtime_launch={})
    assert dict(host_config.HostRelayConfig.load(host.path).worker(identity).runtime_launch) == launch


def test_the_relay_serves_the_captured_resume_command(tmp_path):
    import asyncio
    import dataclasses

    from project_board.client import relay
    from project_board.client.io import content_hash
    from test_attendance_materializes_project import _fresh_host

    host, identity, field, config = _fresh_host(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    calls = []

    class Client:
        async def action(self, *, object_ref, action, payload=None):
            calls.append((action, dict(payload or {})))
            return {"ok": True, "object": {}}

    # The fixture's session is a Codex one.
    assert config.runtime_kind == "codex"
    codex_line = ["codex", "--sandbox", "danger-full-access", "--ask-for-approval", "never", "--search"]
    launched = dataclasses.replace(
        config,
        runtime_launch={"argv": codex_line, "cwd": str(workspace), "tmux_session": ""},
    )
    adapter = relay.ProblemBoardHostRelayAdapter(config=launched, field=field, client=Client())
    payload = {"view_ref": "work:session_resume:20260928T101500Z:resume_0123456789abcdef0123456789abcdef:card"}
    control = {"kind": "session.resume", "project_ref": "", "payload": payload, "payload_hash": content_hash(payload)}
    asyncio.run(adapter._serve_session_resume_view(control))  # noqa: SLF001
    [(action, published)] = calls
    assert action == "session.resume.publish"
    assert published["command"] == (
        f"cd {workspace} && codex resume {config.runtime_session_id} --sandbox danger-full-access --ask-for-approval never --search"
    )

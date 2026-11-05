"""The board can build the resume line while the agent's relay is offline (W1014).

Operator, 2026-11-05: "this is actually the "chicken and the egg" problem of
PB. when the machine restarts, relay is off, and there's no way to restore the
agents since resume command is unavailable", then "i think A". The relay sends
the inputs of `build_session_resume_command` with `worker.publish`; the board
builds the same line with the same function when the relay cannot serve one.
"""

from __future__ import annotations

import asyncio
import dataclasses

from project_board.client import relay
from project_board.client.resume_command import RECONSTRUCTED, board_safe_argv, build_session_resume_command, resume_details
from project_board.client.runtime_launch import sanitize

SESSION = "1815da0e-8302-499b-b371-ba3a3a7fe17c"
PROFILE = {"config_dir": "/home/ana/.claude-accounts/ana@example.com", "executable": "/home/ana/.local/bin/claude"}
CLAUDE_LINE = ["claude", "--add-dir", "/home/ana/.kdcube", "--dangerously-skip-permissions", "--disallowedTools", "AskUserQuestion"]


def _board_line(details: dict, runtime: str, session: str = SESSION) -> str:
    """What the board builds from the published details: the same function, the same inputs."""

    return build_session_resume_command(
        runtime_kind=runtime,
        runtime_session_id=session,
        working_directory=details["working_directory"],
        launch=details["runtime_launch"],
        profile=details.get("claude_profile"),
        kdcube_profile=details["kdcube_profile"],
    )["command"]


def test_a_captured_claude_session_sends_only_the_builders_inputs_and_the_board_line_equals_the_live_one(tmp_path):
    launch = {"argv": CLAUDE_LINE, "cwd": "/home/ana/.kdcube/pb/workspaces/ana@mint", "tmux_session": "ana@mint",
              "captured_at": "2026-11-05T21:00:00Z"}
    details = resume_details(runtime_kind="claude-code", runtime_session_id=SESSION, working_directory=str(tmp_path),
                             launch=launch, profile=PROFILE)
    assert set(details) == {"runtime_launch", "captured_at", "kdcube_profile", "working_directory", "claude_profile"}
    assert details["runtime_launch"] == {"argv": CLAUDE_LINE, "cwd": launch["cwd"], "tmux_session": "ana@mint"}
    assert details["captured_at"] == "2026-11-05T21:00:00Z"
    assert details["claude_profile"] == PROFILE
    live = build_session_resume_command(runtime_kind="claude-code", runtime_session_id=SESSION,
                                        working_directory=str(tmp_path), launch=launch, profile=PROFILE)["command"]
    assert _board_line(details, "claude-code") == live


def test_without_a_capture_the_board_builds_the_same_reconstructed_line_from_a_folder_resolved_here(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    details = resume_details(runtime_kind="codex", runtime_session_id=SESSION, working_directory=str(workspace))
    assert details["runtime_launch"] == {"argv": [], "cwd": "", "tmux_session": ""}
    assert details["working_directory"] == str(workspace.resolve())
    assert "claude_profile" not in details
    line = _board_line(details, "codex")
    assert line.startswith(RECONSTRUCTED)
    assert line == build_session_resume_command(runtime_kind="codex", runtime_session_id=SESSION,
                                                working_directory=str(workspace))["command"]


def test_a_session_with_no_buildable_line_says_why_so_the_board_replaces_an_older_line(tmp_path):
    # Review P2: a current client always answers; {"unavailable": code} replaces what an earlier publish stored.
    assert resume_details(runtime_kind="claude-code", runtime_session_id=SESSION, working_directory=str(tmp_path)) == {
        "unavailable": "work_session_resume_profile_required"}
    assert resume_details(runtime_kind="codex", runtime_session_id="", working_directory=str(tmp_path)) == {
        "unavailable": "work_session_resume_id_required"}
    assert resume_details(runtime_kind="codex", runtime_session_id=SESSION, working_directory="relative/path") == {
        "unavailable": "work_session_resume_path_invalid"}


SECRET_MCP = '{"mcpServers":{"x":{"type":"http","url":"https://example.invalid/mcp","headers":{"Authorization":"Bearer review-only-secret"}}}}'


def test_a_structured_secret_that_passes_the_capture_filter_never_leaves_the_host(tmp_path):
    # Review P1 (codex-infra): --mcp-config takes inline JSON; capture's sanitize() keeps it.
    started = ["claude", "--mcp-config", SECRET_MCP, "--add-dir", "/home/ana/.kdcube", "--dangerously-skip-permissions",
               "--settings={\"env\":{\"X\":\"y\"}}", "--append-system-prompt", "be brief", "--disallowedTools", "AskUserQuestion"]
    captured = sanitize(started, "claude-code")
    assert SECRET_MCP in captured  # the deny-list alone lets it through
    details = resume_details(runtime_kind="claude-code", runtime_session_id=SESSION, working_directory=str(tmp_path),
                             launch={"argv": captured, "cwd": "/home/ana/ws", "tmux_session": ""}, profile=PROFILE)
    assert details["runtime_launch"]["argv"] == [
        "claude", "--add-dir", "/home/ana/.kdcube", "--dangerously-skip-permissions", "--disallowedTools", "AskUserQuestion"]
    flat = repr(details)
    for word in ("review-only-secret", "Authorization", "mcpServers", "--mcp-config", "--settings", "be brief"):
        assert word not in flat, word


def test_only_allow_listed_flags_pass_and_their_values_are_still_checked():
    assert board_safe_argv(["codex", "--sandbox", "danger-full-access", "-c", "model_provider=x", "--search",
                            "--model", "gpt", "do something"], "codex") == ["codex", "--sandbox", "danger-full-access", "--search", "--model", "gpt"]
    assert board_safe_argv(["claude", "--add-dir", "/home/ana/tokens", "--model=opus", "--model={x}"], "claude-code") == [
        "claude", "--model=opus"]
    assert board_safe_argv(["claude", "--unknown-flag", "value", "--verbose"], "claude-code") == ["claude", "--verbose"]


def test_worker_publish_carries_the_details_and_nothing_else_new(tmp_path):
    from test_attendance_materializes_project import _fresh_host

    host, identity, field, config = _fresh_host(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    published = []

    class Client:
        async def action(self, *, object_ref, action, payload=None):
            if action == "worker.publish":
                published.append(dict(payload or {}))
            return {"ok": True, "object": {"worker_alias": config.worker_alias}}

    codex_line = ["codex", "--sandbox", "danger-full-access", "--ask-for-approval", "never", "--search"]
    launched = dataclasses.replace(config, runtime_launch={
        "argv": [*codex_line, "-c", f"mcp_servers.x.http_headers={SECRET_MCP}"], "cwd": str(workspace),
        "tmux_session": "", "captured_at": "2026-11-05T21:00:00Z"})
    adapter = relay.ProblemBoardHostRelayAdapter(config=launched, field=field, client=Client())
    asyncio.run(adapter._publish_worker())  # noqa: SLF001
    [payload] = published
    details = payload["resume_details"]
    assert details == resume_details(
        runtime_kind=launched.runtime_kind, runtime_session_id=launched.runtime_session_id,
        working_directory=launched.workspace or launched.working_directory, launch=launched.runtime_launch,
        kdcube_profile=launched.kdcube_profile)
    assert details["captured_at"] == "2026-11-05T21:00:00Z"
    assert _board_line(details, "codex", launched.runtime_session_id) == (
        f"cd {workspace} && codex resume {launched.runtime_session_id} --sandbox danger-full-access --ask-for-approval never --search")
    assert details["runtime_launch"]["argv"] == codex_line
    flat = repr(payload).lower()
    for word in ("review-only-secret", "secret", "password", "bearer", "cookie", "authorization", "mcp_servers"):
        assert word not in flat, word


def test_a_host_that_refuses_the_resume_view_sends_the_refusal_not_the_details(tmp_path):
    # Review P2: receiver_policy.allow_session_resume_view false covers the offline line too.
    from test_attendance_materializes_project import _fresh_host

    host, identity, field, config = _fresh_host(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    published = []

    class Client:
        async def action(self, *, object_ref, action, payload=None):
            if action == "worker.publish":
                published.append(dict(payload or {}))
            return {"ok": True, "object": {"worker_alias": config.worker_alias}}

    launch = {"argv": ["codex", "--search"], "cwd": str(workspace), "tmux_session": "", "captured_at": "2026-11-05T21:00:00Z"}
    allowed = dataclasses.replace(config, runtime_launch=launch, allow_session_resume_view=True)
    asyncio.run(relay.ProblemBoardHostRelayAdapter(config=allowed, field=field, client=Client())._publish_worker())  # noqa: SLF001
    refused = dataclasses.replace(allowed, allow_session_resume_view=False)
    asyncio.run(relay.ProblemBoardHostRelayAdapter(config=refused, field=field, client=Client())._publish_worker())  # noqa: SLF001
    first, second = published
    assert first["resume_details"]["runtime_launch"]["argv"] == ["codex", "--search"]
    assert second["resume_details"] == {"unavailable": "receiver_policy_session_resume_view_denied"}
    assert str(workspace) not in repr(second)

"""W969, W975: a claude-code procedure install covers ~/.claude and the session's own Claude home, and names each.

Seen twice on 23-24 Oct: an install run from a session whose Claude
configuration directory is an account profile went into that profile only,
left ``~/.claude`` on an older revision, and printed no directory (W969).
Covering every registered profile, and then every profile a worker on the host
is bound to, both reached a profile Project Board must never touch (W975: on
dev-main it is bound to another project's active worker). So without
``--claude-config-dir`` an install covers ``~/.claude`` and the session's own
directory; with it, exactly the folders named (repeatable). The brief output
names each directory written.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from project_board.client import host_config
from project_board.client.claude_profile import capture_binding, register_profile
from project_board.client.procedures import (
    claude_config_roots,
    install_agent_procedure,
    verify_agent_procedure,
)
from project_board.client.render import render_envelope
from project_board.contract.worker_identity import WorkerSessionIdentity


def _folder(home: Path, email: str) -> Path:
    folder = home / ".claude-accounts" / email
    folder.mkdir(parents=True)
    return folder


def _login(folder: Path, email: str, account_id: str) -> None:
    (folder / ".claude.json").write_text(
        '{"oauthAccount": {"accountUuid": "%s", "emailAddress": "%s"}}' % (account_id, email), encoding="utf-8")


def _profile(home: Path, email: str, account_id: str, *, state: str = "active") -> Path:
    """A profile folder a Project Board worker channel on this host is bound to."""

    folder = _folder(home, email)
    _login(folder, email, account_id)
    config = home / "relay.json"
    if not config.exists():
        host_config.initialize_host_config(
            target_id="target", endpoint="https://runtime.example/mcp", tenant="tenant",
            platform_project="project", host_id="host-one", allowed_roots=[str(home)],
            source_repositories={}, config_path=config, state_root=home / "state")
    identity = WorkerSessionIdentity.create("claude-code", str(uuid.uuid4()))
    channel = host_config.enroll_worker_channel(
        config, identity=identity,
        profile="problem-board-claude-" + account_id[:8], authorized=True,
        claude_profile=lambda existing: capture_binding(
            environ={"CLAUDE_CONFIG_DIR": str(folder)}, home=home, existing=existing))
    assert channel.claude_profile.get("config_dir") == str(folder), channel.claude_profile
    if state != "active":
        host_config.set_worker_channel_state(config, identity=identity, state=state)
    return folder


def _registered_only(home: Path, email: str, account_id: str) -> Path:
    """A synthetic Claude profile in the registry that no Project Board worker is bound to."""

    folder = _folder(home, email)
    register_profile(account_id=account_id, email=email, config_dir=folder, confirmed_by="test", home=home)
    return folder


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setenv("KDCUBE_CLIENT_RUNTIME_HOME", str(root / "state"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("PROBLEM_BOARD_CONFIG", raising=False)
    monkeypatch.delenv("PB_PROFILE", raising=False)
    return root


def test_the_default_is_home_claude_and_the_sessions_own_folder(home: Path) -> None:
    ana = _profile(home, "ana@example.com", "11111111-1111-4111-8111-111111111111")
    _profile(home, "bo@example.com", "22222222-2222-4222-8222-222222222222")

    assert claude_config_roots(environ={"CLAUDE_CONFIG_DIR": str(ana)}) == [home / ".claude", ana]
    assert claude_config_roots(environ={}) == [home / ".claude"]


def test_an_explicit_directory_is_the_only_one(home: Path) -> None:
    _profile(home, "ana@example.com", "11111111-1111-4111-8111-111111111111")
    chosen = home / "elsewhere"
    assert claude_config_roots(None, chosen) == [chosen]


def test_several_named_folders_are_covered_once_each(home: Path) -> None:
    one, two = home / "one", home / "two"
    assert claude_config_roots(None, [one, two, one], environ={"CLAUDE_CONFIG_DIR": str(home / "own")}) == [one, two]


def test_a_client_switch_from_an_account_profile_session_leaves_no_stale_home(home: Path) -> None:
    # The acceptance case: the install runs in a session whose own Claude directory is a profile.
    ana = _profile(home, "ana@example.com", "11111111-1111-4111-8111-111111111111")
    import os

    os.environ["CLAUDE_CONFIG_DIR"] = str(ana)
    try:
        installed = install_agent_procedure(["claude-code"])
        verified = verify_agent_procedure(["claude-code"])
    finally:
        del os.environ["CLAUDE_CONFIG_DIR"]

    written = {row["path"] for row in installed}
    assert written == {str(home / ".claude" / "skills" / "problem-board-worker" / "SKILL.md"),
                       str(ana / "skills" / "problem-board-worker" / "SKILL.md")}
    assert {row["state"] for row in verified} == {"current"}
    assert len(verified) == 2


def test_the_brief_output_names_each_directory_written(home: Path) -> None:
    ana = _profile(home, "ana@example.com", "11111111-1111-4111-8111-111111111111")
    installed = install_agent_procedure(["claude-code"], claude_config_dir=[home / ".claude", ana])
    from project_board.client.procedures import source_package

    text = render_envelope({"ok": True, "result": {"package": source_package(), "installed": installed,
                                                 "claude_code_settings_by_dir": [
                                                     {"config_dir": str(home / ".claude"),
                                                      "path": str(home / ".claude" / "settings.json")},
                                                     {"config_dir": str(ana), "path": str(ana / "settings.json")}]}})
    for path in (home / ".claude" / "skills", ana / "skills", home / ".claude" / "settings.json",
                 ana / "settings.json"):
        assert str(path) in text, path


def test_the_brief_verify_names_each_path(home: Path) -> None:
    ana = _profile(home, "ana@example.com", "11111111-1111-4111-8111-111111111111")
    install_agent_procedure(["claude-code"], claude_config_dir=[home / ".claude", ana])
    from project_board.client.procedures import source_package

    verified = verify_agent_procedure(["claude-code"], claude_config_dir=[home / ".claude", ana])
    text = render_envelope({"ok": True, "result": {"package": source_package(), "verified": verified,
                                                   "targets": ["claude-code"]}})
    assert str(home / ".claude" / "skills") in text and str(ana / "skills") in text


def test_listen_reads_the_sessions_own_home_on_a_host_with_several(home: Path, monkeypatch) -> None:
    # Review F1 (#912): listen's procedure check unpacks one row; with several Claude homes it must verify
    # only this session's own, never report the procedure unreadable on a multi-profile host.
    from project_board.client.cli import _runtime_procedure_state

    ana = _profile(home, "ana@example.com", "11111111-1111-4111-8111-111111111111")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(ana))
    install_agent_procedure(["claude-code"])

    state = _runtime_procedure_state("claude-code")

    assert state["state"] == "current", state
    assert len(verify_agent_procedure(["claude-code"])) == 2, "two homes on this host"


def test_procedure_show_still_renders_as_before(home: Path) -> None:
    # Review: `pb procedure show` carries installed and package too; it keeps its own rendering.
    from project_board.client.procedures import source_package

    show = {"procedure": "/x/SKILL.md", "package": source_package(), "installed": [],
            "instruction": "Install the complete package for future sessions."}
    text = render_envelope({"ok": True, "result": show})
    assert "Install the complete package" in text and "--- claude-code" not in text


def test_a_bound_active_profile_of_another_worker_is_never_written_or_verified(home: Path) -> None:
    # W975 acceptance: a plain install or verify never reaches a profile that is not ~/.claude or the
    # session's own, even one bound to an active worker channel on this host or listed in the registry.
    own = _profile(home, "ana@example.com", "11111111-1111-4111-8111-111111111111")
    other = _profile(home, "other@example.com", "44444444-4444-4444-8444-444444444444")
    listed = _registered_only(home, "listed@example.com", "55555555-5555-4555-8555-555555555555")
    import os

    os.environ["CLAUDE_CONFIG_DIR"] = str(own)
    try:
        installed = install_agent_procedure(["claude-code"])
        verified = verify_agent_procedure(["claude-code"])
    finally:
        del os.environ["CLAUDE_CONFIG_DIR"]

    for folder in (other, listed):
        assert not (folder / "skills").exists(), folder
        assert all(not row["path"].startswith(str(folder)) for row in installed + verified)
    assert {Path(row["path"]).parents[2] for row in verified} == {home / ".claude", own}
    assert {row["state"] for row in verified} == {"current"}


def test_a_folder_is_written_only_when_named(home: Path) -> None:
    other = _registered_only(home, "other@example.com", "44444444-4444-4444-8444-444444444444")
    [row] = install_agent_procedure(["claude-code"], claude_config_dir=other)
    assert row["path"] == str(other / "skills" / "problem-board-worker" / "SKILL.md")
    assert not (home / ".claude" / "skills").exists(), "naming a folder covers that folder alone"


def test_claude_config_dir_repeats_on_every_procedure_command() -> None:
    from project_board.client.cli import build_parser

    for command in ("install", "verify", "show"):
        extra = ["--target", "claude-code"]
        args = build_parser().parse_args(["procedure", command, *extra, "--claude-config-dir", "/a", "--claude-config-dir", "/b"])
        assert args.claude_config_dir == ["/a", "/b"], command

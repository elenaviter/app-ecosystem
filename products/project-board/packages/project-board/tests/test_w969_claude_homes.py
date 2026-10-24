"""W969: a claude-code procedure install covers every Claude home on the host and names each one.

Seen twice on 23-24 Oct: an install run from a session whose Claude
configuration directory is an account profile went into that profile only,
left ``~/.claude`` on an older revision, and printed no directory. Without
``--claude-config-dir`` an install now covers ``~/.claude``, every account
profile in the registry and the session's own directory; with it, exactly that
directory. The brief output names each directory written.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from project_board.client.claude_profile import register_profile
from project_board.client.procedures import (
    claude_config_roots,
    install_agent_procedure,
    verify_agent_procedure,
)
from project_board.client.render import render_envelope


def _profile(home: Path, email: str, account_id: str) -> Path:
    folder = home / ".claude-accounts" / email
    folder.mkdir(parents=True)
    register_profile(account_id=account_id, email=email, config_dir=folder, confirmed_by="test", home=home)
    return folder


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return root


def test_every_claude_home_on_the_host_is_covered_once(home: Path) -> None:
    ana = _profile(home, "ana@example.com", "11111111-1111-4111-8111-111111111111")
    bo = _profile(home, "bo@example.com", "22222222-2222-4222-8222-222222222222")

    roots = claude_config_roots(environ={"CLAUDE_CONFIG_DIR": str(ana)})

    assert roots == [home / ".claude", ana, bo], "~/.claude, each registered profile, the session's own once"


def test_an_explicit_directory_is_the_only_one(home: Path) -> None:
    _profile(home, "ana@example.com", "11111111-1111-4111-8111-111111111111")
    chosen = home / "elsewhere"
    assert claude_config_roots(None, chosen) == [chosen]


def test_a_registered_folder_that_does_not_exist_is_skipped(home: Path) -> None:
    gone = _profile(home, "gone@example.com", "33333333-3333-4333-8333-333333333333")
    gone.rmdir()
    assert claude_config_roots(environ={}) == [home / ".claude"]


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
    installed = install_agent_procedure(["claude-code"])
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
    install_agent_procedure(["claude-code"])
    from project_board.client.procedures import source_package

    verified = verify_agent_procedure(["claude-code"])
    text = render_envelope({"ok": True, "result": {"package": source_package(), "verified": verified,
                                                   "targets": ["claude-code"]}})
    assert str(home / ".claude" / "skills") in text and str(ana / "skills") in text

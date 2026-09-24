"""Every W304 onboarding finding is written where a host agent or a person meets it.

The operator, 2026-09-24: "be sure this all documented in procedure."
claude-ops re-onboarded on spark1 from the procedure alone, and each gap it
hit was a step the procedure did not name. These pins keep the steps named.
"""

from __future__ import annotations

from pathlib import Path

from project_board.client.procedures import source_package_path

PROCEDURES = source_package_path().parent
REPO = Path(__file__).resolve().parents[5]


def _words(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


HOST = _words(PROCEDURES / "add-a-worker-host.md")
GUIDE = _words(REPO / "docs" / "project-board" / "add-a-machine.md")


def test_45_the_claude_code_usage_and_stop_settings_are_a_host_step():
    assert "Before the first Claude Code session" in HOST
    assert "the `statusLine` command and the `StopFailure` hook" in HOST
    assert "the `Stop` hook, which keeps the session's watch running" in HOST
    assert "9. Usage and watch settings" in GUIDE


def test_24_an_approved_agent_is_told_to_follow_start_or_resume():
    assert "Authorized. Follow Start Or Resume of the problem-board-worker skill." in HOST
    assert "11. Tell each agent it is approved" in GUIDE


def test_35_sessions_restart_in_place_after_an_update():
    assert "Restart the agent sessions after an update" in HOST
    assert "`/exit`, then the full resume command from step 9" in HOST
    assert "After each update" in GUIDE


def test_38_44_the_peer_list_is_set_on_every_host_old_ones_included():
    assert "pb host configure --allow-peer-worker" in HOST
    assert "A host set up before this setting existed has an empty list" in HOST
    assert "Who may write to your agents" in GUIDE


def test_37_the_welcome_on_joining_is_named_as_pending():
    assert "Joining a project sends the agent no welcome message yet (W304 finding 37, pending)" in HOST


def test_25_33_43_the_watch_and_its_guard_are_the_agent_s_own():
    assert "keeps its own inbox watch and the guard prompt that renews it" in HOST

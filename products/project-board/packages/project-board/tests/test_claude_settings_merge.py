"""pb procedure install --target claude-code merges the worker's status line and hooks (W304 finding 45).

On 2026-09-24 spark1's Claude Code agents had no status line or StopFailure
hook, because the procedure only described them, and their cards said
"limit not reported". The install now merges them into the user's settings,
keeping everything else, and says what it did.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from project_board.client import cli
from project_board.client.claude_settings import merge_claude_code_settings
from project_board.contract.errors import DomainError

PB = "/home/lena/.local/bin/pb"


def _settings(home: Path) -> dict:
    return json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))


def _write(home: Path, value) -> None:
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "settings.json").write_text(json.dumps(value), encoding="utf-8")


def test_a_fresh_host_gets_the_status_line_and_both_hooks(tmp_path):
    report = merge_claude_code_settings(tmp_path, pb=PB)

    settings = _settings(tmp_path)
    assert settings["statusLine"] == {"type": "command", "command": f"{PB} worker limit-state"}
    [failure] = settings["hooks"]["StopFailure"]
    assert failure["matcher"] == "rate_limit|billing_error|account_on_hold|authentication_failed|oauth_org_not_allowed|overloaded|server_error"
    assert failure["hooks"] == [{"type": "command", "command": f"{PB} worker limit-state --source stop-failure"}]
    assert settings["hooks"]["Stop"] == [{"hooks": [{"type": "command", "command": f"{PB} worker stop-guard"}]}]
    assert report["added"] == ["statusLine", "hooks.StopFailure", "hooks.Stop"]
    assert report["backup"] == ""


def test_other_keys_and_hooks_are_kept_and_the_old_file_is_backed_up(tmp_path):
    _write(tmp_path, {"model": "opus", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "notify-me"}]}]}})

    report = merge_claude_code_settings(tmp_path, pb=PB)

    settings = _settings(tmp_path)
    assert settings["model"] == "opus"
    assert settings["hooks"]["Stop"][0] == {"hooks": [{"type": "command", "command": "notify-me"}]}
    assert settings["hooks"]["Stop"][1]["hooks"][0]["command"] == f"{PB} worker stop-guard"
    backup = Path(report["backup"])
    assert json.loads(backup.read_text(encoding="utf-8"))["model"] == "opus"
    assert report["undo"] == f"cp {backup} {tmp_path / '.claude' / 'settings.json'}"


def test_a_second_run_changes_nothing(tmp_path):
    merge_claude_code_settings(tmp_path, pb=PB)
    before = (tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8")

    report = merge_claude_code_settings(tmp_path, pb=PB)

    assert report["changed"] is False
    assert report["added"] == []
    assert (tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8") == before
    assert list((tmp_path / ".claude").glob("settings.json.bak-*")) == []


def test_an_existing_different_status_line_is_left_and_named(tmp_path):
    _write(tmp_path, {"statusLine": {"type": "command", "command": "~/bin/my-status"}})

    report = merge_claude_code_settings(tmp_path, pb=PB)

    assert _settings(tmp_path)["statusLine"]["command"] == "~/bin/my-status"
    assert "statusLine" not in report["added"]
    [note] = report["notes"]
    assert "~/bin/my-status" in note and f"{PB} worker limit-state" in note


def test_a_file_that_is_not_json_is_refused_and_left_alone(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(DomainError) as refused:
        merge_claude_code_settings(tmp_path, pb=PB)

    assert refused.value.code == "work_claude_settings_invalid"
    assert (tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8") == "{not json"


def test_procedure_install_for_claude_code_reports_the_settings_it_merged(tmp_path):
    result = cli._procedure_command(  # noqa: SLF001 - the command under test
        SimpleNamespace(procedure_command="install", target=["claude-code"], home=str(tmp_path), force=False,
                        allow_downgrade=False, config=None)
    )

    assert result["claude_code_settings"]["changed"] is True
    assert "hooks.StopFailure" in result["claude_code_settings"]["added"]
    assert (tmp_path / ".claude" / "settings.json").exists()


def test_procedure_install_for_codex_leaves_claude_settings_alone(tmp_path):
    result = cli._procedure_command(  # noqa: SLF001 - the command under test
        SimpleNamespace(procedure_command="install", target=["codex"], home=str(tmp_path), force=False,
                        allow_downgrade=False, config=None)
    )

    assert "claude_code_settings" not in result
    assert not (tmp_path / ".claude" / "settings.json").exists()

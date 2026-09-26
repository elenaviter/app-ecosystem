"""pb procedure install --target claude-code merges the worker's status line and hooks (W304 finding 45).

On 2026-09-24 spark1's Claude Code agents had no status line or StopFailure
hook, because the procedure only described them, and their cards said
"limit not reported". The install now merges them into the user's settings,
keeping everything else, and says what it did.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from project_board.client import cli
from project_board.client.claude_settings import STOP_FAILURE_ERRORS, merge_claude_code_settings, pb_command
from project_board.contract.errors import DomainError

PB = "/home/agent-user/.local/bin/pb"
ALL_ERRORS = "|".join(STOP_FAILURE_ERRORS)


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


def _executable(path: Path, body: str = "#!/bin/sh\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _install(home: Path, target: str) -> dict:
    return cli._procedure_command(  # noqa: SLF001 - the command under test
        SimpleNamespace(procedure_command="install", target=[target], home=str(home), force=False,
                        allow_downgrade=False, config=None)
    )


def test_procedure_install_for_claude_code_reports_the_settings_it_merged(tmp_path, monkeypatch):
    running = _executable(tmp_path / "opt" / "problem board" / "bin" / "pb")
    monkeypatch.setattr(sys, "argv", [str(running), "procedure", "install"])
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    home = tmp_path / "home"

    result = _install(home, "claude-code")

    assert result["claude_code_settings"]["changed"] is True
    assert "hooks.StopFailure" in result["claude_code_settings"]["added"]
    # Invoked by full path with a PATH that cannot find pb: the hooks name that pb, quoted.
    assert _settings(home)["hooks"]["Stop"][0]["hooks"][0]["command"] == f"'{running}' worker stop-guard"


def test_procedure_install_that_cannot_name_its_pb_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "project_board" / "__main__.py")])
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    home = tmp_path / "home"

    with pytest.raises(DomainError) as refused:
        _install(home, "claude-code")

    assert refused.value.code == "work_claude_settings_pb_unresolved"
    assert not home.exists()


def test_procedure_install_for_codex_leaves_claude_settings_alone(tmp_path):
    result = _install(tmp_path, "codex")

    assert "claude_code_settings" not in result
    assert not (tmp_path / ".claude" / "settings.json").exists()


# -- older Problem Board entries are brought up to date (codex-main review of #116) ------


def test_a_rate_limit_only_stop_failure_group_gains_every_stopping_error(tmp_path):
    _write(tmp_path, {"hooks": {"StopFailure": [
        {"matcher": "rate_limit", "hooks": [{"type": "command", "command": f"{PB} worker limit-state --source stop-failure"}]},
    ]}})

    report = merge_claude_code_settings(tmp_path, pb=PB)

    [group] = _settings(tmp_path)["hooks"]["StopFailure"]
    assert group["matcher"] == ALL_ERRORS
    assert report["updated"] == ["hooks.StopFailure"]
    assert report["changed"] is True and report["backup"]


def test_an_error_the_user_added_to_the_matcher_stays(tmp_path):
    _write(tmp_path, {"hooks": {"StopFailure": [
        {"matcher": "rate_limit|my_error", "hooks": [{"type": "command", "command": "pb worker limit-state --source stop-failure"}]},
    ]}})

    merge_claude_code_settings(tmp_path, pb=PB)

    assert _settings(tmp_path)["hooks"]["StopFailure"][0]["matcher"] == f"{ALL_ERRORS}|my_error"


def test_bare_pb_entries_move_to_this_host_s_pb_and_then_stay(tmp_path):
    _write(tmp_path, {
        "statusLine": {"type": "command", "command": "pb worker limit-state", "padding": 0},
        "hooks": {
            "StopFailure": [{"matcher": ALL_ERRORS, "hooks": [{"type": "command", "command": "pb worker limit-state --source stop-failure"}]}],
            "Stop": [{"hooks": [{"type": "command", "command": "pb worker stop-guard"}]}],
        },
    })

    report = merge_claude_code_settings(tmp_path, pb="/opt/problem-board/bin/pb")

    settings = _settings(tmp_path)
    assert settings["statusLine"] == {"type": "command", "command": "/opt/problem-board/bin/pb worker limit-state", "padding": 0}
    assert settings["hooks"]["StopFailure"][0]["hooks"][0]["command"] == "/opt/problem-board/bin/pb worker limit-state --source stop-failure"
    assert settings["hooks"]["Stop"] == [{"hooks": [{"type": "command", "command": "/opt/problem-board/bin/pb worker stop-guard"}]}]
    assert report["updated"] == ["statusLine", "hooks.StopFailure", "hooks.Stop"]
    assert report["added"] == []
    assert merge_claude_code_settings(tmp_path, pb="/opt/problem-board/bin/pb")["changed"] is False


def test_a_pb_hook_sharing_a_group_leaves_the_user_s_hook_where_it_was(tmp_path):
    _write(tmp_path, {"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "notify-me"},
        {"type": "command", "command": "pb worker stop-guard"},
    ]}]}})

    merge_claude_code_settings(tmp_path, pb=PB)

    assert _settings(tmp_path)["hooks"]["Stop"] == [
        {"hooks": [{"type": "command", "command": "notify-me"}]},
        {"hooks": [{"type": "command", "command": f"{PB} worker stop-guard"}]},
    ]
    assert merge_claude_code_settings(tmp_path, pb=PB)["changed"] is False


# -- a symlinked settings file stays a symlink -------------------------------------------


def test_a_symlinked_settings_file_stays_a_link_and_its_target_changes(tmp_path):
    managed = tmp_path / "dotfiles" / "claude-settings.json"
    managed.parent.mkdir()
    managed.write_text(json.dumps({"model": "opus"}), encoding="utf-8")
    (tmp_path / ".claude").mkdir()
    link = tmp_path / ".claude" / "settings.json"
    link.symlink_to(managed)

    report = merge_claude_code_settings(tmp_path, pb=PB)

    assert link.is_symlink() and os.path.realpath(link) == os.path.realpath(managed)
    written = json.loads(managed.read_text(encoding="utf-8"))
    assert written["model"] == "opus" and written["statusLine"]["command"] == f"{PB} worker limit-state"
    assert report["target"] == os.path.realpath(managed)
    assert json.loads(Path(report["backup"]).read_text(encoding="utf-8")) == {"model": "opus"}


def test_a_symlink_to_a_missing_file_is_refused(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").symlink_to(tmp_path / "gone.json")

    with pytest.raises(DomainError) as refused:
        merge_claude_code_settings(tmp_path, pb=PB)

    assert refused.value.code == "work_claude_settings_invalid"
    assert not (tmp_path / "gone.json").exists()


# -- the pb the hooks name ----------------------------------------------------------------


def test_the_launcher_on_path_is_named_when_it_runs_this_pb(tmp_path):
    running = _executable(tmp_path / "venv" / "bin" / "pb")
    launcher = _executable(tmp_path / "bin" / "pb", f'#!/bin/sh\nexec {running} "$@"\n')

    assert pb_command(str(running), which=lambda name: str(launcher)) == str(launcher)


def test_a_different_pb_on_path_is_not_named(tmp_path):
    running = _executable(tmp_path / "venv" / "bin" / "pb")
    other = _executable(tmp_path / "other" / "pb", '#!/bin/sh\nexec /somewhere/else/pb "$@"\n')

    assert pb_command(str(running), which=lambda name: str(other)) == str(running)


def test_no_pb_executable_is_refused(tmp_path):
    with pytest.raises(DomainError) as refused:
        pb_command(str(tmp_path / "project_board" / "__main__.py"), which=lambda name: None)

    assert refused.value.code == "work_claude_settings_pb_unresolved"


# -- the backup is as private as the settings (codex-main re-review of #116) --------------


def test_the_backup_is_private_and_an_earlier_one_is_kept(tmp_path, monkeypatch):
    _write(tmp_path, {"model": "opus"})
    (tmp_path / ".claude" / "settings.json").chmod(0o600)
    old_umask = os.umask(0o022)
    try:
        first = merge_claude_code_settings(tmp_path, pb=PB)
        _write(tmp_path, {"model": "sonnet"})
        second = merge_claude_code_settings(tmp_path, pb=PB)
    finally:
        os.umask(old_umask)

    assert os.stat(first["backup"]).st_mode & 0o777 == 0o600
    assert os.stat(second["backup"]).st_mode & 0o777 == 0o600
    # Two runs in the same second keep two backups.
    assert first["backup"] != second["backup"]
    assert json.loads(Path(first["backup"]).read_text(encoding="utf-8")) == {"model": "opus"}
    assert json.loads(Path(second["backup"]).read_text(encoding="utf-8")) == {"model": "sonnet"}


# -- W304 (fable-pub's trace, 2026-09-25): pb procedure install through the
#    host launcher. The versioned entry point re-executes the selected release
#    as a .py script, so argv[0] in the release names no executable. --


def test_a_selected_release_names_the_launcher_the_stable_pb_recorded(tmp_path, monkeypatch):
    stable = _executable(tmp_path / "venv" / "bin" / "pb", "#!/bin/sh\n")
    launcher = _executable(tmp_path / "bin" / "pb", f'#!/bin/sh\nexec {stable} "$@"\n')
    release = tmp_path / "releases" / "abc" / "code_entrypoint.py"
    monkeypatch.setattr(sys, "argv", [str(release), "procedure", "install"])
    monkeypatch.setenv("PROBLEM_BOARD_INVOKED_PB", str(stable))
    assert pb_command(which=lambda name: str(launcher)) == str(launcher)


def test_without_the_record_a_selected_release_is_still_refused(tmp_path, monkeypatch):
    release = tmp_path / "releases" / "abc" / "code_entrypoint.py"
    monkeypatch.setattr(sys, "argv", [str(release), "procedure", "install"])
    monkeypatch.delenv("PROBLEM_BOARD_INVOKED_PB", raising=False)
    with pytest.raises(DomainError) as refused:
        pb_command(which=lambda name: None)
    assert refused.value.code == "work_claude_settings_pb_unresolved"


def test_the_entry_point_records_the_stable_pb_before_it_executes_the_release(tmp_path, monkeypatch):
    from project_board.client import entrypoint

    stable = _executable(tmp_path / "venv" / "bin" / "pb", "#!/bin/sh\n")
    release = tmp_path / "releases" / "abc" / "code_entrypoint.py"
    monkeypatch.setattr(sys, "argv", [str(stable), "procedure", "install", "--target", "claude-code"])
    monkeypatch.delenv("PROBLEM_BOARD_INVOKED_PB", raising=False)
    monkeypatch.setattr(entrypoint, "_selected_command", lambda argv, **_: (sys.executable, str(release), *argv))
    executed = {}

    def fake_execv(path, args):
        executed.update(path=path, args=args, recorded=entrypoint.os.environ.get("PROBLEM_BOARD_INVOKED_PB"))
        raise SystemExit(0)

    monkeypatch.setattr(entrypoint.os, "execv", fake_execv)
    with pytest.raises(SystemExit):
        entrypoint.main()
    assert executed["args"][1] == str(release)
    assert executed["recorded"] == str(stable)

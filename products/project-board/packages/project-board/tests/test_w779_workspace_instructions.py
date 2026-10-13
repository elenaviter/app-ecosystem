"""W802 (reworks W779): the workspace AGENTS.md / CLAUDE.md come only from the project's files.

Operator, 2026-10-13: "CLAUDE.md / AGENTS.md made from project file (which we have multiple of them).",
"and in case of new project even NONE", "not sure actually why we promise the CLAUDE.md and AGENTS.md will be
written. that is wrong.", "the folders must be aliases", "--workspaces should be itself removed".
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from project_board.client import cli
from project_board.client import workspace_instructions as wi

ALIAS = "claude-coord-co@e-home"


def _files(**present: bool) -> list[dict[str, str]]:
    rows = [
        {"purpose": "instructions", "alias": "notes", "path": "AGENTS.md", "description": ""},
        {"purpose": "facts", "alias": "notes", "path": "facts.md", "description": ""},
        {"purpose": "environment", "alias": "notes", "path": "env.md", "description": ""},
        {"purpose": "", "alias": "notes", "path": "run-local.md", "description": "How to run it locally"},
    ]
    return [{**row, "state": "present" if present.get(row["path"].split(".")[0].replace("-", "_"), True) else "missing"} for row in rows]


def _ensure(workspace: Path, files, kind="claude-code", alias=ALIAS, revision=3):
    return wi.ensure_project_files_block(
        workspace, alias=alias, runtime_kind=kind, project_id="co-1", files_revision=revision, files=files,
    )


def test_claude_imports_the_purpose_files_and_lists_the_further_ones(tmp_path):
    workspace = tmp_path / ALIAS
    state = _ensure(workspace, _files())
    assert state["state"] == "written" and sorted(state["written"]) == ["AGENTS.md", "CLAUDE.md"]
    agents = (workspace / "AGENTS.md").read_text(encoding="utf-8")
    for line in ("@notes/AGENTS.md", "@notes/facts.md", "@notes/env.md", "- `notes/run-local.md`: How to run it locally"):
        assert line in agents, line
    assert "@notes/run-local.md" not in agents  # further files are read when they fit, not imported
    assert "project=co-1 files_revision=3" in agents
    assert "Anything that waits on the operator" not in agents  # no pb rules copied
    assert "\n@AGENTS.md\n" in (workspace / "CLAUDE.md").read_text(encoding="utf-8")


def test_codex_is_told_to_read_and_gets_no_claude_md(tmp_path):
    workspace = tmp_path / "codex-co@e-home"
    _ensure(workspace, _files(), kind="codex", alias="codex-co@e-home")
    agents = (workspace / "AGENTS.md").read_text(encoding="utf-8")
    assert "Read `notes/AGENTS.md` at the start of every session." in agents and "\n@" not in agents
    assert not (workspace / "CLAUDE.md").exists()


def test_a_project_without_files_gets_nothing(tmp_path):
    workspace = tmp_path / ALIAS
    workspace.mkdir()
    state = _ensure(workspace, [])
    assert state == {"workspace": str(workspace), "state": "none", "written": []}
    assert sorted(path.name for path in workspace.iterdir()) == []


def test_only_files_present_in_the_clones_are_named(tmp_path):
    workspace = tmp_path / ALIAS
    _ensure(workspace, _files(facts=False, env=False, run_local=False))
    agents = (workspace / "AGENTS.md").read_text(encoding="utf-8")
    assert "@notes/AGENTS.md" in agents and "facts.md" not in agents and "run-local" not in agents
    # None present yet: nothing is written.
    other = tmp_path / "claude-two@e-home"
    assert _ensure(other, _files(AGENTS=False, facts=False, env=False, run_local=False), alias="claude-two@e-home")["state"] == "none"
    assert not other.exists() or not any(other.iterdir())


def test_the_block_goes_when_the_files_go_and_user_text_stays(tmp_path):
    workspace = tmp_path / ALIAS
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("# Mine\n\nKeep this.\n", encoding="utf-8")
    _ensure(workspace, _files())
    assert (workspace / "AGENTS.md").read_text(encoding="utf-8").startswith("# Mine\n\nKeep this.\n\n<!-- problem-board:begin")
    again = _ensure(workspace, _files())
    assert again["written"] == []
    gone = _ensure(workspace, [])
    assert sorted(gone["written"]) == ["AGENTS.md (block removed)", "CLAUDE.md (block removed)"]
    assert (workspace / "AGENTS.md").read_text(encoding="utf-8") == "# Mine\n\nKeep this.\n"
    assert not (workspace / "CLAUDE.md").exists()


def test_a_folder_that_is_not_the_alias_is_never_written(tmp_path):
    # Operator: "the folders must be aliases". A <root>/<worker name> fallback folder is never written.
    stray = tmp_path / "claude-code-86a4a7da-2463-4a8e-a720-6e4108918ff7"
    state = _ensure(stray, _files())
    assert state["state"] == "workspace_is_not_an_alias_folder" and not stray.exists()
    assert _ensure(tmp_path / ALIAS, _files(), alias="")["state"] == "workspace_is_not_an_alias_folder"


def test_a_git_work_tree_or_an_unanswerable_git_is_never_written(tmp_path, monkeypatch):
    workspace = tmp_path / ALIAS
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    assert _ensure(workspace, _files())["state"] == "workspace_is_git_worktree"
    assert not (workspace / "AGENTS.md").exists()
    other = tmp_path / "claude-two@e-home"

    def broken(*args, **kwargs):
        raise OSError("git unavailable")

    monkeypatch.setattr(wi.subprocess, "run", broken)
    assert _ensure(other, _files(), alias="claude-two@e-home")["state"] == "workspace_git_check_failed"


def test_w779s_pb_rules_block_and_link_are_retired(tmp_path):
    workspace = tmp_path / ALIAS
    (workspace / ".problem-board").mkdir(parents=True)
    target = tmp_path / "instructions.md"
    target.write_text("x\n", encoding="utf-8")
    (workspace / ".problem-board" / "project-instructions.md").symlink_to(target)
    legacy = "<!-- problem-board:begin rev=2026.10.13.1 -->\n# Problem Board: always-on rules\n1. x\n<!-- problem-board:end -->\n"
    (workspace / "AGENTS.md").write_text("# Mine\n\n" + legacy, encoding="utf-8")
    (workspace / "CLAUDE.md").write_text("<!-- problem-board:begin rev=2026.10.13.1 -->\n@AGENTS.md\n<!-- problem-board:end -->\n", encoding="utf-8")
    state = wi.remove_legacy_rules(workspace)
    assert sorted(state["written"]) == [
        ".problem-board/project-instructions.md (removed)", "AGENTS.md (pb rules block removed)",
        "CLAUDE.md (pb rules block removed)",
    ]
    assert (workspace / "AGENTS.md").read_text(encoding="utf-8") == "# Mine\n"
    assert not (workspace / "CLAUDE.md").exists()
    # A project-files block replaces a legacy block in place, too.
    (workspace / "AGENTS.md").write_text(legacy, encoding="utf-8")
    _ensure(workspace, _files())
    assert "rev=2026.10.13.1" not in (workspace / "AGENTS.md").read_text(encoding="utf-8")


def test_procedure_install_and_verify_take_no_workspaces_flag():
    parser = cli.build_parser()
    for command in (["procedure", "install", "--target", "claude-code", "--workspaces"],
                    ["procedure", "verify", "--workspaces"], ["procedure", "verify", "--workspace", "."]):
        with pytest.raises(SystemExit):
            parser.parse_args(command)


def test_the_context_hook_writes_only_for_the_alias_folder(tmp_path):
    channel = type("Channel", (), {"runtime_kind": "claude-code", "worker_alias": ALIAS})()
    record = {"known": True, "files_revision": 1, "files": []}
    workspace = tmp_path / ALIAS
    workspace.mkdir()
    assert cli._project_files_block(str(workspace), channel, "co-1", record)["state"] == "none"  # noqa: SLF001
    assert list(workspace.iterdir()) == []

"""W779: the always-on rules live in a managed block of the agent workspace's AGENTS.md.

Operator, 2026-10-13: "workspace must get this in form of AGENTS.md so this works for other type of
agents. and if this is claude agent than simply additionally also the CLAUDE.md which imports AGENTS.md".
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from project_board.client import workspace_instructions as wi
from project_board.client.procedures import source_package, source_path
from project_board.contract.errors import DomainError

SKILL = source_path().read_text(encoding="utf-8")
REV = str(source_package()["revision"])


def _ensure(folder, kind="claude-code", rev=REV, project=""):
    return wi.ensure_workspace_instructions(
        folder, runtime_kind=kind, revision=rev, skill_text=SKILL, project_instructions=project,
    )


def _verify(folder, kind="claude-code", rev=REV, project=""):
    return wi.verify_workspace_instructions(
        folder, runtime_kind=kind, revision=rev, skill_text=SKILL, project_instructions=project,
    )


def test_the_rules_are_skill_md_s_own_list_byte_for_byte():
    rules = wi.always_on_rules(SKILL)
    assert len(rules) == 6
    for rule in rules:
        assert rule in SKILL.splitlines()
    assert rules[0].startswith("1. Anything that waits on the operator is a work item assigned to them")


def test_a_skill_without_the_section_is_refused():
    with pytest.raises(DomainError) as caught:
        wi.always_on_rules("# Skill\n\n## Other\n1. x\n")
    assert caught.value.code == "work_workspace_rules_missing"


def test_a_claude_workspace_gets_agents_md_and_a_claude_md_importing_it(tmp_path):
    state = _ensure(tmp_path)
    assert state["state"] == "ok"
    assert sorted(state["written"]) == ["AGENTS.md", "CLAUDE.md"]
    agents = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    for rule in wi.always_on_rules(SKILL):
        assert rule in agents
    assert f"<!-- problem-board:begin rev={REV} -->" in agents
    claude = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert "\n@AGENTS.md\n" in claude


def test_a_codex_workspace_gets_only_agents_md(tmp_path):
    state = _ensure(tmp_path, kind="codex")
    assert state["written"] == ["AGENTS.md"]
    assert not (tmp_path / "CLAUDE.md").exists()


def test_a_second_run_writes_nothing(tmp_path):
    _ensure(tmp_path)
    before = {name: (tmp_path / name).stat().st_mtime_ns for name in ("AGENTS.md", "CLAUDE.md")}
    again = _ensure(tmp_path)
    assert again["written"] == [] and again["state"] == "ok"
    assert before == {name: (tmp_path / name).stat().st_mtime_ns for name in ("AGENTS.md", "CLAUDE.md")}


def test_user_text_outside_the_markers_is_kept(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Mine\n\nKeep this.\n", encoding="utf-8")
    _ensure(tmp_path)
    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert text.startswith("# Mine\n\nKeep this.\n\n<!-- problem-board:begin")
    (tmp_path / "AGENTS.md").write_text(text + "\nAfter the block.\n", encoding="utf-8")
    _ensure(tmp_path, rev="next-revision")
    refreshed = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert refreshed.startswith("# Mine\n\nKeep this.\n\n") and refreshed.endswith("\nAfter the block.\n")
    assert refreshed.count(wi.BEGIN) == 1 and "rev=next-revision" in refreshed


def test_verify_names_missing_stale_and_edited(tmp_path):
    assert _verify(tmp_path)["state"] == "missing"
    _ensure(tmp_path)
    assert _verify(tmp_path)["state"] == "ok"
    assert _verify(tmp_path, rev="newer")["state"] == "stale"
    path = tmp_path / "AGENTS.md"
    path.write_text(path.read_text(encoding="utf-8").replace("1. Anything", "1. Something"), encoding="utf-8")
    assert _verify(tmp_path)["files"]["AGENTS.md"] == "edited"
    assert _ensure(tmp_path)["state"] == "ok"


def test_the_project_instructions_are_linked_never_copied(tmp_path):
    workspace, clone = tmp_path / "ws", tmp_path / "clone"
    clone.mkdir()
    instructions = clone / "AGENTS.md"
    instructions.write_text("project rules\n", encoding="utf-8")
    state = _ensure(workspace, project=str(instructions))
    link = workspace / wi.LINK_DIR / wi.LINK_NAME
    assert link.is_symlink() and os.readlink(link) == str(instructions)
    assert state["project_instructions"] == "ok"
    agents = (workspace / "AGENTS.md").read_text(encoding="utf-8")
    assert f"@{wi.LINK_DIR}/{wi.LINK_NAME}" in agents
    assert "project rules" not in agents
    # Install time knows no project: the link listen made stays, and so does the import.
    again = _ensure(workspace)
    assert again["written"] == [] and link.is_symlink()


def test_codex_is_told_to_read_the_project_file(tmp_path):
    instructions = tmp_path / "p.md"
    instructions.write_text("x\n", encoding="utf-8")
    _ensure(tmp_path / "ws", kind="codex", project=str(instructions))
    agents = (tmp_path / "ws" / "AGENTS.md").read_text(encoding="utf-8")
    assert f"read `{wi.LINK_DIR}/{wi.LINK_NAME}`" in agents and "\n@" not in agents


def test_a_dangling_project_link_is_reported(tmp_path):
    instructions = tmp_path / "p.md"
    instructions.write_text("x\n", encoding="utf-8")
    _ensure(tmp_path / "ws", project=str(instructions))
    instructions.unlink()
    assert _verify(tmp_path / "ws", project=str(instructions))["state"] == "project_instructions_missing"


def test_a_git_work_tree_is_never_written(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    state = _ensure(tmp_path)
    assert state["state"] == "workspace_is_git_worktree" and state["written"] == []
    assert not (tmp_path / "AGENTS.md").exists()


def test_the_block_holds_no_absolute_host_path(tmp_path):
    instructions = tmp_path / "p.md"
    instructions.write_text("x\n", encoding="utf-8")
    _ensure(tmp_path / "ws", project=str(instructions))
    for name in ("AGENTS.md", "CLAUDE.md"):
        text = (tmp_path / "ws" / name).read_text(encoding="utf-8")
        assert str(tmp_path) not in text and str(Path.home()) not in text


def test_instructions_target_prefers_the_instructions_file(tmp_path):
    a, b = tmp_path / "a.md", tmp_path / "b.md"
    a.write_text("a", encoding="utf-8")
    b.write_text("b", encoding="utf-8")
    assert wi.instructions_target({"local_project_instructions": str(a), "local_project_environment": str(b)}) == str(a)
    assert wi.instructions_target({"local_project_instructions": str(tmp_path / "none"), "local_project_environment": str(b)}) == str(b)
    assert wi.instructions_target({}) == ""


def test_configured_workspaces_skip_disabled_agents_and_other_kinds(tmp_path, monkeypatch):
    rows = [
        SimpleNamespace(state="active", runtime_kind="claude-code", worker_name="w1", worker_alias="a1", working_directory=""),
        SimpleNamespace(state="disabled", runtime_kind="claude-code", worker_name="w2", worker_alias="a2", working_directory=""),
        SimpleNamespace(state="active", runtime_kind="codex", worker_name="w3", worker_alias="a3", working_directory=""),
    ]
    config = SimpleNamespace(workers=rows)
    import project_board.client.host_config as host_config

    monkeypatch.setattr(host_config, "agent_workspace", lambda config, **kw: (str(tmp_path / kw["alias"]), "", ""))
    assert wi.configured_workspaces(config, ["claude-code"]) == [(str(tmp_path / "a1"), "claude-code")]
    assert wi.runtime_kind_of(config, str(tmp_path / "a3")) == "codex"

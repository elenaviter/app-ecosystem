"""A project declares its instructions and runtimes; `pb worker context` returns them (W262).

The declaration sits in ``project-setup.json`` at the root of the journal home,
by hand until the project's Control Card holds the same fields. A worker reads
its project's runtimes and their profiles from the context, not from the skill.
"""

from __future__ import annotations

import json
from pathlib import Path

from project_board.client.journals import JournalWorkspace, RepositoryMap
from project_board.client.project_setup import PROJECT_SETUP_FILE, PROJECT_SETUP_SCHEMA

PROJECT = "work:project:project-one"


def _workspace(tmp_path: Path, *, profiles: bool = True) -> tuple[JournalWorkspace, Path, Path]:
    journals = tmp_path / "journal-repo"
    journals.mkdir()
    procedures = tmp_path / "procedures-repo"
    (procedures / "kdcube").mkdir(parents=True)
    (procedures / "kdcube" / "runtime-profile.md").write_text("# KDCube runtime profile\n", encoding="utf-8")
    mapping = {"journals": str(journals)}
    if profiles:
        mapping["procedures"] = str(procedures)
    workspace = JournalWorkspace(tmp_path / "workspace", RepositoryMap.from_mapping(mapping))
    workspace.reconcile(
        {
            "project_ref": PROJECT,
            "journal_home_ref": "repo:journals/projects/project-one",
            "project_artifact_ref": "",
            "revision": 1,
        },
        create_home=True,
    )
    home = journals / "projects" / "project-one"
    (home / "project-instructions.md").write_text("# Project one\n", encoding="utf-8")
    return workspace, home, procedures


def _declare(home: Path, value: dict) -> None:
    (home / PROJECT_SETUP_FILE).write_text(json.dumps(value), encoding="utf-8")


SETUP = {
    "schema": PROJECT_SETUP_SCHEMA,
    "instructions_ref": "repo:journals/projects/project-one/project-instructions.md",
    "runtimes": [
        {
            "name": "dev-main",
            "host": "dev-main",
            "kind": "kdcube",
            "profile_ref": "repo:procedures/kdcube/runtime-profile.md",
            "actions": {
                "refresh": {"who": ["coordinator"], "from_ref": "origin/main"},
                "bundle-reload": {"who": ["coordinator", "operator"], "from_ref": "v1.4.0"},
            },
        }
    ],
}


def test_the_context_returns_the_declared_instructions_and_runtimes(tmp_path):
    workspace, home, procedures = _workspace(tmp_path)
    _declare(home, SETUP)

    context = workspace.context(PROJECT)

    assert context["project_setup_ref"] == "repo:journals/projects/project-one/project-setup.json"
    assert context["project_instructions_ref"] == "repo:journals/projects/project-one/project-instructions.md"
    assert context["local_project_instructions"] == str(home / "project-instructions.md")
    [runtime] = context["runtimes"]
    assert (runtime["name"], runtime["host"], runtime["kind"]) == ("dev-main", "dev-main", "kdcube")
    assert runtime["local_profile"] == str(procedures / "kdcube" / "runtime-profile.md")
    assert runtime["actions"] == [
        {"name": "refresh", "who": ["coordinator"], "from_ref": "origin/main"},
        {"name": "bundle-reload", "who": ["coordinator", "operator"], "from_ref": "v1.4.0"},
    ]
    assert context["project_setup_issues"] == []


def test_an_independent_project_declares_no_runtime_and_gets_none(tmp_path):
    workspace, home, _procedures = _workspace(tmp_path)
    _declare(home, {"schema": PROJECT_SETUP_SCHEMA, "instructions_ref": SETUP["instructions_ref"], "runtimes": []})

    context = workspace.context(PROJECT)

    assert context["runtimes"] == [] and context["project_setup_issues"] == []
    assert context["project_instructions_ref"]


def test_a_project_without_the_file_gets_empty_fields(tmp_path):
    workspace, _home, _procedures = _workspace(tmp_path)

    context = workspace.context(PROJECT)

    assert context["project_setup_ref"] == ""
    assert context["project_instructions_ref"] == "" and context["runtimes"] == []


def test_an_action_without_the_ref_it_releases_is_left_out_and_named(tmp_path):
    workspace, home, _procedures = _workspace(tmp_path)
    broken = json.loads(json.dumps(SETUP))
    broken["runtimes"][0]["actions"]["refresh"] = {"who": ["coordinator"]}
    broken["runtimes"][0]["actions"]["deploy"] = {"who": [], "from_ref": "main"}
    _declare(home, broken)

    context = workspace.context(PROJECT)

    assert [action["name"] for action in context["runtimes"][0]["actions"]] == ["bundle-reload"]
    issues = context["project_setup_issues"]
    assert any("refresh: from_ref must name the git ref" in issue for issue in issues)
    assert any("deploy: who must list" in issue for issue in issues)


def test_an_unreadable_file_never_fails_the_context(tmp_path):
    workspace, home, _procedures = _workspace(tmp_path)
    (home / PROJECT_SETUP_FILE).write_text("{not json", encoding="utf-8")
    assert workspace.context(PROJECT)["project_setup_issues"] == ["project-setup.json is not readable JSON: JSONDecodeError"]

    _declare(home, {"schema": "something-else"})
    assert "must be an object with schema" in workspace.context(PROJECT)["project_setup_issues"][0]

    _declare(home, {**SETUP, "runtimes": [SETUP["runtimes"][0], SETUP["runtimes"][0]]})
    context = workspace.context(PROJECT)
    assert len(context["runtimes"]) == 1 and "declared twice" in context["project_setup_issues"][0]


def test_a_profile_whose_checkout_is_not_on_this_host_keeps_its_ref(tmp_path):
    workspace, home, _procedures = _workspace(tmp_path, profiles=False)
    _declare(home, SETUP)

    [runtime] = workspace.context(PROJECT)["runtimes"]

    assert runtime["profile_ref"] == "repo:procedures/kdcube/runtime-profile.md"
    assert runtime["local_profile"] == ""


def test_a_control_character_or_a_cut_at_the_caps_is_named(tmp_path):
    """Review of #178 (claude-app): every control character is refused, and a cut is said."""

    from project_board.client import project_setup

    workspace, home, _procedures = _workspace(tmp_path)
    broken = json.loads(json.dumps(SETUP))
    broken["runtimes"][0]["host"] = "dev\tmain"
    broken["runtimes"][0]["actions"]["refresh"]["from_ref"] = "origin/main\x1b[31m"
    _declare(home, broken)
    context = workspace.context(PROJECT)
    assert context["runtimes"] == []
    assert any("host must name" in issue for issue in context["project_setup_issues"])

    many = {**SETUP, "runtimes": [
        {**SETUP["runtimes"][0], "name": f"r{index}"} for index in range(project_setup.MAX_RUNTIMES + 2)
    ]}
    many["runtimes"][0] = {**many["runtimes"][0], "actions": {
        f"a{index}": {"who": ["coordinator"], "from_ref": "main"} for index in range(project_setup.MAX_ACTIONS + 1)
    }}
    _declare(home, many)
    context = workspace.context(PROJECT)
    assert len(context["runtimes"]) == project_setup.MAX_RUNTIMES
    assert len(context["runtimes"][0]["actions"]) == project_setup.MAX_ACTIONS
    issues = context["project_setup_issues"]
    assert f"only the first {project_setup.MAX_RUNTIMES} runtimes are read" in issues
    assert any(f"only the first {project_setup.MAX_ACTIONS} actions are read" in issue for issue in issues)

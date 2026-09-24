"""`pb worker context` names the project facts page (W262).

The coordinator asked on 2026-09-24 for one easily reached page holding a
project's standing facts (hosts, agents, the client release) after a context
compaction lost them. The page sits at the root of the journal home, and the
context names it so every agent reads it at start. A project without the page
gets empty values, never a ref to nothing.
"""

from __future__ import annotations

from pathlib import Path

from project_board.client.journals import PROJECT_FACTS_FILE, JournalWorkspace, RepositoryMap

PROJECT = "work:project:project-one"


def _workspace(tmp_path: Path) -> tuple[JournalWorkspace, Path]:
    repository = tmp_path / "journal-repo"
    repository.mkdir()
    workspace = JournalWorkspace(tmp_path / "workspace", RepositoryMap.from_mapping({"journals": str(repository)}))
    workspace.reconcile(
        {
            "project_ref": PROJECT,
            "journal_home_ref": "repo:journals/projects/project-one",
            "project_artifact_ref": "",
            "revision": 1,
        },
        create_home=True,
    )
    return workspace, repository / "projects" / "project-one"


def test_the_context_names_the_project_facts_page_when_it_exists(tmp_path):
    workspace, home = _workspace(tmp_path)
    (home / PROJECT_FACTS_FILE).write_text("# Project one: project facts\n", encoding="utf-8")

    context = workspace.context(PROJECT)

    assert context["project_facts_ref"] == "repo:journals/projects/project-one/project-facts.md"
    assert context["local_project_facts"] == str(home / PROJECT_FACTS_FILE)


def test_a_project_without_the_page_gets_no_ref(tmp_path):
    workspace, _home = _workspace(tmp_path)

    context = workspace.context(PROJECT)

    assert context["project_facts_ref"] == ""
    assert context["local_project_facts"] == ""

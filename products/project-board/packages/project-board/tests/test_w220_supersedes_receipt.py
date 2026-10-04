"""W220: a superseding journal entry records what it supersedes in its receipt.

The real parser kept `supersedes_ref` only in the searchable front matter
text: JournalDocument.metadata() omitted it, so the entry handed to
record_journal_receipt carried none and the immutable receipt recorded an
empty correction link. The regression indexes actual Markdown files, not an
injected entry, and reads the receipt the store keeps.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from project_board.client.journals import JournalWorkspace, RepositoryMap
from project_board.client.store import SharedFieldStore

WORKER = "codex-api"
PROJECT_REF = "work:project:project-one"
ORIGINAL = "work:journal:20261004T001000Z:journal_0123456789abcdef0123456789abcdef:original-finding"
CORRECTION = "work:journal:20261004T002000Z:journal_fedcba9876543210fedcba9876543210:corrected-finding"


@pytest.fixture
def field(tmp_path: Path) -> SharedFieldStore:
    store = SharedFieldStore(tmp_path / "shared-field")
    store.initialize(field_id="test-field")
    store.register_worker(worker_name=WORKER, runtime_kind="codex", capabilities=[], authority_label="synthetic")
    store.listen_worker(WORKER)
    store.create_project(project_id="project-one", title="One", goal="Journals.", owner="operator")
    store.sync_worker_attendances(WORKER, [PROJECT_REF])
    return store


def _workspace(tmp_path: Path) -> JournalWorkspace:
    repository = tmp_path / "journal-repo"
    repository.mkdir()
    workspace = JournalWorkspace(tmp_path / "workspace", RepositoryMap.from_mapping({"journals": str(repository)}))
    workspace.reconcile(
        {"project_ref": PROJECT_REF, "journal_home_ref": "repo:journals/projects/project-one", "project_artifact_ref": "", "revision": 1},
        create_home=True,
    )
    return workspace


def _write(workspace: JournalWorkspace, name: str, **frontmatter) -> str:
    context = workspace.context(PROJECT_REF)
    path = Path(context["local_journal_directory"]) / name
    metadata = {"project_ref": PROJECT_REF, "worker_name": WORKER, "recorded_at": "2026-10-04T00:20:00Z", **frontmatter}
    path.write_text("---\n" + yaml.safe_dump(metadata, sort_keys=False) + "---\n\n# Entry\n", encoding="utf-8")
    return f"{context['journal_authoring']['directory_ref']}/{name}"


@pytest.mark.parametrize("key", ["supersedes_ref", "supersedes"])
def test_a_superseding_entry_records_its_link_in_the_receipt(field, tmp_path, key):
    workspace = _workspace(tmp_path)
    original_ref = _write(workspace, "original.md", entry_ref=ORIGINAL, title="Original finding")
    correction_ref = _write(workspace, "correction.md", entry_ref=CORRECTION, title="Corrected finding", **{key: ORIGINAL})

    original = workspace.prepare_entry(project_ref=PROJECT_REF, repository_journal_ref=original_ref)
    correction = workspace.prepare_entry(project_ref=PROJECT_REF, repository_journal_ref=correction_ref)

    assert original["supersedes_ref"] == ""
    assert correction["supersedes_ref"] == ORIGINAL, "the parser carries the link out of the front matter"

    field.record_journal_receipt("project-one", worker_name=WORKER, entry=original)
    field.record_journal_receipt("project-one", worker_name=WORKER, entry=correction)

    assert field.read_journal_receipt("project-one", entry_ref=CORRECTION)["supersedes_ref"] == ORIGINAL
    assert field.read_journal_receipt("project-one", entry_ref=ORIGINAL)["supersedes_ref"] == ""


def test_an_entry_without_a_link_still_has_an_empty_one(tmp_path):
    workspace = _workspace(tmp_path)
    ref = _write(workspace, "plain.md", entry_ref=ORIGINAL, title="Plain")

    assert workspace.prepare_entry(project_ref=PROJECT_REF, repository_journal_ref=ref)["supersedes_ref"] == ""

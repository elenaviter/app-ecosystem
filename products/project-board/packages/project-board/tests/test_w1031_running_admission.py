"""W1031 slice 3a: running-entry admission at the shared source boundary (R2).

Every reader (explicit index, automatic refresh, catalog search and direct browse)
comes through JournalWorkspace._document_from_path. A running entry is admitted
against its last accepted generation in Git: a rewritten version is reported and
the accepted text is served; the strict explicit index refuses it; a forward
commit that extends the accepted version recovers; running status is sticky; and
missing or shallow history fails closed. Ordinary entries are unaffected.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

from project_board.client.journal_running import format_record, running_entry_ref
from project_board.client.journals import JournalWorkspace, RepositoryMap
from project_board.contract.errors import DomainError

PROJECT_REF = "work:project:project-one"
FEATURE, MONTH = "journal-updates", "2026-11"
ENTRY = running_entry_ref(project_ref=PROJECT_REF, feature=FEATURE, month=MONTH)


def git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), "-c", "user.name=example-worker", "-c", "user.email=t@example.invalid",
                        *args], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def record(at, body, corrects=None):
    return format_record(worker="codex-api", alias="a", work_ref="work:plan:node:w1", recorded_at=at, title="t",
                         body=body, corrects=corrects)


def entry_text(records, title="Journal updates, November", kind="running"):
    meta = {"entry_ref": ENTRY, "project_ref": PROJECT_REF, "worker_name": "codex-api",
            "recorded_at": "2026-11-01T00:00:00Z", "title": title, "kind": kind}
    return "---\n" + yaml.safe_dump(meta, sort_keys=False) + "---\n\n" + "".join(records)


def workspace_for(root: Path, repo: Path) -> JournalWorkspace:
    ws = JournalWorkspace(root, RepositoryMap.from_mapping({"journals": str(repo)}))
    ws.reconcile({"project_ref": PROJECT_REF, "journal_home_ref": "repo:journals/projects/project-one",
                  "project_artifact_ref": "", "revision": 1}, create_home=True)
    return ws


def running_path(ws) -> Path:
    home = Path(ws.context(PROJECT_REF)["local_journal_directory"])
    path = home / "features" / FEATURE / f"{MONTH}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def ref_of(ws) -> str:
    return f"{ws.context(PROJECT_REF)['journal_authoring']['directory_ref']}/features/{FEATURE}/{MONTH}.md"


def publish(ws, repo, text, message="j"):
    running_path(ws).write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def hits(ws, word):
    return [h["entry_ref"] for h in ws.search(word, project_ref=PROJECT_REF)]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "journal-repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "commit", "-q", "--allow-empty", "-m", "root")
    return path


@pytest.fixture
def plain(tmp_path: Path) -> Path:
    path = tmp_path / "plain-repo"
    path.mkdir()
    return path


A = [record("2026-11-02T08:00:00Z", "Tried alphaword.")]


def test_valid_append_is_served(tmp_path, repo):
    root, repo, ws = tmp_path, repo, workspace_for(tmp_path / "ws", repo)
    publish(ws, repo, entry_text(A))
    publish(ws, repo, entry_text(A + [record("2026-11-03T08:00:00Z", "Then betaword.")]))
    assert hits(ws, "betaword") == [ENTRY] and hits(ws, "alphaword") == [ENTRY]


def test_rewrite_serves_accepted_and_strict_index_refuses(tmp_path, repo):
    root, repo, ws = tmp_path, repo, workspace_for(tmp_path / "ws", repo)
    publish(ws, repo, entry_text(A))
    publish(ws, repo, entry_text([record("2026-11-02T08:00:00Z", "Tried gammaword instead.")]), "rewrite")
    assert hits(ws, "gammaword") == [], "the rewritten text must not be served"
    assert hits(ws, "alphaword") == [ENTRY], "the accepted text is served"
    try:
        ws.prepare_entry(project_ref=PROJECT_REF, repository_journal_ref=ref_of(ws))
        refused = None
    except DomainError as exc:
        refused = exc.code
    assert refused == "journal_running_entry_rewritten", refused


def test_forward_commit_recovers(tmp_path, repo):
    root, repo, ws = tmp_path, repo, workspace_for(tmp_path / "ws", repo)
    publish(ws, repo, entry_text(A))
    publish(ws, repo, entry_text([record("2026-11-02T08:00:00Z", "Tried gammaword instead.")]), "rewrite")
    publish(ws, repo, entry_text(A + [record("2026-11-04T08:00:00Z", "Correction deltaword.")]), "recover")
    assert hits(ws, "deltaword") == [ENTRY] and hits(ws, "gammaword") == []
    entry = ws.prepare_entry(project_ref=PROJECT_REF, repository_journal_ref=ref_of(ws))
    assert entry["entry_ref"] == ENTRY


def test_kind_flip_does_not_lift_the_check(tmp_path, repo):
    root, repo, ws = tmp_path, repo, workspace_for(tmp_path / "ws", repo)
    publish(ws, repo, entry_text(A))
    publish(ws, repo, entry_text([record("2026-11-02T08:00:00Z", "Tried gammaword instead.")], kind="ordinary"),
            "flip and rewrite")
    assert hits(ws, "gammaword") == [] and hits(ws, "alphaword") == [ENTRY]


def test_browse_shows_the_accepted_title(tmp_path, repo):
    root, repo, ws = tmp_path, repo, workspace_for(tmp_path / "ws", repo)
    publish(ws, repo, entry_text(A, title="Accepted title"))
    publish(ws, repo, entry_text([record("2026-11-02T08:00:00Z", "rewritten")], title="Rewritten title"), "rewrite")
    titles = [row.get("title") for row in ws.view_catalog_page(project_ref=PROJECT_REF, query="")["entries"]]
    assert "Accepted title" in titles and "Rewritten title" not in titles, titles


def test_malformed_first_version_excluded(tmp_path, repo):
    root, repo, ws = tmp_path, repo, workspace_for(tmp_path / "ws", repo)
    publish(ws, repo, entry_text(["<!-- pb-record [] -->\n## h\nepsilonword\n"]))
    assert hits(ws, "epsilonword") == []


def test_shallow_clone_fails_closed(tmp_path, repo):
    root, repo, ws = tmp_path, repo, workspace_for(tmp_path / "ws", repo)
    publish(ws, repo, entry_text(A))
    publish(ws, repo, entry_text(A + [record("2026-11-03T08:00:00Z", "Then betaword.")]))
    shallow = root / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{repo}", str(shallow)], check=True)
    ws2 = workspace_for(root / "ws2", shallow)
    assert hits(ws2, "betaword") == [] and hits(ws2, "alphaword") == []


def test_non_git_home_running_entry_excluded(tmp_path, plain):
    root, repo, ws = tmp_path, plain, workspace_for(tmp_path / "ws", plain)
    running_path(ws).write_text(entry_text(A), encoding="utf-8")
    assert hits(ws, "alphaword") == []


def test_ordinary_entry_unaffected(tmp_path, plain):
    root, repo, ws = tmp_path, plain, workspace_for(tmp_path / "ws", plain)
    home = Path(ws.context(PROJECT_REF)["local_journal_directory"])
    meta = {"entry_ref": "work:journal:20261004T001000Z:journal_0123456789abcdef0123456789abcdef:plain",
            "project_ref": PROJECT_REF, "worker_name": "codex-api", "recorded_at": "2026-10-04T00:10:00Z",
            "title": "Plain"}
    (home / "plain.md").write_text("---\n" + yaml.safe_dump(meta) + "---\n\nzetaword\n", encoding="utf-8")
    assert len(hits(ws, "zetaword")) == 1


# --- pb-knowledge's slice 3a findings (reproduced at fe97c962) ---------------------------

def other_path(ws, name="other/notes.md") -> Path:
    path = Path(ws.context(PROJECT_REF)["local_journal_directory"]) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def publish_at(repo, path, text, message="j"):
    path.write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def plain_text(body, title="Notes"):
    meta = {"entry_ref": ENTRY, "project_ref": PROJECT_REF, "worker_name": "codex-api",
            "recorded_at": "2026-11-01T00:00:00Z", "title": title}
    return "---\n" + yaml.safe_dump(meta, sort_keys=False) + "---\n\n" + body


def test_a_quoted_yaml_kind_is_running(tmp_path, repo):
    ws = workspace_for(tmp_path / "ws", repo)
    quoted = lambda records: entry_text(records).replace("kind: running", 'kind: "running"')
    publish(ws, repo, quoted(A))
    publish(ws, repo, quoted([record("2026-11-02T08:00:00Z", "Tried gammaword instead.")]), "rewrite")
    assert hits(ws, "gammaword") == [] and hits(ws, "alphaword") == [ENTRY]


def test_running_status_is_sticky_outside_features_on_a_fresh_workspace(tmp_path, repo):
    ws = workspace_for(tmp_path / "ws", repo)
    path = other_path(ws)
    publish_at(repo, path, entry_text(A))
    publish_at(repo, path, plain_text("Rewritten as gammaword, no records."), "flip and strip")
    fresh = workspace_for(tmp_path / "fresh", repo)
    assert hits(fresh, "gammaword") == [] and hits(fresh, "alphaword") == [ENTRY]
    ref = f"{fresh.context(PROJECT_REF)['journal_authoring']['directory_ref']}/other/notes.md"
    with pytest.raises(DomainError) as caught:
        fresh.prepare_entry(project_ref=PROJECT_REF, repository_journal_ref=ref)
    assert caught.value.code == "journal_running_entry_rewritten"


def test_a_rename_out_of_features_that_rewrites_keeps_the_accepted_text(tmp_path, repo):
    ws = workspace_for(tmp_path / "ws", repo)
    publish(ws, repo, entry_text(A))
    old, new = running_path(ws), other_path(ws, "moved.md")
    git(repo, "mv", str(old), str(new))
    new.write_text(plain_text("Moved and rewritten as gammaword."), encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "move, flip and strip")
    fresh = workspace_for(tmp_path / "fresh", repo)
    assert hits(fresh, "gammaword") == [] and hits(fresh, "alphaword") == [ENTRY]

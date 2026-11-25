"""W1031 slice 2: deliberate update of an indexed journal entry through every receipt guard.

Design GO at W1031 revision 23 (R1 with revision 3 and the operation-identity
clarification): ``--update`` passes the workflow pre-check, the receipt step and the
store's locked compare-and-advance together; an absent receipt is materialized
explicitly; the operation identity binds the verified target commit; an older
operation never overwrites a newer receipt. Without the flag nothing changes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from project_board.client import cli
from project_board.client.journal_history import journal_entry_history
from project_board.client.journal_operations import JournalIndexWorkflow
from project_board.client.journals import JournalWorkspace, RepositoryMap
from project_board.client.store import SharedFieldStore
from project_board.contract.errors import DomainError

WORKER = "codex-api"
PROJECT_ID = "project-one"
PROJECT_REF = "work:project:project-one"
ENTRY = "work:journal:20261004T001000Z:journal_0123456789abcdef0123456789abcdef:running-design-record"
ENTRY_REF = ENTRY
TRAILERS = f"Journal-Update: {ENTRY}\nJournal-Worker: {WORKER}"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), "-c", "user.name=example-worker",
                             "-c", "user.email=t@example.invalid", *args], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _field(root: Path) -> SharedFieldStore:
    store = SharedFieldStore(root)
    store.initialize(field_id="test-field")
    store.register_worker(worker_name=WORKER, runtime_kind="codex", capabilities=[], authority_label="synthetic")
    store.listen_worker(WORKER)
    store.create_project(project_id=PROJECT_ID, title="One", goal="Journals.", owner="operator")
    store.sync_worker_attendances(WORKER, [PROJECT_REF])
    return store


class Host:
    """One machine: its own field store (receipts) and journal workspace over a shared Git clone."""

    def __init__(self, root: Path, repository: Path) -> None:
        self.field = _field(root / "field")
        self.workspace = JournalWorkspace(root / "workspace", RepositoryMap.from_mapping({"journals": str(repository)}))
        self.workspace.reconcile(
            {"project_ref": PROJECT_REF, "journal_home_ref": "repo:journals/projects/project-one",
             "project_artifact_ref": "", "revision": 1},
            create_home=True,
        )
        self.workflow = JournalIndexWorkflow(field=self.field, workspace=self.workspace,
                                             operation_root=root / "operations")

    def index(self, ref: str, *, update: bool = False) -> dict:
        return self.workflow.start(project_id=PROJECT_ID, worker_name=WORKER, repository_journal_ref=ref,
                                   update=update)

    def receipt(self) -> dict:
        return self.field.read_journal_receipt(PROJECT_ID, entry_ref=ENTRY)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "journal-repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "commit", "-q", "--allow-empty", "-m", "root")
    return path


@pytest.fixture
def host(tmp_path: Path, repo: Path) -> Host:
    return Host(tmp_path / "host-a", repo)


def _publish(host: Host, repo: Path, body: str, message: str = "journal") -> str:
    context = host.workspace.context(PROJECT_REF)
    path = Path(context["local_journal_directory"]) / "running-design-record.md"
    metadata = {"entry_ref": ENTRY, "project_ref": PROJECT_REF, "worker_name": WORKER,
                "recorded_at": "2026-10-04T00:10:00Z", "title": "Running design record"}
    path.write_text("---\n" + yaml.safe_dump(metadata, sort_keys=False) + "---\n\n" + body + "\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return f"{context['journal_authoring']['directory_ref']}/running-design-record.md"


def test_without_the_flag_a_changed_entry_is_refused_naming_both_routes(host, repo):
    ref = _publish(host, repo, "First decision: alpha.")
    host.index(ref)
    _publish(host, repo, "First decision: alpha. Later finding: beta.", f"amend\n\n{TRAILERS}")
    with pytest.raises(DomainError) as caught:
        host.index(ref)
    error = caught.value
    assert error.code == "field_journal_receipt_conflict"
    assert "--update" in str(error) and "supersedes" in str(error)
    assert error.details["required_action"]["code"] == "author_superseding_journal_entry"
    assert error.details["update_action"]["command"].endswith("--update")


def test_update_advances_the_receipt_and_search_returns_the_amended_text(host, repo):
    ref = _publish(host, repo, "First decision: alpha.")
    host.index(ref)
    commit = _publish(host, repo, "First decision: alpha. Later finding: betaword.", f"amend\n\n{TRAILERS}")
    head = _git(repo, "rev-parse", "HEAD")
    result = host.index(ref, update=True)
    receipt = host.receipt()
    assert receipt["commit"] == head
    assert [op["mode"] for op in receipt["local_operations"]] == ["index", "update"]
    assert all(op["scope"] == "local" for op in receipt["local_operations"])
    assert "warnings" not in result
    hits = host.workspace.search("betaword", project_ref=PROJECT_REF)
    assert [hit["entry_ref"] for hit in hits] == [ENTRY]
    del commit


def test_replaying_an_update_with_the_current_content_adds_nothing(host, repo):
    ref = _publish(host, repo, "alpha")
    host.index(ref)
    _publish(host, repo, "alpha beta", f"amend\n\n{TRAILERS}")
    host.index(ref, update=True)
    before = host.receipt()["local_operations"]
    host.index(ref, update=True)
    assert host.receipt()["local_operations"] == before


def test_a_to_b_to_a_to_b_is_four_transitions_not_replays(host, repo):
    ref = _publish(host, repo, "version A")
    first = host.index(ref)
    ids = {first["operation_id"]}
    for body in ("version B", "version A", "version B"):
        _publish(host, repo, body, f"cycle\n\n{TRAILERS}")
        ids.add(host.index(ref, update=True)["operation_id"])
    assert len(ids) == 4
    assert [op["mode"] for op in host.receipt()["local_operations"]] == ["index", "update", "update", "update"]


def test_update_needs_a_published_version(host, repo):
    ref = _publish(host, repo, "alpha")
    host.index(ref)
    path = Path(host.workspace.context(PROJECT_REF)["local_journal_directory"]) / "running-design-record.md"
    path.write_text(path.read_text(encoding="utf-8") + "\nUncommitted edit.\n", encoding="utf-8")
    with pytest.raises(DomainError) as caught:
        host.index(ref, update=True)
    assert caught.value.code == "journal_entry_not_published"


def test_an_update_without_trailers_warns_that_intent_is_unrecorded(host, repo):
    ref = _publish(host, repo, "alpha")
    host.index(ref)
    _publish(host, repo, "alpha beta", "amend without trailers")
    result = host.index(ref, update=True)
    assert [warning["code"] for warning in result["warnings"]] == ["journal_update_intent_unrecorded"]


def test_a_fresh_host_materializes_the_receipt_and_shares_the_git_history(tmp_path, repo, host):
    ref = _publish(host, repo, "alpha")
    host.index(ref)
    _publish(host, repo, "alpha beta", f"amend\n\n{TRAILERS}")
    host.index(ref, update=True)
    fresh = Host(tmp_path / "host-b", repo)
    fresh.index(ref)  # unflagged, no receipt on this host: works as before
    assert [op["mode"] for op in fresh.receipt()["local_operations"]] == ["materialize"]
    assert fresh.receipt()["content_hash"] == host.receipt()["content_hash"]
    history_a = journal_entry_history(host.workspace, repository_journal_ref=ref)
    history_b = journal_entry_history(fresh.workspace, repository_journal_ref=ref)
    assert history_a["versions"] == history_b["versions"]
    assert [v["intent"] for v in history_a["versions"]] == ["first", "update"]
    assert history_a["complete"] is True


def test_interrupted_update_resumes_to_the_target(host, repo, monkeypatch):
    ref = _publish(host, repo, "alpha")
    host.index(ref)
    _publish(host, repo, "alpha beta", f"amend\n\n{TRAILERS}")
    real = host.field.record_journal_receipt
    calls = {"n": 0}

    def crash_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise DomainError("simulated_crash", "crash before the receipt", status=503)
        return real(*args, **kwargs)

    monkeypatch.setattr(host.field, "record_journal_receipt", crash_once)
    with pytest.raises(DomainError) as crashed:
        host.index(ref, update=True)
    assert crashed.value.code == "simulated_crash"
    assert host.receipt()["local_operations"][-1]["mode"] == "index"  # nothing advanced
    # Retrying the same publication is the same request, so the same operation id:
    # the ledger resumes its failed receipt step instead of starting a new transition.
    result = host.index(ref, update=True)
    resumed = host.workflow.resume(result["operation_id"], worker_name=WORKER)
    assert resumed["operation_id"] == result["operation_id"]
    assert host.receipt()["content_hash"] == result["entry"]["content_hash"]
    assert [op["mode"] for op in host.receipt()["local_operations"]] == ["index", "update"]
    assert host.receipt()["local_operations"][-1]["operation_id"] == result["operation_id"]


# --- the store's locked compare-and-advance --------------------------------------------

def _entry(host: Host, ref: str) -> dict:
    return host.workspace.prepare_entry(project_ref=PROJECT_REF, repository_journal_ref=ref)


def test_store_refuses_a_stale_update_and_a_moved_identity(host, repo):
    ref = _publish(host, repo, "alpha")
    a = _entry(host, ref)
    host.field.record_journal_receipt(PROJECT_ID, worker_name=WORKER, entry=a)
    _publish(host, repo, "alpha beta", f"amend\n\n{TRAILERS}")
    b = _entry(host, ref)
    with pytest.raises(DomainError) as caught:
        host.field.record_journal_receipt(PROJECT_ID, worker_name=WORKER, entry=b, mode="update",
                                          precondition={"kind": "hash", "previous_content_hash": "0" * 64})
    assert caught.value.code == "journal_update_stale"
    moved = {**b, "repository_journal_ref": "repo:journals/projects/project-one/journal/elsewhere.md"}
    with pytest.raises(DomainError) as caught:
        host.field.record_journal_receipt(PROJECT_ID, worker_name=WORKER, entry=moved, mode="update",
                                          precondition={"kind": "hash", "previous_content_hash": a["content_hash"]})
    assert caught.value.code == "field_journal_receipt_conflict"
    advanced = host.field.record_journal_receipt(
        PROJECT_ID, worker_name=WORKER, entry=b, mode="update",
        precondition={"kind": "hash", "previous_content_hash": a["content_hash"]})
    assert advanced["content_hash"] == b["content_hash"]
    with pytest.raises(DomainError) as caught:  # the older operation, finishing late
        host.field.record_journal_receipt(PROJECT_ID, worker_name=WORKER, entry=a, mode="update",
                                          precondition={"kind": "hash", "previous_content_hash": "f" * 64})
    assert caught.value.code == "journal_update_stale"
    assert host.receipt()["content_hash"] == b["content_hash"]


def test_two_first_initializers_end_with_one_receipt(host, repo):
    ref = _publish(host, repo, "alpha")
    entry = _entry(host, ref)
    first = host.field.record_journal_receipt(PROJECT_ID, worker_name=WORKER, entry=entry, mode="index",
                                              precondition={"kind": "absent"})
    second = host.field.record_journal_receipt(PROJECT_ID, worker_name=WORKER, entry=entry, mode="index",
                                               precondition={"kind": "absent"})
    assert first == second
    _publish(host, repo, "alpha from a stale clone? no: newer", f"amend\n\n{TRAILERS}")
    other = _entry(host, ref)
    with pytest.raises(DomainError) as caught:
        host.field.record_journal_receipt(PROJECT_ID, worker_name=WORKER, entry=other, mode="update",
                                          precondition={"kind": "absent"})
    assert caught.value.code == "journal_update_stale"


def test_a_hash_precondition_with_no_receipt_is_stale(host, repo):
    ref = _publish(host, repo, "alpha")
    with pytest.raises(DomainError) as caught:
        host.field.record_journal_receipt(PROJECT_ID, worker_name=WORKER, entry=_entry(host, ref), mode="update",
                                          precondition={"kind": "hash", "previous_content_hash": "a" * 64})
    assert caught.value.code == "journal_update_stale"


def test_a_missing_reference_explains_its_form(host):
    with pytest.raises(DomainError) as caught:
        cli._require_repository_journal_ref(host.workspace,
                                            SimpleNamespace(repository_journal_ref="", project_ref=PROJECT_REF))
    assert caught.value.code == "journal_repository_ref_required"
    assert caught.value.details["example"].startswith("repo:journals/projects/project-one")


# --- pb-knowledge's slice 2 review findings (reproduced at c0ab0087) ---------------------

def test_a_retry_of_the_same_publication_is_the_same_operation(host, repo):
    ref = _publish(host, repo, "alpha")
    first = host.index(ref)
    assert host.index(ref)["operation_id"] == first["operation_id"]  # unflagged first index
    _publish(host, repo, "alpha beta", f"amend\n\n{TRAILERS}")
    update = host.index(ref, update=True)
    assert host.index(ref, update=True)["operation_id"] == update["operation_id"]
    assert [op["mode"] for op in host.receipt()["local_operations"]] == ["index", "update"]


def test_restoring_old_bytes_without_publishing_cannot_roll_the_receipt_back(host, repo):
    ref = _publish(host, repo, "version A")
    host.index(ref)
    _publish(host, repo, "version B", f"amend\n\n{TRAILERS}")
    host.index(ref, update=True)
    b_hash = host.receipt()["content_hash"]
    path = Path(host.workspace.context(PROJECT_REF)["local_journal_directory"]) / "running-design-record.md"
    old = _git(repo, "show", f"HEAD~1:{path.resolve().relative_to(repo.resolve()).as_posix()}")
    path.write_text(old + "\n", encoding="utf-8")
    with pytest.raises(DomainError) as caught:
        host.index(ref, update=True)
    assert caught.value.code == "journal_entry_not_published"
    assert host.receipt()["content_hash"] == b_hash


def test_a_late_update_from_an_older_publication_is_stale_even_with_equal_bytes(host, repo):
    ref = _publish(host, repo, "version A")
    a1 = _entry(host, ref)
    a1_commit = _git(repo, "rev-parse", "HEAD")
    host.field.record_journal_receipt(PROJECT_ID, worker_name=WORKER, entry=a1, target_commit=a1_commit)
    _publish(host, repo, "version B", f"b\n\n{TRAILERS}")
    b = _entry(host, ref)
    b_commit = _git(repo, "rev-parse", "HEAD")
    host.field.record_journal_receipt(PROJECT_ID, worker_name=WORKER, entry=b, mode="update", target_commit=b_commit,
                                      precondition={"kind": "chain", "earlier": [
                                          {"commit": a1_commit, "content_hash": a1["content_hash"]}]})
    _publish(host, repo, "version A", f"a2\n\n{TRAILERS}")
    a2_commit = _git(repo, "rev-parse", "HEAD")
    host.field.record_journal_receipt(PROJECT_ID, worker_name=WORKER, entry=_entry(host, ref), mode="update",
                                      target_commit=a2_commit, precondition={"kind": "chain", "earlier": [
                                          {"commit": a1_commit, "content_hash": a1["content_hash"]},
                                          {"commit": b_commit, "content_hash": b["content_hash"]}]})
    # The late operation prepared when A1 was current, targeting B: its earlier list holds only A1.
    for late in ({"kind": "hash", "previous_content_hash": a1["content_hash"], "previous_commit": a1_commit},
                 {"kind": "chain", "earlier": [{"commit": a1_commit, "content_hash": a1["content_hash"]}]}):
        with pytest.raises(DomainError) as caught:
            host.field.record_journal_receipt(PROJECT_ID, worker_name=WORKER, entry=b, mode="update",
                                              target_commit=b_commit, precondition=late)
        assert caught.value.code == "journal_update_stale"
    assert host.receipt()["commit"] == a2_commit


def test_a_hash_precondition_also_checks_the_commit(host, repo):
    ref = _publish(host, repo, "version A")
    a1, a1_commit = _entry(host, ref), _git(repo, "rev-parse", "HEAD")
    host.field.record_journal_receipt(PROJECT_ID, worker_name=WORKER, entry=a1, target_commit=a1_commit)
    _publish(host, repo, "version B", f"b\n\n{TRAILERS}")
    b, b_commit = _entry(host, ref), _git(repo, "rev-parse", "HEAD")
    host.field.record_journal_receipt(PROJECT_ID, worker_name=WORKER, entry=b, mode="update", target_commit=b_commit,
                                      precondition={"kind": "hash", "previous_content_hash": a1["content_hash"],
                                                    "previous_commit": a1_commit})
    _publish(host, repo, "version A", f"a2\n\n{TRAILERS}")
    a2_commit = _git(repo, "rev-parse", "HEAD")
    host.field.record_journal_receipt(PROJECT_ID, worker_name=WORKER, entry=_entry(host, ref), mode="update",
                                      target_commit=a2_commit,
                                      precondition={"kind": "hash", "previous_content_hash": b["content_hash"],
                                                    "previous_commit": b_commit})
    with pytest.raises(DomainError) as caught:  # prepared from A1; A2 has the same bytes, another commit
        host.field.record_journal_receipt(PROJECT_ID, worker_name=WORKER, entry=b, mode="update",
                                          target_commit=b_commit,
                                          precondition={"kind": "hash", "previous_content_hash": a1["content_hash"],
                                                        "previous_commit": a1_commit})
    assert caught.value.code == "journal_update_stale"


def test_a_committed_revert_to_old_bytes_is_a_new_published_version(host, repo):
    ref = _publish(host, repo, "version A")
    host.index(ref)
    a_hash = host.receipt()["content_hash"]
    _publish(host, repo, "version B", f"amend\n\n{TRAILERS}")
    host.index(ref, update=True)
    _git(repo, "revert", "--no-edit", "HEAD")  # published: the old bytes at a new commit
    revert = _git(repo, "rev-parse", "HEAD")
    result = host.index(ref, update=True)
    receipt = host.receipt()
    assert (receipt["content_hash"], receipt["commit"]) == (a_hash, revert)
    assert [op["mode"] for op in receipt["local_operations"]] == ["index", "update", "update"]
    assert [w["code"] for w in result["warnings"]] == ["journal_update_intent_unrecorded"]

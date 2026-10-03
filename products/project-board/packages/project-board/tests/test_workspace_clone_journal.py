"""Each worker reads its project's journal and pages from its own workspace clone (W343).

On the maintainer host, 2026-09-26, `pb worker context` read the journal home, and with
it the setup, facts, environment and instructions pages, through the host's
repository map: one host-wide checkout that was 186 commits behind its
remote. Every agent there got no runtimes, no instructions ref and "no
project-setup.json", while each had a current clone in its own workspace.

Each worker now reads and writes project state only through
``<workspace>/<alias>``. Two workers on one host whose clones hold different
commits each see their own pages and their own journal, a host map that names
a stale checkout is never read, and a clone that is missing or behind is named
with the step that fixes it.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Timer, get_ident

import pytest

from project_board.client import cli, host_config, relay
from project_board.client.journals import JournalWorkspace
from project_board.client.journal_search import JournalSearchIndex
from project_board.client.io import content_hash, exclusive_lock
from project_board.client.project_setup import PROJECT_SETUP_FILE, PROJECT_SETUP_SCHEMA
from project_board.client.store import SharedFieldStore
from project_board.contract.worker_identity import WorkerSessionIdentity
from project_board.contract.errors import DomainError

PROJECT = "work:project:project-one"
HOME = "projects/project-one"
ENTRY = f"{HOME}/journal/2026.09.26.160000-decision.md"
BINDING = {
    "journal_binding": {
        "project_ref": PROJECT,
        "journal_home_ref": f"repo:journals/{HOME}",
        "project_artifact_ref": "",
        "revision": 1,
    }
}
GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(directory: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(directory), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **GIT_ENV},
    ).stdout.strip()


def publish(checkout: Path, runtime: str) -> str:
    """Commit one version of the project's pages and journal entry, and push it."""

    home = checkout / HOME
    (home / "journal").mkdir(parents=True, exist_ok=True)
    (home / PROJECT_SETUP_FILE).write_text(
        json.dumps(
            {
                "schema": PROJECT_SETUP_SCHEMA,
                "instructions_ref": f"repo:journals/{HOME}/project-instructions.md",
                "runtimes": [
                    {
                        "name": runtime,
                        "host": runtime,
                        "kind": "kdcube",
                        "actions": {
                            "refresh": {
                                "who": ["coordinator"],
                                "releases": [{"repository": "journals", "ref": "origin/main"}],
                            }
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (home / "project-instructions.md").write_text(f"# {runtime}\n", encoding="utf-8")
    (home / "project-facts.md").write_text(f"{runtime} facts\n", encoding="utf-8")
    (checkout / ENTRY).write_text(
        "---\n"
        "entry_ref: work:journal:20260926T160000Z:journal_0123456789abcdef:decision\n"
        f"project_ref: {PROJECT}\n"
        f"title: The {runtime} decision\n"
        "---\n\n"
        f"Decided on {runtime}.\n",
        encoding="utf-8",
    )
    git(checkout, "add", "-A")
    git(checkout, "commit", "--quiet", "-m", runtime)
    git(checkout, "push", "--quiet", "origin", "HEAD:main")
    return git(checkout, "rev-parse", "HEAD")


def clone(remote: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--quiet", str(remote), str(destination)],
        check=True,
        capture_output=True,
        env={**os.environ, **GIT_ENV},
    )


@pytest.fixture()
def host(tmp_path: Path) -> dict:
    """One host, a stale host checkout in its map, and three workers.

    ``behind`` cloned the journal repository at the first commit and fetched
    the second without merging it; ``current`` cloned at the second; ``fresh``
    has cloned nothing yet. The host map names a checkout holding a third,
    older content that no worker may ever read.
    """

    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--quiet", "--bare", "-b", "main", str(remote)],
        check=True,
        env={**os.environ, **GIT_ENV},
    )
    upstream = tmp_path / "upstream"
    clone(remote, upstream)
    git(upstream, "checkout", "--quiet", "-b", "main")
    publish(upstream, "host-checkout-runtime")
    stale = tmp_path / "host-checkout"
    clone(remote, stale)
    first = publish(upstream, "first-runtime")

    agents = tmp_path / "agents"
    behind = agents / "behind"
    clone(remote, behind / "journals")
    second = publish(upstream, "second-runtime")
    git(behind / "journals", "fetch", "--quiet", "origin")
    current = agents / "current"
    clone(remote, current / "journals")
    fresh = agents / "fresh"
    fresh.mkdir(parents=True)

    config = host_config.initialize_host_config(
        target_id="target",
        endpoint="https://runtime.example/mcp",
        tenant="tenant",
        platform_project="project",
        host_id="host-one",
        allowed_roots=[str(tmp_path)],
        source_repositories={"journals": str(stale)},
        config_path=tmp_path / "relay.json",
        state_root=tmp_path / "state",
    )
    field = SharedFieldStore(host_config.HostRelayConfig.load(config.path).field_root)
    workers = {}
    for number, (name, workspace) in enumerate(
        (("behind", behind), ("current", current), ("fresh", fresh)), start=1
    ):
        identity = WorkerSessionIdentity.create(
            "claude-code", f"{number:08d}-0000-4000-8000-000000000000"
        )
        channel = host_config.enroll_worker_channel(
            config.path,
            identity=identity,
            profile=f"problem-board-claude-{name}",
            authorized=True,
            working_directory=str(workspace),
        )
        field.register_worker(
            worker_name=identity.worker_name,
            runtime_kind="claude-code",
            capabilities=[],
            authority_label="authority:test",
        )
        workers[name] = {"identity": identity, "channel": channel, "workspace": workspace}
    loaded = host_config.HostRelayConfig.load(config.path)
    for worker in workers.values():
        # The relay channel of each worker binds the project journal, as on
        # the heartbeat that carries the binding.
        adapter = relay.ProblemBoardHostRelayAdapter(
            config=relay.RelayConfig.from_host_channel(
                loaded, worker["channel"], project_id="project-one"
            ),
            field=SharedFieldStore(loaded.field_root),
            client=object(),
        )
        adapter._reconcile_journal_binding(BINDING)  # noqa: SLF001 - the relay step under test
        worker["relay"] = adapter
    return {
        "config": loaded,
        "workers": workers,
        "stale": stale,
        "first": first,
        "second": second,
        "upstream": upstream,
    }


def context(host: dict, name: str) -> dict:
    identity = host["workers"][name]["identity"]
    args = cli.build_parser().parse_args(
        [
            "worker", "context",
            "--config", str(host["config"].path),
            "--runtime-kind", identity.runtime_kind,
            "--runtime-session-id", identity.runtime_session_id,
            "--project-ref", PROJECT,
        ]
    )
    return cli._worker_command(args)  # noqa: SLF001 - the command under test


def runtimes(result: dict) -> list[str]:
    return [runtime["name"] for runtime in result.get("runtimes") or []]


def test_two_workers_on_different_commits_each_read_their_own_pages(host):
    behind = context(host, "behind")
    current = context(host, "current")

    assert behind["journal_state"] == "available"
    assert runtimes(behind) == ["first-runtime"]
    assert behind["journal_home_commit"] == host["first"]
    assert Path(behind["local_project_facts"]).read_text() == "first-runtime facts\n"
    assert behind["project_instructions_ref"] == f"repo:journals/{HOME}/project-instructions.md"
    assert Path(behind["local_journal_home"]).is_relative_to(host["workers"]["behind"]["workspace"])

    assert current["journal_state"] == "available"
    assert runtimes(current) == ["second-runtime"]
    assert current["journal_home_commit"] == host["second"]
    assert Path(current["local_project_facts"]).read_text() == "second-runtime facts\n"
    assert Path(current["local_journal_home"]).is_relative_to(host["workers"]["current"]["workspace"])


def test_the_host_map_naming_a_stale_checkout_is_never_read(host):
    for name in ("behind", "current"):
        result = context(host, name)

        assert "host-checkout-runtime" not in runtimes(result)
        text = json.dumps(result)
        assert str(host["stale"]) not in text
        assert result["journal_home_read_root"] == ""


def test_a_clone_behind_its_remote_is_named_with_the_fetch_step(host):
    behind = context(host, "behind")
    current = context(host, "current")

    clone_state = behind["journal_clone"]
    assert clone_state["state"] == "behind"
    assert clone_state["alias"] == "journals"
    assert clone_state["path"] == str(host["workers"]["behind"]["workspace"].resolve() / "journals")
    assert clone_state["commit"] == host["first"]
    assert clone_state["compared_commit"] == host["second"]
    assert clone_state["behind"] == 1
    assert "project-workspace.md step 2" in clone_state["action"]
    assert "merge --ff-only" in clone_state["action"]
    assert clone_state["action"] in behind["project_setup_issues"]

    assert current["journal_clone"]["state"] == "current"
    assert current["journal_clone"]["action"] == ""
    assert not any("journals at" in issue for issue in current["project_setup_issues"])


def test_a_missing_clone_is_named_and_no_other_checkout_stands_in(host):
    fresh = context(host, "fresh")

    assert fresh["journal_state"] == "unavailable"
    assert fresh["journal_error_code"] == "journal_repository_root_missing"
    expected = str(host["workers"]["fresh"]["workspace"].resolve() / "journals")
    assert fresh["journal_error_details"]["path"] == expected
    assert fresh["journal_error_details"]["alias"] == "journals"
    assert "project-workspace.md step 2" in fresh["journal_error"]
    assert "runtimes" not in fresh
    assert str(host["stale"]) not in json.dumps(fresh)


def test_each_relay_serves_the_journal_of_its_own_clone(host):
    behind = host["workers"]["behind"]["relay"].journal_workspace
    current = host["workers"]["current"]["relay"].journal_workspace

    old = behind.read_for_project(project_ref=PROJECT, repository_journal_ref=f"repo:journals/{ENTRY}")
    new = current.read_for_project(project_ref=PROJECT, repository_journal_ref=f"repo:journals/{ENTRY}")

    assert "Decided on first-runtime." in old["content"]
    assert "Decided on second-runtime." in new["content"]
    assert [row["title"] for row in behind.search("decision", project_ref=PROJECT)] == ["The first-runtime decision"]
    assert [row["title"] for row in current.search("decision", project_ref=PROJECT)] == ["The second-runtime decision"]
    assert behind.index.path != current.index.path

    stamp = behind.clone_stamp(PROJECT)
    assert stamp["journal_home_commit"] == host["first"]
    assert stamp["journal_clone"]["state"] == "behind"
    assert current.clone_stamp(PROJECT)["journal_clone"]["state"] == "current"


@pytest.mark.parametrize("name,clone_state", [("behind", "behind"), ("current", "current")])
def test_journal_view_transfers_bounded_freshness_not_local_paths(host, name, clone_state):
    workspace = host["workers"][name]["relay"].journal_workspace
    result = relay.ProblemBoardHostRelayAdapter._journal_view_payload(
        workspace, {"query": "decision"}, PROJECT, "catalog", "journal.catalog",
    )
    assert result["source_commit"] == workspace.clone_stamp(PROJECT)["journal_home_commit"]
    status = result["snapshot_status"]
    assert status["schema"] == "problem-board.journal-snapshot-status.v1"
    assert status["local_freshness"] == "current_local_source"
    assert status["clone_state"] == clone_state
    assert status["origin_freshness"] == "last_fetched_ref_only"
    assert status["indexed_at"]
    assert "journal_clone" not in result
    assert str(host["workers"][name]["workspace"]) not in json.dumps(result)
    assert result["content_hash"] == content_hash({
        "entries": result["entries"], "next_cursor": result["next_cursor"],
        "source_commit": result["source_commit"], "snapshot_status": status,
    })


def test_journal_view_status_reduces_private_issue_details_to_counts():
    class Workspace:
        def view_catalog_page(self, **kwargs):
            return {"entries": [], "index_issues": [{"message": "/private/source SECRET"}],
                    "index_exclusions": [{"local_path": "/private/source", "body": "SECRET"}]}

        def clone_stamp(self, project_ref):
            return {"journal_home_commit": "a" * 40, "journal_clone": {
                "state": "behind", "compared_commit": "b" * 40, "behind": 2,
                "path": "/private/source", "action": "SECRET"}}

        def index_status(self):
            return {"state": "partial", "freshness": "unverified", "issue_count": 1,
                    "excluded_count": 1, "issues": [{"message": "SECRET"}],
                    "sources": {"private": {"path": "/private/source"}}}

    result = relay.ProblemBoardHostRelayAdapter._journal_view_payload(
        Workspace(), {}, PROJECT, "catalog", "journal.catalog",
    )
    assert result["snapshot_status"]["index_state"] == "partial"
    assert result["snapshot_status"]["issue_codes"] == ["index_issue"]
    assert result["snapshot_status"]["exclusion_codes"] == ["excluded_entry"]
    assert "SECRET" not in json.dumps(result)
    assert "/private/" not in json.dumps(result)


MERGED_ENTRY = "work:journal:20260930T143800Z:journal_20260930w407source:history-source-prs-and-final-verification"


def advance_journal(host):
    upstream = host["upstream"]
    (upstream / HOME / "journal" / "merged-verification.md").write_text(
        "---\n"
        f"entry_ref: {MERGED_ENTRY}\n"
        f"project_ref: {PROJECT}\n"
        "title: W407 source verification\n"
        "summary: Durable feature rationale and final verification\n"
        "status: in-progress\n"
        f"worker_name: {host['workers']['current']['identity'].worker_name}\n"
        "---\n\n"
        "W407 preserves legacy unknowns rather than inventing history.\n",
        encoding="utf-8",
    )
    git(upstream, "add", "-A")
    git(upstream, "commit", "--quiet", "-m", "Merge journal verification")
    git(upstream, "push", "--quiet", "origin", "HEAD:main")
    checkout = host["workers"]["current"]["workspace"] / "journals"
    git(checkout, "fetch", "--quiet", "origin")
    git(checkout, "merge", "--ff-only", "origin/main")
    return git(checkout, "rev-parse", "HEAD")


def test_cli_search_automatically_finds_merged_entry_after_clone_advance(host):
    workspace = host["workers"]["current"]["relay"].journal_workspace
    assert workspace.search("W407", project_ref=PROJECT) == []
    head = advance_journal(host)
    identity = host["workers"]["current"]["identity"]
    args = cli.build_parser().parse_args([
        "worker", "journal-search", "--config", str(host["config"].path),
        "--runtime-kind", identity.runtime_kind,
        "--runtime-session-id", identity.runtime_session_id,
        "--project-ref", PROJECT, "--query", "legacy unknowns",
    ])

    result = cli._worker_command(args)

    assert [row["entry_ref"] for row in result["entries"]] == [MERGED_ENTRY]
    assert result["index"]["sources"][PROJECT]["commit"] == head
    assert result["index"]["freshness"] == "current_local_source"


def test_heartbeat_refreshes_content_with_unchanged_binding(host):
    adapter = host["workers"]["current"]["relay"]
    workspace = adapter.journal_workspace
    assert workspace.index.inspect(MERGED_ENTRY)["state"] == "entry_absent"
    advance_journal(host)

    result = adapter._reconcile_journal_binding(BINDING)

    assert result["changed"] is False
    assert workspace.index.inspect(MERGED_ENTRY)["state"] == "indexed"
    assert result["indexed_entries"] == 2


def test_restart_does_not_trust_existing_ready_index(host):
    old = host["workers"]["current"]["relay"].journal_workspace
    advance_journal(host)
    restarted = JournalWorkspace(old.root, old.repositories)

    assert restarted.index_status()["state"] == "stale"
    assert [row["entry_ref"] for row in restarted.search("legacy unknowns", project_ref=PROJECT)] == [MERGED_ENTRY]
    assert restarted.index_status()["freshness"] == "current_local_source"


def test_fresh_worker_search_reconstructs_from_its_own_clone(host):
    advance_journal(host)
    worker = host["workers"]["fresh"]
    clone(host["upstream"].parent / "remote.git", worker["workspace"] / "journals")
    workspace = worker["relay"].journal_workspace

    assert [row["entry_ref"] for row in workspace.search("legacy unknowns", project_ref=PROJECT)] == [MERGED_ENTRY]
    assert str(host["stale"]) not in json.dumps(workspace.index_status())
    assert all(Path(row["source_path"]).is_relative_to(worker["workspace"])
               for row in workspace.search("W407", project_ref=PROJECT))


def test_unchanged_search_and_heartbeat_do_not_reread_bodies(host, monkeypatch):
    adapter = host["workers"]["current"]["relay"]
    workspace = adapter.journal_workspace
    stamp = workspace.index_status()["recorded_at"]

    def unexpected_scan():
        raise AssertionError("unchanged generation must not reread journal bodies")

    monkeypatch.setattr(workspace, "_scan_documents", unexpected_scan)
    assert len(workspace.search("decision", project_ref=PROJECT)) == 1
    assert adapter._reconcile_journal_binding(BINDING)["indexed_entries"] is None
    assert workspace.index_status()["recorded_at"] == stamp


def test_local_edit_and_removal_refresh_without_a_commit(host):
    workspace = host["workers"]["current"]["relay"].journal_workspace
    advance_journal(host)
    assert workspace.search("W407", project_ref=PROJECT)
    entry = host["workers"]["current"]["workspace"] / "journals" / HOME / "journal" / "merged-verification.md"
    entry.write_text(entry.read_text().replace("legacy unknowns", "bounded provenance"), encoding="utf-8")
    assert workspace.search("legacy unknowns", project_ref=PROJECT) == []
    assert workspace.search("bounded provenance", project_ref=PROJECT)
    entry.unlink()
    assert workspace.search("W407", project_ref=PROJECT) == []
    assert workspace.index.inspect(MERGED_ENTRY)["state"] == "entry_absent"


def test_offline_clone_refuses_search_and_preserves_previous_index(host):
    workspace = host["workers"]["current"]["relay"].journal_workspace
    checkout = host["workers"]["current"]["workspace"] / "journals"
    checkout.rename(checkout.with_name("temporarily-unavailable"))

    with pytest.raises(DomainError) as caught:
        workspace.search("decision", project_ref=PROJECT)

    assert caught.value.code == "journal_repository_root_missing"
    assert workspace.index_status()["state"] == "stale"
    assert workspace.index_status()["freshness"] == "unverified"
    assert workspace.index.inspect("work:journal:20260926T160000Z:journal_0123456789abcdef:decision")["state"] == "indexed"


def test_refresh_failure_is_explicit_private_and_retryable(host, monkeypatch):
    adapter = host["workers"]["current"]["relay"]
    workspace = adapter.journal_workspace
    advance_journal(host)
    sync = workspace.index.sync

    def fail(_documents):
        raise RuntimeError("sensitive journal body must not appear in a diagnostic")

    monkeypatch.setattr(workspace.index, "sync", fail)
    with pytest.raises(DomainError) as caught:
        workspace.search("W407", project_ref=PROJECT)
    assert caught.value.code == "journal_index_refresh_failed"
    assert "sensitive journal body" not in json.dumps(caught.value.to_dict())
    assert workspace.index_status()["state"] == "stale"
    assert workspace.index.inspect(MERGED_ENTRY)["state"] == "entry_absent"
    assert adapter._reconcile_journal_binding(BINDING)["state"] == "index_unavailable"
    monkeypatch.setattr(workspace.index, "sync", sync)
    assert [row["entry_ref"] for row in workspace.search("W407", project_ref=PROJECT)] == [MERGED_ENTRY]


def test_source_change_during_scan_cannot_be_certified(host, monkeypatch):
    workspace = host["workers"]["current"]["relay"].journal_workspace
    advance_journal(host)
    scan = workspace._scan_documents
    entry = host["workers"]["current"]["workspace"] / "journals" / ENTRY

    def moving_source():
        result = scan()
        entry.write_text(entry.read_text() + "\nA concurrent change.\n", encoding="utf-8")
        return result

    monkeypatch.setattr(workspace, "_scan_documents", moving_source)
    with pytest.raises(DomainError) as caught:
        workspace.search("W407", project_ref=PROJECT)
    assert caught.value.code == "journal_source_changed"
    assert workspace.index_status()["state"] == "stale"
    monkeypatch.setattr(workspace, "_scan_documents", scan)
    assert workspace.search("W407", project_ref=PROJECT)


def test_skipped_invalid_entry_has_explicit_partial_status(host):
    workspace = host["workers"]["current"]["relay"].journal_workspace
    entry = host["workers"]["current"]["workspace"] / "journals" / HOME / "journal" / "wrong-project.md"
    entry.write_text("---\nproject_ref: work:project:other-project\n---\n\nWrong project body.\n", encoding="utf-8")

    assert workspace.search("decision", project_ref=PROJECT)
    # Ranking may include recency hits; the invalid entry itself and its body
    # must never be among the results.
    rows = workspace.search("Wrong project body", project_ref=PROJECT)
    assert all(not row["repository_journal_ref"].endswith("wrong-project.md") for row in rows)
    assert "Wrong project body" not in json.dumps(rows)
    status = workspace.index_status()
    assert status["state"] == "partial"
    assert status["complete"] is False
    assert any(issue["code"] == "journal_entry_project_mismatch" for issue in status["issues"])


def test_journal_symlink_cannot_index_another_workers_content(host):
    workspace = host["workers"]["current"]["relay"].journal_workspace
    journal = host["workers"]["current"]["workspace"] / "journals" / HOME / "journal"
    (journal / "foreign.md").symlink_to(host["stale"] / ENTRY)

    with pytest.raises(DomainError) as caught:
        workspace.search("host-checkout-runtime", project_ref=PROJECT)
    assert caught.value.code == "journal_repository_ref_escape"
    assert workspace.index_status()["state"] == "stale"


def test_concurrent_cli_and_relay_refresh_share_one_generation(host, monkeypatch):
    workspace = host["workers"]["current"]["relay"].journal_workspace
    advance_journal(host)
    second = JournalWorkspace(workspace.root, workspace.repositories)
    original = JournalSearchIndex.sync
    calls = []

    def counted_sync(self, documents):
        calls.append(self.path)
        return original(self, documents)

    monkeypatch.setattr(JournalSearchIndex, "sync", counted_sync)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(workspace.search, "W407", project_ref=PROJECT)
        other = pool.submit(second.search, "W407", project_ref=PROJECT)
        assert [row["entry_ref"] for row in first.result(timeout=10)] == [MERGED_ENTRY]
        assert [row["entry_ref"] for row in other.result(timeout=10)] == [MERGED_ENTRY]
    assert len(calls) == 1


def test_directory_read_error_is_not_an_empty_success(host, monkeypatch):
    workspace = host["workers"]["current"]["relay"].journal_workspace

    def denied_walk(path, *, followlinks, onerror):
        onerror(PermissionError("private details"))
        return iter(())

    monkeypatch.setattr("project_board.client.journals.os.walk", denied_walk)
    with pytest.raises(DomainError) as caught:
        workspace.search("decision", project_ref=PROJECT)
    assert caught.value.code == "journal_index_refresh_failed"
    assert caught.value.details["error_type"] == "PermissionError"
    assert "private details" not in json.dumps(caught.value.to_dict())
    assert workspace.index_status()["state"] == "stale"


def test_interrupted_refresh_is_reconciled_after_restart(host, monkeypatch):
    workspace = host["workers"]["current"]["relay"].journal_workspace

    def interrupted(_documents):
        raise KeyboardInterrupt("simulate process interruption")

    monkeypatch.setattr(workspace.index, "sync", interrupted)
    with pytest.raises(KeyboardInterrupt):
        workspace.rebuild_index()
    assert workspace.index_status()["state"] == "indexing"
    assert workspace.index_status()["freshness"] == "unverified"
    restarted = JournalWorkspace(workspace.root, workspace.repositories)
    assert restarted.search("decision", project_ref=PROJECT)
    assert restarted.index_status()["freshness"] == "current_local_source"


async def wait_for_event(event: Event) -> None:
    deadline = time.monotonic() + 3
    while not event.is_set():
        assert time.monotonic() < deadline, "background job did not reach its fence"
        await asyncio.sleep(0.005)


def heartbeat_adapter(host, monkeypatch):
    """Exercise the actual heartbeat call site, not just the new worker helper."""
    adapter = host["workers"]["current"]["relay"]

    class Client:
        async def action(self, **_kwargs):
            return {"object": {**BINDING, "attendance": "linked"}}

    adapter.client = Client()

    async def nothing(*_args, **_kwargs):
        return {}

    for method in ("_add_runtime_account", "_add_disk_usage", "_pull_controls", "_flush_outbox_unlocked"):
        monkeypatch.setattr(adapter, method, nothing)
    for method in ("_session_report_delta", "_assignment_files_delta", "_store_reads_delta"):
        monkeypatch.setattr(adapter, method, lambda **_kwargs: (None, "unchanged"))
    for method in (
        "_record_project_heartbeat", "_record_session_report",
        "_record_attendance_observation", "_materialize_attended_project",
        "_report_dead_notification_path",
    ):
        monkeypatch.setattr(adapter, method, lambda *_args, **_kwargs: None)
    monkeypatch.setattr(adapter, "_reconcile_assignments", lambda *_args: (0, []))

    return adapter


def test_heartbeat_held_index_lock_keeps_peer_loop_responsive(host, monkeypatch):
    adapter = heartbeat_adapter(host, monkeypatch)
    locked, release = Event(), Event()

    def hold():
        with exclusive_lock(adapter.journal_workspace.control / "locks" / "index.lock"):
            locked.set()
            assert release.wait(3)

    async def scenario():
        await wait_for_event(locked)
        started = time.monotonic()

        async def peer():
            await asyncio.sleep(0.05)
            return time.monotonic() - started

        peer_task = asyncio.create_task(peer())
        # Rescue the rejected synchronous head without hanging the test runner.
        rescue = Timer(0.8, release.set)
        rescue.start()
        try:
            result = await adapter._poll_project_once(agent_sessions=[], force_heartbeat=True)
            elapsed = time.monotonic() - started
            peer_elapsed = await peer_task
            assert elapsed < 0.3, f"heartbeat blocked for {elapsed:.3f}s on index.lock"
            assert peer_elapsed < 0.3, f"peer loop blocked for {peer_elapsed:.3f}s"
            assert result["journal_workspace"]["state"] == "refresh_pending"
        finally:
            release.set()
            rescue.cancel()
            # The same regression also runs read-only against pre-lifecycle
            # source, where this close hook does not exist yet.
            close = getattr(adapter, "aclose", None)
            if close is not None:
                await close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(hold)
        asyncio.run(scenario())
        holder.result(timeout=3)


def test_slow_heartbeat_freshness_is_single_flight_across_child_adapters(host, monkeypatch):
    adapter = heartbeat_adapter(host, monkeypatch)
    entered, release = Event(), Event()
    calls = []
    original = JournalWorkspace._source_stamps

    def slow(workspace):
        calls.append(workspace.root)
        entered.set()
        assert release.wait(3)
        return original(workspace)

    monkeypatch.setattr(JournalWorkspace, "_source_stamps", slow)

    async def scenario():
        try:
            started = time.monotonic()
            result = await adapter._poll_project_once(agent_sessions=[], force_heartbeat=True)
            assert time.monotonic() - started < 0.3
            assert result["journal_workspace"]["state"] == "refresh_pending"
            await wait_for_event(entered)
            future, pool = adapter._journal_refresh_worker._future, adapter._journal_refresh_worker._pool
            for _ in range(30):
                child = relay.ProblemBoardHostRelayAdapter(
                    config=adapter.config, field=adapter.field, client=adapter.client,
                    journal_refresh_worker=adapter._journal_refresh_worker,
                )
                assert (await child._reconcile_journal_binding_background(BINDING))["state"] == "refresh_pending"
                await asyncio.sleep(0)
            assert adapter._journal_refresh_worker._future is future
            assert adapter._journal_refresh_worker._pool is pool
            assert len(calls) == 1
            release.set()
            await asyncio.wait_for(asyncio.wrap_future(future), timeout=3)
            assert (await adapter._reconcile_journal_binding_background(BINDING))["state"] == "bound"
        finally:
            release.set()
            close = getattr(adapter, "aclose", None)
            if close is not None:
                await close()

    asyncio.run(scenario())


def test_held_journal_refresh_does_not_hold_channel_store_executor(host, monkeypatch):
    """The composed heartbeat keeps W461 writes off-loop without using their queue for Git."""
    adapter = heartbeat_adapter(host, monkeypatch)
    entered, release = Event(), Event()
    source_stamps = JournalWorkspace._source_stamps
    record = adapter._record_project_heartbeat_result
    poll = adapter._journal_refresh_worker.poll
    loop_thread = get_ident()
    recorded = []

    def slow(workspace):
        entered.set()
        assert release.wait(3)
        return source_stamps(workspace)

    def record_off_loop(**kwargs):
        recorded.append(get_ident())
        assert get_ident() != loop_thread
        return record(**kwargs)

    def poll_on_loop(*args, **kwargs):
        assert get_ident() == loop_thread
        return poll(*args, **kwargs)

    monkeypatch.setattr(JournalWorkspace, "_source_stamps", slow)
    monkeypatch.setattr(adapter, "_record_project_heartbeat_result", record_off_loop)
    monkeypatch.setattr(adapter._journal_refresh_worker, "poll", poll_on_loop)

    async def scenario():
        rescue = Timer(0.8, release.set)
        rescue.start()
        try:
            started = time.monotonic()
            result = await adapter._poll_project_once(agent_sessions=[], force_heartbeat=True)
            assert time.monotonic() - started < 0.3
            assert result["journal_workspace"]["state"] == "refresh_pending"
            await wait_for_event(entered)
            assert not release.is_set()
            thread = await asyncio.wait_for(
                relay.run_off_loop(get_ident, executor=adapter._store_thread()), timeout=0.3
            )
            assert recorded == [thread] and thread != loop_thread
            assert not release.is_set(), "the store probe must finish while journal work is held"
        finally:
            release.set()
            rescue.cancel()
            await adapter.aclose()

    with ThreadPoolExecutor(max_workers=1) as store:
        adapter._store_executor = store
        asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["bound", "error"])
def test_held_store_executor_does_not_block_journal_poll_or_view_lock(host, monkeypatch, outcome):
    """Polling/views remain loop-owned while completed incident effects wait for field I/O."""
    adapter = host["workers"]["current"]["relay"]
    published = capture_journal_view(adapter)
    worker = adapter._journal_refresh_worker
    held, release, polled, applied = Event(), Event(), Event(), Event()
    poll = worker.poll
    report_incident = adapter._report_journal_incident
    loop_thread = get_ident()

    def hold_store():
        held.set()
        assert release.wait(3)

    def poll_on_loop(*args, **kwargs):
        assert get_ident() == loop_thread
        ready, result = poll(*args, **kwargs)
        if ready:
            polled.set()
            if outcome == "error":
                result = DomainError("journal_index_refresh_failed", "LOCAL refresh failed.")
        return ready, result

    def report_off_loop(*args, **kwargs):
        assert get_ident() != loop_thread
        applied.set()
        return report_incident(*args, **kwargs)

    monkeypatch.setattr(worker, "poll", poll_on_loop)
    monkeypatch.setattr(adapter, "_report_journal_incident", report_off_loop)

    async def scenario(store):
        consume = None
        try:
            assert (await adapter._reconcile_journal_binding_background(BINDING))["state"] == "refresh_pending"
            await asyncio.wait_for(asyncio.wrap_future(worker._future), timeout=3)
            holder = store.submit(hold_store)
            await wait_for_event(held)
            consume = asyncio.create_task(adapter._reconcile_journal_binding_background(BINDING))
            await wait_for_event(polled)
            assert not consume.done() and not applied.is_set()
            # An actual requested view can take and release its asyncio lock
            # while the completed refresh's incident write is still queued.
            await asyncio.wait_for(adapter._serve_journal_view(journal_view_control()), timeout=0.5)
            assert len(published) == 1 and not worker._view_lock.locked()
            assert not release.is_set() and not applied.is_set()
            release.set()
            result = await asyncio.wait_for(consume, timeout=3)
            assert result["state"] == ("bound" if outcome == "bound" else "index_unavailable")
            assert applied.is_set()
            holder.result(timeout=3)
        finally:
            release.set()
            if consume is not None:
                await asyncio.gather(consume, return_exceptions=True)
            await adapter.aclose()

    with ThreadPoolExecutor(max_workers=1) as store:
        adapter._store_executor = store
        asyncio.run(scenario(store))


def journal_view_control(*, query="decision", mode="catalog"):
    payload = {
        "view_ref": "work:journal_view:20260927T214000Z:view_0123456789abcdef0123456789abcdef:journal",
        "mode": mode, "query": query,
        "repository_journal_ref": f"repo:journals/{ENTRY}",
    }
    return {"project_ref": PROJECT, "kind": "journal.catalog" if mode == "catalog" else "journal.read",
            "payload": payload, "payload_hash": content_hash(payload)}


def capture_journal_view(adapter):
    published = []

    class Client:
        async def action(self, **kwargs):
            published.append(kwargs)
            return {}

    adapter.client = Client()
    return published


def test_requested_journal_view_held_index_lock_keeps_peer_loop_responsive(host):
    """Actual async view handler must not block peers behind refresh/CLI locks."""
    adapter = host["workers"]["current"]["relay"]
    published = capture_journal_view(adapter)
    locked, release = Event(), Event()

    def hold():
        with exclusive_lock(adapter.journal_workspace.control / "locks" / "index.lock"):
            locked.set()
            assert release.wait(3)

    async def scenario():
        await wait_for_event(locked)
        started = time.monotonic()

        async def peer():
            await asyncio.sleep(0.05)
            return time.monotonic() - started

        peer_task = asyncio.create_task(peer())
        rescue = Timer(0.8, release.set)
        rescue.start()
        try:
            await adapter._serve_journal_view(journal_view_control())
            elapsed = await peer_task
            assert elapsed < 0.3, f"requested view blocked peer loop for {elapsed:.3f}s"
            assert len(published) == 1 and published[0]["action"] == "journal.view.publish"
            assert published[0]["payload"]["entries"]
        finally:
            release.set()
            rescue.cancel()
            await adapter.aclose()

    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(hold)
        try:
            asyncio.run(scenario())
        finally:
            release.set()
        holder.result(timeout=3)


@pytest.mark.parametrize("cancel_request", [True, False])
def test_requested_journal_view_lock_cancellation_drains_without_publish(host, cancel_request):
    adapter = host["workers"]["current"]["relay"]
    published = capture_journal_view(adapter)
    locked, release = Event(), Event()

    def hold():
        with exclusive_lock(adapter.journal_workspace.control / "locks" / "index.lock"):
            locked.set()
            assert release.wait(3)

    async def scenario():
        await wait_for_event(locked)
        request = asyncio.create_task(adapter._serve_journal_view(journal_view_control()))
        await asyncio.sleep(0.05)
        assert adapter._journal_refresh_worker._view_future is not None
        started = time.monotonic()
        if cancel_request:
            request.cancel()
        else:
            await asyncio.wait_for(adapter.aclose(), timeout=0.5)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(request, timeout=0.5)
        assert time.monotonic() - started < 0.3
        assert not published
        assert adapter._journal_refresh_worker._view_future is None
        release.set()
        await asyncio.sleep(0.03)
        assert not published
        await adapter.aclose()
        assert adapter._journal_refresh_worker._pool is None

    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(hold)
        try:
            asyncio.run(scenario())
        finally:
            release.set()
        holder.result(timeout=3)


def test_requested_journal_view_budget_reports_failure_without_private_details(host):
    adapter = host["workers"]["current"]["relay"]
    adapter._journal_refresh_worker = relay._JournalRefreshWorker(timeout_seconds=0.1)
    published = capture_journal_view(adapter)
    locked, release = Event(), Event()

    def hold():
        with exclusive_lock(adapter.journal_workspace.control / "locks" / "index.lock"):
            locked.set()
            assert release.wait(3)

    async def scenario():
        await wait_for_event(locked)
        try:
            with pytest.raises(DomainError) as caught:
                await asyncio.wait_for(adapter._serve_journal_view(journal_view_control()), timeout=0.5)
            assert caught.value.code == "journal_index_refresh_failed"
            assert caught.value.details == {"error_type": "TimeoutError"}
            assert not published
        finally:
            release.set()
            await adapter.aclose()

    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(hold)
        try:
            asyncio.run(scenario())
        finally:
            release.set()
        holder.result(timeout=3)


def test_requested_journal_view_shares_single_flight_with_heartbeat(host, monkeypatch):
    adapter = host["workers"]["current"]["relay"]
    published = capture_journal_view(adapter)
    entered, release = Event(), Event()
    source_stamps = JournalWorkspace._source_stamps

    def slow(workspace):
        entered.set()
        assert release.wait(3)
        return source_stamps(workspace)

    monkeypatch.setattr(JournalWorkspace, "_source_stamps", slow)

    async def scenario():
        worker = adapter._journal_refresh_worker
        try:
            await adapter._reconcile_journal_binding_background(BINDING)
            await wait_for_event(entered)
            refresh, pool = worker._future, worker._pool
            request = asyncio.create_task(adapter._serve_journal_view(journal_view_control()))
            await asyncio.sleep(0.02)
            assert worker._view_active and worker._view_future is None
            queued = asyncio.create_task(adapter._serve_journal_view(journal_view_control()))
            await asyncio.sleep(0.02)
            queued.cancel()
            with pytest.raises(asyncio.CancelledError):
                await queued
            for _ in range(20):
                assert (await adapter._reconcile_journal_binding_background(BINDING))["state"] == "refresh_pending"
                assert worker._future is refresh and worker._view_future is None and worker._pool is pool
                await asyncio.sleep(0)
            release.set()
            await asyncio.wait_for(request, timeout=2)
            assert len(published) == 1
            assert worker._pool is pool and worker._view_future is None
            assert (await adapter._reconcile_journal_binding_background(BINDING))["state"] == "bound"
        finally:
            release.set()
            await adapter.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["catalog", "document"])
def test_requested_journal_view_slow_read_cancelled_shutdown_drains(host, monkeypatch, mode):
    adapter = host["workers"]["current"]["relay"]
    published = capture_journal_view(adapter)
    entered, release = Event(), Event()
    method = "view_catalog_page" if mode == "catalog" else "read_for_project"
    original = getattr(JournalWorkspace, method)

    def slow(workspace, **kwargs):
        result = original(workspace, **kwargs)
        entered.set()
        assert release.wait(3)
        return result

    monkeypatch.setattr(JournalWorkspace, method, slow)

    async def scenario():
        request = asyncio.create_task(adapter._serve_journal_view(journal_view_control(query="", mode=mode)))
        await wait_for_event(entered)
        future = adapter._journal_refresh_worker._view_future
        request.cancel()
        await asyncio.sleep(0.02)
        request.cancel()
        closing = asyncio.create_task(adapter.aclose())
        await asyncio.sleep(0.02)
        closing.cancel()
        await asyncio.sleep(0.02)
        assert not request.done() and not closing.done()
        assert adapter._journal_refresh_worker._view_future is future
        assert not future.done() and not published
        release.set()
        for task in (request, closing):
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)
        assert adapter._journal_refresh_worker._view_future is None
        assert adapter._journal_refresh_worker._pool is None
        assert not published
        with pytest.raises(asyncio.CancelledError):
            await adapter._serve_journal_view(journal_view_control())
        assert adapter._journal_refresh_worker._pool is None

    try:
        asyncio.run(scenario())
    finally:
        release.set()


@pytest.mark.parametrize("lock_name", ["index.lock", "catalog.lock"])
def test_channel_shutdown_cancels_held_lock_without_late_mutation(host, lock_name):
    adapter = host["workers"]["current"]["relay"]
    workspace = adapter.journal_workspace
    locked, release = Event(), Event()

    def hold():
        with exclusive_lock(workspace.control / "locks" / lock_name):
            locked.set()
            assert release.wait(3)

    async def scenario():
        await wait_for_event(locked)
        assert (await adapter._reconcile_journal_binding_background(BINDING))["state"] == "refresh_pending"
        await asyncio.sleep(0.05)
        started = time.monotonic()
        await asyncio.wait_for(adapter.aclose(), timeout=0.5)
        assert time.monotonic() - started < 0.3
        assert adapter._journal_refresh_worker._future is None
        assert adapter._journal_refresh_worker._pool is None
        before = (workspace.catalog_path.read_bytes(), workspace.index_status_path.read_bytes())
        release.set()
        await asyncio.sleep(0.05)
        assert before == (workspace.catalog_path.read_bytes(), workspace.index_status_path.read_bytes())
        assert (await adapter._reconcile_journal_binding_background(BINDING))["state"] == "refresh_pending"
        assert adapter._journal_refresh_worker._pool is None

    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(hold)
        try:
            asyncio.run(scenario())
        finally:
            release.set()
        holder.result(timeout=3)


def test_cancelled_shutdown_drains_slow_scan_without_certifying_late_result(host, monkeypatch):
    adapter = host["workers"]["current"]["relay"]
    workspace = adapter.journal_workspace
    advance_journal(host)
    entered, release = Event(), Event()
    scan = JournalWorkspace._scan_documents

    def slow(current):
        result = scan(current)
        entered.set()
        assert release.wait(3)
        return result

    monkeypatch.setattr(JournalWorkspace, "_scan_documents", slow)

    async def scenario():
        await adapter._reconcile_journal_binding_background(BINDING)
        await wait_for_event(entered)
        closing = asyncio.create_task(adapter.aclose())
        await asyncio.sleep(0.02)
        closing.cancel()
        await asyncio.sleep(0.02)
        assert not closing.done(), "cancelling shutdown must not orphan the running job"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(closing, timeout=1)
        assert adapter._journal_refresh_worker._future is None
        assert adapter._journal_refresh_worker._pool is None
        assert workspace.index.inspect(MERGED_ENTRY)["state"] == "entry_absent"
        assert workspace.index_status()["freshness"] == "unverified"
        receipt = workspace.index_status_path.read_bytes()
        await asyncio.sleep(0.05)
        assert workspace.index_status_path.read_bytes() == receipt

    try:
        asyncio.run(scenario())
    finally:
        release.set()


def test_background_refresh_budget_refuses_held_lock_and_can_retry(host):
    adapter = host["workers"]["current"]["relay"]
    adapter._journal_refresh_worker = relay._JournalRefreshWorker(timeout_seconds=0.1)
    workspace = adapter.journal_workspace
    locked, release = Event(), Event()

    def hold():
        with exclusive_lock(workspace.control / "locks" / "index.lock"):
            locked.set()
            assert release.wait(3)

    async def scenario():
        await wait_for_event(locked)
        await adapter._reconcile_journal_binding_background(BINDING)
        future = adapter._journal_refresh_worker._future
        await asyncio.wait_for(asyncio.wrap_future(future), timeout=0.5)
        result = await adapter._reconcile_journal_binding_background(BINDING)
        assert result["state"] == "index_unavailable"
        assert result["error_code"] == "journal_index_refresh_failed"
        assert "TimeoutError" not in result["error_summary"]
        release.set()
        adapter._journal_refresh_worker.timeout_seconds = 3
        await adapter._reconcile_journal_binding_background(BINDING)
        await asyncio.wait_for(asyncio.wrap_future(adapter._journal_refresh_worker._future), timeout=3)
        assert (await adapter._reconcile_journal_binding_background(BINDING))["state"] == "bound"
        await adapter.aclose()

    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(hold)
        try:
            asyncio.run(scenario())
        finally:
            release.set()
        holder.result(timeout=3)


def test_superseded_binding_cancels_late_scan_before_index_publication(host, monkeypatch):
    adapter = host["workers"]["current"]["relay"]
    workspace = adapter.journal_workspace
    advance_journal(host)
    entered, release = Event(), Event()
    scan = JournalWorkspace._scan_documents

    def slow(current):
        result = scan(current)
        entered.set()
        assert release.wait(3)
        return result

    monkeypatch.setattr(JournalWorkspace, "_scan_documents", slow)
    newer = {"journal_binding": {**BINDING["journal_binding"], "revision": 2}}

    async def scenario():
        try:
            await adapter._reconcile_journal_binding_background(BINDING)
            await wait_for_event(entered)
            old = adapter._journal_refresh_worker._future
            assert (await adapter._reconcile_journal_binding_background(newer))["state"] == "refresh_pending"
            assert adapter._journal_refresh_worker._future is old
            release.set()
            assert await asyncio.wait_for(asyncio.wrap_future(old), timeout=3) is None
            assert workspace.index.inspect(MERGED_ENTRY)["state"] == "entry_absent"
            assert workspace.index_status()["freshness"] == "unverified"
            await adapter._reconcile_journal_binding_background(newer)
            await asyncio.wait_for(asyncio.wrap_future(adapter._journal_refresh_worker._future), timeout=3)
            result = await adapter._reconcile_journal_binding_background(newer)
            assert result["state"] == "bound" and result["revision"] == 2
            assert workspace.index.inspect(MERGED_ENTRY)["state"] == "indexed"
        finally:
            release.set()
            await adapter.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["private_failure", "source_race", "partial"])
def test_background_refresh_keeps_failure_source_and_partial_semantics(host, monkeypatch, failure):
    adapter = host["workers"]["current"]["relay"]
    workspace = adapter.journal_workspace
    advance_journal(host)
    scan = JournalWorkspace._scan_documents

    if failure == "private_failure":
        def fail(_index, _documents, **_kwargs):
            raise RuntimeError("private body and machine path must not be reported")
        monkeypatch.setattr(JournalSearchIndex, "sync", fail)
    elif failure == "source_race":
        def moving(current):
            result = scan(current)
            entry = host["workers"]["current"]["workspace"] / "journals" / ENTRY
            entry.write_text(entry.read_text() + "\nConcurrent edit.\n", encoding="utf-8")
            return result
        monkeypatch.setattr(JournalWorkspace, "_scan_documents", moving)
    else:
        entry = host["workers"]["current"]["workspace"] / "journals" / HOME / "journal" / "wrong-project.md"
        entry.write_text("---\nproject_ref: work:project:other\n---\n\nExcluded body.\n", encoding="utf-8")

    async def scenario():
        try:
            await adapter._reconcile_journal_binding_background(BINDING)
            await asyncio.wait_for(asyncio.wrap_future(adapter._journal_refresh_worker._future), timeout=3)
            result = await adapter._reconcile_journal_binding_background(BINDING)
            status = workspace.index_status()
            if failure == "partial":
                assert result["state"] == "bound"
                assert status["state"] == "partial" and status["complete"] is False
                assert workspace.index.inspect(MERGED_ENTRY)["state"] == "indexed"
            else:
                assert result["state"] == "index_unavailable"
                assert result["error_code"] == (
                    "journal_source_changed" if failure == "source_race" else "journal_index_refresh_failed"
                )
                assert status["state"] == "stale" and status["freshness"] == "unverified"
                assert workspace.index.inspect(MERGED_ENTRY)["state"] == "entry_absent"
            assert "private body" not in json.dumps(result)
            assert "private body" not in json.dumps(status)
        finally:
            await adapter.aclose()

    asyncio.run(scenario())


def test_automatic_refresh_preserves_author_and_status_filters(host):
    workspace = host["workers"]["current"]["relay"].journal_workspace
    advance_journal(host)
    assert workspace.search("W407", project_ref=PROJECT, status="closed") == []
    assert workspace.search("W407", project_ref=PROJECT, worker_name="codex-another-worker") == []
    author = host["workers"]["current"]["identity"].worker_name
    assert [row["entry_ref"] for row in workspace.search(
        "W407", project_ref=PROJECT, status="in-progress", worker_name=author
    )] == [MERGED_ENTRY]
    assert workspace.index_status()["freshness"] == "current_local_source"

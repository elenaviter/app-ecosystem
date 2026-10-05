"""Background journal freshness is bounded per journal workspace (W448).

On dev-main, 2026-10-04 22:40-23:41Z, the relay's lock logging named
``journals.JournalWorkspace.ensure_index_fresh`` as the holder of the seven
worker journal workspaces' ``index.lock`` files 633 times in an hour, up to
51 s each, about 3200 s in total: every due heartbeat started a check, and each
check resolved and stat'ed every journal file before it could say nothing
changed.

A heartbeat now starts a check only when none ran for this journal workspace
and binding, or the interval has passed; channels serving the same workspace
share it, and one whose own heartbeat did not start a check reports the shared
result. The stamps
resolve only symlinks: the walk already resolves every directory.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from project_board.client import relay
from project_board.contract.errors import DomainError
from test_workspace_clone_journal import HOME, ENTRY, PROJECT, host  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def fresh_shared_state():
    shared = [getattr(relay, name, {}) for name in ("_journal_checks_started", "_journal_check_results")]
    for state in shared:
        state.clear()
    yield
    for state in shared:
        state.clear()


def _workspace(root: Path) -> SimpleNamespace:
    return SimpleNamespace(root=root, repositories=SimpleNamespace(roots={}, missing={}, workspace=None))


class CountingWorker(relay._JournalRefreshWorker):
    """The real poll; the check itself is a counted stub that completes at once."""

    runs: list[str] = []

    def _run(self, source, binding, create_home, cancel_job):
        CountingWorker.runs.append(str(binding.get("journal_home_ref")))
        return {"state": "ready", "binding": dict(binding)}


def _drain(worker: relay._JournalRefreshWorker) -> None:
    if worker._future is not None:
        worker._future.result(timeout=5)


@pytest.fixture
def counted():
    CountingWorker.runs = []
    workers: list[relay._JournalRefreshWorker] = []

    def make(**kwargs) -> relay._JournalRefreshWorker:
        worker = CountingWorker(**kwargs)
        workers.append(worker)
        return worker

    yield make
    for worker in workers:
        worker._stop.set()
        if worker._pool is not None:
            worker._pool.shutdown(wait=True)


def test_one_check_per_interval_across_channels_and_heartbeats(tmp_path, counted):
    workspace = _workspace(tmp_path)
    heartbeat = {"journal_binding": {"journal_home_ref": "repo:journals/home", "revision": 1}}
    channels = [counted() for _ in range(5)]  # the default interval

    for _beat in range(4):
        for worker in channels:
            worker.poll(workspace, heartbeat, create_home=False)
            _drain(worker)

    assert CountingWorker.runs == ["repo:journals/home"], "5 channels x 4 heartbeats start one check"


def test_a_channel_whose_heartbeat_started_no_check_reports_the_shared_result(tmp_path, counted):
    workspace = _workspace(tmp_path)
    heartbeat = {"journal_binding": {"journal_home_ref": "repo:journals/home", "revision": 1}}
    first, second = counted(), counted()

    first.poll(workspace, heartbeat, create_home=False)
    _drain(first)
    ready, result = first.poll(workspace, heartbeat, create_home=False)
    assert ready and result["state"] == "ready"

    ready, result = second.poll(workspace, heartbeat, create_home=False)
    assert ready, "not refresh_pending forever"
    assert result["state"] == "ready"
    assert len(CountingWorker.runs) == 1


def test_a_changed_binding_starts_a_check_at_once(tmp_path, counted):
    workspace = _workspace(tmp_path)
    worker = counted(freshness_interval_seconds=60.0)
    worker.poll(workspace, {"journal_binding": {"journal_home_ref": "repo:journals/home", "revision": 1}}, create_home=False)
    _drain(worker)
    worker.poll(workspace, {"journal_binding": {"journal_home_ref": "repo:journals/home", "revision": 2}}, create_home=False)
    _drain(worker)
    assert len(CountingWorker.runs) == 2


def test_a_check_starts_again_once_the_interval_has_passed(tmp_path, counted, monkeypatch):
    workspace = _workspace(tmp_path)
    heartbeat = {"journal_binding": {"journal_home_ref": "repo:journals/home", "revision": 1}}
    clock = [1000.0]
    monkeypatch.setattr(relay.time, "monotonic", lambda: clock[0])
    worker = counted(freshness_interval_seconds=60.0)

    worker.poll(workspace, heartbeat, create_home=False)
    _drain(worker)
    clock[0] += 59.0
    worker.poll(workspace, heartbeat, create_home=False)
    _drain(worker)
    assert len(CountingWorker.runs) == 1
    clock[0] += 2.0
    worker.poll(workspace, heartbeat, create_home=False)
    _drain(worker)
    assert len(CountingWorker.runs) == 2


def test_journal_check_due_is_shared_by_key_and_interval():
    assert relay._journal_check_due("/w", "k1", interval=60, now=0.0) is True
    assert relay._journal_check_due("/w", "k1", interval=60, now=30.0) is False
    assert relay._journal_check_due("/w", "k2", interval=60, now=31.0) is True, "another binding is due at once"
    assert relay._journal_check_due("/w", "k2", interval=60, now=92.0) is True
    assert relay._journal_check_due("/other", "k2", interval=60, now=93.0) is True, "another workspace is separate"


def test_a_directory_symlink_out_of_the_home_is_still_refused(host):
    workspace = host["workers"]["current"]["relay"].journal_workspace
    journal = host["workers"]["current"]["workspace"] / "journals" / HOME / "journal"
    (journal / "elsewhere").symlink_to(host["stale"], target_is_directory=True)

    with pytest.raises(DomainError) as caught:
        workspace.search("host-checkout-runtime", project_ref=PROJECT)
    assert caught.value.code == "journal_repository_ref_escape"


def test_a_file_symlink_out_of_the_home_is_still_refused(host):
    workspace = host["workers"]["current"]["relay"].journal_workspace
    journal = host["workers"]["current"]["workspace"] / "journals" / HOME / "journal"
    (journal / "foreign.md").symlink_to(host["stale"] / ENTRY)

    with pytest.raises(DomainError) as caught:
        workspace.search("host-checkout-runtime", project_ref=PROJECT)
    assert caught.value.code == "journal_repository_ref_escape"


def test_stamps_of_a_symlink_free_tree_equal_the_resolving_ones(host):
    workspace = host["workers"]["current"]["relay"].journal_workspace
    sources = workspace._source_stamps()
    [(_, source)] = sources.items()
    home = host["workers"]["current"]["workspace"] / "journals" / HOME
    files = []
    for path in workspace._journal_paths(home):
        assert home in path.resolve().parents
        metadata = path.stat()
        files.append((path.relative_to(home).as_posix(), metadata.st_size,
                      metadata.st_mtime_ns, metadata.st_ctime_ns))
    expected = hashlib.sha256(json.dumps(files, separators=(",", ":")).encode("utf-8")).hexdigest()
    assert files, "the fixture has journal entries"
    assert source["content_signature"] == expected


def test_a_failed_check_is_retried_at_the_next_heartbeat(tmp_path, counted):
    workspace = _workspace(tmp_path)
    heartbeat = {"journal_binding": {"journal_home_ref": "repo:journals/home", "revision": 1}}

    class FailingOnce(CountingWorker):
        def _run(self, source, binding, create_home, cancel_job):
            CountingWorker.runs.append("run")
            if len(CountingWorker.runs) == 1:
                return DomainError("journal_index_refresh_failed", "held")
            return {"state": "ready"}

    worker = FailingOnce(freshness_interval_seconds=60.0)
    try:
        worker.poll(workspace, heartbeat, create_home=False)
        _drain(worker)
        ready, result = worker.poll(workspace, heartbeat, create_home=False)
        assert ready and isinstance(result, DomainError)
        _drain(worker)
        assert len(CountingWorker.runs) == 2, "the failure did not hold off the retry"
    finally:
        worker._stop.set()
        if worker._pool is not None:
            worker._pool.shutdown(wait=True)

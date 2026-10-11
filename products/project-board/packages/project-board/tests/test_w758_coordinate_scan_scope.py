"""An addressed queue check never walks another worker's directory.

This pins scan scope, not elapsed time: a busy foreign queue must not become
work performed by each idle channel. No authorization result is cached.
"""
from pathlib import Path
import os

import pytest

from project_board.client.coordinate_queue import CoordinateQueue


def _entry(queue, state, worker, name, modified_at):
    root = queue.root / state / worker
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{name}.json"
    path.write_text("{}", encoding="utf-8")
    os.utime(path, (modified_at, modified_at))
    return path


@pytest.mark.parametrize("workers", [["worker-a"], ["worker-a", "worker-a"]])
def test_addressed_signature_does_not_enumerate_foreign_workers(tmp_path, monkeypatch, workers):
    queue = CoordinateQueue(tmp_path)
    _entry(queue, "pending", "worker-a", "ready", 100)
    _entry(queue, "leased", "worker-a", "expired", 100)
    foreign = {queue.root / state / "worker-b" for state in ("pending", "leased")}
    for state in ("pending", "leased"):
        _entry(queue, state, "worker-b", "foreign", 100)
    monkeypatch.setattr("project_board.client.coordinate_queue.time.time", lambda: 1000)
    scanned = []
    real = os.scandir

    def addressed_only(path):
        root = Path(path)
        scanned.append(root)
        assert root not in foreign, "an addressed check scanned a foreign worker's queue"
        return real(path)

    monkeypatch.setattr(os, "scandir", addressed_only)
    assert queue.work_signature(worker_names=workers) == (1, 100_000_000_000, 1, 1)
    assert scanned == [queue.root / "pending" / "worker-a", queue.root / "leased" / "worker-a"]


def test_addressed_signature_keeps_deferred_and_lease_recovery_semantics(tmp_path, monkeypatch):
    queue = CoordinateQueue(tmp_path)
    _entry(queue, "pending", "worker-a", "ready", 100)
    _entry(queue, "pending", "worker-a", "deferred", 2000)
    _entry(queue, "leased", "worker-a", "recoverable", 100)
    _entry(queue, "leased", "worker-a", "not-expired", 2000)
    _entry(queue, "pending", "worker-b", "foreign", 3000)
    monkeypatch.setattr("project_board.client.coordinate_queue.time.time", lambda: 1000)
    assert queue.work_signature(worker_names=["worker-a"]) == (2, 2_000_000_000_000, 1, 1)
    assert queue.has_ready_work(worker_names=["worker-a"])
    assert queue.work_signature(worker_names=["missing-worker"]) == (0, 0, 0, 0)


def test_unfiltered_signature_still_sees_every_worker_and_new_work(tmp_path, monkeypatch):
    queue = CoordinateQueue(tmp_path)
    _entry(queue, "pending", "worker-a", "ready", 100)
    _entry(queue, "leased", "worker-b", "recoverable", 200)
    monkeypatch.setattr("project_board.client.coordinate_queue.time.time", lambda: 1000)
    assert queue.work_signature() == (1, 100_000_000_000, 1, 1)
    # Each check observes the directory again; a new request is never hidden
    # behind an mtime/size cache or the result of the previous scan.
    _entry(queue, "pending", "worker-b", "new-ready", 300)
    assert queue.work_signature() == (2, 300_000_000_000, 2, 1)
    assert queue.work_signature(worker_names=["worker-b"]) == (1, 300_000_000_000, 1, 1)

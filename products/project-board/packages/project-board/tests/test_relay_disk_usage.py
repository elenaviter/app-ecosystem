"""W423: the relay reports the host's free disk and the agent's workspace size.

The free and total bytes come from one statvfs every beat; the workspace size
walks the tree in a background task at most every DISK_USAGE_REMEASURE_SECONDS,
and the heartbeat never waits for it (review of #395: the walk took 25 s over
13.6 GB of workspaces on one host).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

from project_board.client import relay


def _adapter_class():
    for value in vars(relay).values():
        if isinstance(value, type) and hasattr(value, "_add_disk_usage"):
            return value
    raise AssertionError("no relay adapter defines _add_disk_usage")


def test_disk_usage_rides_the_heartbeat_and_the_walk_is_bounded(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / "wt" / "w1-app").mkdir(parents=True)
    (workspace / "wt" / "w1-app" / "file.bin").write_bytes(b"x" * 4096)
    adapter = SimpleNamespace(config=SimpleNamespace(workspace=str(workspace), working_directory=""))
    adapter_class = _adapter_class()
    adapter._schedule_workspace_measure = lambda path: adapter_class._schedule_workspace_measure(adapter, path)
    walks = []
    real_walk = relay.directory_bytes

    def counting(root):
        walks.append(root)
        return real_walk(root)

    monkeypatch.setattr(relay, "directory_bytes", counting)

    async def beats():
        first: dict = {}
        await adapter_class._add_disk_usage(adapter, first)
        # The first beat carries the disk, and no size until the walk lands.
        assert 0 < first["disk_usage"]["host_free_bytes"] <= first["disk_usage"]["host_total_bytes"]
        assert first["disk_usage"]["workspace_path"] == str(workspace)
        assert "workspace_bytes" not in first["disk_usage"]
        await adapter._workspace_measure_task
        second: dict = {}
        await adapter_class._add_disk_usage(adapter, second)
        third: dict = {}
        await adapter_class._add_disk_usage(adapter, third)
        return second, third

    second, third = asyncio.run(beats())
    assert second["disk_usage"]["workspace_bytes"] == 4096
    # Beats inside the interval reuse the measured size: one walk.
    assert len(walks) == 1 and third["disk_usage"]["workspace_bytes"] == 4096


def test_a_slow_walk_never_delays_the_heartbeat(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = SimpleNamespace(config=SimpleNamespace(workspace=str(workspace), working_directory=""))
    adapter_class = _adapter_class()
    adapter._schedule_workspace_measure = lambda path: adapter_class._schedule_workspace_measure(adapter, path)

    def slow(_root):
        time.sleep(1.0)
        return 7

    monkeypatch.setattr(relay, "directory_bytes", slow)

    async def beat():
        payload: dict = {}
        started = time.monotonic()
        await adapter_class._add_disk_usage(adapter, payload)
        elapsed = time.monotonic() - started
        # A second beat while the walk still runs starts no second walk.
        task = adapter._workspace_measure_task
        await adapter_class._add_disk_usage(adapter, {})
        assert adapter._workspace_measure_task is task
        await task
        return payload, elapsed

    payload, elapsed = asyncio.run(beat())
    assert elapsed < 0.5
    assert "host_free_bytes" in payload["disk_usage"] and "workspace_bytes" not in payload["disk_usage"]


def test_no_workspace_means_no_disk_report(tmp_path: Path):
    adapter = SimpleNamespace(config=SimpleNamespace(workspace="", working_directory=str(tmp_path / "missing")))
    payload: dict = {}
    asyncio.run(_adapter_class()._add_disk_usage(adapter, payload))
    assert "disk_usage" not in payload


def test_workspace_walks_never_use_the_default_executor_and_run_one_at_a_time(tmp_path: Path, monkeypatch):
    """W461: walks of many channels saturated the default executor that OAuth
    file locks and DNS lookups share (dev-main, 2026-10-02, 94% CPU, about
    twenty threads in lstat)."""

    import threading

    adapter_class = _adapter_class()
    adapters = []
    for index in range(4):
        workspace = tmp_path / f"workspace-{index}"
        workspace.mkdir()
        adapter = SimpleNamespace(
            config=SimpleNamespace(workspace=str(workspace), working_directory="", worker_name=f"w{index}")
        )
        adapter._schedule_workspace_measure = (
            lambda path, adapter=adapter: adapter_class._schedule_workspace_measure(adapter, path)
        )
        adapters.append(adapter)
    active = []
    overlap = []
    threads = set()
    guard = threading.Lock()

    def walk(_root):
        with guard:
            active.append(1)
            overlap.append(len(active))
            threads.add(threading.current_thread().name)
        time.sleep(0.05)
        with guard:
            active.pop()
        return 1

    monkeypatch.setattr(relay, "directory_bytes", walk)

    async def scenario():
        loop = asyncio.get_running_loop()
        executor_used = []
        real_run_in_executor = loop.run_in_executor

        def spy(executor, func, *args):
            executor_used.append(executor)
            return real_run_in_executor(executor, func, *args)

        loop.run_in_executor = spy
        for adapter in adapters:
            await adapter_class._add_disk_usage(adapter, {})
        # While the walks run, the default executor still answers at once.
        started = time.monotonic()
        await asyncio.to_thread(lambda: None)
        free_executor_seconds = time.monotonic() - started
        await asyncio.gather(*(adapter._workspace_measure_task for adapter in adapters))
        return executor_used, free_executor_seconds

    executor_used, free_executor_seconds = asyncio.run(scenario())
    assert max(overlap) == 1, "walks ran in parallel"
    assert threads == {"problem-board-workspace-walk"}
    assert executor_used == [None], "only the probe used the default executor"
    assert free_executor_seconds < 0.05
    assert all(adapter._workspace_bytes == 1 for adapter in adapters)


def test_each_walk_is_logged_with_its_timing_and_queue_wait(tmp_path: Path, monkeypatch, caplog):
    import logging

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = SimpleNamespace(config=SimpleNamespace(workspace=str(workspace), working_directory="", worker_name="w-log"))
    adapter_class = _adapter_class()
    adapter._schedule_workspace_measure = lambda path: adapter_class._schedule_workspace_measure(adapter, path)

    def failing(_root):
        raise PermissionError("synthetic")

    monkeypatch.setattr(relay, "directory_bytes", failing)

    async def beat():
        await adapter_class._add_disk_usage(adapter, {})
        await adapter._workspace_measure_task

    with caplog.at_level(logging.INFO, logger=relay.logger.name):
        asyncio.run(beat())
    (line,) = [r for r in caplog.records if "workspace size walk" in r.getMessage()]
    text = line.getMessage()
    assert line.levelno == logging.WARNING
    assert "worker=w-log outcome=PermissionError" in text
    assert "walk_seconds=" in text and "queue_wait_seconds=" in text and "queued_behind=" in text
    assert "synthetic" not in text

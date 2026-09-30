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

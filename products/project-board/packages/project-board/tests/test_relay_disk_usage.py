"""W423: the relay reports the host's free disk and the agent's workspace size.

The free and total bytes come from one statvfs every beat; the workspace size
walks the tree, so it is measured at most every DISK_USAGE_REMEASURE_SECONDS.
"""

from __future__ import annotations

import asyncio
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
    add = _adapter_class()._add_disk_usage
    walks = []
    real_walk = relay.directory_bytes

    def counting(root):
        walks.append(root)
        return real_walk(root)

    monkeypatch.setattr(relay, "directory_bytes", counting)
    payload: dict = {}
    asyncio.run(add(adapter, payload))
    usage = payload["disk_usage"]
    assert usage["workspace_bytes"] == 4096
    assert 0 < usage["host_free_bytes"] <= usage["host_total_bytes"]
    assert usage["workspace_path"] == str(workspace)
    # A second beat inside the interval reuses the measured size.
    second: dict = {}
    asyncio.run(add(adapter, second))
    assert len(walks) == 1 and second["disk_usage"]["workspace_bytes"] == 4096


def test_no_workspace_means_no_disk_report(tmp_path: Path):
    adapter = SimpleNamespace(config=SimpleNamespace(workspace="", working_directory=str(tmp_path / "missing")))
    payload: dict = {}
    asyncio.run(_adapter_class()._add_disk_usage(adapter, payload))
    assert "disk_usage" not in payload

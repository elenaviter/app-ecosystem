"""W423: the relay reports the host's free disk and the agent's workspace size.

The free and total bytes come from one statvfs every beat. The workspace size
needs a walk over every file, so the heartbeat never waits for it.

W461 (2026-10-02): the walk state lived on the adapter that
``poll_attendances_once`` builds anew per project on every poll, so every
heartbeat of every channel walked the whole workspace (spark1: 278 walks in
48 minutes for 3 channels, against one per 900 s each), each walk on the
default executor the OAuth locks and DNS share. These tests drive the real
per-poll rebuild, which the first tests did not.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from project_board.client import relay
from project_board.client.workspace_size import WorkspaceSizes, directory_bytes, walk_in_child_process
from test_attendance_materializes_project import Board, _fresh_host


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _counting_walk(walks: list[str], size: int = 4096):
    async def walk(path: str) -> int:
        walks.append(path)
        return size

    return walk


def test_a_rebuilt_adapter_every_poll_walks_once_per_interval_and_reports_the_size(tmp_path):
    host, identity, field, config = _fresh_host(tmp_path)
    workspace = Path(config.workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    clock = Clock()
    walks: list[str] = []
    sizes = WorkspaceSizes(walk=_counting_walk(walks), clock=clock)
    board = Board(identity.worker_name)
    adapter = relay.ProblemBoardHostRelayAdapter(
        config=config, field=field, client=board, monotonic=clock, workspace_sizes=sizes
    )

    async def polls():
        for _ in range(4):
            await adapter.poll_attendances_once()
            await asyncio.sleep(0)  # the scheduled walk runs
            clock.now += 120  # past the heartbeat wait, far inside the 900 s interval

    asyncio.run(polls())

    assert walks == [str(workspace)], "one walk per interval, though every poll built a new project adapter"
    beats = [
        c["payload"] for c in board.calls
        if c["action"] == "worker.heartbeat" and c["payload"].get("project_ref") and "disk_usage" in c["payload"]
    ]
    assert len(beats) >= 2
    assert "workspace_bytes" not in beats[0]["disk_usage"], "no size until the first walk lands"
    assert beats[-1]["disk_usage"]["workspace_bytes"] == 4096, "the measured size reaches a later heartbeat"


def test_the_size_is_remeasured_after_the_interval():
    clock = Clock()
    walks: list[str] = []
    sizes = WorkspaceSizes(walk=_counting_walk(walks), clock=clock, interval_seconds=900)

    async def scenario():
        await sizes.schedule("/w", worker_name="w")
        assert sizes.schedule("/w", worker_name="w") is None
        clock.now += 901
        await sizes.schedule("/w", worker_name="w")

    asyncio.run(scenario())
    assert walks == ["/w", "/w"]


class _HeldChild:
    """A child process whose exit after SIGKILL the test controls."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.killed = False
        self.exited = asyncio.Event()

    async def communicate(self):
        await asyncio.Event().wait()  # a walk that never finishes by itself

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        await self.exited.wait()
        self.returncode = -9
        return -9


def test_a_cancelled_walk_keeps_its_slot_until_the_killed_child_is_reaped(monkeypatch):
    """A second cancellation must not release the slot before the child exits."""

    children: list[_HeldChild] = []

    async def spawn(*_args, **_kwargs):
        child = _HeldChild()
        children.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    async def scenario():
        walking = asyncio.create_task(walk_in_child_process("/held"))
        for _ in range(20):
            if children:
                break
            await asyncio.sleep(0)
        walking.cancel()
        for _ in range(20):
            if children[0].killed:
                break
            await asyncio.sleep(0)
        assert children[0].killed
        walking.cancel()  # a second cancellation while the child is being reaped
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not walking.done(), "the walk waits for the killed child to be reaped"
        children[0].exited.set()
        with pytest.raises(asyncio.CancelledError):
            await walking
        assert children[0].returncode == -9

    asyncio.run(scenario())


def _peak_walks(sizes_kwargs: dict, count: int) -> int:
    active: list[int] = []
    peak: list[int] = []

    async def walk(path: str) -> int:
        active.append(1)
        peak.append(len(active))
        await asyncio.sleep(0.02)
        active.pop()
        return 1

    sizes = WorkspaceSizes(walk=walk, **sizes_kwargs)

    async def scenario():
        tasks = [sizes.schedule(f"/w{index}", worker_name="w") for index in range(count)]
        await asyncio.gather(*tasks)

    asyncio.run(scenario())
    assert all(sizes.last(f"/w{index}") == 1 for index in range(count))
    return max(peak)


def test_walks_of_different_workspaces_never_exceed_the_concurrency_bound():
    assert _peak_walks({}, 6) == 2, "two child walks at once by default, never more"
    assert _peak_walks({"max_concurrent_walks": 1}, 4) == 1
    with pytest.raises(ValueError):
        WorkspaceSizes(max_concurrent_walks=0)


def test_a_long_walk_does_not_hold_back_the_next_workspace(caplog):
    """W469: at relay startup every channel's walk is scheduled at once.

    With one walk at a time, a short walk queued behind a long one waited for
    all of it: on dev-main the walks behind an 87 s walk waited up to 142 s.
    Here the long walk is held until the short one has finished, so the short
    one can only finish if it is not queued behind the long one.
    """

    long_running = asyncio.Event()
    release_long = asyncio.Event()
    order: list[str] = []

    async def walk(path: str) -> int:
        if path == "/long":
            long_running.set()
            await release_long.wait()
        order.append(path)
        return 1

    sizes = WorkspaceSizes(walk=walk)

    async def scenario():
        long_task = sizes.schedule("/long", worker_name="long")
        await asyncio.wait_for(long_running.wait(), 1)
        short_task = sizes.schedule("/short", worker_name="short")
        await asyncio.wait_for(short_task, 1)  # times out when queued behind /long
        assert not long_task.done(), "the long walk is still running"
        release_long.set()
        await long_task

    with caplog.at_level(logging.INFO, logger="project_board.client.relay"):
        asyncio.run(scenario())
    assert order == ["/short", "/long"]
    short_line = next(r.getMessage() for r in caplog.records if "worker=short " in r.getMessage())
    assert "queue_wait_seconds=0.0" in short_line, short_line


def test_a_cancelled_walk_frees_its_slot():
    started: list[str] = []
    never = asyncio.Event()

    async def walk(path: str) -> int:
        started.append(path)
        if path.startswith("/stuck"):
            await never.wait()
        return 1

    sizes = WorkspaceSizes(walk=walk)

    async def scenario():
        stuck = [sizes.schedule(f"/stuck{index}", worker_name="w") for index in range(2)]
        await asyncio.sleep(0)
        waiting = sizes.schedule("/next", worker_name="w")
        await asyncio.sleep(0)
        assert started == ["/stuck0", "/stuck1"], "both slots are held"
        stuck[0].cancel()
        await asyncio.wait_for(waiting, 1)
        stuck[1].cancel()
        await asyncio.gather(*stuck, return_exceptions=True)

    asyncio.run(scenario())
    assert started[-1] == "/next"
    assert sizes.last("/next") == 1


def test_a_slow_walk_never_delays_the_heartbeat(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    async def slow(path: str) -> int:
        await asyncio.sleep(1.0)
        return 7

    adapter = SimpleNamespace(
        config=SimpleNamespace(workspace=str(workspace), working_directory="", worker_name="w"),
        _workspace_sizes=WorkspaceSizes(walk=slow),
    )

    async def beat():
        payload: dict = {}
        started = time.monotonic()
        await relay.ProblemBoardHostRelayAdapter._add_disk_usage(adapter, payload)
        return payload, time.monotonic() - started

    payload, elapsed = asyncio.run(beat())
    assert elapsed < 0.5
    assert "host_free_bytes" in payload["disk_usage"] and "workspace_bytes" not in payload["disk_usage"]


def test_a_failed_walk_keeps_the_last_size_and_logs_without_its_text(caplog):
    clock = Clock()
    results = [5, PermissionError("synthetic secret path")]

    async def walk(path: str) -> int:
        value = results.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    sizes = WorkspaceSizes(walk=walk, clock=clock, interval_seconds=10)

    async def scenario():
        await sizes.schedule("/w", worker_name="w-log")
        clock.now += 11
        await sizes.schedule("/w", worker_name="w-log")

    with caplog.at_level(logging.INFO, logger=relay.logger.name):
        asyncio.run(scenario())
    assert sizes.last("/w") == 5
    lines = [r for r in caplog.records if "workspace size walk" in r.getMessage()]
    assert [r.levelno for r in lines] == [logging.INFO, logging.WARNING]
    assert "worker=w-log outcome=PermissionError" in lines[1].getMessage()
    assert "synthetic" not in lines[1].getMessage()


def test_the_child_process_walk_counts_the_same_bytes(tmp_path: Path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "one.bin").write_bytes(b"x" * 1000)
    (tmp_path / "two.bin").write_bytes(b"y" * 24)
    (tmp_path / "link").symlink_to(tmp_path / "two.bin")

    assert asyncio.run(walk_in_child_process(str(tmp_path))) == directory_bytes(tmp_path)


def test_no_workspace_means_no_disk_report(tmp_path: Path):
    adapter = SimpleNamespace(
        config=SimpleNamespace(workspace="", working_directory=str(tmp_path / "missing"), worker_name="w"),
        _workspace_sizes=WorkspaceSizes(walk=_counting_walk([])),
    )
    payload: dict = {}
    asyncio.run(relay.ProblemBoardHostRelayAdapter._add_disk_usage(adapter, payload))
    assert "disk_usage" not in payload


def _beat_adapter(config_workspace: str, sizes: WorkspaceSizes, *, working_directory: str = "", worker: str = "w"):
    return SimpleNamespace(
        config=SimpleNamespace(workspace=config_workspace, working_directory=working_directory, worker_name=worker),
        _workspace_sizes=sizes,
    )


def _beats(adapters, rounds: int = 3) -> list[dict]:
    payloads: list[dict] = []

    async def scenario():
        for _ in range(rounds):
            for adapter in adapters:
                payload: dict = {}
                await relay.ProblemBoardHostRelayAdapter._add_disk_usage(adapter, payload)
                payloads.append(payload)
            await asyncio.sleep(0)

    asyncio.run(scenario())
    return payloads


def test_two_channels_on_one_workspace_share_one_walk_and_its_size(tmp_path: Path):
    """Ops case C1: one WorkspaceSizes for the host, two channel adapters, one folder."""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    walks: list[str] = []
    sizes = WorkspaceSizes(walk=_counting_walk(walks))
    payloads = _beats([_beat_adapter(str(workspace), sizes, worker="w1"), _beat_adapter(str(workspace), sizes, worker="w2")])
    assert len(walks) == 1
    assert payloads[-1]["disk_usage"]["workspace_bytes"] == 4096


def test_a_session_folder_is_never_measured_as_the_workspace(tmp_path: Path):
    """Ops case C5: no workspace named means no walk, even when a session folder exists."""

    session_folder = tmp_path / "session-start"
    session_folder.mkdir()
    walks: list[str] = []
    payloads = _beats([_beat_adapter("", WorkspaceSizes(walk=_counting_walk(walks)), working_directory=str(session_folder))], rounds=1)
    assert walks == [] and "disk_usage" not in payloads[0]


def test_one_folder_spelled_three_ways_is_one_walk(tmp_path: Path):
    """Ops case C6: plain path, trailing slash and a symlink name one folder."""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "alias").symlink_to(workspace)
    walks: list[str] = []
    sizes = WorkspaceSizes(walk=_counting_walk(walks))
    _beats([
        _beat_adapter(str(workspace), sizes),
        _beat_adapter(str(workspace) + "/", sizes),
        _beat_adapter(str(tmp_path / "alias"), sizes),
    ])
    assert walks == [os.path.realpath(workspace)]


def test_a_failed_child_walk_names_its_exception_type_never_its_text():
    from project_board.client.workspace_size import _exception_type

    traceback = b"Traceback (most recent call last):\n  File \"x\", line 1\nPermissionError: [Errno 13] /secret/path/token=CANARY\n"
    assert _exception_type(traceback) == "PermissionError"
    assert _exception_type(b"") == "unknown"
    assert _exception_type(b"weird line with spaces: and text\n") == "unknown"
    # Infra's canary: identifier-looking text that is no exception class.
    assert _exception_type(b"InfraSyntheticStderrCanary123\n") == "unknown"
    assert _exception_type(b"some.module.Thing_with_CANARY: x\n") == "unknown"


def test_a_failed_child_walk_logs_no_child_text(monkeypatch, caplog):
    from project_board.client import workspace_size

    class FailedChild:
        returncode = 7

        async def communicate(self):
            return b"", b"InfraSyntheticStderrCanary123\n"

        def kill(self):
            pass

        async def wait(self):
            return 7

    async def fake_exec(*args, **kwargs):
        return FailedChild()

    monkeypatch.setattr(workspace_size.asyncio, "create_subprocess_exec", fake_exec)
    sizes = WorkspaceSizes()

    async def scenario():
        await sizes.schedule("/synthetic-workspace", worker_name="synthetic-worker")

    with caplog.at_level(logging.INFO, logger=relay.logger.name):
        asyncio.run(scenario())
    assert sizes.last("/synthetic-workspace") is None
    assert "WorkspaceWalkFailed:7:unknown" in caplog.text
    assert "Canary" not in caplog.text

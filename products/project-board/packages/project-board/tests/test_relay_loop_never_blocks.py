"""The relay's event loop never runs blocking work (W461, operator 2026-10-02).

Every channel on a host shares one event loop. On 2026-10-02 the dev-main
relay logged 134 loop blocks of 3 s or more; after the upgrade the remaining
ones were git commands run with subprocess.run directly on the loop, from the
worktree observation behind every project heartbeat. These tests pin that
the observation runs in the channel's own thread and that the loop keeps
running while it does.
"""

from __future__ import annotations

import asyncio
import threading
import time

from project_board.client import relay
from test_attendance_materializes_project import Board, _fresh_host


def test_the_worktree_observation_runs_off_the_event_loop_and_the_loop_keeps_ticking(tmp_path, monkeypatch):
    host, identity, field, config = _fresh_host(tmp_path)
    seen: list[str] = []
    real = relay.ProblemBoardHostRelayAdapter._assignment_files_delta

    def slow_delta(self, *, project_ref, fresh=False):
        seen.append(threading.current_thread().name)
        time.sleep(0.5)  # a slow git, as on dev-main
        return real(self, project_ref=project_ref, fresh=fresh)

    monkeypatch.setattr(relay.ProblemBoardHostRelayAdapter, "_assignment_files_delta", slow_delta)
    adapter = relay.ProblemBoardHostRelayAdapter(config=config, field=field, client=Board(identity.worker_name))

    async def scenario():
        loop_thread = threading.current_thread().name
        ticks = 0
        stop = asyncio.Event()

        async def ticker():
            nonlocal ticks
            while not stop.is_set():
                ticks += 1
                await asyncio.sleep(0.02)

        tick_task = asyncio.create_task(ticker())
        await adapter.poll_attendances_once()
        stop.set()
        await tick_task
        return loop_thread, ticks

    loop_thread, ticks = asyncio.run(scenario())
    assert seen, "the observation ran"
    assert all(name != loop_thread for name in seen), "git work ran on the event loop's thread"
    assert all(name.startswith("problem-board-") for name in seen), "not the default executor"
    assert ticks >= 15, f"the loop stalled during a 0.5 s observation ({ticks} ticks)"


def test_the_coordinate_server_pass_reads_files_off_the_loop_and_still_serves(tmp_path, monkeypatch):
    """Every 0.25 s per channel; inline it was 51 of 128 loop blocks on 2026-10-02."""

    from project_board.client import coordinate_queue
    from relay_helpers import StableClient, make_host, make_supervisor, submit_request
    from test_relay_coordinate_beside_cycle import _bound_session

    host, _identity, channel = make_host(tmp_path)
    queue = coordinate_queue.CoordinateQueue(host.field_root)
    supervisor = make_supervisor(host)
    supervisor._sessions[channel.worker_name] = _bound_session(host, channel, supervisor, StableClient())
    threads: list[str] = []
    real_load = relay.HostRelayConfig.load
    real_ready = coordinate_queue.CoordinateQueue.has_ready_work

    def load(path):
        threads.append(threading.current_thread().name)
        return real_load(path)

    def has_ready_work(self, **kwargs):
        threads.append(threading.current_thread().name)
        return real_ready(self, **kwargs)

    monkeypatch.setattr(relay.HostRelayConfig, "load", staticmethod(load))
    monkeypatch.setattr(coordinate_queue.CoordinateQueue, "has_ready_work", has_ready_work)

    async def scenario():
        loop_thread = threading.current_thread().name
        request = submit_request(queue, channel)
        started = await supervisor.serve_coordinate_pass()
        await asyncio.gather(*supervisor._coordinate_draining.values())
        return loop_thread, started, queue.take_response(worker_name=channel.worker_name, request_id=request["request_id"])

    loop_thread, started, response = asyncio.run(scenario())
    assert started == [channel.worker_name]
    assert response is not None and response["ok"] is True
    assert threads and all(name != loop_thread for name in threads), threads


def test_the_project_heartbeat_store_work_runs_off_the_loop(tmp_path, monkeypatch):
    """Before and after the network call, _poll_project_once reads and writes the field store."""

    from project_board.client.store import SharedFieldStore

    host, identity, field, config = _fresh_host(tmp_path)
    threads: dict[str, set[str]] = {}

    def spy(name, real):
        def wrapper(*args, **kwargs):
            threads.setdefault(name, set()).add(threading.current_thread().name)
            return real(*args, **kwargs)
        return wrapper

    for name in ("sync_project_team", "sync_project_coordinator", "read_project"):
        monkeypatch.setattr(SharedFieldStore, name, spy(name, getattr(SharedFieldStore, name)))
    for name in ("_session_report_delta", "_reconcile_assignments", "_record_project_heartbeat"):
        monkeypatch.setattr(relay.ProblemBoardHostRelayAdapter, name, spy(name, getattr(relay.ProblemBoardHostRelayAdapter, name)))

    adapter = relay.ProblemBoardHostRelayAdapter(config=config, field=field, client=Board(identity.worker_name))

    async def scenario():
        await adapter.poll_attendances_once()
        return threading.current_thread().name

    loop_thread = asyncio.run(scenario())
    for name in ("sync_project_team", "sync_project_coordinator", "_session_report_delta", "_reconcile_assignments", "_record_project_heartbeat"):
        assert name in threads, f"{name} did not run"
        assert loop_thread not in threads[name], f"{name} ran on the event loop"

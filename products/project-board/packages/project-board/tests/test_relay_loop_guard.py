"""The relay's main paths call no blocking primitive on the event loop's thread (W461, operator 2026-10-02)."""

from __future__ import annotations

import asyncio

from loop_guard import LoopGuard
from project_board.client import coordinate_queue, relay
from relay_helpers import StableClient, make_host, make_supervisor, submit_request
from test_attendance_materializes_project import Board, _fresh_host
from test_relay_coordinate_beside_cycle import _bound_session


def test_a_project_poll_runs_no_blocking_call_on_the_loop(tmp_path, monkeypatch):
    host, identity, field, config = _fresh_host(tmp_path)
    adapter = relay.ProblemBoardHostRelayAdapter(config=config, field=field, client=Board(identity.worker_name))
    guard = LoopGuard(monkeypatch).install()
    asyncio.run(adapter.poll_attendances_once())
    assert guard.report() == []


def test_the_coordinate_pass_and_its_drain_run_no_blocking_call_on_the_loop(tmp_path, monkeypatch):
    host, _identity, channel = make_host(tmp_path)
    queue = coordinate_queue.CoordinateQueue(host.field_root)
    supervisor = make_supervisor(host)
    supervisor._sessions[channel.worker_name] = _bound_session(host, channel, supervisor, StableClient())
    request = submit_request(queue, channel)
    guard = LoopGuard(monkeypatch).install()

    async def scenario():
        await supervisor.serve_coordinate_pass()
        await asyncio.gather(*supervisor._coordinate_draining.values())

    asyncio.run(scenario())
    assert guard.report() == []

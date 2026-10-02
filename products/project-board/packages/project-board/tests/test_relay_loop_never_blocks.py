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

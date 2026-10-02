"""A stalled relay loop is named by its frames while it is stalled (W456).

The 2026-10-01 relay sample showed the event loop about 60% inside
synchronous open, read and readdir calls, and the relay's own stage timings
showed local-only stages taking 20-60 s, but neither named the Python code
holding the loop. The watchdog thread reads the loop thread's stack during a
stall, so the next stall line says which call blocked every channel.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time

from project_board.client.relay_trace import LoopStallWatchdog


def _synchronous_scan_under_test(seconds: float) -> None:
    # Stands in for a file scan run on the loop thread.
    time.sleep(seconds)


def test_a_blocked_loop_is_named_by_the_call_holding_it(caplog):
    log = logging.getLogger("test.relay.watchdog")
    caplog.set_level(logging.WARNING, logger="test.relay.watchdog")
    watchdog = LoopStallWatchdog(
        log=log,
        blocked_seconds=0.3,
        interval_seconds=0.05,
        log_every_seconds=0.0,
    )

    async def scenario():
        async def beat():
            while True:
                watchdog.beat()
                await asyncio.sleep(0.05)

        beating = asyncio.create_task(beat())
        watchdog.start()
        try:
            await asyncio.sleep(0.2)
            _synchronous_scan_under_test(1.0)
            await asyncio.sleep(0.1)
        finally:
            watchdog.stop()
            beating.cancel()
            await asyncio.gather(beating, return_exceptions=True)

    asyncio.run(scenario())

    lines = [r.getMessage() for r in caplog.records if "loop blocked" in r.getMessage()]
    assert len(lines) == 1, lines
    frames = json.loads(lines[0].split("frames=", 1)[1])
    assert any(frame.endswith(":_synchronous_scan_under_test") for frame in frames), frames
    assert all(frame.count(":") == 2 for frame in frames), "file:line:function only"


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_one_stall_is_reported_once_and_a_beating_loop_never(caplog):
    clock = _Clock()
    log = logging.getLogger("test.relay.watchdog.clock")
    caplog.set_level(logging.WARNING, logger="test.relay.watchdog.clock")
    watchdog = LoopStallWatchdog(
        log=log, blocked_seconds=3.0, log_every_seconds=0.0, monotonic=clock
    )
    watchdog._loop_thread_id = threading.get_ident()
    watchdog.beat()

    clock.now += 2.9
    assert watchdog.check_once() is None, "under the threshold"
    clock.now += 0.2
    first = watchdog.check_once()
    assert first is not None and first["blocked_seconds"] >= 3.0
    clock.now += 5.0
    assert watchdog.check_once() is None, "the same stall is reported once"
    watchdog.beat()
    clock.now += 1.0
    assert watchdog.check_once() is None, "a loop that beats is not blocked"
    assert len([r for r in caplog.records if "loop blocked" in r.getMessage()]) == 1

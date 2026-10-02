"""The serial walker runs blocking walks one at a time off the default executor (W461)."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from project_board.client.serial_walker import SerialWalker


def test_jobs_run_in_order_on_one_daemon_thread():
    walker = SerialWalker("walker-under-test")
    seen = []

    def job(value):
        seen.append((value, threading.current_thread().name, threading.current_thread().daemon))
        return value * 2

    async def scenario():
        return await asyncio.gather(*(walker.run(job, value) for value in range(5)))

    assert asyncio.run(scenario()) == [0, 2, 4, 6, 8]
    assert [value for value, _, _ in seen] == [0, 1, 2, 3, 4]
    assert {(name, daemon) for _, name, daemon in seen} == {("walker-under-test", True)}


def test_an_error_reaches_the_caller_and_the_thread_keeps_serving():
    walker = SerialWalker("walker-errors")

    def boom():
        raise ValueError("synthetic")

    async def scenario():
        with pytest.raises(ValueError):
            await walker.run(boom)
        return await walker.run(lambda: "after")

    assert asyncio.run(scenario()) == "after"


def test_a_cancelled_waiter_skips_its_queued_walk():
    walker = SerialWalker("walker-cancel")
    ran = []
    release = threading.Event()

    def blocker():
        release.wait(2)
        return "first"

    async def scenario():
        first = asyncio.ensure_future(walker.run(blocker))
        await asyncio.sleep(0.05)
        second = asyncio.ensure_future(walker.run(lambda: ran.append("second")))
        await asyncio.sleep(0.05)
        second.cancel()
        release.set()
        assert await first == "first"
        return await walker.run(lambda: "third")

    assert asyncio.run(scenario()) == "third"
    assert ran == []


def test_a_walker_serves_a_new_event_loop_after_the_first_closed():
    walker = SerialWalker("walker-loops")
    assert asyncio.run(walker.run(lambda: 1)) == 1
    time.sleep(0.01)
    assert asyncio.run(walker.run(lambda: 2)) == 2

"""Each credential custody call records its queue, run and resume time (W464, measurement only).

The custody calls themselves are fakes on the real custody thread: a call
held on a test-owned gate, a call queued behind it, a loop kept busy while a
call completes, a cancelled caller and failing calls. The records must name
where the time went, and the calls' results, exceptions and cancellation
must be exactly what they were without the timing.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time

import pytest

from connection_hub.caller.authorization import profile_session, request_records

PROFILE = "problem-board-claude-0123456789ab"
in_custody = profile_session.OAuthProfileSessionService._in_custody


def _records(caplog) -> list[dict[str, str]]:
    return [
        dict(re.findall(r"(\w+)=(\S+)", r.getMessage()))
        for r in caplog.records
        if r.name == "connection_hub.oauth.spans" and "custody call" in r.getMessage()
    ]


def test_a_call_queued_behind_a_held_call_records_its_queue_time(caplog):
    gate = threading.Event()

    def held_get():
        gate.wait(2)
        return "first"

    def queued_put():
        return "second"

    async def scenario():
        with request_records.correlate(PROFILE):
            first = asyncio.create_task(in_custody(held_get))
            await asyncio.sleep(0.05)
            second = asyncio.create_task(in_custody(queued_put))
            await asyncio.sleep(0.3)
            gate.set()
            return await first, await second

    with caplog.at_level(logging.DEBUG, logger="connection_hub.oauth.spans"):
        assert asyncio.run(scenario()) == ("first", "second")
    held, queued = sorted(_records(caplog), key=lambda r: r["seq"])
    assert (held["call"], held["seq"], held["outcome"]) == ("held_get", "1", "ok")
    assert int(held["run_ms"]) >= 300, "the held call's own run time"
    assert (queued["call"], queued["seq"]) == ("queued_put", "2")
    assert int(queued["queue_ms"]) >= 250, "the queued call waited for the one custody thread"
    assert int(queued["run_ms"]) < 100


def test_a_busy_loop_shows_as_resume_time(caplog):
    def quick_get():
        time.sleep(0.05)
        return "token"

    async def scenario():
        call = asyncio.create_task(in_custody(quick_get))
        await asyncio.sleep(0.01)  # the call is on the thread
        time.sleep(0.4)  # the loop is blocked while the call completes
        return await call

    with caplog.at_level(logging.DEBUG, logger="connection_hub.oauth.spans"):
        assert asyncio.run(scenario()) == "token"
    (record,) = _records(caplog)
    assert int(record["resume_ms"]) >= 250, "the result waited for the loop"
    assert int(record["run_ms"]) < 250
    assert record["seq"] == "-" and record["corr"] == "-", "outside a token operation"


def test_a_cancelled_caller_still_waits_for_the_call_and_records_it(caplog):
    gate = threading.Event()
    finished: list[str] = []

    def held_put():
        gate.wait(2)
        finished.append("put")

    async def scenario():
        task = asyncio.create_task(in_custody(held_put))
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done(), "the caller waits for the started call"
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    with caplog.at_level(logging.DEBUG, logger="connection_hub.oauth.spans"):
        asyncio.run(scenario())
    assert finished == ["put"]
    (record,) = _records(caplog)
    assert record["outcome"] == "cancelled"


def test_a_failing_call_raises_unchanged_and_is_recorded(caplog):
    class StoreFailure(Exception):
        pass

    def failing_get():
        raise StoreFailure("CANARY-SECRET-TEXT")

    with caplog.at_level(logging.DEBUG, logger="connection_hub.oauth.spans"):
        with pytest.raises(StoreFailure, match="CANARY-SECRET-TEXT"):
            asyncio.run(in_custody(failing_get))
    (record,) = _records(caplog)
    assert record["outcome"] == "StoreFailure"
    assert "CANARY" not in caplog.text


def test_a_failure_under_cancellation_keeps_its_cleanup_signal(caplog):
    gate = threading.Event()

    def failing_put():
        gate.wait(2)
        raise OSError("store down")

    async def scenario():
        task = asyncio.create_task(in_custody(failing_put))
        await asyncio.sleep(0.05)
        task.cancel()
        gate.set()
        with pytest.raises(profile_session._CancelledAfterFailure) as raised:
            await task
        assert isinstance(raised.value.failure, OSError)

    with caplog.at_level(logging.DEBUG, logger="connection_hub.oauth.spans"):
        asyncio.run(scenario())
    (record,) = _records(caplog)
    assert record["outcome"] == "OSError"


def test_calls_are_numbered_per_token_operation_and_carry_no_arguments(caplog):
    def get(ref):
        return ref

    async def scenario():
        with request_records.correlate(PROFILE) as first:
            await in_custody(get, "CANARY-REF-1")
            await in_custody(get, "CANARY-REF-2")
        with request_records.correlate(PROFILE) as second:
            await in_custody(lambda: None)
        return first, second

    with caplog.at_level(logging.DEBUG, logger="connection_hub.oauth.spans"):
        first, second = asyncio.run(scenario())
    one, two, three = _records(caplog)
    assert (one["seq"], two["seq"], one["corr"], two["corr"]) == ("1", "2", first, first)
    assert (three["seq"], three["corr"], three["call"]) == ("1", second, "other")
    assert PROFILE not in caplog.text and "CANARY" not in caplog.text


def test_a_quick_call_adds_no_line_at_the_default_level(caplog):
    with caplog.at_level(logging.INFO, logger="connection_hub.oauth.spans"):
        assert asyncio.run(in_custody(lambda: 7)) == 7
    assert _records(caplog) == []

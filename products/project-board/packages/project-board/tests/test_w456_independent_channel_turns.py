"""Each relay channel runs its own turn; one slow channel delays only itself (W456).

On 2026-10-01 one or two channels' attendance polls and Data Bus connects
took 30-86 s, and every other channel on the host waited for them: the cycle
gathered all due channels and ended with the slowest. A channel whose socket
dropped could reopen only in the next cycle, so its pb coordinate calls
failed with 90 s deadlines while the server answered in under a second.
"""

from __future__ import annotations

import asyncio
import time

from project_board.client import coordinate_queue

from relay_helpers import (
    Attendance as _Attendance,
    submit_request,
    supervisor_with_fake_channels as _supervisor_with_fake_channels,
    two_channel_host as _two_channel_host,
)


def _rows(result: dict) -> dict[str, dict]:
    return {row["worker_name"]: row for row in result["workers"]}


async def _cycle(supervisor, *, settle: float = 0.05) -> dict:
    """One relay cycle, then the short pause the host runtime takes."""

    result = await asyncio.wait_for(supervisor.poll_once(), timeout=2)
    await asyncio.sleep(settle)
    return result


def test_a_hung_turn_costs_other_channels_at_most_one_short_grace(tmp_path):
    host, fast, slow = _two_channel_host(tmp_path)

    async def scenario():
        attendance = _Attendance(slow.worker_name)
        supervisor, _opened = _supervisor_with_fake_channels(host, attendance)
        grace = supervisor.CHANNEL_TURN_CYCLE_GRACE_SECONDS
        assert 0 < grace <= 0.5
        try:
            started = time.monotonic()
            first = await asyncio.wait_for(supervisor.poll_once(), timeout=2)
            first_seconds = time.monotonic() - started
            started = time.monotonic()
            second = await asyncio.wait_for(supervisor.poll_once(), timeout=2)
            second_seconds = time.monotonic() - started
            await _cycle(supervisor)
        finally:
            attendance.gate.set()
            await supervisor.aclose()
        assert first_seconds < grace + 0.3, f"the first cycle took {first_seconds:.2f}s"
        assert second_seconds < 0.3, "a later cycle never waits for the hung turn"
        assert attendance.polls[fast.worker_name] >= 2
        assert attendance.polls[slow.worker_name] == 1, "a running turn is not started twice"
        for result in (first, second):
            rows = _rows(result)
            assert rows[fast.worker_name]["state"] == "attending"
            assert rows[slow.worker_name]["state"] == "running"
            assert rows[slow.worker_name]["reason"] == "turn_in_progress"

    asyncio.run(scenario())


def test_a_turn_past_its_deadline_fails_alone_with_a_named_outcome(tmp_path):
    host, fast, slow = _two_channel_host(tmp_path)

    async def scenario():
        attendance = _Attendance(slow.worker_name)
        supervisor, _opened = _supervisor_with_fake_channels(host, attendance)
        supervisor.CHANNEL_TURN_DEADLINE_SECONDS = 0.2
        try:
            # The deadline ends inside the cycle's grace, so this cycle reports it.
            result = await _cycle(supervisor)
        finally:
            attendance.gate.set()
            await supervisor.aclose()
        rows = _rows(result)
        assert rows[fast.worker_name]["state"] == "attending"
        failed = rows[slow.worker_name]
        assert failed["state"] == "error"
        assert failed["error_code"] == "work_relay_channel_turn_deadline_exceeded"
        assert failed["retryable"] is True
        assert slow.worker_name not in supervisor._sessions, "the hung session is dropped"
        assert supervisor._pacing.channel_due(fast.worker_name)
        assert not supervisor._pacing.channel_due(slow.worker_name), "only the hung channel backs off"

    asyncio.run(scenario())


def test_the_deadline_covers_the_turns_own_finishing(tmp_path):
    """A wake that hangs after a quick poll is cut by the turn's deadline."""

    host, fast, slow = _two_channel_host(tmp_path)

    async def scenario():
        attendance = _Attendance(slow.worker_name)
        attendance.gate.set()
        supervisor, _opened = _supervisor_with_fake_channels(host, attendance)
        supervisor.CHANNEL_TURN_DEADLINE_SECONDS = 0.05
        hung_wake = asyncio.Event()

        async def notify(_host, channel):
            if channel.worker_name == fast.worker_name:
                await hung_wake.wait()
            return None

        supervisor._notify_available_input = notify
        try:
            # The deadline ends inside the cycle's grace, so this cycle reports it.
            result = await _cycle(supervisor)
            turn = supervisor._channel_turns.get(fast.worker_name)
            assert turn is None or turn.done(), "the turn outlived its deadline"
        finally:
            hung_wake.set()
            await supervisor.aclose()
        rows = _rows(result)
        assert rows[fast.worker_name]["error_code"] == (
            "work_relay_channel_turn_deadline_exceeded"
        )
        assert rows[slow.worker_name]["state"] == "attending"
        assert not supervisor._notify_lock(fast.worker_name).locked()

    asyncio.run(scenario())


def test_a_failed_late_turn_wakes_the_next_cycle_and_a_success_does_not(tmp_path):
    host, _fast, slow = _two_channel_host(tmp_path)

    async def scenario():
        attendance = _Attendance(slow.worker_name, slow_fails=True)
        supervisor, _opened = _supervisor_with_fake_channels(host, attendance)
        try:
            await _cycle(supervisor)
            started = time.monotonic()
            quiet = await supervisor.wait_for_wakeup(0.3)
            quiet_seconds = time.monotonic() - started
            await _cycle(supervisor)
            attendance.gate.set()
            started = time.monotonic()
            stopping = await asyncio.wait_for(
                supervisor.wait_for_wakeup(30.0), timeout=5
            )
            woke_after = time.monotonic() - started
            result = await _cycle(supervisor)
        finally:
            await supervisor.aclose()
        assert quiet is False and quiet_seconds >= 0.25, (
            "successful turns do not wake the loop"
        )
        assert stopping is False
        assert woke_after < 2.0, f"the failed turn woke the loop after {woke_after:.2f}s"
        assert _rows(result)[slow.worker_name]["state"] == "error"

    asyncio.run(scenario())


def test_a_dropped_channel_reopens_and_serves_its_calls_while_another_hangs(tmp_path):
    """At production defaults, with the host runtime's own loop around poll_once."""

    host, fast, slow = _two_channel_host(tmp_path)
    queue = coordinate_queue.CoordinateQueue(host.field_root)

    async def scenario():
        attendance = _Attendance(slow.worker_name)
        supervisor, opened = _supervisor_with_fake_channels(host, attendance)

        async def run_relay():
            # What the host relay runtime does: a cycle, a short wait, again.
            while True:
                await supervisor.poll_once()
                await asyncio.sleep(0.05)

        relay_loop = asyncio.create_task(run_relay())
        try:
            await asyncio.wait_for(attendance.polled.wait(), timeout=2)
            while attendance.polls.get(fast.worker_name, 0) < 1:
                await asyncio.sleep(0.01)
            # The fast channel's socket drops while the slow channel hangs.
            await supervisor._drop_session(fast.worker_name)
            request = submit_request(queue, fast)
            started = time.monotonic()
            response = None
            while response is None and time.monotonic() - started < 2.0:
                await asyncio.sleep(0.02)
                response = queue.take_response(
                    worker_name=fast.worker_name, request_id=request["request_id"]
                )
            served_after = time.monotonic() - started
        finally:
            relay_loop.cancel()
            await asyncio.gather(relay_loop, return_exceptions=True)
            attendance.gate.set()
            await supervisor.aclose()
        assert response is not None and response["ok"] is True, (
            "the dropped channel's call waited on the hung channel"
        )
        assert served_after < 2.0
        assert opened.count(fast.worker_name) >= 2, "the dropped channel reopened"
        assert attendance.polls[slow.worker_name] == 1

    asyncio.run(scenario())


def test_a_one_shot_cycle_reports_its_finished_turns(tmp_path):
    """`pb relay --once` waits for its turns, so the probe shows real outcomes."""

    host, fast, slow = _two_channel_host(tmp_path)

    async def scenario():
        attendance = _Attendance(slow.worker_name)
        attendance.gate.set()
        supervisor, _opened = _supervisor_with_fake_channels(host, attendance)
        supervisor.CHANNEL_TURN_CYCLE_GRACE_SECONDS = 2.0
        try:
            result = await asyncio.wait_for(supervisor.poll_once(), timeout=3)
        finally:
            await supervisor.aclose()
        rows = _rows(result)
        assert rows[fast.worker_name]["state"] == "attending"
        assert rows[slow.worker_name]["state"] == "attending"

    asyncio.run(scenario())

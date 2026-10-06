"""W456 criterion 6: a slow turn names its stage; each channel shows its last poll, success and backoff.

Ops' review of 2026-10-05 12:05Z: on spark1 the 62 slow-turn warnings since the
restart named the channel but no stage (0 `stage=` fields), and `pb worker
inspect` showed no per-channel backoff, last attendance poll or last success.
"""

from __future__ import annotations

import asyncio
import logging

from project_board.client import relay_channel_status
from project_board.client.relay_channel_status import ChannelStatusBook, channel_status
from project_board.client.relay_trace import RelayActivityTrace
from project_board.client.render import _relay_turns_line

from relay_helpers import (
    Attendance,
    supervisor_with_fake_channels,
    two_channel_host,
)


def test_the_slowest_stage_of_the_running_turn_is_named():
    now = [100.0]
    trace = RelayActivityTrace(monotonic=lambda: now[0], wall_clock=lambda: now[0])

    async def turn():
        state = trace.begin_turn("worker-a")
        try:
            with trace.stage("channel.open", channel="worker-a", operation="data_bus.connect"):
                now[0] += 1.5
            with trace.stage("attendance.poll", channel="worker-a", operation="attendance.reconcile"):
                now[0] += 4.0
                running = trace.slowest_turn_stage()
            return running, trace.slowest_turn_stage()
        finally:
            trace.end_turn(state, "succeeded")

    (running, finished) = asyncio.run(turn())
    assert running == ("attendance.poll", 4.0), "a stage still running counts with its time so far"
    assert finished == ("attendance.poll", 4.0)
    assert trace.slowest_turn_stage() == ("", 0.0), "outside a turn nothing is named"


def test_a_slow_turn_warning_names_the_stage_that_held_it(tmp_path, caplog):
    host, fast, slow = two_channel_host(tmp_path)

    async def scenario():
        attendance = Attendance(slow.worker_name)
        supervisor, _opened = supervisor_with_fake_channels(host, attendance)
        supervisor.CHANNEL_TURN_DEADLINE_SECONDS = 0.2
        supervisor._trace.slow_seconds = 0.1
        try:
            await asyncio.wait_for(supervisor.poll_once(), timeout=2)
            await asyncio.sleep(0.3)
        finally:
            attendance.gate.set()
            await supervisor.aclose()

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    lines = [r.getMessage() for r in caplog.records if "slow channel turn" in r.getMessage()]
    held = [line for line in lines if f"worker={slow.worker_name}" in line]
    assert held, lines
    assert "stage=attendance.poll" in held[0], held[0]
    assert "outcome=failed:work_relay_channel_turn_deadline_exceeded" in held[0]
    assert not [line for line in lines if f"worker={fast.worker_name}" in line]


def test_each_channels_last_poll_success_and_outcome_reach_its_readers(tmp_path, monkeypatch):
    monkeypatch.setattr(relay_channel_status, "WRITE_INTERVAL_SECONDS", 0.0)
    host, fast, slow = two_channel_host(tmp_path)

    async def scenario():
        attendance = Attendance(slow.worker_name)
        supervisor, _opened = supervisor_with_fake_channels(host, attendance)
        supervisor.CHANNEL_TURN_DEADLINE_SECONDS = 0.2
        try:
            await asyncio.wait_for(supervisor.poll_once(), timeout=2)
            await asyncio.sleep(0.4)
            for _ in range(3):
                if supervisor._channel_status_write is not None:
                    await supervisor._channel_status_write
                supervisor._write_channel_status_soon()
        finally:
            attendance.gate.set()
            await supervisor.aclose()
        if supervisor._channel_status_write is not None:
            await supervisor._channel_status_write

    asyncio.run(scenario())
    served = channel_status(host.path, fast.worker_name)
    assert served["last_outcome"] == "succeeded" and served["last_code"] == ""
    assert served["last_attendance_poll_at"] and served["last_success_at"] and served["recorded_at"]
    held = channel_status(host.path, slow.worker_name)
    assert held["last_outcome"] == "deadline"
    assert held["last_code"] == "work_relay_channel_turn_deadline_exceeded"
    assert held["last_attendance_poll_at"] and held["last_success_at"] == ""


def test_the_book_is_written_at_most_once_per_interval():
    now = [1000.0]
    book = ChannelStatusBook(clock=lambda: now[0])
    assert not book.due(), "nothing changed, nothing to write"
    book.note_turn("a", "succeeded")
    assert book.due()
    book.take_snapshot()
    book.note_turn("a", "failed", "code")
    assert not book.due(), "written moments ago"
    now[0] += relay_channel_status.WRITE_INTERVAL_SECONDS
    assert book.due()


def test_inspect_prints_the_last_poll_success_and_backoff():
    assert _relay_turns_line(None) == "relay turns: not recorded yet"
    line = _relay_turns_line({
        "last_attendance_poll_at": "2026-10-05T12:00:00Z", "last_success_at": "2026-10-05T11:59:00Z",
        "last_outcome": "failed", "last_code": "work_relay_channel_open_failed",
        "recorded_at": "2026-10-05T12:00:05Z",
        "backoff": {"attempts": 3, "next_attempt_at": "2026-10-05T12:02:00Z", "reason": "profile_not_found"},
    })
    assert line == (
        "relay turns: last attendance poll 2026-10-05T12:00:00Z · last success 2026-10-05T11:59:00Z"
        " · last outcome failed (work_relay_channel_open_failed)"
        " · backoff attempt 3 · next 2026-10-05T12:02:00Z · profile_not_found · recorded 2026-10-05T12:00:05Z"
    )

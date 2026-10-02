"""The relay's local-work scans never pile up (W459).

On dev-main on 2026-10-02, after the fd7c266 and 42adcd8 activations, all 20
default-pool threads of the relay were in directory scans of a 70,009-file
field, most of their time waiting for the interpreter lock. Each
``wait_for_wakeup`` starts a local-work waiter whose scans run in worker
threads. When any other waiter fires, or the wait times out, the local-work
waiter is cancelled, but a scan already in its thread runs on, and the next
wait starts new scans beside it.

These tests run the real ``wait_for_wakeup`` repeatedly while every scan is
held by the test, and check, before the scans are released, how many run at
once and that the relay's own cycle still serves a coordinate call.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

from project_board.client import coordinate_queue, relay
from project_board.client.coordinate_queue import CoordinateQueue
from project_board.client.outbox_store import OutboxStore

from relay_helpers import (
    Attendance,
    LoopHeartbeat,
    submit_request,
    supervisor_with_fake_channels,
    two_channel_host,
)

GATE_LIMIT_SECONDS = 4.0
WAITS = 10
WAIT_SECONDS = 0.2
PEER_LIMIT_SECONDS = 2.0


class ScanGate:
    """Every local-work scan made in a worker thread waits until the test opens the gate.

    A scan the relay makes on its own event loop (the coordinate serve loop
    reads the queue there) is not held, so the relay keeps serving.
    """

    def __init__(self, monkeypatch, *, error: Exception | None = None) -> None:
        self.opened = threading.Event()
        self.error = error
        self._lock = threading.Lock()
        self.running = 0
        self.max_running = 0
        self.started = 0
        self.finished = 0
        for owner, name in (
            (CoordinateQueue, "has_ready_work"),
            (OutboxStore, "ready_signature"),
            (relay.ProblemBoardRelaySupervisor, "_local_work_signature"),
        ):
            monkeypatch.setattr(owner, name, self._held(getattr(owner, name)))

    def _held(self, real):
        gate = self

        def scan(*args, **kwargs):
            if threading.current_thread() is threading.main_thread():
                return real(*args, **kwargs)
            with gate._lock:
                gate.running += 1
                gate.started += 1
                gate.max_running = max(gate.max_running, gate.running)
            try:
                gate.opened.wait(GATE_LIMIT_SECONDS)
                if gate.error is not None:
                    raise gate.error
                return real(*args, **kwargs)
            finally:
                with gate._lock:
                    gate.running -= 1
                    gate.finished += 1

        return scan

    async def wait_started(self) -> None:
        deadline = time.monotonic() + GATE_LIMIT_SECONDS + 1
        while self.started < 1:
            assert time.monotonic() < deadline, "no scan started"
            await asyncio.sleep(0.01)

    async def wait_idle(self) -> None:
        deadline = time.monotonic() + GATE_LIMIT_SECONDS + 1
        while self.running:
            assert time.monotonic() < deadline, "a scan never ended"
            await asyncio.sleep(0.01)


def test_repeated_waits_with_held_scans_run_one_scan_and_keep_the_relay_serving(tmp_path, monkeypatch):
    host, fast, _slow = two_channel_host(tmp_path)
    queue = coordinate_queue.CoordinateQueue(host.field_root)
    gate = ScanGate(monkeypatch)

    async def scenario():
        attendance = Attendance("no-channel-hangs-its-poll")
        supervisor, _opened = supervisor_with_fake_channels(host, attendance)

        async def run_relay():
            while True:
                await supervisor.poll_once()
                await asyncio.sleep(0.05)

        relay_loop = asyncio.create_task(run_relay())
        beat = LoopHeartbeat()
        beat.start()
        try:
            # What the host runtime does between cycles: wait, give up, wait again.
            for _ in range(WAITS):
                assert await supervisor.wait_for_wakeup(WAIT_SECONDS) is False
            await gate.wait_started()
            request = submit_request(queue, fast)
            started = time.monotonic()
            response = None
            while response is None and time.monotonic() - started < PEER_LIMIT_SECONDS:
                await asyncio.sleep(0.02)
                response = queue.take_response(
                    worker_name=fast.worker_name, request_id=request["request_id"]
                )
            served_after = time.monotonic() - started
            held = not gate.opened.is_set()
            running_while_held = gate.running
            started_while_held = gate.started
            await beat.stop()
            gate.opened.set()
            await gate.wait_idle()
        finally:
            gate.opened.set()
            relay_loop.cancel()
            await asyncio.gather(relay_loop, return_exceptions=True)
            await supervisor.aclose()
        return (held, running_while_held, started_while_held, response, served_after,
                beat.max_gap)

    held, running, started, response, served_after, max_gap = asyncio.run(scenario())

    assert held, "the counts were taken while every scan was still held"
    assert running <= 1 and gate.max_running <= 1, (
        f"{gate.max_running} scans ran at once after {WAITS} waits"
    )
    assert started <= 1, f"{started} scans started while the first was held"
    assert response is not None and response["ok"] is True and served_after < PEER_LIMIT_SECONDS, (
        "the relay's own cycle kept serving coordinate calls"
    )
    assert max_gap < 0.3, f"the event loop stalled {max_gap:.2f}s"


def test_a_scan_finished_after_its_wait_ended_still_serves_the_next_wait(tmp_path, monkeypatch):
    """Late completion: the next wait reads local work at once, and a new local change wakes it."""

    host, _fast, _slow = two_channel_host(tmp_path)
    gate = ScanGate(monkeypatch)

    async def scenario():
        attendance = Attendance("no-channel-hangs-its-poll")
        supervisor, _opened = supervisor_with_fake_channels(host, attendance)
        try:
            assert await supervisor.wait_for_wakeup(WAIT_SECONDS) is False
            await gate.wait_started()
            gate.opened.set()
            await gate.wait_idle()
            responses = Path(host.field_root) / ".problem-board" / "operator-responses"
            waiting = asyncio.create_task(supervisor.wait_for_wakeup(30.0))
            await asyncio.sleep(0.5)
            responses.mkdir(parents=True, exist_ok=True)
            (responses / "response-1.json").write_text("{}", encoding="utf-8")
            started = time.monotonic()
            woke = await asyncio.wait_for(waiting, timeout=5)
            return woke, time.monotonic() - started
        finally:
            await supervisor.aclose()

    woke, woke_after = asyncio.run(scenario())

    assert woke is False, "a local wake is not a stop"
    assert woke_after < 1.0, f"a local operator response woke the relay after {woke_after:.2f}s"


def test_closing_the_relay_during_a_held_scan_starts_no_further_scan(tmp_path, monkeypatch):
    host, _fast, _slow = two_channel_host(tmp_path)
    gate = ScanGate(monkeypatch)

    async def scenario():
        attendance = Attendance("no-channel-hangs-its-poll")
        supervisor, _opened = supervisor_with_fake_channels(host, attendance)
        assert await supervisor.wait_for_wakeup(WAIT_SECONDS) is False
        await gate.wait_started()
        started = time.monotonic()
        await asyncio.wait_for(supervisor.aclose(), timeout=PEER_LIMIT_SECONDS)
        closed_after = time.monotonic() - started
        started_at_close = gate.started
        gate.opened.set()
        await gate.wait_idle()
        await asyncio.sleep(0.3)
        return closed_after, started_at_close, gate.started

    closed_after, started_at_close, started_after = asyncio.run(scenario())

    assert closed_after < PEER_LIMIT_SECONDS, "closing does not wait for a read-only scan"
    assert started_after == started_at_close, "no scan starts after the relay closed"
    assert gate.running == 0


def test_a_failing_scan_ends_the_wait_as_before(tmp_path, monkeypatch):
    host, _fast, _slow = two_channel_host(tmp_path)
    gate = ScanGate(monkeypatch, error=OSError("field unreadable"))
    gate.opened.set()

    async def scenario():
        attendance = Attendance("no-channel-hangs-its-poll")
        supervisor, _opened = supervisor_with_fake_channels(host, attendance)
        try:
            started = time.monotonic()
            woke = await asyncio.wait_for(supervisor.wait_for_wakeup(30.0), timeout=5)
            return woke, time.monotonic() - started
        finally:
            await supervisor.aclose()

    woke, ended_after = asyncio.run(scenario())

    assert woke is False and ended_after < 1.0, "a failed scan ends the wait instead of hanging it"
    assert gate.started >= 1 and gate.running == 0

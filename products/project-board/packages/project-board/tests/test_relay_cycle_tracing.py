from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from project_board.client import coordinate_queue, relay
from project_board.client.io import atomic_write_json, read_json
from project_board.client.relay_trace import RelayActivityTrace
from project_board.client.store import SharedFieldStore

from relay_helpers import StableClient, make_host, make_supervisor, submit_request


class _Clock:
    def __init__(self, epoch: float) -> None:
        self.monotonic_value = 0.0
        self.epoch_value = epoch

    def monotonic(self) -> float:
        return self.monotonic_value

    def time(self) -> float:
        return self.epoch_value

    def advance(self, seconds: float) -> None:
        self.monotonic_value += seconds
        self.epoch_value += seconds


def _trace(clock: _Clock) -> RelayActivityTrace:
    return RelayActivityTrace(
        slow_seconds=5.0,
        monotonic=clock.monotonic,
        wall_clock=clock.time,
        log=logging.getLogger("test.relay.trace"),
    )


def _warning_payload(caplog, marker: str) -> list[dict[str, object]]:
    message = next(record.message for record in caplog.records if marker in record.message)
    return json.loads(message.split(marker, 1)[1])


def _utc(epoch: float) -> str:
    return (
        datetime.fromtimestamp(epoch, timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def test_slow_supervisor_cycle_logs_per_channel_stage(
    tmp_path, monkeypatch, caplog
) -> None:
    host, _identity, _channel = make_host(tmp_path)
    clock = _Clock(time.time())
    trace = _trace(clock)
    supervisor = make_supervisor(host)
    supervisor._trace = trace

    async def slow_cycle():
        with trace.stage(
            "attendance.poll",
            channel="worker-one",
            operation="attendance.reconcile",
        ):
            clock.advance(5.25)
        return {"workers": []}

    monkeypatch.setattr(supervisor, "_poll_once_body", slow_cycle)
    caplog.set_level(logging.WARNING, logger="test.relay.trace")

    assert asyncio.run(supervisor.poll_once()) == {"workers": []}

    stages = _warning_payload(caplog, "stages=")
    assert stages == [
        {
            "channel": "worker-one",
            "operation": "attendance.reconcile",
            "outcome": "succeeded",
            "seconds": 5.25,
            "stage": "attendance.poll",
        }
    ]


def test_short_cycle_emits_no_slow_warning(caplog) -> None:
    clock = _Clock(time.time())
    trace = _trace(clock)
    caplog.set_level(logging.WARNING, logger="test.relay.trace")

    cycle = trace.start_cycle()
    with trace.stage("host.load", operation="relay.config"):
        clock.advance(4.999)
    trace.finish_cycle(cycle, outcome="succeeded")

    assert not caplog.records


def test_slow_coordinate_wait_names_overlapping_relay_work(
    tmp_path, caplog
) -> None:
    host, _identity, channel = make_host(tmp_path)
    queue = coordinate_queue.CoordinateQueue(host.field_root)
    request = submit_request(queue, channel)
    started_at = time.time() - 6.5
    request_path = queue._path(
        "pending",
        channel.worker_name,
        request["request_id"],
    )
    stored = read_json(request_path)
    stored["created_at"] = _utc(started_at)
    stored["expires_at"] = _utc(time.time() + 60.0)
    atomic_write_json(request_path, stored)

    clock = _Clock(started_at)
    trace = _trace(clock)
    supervisor = make_supervisor(host)
    supervisor._trace = trace
    session = SimpleNamespace(adapter=SimpleNamespace(client=StableClient()))
    caplog.set_level(logging.WARNING)

    with trace.stage(
        "attendance.poll",
        channel="worker-blocking-the-cycle",
        operation="worker.heartbeat",
    ):
        clock.advance(6.5)
        counts = asyncio.run(
            supervisor._drain_coordinate_requests(host, channel, session)
        )

    assert counts["completed"] == 1
    waited_on = _warning_payload(caplog, "waited_on=")
    assert len(waited_on) == 1
    stage = waited_on[0]
    assert stage["channel"] == "worker-blocking-the-cycle"
    assert stage["cycle"] == 0
    assert stage["operation"] == "worker.heartbeat"
    assert stage["stage"] == "attendance.poll"
    assert 6.5 <= stage["overlap_seconds"] < 6.6


def test_startup_recovery_is_a_named_cycle_stage(
    tmp_path, monkeypatch, caplog
) -> None:
    host, _identity, channel = make_host(tmp_path)
    clock = _Clock(time.time())
    trace = _trace(clock)
    adapter = relay.ProblemBoardHostRelayAdapter(
        config=relay.RelayConfig.from_host_channel(
            host,
            channel,
            project_id="project",
        ),
        field=SharedFieldStore(host.field_root),
        client=StableClient(),
        trace=trace,
    )

    def recover(_recipients):
        clock.advance(5.5)
        return {"state": "complete"}

    monkeypatch.setattr(adapter, "_reconcile_project_mailboxes", recover)
    caplog.set_level(logging.WARNING, logger="test.relay.trace")
    cycle = trace.start_cycle()

    result = asyncio.run(adapter._run_startup_recovery([]))
    trace.finish_cycle(cycle, outcome="succeeded")

    assert result == {"state": "complete"}
    stages = _warning_payload(caplog, "stages=")
    assert stages == [
        {
            "channel": channel.worker_name,
            "operation": "mailbox.reconciliation",
            "outcome": "succeeded",
            "seconds": 5.5,
            "stage": "startup_recovery",
        }
    ]

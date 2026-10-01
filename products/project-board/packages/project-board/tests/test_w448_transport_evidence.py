"""W448: a governed request's failure says how its wait ended, and the relay
measures its own event-loop lag.

One host's relay logs (2026-10-01, 07:36-11:04Z) showed 147 ingress.ack
timeouts against a 15 s deadline, 99 of them firing more than 3 s late and
one 102 s late, while the recorded evidence described the socket only as it
was when the request began. These fields let the next wave say whether the
transport dropped during the wait and whether the relay itself stalled.
"""

from __future__ import annotations

import asyncio
import logging
import time

from project_board.client import relay_trace
from project_board.client.relay import ProblemBoardRelaySupervisor
from project_board.client.relay_trace import RelayActivityTrace, process_memory
from project_board.client.store import SharedFieldStore
from project_board.contract.errors import DomainError

from relay_helpers import make_host


WAIT_EVIDENCE = {
    "message_id": "message-1",
    "ingress_ack_received": False,
    "connection_generation": 3,
    "socket_id": "socketio-3",
    "connection_active": True,
    "connection_generation_at_failure": 4,
    "socket_id_at_failure": "socketio-4",
    "connection_active_at_failure": True,
    "disconnected_during_request": True,
    "timeout_seconds": 15.0,
    "timer_overrun_seconds": 6.987,
    "transport_message_id": "message-1",
    "transport_phase": "ingress.ack",
    "elapsed_seconds": 21.987,
}
NEW_KEYS = (
    "ingress_ack_received",
    "connection_generation_at_failure",
    "socket_id_at_failure",
    "connection_active_at_failure",
    "disconnected_during_request",
    "timeout_seconds",
    "timer_overrun_seconds",
)


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value


def test_failure_evidence_keeps_how_the_wait_ended() -> None:
    error = DomainError(
        "data_bus_outcome_unknown", "timed out", status=504, details=WAIT_EVIDENCE
    )

    evidence = ProblemBoardRelaySupervisor._failure_evidence(error)

    for key in NEW_KEYS:
        assert evidence[key] == WAIT_EVIDENCE[key], key
    # The request-start values stay beside them, under their old names.
    assert evidence["connection_active"] is True
    assert evidence["connection_generation"] == 3
    assert "accepted" not in evidence


def test_the_degraded_channel_record_keeps_the_wait_evidence(tmp_path) -> None:
    host, identity, _channel = make_host(tmp_path)
    field = SharedFieldStore(host.field_root)
    field.register_worker(
        worker_name=identity.worker_name,
        worker_alias="worker",
        worker_identity=identity.worker_identity,
        runtime_kind=identity.runtime_kind,
        runtime_session_id=identity.runtime_session_id,
        capabilities=[],
        authority_label="connection-hub:test",
        host_id=host.host_id,
        host_label=host.host_label,
        host_kind=host.host_kind,
        relay_id=host.relay_id,
    )
    error = DomainError(
        "data_bus_outcome_unknown", "timed out", status=504, details=WAIT_EVIDENCE
    )

    for _ in range(2):
        field.record_relay_channel_degraded(
            identity.worker_name,
            code="data_bus_outcome_unknown",
            request=ProblemBoardRelaySupervisor._failure_evidence(error),
        )

    diagnostic = field.read_worker(identity.worker_name)["relay_diagnostic"]
    for key in NEW_KEYS:
        assert diagnostic["request"][key] == WAIT_EVIDENCE[key], key
        assert diagnostic["attempts"][-1]["request"][key] == WAIT_EVIDENCE[key], key


def test_a_stall_is_logged_at_once_then_at_most_every_thirty_seconds(caplog) -> None:
    clock = _Clock()
    trace = RelayActivityTrace(monotonic=clock.monotonic, log=logging.getLogger("t.w448"))
    caplog.set_level(logging.WARNING, logger="t.w448")

    trace.record_loop_lag(0.2)
    assert caplog.records == []
    trace.record_loop_lag(7.0)
    clock.value = 10.0
    trace.record_loop_lag(2.0)
    trace.record_loop_lag(3.5)
    clock.value = 31.0
    trace.record_loop_lag(1.5)

    lines = [record.message for record in caplog.records]
    assert len(lines) == 2
    assert "relay loop stalled max_lag_seconds=7.000 stalls=1 " in lines[0]
    # The stalls held back inside the window are counted in the next line.
    assert "relay loop stalled max_lag_seconds=3.500 stalls=3 " in lines[1]


def test_the_cycle_summary_and_slow_cycle_line_carry_the_largest_loop_lag(caplog) -> None:
    clock = _Clock()
    trace = RelayActivityTrace(
        slow_seconds=5.0, monotonic=clock.monotonic, log=logging.getLogger("t.w448")
    )
    caplog.set_level(logging.WARNING, logger="t.w448")

    cycle = trace.start_cycle()
    trace.record_loop_lag(0.4)
    trace.record_loop_lag(0.9)
    clock.value = 6.0
    summary = trace.finish_cycle(cycle, outcome="succeeded")

    assert summary["loop_lag_max_seconds"] == 0.9
    slow = next(r.message for r in caplog.records if "relay slow cycle" in r.message)
    assert "loop_lag_max_seconds=0.900" in slow
    for key in process_memory():
        assert f" {key}=" in slow, key
    # The window restarts with each cycle.
    cycle = trace.start_cycle()
    assert trace.finish_cycle(cycle, outcome="succeeded")["loop_lag_max_seconds"] == 0.0


def test_the_sampler_measures_a_blocked_event_loop(monkeypatch) -> None:
    trace = RelayActivityTrace(log=logging.getLogger("t.w448"))
    monkeypatch.setattr(relay_trace, "LOOP_STALL_SECONDS", 10.0)

    async def run() -> None:
        sampler = asyncio.create_task(trace.sample_loop_lag(0.05))
        await asyncio.sleep(0.01)
        time.sleep(0.3)  # a synchronous stall: the loop cannot run the sampler
        await asyncio.sleep(0.1)
        sampler.cancel()
        await asyncio.gather(sampler, return_exceptions=True)

    asyncio.run(run())

    cycle = trace.start_cycle()
    assert trace.finish_cycle(cycle, outcome="succeeded")["loop_lag_max_seconds"] >= 0.2


def test_process_memory_reports_only_what_the_platform_gives() -> None:
    memory = process_memory()
    assert set(memory) <= {"rss_bytes", "rss_peak_bytes"}
    assert all(isinstance(value, int) and value > 0 for value in memory.values())

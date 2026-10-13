from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import uuid

import pytest

from app_foundation.data_bus.timing import RequestTiming, RequestTimingPhase
from project_board.client import message_timing as timing
from project_board.client.cli import build_parser


def enabled():
    return timing.MessageTimingConfig(True, (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat())


def event(request_id, **changes):
    return replace(RequestTiming(request_id, RequestTimingPhase.START, datetime.now(timezone.utc).isoformat(), 0.01), **changes)


class Owner:
    def __init__(self, accepted=True):
        self.records = []
        self.accepted = accepted

    def submit_record(self, record):
        self.records.append(record)
        return self.accepted


@pytest.fixture
def owner(monkeypatch):
    value = Owner()
    monkeypatch.setattr("project_board.client.relay_logging.relay_log_owner", lambda: value)
    return value


def test_default_off_does_not_read_clock_or_submit(monkeypatch, owner):
    gate = timing.MessageTimingGate()
    monkeypatch.setattr(timing.time, "time", lambda: pytest.fail("disabled gate read clock"))
    assert gate.observer("worker.heartbeat", str(uuid.uuid4())) is None
    assert gate.sampled_requests == 0
    assert not owner.records


@pytest.mark.parametrize("value", [True, {"enabled": "yes"}, {"payload": "marker"}, {"enabled": True}, {"enabled": True, "expires_at": "2030-01-01T00:00:00Z"}, {"enabled": True, "expires_at": "2026-01-01T00:00:00"}])
def test_config_is_typed_and_expiry_is_bounded(value):
    with pytest.raises(ValueError):
        timing.MessageTimingConfig.from_mapping(value)


def test_supported_host_configuration_flags():
    parser = build_parser()
    assert parser.parse_args(["host", "configure", "--message-timing-off"]).message_timing_off
    assert parser.parse_args(["host", "configure", "--message-timing-until", "2026-10-13T00:00:00Z"]).message_timing_until.endswith("Z")


def test_explicit_allowlist_and_same_uuid_retry_attempts(owner):
    gate = timing.MessageTimingGate()
    gate.configure(enabled())
    request_id = str(uuid.uuid4())
    for _ in range(2):
        observer = gate.observer("worker.heartbeat", request_id)
        observer(event(request_id))
    rows = [json.loads(record.msg) for record in owner.records]
    assert [row["attempt"] for row in rows] == [1, 2]
    assert set(rows[0]) == {"schema", "phase", "operation", "message_id", "attempt", "utc", "elapsed_seconds", "dropped_records", "dropped_bytes"}
    assert all(len(record.msg.encode()) + timing.PREFIX_BYTES <= 512 for record in owner.records)
    assert gate.observer("arbitrary.marker.operation", request_id) is None
    assert gate.observer("control.pull", "marker-not-a-uuid") is None


@pytest.mark.parametrize("changes", [{"message_id": "marker"}, {"phase": "marker"}, {"utc": "marker"}, {"elapsed_seconds": float("nan")}, {"elapsed_seconds": -1}, {"elapsed_seconds": "marker"}])
def test_negative_records_never_reach_sink(changes, owner):
    gate = timing.MessageTimingGate()
    gate.configure(enabled())
    request_id = str(uuid.uuid4())
    gate.observer("control.pull", request_id)(event(request_id, **changes))
    assert not owner.records
    assert gate.dropped_records == 1


def test_process_request_and_byte_budgets_survive_reconfigure(owner):
    gate = timing.MessageTimingGate()
    gate.configure(enabled())
    request_id = str(uuid.uuid4())
    observer = gate.observer("control.pull", request_id)
    for _ in range(1000):
        observer(event(request_id))
    assert gate.admitted_bytes <= 256 * 1024
    assert gate.dropped_records > 0
    for _ in range(110):
        gate.observer("control.pull", str(uuid.uuid4()))
    assert gate.sampled_requests == 100
    gate.configure(timing.MessageTimingConfig())
    gate.configure(enabled())
    assert gate.observer("control.pull", str(uuid.uuid4())) is None


def test_expiry_and_busy_admission_do_not_wait(owner, monkeypatch):
    gate = timing.MessageTimingGate()
    gate.configure(enabled())
    request_id = str(uuid.uuid4())
    observer = gate.observer("control.pull", request_id)
    with gate._lock:
        observer(event(request_id))
        assert gate.observer("control.pull", request_id) is None
    assert gate.dropped_records == 2
    monkeypatch.setattr(timing.time, "monotonic", lambda: gate._deadline + 1)
    observer(event(request_id))
    assert gate.observer("control.pull", request_id) is None
    assert not owner.records


def test_actual_submit_refusal_is_counted(owner):
    owner.accepted = False
    gate = timing.MessageTimingGate()
    gate.configure(enabled())
    request_id = str(uuid.uuid4())
    gate.observer("control.pull", request_id)(event(request_id))
    assert len(owner.records) == 1
    assert gate.dropped_records == 1
    assert gate.dropped_bytes > 0

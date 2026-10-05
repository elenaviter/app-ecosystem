"""The five dev-main loop blockers of 2026-10-05 run off the event loop (W456 criterion 4).

The dev-main relay (client 97729734) logged eight `relay loop blocked` lines
between 11:20 and 12:11Z. Six named project code on the loop:
- the outbox server's ready-row scan (outbox_store.in_flight, 3.6 s, twice);
- the flush's claim listing (pull_outbox -> in_flight, 3.5 s);
- a channel turn's relay-fault check (consume_relay_fault -> exclusive_lock, 4.4 s);
- the local-work wait's queue construction (CoordinateQueue -> Path.resolve, 3.7 s);
- a channel turn's Card check (_session_matches -> _card_fingerprint read_text, 3.2 s).
Each test makes that one call slow and holds the loop to a short gap.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from project_board.client import coordinate_queue, outbox_store, relay
from project_board.client.io import exclusive_lock
from project_board.client.store import SharedFieldStore

from relay_helpers import Attendance, LoopHeartbeat, make_host, make_supervisor, supervisor_with_fake_channels, two_channel_host

SLOW = 1.0
GAP = 0.5


def _slow(original):
    def call(*args, **kwargs):
        time.sleep(SLOW)
        return original(*args, **kwargs)

    return call


async def _watched(coroutine):
    beat = LoopHeartbeat()
    beat.start()
    started = time.monotonic()
    result = await coroutine
    waited = time.monotonic() - started
    await beat.stop()
    return result, waited, beat.max_gap


def test_the_outbox_servers_ready_scan_runs_off_the_loop(tmp_path, monkeypatch):
    host, fast, slow = two_channel_host(tmp_path)

    async def scenario():
        attendance = Attendance(slow.worker_name)
        attendance.gate.set()
        supervisor, _opened = supervisor_with_fake_channels(host, attendance)
        try:
            await supervisor._poll_channel(host, fast)  # opens the session
            monkeypatch.setattr(
                outbox_store.OutboxStore, "ready_project_refs",
                _slow(outbox_store.OutboxStore.ready_project_refs),
            )
            return await _watched(supervisor._outbox_server.serve_once_off_loop())
        finally:
            await supervisor.aclose()

    _started, waited, max_gap = asyncio.run(scenario())
    assert waited >= SLOW * 0.9, "the scan ran"
    assert max_gap < GAP, f"the loop stalled {max_gap:.2f}s in the outbox server's scan"


@pytest.fixture
def field(tmp_path: Path) -> SharedFieldStore:
    store = SharedFieldStore(tmp_path / "shared-field")
    store.initialize(field_id="test-field")
    store.register_worker(worker_name="codex-api", runtime_kind="codex", capabilities=[], authority_label="authority:codex-api")
    store.create_project(project_id="project-one", title="Outbox", goal="Keep the loop free.", owner="operator")
    return store


def _adapter(field: SharedFieldStore):
    adapter = object.__new__(relay.ProblemBoardHostRelayAdapter)
    adapter.field = field
    adapter.config = SimpleNamespace(relay_id="relay-01", worker_name="codex-api", project_id="project-one")
    adapter._outbox_finishes = set()
    return adapter


def test_the_flushs_claim_check_runs_off_the_loop(field, monkeypatch):
    field.enqueue_service_event(
        "project-one", worker_name="codex-api", kind="note.recorded", summary="note", source_event_ref="local:test:slow",
    )
    monkeypatch.setattr(outbox_store.OutboxStore, "has_in_flight", _slow(outbox_store.OutboxStore.has_in_flight))

    _counts, waited, max_gap = asyncio.run(_watched(_adapter(field)._flush_outbox_unlocked(kinds={"no-such-kind"})))
    assert waited >= SLOW * 0.9
    assert max_gap < GAP, f"the loop stalled {max_gap:.2f}s in the claim check"


def test_a_flush_with_nothing_in_flight_takes_no_lock(field):
    held = threading.Event()
    release = threading.Event()

    def hold():
        with exclusive_lock(field._outbox.lock):
            held.set()
            release.wait(5)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert held.wait(2)
    try:
        counts, waited, _gap = asyncio.run(_watched(_adapter(field)._flush_outbox_unlocked()))
    finally:
        release.set()
        holder.join()
    assert counts["outbox_sent"] == 0
    assert waited < 0.5, "an empty outbox does not wait for the outbox lock"


def test_a_channel_turns_fault_check_runs_off_the_loop(tmp_path, monkeypatch):
    host, fast, slow = two_channel_host(tmp_path)
    monkeypatch.setattr(relay, "consume_relay_fault", _slow(relay.consume_relay_fault))

    async def scenario():
        attendance = Attendance(slow.worker_name)
        attendance.gate.set()
        supervisor, _opened = supervisor_with_fake_channels(host, attendance)
        try:
            return await _watched(supervisor._poll_channel(host, fast))
        finally:
            await supervisor.aclose()

    _result, waited, max_gap = asyncio.run(scenario())
    assert waited >= SLOW * 0.9
    assert max_gap < GAP, f"the loop stalled {max_gap:.2f}s in the relay-fault check"


def test_a_channel_turns_card_check_runs_off_the_loop(tmp_path, monkeypatch):
    host, fast, slow = two_channel_host(tmp_path)

    async def scenario():
        attendance = Attendance(slow.worker_name)
        attendance.gate.set()
        supervisor, _opened = supervisor_with_fake_channels(host, attendance)
        try:
            await supervisor._poll_channel(host, fast)  # opens the session
            original = relay.ProblemBoardRelaySupervisor._card_fingerprint.__func__
            monkeypatch.setattr(
                relay.ProblemBoardRelaySupervisor, "_card_fingerprint",
                classmethod(_slow(original)),
            )
            return await _watched(supervisor._poll_channel(host, fast))
        finally:
            await supervisor.aclose()

    _result, waited, max_gap = asyncio.run(scenario())
    assert waited >= SLOW * 0.9
    assert max_gap < GAP, f"the loop stalled {max_gap:.2f}s in the Card check"


def test_the_local_work_waits_handles_are_built_off_the_loop_once(tmp_path, monkeypatch):
    host, _identity, channel = make_host(tmp_path)
    supervisor = make_supervisor(host)
    built = []
    original = coordinate_queue.CoordinateQueue.__init__

    def slow_init(self, field_root):
        # Slow only the first construction, the one the wait itself makes.
        built.append(field_root)
        if len(built) == 1:
            time.sleep(SLOW)
        original(self, field_root)

    monkeypatch.setattr(coordinate_queue.CoordinateQueue, "__init__", slow_init)

    async def scenario():
        first = await _watched(supervisor._wait_for_local_work(host.field_root, 0.1, worker_names=[channel.worker_name]))
        second = await _watched(supervisor._wait_for_local_work(host.field_root, 0.1, worker_names=[channel.worker_name]))
        return first, second

    (_woke, first_waited, first_gap), (_woke2, _second_waited, second_gap) = asyncio.run(scenario())
    assert first_waited >= SLOW * 0.9, "the first wait built the queue"
    assert first_gap < GAP, f"the loop stalled {first_gap:.2f}s building the queue"
    assert second_gap < GAP
    assert list(supervisor._local_work_handle_cache) == [str(host.field_root)], "built once per field root"

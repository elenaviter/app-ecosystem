"""The relay's event loop never waits on the outbox lock or a directory scan (W456).

After the b8403a54 activation on 2026-10-01 the relay's watchdog named two
loop blockers: a 3.8 s ``fcntl.flock`` wait in ``pull_outbox`` (the outbox
lock is also held by the local-state maintenance thread and by pb commands)
and a 3.2 s directory glob in the local-work wait. Either one froze every
channel on the host.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from project_board.client import relay
from project_board.client.io import FileLockBusy, exclusive_lock
from project_board.client.store import SharedFieldStore

from relay_helpers import LoopHeartbeat, make_host, make_supervisor


WORKER = "codex-api"
PROJECT = "project-one"


@pytest.fixture
def field(tmp_path: Path) -> SharedFieldStore:
    store = SharedFieldStore(tmp_path / "shared-field")
    store.initialize(field_id="test-field")
    store.register_worker(
        worker_name=WORKER, runtime_kind="codex", capabilities=[],
        authority_label="authority:codex-api",
    )
    store.create_project(
        project_id=PROJECT, title="Outbox", goal="Keep the loop free.", owner="operator",
    )
    return store


def _adapter(field: SharedFieldStore):
    """The real flush method on a minimal adapter: field, config, settle set."""

    adapter = object.__new__(relay.ProblemBoardHostRelayAdapter)
    adapter.field = field
    adapter.config = SimpleNamespace(relay_id="relay-01", worker_name=WORKER, project_id=PROJECT)
    adapter._outbox_finishes = set()
    return adapter


def _hold(lock_path: Path, seconds: float, held: threading.Event) -> threading.Thread:
    def run() -> None:
        with exclusive_lock(lock_path):
            held.set()
            time.sleep(seconds)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert held.wait(2.0)
    return thread


def test_a_non_waiting_lock_refuses_at_once_while_another_holder_has_it(field):
    held = threading.Event()
    holder = _hold(field._outbox.lock, 0.5, held)
    started = time.monotonic()
    with pytest.raises(FileLockBusy):
        with exclusive_lock(field._outbox.lock, wait=False):
            pass
    assert time.monotonic() - started < 0.2
    holder.join()
    with exclusive_lock(field._outbox.lock, wait=False):
        pass


def test_a_non_waiting_lock_with_cancellation_still_makes_one_attempt(tmp_path):
    lock = tmp_path / "combined.lock"
    held = threading.Event()
    holder = _hold(lock, 0.5, held)
    checks = []
    started = time.monotonic()
    try:
        with pytest.raises(FileLockBusy):
            with exclusive_lock(lock, wait=False, check_cancelled=lambda: checks.append(True)):
                pytest.fail("the held lock must not enter the block")
        assert time.monotonic() - started < 0.2
        assert len(checks) == 1
    finally:
        holder.join()


def test_a_non_waiting_lock_checks_cancellation_before_and_after_acquisition(tmp_path):
    checks = []
    with exclusive_lock(
        tmp_path / "combined.lock", wait=False, check_cancelled=lambda: checks.append(True)
    ):
        assert len(checks) == 2


@pytest.mark.parametrize("wait", [True, False])
def test_a_lock_cancelled_before_acquisition_does_not_open_a_file(tmp_path, wait):
    lock = tmp_path / "cancelled.lock"

    def cancelled():
        raise RuntimeError("cancelled before acquisition")

    with pytest.raises(RuntimeError, match="cancelled before acquisition"):
        with exclusive_lock(lock, wait=wait, check_cancelled=cancelled):
            pytest.fail("a cancelled lock must not enter the block")
    assert not lock.exists()


@pytest.mark.parametrize("wait", [True, False])
def test_cancellation_after_acquisition_releases_the_lock(tmp_path, wait):
    lock = tmp_path / "cancelled.lock"
    checks = 0
    # Cancellable waiting also checks immediately before its flock attempt.
    cancel_on = 3 if wait else 2

    def cancelled():
        nonlocal checks
        checks += 1
        if checks == cancel_on:
            raise RuntimeError("cancelled after acquisition")

    with pytest.raises(RuntimeError, match="cancelled after acquisition"):
        with exclusive_lock(lock, wait=wait, check_cancelled=cancelled):
            pytest.fail("post-acquisition cancellation must not enter the block")
    assert checks == cancel_on
    with exclusive_lock(lock, wait=False):
        pass


def test_a_held_outbox_lock_does_not_stall_the_event_loop(field):
    async def scenario():
        held = threading.Event()
        holder = _hold(field._outbox.lock, 2.0, held)
        beat = LoopHeartbeat()
        beat.start()
        started = time.monotonic()
        counts = await asyncio.wait_for(
            _adapter(field)._flush_outbox_unlocked(kinds={"no-such-kind"}), timeout=10
        )
        waited = time.monotonic() - started
        await beat.stop()
        holder.join()
        return counts, waited, beat.max_gap

    counts, waited, max_gap = asyncio.run(scenario())
    assert counts["outbox_sent"] == 0
    assert waited >= 1.5, "the flush ran only after the holder released the lock"
    assert max_gap < 0.5, f"the event loop stalled {max_gap:.2f}s on the outbox lock"


def test_a_flush_cancelled_while_waiting_claims_nothing_and_leaves_no_thread(field):
    row = field.enqueue_service_event(
        PROJECT, worker_name=WORKER, kind="note.recorded", summary="note",
        source_event_ref="local:test:1",
    )

    async def scenario():
        threads_before = threading.active_count()
        held = threading.Event()
        holder = _hold(field._outbox.lock, 1.0, held)
        flush = asyncio.create_task(_adapter(field)._flush_outbox_unlocked())
        await asyncio.sleep(0.3)
        flush.cancel()
        await asyncio.gather(flush, return_exceptions=True)
        holder.join()
        await asyncio.sleep(0.3)
        return threads_before, threading.active_count(), flush.cancelled()

    threads_before, threads_after, cancelled = asyncio.run(scenario())
    assert cancelled
    assert field.read_outbox_record(row["outbox_id"])["state"] == "pending", (
        "a cancelled claim left the row leased"
    )
    assert threads_after <= threads_before, "a claimer thread outlived the cancelled flush"


def test_a_slow_local_work_scan_does_not_stall_the_event_loop(tmp_path, monkeypatch):
    host, _identity, channel = make_host(tmp_path)
    supervisor = make_supervisor(host)

    def slow_signature(_self, field_root, *, worker_names=()):
        time.sleep(1.0)  # a directory scan on a paging host
        return ("unchanged",)

    monkeypatch.setattr(
        relay.ProblemBoardRelaySupervisor, "_local_work_signature", slow_signature
    )

    async def scenario():
        beat = LoopHeartbeat()
        beat.start()
        woke = await supervisor._wait_for_local_work(
            host.field_root, 1.5, worker_names=[channel.worker_name]
        )
        await beat.stop()
        return woke, beat.max_gap

    woke, max_gap = asyncio.run(scenario())
    assert woke is False
    assert max_gap < 0.5, f"the event loop stalled {max_gap:.2f}s in the local-work scan"

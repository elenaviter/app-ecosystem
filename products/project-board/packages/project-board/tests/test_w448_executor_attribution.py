"""W448: name the call behind an executor wait and the calls that held its thread.

The W461 accounting counted executor waits per turn but not who caused them.
On dev-main a channel turn waited up to 28.3 s for its store thread while
spark1 waited 0.009 s. These tests pin the attribution that names the holder:
the waiting call's label and the calls that ran in its single thread while it
waited, whichever task submitted them.
"""

from __future__ import annotations

import asyncio
import functools
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from project_board.client import off_loop, relay_trace
from project_board.client.off_loop import (
    HOLDER_MIN_WAIT_SECONDS,
    OFF_LOOP_OBSERVER,
    call_label,
    run_off_loop,
)
from project_board.client.relay_trace import RelayActivityTrace


class _Store:
    def pending_worker_mail_refs(self) -> list[str]:
        return []


def hold_thread(release: threading.Event) -> None:
    release.wait(5.0)


def quick_read() -> str:
    return "read"


def test_label_names_module_and_qualname_from_source_names_only() -> None:
    assert call_label(_Store().pending_worker_mail_refs) == (
        "test_w448_executor_attribution._Store.pending_worker_mail_refs"
    )
    assert call_label(functools.partial(functools.partial(quick_read))) == (
        "test_w448_executor_attribution.quick_read"
    )
    assert call_label(time.sleep) == "time.sleep"

    def nested() -> None:
        return None

    assert call_label(nested).endswith("<locals>.nested")

    class Odd:
        __qualname__ = "has space/and slash"
        __module__ = "x"

        def __call__(self) -> None:
            return None

    assert call_label(Odd()) == "other"


def _waiting_call_behind_holder(executor: ThreadPoolExecutor, hold: float) -> list[tuple]:
    """A holder submitted outside any turn, then a call that waits behind it."""

    observed: list[tuple] = []

    async def scenario() -> None:
        release = threading.Event()
        holder = asyncio.ensure_future(run_off_loop(hold_thread, release, executor=executor))
        await asyncio.sleep(0.05)  # the holder is running in the thread now
        token = OFF_LOOP_OBSERVER.set(lambda *args: observed.append(args))
        try:
            waiter = asyncio.ensure_future(run_off_loop(quick_read, executor=executor))
            await asyncio.sleep(hold)
            release.set()
            assert await waiter == "read"
        finally:
            OFF_LOOP_OBSERVER.reset(token)
        await holder

    asyncio.run(scenario())
    return observed


def test_a_long_wait_names_the_call_that_held_the_single_thread() -> None:
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        observed = _waiting_call_behind_holder(executor, HOLDER_MIN_WAIT_SECONDS + 0.2)
    finally:
        executor.shutdown(wait=True)
    [(wait, run, label, holders)] = observed
    assert wait >= HOLDER_MIN_WAIT_SECONDS
    assert run < wait
    assert label == "test_w448_executor_attribution.quick_read"
    [(holder, held)] = holders
    assert holder == "test_w448_executor_attribution.hold_thread"
    assert HOLDER_MIN_WAIT_SECONDS <= held <= wait + 0.01


def test_a_short_wait_reports_no_holders() -> None:
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        observed = _waiting_call_behind_holder(executor, 0.05)
    finally:
        executor.shutdown(wait=True)
    [(wait, _run, label, holders)] = observed
    assert wait < HOLDER_MIN_WAIT_SECONDS
    assert label == "test_w448_executor_attribution.quick_read"
    assert holders == []


def test_holders_are_ranked_and_bounded_from_the_thread_history() -> None:
    history = off_loop._history()
    history.clear()
    for index in range(off_loop.HOLDER_HISTORY + 4):
        history.append((f"m.call{index}", 10.0 + index, 10.0 + index + 0.1 * (index % 5)))
    assert len(history) == off_loop.HOLDER_HISTORY
    holders = off_loop._holders(submitted=0.0, started=100.0)
    assert len(holders) == off_loop.HOLDERS_REPORTED
    assert [held for _label, held in holders] == sorted(
        (held for _label, held in holders), reverse=True
    )
    # Only the part of a call inside the wait counts.
    history.clear()
    history.append(("m.before", 0.0, 2.0))
    history.append(("m.inside", 2.0, 3.0))
    assert off_loop._holders(submitted=1.5, started=2.5) == [("m.before", 0.5), ("m.inside", 0.5)]
    history.clear()


def test_a_failing_call_is_still_recorded_as_a_holder() -> None:
    off_loop._history().clear()

    def boom() -> None:
        raise ValueError("fixture")

    async def scenario() -> None:
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            try:
                await run_off_loop(boom, executor=executor)
            except ValueError:
                pass
            labels = await run_off_loop(
                lambda: [label for label, _b, _e in off_loop._history()], executor=executor
            )
        finally:
            executor.shutdown(wait=True)
        assert labels[-1].endswith("<locals>.boom")

    asyncio.run(scenario())


def test_turn_summary_and_bucket_name_the_waiter_and_its_holders() -> None:
    trace = RelayActivityTrace()
    accounting = trace.accounting
    turn = accounting.begin_turn("w1")
    turn.record_executor(0.01, 0.0, "relay.fast", [])
    turn.record_executor(6.0, 0.1, "store.SharedFieldStore.pull_mail", [("store.SharedFieldStore.pending_worker_mail_refs", 5.5), ("coordinate_queue.CoordinateQueue.claim", 0.4)])
    turn.record_executor(1.0, 0.1, "relay.RelayAdapter._prepare_project_heartbeat", [("coordinate_queue.CoordinateQueue.claim", 0.9)])
    summary = accounting.end_turn(turn, "failed", code="fixture")
    assert summary is not None
    assert summary["executor"]["wait_max_callable"] == "store.SharedFieldStore.pull_mail"
    assert summary["executor"]["wait_max_holders"] == [
        ["store.SharedFieldStore.pending_worker_mail_refs", 5.5],
        ["coordinate_queue.CoordinateQueue.claim", 0.4],
    ]
    payload = accounting._bucket.payload(
        process_id="p", ended_monotonic=0.0, ended_at=0.0, final=True
    )
    executor = payload["executor"]
    assert [row["callable"] for row in executor["slow_waits"]] == [
        "store.SharedFieldStore.pull_mail",
        "relay.RelayAdapter._prepare_project_heartbeat",
    ]
    holders = {row["callable"]: row for row in executor["holders"]}
    assert holders["store.SharedFieldStore.pending_worker_mail_refs"]["held_seconds_sum"] == 5.5
    claim = holders["coordinate_queue.CoordinateQueue.claim"]
    assert claim["count"] == 2 and claim["held_seconds_sum"] == 1.3 and claim["held_seconds_max"] == 0.9
    assert all("relay.fast" != row["callable"] for row in executor["slow_waits"])


def test_bucket_label_tables_stay_bounded() -> None:
    trace = RelayActivityTrace()
    accounting = trace.accounting
    for index in range(relay_trace.EXECUTOR_LABELS + 10):
        turn = accounting.begin_turn("w1")
        turn.record_executor(1.0, 0.0, f"m.waiter{index}", [(f"m.holder{index}", 1.0)])
        accounting.end_turn(turn, "succeeded")
    bucket = accounting._bucket
    assert len(bucket.slow_waits) == relay_trace.EXECUTOR_LABELS + 1
    assert bucket.slow_waits["other"]["count"] == 10
    assert len(bucket.holders) == relay_trace.EXECUTOR_LABELS + 1
    payload = bucket.payload(process_id="p", ended_monotonic=0.0, ended_at=0.0, final=True)
    assert len(payload["executor"]["slow_waits"]) == relay_trace.EXECUTOR_TOP
    assert len(payload["executor"]["holders"]) == relay_trace.EXECUTOR_TOP


def test_long_waits_kept_per_turn_are_bounded() -> None:
    trace = RelayActivityTrace()
    turn = trace.accounting.begin_turn("w1")
    for index in range(relay_trace.EXECUTOR_TOP + 3):
        turn.record_executor(1.0, 0.0, f"m.waiter{index}", [])
    assert len(turn.executor_long_waits) == relay_trace.EXECUTOR_TOP
    trace.accounting.end_turn(turn, "succeeded")

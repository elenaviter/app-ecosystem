"""Channel turns, requests and jobs are counted and correlated without timestamps (W461).

A 31.7 s turn on 2026-10-03 could not be attributed: the slow-turn line had a
worker and a duration but no turn id, and its stages lived in a 300 s volatile
trace. Here a turn gets an opaque id in its task's context, every stage,
coordinate request and executor call made under it carries that id, every
turn is counted in 900 s buckets, and only slow, failed or cancelled turns get
a bounded summary. Lines leave the loop through a writer of their own whose
receipt is explicit: appended, failed (by class), or unknown.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from project_board.client import relay_trace
from project_board.client.off_loop import run_off_loop
from project_board.client.relay_trace import (
    ACCOUNTING_MAX_WORKERS,
    ACCOUNTING_RETAINED_BUCKETS,
    ACCOUNTING_RETAINED_BYTES,
    LOG_PREFIX_RESERVE_BYTES,
    SUMMARY_MAX_BYTES,
    SUMMARY_MAX_STAGES,
    DiagnosticWriter,
    RelayActivityTrace,
    TurnAccounting,
    duration_bucket,
)
from project_board.client.workspace_size import WorkspaceSizes

SECRET = "sk-SENTINEL-do-not-leak"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _trace(clock: _Clock | None = None, writer: DiagnosticWriter | None = None) -> RelayActivityTrace:
    clock = clock or _Clock()
    return RelayActivityTrace(monotonic=clock, wall_clock=lambda: 1_700_000_000.0 + clock.now, writer=writer)


def _pending_lines(trace: RelayActivityTrace) -> list[str]:
    return [line for _kind, _seq, line, _size in trace.accounting._pending]


def _summaries(trace: RelayActivityTrace) -> list[dict]:
    out = []
    for line in _pending_lines(trace):
        if line.startswith(relay_trace.TURN_SUMMARY_MESSAGE):
            out.append(json.loads(line[len(relay_trace.TURN_SUMMARY_MESSAGE):]))
    return out


# Correlation ------------------------------------------------------------

def test_a_turn_id_reaches_stages_in_tasks_it_creates_and_is_reset_after() -> None:
    trace = _trace()

    async def scenario():
        async def body():
            with trace.stage("channel.open", channel="w1", operation="data_bus.connect"):
                await asyncio.sleep(0)
            return relay_trace._TURN.get()

        turn = trace.begin_turn("w1")
        inner = await asyncio.ensure_future(body())
        trace.end_turn(turn, "succeeded")
        return turn, inner, relay_trace._TURN.get()

    turn, inner, after = asyncio.run(scenario())
    assert inner is turn, "a task created inside the turn carries its id"
    assert after is None, "the turn's own context is reset when it ends"
    [record] = list(trace._history)
    assert record["turn_id"] == turn.turn_id and record["origin_turn_id"] == ""


def test_overlapping_turns_keep_their_own_ids() -> None:
    trace = _trace()

    async def scenario():
        async def turn_task(worker, gate):
            turn = trace.begin_turn(worker)
            with trace.stage("attendance.poll", channel=worker):
                await gate.wait()
            trace.end_turn(turn, "succeeded")
            return turn.turn_id

        a, b = asyncio.Event(), asyncio.Event()
        ta = asyncio.create_task(turn_task("w1", a))
        tb = asyncio.create_task(turn_task("w2", b))
        await asyncio.sleep(0)
        b.set()
        await asyncio.sleep(0)
        a.set()
        return await ta, await tb

    id_a, id_b = asyncio.run(scenario())
    assert id_a != id_b
    by_channel = {r["channel"]: r["turn_id"] for r in trace._history}
    assert by_channel == {"w1": id_a, "w2": id_b}, "never attributed by time overlap"


def test_a_child_outliving_its_turn_names_it_only_as_origin() -> None:
    trace = _trace()

    async def scenario():
        release = asyncio.Event()

        async def child():
            await release.wait()
            with trace.stage("session.notify", channel="w1"):
                pass

        turn = trace.begin_turn("w1")
        task = asyncio.create_task(child())
        trace.end_turn(turn, "succeeded")
        release.set()
        await task
        return turn

    turn = asyncio.run(scenario())
    [record] = list(trace._history)
    assert record["turn_id"] == "" and record["origin_turn_id"] == turn.turn_id


def test_request_scope_is_set_per_request_and_reset_on_every_exit() -> None:
    trace = _trace()

    async def scenario():
        turn = trace.begin_turn("w1")
        scope = trace.request_scope()
        seen = []
        try:
            for request_id in ("req-1", "req-2"):
                scope.enter(request_id)
                with trace.stage("coordinate.drain", channel="w1"):
                    seen.append(relay_trace._REQUEST.get())
                if request_id == "req-2":
                    raise RuntimeError("drain failed")
        except RuntimeError:
            pass
        finally:
            scope.close()
        after = relay_trace._REQUEST.get()
        trace.end_turn(turn, "succeeded")
        return turn, seen, after

    turn, seen, after = asyncio.run(scenario())
    assert seen == ["req-1", "req-2"] and after == ""
    assert turn.requests == ["req-1", "req-2"]
    assert [r["request_id"] for r in trace._history] == ["req-1", "req-2"]


def test_a_request_drained_outside_any_turn_has_no_turn_id() -> None:
    trace = _trace()

    async def scenario():
        scope = trace.request_scope()
        scope.enter("side-1")
        with trace.stage("coordinate.drain", channel="w1"):
            pass
        scope.close()

    asyncio.run(scenario())
    [record] = list(trace._history)
    assert record["turn_id"] == "" and record["request_id"] == "side-1"


# Executor timing --------------------------------------------------------

def test_executor_wait_and_run_are_recorded_only_inside_a_turn() -> None:
    trace = _trace()

    async def scenario():
        await run_off_loop(time.sleep, 0.01)
        turn = trace.begin_turn("w1")
        await run_off_loop(time.sleep, 0.02)
        await run_off_loop(time.sleep, 0.0)
        trace.end_turn(turn, "succeeded")
        return turn

    turn = asyncio.run(scenario())
    assert turn.executor_calls == 2
    assert turn.executor_run_seconds >= 0.015


def test_lock_wait_is_its_own_stage_before_the_work() -> None:
    trace = RelayActivityTrace()  # real clocks: the wait is measured

    async def scenario():
        lock = asyncio.Lock()
        await lock.acquire()
        turn = trace.begin_turn("w1")

        async def waiter():
            async with trace.timed_lock(lock, "session.notify_lock_wait", channel="w1"):
                with trace.stage("session.notify", channel="w1"):
                    pass

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.02)
        lock.release()
        await task
        trace.end_turn(turn, "succeeded")

    asyncio.run(scenario())
    stages = [(r["stage"], r["seconds"]) for r in trace._history]
    assert [name for name, _ in stages] == ["session.notify_lock_wait", "session.notify"]
    assert stages[0][1] >= 0.015


# Counting ---------------------------------------------------------------

def test_every_outcome_is_counted_once_and_host_totals_reconcile() -> None:
    clock = _Clock()
    trace = _trace(clock)
    for index, (outcome, seconds) in enumerate(
        [("succeeded", 0.5), ("succeeded", 6.0), ("failed", 1.0), ("cancelled", 25.0), ("deadline", 60.0)]
    ):
        turn = trace.begin_turn(f"w{index % 2}")
        clock.now += seconds
        trace.end_turn(turn, outcome, code="work_relay_x")
        trace.end_turn(turn, "succeeded")  # a second end is ignored
    bucket = trace.accounting._bucket
    host = bucket.hosts["startup"]
    assert host["started"] == 5
    assert (host["succeeded"], host["failed"], host["cancelled"], host["deadline"]) == (2, 1, 1, 1)
    assert sum(host["histogram"]) == 5
    for key in ("started", "succeeded", "failed", "cancelled", "deadline"):
        assert sum(c[key] for c in bucket.workers.values()) == host[key]
    assert host["histogram"][duration_bucket(60.0)] == 1
    # Only the slow success and the three non-successes produce a summary.
    assert [s["outcome"] for s in _summaries(trace)] == ["succeeded", "failed", "cancelled", "deadline"]


def test_more_workers_than_the_cap_roll_into_other_and_host_totals_stay_exact() -> None:
    trace = _trace()
    for index in range(ACCOUNTING_MAX_WORKERS + 5):
        turn = trace.begin_turn(f"worker-{index}")
        trace.end_turn(turn, "succeeded")
    bucket = trace.accounting._bucket
    assert len([w for w in bucket.workers if w != "other"]) == ACCOUNTING_MAX_WORKERS
    assert bucket.workers["other"]["started"] == 5
    assert bucket.hosts["startup"]["started"] == ACCOUNTING_MAX_WORKERS + 5
    payload = bucket.payload(process_id="p", ended_monotonic=bucket.started_monotonic + 1, ended_at=0.0, final=False)
    assert payload["worker_turns_other_is_rollup"] is True and payload["overflow_worker_ids"] == 5


def test_a_turn_running_at_shutdown_is_abandoned_once_never_also_cancelled() -> None:
    trace = _trace()
    turn = trace.begin_turn("w1")
    assert trace.accounting.abandon_in_flight() == 1
    assert trace.end_turn(turn, "cancelled") is None
    host = trace.accounting._bucket.hosts["startup"]
    assert (host["abandoned_at_shutdown"], host["cancelled"]) == (1, 0)


def test_workspace_jobs_are_counted_by_decision() -> None:
    trace = _trace()

    async def scenario():
        release = asyncio.Event()

        async def walk(path):
            await release.wait()
            return 7

        sizes = WorkspaceSizes(walk=walk, observer=trace.record_job, interval_seconds=900.0)
        first = sizes.schedule("/w")
        assert sizes.schedule("/w") is None  # coalesced while in flight
        release.set()
        await first
        assert sizes.schedule("/w") is None  # not due yet

    asyncio.run(scenario())
    job = trace.accounting._bucket.jobs["workspace_size"]
    assert (job["schedule_attempted"], job["accepted"], job["coalesced_in_flight"], job["not_due"]) == (3, 1, 1, 1)
    assert (job["walk_started"], job["completed"], job["failed"]) == (1, 1, 0)
    assert sum(job["queue_wait_histogram"]) == 1 and sum(job["run_histogram"]) == 1


# Summaries and budgets --------------------------------------------------

def test_a_summary_is_bounded_allowlisted_and_never_carries_secrets() -> None:
    clock = _Clock()
    trace = _trace(clock)

    async def scenario():
        turn = trace.begin_turn("wörker-ünïcode-" + "x" * 40)
        for index in range(SUMMARY_MAX_STAGES + 20):
            try:
                with trace.stage(f"made.up.{SECRET}", channel=SECRET, operation=f"op-{SECRET}"):
                    clock.now += 0.001 * index
                    if index == 0:
                        raise ValueError(SECRET)
            except ValueError:
                pass
        clock.now += 7.0
        trace.end_turn(turn, "failed", code="work_relay_x")

    asyncio.run(scenario())
    [line] = _pending_lines(trace)
    assert SECRET not in line
    assert len(line.encode("utf-8")) + LOG_PREFIX_RESERVE_BYTES <= SUMMARY_MAX_BYTES
    [summary] = _summaries(trace)
    assert len(summary["stages"]) <= SUMMARY_MAX_STAGES
    assert summary["omitted_stages"] == SUMMARY_MAX_STAGES + 20 - len(summary["stages"])
    assert {s["stage"] for s in summary["stages"]} == {"other"}
    assert all(s["operation"] == "other" for s in summary["stages"])


def test_a_burst_over_the_rate_cap_is_dropped_and_counted() -> None:
    clock = _Clock()
    trace = _trace(clock)
    for _ in range(400):
        turn = trace.begin_turn("w1")
        trace.end_turn(turn, "failed", code="work_relay_x")
    bucket = trace.accounting._bucket
    assert bucket.summaries_queued + bucket.summaries_dropped_rate + bucket.summaries_dropped_pending == 400
    assert trace.accounting._pending_bytes <= ACCOUNTING_RETAINED_BYTES


def test_unwritten_buckets_stay_within_budget_and_each_loss_counts_once() -> None:
    clock = _Clock()
    trace = _trace(clock)  # no writer: nothing leaves, so everything queues
    for index in range(ACCOUNTING_RETAINED_BUCKETS + 6):
        for worker in range(ACCOUNTING_MAX_WORKERS + 4):
            turn = trace.begin_turn(f"worker-{worker}-{'é' * 20}")
            trace.end_turn(turn, "succeeded")
        trace.record_job("workspace_size", "accepted")
        clock.now += relay_trace.ACCOUNTING_BUCKET_SECONDS
        assert trace.accounting.roll_due()
    accounting = trace.accounting
    assert accounting._pending_buckets <= ACCOUNTING_RETAINED_BUCKETS
    assert accounting._pending_bytes <= ACCOUNTING_RETAINED_BYTES
    assert accounting.buckets_lost_unwritten + accounting._pending_buckets == ACCOUNTING_RETAINED_BUCKETS + 6


def test_the_process_start_line_says_prior_coverage_is_uncertain() -> None:
    trace = _trace()
    trace.accounting.announce_process_start()
    trace.accounting.announce_process_start()
    lines = [l for l in _pending_lines(trace) if '"process_start"' in l]
    assert len(lines) == 1 and '"prior_coverage":"uncertain"' in lines[0]


# Writer -----------------------------------------------------------------

def test_the_writer_reports_appended_failed_by_class_and_unknown_then_late_once() -> None:
    written: list[str] = []
    gate = threading.Event()

    def sink(line: str) -> None:
        if line == "fail":
            raise OSError(SECRET)
        if line == "slow":
            gate.wait(5)
        written.append(line)

    async def scenario():
        writer = DiagnosticWriter(sink, timeout_seconds=0.05)
        receipts = [await writer.write(("t", 1), "ok"), await writer.write(("t", 2), "fail")]
        receipts.append(await writer.write(("b", 3), "slow"))
        receipts.append(await writer.write(("t", 4), "ok2"))  # one in flight: nothing starts
        gate.set()
        for _ in range(100):
            if not writer.busy():
                break
            await asyncio.sleep(0.01)
        receipts.append(await writer.write(("t", 5), "ok3"))
        writer.close()
        return writer, receipts

    writer, receipts = asyncio.run(scenario())
    assert receipts == ["appended", "failed", "unknown", "busy", "appended"]
    assert writer.receipts["late_appended"] == 1 and writer.receipts["unknown_at_timeout"] == 1
    assert writer.failure_classes == {"OSError": 1}
    assert SECRET not in json.dumps(writer.receipts) + json.dumps(writer.failure_classes)
    assert written == ["ok", "slow", "ok3"]


def test_a_stuck_writer_never_holds_process_exit(tmp_path) -> None:
    script = textwrap.dedent(
        """
        import asyncio, threading
        from project_board.client.relay_trace import DiagnosticWriter
        never = threading.Event()
        async def main():
            writer = DiagnosticWriter(lambda line: never.wait(), timeout_seconds=0.05)
            assert await writer.write(("b", 1), "x") == "unknown"
            writer.close()
        asyncio.run(main())
        print("exited")
        """
    )
    started = time.monotonic()
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=20)
    assert done.returncode == 0 and "exited" in done.stdout
    assert time.monotonic() - started < 20


def test_flush_hands_lines_to_the_writer_and_stops_at_an_unknown_write() -> None:
    clock = _Clock()
    gate = threading.Event()
    written: list[str] = []

    def sink(line: str) -> None:
        if "bucket" in line:
            gate.wait(5)
        written.append(line)

    async def scenario():
        trace = _trace(clock, writer=DiagnosticWriter(sink, timeout_seconds=0.05))
        turn = trace.begin_turn("w1")
        trace.end_turn(turn, "failed", code="x")
        clock.now += relay_trace.ACCOUNTING_BUCKET_SECONDS
        first = await trace.flush_accounting()
        second = await trace.flush_accounting()
        gate.set()
        return trace, first, second

    trace, first, second = asyncio.run(scenario())
    assert first == {"appended": 1, "failed": 0, "unknown": 1, "busy": 0, "dropped": 0}
    assert second["busy"] in (0, 1) and second["appended"] == 0
    assert trace.accounting._pending_buckets == 0


def test_a_write_whose_caller_was_cancelled_is_delivered_late_exactly_once() -> None:
    import concurrent.futures

    receipt: concurrent.futures.Future = concurrent.futures.Future()
    receipt.set_running_or_notify_cancel()
    late: list = []

    async def scenario():
        writer = DiagnosticWriter(submit=lambda line: receipt, timeout_seconds=5.0)
        writer.on_late = lambda key, appended: late.append((key, appended))
        task = asyncio.create_task(writer.write(("bucket", 7), "line"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert writer.busy(), "the cancelled caller's write still holds the slot"
        receipt.set_exception(OSError("synthetic"))
        await asyncio.sleep(0)
        assert not writer.busy()
        writer.busy()
        return writer

    writer = asyncio.run(scenario())
    assert late == [(("bucket", 7), False)]
    assert (writer.receipts["late_failed"], writer.receipts["failed"]) == (1, 0)

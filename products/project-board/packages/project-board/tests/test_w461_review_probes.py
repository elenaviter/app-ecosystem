"""Independent exact-PR481 negative controls; all faults are synthetic/local."""

import asyncio
import concurrent.futures
import contextlib
import gc
import io
import logging
import threading
import time
from logging.handlers import RotatingFileHandler

import pytest

from project_board.client.relay_logging import RelayLogOwner
from project_board.client.relay_trace import DiagnosticWriter, RelayActivityTrace

SENTINEL = "SYNTHETIC_REVIEW_SENTINEL_NOT_A_CREDENTIAL"


class HeldHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def emit(self, record):
        self.started.set()
        assert self.release.wait(3), "probe cleanup deadline"


def record(message):
    return logging.LogRecord("probe", logging.WARNING, __file__, 1, message, (), None)


def test_inflight_record_remains_in_the_count_and_byte_budget():
    handler = HeldHandler()
    owner = RelayLogOwner(handler, max_records=1, max_bytes=256)
    try:
        assert owner.submit_record(record("a" * 128))
        assert handler.started.wait(1)
        reported = owner.pending()
        second_admitted = owner.submit_record(record("b" * 128))
        assert reported == (1, 256) and not second_admitted, (
            f"first record still in I/O, pending reports {reported}; "
            f"second 256-byte record admitted={second_admitted} under one-record/256-byte cap"
        )
    finally:
        handler.release.set()
        owner.close(1)


def test_handler_final_close_is_owned_and_included_in_the_bounded_wait():
    release = threading.Event()
    seen = []

    class SlowClose(logging.Handler):
        def emit(self, rec):
            pass

        def close(self):
            seen.append(threading.current_thread().name)
            release.wait(3)
            super().close()

    owner = RelayLogOwner(SlowClose())
    timer = threading.Timer(0.30, release.set)
    timer.start()
    started = time.monotonic()
    try:
        owner.close(0.05)
        elapsed = time.monotonic() - started
        assert elapsed < 0.15 and seen == ["problem-board-relay-log"], (
            f"close(wait=0.05) took {elapsed:.3f}s; handler.close threads={seen}"
        )
    finally:
        release.set()
        timer.join(1)


def test_ordinary_file_error_does_not_use_handleerror_stderr_fallback(tmp_path):
    handler = RotatingFileHandler(tmp_path / "probe.log", maxBytes=10000, backupCount=1)
    original_stream = handler.stream

    class BrokenStream:
        def write(self, text):
            raise OSError(SENTINEL)

        def __getattr__(self, name):
            return getattr(original_stream, name)

    handler.stream = BrokenStream()
    owner = RelayLogOwner(handler)
    captured = io.StringIO()
    with contextlib.redirect_stderr(captured):
        owner.submit_record(record("ordinary warning"))
        assert owner.close(1)
    assert SENTINEL not in captured.getvalue(), "raw file exception text reached stderr"


def test_failed_diagnostic_async_wrapper_is_consumed_without_error_text():
    def fail(line):
        raise OSError(SENTINEL)

    async def scenario():
        leaks = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: leaks.append(context))
        writer = DiagnosticWriter(fail, timeout_seconds=1)
        assert await writer.write(("bucket", 1), "safe") == "failed"
        writer.close()
        for _ in range(3):
            gc.collect()
            await asyncio.sleep(0)
        return [str(item.get("exception", "")) for item in leaks]

    errors = asyncio.run(scenario())
    assert not any(SENTINEL in error for error in errors), (
        f"asyncio unhandled-Future error callback received {len(errors)} raw exceptions"
    )


def test_cancelled_writer_await_keeps_the_single_inflight_receipt():
    submitted = []

    def submit(line):
        future = concurrent.futures.Future()
        future.set_running_or_notify_cancel()
        submitted.append(future)
        return future

    async def scenario():
        writer = DiagnosticWriter(submit=submit, timeout_seconds=1)
        task = asyncio.create_task(writer.write(("bucket", 1), "first"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        is_busy = writer.busy()
        for future in submitted:
            future.set_result(None)
        await asyncio.sleep(0)
        writer.close()
        return is_busy

    assert asyncio.run(scenario()), "cancellation erased a submitted, still-running receipt"


def test_concurrent_writes_reserve_one_slot_before_the_new_await():
    submitted = []

    def submit(line):
        future = concurrent.futures.Future()
        future.set_running_or_notify_cancel()
        submitted.append((line, future))
        return future

    async def scenario():
        writer = DiagnosticWriter(submit=submit, timeout_seconds=0.02)
        outcomes = await asyncio.gather(
            writer.write(("bucket", 1), "first"), writer.write(("bucket", 2), "second")
        )
        count = len(submitted)
        for _line, future in submitted:
            future.set_result(None)
        await asyncio.sleep(0)
        writer.busy()
        writer.close()
        return count, outcomes

    count, outcomes = asyncio.run(scenario())
    assert count == 1 and sorted(outcomes) == ["busy", "unknown"], (
        f"submitted {count} writes concurrently; outcomes={outcomes}"
    )


def test_a_late_failed_bucket_is_counted_lost_exactly_once():
    submitted = []

    def submit(line):
        future = concurrent.futures.Future()
        future.set_running_or_notify_cancel()
        submitted.append(future)
        return future

    async def scenario():
        clock = [0.0]
        writer = DiagnosticWriter(submit=submit, timeout_seconds=0.01)
        trace = RelayActivityTrace(monotonic=lambda: clock[0], wall_clock=lambda: clock[0], writer=writer)
        clock[0] = 901
        assert (await trace.flush_accounting())["unknown"] == 1
        submitted[0].set_exception(OSError("synthetic late failure"))
        await asyncio.sleep(0)
        writer.busy()
        writer.busy()
        writer.close()
        return trace.accounting.buckets_lost_unwritten, writer.receipts["late_failed"]

    lost, late = asyncio.run(scenario())
    assert (lost, late) == (1, 1), f"definitely failed bucket lost={lost}, late_failed={late}"

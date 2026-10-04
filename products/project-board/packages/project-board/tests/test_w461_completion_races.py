"""Synthetic exact-head review probes: completed receipt versus suspended caller."""

import asyncio
import concurrent.futures

from project_board.client.relay_trace import DiagnosticWriter, RelayActivityTrace


def test_old_waiter_cannot_erase_a_newer_inflight_slot():
    submitted = []

    def submit(line):
        future = concurrent.futures.Future()
        future.set_running_or_notify_cancel()
        submitted.append((line, future))
        if len(submitted) == 1:
            future.set_result(None)
        return future

    async def scenario():
        writer = DiagnosticWriter(submit=submit, timeout_seconds=0.01)
        try:
            outcomes = await asyncio.gather(
                writer.write(("bucket", 1), "first"),
                writer.write(("bucket", 2), "second"),
            )
            retained = writer.busy()
            third = await writer.write(("bucket", 3), "third")
            return retained, len(submitted), outcomes, third, dict(writer.receipts)
        finally:
            for _, future in submitted:
                if not future.done():
                    future.set_result(None)
            await asyncio.sleep(0)
            writer.busy()
            writer.close()

    retained, count, outcomes, third, receipts = asyncio.run(scenario())
    assert retained and count == 2 and third == "busy", (
        f"second write still pending; busy={retained}, submitted={count}, "
        f"outcomes={outcomes}, third={third}, receipts={receipts}"
    )


def test_reconcile_cannot_count_one_awaited_failure_twice():
    submitted = []

    def submit(line):
        future = concurrent.futures.Future()
        future.set_running_or_notify_cancel()
        submitted.append(future)
        future.set_exception(OSError("synthetic review error"))
        return future

    async def scenario():
        clock = [0.0]
        writer = DiagnosticWriter(submit=submit, timeout_seconds=1)
        trace = RelayActivityTrace(
            monotonic=lambda: clock[0], wall_clock=lambda: clock[0], writer=writer
        )
        clock[0] = 901
        trace.accounting.roll_due()
        task = asyncio.create_task(trace.accounting.flush(max_writes=1))
        await asyncio.sleep(0)  # flush handed off the first completed receipt
        writer.busy()  # another normal relay caller reconciles before flush resumes
        result = await task
        await asyncio.sleep(0)
        writer.close()
        return trace.accounting.buckets_lost_unwritten, result, dict(writer.receipts)

    lost, result, receipts = asyncio.run(scenario())
    assert lost == 1 and receipts["failed"] + receipts["late_failed"] == 1, (
        f"one failed bucket counted lost={lost}; flush={result}; receipts={receipts}"
    )

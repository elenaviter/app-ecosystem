import asyncio
import concurrent.futures
import time

from project_board.client.relay_trace import DiagnosticWriter


def test_timeout_handoff_keeps_completed_receipt_after_reconcile():
    async def scenario():
        receipt = concurrent.futures.Future()
        writer = DiagnosticWriter(submit=lambda line: receipt, timeout_seconds=0.01)
        late = []
        writer.on_late = lambda key, appended: late.append((key, appended))
        task = asyncio.create_task(writer.write(("bucket", 1), "line"))
        await asyncio.sleep(0)
        loop = asyncio.get_running_loop()

        def finish_and_reconcile():
            receipt.set_result(None)
            assert not writer.busy()

        # A finite local fixture delay makes both due callbacks run before the
        # waiting task resumes. The asyncio wait timeout fires first; the
        # underlying concurrent receipt finishes before its wrapper is copied.
        loop.call_later(0.005, time.sleep, 0.03)
        loop.call_later(0.0101, finish_and_reconcile)
        outcome = await task
        await asyncio.sleep(0)
        writer.busy()
        observed = (outcome, dict(writer.receipts), late, writer.last_appended)
        assert writer.receipts["appended"] + writer.receipts["late_appended"] == 1, observed
        assert writer.last_appended == ("bucket", 1), observed

    asyncio.run(scenario())

"""The observer is bounded, private, non-authoritative and safe to lose."""
from __future__ import annotations

import asyncio
import json
from concurrent.futures import Future

import pytest

from project_board.client import wake_trace as module
from project_board.client.wake_trace import WakeTrace, record, tag

WORKER = "codex-11111111-1111-4111-8111-111111111111"
PEER = "codex-22222222-2222-4222-8222-222222222222"


def observer():
    clock = [100.0]
    lines = []
    def submit(line):
        lines.append(line)
        return True
    trace = WakeTrace(submit=submit, process_id="test-process", monotonic=lambda: clock[0],
                      wall_clock=lambda: 1794350400.0 + clock[0])
    return trace, lines, clock


def rows(lines):
    return [json.loads(line.removeprefix(module.MESSAGE)) for line in lines]


def test_allowlist_hashes_locators_and_never_serializes_private_fields():
    trace, lines, _ = observer()
    secret = "private-canary@example.test /private/canary bearer-canary"
    trace.emit("decision", WORKER, reason=secret, error_code=secret, body=secret,
               email=secret, balance=secret, payload={"secret": secret}, message_ref=secret,
               source_message_ref=secret, message_refs=[secret] * 200, pending=200)
    assert len(lines) == 1 and secret not in lines[0]
    row = rows(lines)[0]
    assert row["reason"] == row["error_code"] == "other"
    assert row["message_ref_sha256"] == tag(secret)
    assert len(row["message_refs_sha256"]) == 8 and row["message_refs_omitted"] == 192
    assert not {"body", "email", "balance", "payload"}.intersection(row)


@pytest.mark.parametrize("worker,event", [("alias with private words", "decision"),
                                         (WORKER, "arbitrary event text")])
def test_unrecognized_identity_or_event_emits_nothing(worker, event):
    trace, lines, _ = observer()
    assert not trace.emit(event, worker, reason="empty") and lines == []


def test_unchanged_holds_are_sampled_but_changed_reason_and_pending_are_prompt():
    trace, lines, clock = observer()
    for n in range(100):
        trace.emit("decision", WORKER, repeat=True, reason="quota_error", pending=1,
                   seconds=n, retry_at=f"2026-11-10T21:00:{n % 60:02}Z")
    assert len(lines) == 1
    trace.emit("decision", WORKER, repeat=True, reason="quota_error", pending=2)
    trace.emit("decision", WORKER, repeat=True, reason="eligible", pending=2)
    assert len(lines) == 3
    clock[0] += 60
    trace.emit("decision", WORKER, repeat=True, reason="eligible", pending=2)
    assert len(lines) == 4


def test_byte_budget_line_and_signature_limits_report_loss_after_window():
    trace, lines, clock = observer()
    for n in range(1000):
        trace.emit("mail_materialized", WORKER, message_ref=f"message-{n}", message_refs=[str(n)] * 8)
    assert sum(len(line.encode()) + module.PREFIX_RESERVE_BYTES for line in lines) <= module.BYTES_PER_MINUTE
    assert all(len(line.encode()) + module.PREFIX_RESERVE_BYTES <= module.MAX_LINE_BYTES for line in lines)
    assert trace.losses["budget"] > 0
    clock[0] += 60
    trace.emit("decision", WORKER, reason="empty")
    assert rows(lines)[-1]["prior_losses"]["budget"] > 0
    for n in range(400):
        clock[0] += 60
        trace.emit("decision", f"codex-{n:08x}-1111-4111-8111-111111111111", reason="empty", repeat=True)
    assert len(trace._seen) == module.MAX_SIGNATURES


def test_owner_refusal_failure_and_disabled_observation_do_not_escape():
    trace = WakeTrace(submit=lambda _: None, process_id="test")
    assert not trace.emit("decision", WORKER, reason="empty")
    assert trace.losses["owner_refused"] == 1
    def failed(_):
        raise OSError("private failure text")
    trace._submit = failed
    assert not trace.emit("decision", WORKER, reason="empty")
    assert trace.losses["submit_failed"] == 1
    trace._monotonic = failed
    with trace.check(WORKER):
        assert not trace.emit("decision", WORKER, reason="empty")
    disabled = WakeTrace(submit=None, process_id="test")
    with disabled.check(WORKER):
        assert not disabled.emit("decision", WORKER, body=object())
    record(None, "decision", WORKER, body=object())


def test_failed_write_receipt_is_counted_on_the_next_record():
    lines = []
    receipts = []

    def submit(line):
        lines.append(line)
        receipt = Future()
        receipts.append(receipt)
        return receipt

    trace = WakeTrace(submit=submit, process_id="test")
    assert trace.emit("decision", WORKER, reason="eligible")
    assert not receipts[0].done()
    receipts[0].set_exception(OSError("private write failure"))
    assert trace.emit("queue_started", WORKER)
    receipts[1].set_result(None)
    assert trace.emit("queue_finished", WORKER, reason="queued")
    receipts[2].set_result(None)
    assert rows(lines)[1]["prior_losses"].get("write_failed") == 1
    assert rows(lines)[2]["prior_losses"]["write_failed"] == 1
    assert "private write failure" not in "\n".join(lines)


def test_check_ids_are_task_local_and_reset_even_when_a_check_is_cancelled():
    trace, lines, _ = observer()
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow():
            with trace.check(WORKER):
                trace.emit("quota_read_started", WORKER)
                entered.set()
                await release.wait()
        task = asyncio.create_task(slow())
        await entered.wait()
        with trace.check(PEER):
            trace.emit("decision", PEER, reason="empty")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        trace.emit("decision", WORKER, reason="empty")
    asyncio.run(scenario())
    first, peer, outside = rows(lines)
    assert first["check_id"] != peer["check_id"]
    assert "check_id" not in outside


def test_invalid_timestamps_numbers_and_non_scalar_fields_cannot_leak():
    trace, lines, _ = observer()
    trace.emit("decision", WORKER, reason="quota_error", seconds=float("nan"),
               pending=True, retry_at="2026-99-99T00:00:00Z", quota_observed_at="sensitive text",
               submission_id={"private": "canary"})
    row = rows(lines)[0]
    assert not {"seconds", "pending", "retry_at", "quota_observed_at", "submission_id_sha256"}.intersection(row)

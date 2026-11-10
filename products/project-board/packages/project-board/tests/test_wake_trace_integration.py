"""Exercise the real wake path with synthetic quota/queue adapters, never native reads."""
from __future__ import annotations

import asyncio
import hashlib
import json
from concurrent.futures import Future
from types import SimpleNamespace

from project_board.client import relay
from project_board.contract.errors import DomainError
from relay_helpers import make_supervisor
from test_w438_quota_redemption import _held
from test_w456_notify_off_loop import _two_codex_channels, _with_session_stubs

PREFIX = "Problem Board wake trace v1 "


def capture(monkeypatch):
    lines = []
    def submit(line):
        lines.append(line)
        receipt = Future()
        receipt.set_result(None)
        return receipt
    monkeypatch.setattr(relay, "relay_log_owner", lambda: SimpleNamespace(submit_line=submit))
    return lines


def events(lines, event):
    return [row for line in lines if line.startswith(PREFIX)
            and (row := json.loads(line[len(PREFIX):]))["event"] == event]


def test_failed_native_read_and_cached_hold_are_named_without_extra_reads(tmp_path, monkeypatch):
    lines = capture(monkeypatch)
    host, identity, channel, field, supervisor, pushes = _held(tmp_path, monkeypatch)
    calls = []
    async def unavailable(**kwargs):
        calls.append(kwargs)
        raise DomainError("work_codex_quota_timeout", "private exception text")
    supervisor._codex_quota_reader = unavailable
    async def scenario():
        first = await supervisor._notify_available_input(host, channel)
        second = await supervisor._notify_available_input(host, channel)
        await supervisor.aclose()
        return first, second
    first, second = asyncio.run(scenario())
    assert first["reason"] == second["reason"] == "work_codex_quota_timeout"
    assert first["wake_deferred"] and second["wake_deferred"] and pushes == []
    assert len(calls) == 1 and field.wake_hold(identity.worker_name)
    completed = events(lines, "quota_read_finished")
    assert completed, "a failed native read currently defers mail without naming the read outcome"
    assert completed[0]["reason"] == "read_error"
    assert completed[0]["error_code"] == "work_codex_quota_timeout"
    assert events(lines, "quota_cache")[0]["reason"] == "cache_error"
    decision = events(lines, "decision")[0]
    assert decision["reason"] == "quota_error" and decision["pending"] > 0
    assert decision["mailbox_read"] is True and decision["message_refs_sha256"]
    assert decision["check_id"] == completed[0]["check_id"]
    assert "private exception text" not in "\n".join(lines)


def test_eligible_native_queue_records_admission_and_coalesces_later_mail(tmp_path, monkeypatch):
    lines = capture(monkeypatch)
    host, field, worker, _peer = _two_codex_channels(tmp_path)
    pushed = []
    supervisor = _with_session_stubs(make_supervisor(host), pushed)
    async def scenario():
        first = await supervisor._notify_available_input(host, worker)
        later = field.send_mail("", sender="control-plane", recipient=worker.worker_name,
                                kind="request", subject="Later", body="No content in trace.",
                                idempotency_key="later")
        second = await supervisor._notify_available_input(host, worker)
        await supervisor.aclose()
        return first, later, second
    first, later, second = asyncio.run(scenario())
    assert first["delivered"] and second["deduplicated"]
    assert pushed == [worker.worker_name]
    assert later["message_ref"] in field.worker_listener_session(worker.worker_name)["subscription"]["last_wake_message_refs"]
    assert len(events(lines, "queue_started")) == len(events(lines, "queue_finished")) == 1
    assert events(lines, "queue_finished")[0]["reason"] == "queued"
    assert events(lines, "decision")[-1]["reason"] == "coalesced"


def test_materialization_and_replay_keep_the_original_receipt_and_hash_only_locators(monkeypatch):
    lines = capture(monkeypatch)
    trace = relay.RelayActivityTrace(wake_submit=relay.relay_log_owner().submit_line)
    worker = "codex-11111111-1111-4111-8111-111111111111"
    item = {"recipient": worker, "ref": "control-private-title", "created_at": "2026-11-10T00:00:00Z",
            "payload": {"mail": {"source_message_ref": "mail-private-title"}}}
    receipt = {"message_ref": "local-private-title", "replayed": False, "delivery_status": "pending"}
    adapter = object.__new__(relay.ProblemBoardHostRelayAdapter)
    adapter._trace = trace
    calls = []
    def materialize(control):
        calls.append(control)
        return receipt
    adapter.field = SimpleNamespace(materialize_control=materialize)
    assert adapter._materialize_control_traced(item) is receipt
    receipt["replayed"] = True
    assert adapter._materialize_control_traced(item) is receipt
    assert calls == [item, item]
    row = events(lines, "mail_materialized")[0]
    assert row["delivery_status"] == "pending"
    assert row["source_message_ref_sha256"] == hashlib.sha256(b"mail-private-title").hexdigest()
    assert row["remote_created_at"] == item["created_at"]
    assert len(events(lines, "mail_replayed")) == 1
    assert "private-title" not in "\n".join(lines)


def test_failed_command_with_admission_evidence_is_not_reported_as_rejection(tmp_path, monkeypatch):
    lines = capture(monkeypatch)
    host, _field, worker, _peer = _two_codex_channels(tmp_path)
    supervisor = _with_session_stubs(make_supervisor(host), [])
    def admitted_then_failed(_channel, **_kwargs):
        return {"adapter": "codex-queue", "state": "unreachable", "delivered": False,
                "reason": "codex_queue_failed", "admission_seen": True,
                "queued_submission_id": "admitted-submission"}
    supervisor.session_notifier = admitted_then_failed
    async def scenario():
        delivery = await supervisor._notify_available_input(host, worker)
        await supervisor.aclose()
        return delivery
    assert asyncio.run(scenario())["delivered"] is False
    row = events(lines, "queue_finished")[0]
    assert row["reason"] == "unconfirmed" and row["admission_seen"] is True
    assert row["submission_id_sha256"] == hashlib.sha256(b"admitted-submission").hexdigest()
    assert "admitted-submission" not in "\n".join(lines)

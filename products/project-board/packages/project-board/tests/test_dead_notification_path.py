"""The relay acts on a dead notification path instead of only exposing it (W182).

Since the operator's ruling of 2026-09-23 the relay's notices are project events,
never mail: this file moved here from the applications suite with the change,
and every note below is a `worker.notification_path` event.

On 2026-09-18 two Claude Code workers were unreachable for hours while every
worker listing carried their overdue inbox checks. The worker that has the dead
path cannot report it, so its relay does: one operator update on the transition
to dead, one on recovery, nothing in between.

A Codex session's inbox checks lapse while it idles, so its dead path is a
different observation: a wake the relay enqueued into the native queue that the
session has not taken past its deadline (W198, 76 minutes silent on 2026-09-19
while Codex was exempt here).
"""

from __future__ import annotations

import re
from types import SimpleNamespace

from project_board.client import relay as relay_module
from project_board.contract.errors import DomainError


class _Field:
    def __init__(self, reach: dict) -> None:
        self.reach = reach
        self.notes: list[dict] = []
        self.raise_on_enqueue = False
        # The outage record persists here, not on the adapter, which the relay
        # rebuilds every cycle. Phases: dead_note and recovery_note, pending
        # or enqueued.
        self.record: dict = {}
        self.fail_record_writes = False

    def worker_reachability(self, worker_name: str) -> dict:
        assert worker_name == "claude-code-abc"
        return dict(self.reach)

    def dead_path_record(self, worker_name: str) -> dict:
        assert worker_name == "claude-code-abc"
        return dict(self.record)

    def write_dead_path_record(self, worker_name: str, record) -> None:
        assert worker_name == "claude-code-abc"
        if self.fail_record_writes:
            raise DomainError("field_record_not_found", "gone", status=404)
        self.record = dict(record) if record else {}

    def service_event_receipt_exists(self, *, worker_name: str, idempotency_key: str) -> bool:
        assert worker_name == "claude-code-abc"
        return any(note["idempotency_key"] == idempotency_key for note in self.notes)

    @property
    def reported_since(self) -> str:
        return self.record.get("since", "") if self.record.get("open_note") == "enqueued" else ""

    def enqueue_service_event(self, project_id: str, **event) -> dict:
        if self.raise_on_enqueue:
            raise DomainError("field_record_not_found", "refused", status=409)
        # The real store keeps one receipt per idempotency key: a second call
        # under the same key replays the first row.
        for note in self.notes:
            if note["idempotency_key"] == event["idempotency_key"]:
                return {"queued": True, "replayed": True}
        subject, _, body = str(event["summary"]).partition(". ")
        self.notes.append({"project_id": project_id, "subject": subject, "body": body, **event})
        return {"queued": True}


def _adapter(runtime_kind: str, reach: dict) -> tuple[object, _Field]:
    adapter = relay_module.ProblemBoardHostRelayAdapter.__new__(relay_module.ProblemBoardHostRelayAdapter)
    adapter.config = SimpleNamespace(
        runtime_kind=runtime_kind,
        worker_name="claude-code-abc",
        worker_alias="fable-pub",
        runtime_session_id="0247bb87-0b0f-4009-aadb-e2df67c509fe",
        project_id="quickstart-works",
    )
    field = _Field(reach)
    adapter.field = field
    return adapter, field


DEAD = {
    "state": "not_listening",
    "session_state": "working",
    "pending_messages": 1,
    "last_inbox_check_at": "2026-09-18T06:52:06Z",
    "overdue_by_seconds": 17730,
}
ALIVE = {
    "state": "listening",
    "session_state": "working",
    "pending_messages": 0,
    "last_inbox_check_at": "2026-09-18T11:50:11Z",
    "overdue_by_seconds": 0,
}


def test_dead_path_queues_one_operator_update_with_the_session_to_wake() -> None:
    adapter, field = _adapter("claude-code", DEAD)
    first = adapter._report_dead_notification_path()
    assert first == {"state": "dead", "since": "2026-09-18T06:52:06Z", "reported": True}
    assert len(field.notes) == 1
    note = field.notes[0]
    assert note["project_id"] == "quickstart-works"
    assert note["kind"] == "worker.notification_path"
    assert note["worker_name"] == "claude-code-abc"
    assert note["source_event_ref"] == "relay:" + note["idempotency_key"]
    assert note["metadata"]["state"] == "dead"
    assert note["metadata"]["pending_messages"] == 1
    assert note["metadata"]["since"] == "2026-09-18T06:52:06Z"
    assert "recipient" not in note, "an event, never mail"
    assert note["idempotency_key"] == "dead-path:claude-code-abc:dead_path:-:2026-09-18T06:52:06Z"
    assert "dead since 2026-09-18T06:52:06Z" in note["subject"]
    assert "1 message(s) waiting" in note["subject"]
    assert "0247bb87-0b0f-4009-aadb-e2df67c509fe" in note["body"]
    assert "published by the relay, not by the model" in note["body"]
    # The next cycles see the same outage and say nothing more, and the relay
    # builds a fresh adapter every cycle, so the marker must outlive this one.
    second = adapter._report_dead_notification_path()
    assert second == {"state": "dead", "since": "2026-09-18T06:52:06Z", "reported": False}
    fresh, _ = _adapter("claude-code", DEAD)
    fresh.field = field
    assert fresh._report_dead_notification_path()["reported"] is False
    assert len(field.notes) == 1


def test_recovery_queues_one_restored_update_keyed_by_the_outage_start() -> None:
    # The relay rebuilds the adapter every cycle, so each step below runs on a
    # fresh one: dead, still dead across cycles, recovered. One dead note, then
    # silence, then one restored note, then silence (W198 live finding).
    adapter, field = _adapter("claude-code", DEAD)
    adapter._report_dead_notification_path()
    for _ in range(3):
        still_dead, _ = _adapter("claude-code", DEAD)
        still_dead.field = field
        assert still_dead._report_dead_notification_path()["reported"] is False
    assert len(field.notes) == 1
    recovered, _ = _adapter("claude-code", ALIVE)
    recovered.field = field
    field.reach = dict(ALIVE)
    restored = recovered._report_dead_notification_path()
    assert restored == {"state": "restored", "since": "2026-09-18T06:52:06Z", "reported": True}
    assert len(field.notes) == 2
    assert field.notes[1]["idempotency_key"] == "dead-path-restored:claude-code-abc:dead_path:-:2026-09-18T06:52:06Z"
    assert "restored" in field.notes[1]["subject"]
    # Alive stays quiet, on this adapter and on the next fresh one.
    assert recovered._report_dead_notification_path() == {"state": "alive", "since": "", "reported": False}
    quiet, _ = _adapter("claude-code", ALIVE)
    quiet.field = field
    assert quiet._report_dead_notification_path()["reported"] is False
    assert len(field.notes) == 2


def test_alive_worker_never_reports() -> None:
    adapter, field = _adapter("claude-code", ALIVE)
    assert adapter._report_dead_notification_path()["reported"] is False
    assert field.notes == []


def test_detached_or_retired_sessions_are_not_dead_paths() -> None:
    adapter, field = _adapter("claude-code", {**DEAD, "session_state": "detached"})
    assert adapter._report_dead_notification_path()["state"] == "alive"
    assert field.notes == []


CODEX_QUEUED_OVERDUE = {
    "state": "not_listening",
    "session_state": "working",
    "pending_messages": 3,
    "last_inbox_check_at": "2026-09-19T11:58:32Z",
    "overdue_by_seconds": 4300,
    "wake_state": "queued_overdue",
    "wake_overdue_since": "2026-09-19T12:07:00Z",
    "wake_queued_overdue_since": "2026-09-19T12:07:00Z",
    "wake_retry_exhausted_since": "",
    "wake_last_error": "",
    "outstanding_wake_id": "wake_0a806c1efff34bdb8341f7334e6bf632",
}
CODEX_CONSUMED_OVERDUE = {
    "state": "listening",
    "session_state": "working",
    "pending_messages": 2,
    "last_inbox_check_at": "2026-09-19T17:39:24Z",
    "overdue_by_seconds": 0,
    "wake_state": "consumed_overdue",
    "wake_overdue_since": "2026-09-19T17:45:00Z",
    "wake_queued_overdue_since": "",
    "wake_retry_exhausted_since": "2026-09-19T17:45:00Z",
    "wake_last_error": "",
    "outstanding_wake_id": "wake_5d1f0c2e9a7b4c33a1e2f3d4c5b6a798",
}
CODEX_FAILED_OVERDUE = {
    **CODEX_CONSUMED_OVERDUE,
    "wake_state": "failed_overdue",
    "wake_overdue_since": "2026-09-19T17:52:00Z",
    "wake_retry_exhausted_since": "2026-09-19T17:52:00Z",
    "wake_last_error": "codex_queue_failed",
}
CODEX_IDLE = {
    "state": "not_listening",
    "session_state": "waiting",
    "pending_messages": 0,
    "last_inbox_check_at": "2026-09-19T11:58:32Z",
    "overdue_by_seconds": 4300,
    "wake_state": "",
    "wake_overdue_since": "",
    "wake_queued_overdue_since": "",
    "wake_retry_exhausted_since": "",
    "wake_last_error": "",
    "outstanding_wake_id": "",
}


def test_codex_lapsed_inbox_checks_alone_are_not_a_dead_path() -> None:
    # The relay owns a Codex session's wake, so silence between wakes is idle,
    # not dead. Only an untaken wake says the path is dead.
    adapter, field = _adapter("codex", CODEX_IDLE)
    assert adapter._report_dead_notification_path()["state"] == "alive"
    assert field.notes == []


def test_codex_wake_still_queued_past_the_threshold_is_a_queue_notice_not_a_dead_path() -> None:
    adapter, field = _adapter("codex", CODEX_QUEUED_OVERDUE)
    first = adapter._report_dead_notification_path()
    # State, key, subject, body and log all say queued; none says dead.
    assert first == {"state": "queued", "since": "2026-09-19T12:07:00Z", "reported": True}
    assert len(field.notes) == 1
    note = field.notes[0]
    assert note["kind"] == "worker.notification_path"
    assert note["metadata"]["state"] == "wake_queued"
    assert note["idempotency_key"] == "wake-queued:claude-code-abc:queued:wake_0a806c1efff34bdb8341f7334e6bf632:2026-09-19T12:07:00Z"
    assert field.record["kind"] == "queued"
    assert field.record["wake_id"] == "wake_0a806c1efff34bdb8341f7334e6bf632"
    # A wake still queued is a factual notice, not a dead-session verdict.
    assert note["subject"].startswith("fable-pub: wake still queued since 2026-09-19T12:07:00Z")
    assert "3 message(s) waiting" in note["subject"]
    assert "wake_0a806c1efff34bdb8341f7334e6bf632" in note["body"]
    assert note["body"].startswith("Wake still queued.")
    # The timing sentence follows the stamps: since is the first expired
    # acknowledgement deadline, about a minute after enqueue, and the report
    # comes after the threshold has elapsed on top of it.
    assert "first acknowledgement deadline passed at 2026-09-19T12:07:00Z, about a minute after it was enqueued" in note["body"]
    assert "a listing taken 300 seconds or more after that" in note["body"]
    assert "has not started a turn on it, and nothing more" in note["body"]
    assert re.search(r"\bdead\b", note["body"]) is None
    assert re.search(r"\bdead\b", note["subject"]) is None
    assert "will not enqueue a second wake" in note["body"]
    assert "0247bb87-0b0f-4009-aadb-e2df67c509fe" in note["body"]
    # Same incident, no second note.
    assert adapter._report_dead_notification_path()["reported"] is False
    assert len(field.notes) == 1
    # The wake is taken: the incident closes as a queue notice, keyed by its
    # start, and never as a restored dead path.
    field.reach = {**CODEX_IDLE, "session_state": "working", "last_inbox_check_at": "2026-09-19T17:17:06Z"}
    cleared = adapter._report_dead_notification_path()
    assert cleared == {"state": "queue_cleared", "since": "2026-09-19T12:07:00Z", "reported": True}
    assert field.notes[1]["idempotency_key"] == "wake-queued-cleared:claude-code-abc:queued:wake_0a806c1efff34bdb8341f7334e6bf632:2026-09-19T12:07:00Z"
    assert field.notes[1]["subject"] == "fable-pub: wake no longer queued (queued since 2026-09-19T12:07:00Z)"
    assert re.search(r"\bdead\b", field.notes[1]["body"]) is None
    assert "restored" not in field.notes[1]["subject"]
    assert field.record == {}


def test_queued_incident_that_becomes_consumed_closes_as_queue_and_opens_as_dead_path() -> None:
    # Cross-state on the same wake: queued past the threshold, then the session
    # takes it and its retry and acknowledges neither. The queued notice is
    # closed under its own key, the dead path is opened under its own key,
    # and a crash between them is repaired the same way as any other.
    adapter, field = _adapter("codex", CODEX_QUEUED_OVERDUE)
    adapter._report_dead_notification_path()
    assert field.record["kind"] == "queued"
    consumed = {**CODEX_CONSUMED_OVERDUE, "outstanding_wake_id": "wake_0a806c1efff34bdb8341f7334e6bf632"}
    field.reach = consumed
    # The queued close note goes out and the process dies before any record
    # write lands: the record still says queued, open note enqueued. The next
    # cycle must find the close in the outbox, not send it again, and open
    # the dead path once.
    field.fail_record_writes = True
    first = _adapter("codex", consumed)[0]
    first.field = field
    first._report_dead_notification_path()
    assert field.record["kind"] == "queued"
    field.fail_record_writes = False
    second = _adapter("codex", consumed)[0]
    second.field = field
    result = second._report_dead_notification_path()
    assert result == {"state": "dead", "since": "2026-09-19T17:45:00Z", "reported": True}
    keys = [n["idempotency_key"] for n in field.notes]
    assert keys == [
        "wake-queued:claude-code-abc:queued:wake_0a806c1efff34bdb8341f7334e6bf632:2026-09-19T12:07:00Z",
        "wake-queued-cleared:claude-code-abc:queued:wake_0a806c1efff34bdb8341f7334e6bf632:2026-09-19T12:07:00Z",
        "dead-path:claude-code-abc:consumed_overdue:wake_0a806c1efff34bdb8341f7334e6bf632:2026-09-19T17:45:00Z",
    ]
    assert field.record["kind"] == "consumed_overdue"
    assert field.record["open_note"] == "enqueued"


def test_codex_wake_consumed_twice_without_acknowledgement_is_a_dead_path() -> None:
    # The session took the wake and its one retry and acknowledged neither.
    # Inbox checks look current, so only the wake condition says the path is
    # dead. The note names the receive command that clears it.
    adapter, field = _adapter("codex", CODEX_CONSUMED_OVERDUE)
    first = adapter._report_dead_notification_path()
    assert first == {"state": "dead", "since": "2026-09-19T17:45:00Z", "reported": True}
    assert len(field.notes) == 1
    note = field.notes[0]
    assert note["idempotency_key"] == "dead-path:claude-code-abc:consumed_overdue:wake_5d1f0c2e9a7b4c33a1e2f3d4c5b6a798:2026-09-19T17:45:00Z"
    assert "took it, and took the one retry" in note["body"]
    assert "pb worker receive --wake-id wake_5d1f0c2e9a7b4c33a1e2f3d4c5b6a798" in note["body"]
    assert "will not enqueue another" in note["body"]
    assert adapter._report_dead_notification_path()["reported"] is False
    assert len(field.notes) == 1


def test_failed_then_consumed_on_the_same_wake_and_stamp_are_two_incidents() -> None:
    # The retry failed at the queue (failed_overdue), and a later listing
    # reads the wake as consumed under the same exhausted stamp. Same wake,
    # same since, different kind: two incidents, two open keys, the first
    # closed under its own key, the second's open not swallowed by a replay
    # of the first (codex-ui review of 5857dffa).
    adapter, field = _adapter("codex", CODEX_FAILED_OVERDUE)
    adapter._report_dead_notification_path()
    consumed = {**CODEX_FAILED_OVERDUE, "wake_state": "consumed_overdue", "wake_last_error": ""}
    field.reach = consumed
    second, _ = _adapter("codex", consumed)
    second.field = field
    result = second._report_dead_notification_path()
    assert result == {"state": "dead", "since": "2026-09-19T17:52:00Z", "reported": True}
    keys = [n["idempotency_key"] for n in field.notes]
    assert keys == [
        "dead-path:claude-code-abc:failed_overdue:wake_5d1f0c2e9a7b4c33a1e2f3d4c5b6a798:2026-09-19T17:52:00Z",
        "dead-path-restored:claude-code-abc:failed_overdue:wake_5d1f0c2e9a7b4c33a1e2f3d4c5b6a798:2026-09-19T17:52:00Z",
        "dead-path:claude-code-abc:consumed_overdue:wake_5d1f0c2e9a7b4c33a1e2f3d4c5b6a798:2026-09-19T17:52:00Z",
    ]
    assert "took it, and took the one retry" in field.notes[2]["body"]
    assert field.record["kind"] == "consumed_overdue" and field.record["open_note"] == "enqueued"


def test_a_v1_record_is_read_as_a_dead_path_with_its_phases() -> None:
    adapter, field = _adapter("claude-code", DEAD)
    # The store maps v1 names; the double stores what it is given, so map here
    # the way dead_path_record does and check the reporter resumes the phase.
    field.record = {"since": "2026-09-18T06:52:06Z", "kind": "dead_path", "open_note": "enqueued"}
    assert adapter._report_dead_notification_path()["reported"] is False
    assert field.notes == []


def test_codex_retry_that_never_reached_the_queue_is_reported_as_such() -> None:
    # The session took the first wake without acknowledging it, and the one
    # retry failed at the queue boundary. The note must not say the session
    # took a retry it was never offered; it names the queue error instead.
    adapter, field = _adapter("codex", CODEX_FAILED_OVERDUE)
    first = adapter._report_dead_notification_path()
    assert first == {"state": "dead", "since": "2026-09-19T17:52:00Z", "reported": True}
    note = field.notes[0]
    assert "took the one retry" not in note["body"]
    assert "could not be queued" in note["body"]
    assert "codex_queue_failed" in note["body"]
    assert "pb worker receive --wake-id wake_5d1f0c2e9a7b4c33a1e2f3d4c5b6a798" in note["body"]
    assert adapter._report_dead_notification_path()["reported"] is False
    assert len(field.notes) == 1


def test_a_refused_note_is_retried_next_cycle_until_one_is_delivered() -> None:
    # The note goes out before the marker is written. A refusal keeps the
    # marker empty so the next cycle, on a fresh adapter, tries again; the
    # outage is announced exactly once, and never claimed announced while it
    # was not (W198 review on f3c2937b).
    adapter, field = _adapter("claude-code", DEAD)
    field.raise_on_enqueue = True
    result = adapter._report_dead_notification_path()
    assert result == {"state": "dead", "since": "2026-09-18T06:52:06Z", "reported": False, "retry": True}
    assert field.notes == []
    assert field.reported_since == ""
    field.raise_on_enqueue = False
    retry, _ = _adapter("claude-code", DEAD)
    retry.field = field
    assert retry._report_dead_notification_path()["reported"] is True
    assert len(field.notes) == 1
    assert field.reported_since == "2026-09-18T06:52:06Z"
    quiet, _ = _adapter("claude-code", DEAD)
    quiet.field = field
    assert quiet._report_dead_notification_path()["reported"] is False
    assert len(field.notes) == 1


def test_a_refused_restored_note_keeps_the_marker_and_is_retried() -> None:
    adapter, field = _adapter("claude-code", DEAD)
    adapter._report_dead_notification_path()
    assert len(field.notes) == 1
    field.reach = dict(ALIVE)
    field.raise_on_enqueue = True
    first, _ = _adapter("claude-code", ALIVE)
    first.field = field
    result = first._report_dead_notification_path()
    assert result == {"state": "restored", "since": "2026-09-18T06:52:06Z", "reported": False, "retry": True}
    assert field.reported_since == "2026-09-18T06:52:06Z"
    assert len(field.notes) == 1
    field.raise_on_enqueue = False
    second, _ = _adapter("claude-code", ALIVE)
    second.field = field
    assert second._report_dead_notification_path()["reported"] is True
    assert len(field.notes) == 2
    assert field.reported_since == ""
    third, _ = _adapter("claude-code", ALIVE)
    third.field = field
    assert third._report_dead_notification_path()["reported"] is False
    assert len(field.notes) == 2


def test_a_crash_between_note_and_marker_costs_one_deduplicated_retry() -> None:
    # The note was written and the process died before the marker was. The
    # next cycle retries under the same key; the pending count has moved, so
    # the outbox answers with an idempotency conflict, which means the note
    # exists. The marker is written then and nothing is sent twice.
    adapter, field = _adapter("claude-code", DEAD)
    writes = {"count": 0}
    real_write = field.write_dead_path_record

    def intent_then_fail(worker_name: str, record) -> None:
        writes["count"] += 1
        if writes["count"] == 2:
            raise DomainError("field_record_not_found", "gone", status=404)
        real_write(worker_name, record)

    field.write_dead_path_record = intent_then_fail  # type: ignore[assignment]
    adapter._report_dead_notification_path()
    assert len(field.notes) == 1
    assert {k: field.record[k] for k in ("since", "open_note")} == {
        "since": "2026-09-18T06:52:06Z", "open_note": "pending"
    }
    field.write_dead_path_record = real_write  # type: ignore[assignment]
    field.reach = {**DEAD, "pending_messages": 4}
    retry, _ = _adapter("claude-code", field.reach)
    retry.field = field
    assert retry._report_dead_notification_path()["reported"] is True
    assert len(field.notes) == 1
    assert field.reported_since == "2026-09-18T06:52:06Z"


def test_intent_that_cannot_be_written_gates_the_note() -> None:
    # The initial intent is a gate. With no record, a note that went out
    # before the process died could never be matched to a recovery, so when
    # the record cannot be written nothing is enqueued and the next cycle
    # tries the whole step again (codex-main's review of 1fc8b990).
    adapter, field = _adapter("claude-code", DEAD)
    field.fail_record_writes = True
    result = adapter._report_dead_notification_path()
    assert result == {"state": "dead", "since": "2026-09-18T06:52:06Z", "reported": False, "retry": True}
    assert field.notes == [] and field.record == {}
    field.fail_record_writes = False
    retry, _ = _adapter("claude-code", DEAD)
    retry.field = field
    assert retry._report_dead_notification_path()["reported"] is True
    assert len(field.notes) == 1
    assert {k: field.record[k] for k in ("since", "open_note", "kind")} == {
        "since": "2026-09-18T06:52:06Z", "open_note": "enqueued", "kind": "dead_path"
    }


def test_dead_note_out_and_phase_lost_then_recovery_before_the_next_cycle() -> None:
    # codex-ui's first cross-state case: the intent is durable, the dead note
    # is enqueued, the phase advance fails, and the session recovers before
    # the next fresh adapter runs. The receipt says the note is out, so the
    # recovery it owes is sent: exactly one dead note and one restored note.
    adapter, field = _adapter("claude-code", DEAD)
    writes = {"count": 0}
    real_write = field.write_dead_path_record

    def intent_then_fail(worker_name: str, record) -> None:
        writes["count"] += 1
        if writes["count"] == 2:
            raise DomainError("field_record_not_found", "gone", status=404)
        real_write(worker_name, record)

    field.write_dead_path_record = intent_then_fail  # type: ignore[assignment]
    adapter._report_dead_notification_path()
    assert len(field.notes) == 1
    assert {k: field.record[k] for k in ("since", "open_note")} == {
        "since": "2026-09-18T06:52:06Z", "open_note": "pending"
    }
    field.write_dead_path_record = real_write  # type: ignore[assignment]
    field.reach = dict(ALIVE)
    alive, _ = _adapter("claude-code", ALIVE)
    alive.field = field
    result = alive._report_dead_notification_path()
    assert result == {"state": "restored", "since": "2026-09-18T06:52:06Z", "reported": True}
    assert len(field.notes) == 2
    assert field.notes[1]["idempotency_key"] == "dead-path-restored:claude-code-abc:dead_path:-:2026-09-18T06:52:06Z"
    assert field.record == {}


def test_recovery_out_and_phase_lost_then_a_new_outage_before_the_next_cycle() -> None:
    # codex-ui's second cross-state case: the restored note is enqueued, the
    # record still says recovery pending, and a new outage begins before the
    # next fresh adapter. The old recovery is found in the outbox, the record
    # is dropped, and the new outage is reported once under its own start.
    adapter, field = _adapter("claude-code", DEAD)
    adapter._report_dead_notification_path()
    field.reach = dict(ALIVE)
    writes = {"count": 0}
    real_write = field.write_dead_path_record

    def intent_then_fail(worker_name: str, record) -> None:
        # The recovery intent is written; the clear after the enqueue fails.
        writes["count"] += 1
        if writes["count"] == 2:
            raise DomainError("field_record_not_found", "gone", status=404)
        real_write(worker_name, record)

    field.write_dead_path_record = intent_then_fail  # type: ignore[assignment]
    recovered, _ = _adapter("claude-code", ALIVE)
    recovered.field = field
    recovered._report_dead_notification_path()
    assert len(field.notes) == 2
    field.write_dead_path_record = real_write  # type: ignore[assignment]
    # The record is as the crash left it: dead enqueued, recovery pending.
    assert field.record["close_note"] == "pending"
    new_outage = {**DEAD, "last_inbox_check_at": "2026-09-18T12:00:00Z"}
    field.reach = new_outage
    again, _ = _adapter("claude-code", new_outage)
    again.field = field
    result = again._report_dead_notification_path()
    assert result == {"state": "dead", "since": "2026-09-18T12:00:00Z", "reported": True}
    assert len(field.notes) == 3
    assert field.notes[2]["idempotency_key"] == "dead-path:claude-code-abc:dead_path:-:2026-09-18T12:00:00Z"
    assert {k: field.record[k] for k in ("since", "open_note")} == {
        "since": "2026-09-18T12:00:00Z", "open_note": "enqueued"
    }
    # And the old outage's recovery was not sent twice.
    assert sum(1 for n in field.notes if n["idempotency_key"].startswith("dead-path-restored:")) == 1


def test_unreadable_reachability_is_reported_as_unknown() -> None:
    adapter, field = _adapter("claude-code", DEAD)

    def boom(_name: str) -> dict:
        raise DomainError("work_worker_not_found", "gone", status=404)

    field.worker_reachability = boom  # type: ignore[assignment]
    assert adapter._report_dead_notification_path() == {"state": "unknown"}

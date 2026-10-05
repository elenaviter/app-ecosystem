"""A wake waiting in the native queue does not re-read the mailbox (W448 fix 3).

On dev-main, 2026-10-05 01:18-01:34Z, after fixes 1 and 2, the relay's
accounting still named ``SharedFieldStore.pending_worker_mail_refs`` as the
holder of a channel's store thread 22 times, up to 10.7 s. The relay log had
80 ``wake deduplicated`` cycles for the coordinator in those 15 minutes and one
push: every cycle read its 382-message inbox twice, though a wake already in
the native queue is never pushed again and the cycle could only end as a
deduplicated wake. While a queued wake waits before its deadline, its own refs
now stand in for the mailbox; nothing is kept between cycles (the operator,
2026-10-03: "we are distributed system, and no we do not use any module level
caches!"). Past the deadline, or in any other wake state, the mailbox is read
as before.
"""

from __future__ import annotations

import asyncio

import pytest

from project_board.client import relay
from project_board.client import store as store_module
from project_board.client.store import SharedFieldStore
from relay_helpers import make_host, make_supervisor

WAKE = "wake_0000000000000000000000000000f448"
REAL_FUTURE = store_module._future


def _queued(tmp_path, monkeypatch, *, deadline_passed: bool = False):
    host, identity, channel = make_host(tmp_path)
    field = SharedFieldStore(host.field_root)
    field.initialize(field_id="w448-fix3")
    field.register_worker(
        worker_name=identity.worker_name, worker_identity=identity.worker_identity,
        runtime_kind=identity.runtime_kind, runtime_session_id=identity.runtime_session_id,
        capabilities=[], authority_label="connection-hub:test-profile", control_plane_state="published",
    )
    field.listen_worker(identity.worker_name, check_interval_seconds=30)
    refs = [
        field.send_mail("", sender="control-plane", recipient=identity.worker_name, kind="request",
                        subject=f"Pending {number}", body="Waiting.",
                        idempotency_key=f"pending-{number}")["message_ref"]
        for number in range(3)
    ]
    if deadline_passed:
        monkeypatch.setattr(store_module, "_future", lambda seconds: "2020-01-01T00:00:00Z")
    field.prepare_worker_session_wake(identity.worker_name, message_refs=refs, wake_id=WAKE)
    field.record_worker_session_delivery(
        identity.worker_name, adapter="codex-queue", state="attached", event_kind="input.available",
        delivered=True, message_refs=refs, wake_id=WAKE, prepared=True, queued_submission_id="sub-1",
    )
    monkeypatch.setattr(store_module, "_future", REAL_FUTURE)
    subscription = field.worker_listener_session(identity.worker_name)["subscription"]
    assert subscription["outstanding_wake_id"] == WAKE
    assert subscription["wake_delivery_state"] == "queued"
    return host, identity, channel, field, refs


@pytest.fixture
def driven(monkeypatch):
    """The relay's wake decision with the queue listing and the push replaced."""

    reads: list[str] = []
    real_read = SharedFieldStore.pending_worker_mail_refs

    def counted(self, worker_name, *, wait=True):
        reads.append(worker_name)
        return real_read(self, worker_name, wait=wait)

    monkeypatch.setattr(SharedFieldStore, "pending_worker_mail_refs", counted)
    monkeypatch.setattr(relay, "session_with_limit_state",
                        lambda listener, **_: {**listener, "limit_state": {"kind": "ok", "resets_at": ""}})

    def drive(host, channel):
        supervisor = make_supervisor(host)
        pushes: list[dict] = []

        async def reconcile(*_args, **_kwargs):
            return None

        async def notify(_host, _channel, **kwargs):
            pushes.append(kwargs)
            return {"delivered": True}

        monkeypatch.setattr(supervisor, "_reconcile_session_queue", reconcile)
        monkeypatch.setattr(supervisor, "_notify_session", notify)
        result = asyncio.run(supervisor._notify_available_input(host, channel))
        return result, pushes

    return reads, drive


def test_a_queued_wake_before_its_deadline_reads_no_mailbox(tmp_path, monkeypatch, driven):
    reads, drive = driven
    host, _identity, channel, _field, _refs = _queued(tmp_path, monkeypatch)

    for _cycle in range(4):
        result, pushes = drive(host, channel)
        assert result["deduplicated"] is True and result["wake_id"] == WAKE
        assert pushes == [], "a queued wake is never pushed again"

    assert reads == [], "four cycles read the mailbox 8 times before fix 3"


def test_the_skip_is_said_in_the_dedup_line(tmp_path, monkeypatch, driven, caplog):
    _reads, drive = driven
    host, _identity, channel, _field, _refs = _queued(tmp_path, monkeypatch)
    with caplog.at_level("INFO", logger="project_board.client.relay"):
        drive(host, channel)
    lines = [record.getMessage() for record in caplog.records if "wake deduplicated" in record.getMessage()]
    assert lines and lines[-1].endswith("pending=3 mailbox=unread_queued_wake")


def test_past_the_deadline_the_mailbox_is_read_and_new_mail_rides_the_retry(tmp_path, monkeypatch, driven):
    reads, drive = driven
    host, identity, channel, field, refs = _queued(tmp_path, monkeypatch, deadline_passed=True)
    later = field.send_mail("", sender="control-plane", recipient=identity.worker_name, kind="request",
                            subject="Arrived during the wait", body="New.", idempotency_key="later")["message_ref"]

    _result, pushes = drive(host, channel)

    assert reads, "past its deadline the wake decision reads the mailbox again"
    assert len(pushes) == 1 and pushes[0]["retried"] is True and pushes[0]["wake_id"] == WAKE
    assert set(pushes[0]["message_refs"]) == {*refs, later}


def test_mail_that_arrives_during_a_queued_wake_stays_pending(tmp_path, monkeypatch, driven):
    _reads, drive = driven
    host, identity, channel, field, refs = _queued(tmp_path, monkeypatch)
    later = field.send_mail("", sender="control-plane", recipient=identity.worker_name, kind="request",
                            subject="Arrived during the wait", body="New.", idempotency_key="later")["message_ref"]

    drive(host, channel)

    assert later
    assert field.pending_worker_mail_count_snapshot(identity.worker_name) == len(refs) + 1, \
        "the skipped read changed no delivery state: the new mail waits for the next receive"


def test_a_rate_limited_agent_with_a_queued_wake_is_still_deferred(tmp_path, monkeypatch, driven):
    _reads, drive = driven
    host, identity, channel, field, _refs = _queued(tmp_path, monkeypatch)
    monkeypatch.setattr(
        relay, "session_with_limit_state",
        lambda listener, **_: {**listener, "limit_state": {"kind": "rate_limited", "resets_at": "2999-01-01T00:00:00Z"}},
    )

    result, pushes = drive(host, channel)

    assert result["wake_deferred"] is True and result["wake_deferred_until"] == "2999-01-01T00:00:00Z"
    assert pushes == []
    assert field.wake_hold(identity.worker_name), "the card still shows the hold (W334)"


@pytest.mark.parametrize(
    ("subscription", "expected"),
    [
        ({"outstanding_wake_id": WAKE, "wake_delivery_state": "queued",
          "wake_ack_deadline_at": "2999-01-01T00:00:00Z", "last_wake_message_refs": ["work:mail:a"]}, ["work:mail:a"]),
        ({"outstanding_wake_id": WAKE, "wake_delivery_state": "queued",
          "wake_ack_deadline_at": "2020-01-01T00:00:00Z", "last_wake_message_refs": ["work:mail:a"]}, None),
        ({"outstanding_wake_id": WAKE, "wake_delivery_state": "queued",
          "wake_ack_deadline_at": "", "last_wake_message_refs": ["work:mail:a"]}, None),
        ({"outstanding_wake_id": WAKE, "wake_delivery_state": "queued",
          "wake_ack_deadline_at": "not-a-time", "last_wake_message_refs": ["work:mail:a"]}, None),
        ({"outstanding_wake_id": WAKE, "wake_delivery_state": "failed",
          "wake_ack_deadline_at": "2999-01-01T00:00:00Z", "last_wake_message_refs": ["work:mail:a"]}, None),
        ({"outstanding_wake_id": WAKE, "wake_delivery_state": "consumed",
          "wake_ack_deadline_at": "2999-01-01T00:00:00Z", "last_wake_message_refs": ["work:mail:a"]}, None),
        ({"outstanding_wake_id": WAKE, "wake_delivery_state": "queued",
          "wake_ack_deadline_at": "2999-01-01T00:00:00Z", "last_wake_message_refs": []}, None),
        ({"wake_delivery_state": "queued", "wake_ack_deadline_at": "2999-01-01T00:00:00Z",
          "last_wake_message_refs": ["work:mail:a"]}, None),
    ],
    ids=["queued-before-deadline", "deadline-passed", "no-deadline", "unreadable-deadline",
         "failed", "consumed", "no-refs", "no-outstanding-wake"],
)
def test_only_a_queued_wake_before_its_deadline_stands_in_for_the_mailbox(subscription, expected):
    assert relay._queued_wake_refs({"state": "attached", "subscription": subscription}) == expected


def test_no_listener_or_subscription_reads_the_mailbox():
    assert relay._queued_wake_refs(None) is None
    assert relay._queued_wake_refs({"state": "attached"}) is None
    assert relay._queued_wake_refs({"subscription": "not-a-mapping"}) is None

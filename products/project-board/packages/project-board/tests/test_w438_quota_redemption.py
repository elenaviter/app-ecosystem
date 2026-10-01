"""Early native quota redemption must release one held, same-session wake."""

from __future__ import annotations

import asyncio
import hashlib
import pytest
from types import SimpleNamespace

from project_board.client import relay, store as store_module
from project_board.client.limit_state import limit_state_from_codex
from project_board.contract.errors import DomainError
from relay_helpers import make_supervisor
from test_w390_limit_refused_wake import _stranded, _subscription, WAKE

EMAIL = "worker@example.test"
NOW = "2026-10-01T00:41:00Z"
REFUSED = "2026-10-01T00:40:01Z"


def _capacity(session_id, *, percent=0):
    state = limit_state_from_codex({
        "primary": {"used_percent": percent, "window_minutes": 300,
                    "resets_at": 1790892000},
        "secondary": {"used_percent": percent, "window_minutes": 10080,
                      "resets_at": 1791496800},
    }, observed_at=NOW)
    state.update(source="codex-app-server", runtime_session_id=session_id,
                 account_email_sha256=hashlib.sha256(EMAIL.encode()).hexdigest(),
                 account_bound=True, limit_id="codex")
    state["buckets"] = {"codex": dict(state)}
    return state


def _held(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "utc_now", lambda: "2026-10-01T00:40:00Z")
    host, identity, channel, field = _stranded(tmp_path, monkeypatch)
    monkeypatch.setattr(store_module, "utc_now", lambda: NOW)
    monkeypatch.setattr(relay, "utc_now", lambda: NOW)
    field.record_worker_board_record(identity.worker_name, {
        "runtime_account": {"email": EMAIL},
    })
    old = {"kind": "rate_limited", "source": "codex-rollout",
           "refusal": "usage_limit_exceeded", "observed_at": REFUSED,
           "resets_at": "2999-01-01T00:00:00Z", "limit_id": "codex",
           "windows": [{"name": "secondary", "used_percent": 100,
                        "window_minutes": 10080, "resets_at": "2999-01-01T00:00:00Z"}]}
    monkeypatch.setattr(relay, "session_with_limit_state",
                        lambda listener, **_: {**listener, "limit_state": old})
    monkeypatch.setattr(relay, "codex_limit_state", lambda *_args, **_kwargs: old)
    supervisor = make_supervisor(host)
    pushes = []

    async def reconcile(*_args, **_kwargs):
        return None

    async def notify(_host, _channel, **kwargs):
        pushes.append(kwargs)
        field.prepare_worker_session_wake(identity.worker_name,
            message_refs=kwargs["message_refs"], wake_id=kwargs["wake_id"], retry=True)
        field.record_worker_session_delivery(identity.worker_name,
            adapter="codex-queue", state="attached", event_kind="input.available",
            delivered=True, message_refs=kwargs["message_refs"], wake_id=kwargs["wake_id"],
            prepared=True, queued_submission_id="quota-recovered")
        return {"delivered": True, "wake_id": kwargs["wake_id"]}

    monkeypatch.setattr(supervisor, "_reconcile_session_queue", reconcile)
    monkeypatch.setattr(supervisor, "_notify_session", notify)
    return host, identity, channel, field, supervisor, pushes


def test_early_redemption_rearms_only_one_current_same_session_wake(tmp_path, monkeypatch):
    host, identity, channel, field, supervisor, pushes = _held(tmp_path, monkeypatch)
    reads = []

    async def native(**kwargs):
        reads.append(kwargs)
        return _capacity(identity.runtime_session_id)

    supervisor._codex_quota_reader = native
    result = asyncio.run(supervisor._notify_available_input(host, channel))
    assert len(pushes) == 1, "fresh matched capacity before the old reset must release the held wake"
    assert pushes[0]["wake_id"] == WAKE and pushes[0]["retried"] is True
    assert reads[0]["runtime_session_id"] == identity.runtime_session_id
    assert not result.get("wake_deferred")
    assert not field.wake_hold(identity.worker_name)
    assert _subscription(field, identity.worker_name)["wake_quota_rearmed_for"] == WAKE

    asyncio.run(supervisor._notify_available_input(host, channel))
    assert len(pushes) == 1 and len(reads) == 1, "duplicate observations neither query nor push in a loop"


def test_still_exhausted_and_read_errors_submit_no_model_turn(tmp_path, monkeypatch):
    host, identity, channel, field, supervisor, pushes = _held(tmp_path, monkeypatch)

    async def native(**_kwargs):
        return _capacity(identity.runtime_session_id, percent=100)

    supervisor._codex_quota_reader = native
    assert asyncio.run(supervisor._notify_available_input(host, channel))["wake_deferred"]
    assert pushes == []

    supervisor._codex_quota_refresh_at.clear()

    async def unavailable(**_kwargs):
        raise DomainError("work_codex_quota_account_mismatch", "Account mismatch.")

    supervisor._codex_quota_reader = unavailable
    result = asyncio.run(supervisor._notify_available_input(host, channel))
    assert result["wake_deferred"] and pushes == []
    assert result["reason"] == "work_codex_quota_account_mismatch"


def test_no_pending_work_never_submits_a_turn(tmp_path, monkeypatch):
    host, identity, channel, field, supervisor, pushes = _held(tmp_path, monkeypatch)
    leased = field.pull_mail("", worker_name=identity.worker_name, lease_owner="test", limit=10)
    for message in leased:
        field.settle_mail("", worker_name=identity.worker_name,
            message_ref=message["message_ref"], lease_id=message["lease"]["lease_id"], lease_owner="test",
            outcome="acknowledged", summary="Synthetic work ended.")

    async def native(**_kwargs):
        raise AssertionError("no pending work needs no recovery probe")

    supervisor._codex_quota_reader = native
    asyncio.run(supervisor._notify_available_input(host, channel))
    assert pushes == []


def test_unmeasured_read_stays_held_on_cached_cycles_without_spinning(tmp_path, monkeypatch):
    host, identity, channel, _field, supervisor, pushes = _held(tmp_path, monkeypatch)
    reads = []

    async def native(**_kwargs):
        reads.append(1)
        return {**_capacity(identity.runtime_session_id), "kind": "unknown", "buckets": {}}

    supervisor._codex_quota_reader = native
    for _ in range(2):
        result = asyncio.run(supervisor._notify_available_input(host, channel))
        assert result["wake_deferred"] and result["reason"] == "work_codex_quota_unmeasured"
    assert reads == [1] and pushes == []


def test_work_ending_during_the_native_read_cannot_wake_stale_captured_input(tmp_path, monkeypatch):
    host, identity, channel, field, supervisor, pushes = _held(tmp_path, monkeypatch)

    async def native(**_kwargs):
        field.detach_worker_listener(identity.worker_name)
        return _capacity(identity.runtime_session_id)

    supervisor._codex_quota_reader = native
    asyncio.run(supervisor._notify_available_input(host, channel))
    assert pushes == [] and not field.wake_hold(identity.worker_name)


def test_quota_rearm_allowance_is_durable_and_not_per_observation(tmp_path, monkeypatch):
    host, identity, channel, field, supervisor, pushes = _held(tmp_path, monkeypatch)
    monkeypatch.setattr(store_module, "_future", lambda _: "2020-01-01T00:00:00Z")

    async def native(**_kwargs):
        return _capacity(identity.runtime_session_id)

    supervisor._codex_quota_reader = native
    asyncio.run(supervisor._notify_available_input(host, channel))
    assert len(pushes) == 1
    # The one recovery was consumed without receive: restore the ordinary
    # exhausted ceiling using the real queue reconciliation/store path.
    monkeypatch.setattr(store_module, "_future", lambda _: "2020-01-01T00:00:00Z")
    for _ in range(3):
        field.record_worker_session_queue_reconciliation(identity.worker_name,
            expected_wake_id=WAKE, result={"reconciled": True, "queued_submission_ids": []})
    assert _subscription(field, identity.worker_name).get("wake_retry_exhausted_since")
    newer = {**_capacity(identity.runtime_session_id), "observed_at": "2026-10-01T00:42:00Z"}
    field.record_runtime_limit_state(identity.worker_name, newer)
    monkeypatch.setattr(store_module, "utc_now", lambda: "2026-10-01T00:42:00Z")
    assert not field.rearm_limit_consumed_wake(identity.worker_name, wake_id=WAKE,
        refused_at=REFUSED, cleared_at=newer["observed_at"], quota_observed_at=newer["observed_at"],
        runtime_session_id=identity.runtime_session_id), "even a newer reading cannot grant another recovery of this wake"
    restarted = make_supervisor(host)
    assert not restarted._codex_quota_refresh_at
    assert _subscription(field, identity.worker_name)["wake_quota_rearmed_for"] == WAKE


def test_recovery_receive_reads_current_assignment_and_old_ownership_stays_refused(tmp_path, monkeypatch):
    host, identity, channel, field, supervisor, pushes = _held(tmp_path, monkeypatch)
    field.create_project(project_id="one", title="Synthetic", goal="Fenced work", owner="test")
    field.sync_worker_attendances(identity.worker_name, ["work:project:one"])
    assignment = {"assignment_ref": "work:assignment:20261001T004000Z:assignment_quota:synthetic",
        "project_ref": "work:project:one", "work_ref": "work:plan:node:20261001T004000Z:w438:synthetic",
        "worker_name": identity.worker_name, "title": "Current assignment", "ownership_version": 1,
        "task": {"instructions": "Old assignment before exhaustion."}}
    field.materialize_assignment("one", assignment)
    current = field.materialize_assignment("one", {**assignment, "ownership_version": 2,
        "task": {"instructions": "Read this live current assignment, not the old notice."}})
    notice = field.send_assignment_notice("one", assignment=current,
        recipient=identity.worker_name, item_status="working")

    async def native(**_kwargs):
        return _capacity(identity.runtime_session_id)

    supervisor._codex_quota_reader = native
    asyncio.run(supervisor._notify_available_input(host, channel))
    assert len(pushes) == 1 and pushes[0]["wake_id"] == WAKE
    assert notice["message_ref"] in pushes[0]["message_refs"]
    messages = field.pull_mail("one", worker_name=identity.worker_name, lease_owner="synthetic-session")
    assert [message["message_ref"] for message in messages] == [notice["message_ref"]]
    live = field.list_assignments("one", work_ref=assignment["work_ref"])
    assert live[0]["ownership_version"] == 2 and live[0]["task"] == current["task"]
    with pytest.raises(DomainError) as old:
        field.materialize_assignment("one", assignment)
    assert old.value.code == "field_assignment_stale"
    monkeypatch.setattr(store_module, "utc_now", lambda: "2026-10-01T00:41:01Z")
    field.check_in_worker_listener(identity.worker_name, inbox_checked=True,
        message_refs=[notice["message_ref"]], wake_id=WAKE)
    assert not _subscription(field, identity.worker_name).get("outstanding_wake_id")
    assert field.worker_listener_session(identity.worker_name)["last_inbox_result_at"] == "2026-10-01T00:41:01Z"


def test_heartbeat_keeps_positive_quota_visible_without_claiming_receive(tmp_path, monkeypatch):
    _host, identity, _channel, field, _supervisor, _pushes = _held(tmp_path, monkeypatch)
    positive = {**_capacity(identity.runtime_session_id), "cleared_at": NOW,
                "reached": "capacity_available_receive_pending"}
    monkeypatch.setattr(relay, "session_with_limit_state", lambda listener, **_: {
        **listener, "limit_state": positive})
    monkeypatch.setattr(relay, "session_with_runtime_model", lambda listener, **_: listener)
    adapter = relay.ProblemBoardHostRelayAdapter.__new__(relay.ProblemBoardHostRelayAdapter)
    adapter.field = field
    adapter.config = SimpleNamespace(worker_name=identity.worker_name, runtime_kind="codex",
                                     runtime_session_id=identity.runtime_session_id)
    row = adapter._listener_sessions()[0]
    assert row["limit_state"]["kind"] == "ok" and "cleared_at" not in row["limit_state"]
    assert row["limit_state"]["reached"] == "capacity_available_receive_pending"
    assert not row.get("last_inbox_result_at"), "account reads never invent an actual receive"
    assert positive["cleared_at"] == NOW, "local rearm evidence is retained separately"

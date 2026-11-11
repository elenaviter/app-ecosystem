"""Inactive sentinels and real-store bounded-input/optimistic-commit cases.

This file is regression source, not a measured latency result. A blocking
fixture proves lock separation, not a bound on a stalled filesystem's time.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event

import pytest

from project_board.client import return_policy as policy
from project_board.client import return_reconciliation as returns
from project_board.client.io import atomic_write_json, read_json
from project_board.client.store import SharedFieldStore
from project_board.contract.errors import DomainError

PROJECT = "work:project:project-one"
WORKER = "codex-11111111-1111-4111-8111-111111111111"
NOW = "2026-11-08T10:00:00Z"


@pytest.fixture
def field(tmp_path, monkeypatch):
    monkeypatch.setattr(returns, "utc_now", lambda: NOW)
    store = SharedFieldStore(tmp_path / "field")
    store.initialize(field_id="return-census")
    store.create_project(project_id="project-one", title="Return", goal="Current work", owner="operator")
    store.register_worker(
        worker_name=WORKER, worker_identity="codex:11111111-1111-4111-8111-111111111111",
        runtime_kind="codex", runtime_session_id="11111111-1111-4111-8111-111111111111",
        capabilities=[], authority_label="connection-hub:fixture-card", control_plane_state="published",
        attended_project_refs=[PROJECT],
    )
    store.sync_project_coordinator("project-one", coordinator())
    return store


def coordinator(revision=1):
    return {"state": "held", "holder": {"worker_name": "coordinator-one", "attending": True},
            "revision": revision, "since": "2026-11-08T08:00:00Z"}


def enqueue(field, key, body="Inspect the current authority first."):
    return field.send_mail("project-one", sender="control-plane", recipient=WORKER, kind="request",
                           subject=key, body=body, idempotency_key=key)


def requested(field, state):
    enqueue(field, "old-task")
    state.record_cause("quota_hold", {"hold_id": "A", "source": "fixture"})
    [signal] = state.signals()
    context = state.request_context(PROJECT, signal["correlation_id"], "coordinator-one", coordinator())
    state.record_request(PROJECT, context, {"message_ref": "work:mail:census-A", "created_at": NOW})
    decision = {"message_ref": "work:mail:decision-A", "project_ref": PROJECT, "sender": "coordinator-one",
                "kind": "reply", "correlation_id": signal["correlation_id"],
                "reply_to": "work:mail:census-A", "created_at": NOW}
    return signal, decision


def forbid_census(*_args, **_kwargs):
    pytest.fail("An inactive return path must not enumerate or deserialize the inbox")


@pytest.mark.parametrize("via", ["prepare", "receive", "wake-ack", "lease-read", "settle"])
@pytest.mark.parametrize("project_count", [1, 17])
def test_inactive_none_no_requirements_no_episode_never_reads_headers(field, monkeypatch, via, project_count):
    state = returns.ReturnReconciliation(field, WORKER)
    enqueue(field, "ordinary-task")  # a nonempty inbox is not permission to scan
    field.sync_worker_attendances(WORKER, [PROJECT, *(
        f"work:project:project-extra-{n}" for n in range(project_count - 1)
    )])
    monkeypatch.setattr(field, "pending_mail_headers", forbid_census)
    monkeypatch.setattr(field, "pending_return_census", forbid_census)
    signals = state.prepare() if via == "prepare" else state.model_entry(via)
    assert signals == []
    row = read_json(state.path)
    assert row["episodes"] == {}
    assert row["pending_requirements"] == {}
    assert not row.get("overflow")  # unsupported scope alone is not a return cause


@pytest.mark.parametrize("via", ["prepare", "receive", "wake-ack", "lease-read", "settle"])
def test_unchanged_confirmed_fast_path_still_revokes_changed_holder(field, monkeypatch, via):
    state = returns.ReturnReconciliation(field, WORKER)
    signal, decision = requested(field, state)
    state.confirm(PROJECT, signal["episode_id"], decision, coordinator())
    monkeypatch.setattr(field, "pending_mail_headers", forbid_census)
    monkeypatch.setattr(field, "pending_return_census", forbid_census)
    assert (state.prepare() if via == "prepare" else state.model_entry(via)) == []
    field.sync_project_coordinator("project-one", coordinator(2))
    [changed] = state.prepare() if via == "prepare" else state.model_entry(via)
    assert changed["episode_id"] == signal["episode_id"]
    episode = read_json(state.path)["episodes"][PROJECT]
    assert episode["state"] == "pending"
    assert episode["request"] is None
    assert episode["reconciliation_generation"] > signal["reconciliation_generation"]


def capture(field, **changes):
    limits = dict(max_entries=256, max_bytes=1024 * 1024, max_record_bytes=128 * 1024,
                  max_headers=128, max_projects=16)
    return field.pending_return_census(WORKER, **{**limits, **changes})


def test_real_store_header_budget_does_not_deserialize_the_full_inbox(field, monkeypatch):
    for number in range(12):
        enqueue(field, f"task-{number}")
    monkeypatch.setattr(field, "pending_mail_headers", forbid_census)
    snapshot = capture(field, max_headers=3)
    assert snapshot["overflow"] == "census_headers"
    assert len(snapshot["headers"]) == 3
    assert snapshot["records_read"] == 3  # absent backlog file costs no record read
    assert snapshot["entries_seen"] == 4  # one entry probes the bound; no fourth message read
    assert snapshot["stable"] is True
    assert field.pending_return_census_current(WORKER, snapshot)


def test_real_store_byte_budget_stops_before_deserializing_an_oversized_record(field):
    enqueue(field, "large", body="x" * 12000)
    snapshot = capture(field, max_bytes=1024, max_record_bytes=2048)
    assert snapshot["overflow"] == "census_bytes"
    assert snapshot["headers"] == []
    assert snapshot["bytes_read"] == 1025  # one overflow sentinel byte
    assert snapshot["records_read"] == 1


def test_real_store_entry_budget_does_not_sort_or_consume_the_directory(field):
    root = field._mail_root("project-one", WORKER) / "inbox"
    root.mkdir(parents=True, exist_ok=True)
    for number in range(12):
        atomic_write_json(root / f"entry-{number}.other", {"ignored": True})
    snapshot = capture(field, max_entries=3)
    assert snapshot["overflow"] == "census_entries"
    assert snapshot["entries_seen"] == 4
    assert snapshot["records_read"] == 0
    assert snapshot["headers"] == []


def test_full_census_keeps_its_existing_complete_contract_and_projection(field):
    for number in range(4):
        enqueue(field, f"task-{number}")
    before = field.pending_mail_headers(WORKER)
    assert len(before) == 4
    assert capture(field, max_headers=2)["overflow"] == "census_headers"
    assert field.pending_mail_headers(WORKER) == before
    bounded = capture(field)
    assert {h["message_ref"]: h for h in bounded["headers"]} == {h["message_ref"]: h for h in before}


def test_real_input_stamps_reject_admission_and_backlog_changes(field):
    enqueue(field, "one")
    first = capture(field)
    enqueue(field, "two")
    assert not field.pending_return_census_current(WORKER, first)
    second = capture(field)
    field.mark_backlog(WORKER, reason="Fixture marks only; no settlement")
    assert not field.pending_return_census_current(WORKER, second)
    assert all(h["backlog"] for h in capture(field)["headers"])


def test_legacy_pending_request_cannot_take_the_inactive_shortcut(field, monkeypatch):
    state = returns.ReturnReconciliation(field, WORKER)
    signal, _ = requested(field, state)
    row = read_json(state.path)
    row.pop("state_version")
    row.pop("pending_requirements")
    row.pop("seen_requirements")
    episode = row["episodes"][PROJECT]
    episode.pop("requirements")
    episode.pop("reconciliation_generation")
    row["consumed_cause"] = row["cause"]["token"]
    original = deepcopy(episode["inventory"])
    atomic_write_json(state.path, row)
    reader = field.pending_return_census
    calls = []

    def counted(*args, **kwargs):
        calls.append(True)
        return reader(*args, **kwargs)

    monkeypatch.setattr(field, "pending_return_census", counted)
    [pending] = state.prepare()
    migrated = read_json(state.path)["episodes"][PROJECT]
    assert len(calls) == 1
    assert pending["episode_id"] == signal["episode_id"]
    assert migrated["inventory"] == original
    assert migrated["requirements"]
    assert migrated["request"] is None


def test_slow_capture_leaves_owner_lock_available_and_revalidates_b(field, monkeypatch):
    state = returns.ReturnReconciliation(field, WORKER)
    signal, decision = requested(field, state)
    reader = field.pending_return_census
    entered, release = Event(), Event()
    calls = []

    def slow_once(*args, **kwargs):
        snapshot = reader(*args, **kwargs)
        calls.append(len(calls))
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
        return snapshot

    monkeypatch.setattr(field, "pending_return_census", slow_once)
    other = returns.ReturnReconciliation(field, WORKER)
    with ThreadPoolExecutor(max_workers=2) as pool:
        waiting = pool.submit(state.prepare)
        try:
            assert entered.wait(5)
            admitted = pool.submit(other.record_cause, "quota_hold", {"hold_id": "B", "source": "fixture"})
            admitted.result(timeout=2)  # cannot finish if capture owns the return lock
            during = read_json(state.path)["episodes"][PROJECT]
            assert len(during["requirements"]) == 2
            assert during["request"] is None
        finally:
            release.set()
        [current] = waiting.result(timeout=5)
    assert len(calls) >= 3  # B's capture, then the old writer's state-revalidated retry
    assert current["episode_id"] == signal["episode_id"]
    assert len(current["requirements"]) == 2
    with pytest.raises(DomainError):
        state.confirm(PROJECT, signal["episode_id"], decision, coordinator())


def test_changed_mailbox_snapshot_retries_before_approving_a_census(field, monkeypatch):
    state = returns.ReturnReconciliation(field, WORKER)
    signal, _ = requested(field, state)
    reader = field.pending_return_census
    calls = []

    def raced_once(*args, **kwargs):
        snapshot = reader(*args, **kwargs)
        calls.append(snapshot)
        if len(calls) == 1:
            enqueue(field, "newly-admitted")
        return snapshot

    monkeypatch.setattr(field, "pending_return_census", raced_once)
    context = state.request_context(PROJECT, signal["correlation_id"], "coordinator-one", coordinator())
    assert len(calls) == 2
    assert context["inventory_hash"] != signal["inventory_hash"]
    assert state.signals()[0]["inventory_count"] == 2


def test_repeated_input_race_is_durably_fenced_without_advancing_baseline(field, monkeypatch):
    state = returns.ReturnReconciliation(field, WORKER)
    signal, decision = requested(field, state)
    before = read_json(state.path)
    reader = field.pending_return_census
    calls = []

    def always_raced(*args, **kwargs):
        snapshot = reader(*args, **kwargs)
        calls.append(snapshot)
        snapshot["stable"] = False
        return snapshot

    monkeypatch.setattr(field, "pending_return_census", always_raced)
    [pending] = state.model_entry("receive", now="2026-11-08T12:00:00Z")
    assert len(calls) == policy.MAX_CENSUS_ATTEMPTS
    assert pending["state_overflow"]["reason"] == "census_retry_exhausted"
    after = read_json(state.path)
    assert after["activity_baseline"] == before["activity_baseline"]
    assert after["episodes"][PROJECT]["inventory"] == before["episodes"][PROJECT]["inventory"]
    assert after["episodes"][PROJECT]["request"] is None
    monkeypatch.setattr(field, "pending_return_census", forbid_census)
    assert state.prepare()[0]["state_overflow"]  # recovery/control probes do not rescan
    with pytest.raises(DomainError) as error:
        state.confirm(PROJECT, signal["episode_id"], decision, coordinator())
    assert error.value.code == "field_return_state_overflow"


def test_bounded_oversized_census_preserves_existing_prefix_and_pending_gate(field, monkeypatch):
    state = returns.ReturnReconciliation(field, WORKER)
    signal, decision = requested(field, state)
    prefix = deepcopy(read_json(state.path)["episodes"][PROJECT]["inventory"])
    for number in range(8):
        enqueue(field, f"overflow-{number}")
    monkeypatch.setattr(policy, "MAX_INVENTORY", 3)
    [pending] = state.prepare()
    row = read_json(state.path)
    assert pending["state_overflow"]["reason"] == "census_headers"
    assert row["episodes"][PROJECT]["inventory"][:len(prefix)] == prefix
    assert len(row["episodes"][PROJECT]["inventory"]) <= 3
    assert row["episodes"][PROJECT]["request"] is None
    with pytest.raises(DomainError):
        state.confirm(PROJECT, signal["episode_id"], decision, coordinator())


def test_overflow_after_confirmation_remains_visible_without_actionable_snapshot(field, monkeypatch):
    state = returns.ReturnReconciliation(field, WORKER)
    signal, decision = requested(field, state)
    state.confirm(PROJECT, signal["episode_id"], decision, coordinator())
    monkeypatch.setattr(policy, "MAX_CENSUS_BYTES", 1)
    state.record_cause("quota_hold", {"hold_id": "B", "source": "fixture"})
    [pending] = state.signals()
    assert pending["state_overflow"]["reason"] == "census_bytes"
    assert pending["correlation_id"] and pending["episode_id"]
    monkeypatch.setattr(field, "pending_return_census", forbid_census)
    assert state.model_entry("wake-ack")[0]["state_overflow"]
    with pytest.raises(DomainError) as error:
        state.confirm(PROJECT, signal["episode_id"], decision, coordinator())
    assert error.value.code == "field_return_state_overflow"


@pytest.mark.parametrize("initial", ["pending", "confirmed", "legacy_pending"])
@pytest.mark.parametrize("via", ["prepare", "receive", "wake-ack", "lease-read", "settle",
                                "request", "record_request", "confirm", "activity", "cause"])
def test_unsupported_attendance_never_fans_out_and_keeps_all_evidence(field, monkeypatch, initial, via):
    state = returns.ReturnReconciliation(field, WORKER)
    state.model_entry("receive")
    signal, decision = requested(field, state)
    context = deepcopy(read_json(state.path)["episodes"][PROJECT]["request"])
    if initial == "confirmed":
        state.confirm(PROJECT, signal["episode_id"], decision, coordinator())
    elif initial == "legacy_pending":
        legacy = read_json(state.path)
        legacy.pop("state_version")
        atomic_write_json(state.path, legacy)
    before = read_json(state.path)
    projects = sorted([PROJECT, *(f"work:project:project-extra-{n}" for n in range(16))])
    field.sync_worker_attendances(WORKER, projects)  # the real owner permits 17
    holder_reads = []

    def forbid_holder(project):
        holder_reads.append(project)
        pytest.fail("Unsupported attendance must be fenced before any coordinator-file read")

    monkeypatch.setattr(field, "read_project_coordinator", forbid_holder)
    monkeypatch.setattr(field, "pending_mail_headers", forbid_census)
    monkeypatch.setattr(field, "pending_return_census", forbid_census)

    def probe():
        if via == "prepare":
            state.prepare()
        elif via in {"receive", "wake-ack", "lease-read", "settle"}:
            state.model_entry(via, now="2026-11-08T12:00:00Z")
        elif via == "request":
            state.request_context(PROJECT, signal.get("correlation_id", ""), "coordinator-one", coordinator())
        elif via == "record_request":
            state.record_request(PROJECT, context, {"message_ref": "work:mail:new-census", "created_at": NOW})
        elif via == "confirm":
            state.confirm(PROJECT, signal.get("episode_id", ""), decision, coordinator())
        elif via == "activity":
            state.observe_activity({"session_id": field.read_worker(WORKER)["runtime_session_id"],
                                    "source_generation": "fixture", "cursor": 1,
                                    "turn_key": "fixture-one", "occurred_at": NOW})
        else:
            state.record_cause("session_suspension", {"occurrence_id": "B", "source": "fixture"})

    for _ in range(2):  # first detection AND an already-fenced control/model probe
        if via in {"request", "record_request", "confirm"}:
            with pytest.raises(DomainError) as error:
                probe()
            assert error.value.code == "field_return_state_overflow"
        else:
            probe()
        [notice] = state.signals()
        assert notice["recovery_scope"] == "worker"
        assert notice["attended_project_count"] == 17
        assert notice["supported_project_limit"] == 16
        assert notice["state_overflow"]
        assert notice["inventory_count"] == "unknown"  # never a complete/partial census claim
        after = read_json(state.path)
        assert after["activity_baseline"] == before["activity_baseline"]
        assert after["pending_requirements"] == before["pending_requirements"]
        assert set(after["episodes"]) == set(before["episodes"])
        for project, original in before["episodes"].items():
            episode = after["episodes"][project]
            assert episode["inventory"] == original["inventory"]
            assert episode["requirements"] == original["requirements"]
            if original["state"] == "pending":
                assert episode["request"] is None
        assert field.read_worker(WORKER)["attended_project_refs"] == projects
    assert holder_reads == []


def test_first_known_return_with_unsupported_scope_keeps_cause_and_visible_fence(field, monkeypatch):
    state = returns.ReturnReconciliation(field, WORKER)
    state.model_entry("receive")
    before = read_json(state.path)
    projects = [PROJECT, *(f"work:project:project-extra-{n}" for n in range(16))]
    field.sync_worker_attendances(WORKER, projects)
    monkeypatch.setattr(field, "read_project_coordinator", forbid_census)
    monkeypatch.setattr(field, "pending_return_census", forbid_census)
    state.record_cause("quota_hold", {"hold_id": "A", "source": "fixture"})
    after = read_json(state.path)
    assert after["activity_baseline"] == before["activity_baseline"]
    assert after["cause"]["evidence"]["hold_id"] == "A"
    assert after["overflow"]["blocked_requirement_id"] == after["cause"]["token"]
    assert after["episodes"] == {}  # no partial set of newly invented episodes
    [notice] = state.prepare()
    assert notice["recovery_scope"] == "worker"
    assert notice["attended_project_count"] == 17
    assert len(field.read_worker(WORKER)["attended_project_refs"]) == 17


@pytest.mark.parametrize("repeat", [False, True])
def test_capture_scope_p_q_p_cannot_confirm_a_request_missing_new_p_mail(field, monkeypatch, repeat):
    state = returns.ReturnReconciliation(field, WORKER)
    signal, decision = requested(field, state)
    before = read_json(state.path)
    other = "work:project:project-two"
    reader = field.pending_return_census
    snapshots = []

    def attendance_race(*args, **kwargs):
        if not snapshots:
            # Admission is BEFORE capture/final validation, while P is attended.
            enqueue(field, "new-P-task")
        if not snapshots or repeat:
            field.sync_worker_attendances(WORKER, [other])
            snapshot = reader(*args, **kwargs)
            assert snapshot["stable"] is True
            assert snapshot["context"]["projects"] == [other]
            omitted = str(field._mail_root("project-one", WORKER) / "inbox")
            assert omitted not in {path for path, _stamp in snapshot["stamps"]}
            field.sync_worker_attendances(WORKER, [PROJECT])
        else:
            snapshot = reader(*args, **kwargs)
        snapshots.append(snapshot)
        return snapshot

    monkeypatch.setattr(field, "pending_return_census", attendance_race)
    with pytest.raises(DomainError) as error:
        state.confirm(PROJECT, signal["episode_id"], decision, coordinator())
    after = read_json(state.path)
    assert len(snapshots) == policy.MAX_CENSUS_ATTEMPTS
    assert after["episodes"][PROJECT]["state"] == "pending"
    assert after["episodes"][PROJECT]["request"] is None
    assert after["activity_baseline"] == before["activity_baseline"]
    assert field.read_worker(WORKER)["attended_project_refs"] == [PROJECT]
    if repeat:
        assert error.value.code == "field_return_state_overflow"
        assert after["overflow"]["reason"] == "census_retry_exhausted"
        assert after["episodes"][PROJECT]["inventory"] == before["episodes"][PROJECT]["inventory"]
    else:
        assert error.value.code == "field_return_decision_mismatch"
        assert snapshots[1]["context"]["projects"] == [PROJECT]
        assert {h["subject"] for h in after["episodes"][PROJECT]["inventory"]} == {"old-task", "new-P-task"}


@pytest.mark.parametrize("key,value", [("worker_name", "other-worker"), ("session_id", "other-session"),
                                     ("projects", ["work:project:project-two"]), ("missing", None)])
def test_store_validation_checks_captured_identity_not_just_matching_stamps(field, key, value):
    snapshot = capture(field)
    assert field.pending_return_census_current(WORKER, snapshot)
    if key == "missing":
        snapshot.pop("context")
    else:
        snapshot["context"][key] = value
    assert not field.pending_return_census_current(WORKER, snapshot)

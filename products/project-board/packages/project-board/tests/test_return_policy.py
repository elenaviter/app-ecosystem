"""Restricted reducer/fence regression source, not native cadence qualification.

Synthetic metadata points exercise ordering and persistence only. They do not
attest any installed producer, ordinary coding/tool cadence or active coverage.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import Event

import pytest

from project_board.client import return_policy as policy
from project_board.client import return_reconciliation as returns
from project_board.client.io import atomic_write_json, read_json
from project_board.contract.errors import DomainError

PROJECT = "work:project:project-one"
OTHER = "work:project:project-two"
EPOCH = datetime(2026, 11, 8, tzinfo=timezone.utc)


def at(seconds):
    return (EPOCH + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def header(key="task-one", project=PROJECT, **changes):
    return {"message_ref": "work:mail:" + key, "project_ref": project,
            "kind": "request", "sender": "worker-two", "subject": key,
            "created_at": at(-86400), "correlation_id": "work", "expected_reaction": "",
            **changes}


class Field:
    def __init__(self, path):
        self.control = path
        self.worker = {"runtime_session_id": "native-one", "attended_project_refs": [PROJECT]}
        self.headers = []
        self.coordinators = {
            p: {"state": "held", "holder": {"worker_name": "coordinator-one", "attending": True},
                "revision": 1, "since": at(-3600)} for p in (PROJECT, OTHER)
        }
        self.seconds = 0

    def read_worker(self, _name):
        return deepcopy(self.worker)

    def pending_mail_headers(self, _name):
        return deepcopy(self.headers)

    def read_project_coordinator(self, project_id):
        return deepcopy(self.coordinators["work:project:" + project_id])


@pytest.fixture
def world(tmp_path, monkeypatch):
    field = Field(tmp_path)
    monkeypatch.setattr(returns, "utc_now", lambda: at(field.seconds))
    return field, returns.ReturnReconciliation(field, "worker-one")


def point(seconds, cursor=1, **changes):
    return {"session_id": "native-one", "source_generation": "synthetic-reader-one",
            "cursor": cursor, "turn_key": "synthetic-turn", "occurred_at": at(seconds), **changes}


def baseline(field, state):
    assert state.model_entry("receive", now=at(0), threshold_seconds=3600) == []


def hold(field, state, key="A"):
    state.record_cause("quota_hold", {"hold_id": key, "source": "synthetic-cause"})


def request(field, state, key="A", project=PROJECT):
    signal = next(s for s in state.signals() if s["project_ref"] == project)
    coordinator = field.coordinators[project]
    context = state.request_context(project, signal["correlation_id"], "coordinator-one", coordinator)
    message = {"message_ref": "work:mail:census-" + key, "created_at": at(field.seconds)}
    receipt = state.record_request(project, context, message)
    decision = {"message_ref": "work:mail:decision-" + key, "project_ref": project,
                "sender": "coordinator-one", "kind": "reply", "reply_to": message["message_ref"],
                "correlation_id": signal["correlation_id"], "created_at": at(field.seconds)}
    return signal, context, receipt, decision


@pytest.mark.parametrize("via", ["receive", "wake-ack", "lease-read", "settle"])
@pytest.mark.parametrize("seconds", [3599, 3600, 3601])
def test_initial_boundary_states_are_independent(world, via, seconds):
    field, state = world
    baseline(field, state)
    field.headers = [header()]
    signals = state.model_entry(via, now=at(seconds), threshold_seconds=3600)
    assert bool(signals) is (seconds >= 3600)
    if signals:
        [first] = signals
        [retry] = state.model_entry(via, now=at(seconds + 1), threshold_seconds=3600)
        assert retry["episode_id"] == first["episode_id"]
        assert retry["last_model_handled_at"] == at(0)
        assert retry["cause"]["observed_gap_seconds"] == seconds
        assert retry["requirements_hash"] == first["requirements_hash"]


def test_lone_late_point_latches_before_future_baseline_and_information(world):
    field, state = world
    baseline(field, state)
    field.headers = [header()]
    field.seconds = 4500
    [first] = state.observe_activity(point(4500), threshold_seconds=3600)
    assert first["cause"]["observed_gap_seconds"] == 4500
    assert first["last_model_handled_at"] == at(0)
    assert read_json(state.path)["activity_baseline"]["at"] == at(4500)
    field.headers = [header("information", kind="update"),
                     header("control", kind="reply", correlation_id=first["correlation_id"])]
    state.model_entry("wake-ack", now=at(4530), threshold_seconds=3600)
    field.headers = [header()]
    [later] = state.model_entry("receive", now=at(4560), threshold_seconds=3600)
    assert later["episode_id"] == first["episode_id"]
    assert later["requirements"] == first["requirements"]
    assert later["reconciliation_generation"] == first["reconciliation_generation"]


@pytest.mark.parametrize("headers", [[], [header("info", kind="update")],
    [header("control", kind="reply", correlation_id=returns.PREFIX + "current")],
    [header("ack", expected_reaction="acknowledge_only")],
    [header("review", expected_reaction="await_review")]])
def test_candidate_survives_without_an_actionable_cohort(world, headers):
    field, state = world
    baseline(field, state)
    field.headers = deepcopy(headers)
    assert state.model_entry("wake-ack", now=at(4500), threshold_seconds=3600) == []
    row = read_json(state.path)
    assert len(row["pending_requirements"]) == 1
    field.headers.append(header())
    [signal] = state.model_entry("receive", now=at(4560), threshold_seconds=3600)
    assert signal["cause"]["observed_gap_seconds"] == 4500
    assert signal["last_model_handled_at"] == at(0)


def test_empty_first_attendance_is_not_an_unknown_interval_requirement(world):
    field, state = world
    assert state.model_entry("receive", now=at(0), threshold_seconds=3600) == []
    assert read_json(state.path)["pending_requirements"] == {}
    field.headers = [header()]
    assert state.model_entry("receive", now=at(60), threshold_seconds=3600) == []


@pytest.mark.parametrize("prior", [None, "invalid-clock", at(100)])
def test_actionable_unknown_or_backward_interval_stays_unknown(world, prior):
    field, state = world
    baseline(field, state)
    row = read_json(state.path)
    row["activity_baseline"] = {"at": prior} if prior else {}
    atomic_write_json(state.path, row)
    field.headers = [header()]
    [signal] = state.model_entry("receive", now=at(60), threshold_seconds=3600)
    assert signal["cause"]["kind"] == "activity_interval_unknown"
    assert signal["cause"]["observed_gap_seconds"] is None
    assert signal["cause"]["activity_coverage"] == "unknown"
    assert "sleep" not in signal["cause"]


def test_no_production_threshold_or_native_active_coverage_claim(world):
    field, state = world
    assert returns.INACTIVITY_THRESHOLD_SECONDS is None
    assert policy.CANDIDATE_INACTIVITY_SECONDS == 3600
    assert policy.CANDIDATE_ACTIVE_FRESHNESS_SECONDS == 60
    baseline(field, state)
    field.headers = [header()]
    field.seconds = 9000
    assert state.observe_activity(point(9000)) == []
    assert state.model_entry("receive", now=at(18000)) == []
    assert read_json(state.path)["pending_requirements"] == {}


def test_dense_synthetic_point_chain_is_reducer_only_not_cadence_proof(world):
    field, state = world
    baseline(field, state)
    field.headers = [header()]
    for cursor, seconds in enumerate(range(30, 5401, 30), 1):
        field.seconds = seconds
        assert state.observe_activity(point(seconds, cursor), threshold_seconds=3600) == []
    row = read_json(state.path)
    assert row["model_entry"]["at"] == at(0)
    assert row["activity_baseline"]["at"] == at(5400)
    assert "active_coverage" not in row
    assert "native_producer_version" not in row
    # The named 60-second candidate is not an absence trigger.
    assert state.model_entry("receive", now=at(5461), threshold_seconds=3600) == []


@pytest.mark.parametrize("changes", [
    {"session_id": "other-native-session"}, {"cursor": -1}, {"cursor": True},
    {"cursor": "one"}, {"turn_key": ""}, {"source_generation": "x" * 3000},
    {"occurred_at": at(61)}, {"arguments": "not metadata"},
])
def test_invalid_or_foreign_activity_never_advances_state(world, changes):
    field, state = world
    baseline(field, state)
    field.seconds = 60
    before = state.path.read_bytes()
    with pytest.raises(DomainError):
        state.observe_activity(point(60, **changes), threshold_seconds=3600)
    assert state.path.read_bytes() == before


def test_metadata_order_duplicate_and_generation_boundary(world):
    field, state = world
    baseline(field, state)
    field.seconds = 120
    state.observe_activity(point(60, 2), threshold_seconds=3600)
    before = state.path.read_bytes()
    assert state.observe_activity(point(60, 2), threshold_seconds=3600) == []
    assert state.path.read_bytes() == before
    for stale in (point(60, 1), point(61, 2), point(59, 3),
                  point(59, 0, source_generation="synthetic-reader-two")):
        with pytest.raises(DomainError) as error:
            state.observe_activity(stale, threshold_seconds=3600)
        assert error.value.code == "field_return_activity_order"
        assert state.path.read_bytes() == before
    state.observe_activity(point(120, 0, source_generation="synthetic-reader-two"), threshold_seconds=3600)
    assert read_json(state.path)["activity_baseline"]["at"] == at(120)


def test_duplicate_point_does_not_hide_its_later_open_tail(world):
    field, state = world
    baseline(field, state)
    field.headers = [header()]
    field.seconds = 60
    assert state.observe_activity(point(60), threshold_seconds=3600) == []
    field.seconds = 3660
    [signal] = state.observe_activity(point(60), threshold_seconds=3600)
    assert signal["cause"]["observed_gap_seconds"] == 3600
    assert signal["last_model_handled_at"] == at(60)
    assert read_json(state.path)["activity_baseline"]["at"] == at(60)


@pytest.mark.parametrize("committed", [False, True])
def test_atomic_failure_retry_never_commits_only_the_future_baseline(world, monkeypatch, committed):
    field, state = world
    baseline(field, state)
    field.headers = [header()]
    field.seconds = 4500
    writer = returns.atomic_write_json

    def uncertain_write(path, value):
        if committed:
            writer(path, value)
        raise OSError("synthetic interruption at atomic replacement")

    monkeypatch.setattr(returns, "atomic_write_json", uncertain_write)
    with pytest.raises(OSError):
        state.observe_activity(point(4500), threshold_seconds=3600)
    after_failure = read_json(state.path)
    assert after_failure["activity_baseline"]["at"] == at(4500 if committed else 0)
    assert len(after_failure["pending_requirements"]) == int(committed)
    monkeypatch.setattr(returns, "atomic_write_json", writer)
    [signal] = state.observe_activity(point(4500), threshold_seconds=3600)
    final = read_json(state.path)
    assert len(final["pending_requirements"]) == 1
    assert signal["last_model_handled_at"] == at(0)
    assert signal["cause"]["observed_gap_seconds"] == 4500
    assert final["activity_baseline"]["at"] == at(4500)


def test_new_requirement_fences_context_even_without_inventory_change(world):
    field, state = world
    field.headers = [header()]
    hold(field, state)
    signal, old_context, _, old_decision = request(field, state)
    hold(field, state, "B")
    with pytest.raises(DomainError) as error:
        state.record_request(PROJECT, old_context, {"message_ref": "work:mail:late-census", "created_at": at(0)})
    assert error.value.code == "field_return_inventory_changed"
    with pytest.raises(DomainError):
        state.confirm(PROJECT, signal["episode_id"], old_decision, field.coordinators[PROJECT])
    [current] = state.signals()
    assert current["episode_id"] == signal["episode_id"]
    assert current["inventory_hash"] == signal["inventory_hash"]
    assert len(current["requirements"]) == 2
    assert current["requirements_hash"] != old_context["requirements_hash"]
    assert current["reconciliation_generation"] > old_context["reconciliation_generation"]
    assert read_json(state.path)["episodes"][PROJECT]["request"] is None
    _, _, fresh, decision = request(field, state, "B")
    hold(field, state, "A")
    hold(field, state, "B")
    assert read_json(state.path)["episodes"][PROJECT]["request"] == fresh
    assert state.confirm(PROJECT, signal["episode_id"], decision, field.coordinators[PROJECT])["replayed"] is False


def test_concurrent_admission_before_delayed_confirmation_cannot_restore_request(world):
    field, state = world
    field.headers = [header()]
    hold(field, state)
    signal, _, _, decision = request(field, state)
    admitted = Event()
    contender = returns.ReturnReconciliation(field, "worker-one")

    def admit_b():
        hold(field, contender, "B")
        admitted.set()

    def late_reply():
        assert admitted.wait(5)
        with pytest.raises(DomainError) as error:
            state.confirm(PROJECT, signal["episode_id"], decision, field.coordinators[PROJECT])
        return error.value.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        reply = pool.submit(late_reply)
        pool.submit(admit_b).result(timeout=5)
        assert reply.result(timeout=5) == "field_return_decision_mismatch"
    row = read_json(state.path)
    episode = row["episodes"][PROJECT]
    assert episode["state"] == "pending"
    assert episode["request"] is None
    assert len(episode["requirements"]) == 2
    assert episode["invalidated_requests"][-1]["message_ref"] == "work:mail:census-A"


def test_projects_workers_native_sessions_and_holder_epochs_are_isolated(world):
    field, state = world
    field.worker["attended_project_refs"].append(OTHER)
    field.headers = [header(), header("other", OTHER)]
    hold(field, state)
    signal, _, _, decision = request(field, state)
    other_signal, _, _, other_decision = request(field, state, "other", OTHER)
    state.confirm(PROJECT, signal["episode_id"], decision, field.coordinators[PROJECT])
    assert [s["project_ref"] for s in state.signals()] == [OTHER]
    field.coordinators[OTHER]["revision"] += 1  # same name, different authority epoch
    with pytest.raises(DomainError):
        state.confirm(OTHER, other_signal["episode_id"], other_decision, field.coordinators[OTHER])
    assert read_json(state.path)["episodes"][PROJECT]["state"] == "confirmed"
    separate = returns.ReturnReconciliation(field, "worker-two")
    assert separate.signals() == []
    assert separate.path != state.path
    field.worker["runtime_session_id"] = "native-two"
    field.seconds = 60
    with pytest.raises(DomainError):
        state.confirm(PROJECT, signal["episode_id"], decision, field.coordinators[PROJECT])
    row = read_json(state.path)
    assert row["session_id"] == "native-two"
    assert row["episodes"] == {}
    assert row["previous_session"] == "native-one"
    assert row["previous_session_evidence"]["episode_ids"]


def legacy_row(field, *, confirmed=False):
    cause = {"kind": "quota_hold", "token": "legacy-A", "at": at(0), "evidence": {"hold_id": "A"}}
    episode = {"episode_id": "return_legacy", "project_ref": PROJECT,
               "state": "confirmed" if confirmed else "pending", "began_at": at(0),
               "prior_model_entry": {"at": at(-7200), "via": "receive"},
               "cause": deepcopy(cause), "inventory": [header()], "inventory_hash": "legacy-inventory"}
    binding = returns.coordinator_binding(field.coordinators[PROJECT])
    episode["request"] = {"episode_id": episode["episode_id"], "inventory_hash": episode["inventory_hash"],
                          "coordinator": binding, "message_ref": "work:mail:legacy-census", "created_at": at(0)}
    if confirmed:
        episode.update(coordinator=binding, decision_message_ref="work:mail:legacy-decision", confirmed_at=at(0))
    return {"session_id": "native-one", "model_entry": {"at": at(0), "via": "receive"},
            "cause": cause, "consumed_cause": "legacy-A", "episodes": {PROJECT: episode}}


def test_legacy_pending_migration_retains_a_and_b_and_invalidates_old_request(world):
    field, state = world
    row = legacy_row(field)
    inventory = deepcopy(row["episodes"][PROJECT]["inventory"])
    row["cause"] = {"kind": "quota_hold", "token": "legacy-B", "at": at(0), "evidence": {"hold_id": "B"}}
    atomic_write_json(state.path, row)
    field.headers = []  # census remains enumerable even after original mail receipt
    [signal] = state.prepare()
    migrated = read_json(state.path)
    episode = migrated["episodes"][PROJECT]
    assert migrated["state_version"] == policy.STATE_VERSION
    assert episode["cause"]["token"] == "legacy-A"
    assert set(episode["requirements"]) == {"legacy-A", "legacy-B"}
    assert episode["inventory"] == inventory
    assert episode["request"] is None
    assert episode["invalidated_requests"][0]["message_ref"] == "work:mail:legacy-census"
    assert len(state.census_headers()) == 1
    assert state.prepare()[0]["requirements_hash"] == signal["requirements_hash"]
    assert read_json(state.path)["episodes"][PROJECT] == episode


def test_legacy_confirmed_receipt_replays_without_clearing_pending_other_project(world):
    field, state = world
    field.worker["attended_project_refs"].append(OTHER)
    row = legacy_row(field, confirmed=True)
    other = deepcopy(legacy_row(field)["episodes"][PROJECT])
    other.update(project_ref=OTHER, episode_id="return_legacy_other")
    other["request"]["episode_id"] = other["episode_id"]
    row["episodes"][OTHER] = other
    atomic_write_json(state.path, row)
    decision = {"message_ref": "work:mail:legacy-decision", "project_ref": PROJECT, "sender": "coordinator-one",
                "kind": "reply", "correlation_id": returns.PREFIX + "return_legacy",
                "reply_to": "work:mail:legacy-census", "created_at": at(0)}
    assert state.confirm(PROJECT, "return_legacy", decision, field.coordinators[PROJECT])["replayed"] is True
    migrated = read_json(state.path)
    assert [s["project_ref"] for s in state.signals()] == [OTHER]
    assert migrated["pending_requirements"]["legacy-A"]["consumed_by"] == [PROJECT]
    assert migrated["episodes"][OTHER]["request"] is None
    signal, _, _, other_decision = request(field, state, "new-other", OTHER)
    state.confirm(OTHER, signal["episode_id"], other_decision, field.coordinators[OTHER])
    assert state.signals() == []
    assert read_json(state.path)["pending_requirements"] == {}


def test_oversized_legacy_inventory_is_preserved_but_not_approved(world):
    field, state = world
    row = legacy_row(field)
    row["episodes"][PROJECT]["inventory"][0]["subject"] = "x" * 3000
    original = deepcopy(row["episodes"][PROJECT]["inventory"])
    atomic_write_json(state.path, row)
    [signal] = state.prepare()
    assert signal["state_overflow"]
    assert read_json(state.path)["episodes"][PROJECT]["inventory"] == original
    with pytest.raises(DomainError) as error:
        state.request_context(PROJECT, signal["correlation_id"], "coordinator-one", field.coordinators[PROJECT])
    assert error.value.code == "field_return_state_overflow"


def test_legacy_confirmed_a_never_consumes_later_b_even_with_identical_clock(world):
    field, state = world
    row = legacy_row(field, confirmed=True)
    row["cause"] = {"kind": "quota_hold", "token": "legacy-B", "at": at(0), "evidence": {"hold_id": "B"}}
    atomic_write_json(state.path, row)
    assert state.prepare() == []  # no actionable cohort yet
    pending = read_json(state.path)["pending_requirements"]["legacy-B"]
    assert pending["consumed_by"] == []
    field.headers = [header()]
    [signal] = state.prepare()
    assert signal["episode_id"] != "return_legacy"
    assert set(signal["requirements"]) == {"legacy-B"}
    assert read_json(state.path)["episodes"][PROJECT].get("request") is None


def test_pending_requirement_overflow_preserves_prefix_and_blocks_checkpoint(world, monkeypatch):
    field, state = world
    monkeypatch.setattr(policy, "MAX_REQUIREMENTS", 2)
    field.headers = [header()]
    hold(field, state, "A")
    signal, _, _, decision = request(field, state)
    hold(field, state, "B")
    prefix = read_json(state.path)["pending_requirements"]
    hold(field, state, "C")
    before = read_json(state.path)
    assert before["pending_requirements"] == prefix
    assert before["overflow"]["blocked_requirement_id"]
    assert before["episodes"][PROJECT]["request"] is None
    [inspection] = state.model_entry("wake-ack", now=at(9999), threshold_seconds=3600)
    after = read_json(state.path)
    assert inspection["state_overflow"]
    assert after["activity_baseline"] == before["activity_baseline"]
    assert after.get("model_entry") == before.get("model_entry")
    for action in (
        lambda: state.request_context(PROJECT, signal["correlation_id"], "coordinator-one", field.coordinators[PROJECT]),
        lambda: state.confirm(PROJECT, signal["episode_id"], decision, field.coordinators[PROJECT]),
    ):
        with pytest.raises(DomainError) as error:
            action()
        assert error.value.code == "field_return_state_overflow"


def test_open_tail_overflow_does_not_commit_tentative_future_baseline(world, monkeypatch):
    field, state = world
    baseline(field, state)
    monkeypatch.setattr(policy, "MAX_REQUIREMENTS", 1)
    field.headers = [header()]
    field.seconds = 9000
    [signal] = state.observe_activity(point(4500), threshold_seconds=3600)
    row = read_json(state.path)
    assert signal["state_overflow"]
    assert signal["cause"]["observed_gap_seconds"] == 4500
    assert row["activity_baseline"]["at"] == at(0)
    assert "activity_cursor" not in row
    assert len(row["pending_requirements"]) == 1


@pytest.mark.parametrize("limit", ["inventory", "header_bytes", "projects"])
def test_other_bounds_leave_enumerable_fail_closed_evidence(world, monkeypatch, limit):
    field, state = world
    field.headers = [header()]
    if limit == "inventory":
        monkeypatch.setattr(policy, "MAX_INVENTORY", 1)
        field.headers.append(header("task-two"))
    elif limit == "header_bytes":
        field.headers[0]["subject"] = "x" * 3000
    else:
        monkeypatch.setattr(policy, "MAX_EPISODES", 1)
        field.worker["attended_project_refs"].append(OTHER)
        field.headers.append(header("other", OTHER))
    hold(field, state)
    row = read_json(state.path)
    assert row["overflow"]
    assert len(row["episodes"]) <= policy.MAX_EPISODES
    assert all(len(e["inventory"]) <= policy.MAX_INVENTORY for e in row["episodes"].values())
    assert state.prepare()
    assert all(s["episode_id"] and s["correlation_id"] and s["state_overflow"] for s in state.signals())
    assert state.model_entry("receive", now=at(60))


def test_repeated_known_causes_do_not_advance_generation_and_suspension_is_distinct(world):
    field, state = world
    field.headers = [header()]
    hold(field, state)
    signal, _, receipt, _ = request(field, state)
    hold(field, state)
    assert read_json(state.path)["episodes"][PROJECT]["request"] == receipt
    assert state.signals()[0]["reconciliation_generation"] == signal["reconciliation_generation"]
    state.record_cause("session_suspension", {"occurrence_id": "suspension-one"})
    [current] = state.signals()
    assert len(current["requirements"]) == 2
    assert current["reconciliation_generation"] > signal["reconciliation_generation"]
    assert read_json(state.path)["episodes"][PROJECT]["request"] is None

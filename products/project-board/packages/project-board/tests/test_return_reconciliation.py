"""A wake returns to current authority; delivery is not execution approval."""
from copy import deepcopy

import pytest

from project_board.client.io import read_json
from project_board.client.return_reconciliation import ReturnReconciliation, PREFIX
from project_board.contract.errors import DomainError

PROJECT = "work:project:project-one"
OTHER = "work:project:project-two"
NOW = "2026-11-08T10:00:00Z"


class Field:
    def __init__(self, path):
        self.control = path
        self.worker = {"runtime_session_id": "native-one", "attended_project_refs": [PROJECT]}
        self.headers = []
        self.coordinator = {"state": "held", "holder": {"worker_name": "coordinator-one", "attending": True},
                            "revision": 1, "since": "2026-11-08T08:00:00Z"}

    def read_worker(self, name):
        return deepcopy(self.worker)

    def pending_mail_headers(self, name):
        return deepcopy(self.headers)

    def read_project_coordinator(self, project):
        return deepcopy(self.coordinator)


def header(ref="work:mail:old-one", project=PROJECT, kind="request"):
    return {"message_ref": ref, "project_ref": project, "kind": kind,
            "created_at": "2026-11-01T10:00:00Z", "sender": "worker-two", "correlation_id": "old",
            "subject": "Inspect the current assignment", "expected_reaction": ""}


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr("project_board.client.return_reconciliation.utc_now", lambda: NOW)
    field = Field(tmp_path)
    return field, ReturnReconciliation(field, "worker-one")


def start(field, state):
    field.headers.append(header())
    state.record_cause("quota_hold", {"until": NOW, "source": "native"})
    signals = state.model_entry("receive")
    return next(s for s in signals if s["project_ref"] == PROJECT)


def requested(field, state):
    signal = start(field, state)
    context = state.request_context(PROJECT, signal["correlation_id"], "coordinator-one", field.coordinator)
    request = {"message_ref": "work:mail:request-one", "created_at": NOW}
    state.record_request(PROJECT, context, request)
    decision = {"message_ref": "work:mail:decision-one", "project_ref": PROJECT, "sender": "coordinator-one",
                "kind": "reply", "correlation_id": signal["correlation_id"], "reply_to": request["message_ref"],
                "created_at": "2026-11-08T10:00:01Z"}
    return signal, decision


def test_first_return_preserves_prior_entry_across_receive_retries(world):
    field, state = world
    state.model_entry("receive", now="2026-11-08T02:00:00Z")
    field.headers.append(header())
    [first] = state.model_entry("receive", now=NOW, threshold_seconds=21600)
    [again] = state.model_entry("receive", now=NOW, threshold_seconds=21600)
    assert again["episode_id"] == first["episode_id"]
    assert again["last_model_handled_at"] == "2026-11-08T02:00:00Z"


def test_no_numeric_default_and_no_false_sleep_claim(world):
    field, state = world
    state.model_entry("receive", now="2025-01-01T00:00:00Z")
    field.headers.append(header())
    assert state.model_entry("receive", now=NOW) == []
    assert state.model_entry("receive", now="2026-11-08T09:00:00Z", threshold_seconds=1) == []


def test_known_quota_cause_survives_empty_and_informational_wakes(world):
    field, state = world
    state.record_cause("quota_hold", {"until": NOW})
    assert state.prepare() == []
    field.headers.append(header(kind="update"))
    assert state.prepare() == []
    assert "model_entry" not in read_json(state.path)
    field.headers.append(header())
    [signal] = state.model_entry("wake-ack")
    assert signal["cause"]["kind"] == "quota_hold"
    assert signal["last_model_handled_at"] == "unknown"
    assert state.signals()[0]["episode_id"] == signal["episode_id"]


def test_ordinary_receive_settlement_and_no_reply_never_clear(world):
    field, state = world
    signal = start(field, state)
    for via in ("receive", "lease-read", "wake-ack", "settle"):
        assert state.model_entry(via)[0]["episode_id"] == signal["episode_id"]
    with pytest.raises(DomainError):
        state.confirm(PROJECT, signal["episode_id"], {}, field.coordinator)
    assert state.signals()


@pytest.mark.parametrize("change", [
    {"sender": "old-coordinator"}, {"project_ref": OTHER}, {"kind": "inform"},
    {"correlation_id": PREFIX + "old-episode"}, {"reply_to": "work:mail:old-request"},
    {"created_at": "2026-11-08T09:59:59Z"},
])
def test_stale_or_unrelated_decision_cannot_clear(world, change):
    field, state = world
    signal, decision = requested(field, state)
    with pytest.raises(DomainError) as error:
        state.confirm(PROJECT, signal["episode_id"], {**decision, **change}, field.coordinator)
    assert error.value.code == "field_return_decision_mismatch"
    assert state.signals()


def test_explicit_checkpoint_replays_and_leaves_another_project_pending(world):
    field, state = world
    field.worker["attended_project_refs"].append(OTHER)
    field.headers.append(header("work:mail:other-one", OTHER))
    signal, decision = requested(field, state)
    first = state.confirm(PROJECT, signal["episode_id"], decision, field.coordinator)
    again = state.confirm(PROJECT, signal["episode_id"], decision, field.coordinator)
    assert first["confirmed_at"] == again["confirmed_at"]
    assert again["replayed"] is True
    assert [s["project_ref"] for s in state.signals()] == [OTHER]


def test_coordinator_change_requires_a_new_request_even_when_name_is_unchanged(world):
    field, state = world
    signal, decision = requested(field, state)
    field.coordinator["revision"] = 3
    with pytest.raises(DomainError):
        state.confirm(PROJECT, signal["episode_id"], decision, field.coordinator)
    assert state.signals()


def test_new_action_changes_the_census_and_invalidates_an_earlier_reply(world):
    field, state = world
    signal, decision = requested(field, state)
    field.headers.append(header("work:mail:new-one"))
    with pytest.raises(DomainError):
        state.confirm(PROJECT, signal["episode_id"], decision, field.coordinator)
    assert state.signals()[0]["inventory_count"] == 2


def test_native_session_change_never_inherits_a_checkpoint(world):
    field, state = world
    signal, decision = requested(field, state)
    state.confirm(PROJECT, signal["episode_id"], decision, field.coordinator)
    field.worker["runtime_session_id"] = "native-two"
    with pytest.raises(DomainError):
        state.confirm(PROJECT, signal["episode_id"], decision, field.coordinator)


def test_notifier_or_heartbeat_cannot_advance_a_model_entry(world):
    field, state = world
    state.model_entry("receive", now="2026-11-08T09:00:00Z")
    state.prepare()
    assert read_json(state.path)["model_entry"]["at"] == "2026-11-08T09:00:00Z"
    with pytest.raises(ValueError):
        state.model_entry("heartbeat")

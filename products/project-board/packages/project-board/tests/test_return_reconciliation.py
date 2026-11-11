"""A wake returns to current authority; delivery is not execution approval."""
from copy import deepcopy

import pytest

from project_board.client.io import content_hash, read_json
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

    def pending_return_census(self, name, **limits):
        headers = self.pending_mail_headers(name)
        return {"headers": headers[:limits["max_headers"]], "stable": True,
                "context": {"worker_name": name, "session_id": self.worker["runtime_session_id"],
                            "projects": list(self.worker["attended_project_refs"])},
                "overflow": "census_headers" if len(headers) > limits["max_headers"] else "",
                "token": content_hash(headers)}

    def pending_return_census_current(self, _name, snapshot):
        return snapshot["token"] == content_hash(self.headers)

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
    [unknown] = state.model_entry("receive", now="2026-11-08T09:00:00Z", threshold_seconds=1)
    assert unknown["cause"]["kind"] == "activity_interval_unknown"
    assert unknown["cause"]["observed_gap_seconds"] is None
    assert "sleep" not in unknown["cause"]


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


@pytest.mark.parametrize("resets_at", ["", "2999-01-01T00:00:00Z"])
@pytest.mark.parametrize("confirm_first", [False, True])
def test_distinct_native_quota_holds_open_new_return_episodes(tmp_path, monkeypatch, resets_at, confirm_first):
    """Exercise actual relay hold creation, repeated reads, and capacity recovery."""
    import asyncio

    from project_board.client import relay, store as store_module
    from project_board.client.store import SharedFieldStore
    from relay_helpers import make_host, make_supervisor

    # Even identical clock values must not merge two different occurrences.
    monkeypatch.setattr(store_module, "utc_now", lambda: NOW)
    monkeypatch.setattr(relay, "utc_now", lambda: NOW)
    monkeypatch.setattr("project_board.client.return_reconciliation.utc_now", lambda: NOW)
    host, identity, channel = make_host(tmp_path)
    field = SharedFieldStore(host.field_root)
    field.initialize(field_id="two-quota-holds")
    field.create_project(project_id="project-one", title="Return", goal="Current work", owner="operator")
    field.register_worker(
        worker_name=identity.worker_name, worker_identity=identity.worker_identity,
        runtime_kind=identity.runtime_kind, runtime_session_id=identity.runtime_session_id,
        capabilities=[], authority_label="connection-hub:fixture-card", control_plane_state="published",
        attended_project_refs=[PROJECT],
    )
    field.listen_worker(identity.worker_name)
    coordinator = {"state": "held", "holder": {"worker_name": "coordinator-one", "attending": True},
                   "revision": 1, "since": "2026-11-08T08:00:00Z"}
    field.sync_project_coordinator("project-one", coordinator)
    state = ReturnReconciliation(field, identity.worker_name)
    supervisor = make_supervisor(host)
    quota = {"kind": "rate_limited", "source": "codex-app-server", "resets_at": resets_at}
    pushes = []

    # Native provider reads and the external queue are boundaries. The relay,
    # listener, mailbox, hold store and reconciliation state stay real.
    monkeypatch.setattr(relay, "session_with_limit_state", lambda listener, **_: dict(listener))
    async def read_quota(*_args, **_kwargs):
        return dict(quota), ""
    async def reconcile(*_args, **_kwargs):
        return None
    async def notify(*_args, **kwargs):
        pushes.append(kwargs)
        return {"delivered": True}
    monkeypatch.setattr(supervisor, "_refresh_codex_quota", read_quota)
    monkeypatch.setattr(supervisor, "_reconcile_session_queue", reconcile)
    monkeypatch.setattr(supervisor, "_notify_session", notify)

    def enqueue(key):
        return field.send_mail(
            "project-one", sender="control-plane", recipient=identity.worker_name,
            kind="request", subject=key, body="Ask current authority before resuming.", idempotency_key=key,
        )
    def observe():
        return asyncio.run(supervisor._notify_available_input(host, channel))

    enqueue("old-task-A")
    assert observe()["wake_deferred"] is True
    hold_a = field.wake_hold(identity.worker_name)
    [first] = state.model_entry("receive")
    assert first["cause"]["kind"] == "quota_hold"
    assert observe()["wake_deferred"] is True
    assert state.prepare()[0]["episode_id"] == first["episode_id"]
    assert field.wake_hold(identity.worker_name).get("hold_id") == hold_a.get("hold_id")

    context = state.request_context(PROJECT, first["correlation_id"], "coordinator-one", coordinator)
    request = {"message_ref": "work:mail:census-A", "created_at": NOW}
    state.record_request(PROJECT, context, request)
    decision_a = {
        "message_ref": "work:mail:decision-A", "project_ref": PROJECT, "sender": "coordinator-one",
        "kind": "reply", "correlation_id": first["correlation_id"], "reply_to": request["message_ref"],
        "created_at": NOW,
    }
    if confirm_first:
        state.confirm(PROJECT, first["episode_id"], decision_a, coordinator)
    # Repeated exhaustion after confirmation is still the same hold. Neither
    # a repeated observation nor a heartbeat constitutes capacity recovery.
    assert observe()["wake_deferred"] is True
    if confirm_first:
        assert state.prepare() == []
        [old] = field.pull_mail("project-one", worker_name=identity.worker_name, lease_owner=identity.runtime_session_id)
        field.settle_mail(
            "project-one", worker_name=identity.worker_name, message_ref=old["message_ref"],
            lease_id=old["lease"]["lease_id"], lease_owner=identity.runtime_session_id,
            outcome="obsolete", summary="Current coordinator recorded an empty resume list.",
        )
        enqueue("fresh-task-B")
    else:
        assert state.prepare()[0]["reconciliation_generation"] == first["reconciliation_generation"]
        assert read_json(state.path)["episodes"][PROJECT]["request"]["message_ref"] == request["message_ref"]
    quota.update(kind="ok", resets_at="")
    assert observe()["delivered"] is True
    assert len(pushes) == 1
    assert field.wake_hold(identity.worker_name) == {}
    if confirm_first:
        assert state.prepare() == []
    else:
        # A/R_A remains pending across actual relay-observed recovery. Do not
        # change the inventory: B alone must fence a delayed reply to R_A.
        assert state.prepare()[0]["inventory_hash"] == first["inventory_hash"]

    quota.update(kind="rate_limited", resets_at=resets_at)
    assert observe()["wake_deferred"] is True
    # d65992a2 has consumed the same kind/source token for A and yields no
    # pending episode here. This assertion precedes access to the new ID.
    [second] = state.prepare()
    if confirm_first:
        assert second["episode_id"] != first["episode_id"]
    else:
        assert second["episode_id"] == first["episode_id"]
        assert second["cause"] == first["cause"]
        assert second["inventory_hash"] == first["inventory_hash"]
        assert second["reconciliation_generation"] > first["reconciliation_generation"]
        assert len(second["requirements"]) == 2
        with pytest.raises(DomainError) as error:
            state.confirm(PROJECT, first["episode_id"], decision_a, coordinator)
        assert error.value.code == "field_return_decision_mismatch"
        assert read_json(state.path)["episodes"][PROJECT]["request"] is None
    assert second["inventory_count"] == 1
    hold_b = field.wake_hold(identity.worker_name)
    assert hold_b["hold_id"] != hold_a["hold_id"]
    assert hold_b["since"] == hold_a["since"] == NOW
    assert observe()["wake_deferred"] is True
    assert state.prepare()[0]["episode_id"] == second["episode_id"]
    assert field.wake_hold(identity.worker_name)["hold_id"] == hold_b["hold_id"]
    if not confirm_first:
        context_b = state.request_context(PROJECT, second["correlation_id"], "coordinator-one", coordinator)
        request_b = {"message_ref": "work:mail:census-B", "created_at": NOW}
        state.record_request(PROJECT, context_b, request_b)
        # Repeated B is still B; it must not invalidate its fresh request.
        assert observe()["wake_deferred"] is True
        assert read_json(state.path)["episodes"][PROJECT]["request"]["message_ref"] == request_b["message_ref"]
        state.confirm(PROJECT, second["episode_id"], {
            **decision_a, "message_ref": "work:mail:decision-B", "reply_to": request_b["message_ref"],
        }, coordinator)
        assert state.prepare() == []

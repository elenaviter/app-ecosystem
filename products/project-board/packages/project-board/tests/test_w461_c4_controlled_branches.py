"""W461 C4: the controlled reconnect branches the live checks did not drive.

Ops' 02:26 UTC installed-release run on 6 October passed 52 of 52 existing
probes, and Mint's 02:29 UTC verdict found that none drove three branches:
an accepted write whose answer is lost, a refused credential, and a bounded
reconnect (Ops' branch list, 04:33 UTC). Each case here uses isolated stores,
an injected clock and synthetic identities only, and fails when its property
is broken. Each records its own timings as junit properties.

Two cases are expected to fail on the current source, because the property
they pin does not hold there (strict xfail, so a fix turns them into a pass
that must be acknowledged):
- a revoked Card is not a terminal channel refusal;
- an accepted write whose answer is lost can only be settled by sending the
  exact request again, since there is no remote receipt lookup.
"""

from __future__ import annotations

import time

import pytest

from project_board.client import relay_pacing
from project_board.client.authorization import authorization_observation
from project_board.client.coordinate_queue import CoordinateQueue
from project_board.client.coordinate_recovery import CoordinateRecovery, coordinate_request_hash
from project_board.contract.errors import DomainError

from relay_helpers import make_host

PROJECT = "work:project:w461-c4"
WORK_REF = "work:plan:node:20261006T050000Z:w1:synthetic-c4"


class Clock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _refusal(code: str, status: int = 401) -> DomainError:
    return DomainError(code, f"Synthetic refusal {code}.", status=status)


# (c) Bounded reconnect ------------------------------------------------------


def test_c_repeated_drops_stay_on_a_capped_doubling_and_keep_their_evidence(record_property):
    clock = Clock()
    pacing = relay_pacing.RelayPacing(None, clock=clock, rng=lambda: 1.0)
    started = time.monotonic()
    delays = []
    for _ in range(12):
        delays.append(pacing.record_failure("w", "work_relay_transport_unavailable"))
        assert pacing.channel_due("w") is False, "no attempt before its backoff has passed"
        clock.now += delays[-1]
        assert pacing.channel_due("w") is True
    record_property("c_recovery_schedule_seconds", ",".join(f"{delay:g}" for delay in delays))
    record_property("c_evaluation_ms", round((time.monotonic() - started) * 1000, 3))

    assert delays[0] == relay_pacing.CHANNEL_BACKOFF_BASE_SECONDS
    assert all(later >= earlier for earlier, later in zip(delays, delays[1:])), "spacing never shrinks"
    assert max(delays) == relay_pacing.CHANNEL_BACKOFF_CAP_SECONDS, "spacing is capped"
    record = pacing.snapshot()["channels"]["w"]
    assert record["reason"] == "work_relay_transport_unavailable"
    assert record["attempts"] == 12, "every attempt stays counted as evidence"


def test_c_handshake_timeouts_retry_fast_only_up_to_their_limit(record_property):
    clock = Clock()
    pacing = relay_pacing.RelayPacing(None, clock=clock, rng=lambda: 1.0)
    delays = []
    for _ in range(relay_pacing.HANDSHAKE_RETRY_LIMIT + 2):
        delays.append(pacing.record_failure("w", "data_bus_handshake_timeout", handshake_timeout=True))
        clock.now += delays[-1]
    record_property("c_handshake_schedule_seconds", ",".join(f"{delay:g}" for delay in delays))

    fast = delays[: relay_pacing.HANDSHAKE_RETRY_LIMIT]
    assert all(delay <= relay_pacing.HANDSHAKE_RETRY_CAP_SECONDS for delay in fast)
    assert all(
        delay >= relay_pacing.CHANNEL_BACKOFF_BASE_SECONDS
        for delay in delays[relay_pacing.HANDSHAKE_RETRY_LIMIT:]
    ), "past the limit the channel falls back to the normal doubling"


# (b) Refused credential ------------------------------------------------------


@pytest.mark.parametrize("code", ["delegated_card_expired", "delegated_card_not_active"])
def test_b_an_expired_or_inactive_card_is_a_named_terminal_refusal(code, record_property):
    started = time.monotonic()
    observed = authorization_observation(_refusal(code))
    record_property("b_classify_ms", round((time.monotonic() - started) * 1000, 3))

    assert observed["state"] == "delegated_card_not_active"
    assert observed["error_code"] == code
    assert observed["terminal_channel"] is True, "the channel waits for authorization, no reconnect loop"
    assert observed["action"] == "authorize"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "W461 C4 finding: authorization_observation does not list delegated_card_revoked; "
        "a revoked Card falls to connection_unavailable and is retried on the backoff"
    ),
)
@pytest.mark.parametrize("code", ["delegated_card_revoked", "delegated_card_not_found"])
def test_b_a_revoked_card_is_a_named_terminal_refusal(code):
    observed = authorization_observation(_refusal(code))

    assert observed["error_code"] == code
    assert observed["terminal_channel"] is True, "a revoked Card must not be retried"


# (a) Accepted write, then unknown outcome ------------------------------------


def _prior_request(tmp_path, key: str):
    host, identity, channel = make_host(tmp_path)
    payload = {"work_ref": WORK_REF, "expected_revision": 7, "idempotency_key": key}
    request = {"action": "review.accept", "object_ref": PROJECT, "payload": payload}
    recovery = CoordinateRecovery(host.field_root)
    recovery.reserve(channel.worker_name, key, **request)
    queue = CoordinateQueue(host.field_root)
    queue.submit(
        worker_name=channel.worker_name, worker_identity=channel.worker_identity,
        runtime_kind=channel.runtime_kind, runtime_session_id=channel.runtime_session_id,
        timeout_seconds=30, request_id=f"{key}-1", **request,
    )
    recovery.record_submission(channel.worker_name, key, request_id=f"{key}-1", **request)
    return host, channel, recovery, request


def test_a_a_committed_receipt_settles_the_key_without_a_second_send(tmp_path, record_property):
    host, channel, recovery, request = _prior_request(tmp_path, "c4-a-1")
    # The peer committed once and its receipt reached the client store late.
    started = time.monotonic()
    recovery.settle_attempt(
        channel.worker_name, "c4-a-1", "c4-a-1-1", "applied",
        receipt={"operation": "review.accept", "status": "ok", "state": "applied"},
    )
    record = recovery.read(channel.worker_name, "c4-a-1")
    record_property("a_settle_ms", round((time.monotonic() - started) * 1000, 3))

    assert record["state"] == "applied"
    assert record["request_ids"] == ["c4-a-1-1"], "one request, one commit"
    assert record["request_hash"] == coordinate_request_hash(request["action"], PROJECT, request["payload"])


@pytest.mark.xfail(
    strict=True,
    reason=(
        "W461 C4 finding: there is no remote receipt lookup; when the answer is lost the "
        "only settlement is a same-key resend, which is a further mutation send"
    ),
)
def test_a_a_lost_answer_is_settled_by_a_status_read_not_a_resend(tmp_path):
    from project_board.client import coordinate_recovery

    assert hasattr(coordinate_recovery, "lookup_remote_receipt"), (
        "a status read that settles an unknown outcome without a mutation send"
    )


# (a) at the transport, (b) older generation, (c) named end state -------------


def _peer_that_commits_then_loses_the_reply(client, effects):
    async def uncertain_action(**arguments):
        client.calls.append(arguments)
        transport_id = arguments["transport_request_id"]
        if transport_id not in effects:
            effects[transport_id] = {"ok": True, "object": {"saved": True}}
            raise DomainError("data_bus_outcome_unknown", "Synthetic: reply lost after commit.", status=504)
        return effects[transport_id]

    client.action_with_transport_identity = uncertain_action


def _serve_twice(supervisor, queue, channel, monkeypatch):
    import asyncio
    from datetime import datetime, timedelta, timezone

    from project_board.client import coordinate_queue
    from relay_helpers import submit_request

    async def scenario():
        request = submit_request(
            queue, channel, action="plan.item.update",
            payload={"idempotency_key": "c4-transport-key", "changes": {"title": "Synthetic"}},
        )
        supervisor.serve_coordinate_once()
        await asyncio.gather(*supervisor._coordinate_draining.values())
        first = queue.take_response(worker_name=channel.worker_name, request_id=request["request_id"])
        later = datetime.now(timezone.utc) + timedelta(seconds=3)
        monkeypatch.setattr(coordinate_queue, "utc_now", lambda: later.isoformat())
        monkeypatch.setattr(coordinate_queue.time, "time", lambda: later.timestamp())
        supervisor.serve_coordinate_once()
        await asyncio.gather(*supervisor._coordinate_draining.values())
        second = queue.take_response(worker_name=channel.worker_name, request_id=request["request_id"])
        return request, first, second

    return asyncio.run(scenario())


def test_a_transport_commit_then_lost_reply_keeps_one_effect_and_the_session(tmp_path, monkeypatch, record_property):
    from test_connected_degraded_admission import _fixture

    host, _identity, channel, supervisor, session, client = _fixture(tmp_path)
    queue = CoordinateQueue(host.field_root)
    effects: dict = {}
    _peer_that_commits_then_loses_the_reply(client, effects)
    started = time.monotonic()

    request, first, second = _serve_twice(supervisor, queue, channel, monkeypatch)

    record_property("a_recovery_ms", round((time.monotonic() - started) * 1000, 3))
    record_property("a_peer_sends", len(client.calls))
    assert first is None, "the lost reply is not reported as an answer"
    assert second is not None and second["ok"] is True
    assert len(effects) == 1, "the peer committed exactly once"
    assert {call["transport_request_id"] for call in client.calls} == {request["request_id"]}
    assert supervisor._sessions[channel.worker_name] is session, "the retained session is kept"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "W461 C4 finding: the lost answer is settled by a second send of the same transport "
        "request to the peer, not by a status read"
    ),
)
def test_a_transport_lost_reply_is_settled_without_a_second_send(tmp_path, monkeypatch):
    from test_connected_degraded_admission import _fixture

    host, _identity, channel, supervisor, _session, client = _fixture(tmp_path)
    queue = CoordinateQueue(host.field_root)
    _peer_that_commits_then_loses_the_reply(client, {})

    _serve_twice(supervisor, queue, channel, monkeypatch)

    assert len(client.calls) == 1, "zero further mutation sends after the commit"


def test_c_an_unknown_outcome_leaves_a_named_degraded_state_with_its_evidence(tmp_path, record_property):
    from test_connected_degraded_admission import _fixture, _unknown

    host, _identity, channel, supervisor, _session, _client = _fixture(tmp_path)
    started = time.monotonic()
    _unknown(supervisor, channel)
    state = relay_pacing.channel_reconnect_state(host.path, channel.worker_name, clock=lambda: 1000.0)
    record_property("c_state_read_ms", round((time.monotonic() - started) * 1000, 3))

    assert state["state"] == "degraded"
    assert state["reason"] == "data_bus_outcome_unknown"
    assert state["attempts"] == 1 and state["next_attempt_at"]


def test_b_a_session_on_a_replaced_card_sends_nothing_and_keeps_the_request(tmp_path):
    import asyncio

    from relay_helpers import StableClient, make_supervisor, submit_request
    from test_relay_coordinate_beside_cycle import _bound_session, _write_profile

    host, _, channel = make_host(tmp_path)
    queue = CoordinateQueue(host.field_root)
    supervisor = make_supervisor(host)
    old_client = StableClient()
    session = _bound_session(host, channel, supervisor, old_client)
    supervisor._sessions[channel.worker_name] = session
    _write_profile(host, channel, access_id="synthetic-newer-card", updated_at="later")
    assert supervisor._card_fingerprint(host, channel) != session.card_fingerprint
    request = submit_request(queue, channel)

    async def scenario():
        await supervisor.serve_coordinate_pass()
        await asyncio.gather(*supervisor._coordinate_draining.values())

    try:
        asyncio.run(scenario())
        assert old_client.calls == [], "a session on the older Card writes nothing"
        assert queue.holds(worker_name=channel.worker_name, request_id=request["request_id"])
        assert not supervisor._session_matches(host, channel, session, require_card=True)
    finally:
        supervisor._store_executors.shutdown()

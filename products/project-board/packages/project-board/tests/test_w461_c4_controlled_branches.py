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

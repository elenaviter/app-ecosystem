"""W461 C4: the controlled reconnect branches the live checks did not drive.

Ops' 02:26 UTC installed-release run on 6 October passed 52 of 52 existing
probes, and Mint's 02:29 UTC verdict found that none drove three branches:
an accepted write whose answer is lost, a refused credential, and a bounded
reconnect (Ops' branch list, 04:33 UTC). Each case here uses isolated stores,
an injected clock and synthetic identities only, and fails when its property
is broken. Each records its own timings as junit properties.

Cases marked strict xfail pin a property that does not hold on the current
source, so a fix turns them into a pass that must be acknowledged: an accepted
write whose answer is lost can only be settled by sending the exact request
again, since there is no remote receipt lookup. The revoked-Card finding was
fixed by W461 C4 fix 1 and is now an ordinary pass.
"""

from __future__ import annotations

import time

import pytest

from project_board.client import relay_pacing
from project_board.client.authorization import authorization_observation
from project_board.client.coordinate_queue import CoordinateQueue
from project_board.client.coordinate_recovery import CoordinateRecovery, coordinate_request_hash
from project_board.contract.errors import DomainError
from project_board.contract.operation_identity import transport_request_hash
from project_board.contract.operation_outcomes import require_successful_operation_envelope

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


@pytest.mark.parametrize("code", ["delegated_card_revoked", "delegated_card_not_found"])
def test_b_a_revoked_card_is_a_named_terminal_refusal(code):
    # Was a strict-xfail finding (Mint, C4 NOT MET 05:36 UTC): a revoked Card
    # fell to connection_unavailable and was retried on the backoff.
    observed = authorization_observation(_refusal(code))

    assert observed["error_code"] == code
    assert observed["terminal_channel"] is True, "a revoked Card must not be retried"
    assert (observed["state"], observed["action"]) == ("delegated_card_revoked", "replace_card")


def test_b_no_permanent_credential_code_is_classified_retryable():
    # Ops design verdict (05:38 UTC): the relay and the classifier read one set
    # of permanent codes, and none of them may be reported as retryable.
    from project_board.client import relay
    from project_board.contract.errors import PERMANENT_CREDENTIAL_CODES

    assert relay.PERMANENT_ERROR_CODES is PERMANENT_CREDENTIAL_CODES
    retryable = sorted(
        code for code in PERMANENT_CREDENTIAL_CODES
        if not authorization_observation(_refusal(code))["terminal_channel"]
    )
    assert retryable == []


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


def test_a_a_lost_answer_is_settled_by_a_status_read_not_a_resend(tmp_path):
    # Was a strict-xfail finding (no remote receipt lookup). W574: a rerun
    # reads the board's durable receipt; only "applied" settles it, and the
    # read is a status read, not a mutation send.
    from project_board.client import coordinate_recovery

    recovery = coordinate_recovery.CoordinateRecovery(tmp_path)
    payload = {"idempotency_key": "c4-client-key", "changes": {"title": "Synthetic"}}
    recovery.reserve("worker-c4", "c4-client-key", action="plan.item.update", object_ref="work:plan:node:x", payload=payload)
    recovery.begin_attempt(
        "worker-c4", "c4-client-key", action="plan.item.update", object_ref="work:plan:node:x",
        payload=payload, request_id="coordinate_one",
    )
    recovery.record_submission(
        "worker-c4", "c4-client-key", action="plan.item.update", object_ref="work:plan:node:x",
        payload=payload, request_id="coordinate_one", token="coordinate_one",
    )
    record = recovery.read("worker-c4", "c4-client-key")
    reads = []

    def read(action, object_ref, read_payload):
        reads.append((action, object_ref, dict(read_payload)))
        return {"operation": action, "object": {"state": "applied", "outcome": {"operation": "plan.item.update"}}}

    receipt = coordinate_recovery.lookup_remote_receipt(record, read)

    assert receipt["state"] == "applied"
    assert reads == [(
        "operation.receipt.get", "work:plan:node:x",
        {
            "operation": "plan.item.update",
            "idempotency_key": "c4-client-key",
            "request_hash": transport_request_hash("plan.item.update", "work:plan:node:x", payload),
        },
    )]


def test_a_a_board_without_the_receipt_read_is_named_unavailable(tmp_path):
    from project_board.client import coordinate_recovery

    record = {
        "action": "plan.item.update", "object_ref": "work:plan:node:x", "idempotency_key": "k",
        "payload": {"idempotency_key": "k"}, "request_ids": ["coordinate_one"], "state": "outcome_unknown",
    }

    def old_board(action, object_ref, read_payload):
        raise DomainError(
            "work_worker_stream_operation_denied", "Not part of the governed service.",
            status=403, details={"operation": action},
        )

    assert coordinate_recovery.lookup_remote_receipt(record, old_board) == {"state": "unavailable"}
    assert coordinate_recovery.lookup_remote_receipt({**record, "state": "applied"}, old_board) is None


# (a) at the transport, (b) older generation, (c) named end state -------------


def _peer_that_commits_then_loses_the_reply(client, effects, *, ledger=False, receipt_state=None):
    """A peer that commits once and loses the reply.

    Without ``ledger`` it is a board that predates W574: it refuses the
    receipt read, so the relay keeps the same-transport resend. With
    ``ledger`` it answers operation.receipt.get from what it committed, or
    with ``receipt_state`` when a test forces one.
    """

    rows: dict = {}

    async def uncertain_action(**arguments):
        arguments.setdefault("payload", {})
        client.calls.append(arguments)
        if arguments["action"] == "operation.receipt.get":
            if not ledger:
                raise DomainError(
                    "work_worker_stream_operation_denied",
                    "This operation is not part of the governed Problem Board service.",
                    status=403, details={"operation": "operation.receipt.get"},
                )
            row = rows.get(arguments["payload"]["idempotency_key"])
            if receipt_state is not None:
                body = dict(receipt_state)
            elif row is None:
                body = {"state": "no_record"}
            else:
                body = {"state": "applied", "outcome": row, "settled_at": "2026-10-06T06:30:00Z"}
            # Through the same success-envelope check the real MCP client
            # applies to every reply (mcp_client.py), as Ops found it runs.
            return require_successful_operation_envelope(
                "operation.receipt.get", {"ok": True, "operation": "operation.receipt.get", "object": body},
            )
        transport_id = arguments["transport_request_id"]
        if transport_id not in effects:
            effects[transport_id] = {"ok": True, "object": {"saved": True}}
            rows[arguments["payload"].get("idempotency_key", "")] = effects[transport_id]
            raise DomainError("data_bus_outcome_unknown", "Synthetic: reply lost after commit.", status=504)
        return effects[transport_id]

    client.action_with_transport_identity = uncertain_action


def _mutation_sends(client):
    return [call for call in client.calls if call["action"] != "operation.receipt.get"]


def _serve_twice(supervisor, queue, channel, monkeypatch, *, action="plan.item.update", payload=None):
    import asyncio
    from datetime import datetime, timedelta, timezone

    from project_board.client import coordinate_queue
    from relay_helpers import submit_request

    async def scenario():
        request = submit_request(
            queue, channel, action=action,
            payload=payload if payload is not None else {
                "idempotency_key": "c4-transport-key", "changes": {"title": "Synthetic"},
            },
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


def test_a_board_without_the_receipt_read_gets_no_automatic_resend(tmp_path, monkeypatch, record_property):
    # W574 criterion 3: zero automatic extra mutation sends, legacy boards
    # included (Root and Ops, 06:43 and 06:46 UTC). This fails if the old
    # same-transport resend fallback returns.
    from test_connected_degraded_admission import _fixture

    host, _identity, channel, supervisor, session, client = _fixture(tmp_path)
    queue = CoordinateQueue(host.field_root)
    effects: dict = {}
    _peer_that_commits_then_loses_the_reply(client, effects)
    started = time.monotonic()

    request, first, second = _serve_twice(supervisor, queue, channel, monkeypatch)

    record_property("a_recovery_ms", round((time.monotonic() - started) * 1000, 3))
    record_property("a_peer_sends", len(_mutation_sends(client)))
    assert first is None, "the lost reply is not reported as an answer"
    assert second is None, "the board cannot confirm the outcome, so it stays unknown"
    assert len(effects) == 1, "the peer committed exactly once"
    assert len(_mutation_sends(client)) == 1, "no automatic resend on a board without the receipt read"
    assert queue.holds(worker_name=channel.worker_name, request_id=request["request_id"])
    assert supervisor._sessions[channel.worker_name] is session, "the retained session is kept"


def test_a_transport_lost_reply_is_settled_without_a_second_send(tmp_path, monkeypatch, record_property):
    # Was a strict-xfail finding (Mint, C4 NOT MET): the lost answer was
    # settled by sending the same request again. W574: the relay reads the
    # board's durable receipt instead.
    from test_connected_degraded_admission import _fixture

    host, _identity, channel, supervisor, _session, client = _fixture(tmp_path)
    queue = CoordinateQueue(host.field_root)
    _peer_that_commits_then_loses_the_reply(client, {}, ledger=True)

    _request, first, second = _serve_twice(supervisor, queue, channel, monkeypatch)

    record_property("a_mutation_sends", len(_mutation_sends(client)))
    assert len(_mutation_sends(client)) == 1, "zero further mutation sends after the commit"
    assert first is None
    assert second is not None and second["ok"] is True
    assert second["result"]["receipt_read"]["state"] == "applied"
    assert second["result"]["object"] == {"saved": True}


def test_a_refused_receipt_fails_the_request_with_the_stored_code_without_a_resend(tmp_path, monkeypatch):
    from test_connected_degraded_admission import _fixture

    host, _identity, channel, supervisor, _session, client = _fixture(tmp_path)
    queue = CoordinateQueue(host.field_root)
    _peer_that_commits_then_loses_the_reply(
        client, {}, ledger=True,
        receipt_state={"state": "refused", "code": "work_item_revision_conflict", "message": "Synthetic stale revision."},
    )

    _request, first, second = _serve_twice(supervisor, queue, channel, monkeypatch)

    assert len(_mutation_sends(client)) == 1
    assert first is None and second is not None and second["ok"] is False
    assert second["error"]["code"] == "work_item_revision_conflict"


def test_an_applied_receipt_with_a_mixed_stored_outcome_raises_its_own_code(tmp_path, monkeypatch):
    # The stored outcome gets the check its first reply would have had.
    from test_connected_degraded_admission import _fixture

    host, _identity, channel, supervisor, _session, client = _fixture(tmp_path)
    queue = CoordinateQueue(host.field_root)
    mixed = {
        "ok": True, "operation": "plan.item.update",
        "object": {"state": "partially_applied", "summary": "Synthetic mixed outcome."},
    }
    _peer_that_commits_then_loses_the_reply(
        client, {}, ledger=True, receipt_state={"state": "applied", "outcome": mixed},
    )

    _request, first, second = _serve_twice(supervisor, queue, channel, monkeypatch)

    assert len(_mutation_sends(client)) == 1
    assert first is None and second["ok"] is False
    assert second["error"]["code"] == "work_operation_mixed"


def test_a_receipt_read_is_not_judged_by_the_state_it_reports():
    refused = {
        "ok": True, "operation": "operation.receipt.get",
        "object": {"state": "refused", "code": "work_item_revision_conflict", "message": "Stale."},
    }

    assert require_successful_operation_envelope("operation.receipt.get", refused) is refused
    # A reply to another operation that calls itself a receipt read is still
    # judged by its state (Ops, 08:25 UTC).
    with pytest.raises(DomainError):
        require_successful_operation_envelope("plan.item.update", refused)


@pytest.mark.parametrize(
    "receipt",
    [
        {"state": "in_progress", "admitted_at": "2026-10-06T06:29:00Z"},
        # Not admitted at the time of the read is not "no effect": the
        # original may still arrive (Ops, 06:22 UTC).
        {"state": "no_record", "ledger_cutover_at": "2026-10-06T07:00:00Z"},
    ],
    ids=["in_progress", "no_record"],
)
def test_an_unsettled_receipt_is_read_again_and_never_resent(tmp_path, monkeypatch, receipt):
    from test_connected_degraded_admission import _fixture

    host, _identity, channel, supervisor, _session, client = _fixture(tmp_path)
    queue = CoordinateQueue(host.field_root)
    _peer_that_commits_then_loses_the_reply(client, {}, ledger=True, receipt_state=receipt)

    request, first, second = _serve_twice(supervisor, queue, channel, monkeypatch)

    assert len(_mutation_sends(client)) == 1
    assert first is None and second is None, "still unknown, kept for another read"
    assert queue.holds(worker_name=channel.worker_name, request_id=request["request_id"])
    reads = [call for call in client.calls if call["action"] == "operation.receipt.get"]
    assert len(reads) == 1
    assert reads[0]["payload"]["request_hash"] == transport_request_hash(
        request["action"], request["object_ref"], request["payload"],
    )


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


@pytest.mark.parametrize("code", ["delegated_card_revoked", "delegated_card_not_found"])
def test_b_a_revoked_card_seen_by_the_relay_parks_the_channel(tmp_path, code):
    # W461 C4 fix 1: before, the relay's cycle backed the channel off and kept
    # opening it with the same dead Card.
    from project_board.client import host_config
    from test_reauthorization_signal import _cycle, _host, _refusing_supervisor

    host, identity, _channel = _host(tmp_path)

    _cycle(_refusing_supervisor(host, DomainError(code, "Synthetic: the Card is gone.", status=401)))

    assert host_config.HostRelayConfig.load(host.path).worker(identity).state == "pending_authorization"
    refusal = relay_pacing.channel_pending_refusal(host.path, identity.worker_name)
    assert refusal["permanent"] is True


@pytest.mark.parametrize(
    ("ledger", "receipt_state", "evidence"),
    [
        (True, {"state": "no_record", "ledger_cutover_at": "2026-10-06T07:00:00Z"},
         {"receipt_read": "no_record", "ledger_cutover_at": "2026-10-06T07:00:00Z"}),
        # A board without the read: "receipt read unavailable on this board".
        (False, None, {"receipt_read": "unavailable"}),
    ],
    ids=["no_record", "board_without_the_read"],
)
def test_an_unsettled_receipt_is_reported_unknown_with_its_evidence_at_expiry(
    tmp_path, monkeypatch, ledger, receipt_state, evidence,
):
    # The re-read is bounded by the request's own expiry; past it the request
    # ends as an unknown outcome naming the last read, and is never resent.
    import asyncio
    from datetime import datetime, timedelta, timezone

    from project_board.client import coordinate_queue
    from relay_helpers import submit_request
    from test_connected_degraded_admission import _fixture

    host, _identity, channel, supervisor, _session, client = _fixture(tmp_path)
    queue = CoordinateQueue(host.field_root)
    _peer_that_commits_then_loses_the_reply(client, {}, ledger=ledger, receipt_state=receipt_state)

    async def scenario():
        request = submit_request(
            queue, channel, action="plan.item.update",
            payload={"idempotency_key": "c4-transport-key", "changes": {"title": "Synthetic"}},
        )
        expires_at = datetime.fromisoformat(request["expires_at"].replace("Z", "+00:00"))
        responses = []
        for later in (datetime.now(timezone.utc) + timedelta(seconds=3), expires_at - timedelta(seconds=1),
                      expires_at + timedelta(seconds=1)):
            supervisor.serve_coordinate_once()
            await asyncio.gather(*supervisor._coordinate_draining.values())
            responses.append(queue.take_response(worker_name=channel.worker_name, request_id=request["request_id"]))
            monkeypatch.setattr(coordinate_queue, "utc_now", lambda at=later: at.isoformat())
            monkeypatch.setattr(coordinate_queue.time, "time", lambda at=later: at.timestamp())
        supervisor.serve_coordinate_once()
        await asyncio.gather(*supervisor._coordinate_draining.values())
        responses.append(queue.take_response(worker_name=channel.worker_name, request_id=request["request_id"]))
        return responses

    responses = asyncio.run(scenario())

    assert len(_mutation_sends(client)) == 1
    assert responses[:-1] == [None, None, None]
    final = responses[-1]
    assert final is not None and final["ok"] is False
    assert final["error"]["code"] == "work_coordinate_outcome_unknown"
    shown = final["error"]["details"]["last_receipt_read"]
    assert {field: shown[field] for field in evidence} == evidence
    reads = [call for call in client.calls if call["action"] == "operation.receipt.get"]
    assert len(reads) == 2, "read again on the bounded schedule, then stopped at expiry"
    assert len({call["transport_request_id"] for call in reads}) == 2, "each read is its own transport request"


def test_a_keyless_mutation_is_never_resent_after_an_unknown_outcome(tmp_path, monkeypatch):
    # Ops, 07:20 UTC: 72 contract operations declare no idempotency_key, some
    # of them mutations. A keyless mutation cannot be looked up, so it stays
    # unknown and is never sent again.
    from test_connected_degraded_admission import _fixture

    host, _identity, channel, supervisor, _session, client = _fixture(tmp_path)
    queue = CoordinateQueue(host.field_root)
    effects: dict = {}
    _peer_that_commits_then_loses_the_reply(client, effects, ledger=True)

    request, first, second = _serve_twice(
        supervisor, queue, channel, monkeypatch,
        action="project.control.update", payload={"changes": {"banner": "Synthetic"}},
    )

    assert first is None and second is None
    assert len(effects) == 1
    assert len(client.calls) == 1, "no resend and no receipt read for a keyless mutation"
    assert queue.holds(worker_name=channel.worker_name, request_id=request["request_id"])


def test_every_contract_operation_is_either_a_declared_read_or_never_resent():
    # Enumerates the whole contract: only READ_OPERATIONS may be sent again
    # after an unknown outcome. Every other operation, keyed or not, gets no
    # automatic resend, so a new operation is a mutation until declared a read.
    import asyncio

    from project_board.client.relay import ReceiptPending, _settle_unknown_by_receipt
    from project_board.contract.worker_operation_contract import (
        PROBLEM_BOARD_OPERATIONS, READ_OPERATIONS,
    )

    async def board_without_the_read(**arguments):
        sent.append(arguments["action"])
        raise DomainError(
            "work_worker_stream_operation_denied", "Not governed here.",
            status=403, details={"operation": arguments["action"]},
        )

    for operation in sorted(PROBLEM_BOARD_OPERATIONS):
        for payload in ({}, {"idempotency_key": "k"}):
            sent: list = []
            request = {
                "request_id": "coordinate_x", "transport_attempts": 1,
                "last_transport_error": {"code": "data_bus_outcome_unknown"},
            }
            arguments = {"object_ref": "work:project:x", "action": operation, "payload": payload}
            if operation in READ_OPERATIONS:
                assert asyncio.run(_settle_unknown_by_receipt(board_without_the_read, request, arguments)) is None
                assert sent == []
                continue
            with pytest.raises(ReceiptPending):
                asyncio.run(_settle_unknown_by_receipt(board_without_the_read, request, arguments))
            assert operation not in sent, f"{operation} was sent again"

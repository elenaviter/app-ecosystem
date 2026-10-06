"""Unknown delivery on a retained socket is not a transport reconnect."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from project_board.client import cli, coordinate_queue, host_config, relay, relay_pacing
from project_board.client.io import utc_now
from project_board.client.outbox_store import OutboxStore
from project_board.client.store import SharedFieldStore
from project_board.contract.errors import DomainError

from relay_helpers import StableClient, make_host, make_supervisor, submit_request


def _fixture(tmp_path):
    host, identity, channel = make_host(tmp_path)
    supervisor = make_supervisor(host)
    host.connection_hub_state_root.mkdir(parents=True, exist_ok=True)
    profile = {
        "name": channel.profile,
        "access_id": "test-card",
        "credential_ref": "test-credential-slot",
        "endpoint": "https://runtime.example/mcp",
        "auth_type": "oauth",
    }
    (host.connection_hub_state_root / "profiles.json").write_text(
        json.dumps({"profiles": [profile]}), encoding="utf-8"
    )
    client = StableClient()
    session = SimpleNamespace(
        adapter=SimpleNamespace(client=client),
        closing=False,
        close_failure=None,
        profile=channel.profile,
        worker_name=channel.worker_name,
        channel_identity=channel.worker_identity,
        replacement_epoch=1,
        card_fingerprint=supervisor._card_fingerprint(host, channel),
    )
    supervisor._sessions[channel.worker_name] = session
    supervisor._pacing = relay_pacing.RelayPacing(
        host.path.parent / relay_pacing.PACING_FILENAME,
        clock=lambda: 1000.0,
        rng=lambda: 1.0,
    )
    supervisor._outbox_server.pacing = supervisor._pacing
    return host, identity, channel, supervisor, session, client


def _unknown(supervisor, channel):
    error = DomainError("data_bus_outcome_unknown", "No terminal evidence.", status=504)
    assert supervisor._record_channel_failure(
        supervisor._pacing, channel.worker_name, error
    ) == 60.0


def test_retained_unknown_does_not_close_cli_admission_or_erase_evidence(tmp_path):
    host, _identity, channel, supervisor, _session, _client = _fixture(tmp_path)
    _unknown(supervisor, channel)

    failure = relay_pacing.channel_reconnect_state(
        host.path, channel.worker_name, clock=lambda: 1000.0
    )
    assert failure["state"] == "degraded"
    assert failure["reason"] == "data_bus_outcome_unknown"
    assert failure["attempts"] == 1
    assert failure["next_attempt_at"] == "1970-01-01T00:17:40Z"
    assert not supervisor._pacing.channel_due(channel.worker_name)
    cli._raise_if_channel_reconnecting(host.path, channel.worker_name)
    cli._raise_if_send_channel_reconnecting(
        host.path, channel.worker_name, idempotency_key="unchanged-message-key"
    )


def test_coordinate_recovers_on_same_socket_before_periodic_poll_backoff(tmp_path):
    host, _identity, channel, supervisor, session, client = _fixture(tmp_path)
    queue = coordinate_queue.CoordinateQueue(host.field_root)
    _unknown(supervisor, channel)

    async def scenario():
        request = submit_request(queue, channel)
        assert supervisor.serve_coordinate_once() == [channel.worker_name]
        await asyncio.gather(*supervisor._coordinate_draining.values())
        response = queue.take_response(
            worker_name=channel.worker_name, request_id=request["request_id"]
        )
        assert response["ok"] is True
        assert client.calls[0]["transport_request_id"] == request["request_id"]
        assert supervisor._sessions[channel.worker_name] is session
        assert not supervisor._pacing.channel_due(channel.worker_name)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "fence", ["disconnected", "closing", "card", "channel", "profile", "quiet", "authorization", "missing_card"]
)
def test_degraded_admission_does_not_bypass_execution_fences(tmp_path, fence):
    host, _identity, channel, supervisor, session, client = _fixture(tmp_path)
    queue = coordinate_queue.CoordinateQueue(host.field_root)
    _unknown(supervisor, channel)
    if fence == "disconnected":
        client.connected = False
    elif fence == "closing":
        session.closing = True
    elif fence == "card":
        session.card_fingerprint = "replaced-card"
    elif fence == "channel":
        session.channel_identity = "codex:replaced-native-session"
    elif fence == "profile":
        session.profile = "replaced-profile"
    elif fence == "quiet":
        supervisor._pacing.observe(DomainError("rate_limited", "Quiet.", status=429))
    elif fence == "authorization":
        host_config.set_worker_channel_state(
            host.path, identity=_identity, state="pending_authorization", expected_state="active"
        )
    else:
        (host.connection_hub_state_root / "profiles.json").write_text("{}", encoding="utf-8")

    async def scenario():
        submit_request(queue, channel)
        assert supervisor.serve_coordinate_once() == []
        assert not client.calls

    asyncio.run(scenario())


@pytest.mark.parametrize("code", ["data_bus_connection_failed", "delegated_card_refresh_refused"])
def test_unavailable_or_authorization_failure_still_refuses_cli(tmp_path, code):
    host, _identity, channel, supervisor, _session, _client = _fixture(tmp_path)
    supervisor._record_channel_failure(
        supervisor._pacing, channel.worker_name, DomainError(code, "Refused.", status=503)
    )
    with pytest.raises(DomainError, match="channel is not open") as caught:
        cli._raise_if_channel_reconnecting(host.path, channel.worker_name)
    assert caught.value.code == "work_coordinate_channel_reconnecting"


def test_relay_restart_does_not_reuse_retained_socket_evidence(tmp_path):
    host, _identity, channel, supervisor, _session, _client = _fixture(tmp_path)
    _unknown(supervisor, channel)
    relay_pacing.RelayPacing(
        host.path.parent / relay_pacing.PACING_FILENAME,
        clock=lambda: 1000.0,
        forget_permanent=True,
    )
    assert relay_pacing.channel_reconnect_state(host.path, channel.worker_name)["state"] == "reconnecting"


def test_failure_recording_without_session_state_does_not_claim_a_retained_socket(tmp_path):
    config = tmp_path / "relay.json"
    pacing = relay_pacing.RelayPacing(
        tmp_path / relay_pacing.PACING_FILENAME, clock=lambda: 1000.0, rng=lambda: 1.0
    )
    supervisor = object.__new__(relay.ProblemBoardRelaySupervisor)
    assert supervisor._record_channel_failure(
        pacing, "test-worker", DomainError("data_bus_outcome_unknown", "Unknown.", status=504)
    ) == 60.0
    assert relay_pacing.channel_reconnect_state(config, "test-worker")["state"] == "reconnecting"


def test_unknown_without_a_retained_socket_or_past_retry_time_is_not_admitted(tmp_path):
    host, _identity, channel, supervisor, _session, client = _fixture(tmp_path)
    client.connected = False
    _unknown(supervisor, channel)
    supervisor._pacing._clock = lambda: 1061.0
    assert supervisor._pacing.channel_due(channel.worker_name)
    with pytest.raises(DomainError) as caught:
        cli._raise_if_send_channel_reconnecting(
            host.path, channel.worker_name, idempotency_key="original-key"
        )
    assert caught.value.code == "work_send_channel_reconnecting"
    assert caught.value.details["idempotency_key"] == "original-key"
    assert caught.value.details["delivered"] is False


def test_outbox_mail_uses_retained_socket_once_before_poll_backoff(tmp_path):
    host, _identity, channel, supervisor, session, client = _fixture(tmp_path)

    async def action(**arguments):
        client.calls.append(arguments)
        return {"object": {"ref": "work:message:test-receipt"}}

    client.action = action
    session.adapter = relay.ProblemBoardHostRelayAdapter(
        config=relay.RelayConfig.from_host_channel(host, channel, project_id="attendance"),
        field=SharedFieldStore(host.field_root),
        client=client,
        trace=supervisor._trace,
        outbox_drain_lock=supervisor._outbox_drain_lock(channel.worker_name),
    )
    _unknown(supervisor, channel)
    outbox = OutboxStore(host.field_root / ".problem-board")
    outbox.write_pending({
        "outbox_id": "outbox_connected_degraded",
        "kind": "mail.route",
        "worker_name": channel.worker_name,
        "project_ref": "work:project:one",
        "object_ref": "work:project:one",
        "payload": {"recipient": "operator", "kind": "update", "subject": "Test", "body": "Test body."},
        "state": "pending",
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "next_attempt_at": "",
    })

    async def scenario():
        assert supervisor.serve_outbox_once() == [channel.worker_name]
        await asyncio.gather(*supervisor._outbox_draining.values())
        assert supervisor.serve_outbox_once() == []
        assert len(client.calls) == 1
        assert supervisor._sessions[channel.worker_name] is session
        assert not supervisor._pacing.channel_due(channel.worker_name)

    asyncio.run(scenario())


def test_early_coordinate_retry_preserves_unknown_mutation_identity(tmp_path, monkeypatch):
    host, _identity, channel, supervisor, _session, client = _fixture(tmp_path)
    queue = coordinate_queue.CoordinateQueue(host.field_root)
    _unknown(supervisor, channel)
    effects = {}

    async def uncertain_action(**arguments):
        client.calls.append(arguments)
        if arguments["action"] == "operation.receipt.get":
            # A board before W574 has no receipt read; the relay falls back
            # to the same-transport resend below.
            raise DomainError(
                "work_worker_stream_operation_denied", "Not part of the governed service.",
                status=403, details={"operation": "operation.receipt.get"},
            )
        # The synthetic service committed the effect, but its first terminal
        # reply was lost. The replay must address the same transport operation.
        transport_id = arguments["transport_request_id"]
        if transport_id not in effects:
            effects[transport_id] = {"ok": True, "object": {"saved": True}}
            raise DomainError("data_bus_outcome_unknown", "Reply lost.", status=504)
        return effects[transport_id]

    client.action_with_transport_identity = uncertain_action

    async def scenario():
        request = submit_request(
            queue, channel, action="plan.item.update",
            payload={"idempotency_key": "original-mutation-key", "changes": {"title": "Test"}},
        )
        assert supervisor.serve_coordinate_once() == [channel.worker_name]
        await asyncio.gather(*supervisor._coordinate_draining.values())
        assert queue.take_response(
            worker_name=channel.worker_name, request_id=request["request_id"]
        ) is None
        # Advance only fixture time past the queue's one-second retry, not
        # the periodic channel backoff; no elapsed-time sleep is involved.
        later = datetime.now(timezone.utc) + timedelta(seconds=3)
        monkeypatch.setattr(coordinate_queue, "utc_now", lambda: later.isoformat())
        monkeypatch.setattr(coordinate_queue.time, "time", lambda: later.timestamp())
        assert supervisor.serve_coordinate_once() == [channel.worker_name]
        await asyncio.gather(*supervisor._coordinate_draining.values())
        response = queue.take_response(
            worker_name=channel.worker_name, request_id=request["request_id"]
        )
        # W574: the board cannot confirm the outcome, so the request stays
        # unknown with the same identity; it is never sent again by the relay.
        assert response is None
        assert queue.holds(worker_name=channel.worker_name, request_id=request["request_id"])
        sends = [call for call in client.calls if call["action"] != "operation.receipt.get"]
        reads = [call for call in client.calls if call["action"] == "operation.receipt.get"]
        assert [call["transport_request_id"] for call in sends] == [request["request_id"]]
        assert [call["payload"]["idempotency_key"] for call in reads] == ["original-mutation-key"]
        assert len(effects) == 1

    asyncio.run(scenario())

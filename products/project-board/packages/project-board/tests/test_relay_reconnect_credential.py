"""A dropped channel comes back on its own, with a bearer valid at reconnect time.

The Data Bus client reconnects after a transport drop and asks the relay for
the credential to present at each reconnect handshake. Before this, the
socket presented the bearer captured at first connect, a delegated bearer
that lives one hour under a socket that lived up to nine, and the server
refused it on every attempt until the cycle replaced the session minutes
later. Three things here:

- the credential source the relay hands the client (relay_admission): the
  profile's current bearer on the clock, one re-mint after a refusal, never a
  second one in the same episode;
- the cycle's grace: a session whose socket is reconnecting is given its ten
  seconds before the poll replaces it;
- `pb worker send` while the relay has the channel down: refused at once,
  naming the channel state and the retry with the same idempotency key.
"""

from __future__ import annotations

import asyncio
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from project_board.client import cli, relay_admission, relay_pacing as pacing
from project_board.client.store import SharedFieldStore
from project_board.contract.errors import DomainError

from relay_helpers import make_host as _host, make_supervisor as _supervisor
from test_relay_coordinate_beside_cycle import _bound_session


# -- the credential source ----------------------------------------------------


def _attempt(generation: int, attempt: int, refusal: dict[str, Any] | None = None):
    return SimpleNamespace(
        connection_generation=generation, attempt=attempt, previous_refusal=refusal
    )


def _source(*, refresh: bool = True):
    calls: list[str] = []

    async def resolve_bearer() -> str:
        calls.append("resolve")
        return f"clock-{len(calls)}"

    async def refresh_bearer() -> str:
        calls.append("refresh")
        return f"minted-{len(calls)}"

    source = relay_admission.reconnect_credential_source(
        resolve_bearer=resolve_bearer,
        refresh_bearer=refresh_bearer if refresh else None,
        credential=lambda bearer: {"bearer": bearer},
        profile="problem-board-codex-one",
    )
    return source, calls


def test_the_first_reconnect_handshake_presents_the_clocks_bearer():
    source, calls = _source()

    credential = asyncio.run(source(_attempt(1, 1)))

    assert credential == {"bearer": "clock-1"}
    assert calls == ["resolve"]


def test_a_refused_handshake_is_followed_by_one_refresh_and_no_second():
    source, calls = _source()
    refused = {"code": "invalid_bearer", "status": 401, "message": "token_expired"}

    async def scenario():
        first = await source(_attempt(1, 1))
        second = await source(_attempt(1, 2, refused))
        third = await source(_attempt(1, 3, refused))
        return first, second, third

    first, second, third = asyncio.run(scenario())

    assert first == {"bearer": "clock-1"}
    assert second == {"bearer": "minted-2"}, "the refusal earns one re-mint through the card"
    assert third == {"bearer": "clock-3"}, "a second refusal in the episode is the cycle's to classify"
    assert calls == ["resolve", "refresh", "resolve"]


def test_a_new_connection_generation_may_refresh_again():
    source, calls = _source()
    refused = {"code": "invalid_bearer", "status": 401, "message": "token_expired"}

    async def scenario():
        await source(_attempt(1, 2, refused))
        await source(_attempt(2, 2, refused))

    asyncio.run(scenario())

    assert calls == ["refresh", "refresh"]


def test_a_static_bearer_profile_never_refreshes():
    source, calls = _source(refresh=False)
    refused = {"code": "invalid_bearer", "status": 401, "message": "token_expired"}

    credential = asyncio.run(source(_attempt(1, 2, refused)))

    assert credential == {"bearer": "clock-1"}
    assert calls == ["resolve"]


# -- the cycle's grace ---------------------------------------------------------


class _ReconnectingClient:
    """A Data Bus client whose socket dropped and reconnects on its own."""

    def __init__(self, *, comes_back: bool) -> None:
        self.connected = False
        self.comes_back = comes_back
        self.waits: list[float] = []
        self.calls: list[dict[str, Any]] = []

    async def wait_until_connected(self, timeout_seconds: float) -> bool:
        self.waits.append(timeout_seconds)
        self.connected = self.comes_back
        return self.connected

    async def action_with_transport_identity(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(dict(kwargs))
        return {"ok": True, "object": {"ref": kwargs["object_ref"]}}


def _adapter(client: _ReconnectingClient) -> SimpleNamespace:
    async def poll_attendances_once() -> dict[str, Any]:
        if not client.connected:
            raise DomainError(
                "work_relay_transport_unavailable",
                "The Data Bus session is not connected.",
                status=503,
                details={"transport_code": "data_bus_not_connected"},
            )
        return {"ok": True, "polled": True}

    return SimpleNamespace(client=client, poll_attendances_once=poll_attendances_once)


def test_the_cycle_keeps_a_session_whose_socket_reconnects_within_the_grace(tmp_path, caplog):
    host, _identity, channel = _host(tmp_path)
    supervisor = _supervisor(host)
    supervisor.reconnect_grace_seconds = 0.05
    client = _ReconnectingClient(comes_back=True)
    session = _bound_session(host, channel, supervisor, client, adapter=_adapter(client))
    supervisor._sessions[channel.worker_name] = session

    with caplog.at_level("INFO", logger=_relay_logger_name()):
        result = asyncio.run(supervisor._poll_channel(host, channel))

    assert result["polled"] is True
    assert client.waits == [0.05], "the wait is the cycle's grace, not the client's default"
    assert supervisor._sessions[channel.worker_name] is session, "the same session carried on"
    assert session.closed == [], "nothing was torn down"
    assert "event=awaiting_reconnect" in caplog.text
    assert "event=reconnect_observed" in caplog.text


def test_the_cycle_replaces_a_session_whose_socket_stays_down_past_the_grace(tmp_path, caplog):
    host, _identity, channel = _host(tmp_path)
    supervisor = _supervisor(host)
    supervisor.reconnect_grace_seconds = 0.05
    client = _ReconnectingClient(comes_back=False)
    session = _bound_session(host, channel, supervisor, client, adapter=_adapter(client))
    supervisor._sessions[channel.worker_name] = session

    with caplog.at_level("INFO", logger=_relay_logger_name()):
        with pytest.raises(DomainError) as failed:
            asyncio.run(supervisor._poll_channel(host, channel))

    assert failed.value.code == "work_relay_transport_unavailable"
    assert client.waits == [0.05]
    assert channel.worker_name not in supervisor._sessions, "the cycle replaces it as before"
    assert "event=reconnect_grace_expired" in caplog.text


def test_a_connected_session_is_not_made_to_wait(tmp_path):
    host, _identity, channel = _host(tmp_path)
    supervisor = _supervisor(host)
    client = _ReconnectingClient(comes_back=True)
    client.connected = True
    session = _bound_session(host, channel, supervisor, client, adapter=_adapter(client))
    supervisor._sessions[channel.worker_name] = session

    result = asyncio.run(supervisor._poll_channel(host, channel))

    assert result["polled"] is True
    assert client.waits == []


def _relay_logger_name() -> str:
    from project_board.client import relay

    return relay.logger.name


# -- pb worker send while the relay has the channel down ------------------------


def _mark_reconnecting(config_path: Path, worker_name: str) -> None:
    state = pacing.RelayPacing(Path(config_path).parent / pacing.PACING_FILENAME, rng=lambda: 1.0)
    state.record_failure(worker_name, pacing.HANDSHAKE_TIMEOUT_REASON, handshake_timeout=True)


def _send_args(host, identity, **overrides: Any) -> Namespace:
    values = {
        "command": "worker",
        "worker_command": "send",
        "runtime_kind": identity.runtime_kind,
        "runtime_session_id": identity.runtime_session_id,
        "config": str(host.path),
        "project_ref": None,
        "recipient": "operator",
        "route": "auto",
        "kind": "update",
        "subject": "A note for the operator",
        "body": "one line",
        "body_file": None,
        "payload_file": None,
        "work_ref": None,
        "correlation_id": None,
        "reply_to": None,
        "idempotency_key": "send-while-down-1",
        "attach": None,
    }
    values.update(overrides)
    return Namespace(**values)


def test_worker_send_refuses_at_once_while_the_channel_is_reconnecting(tmp_path):
    host, identity, channel = _host(tmp_path)
    _mark_reconnecting(host.path, channel.worker_name)

    with pytest.raises(DomainError) as refused:
        cli._worker_command(_send_args(host, identity))

    assert refused.value.code == "work_send_channel_reconnecting"
    details = refused.value.details
    assert details["channel_state"] == "reconnecting"
    assert details["delivered"] is False
    assert details["idempotency_key"] == "send-while-down-1"
    assert details["last_error"] == pacing.HANDSHAKE_TIMEOUT_REASON
    assert details["attempts"] == 1
    assert details["next_attempt_at"]
    assert "not delivered" in str(refused.value)
    assert "same idempotency key" in str(refused.value)


def test_worker_send_is_queued_when_the_relay_has_no_failure_on_record(tmp_path):
    host, identity, _channel = _host(tmp_path)
    field = SharedFieldStore(host.field_root)
    field.initialize(field_id="send-with-open-channel")
    field.register_worker(
        worker_name=identity.worker_name,
        worker_identity=identity.worker_identity,
        runtime_kind=identity.runtime_kind,
        runtime_session_id=identity.runtime_session_id,
        capabilities=[],
        authority_label="connection-hub:test-profile",
        control_plane_state="published",
    )

    notice = cli._worker_command(_send_args(host, identity))

    assert notice, "an open channel carries the message as before"

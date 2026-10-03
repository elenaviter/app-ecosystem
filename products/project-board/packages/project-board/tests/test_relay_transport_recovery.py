"""An unknown outcome during a plain transport drop keeps the channel's session.

A remote relay host, 2026-10-02 21:03Z: a heartbeat was in its outcome wait
when the socket dropped. The Data Bus client reconnected on its own within
seconds, but the heartbeat then failed with an unknown outcome while the
client was still disconnected, so the cycle dropped the session. The channel
came back only through a full reopen with a token refresh after its 30 to 60 s
backoff. The client now says when a drop is only transport
(``transport_recovering``), and the cycle keeps the session then, as it
already did for an unknown outcome on a connected socket.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from project_board.client import relay_pacing
from project_board.contract.errors import DomainError

from relay_helpers import make_host as _host, make_supervisor as _supervisor
from test_relay_coordinate_beside_cycle import _bound_session


class _DroppingClient:
    """Connected when the turn starts; the transport drops during the poll."""

    def __init__(self, *, recovering_after_drop: bool) -> None:
        self.connected = True
        self.transport_recovering = False
        self.recovering_after_drop = recovering_after_drop
        self.waits: list[float] = []

    async def wait_until_connected(self, timeout_seconds: float) -> bool:
        self.waits.append(timeout_seconds)
        return self.connected


def _dropping_adapter(client: _DroppingClient, *, failure: BaseException | None = None):
    async def poll_attendances_once() -> dict[str, Any]:
        client.connected = False
        client.transport_recovering = client.recovering_after_drop
        raise failure or DomainError(
            "data_bus_outcome_unknown",
            "The Data Bus operation did not return a terminal result before the timeout.",
            status=504,
            details={"disconnected_during_request": True},
        )

    return SimpleNamespace(client=client, poll_attendances_once=poll_attendances_once)


def _fixture(tmp_path, *, recovering_after_drop: bool, failure: BaseException | None = None):
    host, _identity, channel = _host(tmp_path)
    supervisor = _supervisor(host)
    supervisor._pacing = relay_pacing.RelayPacing(
        host.path.parent / relay_pacing.PACING_FILENAME,
        clock=lambda: 1000.0,
        rng=lambda: 1.0,
    )
    client = _DroppingClient(recovering_after_drop=recovering_after_drop)
    session = _bound_session(
        host,
        channel,
        supervisor,
        client,
        adapter=_dropping_adapter(client, failure=failure),
    )
    supervisor._sessions[channel.worker_name] = session
    return host, channel, supervisor, session, client


def _poll_failure(supervisor, host, channel) -> BaseException:
    with pytest.raises(BaseException) as failed:
        asyncio.run(supervisor._poll_channel(host, channel))
    return failed.value


def test_an_unknown_outcome_while_the_client_reconnects_keeps_the_session(tmp_path):
    host, channel, supervisor, session, client = _fixture(tmp_path, recovering_after_drop=True)

    error = _poll_failure(supervisor, host, channel)

    assert getattr(error, "code", "") == "data_bus_outcome_unknown"
    assert supervisor._sessions[channel.worker_name] is session, "the session carried on"
    assert session.closed == [], "nothing was torn down: no new credential, no reopen"

    supervisor._record_channel_failure(supervisor._pacing, channel.worker_name, error)
    state = relay_pacing.channel_reconnect_state(
        host.path, channel.worker_name, clock=lambda: 1000.0
    )
    assert state["state"] == "degraded", "a retained socket, not a reconnecting channel"
    # Foreground work waits while the socket is down and resumes once it is back.
    assert not supervisor._pacing.channel_request_due(channel.worker_name, connected=False)
    client.connected = True
    assert supervisor._pacing.channel_request_due(channel.worker_name, connected=True)


def test_an_unknown_outcome_without_a_recovering_transport_still_drops_the_session(tmp_path):
    host, channel, supervisor, session, _client = _fixture(tmp_path, recovering_after_drop=False)

    error = _poll_failure(supervisor, host, channel)

    assert getattr(error, "code", "") == "data_bus_outcome_unknown"
    assert channel.worker_name not in supervisor._sessions
    assert session.closed == ["closed"]
    supervisor._record_channel_failure(supervisor._pacing, channel.worker_name, error)
    state = relay_pacing.channel_reconnect_state(
        host.path, channel.worker_name, clock=lambda: 1000.0
    )
    assert state["state"] == "reconnecting"


def test_a_replaced_card_is_never_kept_by_a_recovering_transport(tmp_path):
    host, channel, supervisor, session, _client = _fixture(tmp_path, recovering_after_drop=True)
    original_poll = session.adapter.poll_attendances_once

    async def poll_while_the_card_is_replaced() -> dict[str, Any]:
        session.card_fingerprint = "replaced-card"
        return await original_poll()

    session.adapter.poll_attendances_once = poll_while_the_card_is_replaced

    _poll_failure(supervisor, host, channel)

    assert channel.worker_name not in supervisor._sessions
    assert session.closed == ["closed"]


def test_an_unreadable_binding_drops_the_session_and_keeps_the_original_failure(tmp_path):
    host, channel, supervisor, session, _client = _fixture(tmp_path, recovering_after_drop=True)

    def unreadable(*_args: Any, **_kwargs: Any) -> bool:
        raise OSError("profile record unreadable")

    original_poll = session.adapter.poll_attendances_once

    async def poll_while_the_profile_breaks() -> dict[str, Any]:
        supervisor._session_matches = unreadable
        return await original_poll()

    session.adapter.poll_attendances_once = poll_while_the_profile_breaks

    error = _poll_failure(supervisor, host, channel)

    assert getattr(error, "code", "") == "data_bus_outcome_unknown"
    assert channel.worker_name not in supervisor._sessions
    assert session.closed == ["closed"]


@pytest.mark.parametrize(
    "failure",
    [
        DomainError("data_bus_connection_failed", "Refused.", status=503),
        DomainError("delegated_card_refresh_refused", "Refused.", status=401),
        asyncio.CancelledError(),
    ],
    ids=["other-failure", "authorization", "cancelled"],
)
def test_only_an_unknown_outcome_is_kept_through_a_transport_recovery(tmp_path, failure):
    host, channel, supervisor, session, _client = _fixture(
        tmp_path, recovering_after_drop=True, failure=failure
    )

    _poll_failure(supervisor, host, channel)

    assert channel.worker_name not in supervisor._sessions
    assert session.closed == ["closed"]


def test_a_closing_session_is_never_counted_as_retained(tmp_path):
    host, channel, supervisor, session, client = _fixture(tmp_path, recovering_after_drop=True)
    client.connected = False
    client.transport_recovering = True
    session.closing = True

    supervisor._record_channel_failure(
        supervisor._pacing,
        channel.worker_name,
        DomainError("data_bus_outcome_unknown", "Unknown.", status=504),
    )

    state = relay_pacing.channel_reconnect_state(
        host.path, channel.worker_name, clock=lambda: 1000.0
    )
    assert state["state"] == "reconnecting"


def test_a_client_without_the_recovery_signal_keeps_the_old_rule(tmp_path):
    # An older client has no transport_recovering: only a connected socket is kept.
    host, channel, supervisor, session, _client = _fixture(tmp_path, recovering_after_drop=True)
    older = SimpleNamespace(connected=True)

    async def poll_attendances_once() -> dict[str, Any]:
        older.connected = False
        raise DomainError("data_bus_outcome_unknown", "Unknown.", status=504)

    session.adapter = SimpleNamespace(client=older, poll_attendances_once=poll_attendances_once)

    _poll_failure(supervisor, host, channel)

    assert channel.worker_name not in supervisor._sessions
    assert session.closed == ["closed"]

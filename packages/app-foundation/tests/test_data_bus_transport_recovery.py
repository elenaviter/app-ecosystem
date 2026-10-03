"""Transport recovery of the Data Bus client: a silent transport is replaced at
the first ingress timeout, a transient drop says so to the owner, and a
reconnect handshake says where its time went.

Origin (2026-10-02, a remote relay host): every inbound packet on three sockets
stopped at once. Two ingress acknowledgements timed out after 15 s and the
socket stayed in place until Engine.IO gave up 30 s later. A request still in
its outcome wait then failed after the drop, and its owner replaced the whole
session with a 30 to 60 s backoff and a token refresh, although the client
reconnected on its own within seconds.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from contextlib import asynccontextmanager
from typing import Any

import pytest
import socketio
import uvicorn
from socketio.exceptions import TimeoutError as SocketIOTimeoutError

from app_foundation.data_bus import (
    DataBusClaim,
    DataBusOutcomeUnknown,
    DelegatedCardCredential,
    FederatedDataBusClient,
    HandshakeAttempt,
)
from app_foundation.data_bus import client as client_module


def _claim(expires_at: int = 2_000_000_000) -> DataBusClaim:
    return DataBusClaim(
        tenant="tenant-a",
        project="project-a",
        bundle_id="problem-board@1-0",
        session_id="session-a",
        expires_at=expires_at,
        federated_token="secret-token",
        partition_ref="work:worker-stream:abc",
    )


class _Bus:
    """A real Socket.IO server whose ingress answers the way a test says."""

    def __init__(self) -> None:
        self.sio = socketio.AsyncServer(async_mode="asgi")
        self.sids: list[str] = []
        self.connected = asyncio.Event()
        # For each successive publish: "ack" answers at once, "silent" never.
        self.publish_plan: list[str] = []
        self.published: list[str] = []
        # Handshakes the server refuses, by order (0 is the first connect).
        self.refuse_handshakes: set[int] = set()
        self.admission_delays: dict[int, float] = {}
        # Sessions the server saw end: proof that a client's old transport
        # actually closed, not only that the client forgot it.
        self.ended: list[str] = []
        self._release = asyncio.Event()

        @self.sio.event
        async def disconnect(sid: str, *args: Any) -> None:
            self.ended.append(sid)

        @self.sio.event
        async def connect(sid: str, environ: dict[str, Any], auth: Any) -> bool:
            index = len(self.sids)
            self.sids.append(sid)
            await asyncio.sleep(self.admission_delays.get(index, 0.0))
            if index in self.refuse_handshakes:
                raise socketio.exceptions.ConnectionRefusedError(
                    {"error_type": "invalid_bearer", "status": 401, "message": "refused"}
                )
            self.connected.set()
            return True

        @self.sio.on("data_bus.publish")
        async def publish(sid: str, data: dict[str, Any]) -> Any:
            message_id = data["messages"][0]["message_id"]
            self.published.append(message_id)
            mode = self.publish_plan.pop(0) if self.publish_plan else "ack"
            if mode == "silent":
                await self._release.wait()
                return None
            return {"status": "accepted", "accepted": [{"message_id": message_id}]}

    async def deliver_result(self, sid: str, message_id: str) -> None:
        await self.sio.emit(
            "chat_service",
            {
                "type": "kdcube.data_bus.result",
                "data": {"message_id": message_id, "data": {"ok": True}},
            },
            to=sid,
        )

    def release(self) -> None:
        self._release.set()


@asynccontextmanager
async def _bus_server():
    bus = _Bus()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            socketio.ASGIApp(bus.sio),
            host="127.0.0.1",
            port=port,
            lifespan="off",
            log_level="critical",
        )
    )
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    while not server.started:
        if server_task.done():
            await server_task
        await asyncio.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}", bus
    finally:
        bus.release()
        server.should_exit = True
        await server_task


async def _owned_client(url: str, **kwargs: Any) -> FederatedDataBusClient:
    client = FederatedDataBusClient(
        platform_url=url,
        credential=kwargs.pop("credential", _claim()),
        ingress_timeout_seconds=kwargs.pop("ingress_timeout_seconds", 0.3),
        outcome_timeout_seconds=kwargs.pop("outcome_timeout_seconds", 2.0),
        namespace_admission_timeout_seconds=2.0,
        **kwargs,
    )
    client._reconnect_delay_seconds = 0.01
    await client.connect()
    return client


async def _request(client: FederatedDataBusClient, message_id: str) -> Any:
    return await client.request(
        subject="problem_board.command.v1",
        object_ref="work:worker-stream:abc",
        payload={},
        idempotency_key=f"key-{message_id}",
        message_id=message_id,
    )


async def _until(predicate: Any, seconds: float = 3.0) -> None:
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


# -- A: a silent transport is replaced at the first ingress timeout ----------


@pytest.mark.asyncio
async def test_an_ingress_timeout_on_a_silent_transport_reconnects_at_once(caplog) -> None:
    async with _bus_server() as (url, bus):
        client = await _owned_client(url)
        first_socket = client.socket
        bus.publish_plan = ["silent"]

        with caplog.at_level(logging.INFO, logger="app_foundation.data_bus.client"):
            with pytest.raises(DataBusOutcomeUnknown) as captured:
                await _request(client, "message-1")
            assert captured.value.details["silent_transport_replaced"] is True
            assert captured.value.details["ingress_ack_received"] is False

            # The reconnect runs now, not after Engine.IO's own 30 s or more.
            assert await client.wait_until_connected(3.0) is True

        assert client.connection_generation == 2
        assert client.socket is not first_socket
        assert len(bus.sids) == 2
        assert "event=silent_transport_replaced connection_generation=1" in caplog.text
        assert "secret-token" not in caplog.text
        # The replacement works: the next request is acknowledged and answered.
        bus.publish_plan = ["ack"]

        async def answer() -> None:
            await _until(lambda: "message-2" in bus.published)
            await bus.deliver_result(bus.sids[-1], "message-2")

        answering = asyncio.create_task(answer())
        outcome = await _request(client, "message-2")
        await answering
        assert outcome.status == "ok"
        await client.close()


@pytest.mark.asyncio
async def test_a_transport_that_delivered_anything_during_the_wait_is_kept() -> None:
    async with _bus_server() as (url, bus):
        client = await _owned_client(url)
        first_socket = client.socket
        bus.publish_plan = ["silent"]

        async def talk() -> None:
            await _until(lambda: bus.published)
            # Any service event on this socket: here another peer's receipt.
            await bus.deliver_result(bus.sids[0], "someone-else")

        talking = asyncio.create_task(talk())
        with pytest.raises(DataBusOutcomeUnknown) as captured:
            await _request(client, "message-1")
        await talking

        assert captured.value.details["silent_transport_replaced"] is False
        assert client.connected
        assert client.connection_generation == 1
        assert client.socket is first_socket
        await client.close()


@pytest.mark.asyncio
async def test_a_request_that_outlived_a_reconnect_never_closes_the_newer_socket() -> None:
    async with _bus_server() as (url, bus):
        client = await _owned_client(url)
        bus.publish_plan = ["silent"]

        async def drop_during_wait() -> None:
            await _until(lambda: bus.published)
            await bus.sio.disconnect(bus.sids[0])
            await _until(lambda: client.connection_generation == 2)

        dropping = asyncio.create_task(drop_during_wait())
        with pytest.raises(DataBusOutcomeUnknown) as captured:
            await _request(client, "message-1")
        await dropping
        newer = client.socket

        assert captured.value.details["disconnected_during_request"] is True
        assert captured.value.details["silent_transport_replaced"] is False
        await asyncio.sleep(0.05)
        assert client.connected
        assert client.socket is newer
        assert client.connection_generation == 2
        await client.close()


@pytest.mark.asyncio
async def test_a_waiting_outcome_survives_the_replacement_and_resolves_on_the_new_socket() -> None:
    # The replacement is a drop like any other: requests already in their
    # outcome wait keep waiting, because receipts fan out to the session.
    async with _bus_server() as (url, bus):
        client = await _owned_client(url, outcome_timeout_seconds=3.0)
        bus.publish_plan = ["ack", "silent"]

        waiting = asyncio.create_task(_request(client, "message-1"))
        await _until(lambda: "message-1" in bus.published)
        with pytest.raises(DataBusOutcomeUnknown) as captured:
            await _request(client, "message-2")
        assert captured.value.details["silent_transport_replaced"] is True
        assert await client.wait_until_connected(3.0) is True
        assert not waiting.done()

        await bus.deliver_result(bus.sids[-1], "message-1")
        outcome = await asyncio.wait_for(waiting, 2.0)
        assert outcome.status == "ok"
        assert client._pending == {}
        await client.close()


@pytest.mark.asyncio
async def test_a_late_timer_is_not_taken_for_a_silent_transport() -> None:
    # W448: a blocked event loop fires the deadline late and can leave inbound
    # packets unread. That silence says nothing about the transport.
    class StallingSocket:
        def __init__(self) -> None:
            self.connected = False
            self.handlers: dict[str, Any] = {}
            self.disconnect_calls = 0

        def on(self, event: str, handler: Any) -> None:
            self.handlers[event] = handler

        def get_sid(self) -> str:
            return "socketio-1"

        async def connect(self, *args: Any, **kwargs: Any) -> None:
            self.connected = True
            await self.handlers["connect"]()

        async def call(self, event: str, data: dict[str, Any], timeout: float) -> Any:
            time.sleep(timeout + client_module._SILENT_TRANSPORT_MAX_TIMER_OVERRUN_SECONDS + 0.1)
            raise SocketIOTimeoutError()

        async def disconnect(self) -> None:
            self.disconnect_calls += 1
            self.connected = False

        async def shutdown(self) -> None:
            self.connected = False

    stalling = StallingSocket()
    client = FederatedDataBusClient(
        platform_url="https://platform.example",
        credential=_claim(),
        socket_factory=lambda: stalling,
        ingress_timeout_seconds=0.05,
    )
    # The production factory owns its reconnects; this one stands in for it.
    client._owns_reconnect = True
    await client.connect()

    with pytest.raises(DataBusOutcomeUnknown) as captured:
        await _request(client, "message-1")

    assert captured.value.details["timer_overrun_seconds"] >= 1.0
    assert captured.value.details["silent_transport_replaced"] is False
    assert stalling.disconnect_calls == 0
    assert client.connection_generation == 1
    await client.close()


@pytest.mark.asyncio
async def test_a_custom_socket_factory_keeps_its_own_transport() -> None:
    class TimingOutSocket:
        def __init__(self) -> None:
            self.connected = False
            self.handlers: dict[str, Any] = {}
            self.disconnect_calls = 0

        def on(self, event: str, handler: Any) -> None:
            self.handlers[event] = handler

        def get_sid(self) -> str:
            return "socketio-1"

        async def connect(self, *args: Any, **kwargs: Any) -> None:
            self.connected = True
            await self.handlers["connect"]()

        async def call(self, event: str, data: dict[str, Any], timeout: float) -> Any:
            raise SocketIOTimeoutError()

        async def disconnect(self) -> None:
            self.disconnect_calls += 1

        async def shutdown(self) -> None:
            self.connected = False

    timing_out = TimingOutSocket()
    client = FederatedDataBusClient(
        platform_url="https://platform.example",
        credential=_claim(),
        socket_factory=lambda: timing_out,
    )
    await client.connect()

    with pytest.raises(DataBusOutcomeUnknown) as captured:
        await _request(client, "message-1")

    assert captured.value.details["silent_transport_replaced"] is False
    assert timing_out.disconnect_calls == 0
    await client.close()


def _released(socket: Any) -> bool:
    """The old transport's network resources are gone, not just forgotten."""

    eio = socket.eio
    loops_done = all(
        task is None or task.done()
        for task in (eio.read_loop_task, eio.write_loop_task)
    )
    return bool(
        eio.http is not None
        and eio.http.closed
        and loops_done
        and eio.state != "connected"
        and socket.connected is False
    )


async def _replace_with_hung_disconnect(
    client: FederatedDataBusClient, bus: _Bus, message_id: str
) -> Any:
    """One silent replacement whose old socket never finishes disconnecting."""

    old = client.socket
    hung = asyncio.Event()

    async def hung_disconnect() -> None:
        await hung.wait()

    old.disconnect = hung_disconnect
    bus.publish_plan = ["silent"]
    with pytest.raises(DataBusOutcomeUnknown) as captured:
        await _request(client, message_id)
    assert captured.value.details["silent_transport_replaced"] is True
    assert await client.wait_until_connected(3.0) is True
    return old


@pytest.mark.asyncio
async def test_close_releases_every_retired_transport_even_when_its_disconnect_hangs() -> None:
    # Review of the first head: close() cancelled the retirement tasks but
    # left every old socket and its HTTP session open.
    async with _bus_server() as (url, bus):
        client = await _owned_client(url)
        old_sockets = []
        retiring_counts = []
        for index in range(3):
            old_sockets.append(await _replace_with_hung_disconnect(client, bus, f"message-{index}"))
            retiring_counts.append(len(client._retiring_transports))
        old_sids = bus.sids[:3]
        newest = client.socket

        await asyncio.wait_for(client.close(), 3.0)

        assert retiring_counts == [1, 2, 3]
        assert client._retiring_transports == {}
        assert all(_released(old) for old in old_sockets)
        await _until(lambda: set(old_sids) <= set(bus.ended))
        assert newest not in old_sockets


@pytest.mark.asyncio
async def test_a_hung_disconnect_is_released_after_its_bound_without_close(monkeypatch) -> None:
    monkeypatch.setattr(client_module, "_RETIRE_TRANSPORT_GRACE_SECONDS", 0.2)
    async with _bus_server() as (url, bus):
        client = await _owned_client(url)
        old = await _replace_with_hung_disconnect(client, bus, "message-1")
        newer = client.socket

        await _until(lambda: not client._retiring_transports)
        assert _released(old)
        await _until(lambda: bus.sids[0] in bus.ended)
        # Only the old transport was touched: the newer one still serves.
        assert client.connected
        assert client.socket is newer
        assert bus.sids[1] not in bus.ended
        await client.close()


@pytest.mark.asyncio
async def test_too_many_retiring_transports_release_a_new_one_at_once(monkeypatch) -> None:
    monkeypatch.setattr(client_module, "_MAX_RETIRING_TRANSPORTS", 0)
    async with _bus_server() as (url, bus):
        client = await _owned_client(url)
        old = client.socket
        graceful_calls = []

        async def counted_disconnect() -> None:
            graceful_calls.append(True)

        old.disconnect = counted_disconnect
        bus.publish_plan = ["silent"]
        with pytest.raises(DataBusOutcomeUnknown):
            await _request(client, "message-1")

        await _until(lambda: not client._retiring_transports)
        assert graceful_calls == [], "past the cap the graceful step is skipped"
        assert _released(old)
        assert await client.wait_until_connected(3.0) is True
        await client.close()


@pytest.mark.asyncio
async def test_close_before_the_reconnect_lands_does_not_shut_the_released_socket_again() -> None:
    async with _bus_server() as (url, bus):
        client = await _owned_client(url)
        # The reconnect waits, so the retired socket is still the client's
        # current one when close() runs.
        client._reconnect_delay_seconds = 30.0
        old = client.socket
        shutdowns = []
        original_shutdown = old.shutdown

        async def counted_shutdown() -> None:
            shutdowns.append(True)
            await original_shutdown()

        old.shutdown = counted_shutdown
        bus.publish_plan = ["silent"]
        with pytest.raises(DataBusOutcomeUnknown):
            await _request(client, "message-1")
        await _until(lambda: not client._retiring_transports)
        assert client.socket is old

        await asyncio.wait_for(client.close(), 3.0)

        assert _released(old)
        assert shutdowns == []


# -- C: the client tells its owner when a drop is only transport -------------


@pytest.mark.asyncio
async def test_a_dropped_transport_is_recovering_until_the_reconnect_lands() -> None:
    async with _bus_server() as (url, bus):
        bus.admission_delays[1] = 0.4
        client = await _owned_client(url)
        assert client.transport_recovering is False, "connected is not recovering"

        await bus.sio.disconnect(bus.sids[0])
        await _until(lambda: not client.connected)
        assert client.transport_recovering is True

        assert await client.wait_until_connected(3.0) is True
        assert client.transport_recovering is False
        await client.close()


@pytest.mark.asyncio
async def test_a_refused_handshake_ends_the_recovery_for_the_whole_episode() -> None:
    async with _bus_server() as (url, bus):
        bus.refuse_handshakes = {1, 2, 3, 4, 5, 6, 7, 8}
        client = await _owned_client(url)

        await bus.sio.disconnect(bus.sids[0])
        await _until(lambda: len(bus.sids) >= 2)
        await _until(lambda: client._episode_refused)
        assert client.transport_recovering is False
        # The next attempt starts and clears the per-attempt refusal record;
        # the episode stays refused.
        await _until(lambda: len(bus.sids) >= 3)
        assert client.transport_recovering is False
        await client.close()


@pytest.mark.asyncio
async def test_recovery_is_bounded_by_its_window(monkeypatch) -> None:
    async with _bus_server() as (url, bus):
        bus.admission_delays[1] = 1.0
        client = await _owned_client(url)
        await bus.sio.disconnect(bus.sids[0])
        await _until(lambda: not client.connected)
        assert client.transport_recovering is True

        monkeypatch.setattr(client_module, "_TRANSPORT_RECOVERY_WINDOW_SECONDS", 0.0)
        await asyncio.sleep(0.01)
        assert client.transport_recovering is False
        await client.close()


@pytest.mark.asyncio
async def test_an_expired_credential_or_a_closed_client_is_not_recovering() -> None:
    async with _bus_server() as (url, bus):
        now = [1_000.0]
        bus.admission_delays[1] = 1.0
        client = await _owned_client(
            url, credential=_claim(expires_at=2_000), clock=lambda: now[0]
        )
        await bus.sio.disconnect(bus.sids[0])
        await _until(lambda: not client.connected)
        assert client.transport_recovering is True

        now[0] = 3_000.0
        assert client.transport_recovering is False

        now[0] = 1_000.0
        await client.close()
        assert client.transport_recovering is False


def test_a_custom_socket_factory_is_never_recovering() -> None:
    client = FederatedDataBusClient(
        platform_url="https://platform.example",
        credential=_claim(),
        socket_factory=lambda: type("S", (), {"on": lambda self, *a: None, "connected": False})(),
    )
    assert client.transport_recovering is False


# -- I: a reconnect handshake says where its time went ----------------------


_CARD_RESOURCE = "https://platform.example/api/integrations/bundles/demo-tenant/demo-project/problem-board@1-0/public/mcp/problem_board"


def _card(bearer: str) -> DelegatedCardCredential:
    return DelegatedCardCredential(
        tenant="demo-tenant",
        project="demo-project",
        bundle_id="problem-board@1-0",
        resource=_CARD_RESOURCE,
        bearer_token=bearer,
    )


@pytest.mark.asyncio
async def test_a_reconnect_handshake_logs_transport_and_bearer_time_apart(caplog) -> None:
    async with _bus_server() as (url, bus):
        asked: list[HandshakeAttempt] = []

        async def slow_source(attempt: HandshakeAttempt) -> DelegatedCardCredential:
            asked.append(attempt)
            await asyncio.sleep(0.25)
            return _card("bearer-canary-2")

        client = await _owned_client(
            url, credential=_card("bearer-canary-1"), credential_source=slow_source
        )
        with caplog.at_level(logging.INFO, logger="app_foundation.data_bus.client"):
            await bus.sio.disconnect(bus.sids[0])
            await _until(lambda: not client.connected)
            assert await client.wait_until_connected(3.0) is True
        await client.close()

        (line,) = [
            record.getMessage()
            for record in caplog.records
            if "event=handshake " in record.getMessage()
        ]
        fields = dict(
            part.split("=", 1) for part in line.split() if "=" in part
        )
        assert float(fields["bearer_resolve_seconds"]) >= 0.25
        assert 0.0 <= float(fields["transport_open_seconds"]) < 0.25
        assert float(fields["since_disconnect_seconds"]) >= float(
            fields["transport_open_seconds"]
        )
        assert len(asked) == 1
        assert "bearer-canary" not in caplog.text

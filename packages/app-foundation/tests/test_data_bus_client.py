from __future__ import annotations

import asyncio
import inspect
from typing import Any, Callable

import pytest
from socketio.exceptions import TimeoutError as SocketIOTimeoutError

from app_foundation.data_bus import (
    DelegatedCardCredential,
    DataBusClaim,
    DataBusClientError,
    DataBusIngressRejected,
    DataBusOutcomeUnknown,
    FederatedDataBusClient,
    HandshakeAttempt,
)


async def _resolve_auth(auth: Any) -> Any:
    """What python-socketio does with ``auth`` before every namespace handshake
    (its ``_get_real_value``): a callable is called, a coroutine function awaited."""

    if not callable(auth):
        return auth
    if inspect.iscoroutinefunction(auth):
        return await auth()
    return auth()


class _Socket:
    def __init__(self) -> None:
        self.connected = False
        self.sid = "engineio-1"
        self.namespace_sid = "socketio-1"
        self.handlers: dict[str, Any] = {}
        self.ack: dict[str, Any] = {}
        self.terminal: dict[str, Any] | None = None
        self.calls: list[tuple[str, dict[str, Any], float]] = []
        self.connect_args: tuple[Any, ...] | None = None
        self.connect_kwargs: dict[str, Any] = {}
        # The auth exactly as the client handed it over (a coroutine function),
        # kept the way python-socketio keeps ``connection_auth`` for reconnects.
        self.connect_auth: Any = None
        self.connect_calls = 0
        # Every auth payload a handshake presented, first connect included.
        self.presented: list[dict[str, Any]] = []
        # The server's answer to a presented payload: a refusal to hand to
        # connect_error, or None to accept. None means accept everything.
        self.refuse: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None
        self.shutdown_calls = 0

    def on(self, event: str, handler: Any) -> None:
        self.handlers[event] = handler

    def get_sid(self) -> str:
        return self.namespace_sid

    async def connect(self, *args: Any, **kwargs: Any) -> None:
        self.connect_args = args
        self.connect_calls += 1
        self.connect_auth = kwargs.get("auth")
        auth = await _resolve_auth(self.connect_auth)
        self.connect_kwargs = {**kwargs, "auth": auth}
        self.presented.append(dict(auth or {}))
        self.connected = True
        await self.handlers["connect"]()

    async def reconnect(self) -> bool:
        """One reconnect handshake, the way python-socketio runs it: the stored
        auth is resolved again and presented, and the server either accepts
        (connect) or refuses (connect_error). True when it connected."""

        auth = await _resolve_auth(self.connect_auth)
        self.presented.append(dict(auth or {}))
        refusal = self.refuse(auth) if self.refuse is not None else None
        if refusal is not None:
            await self.handlers["connect_error"](refusal)
            return False
        self.namespace_sid = f"socketio-{len(self.presented)}"
        self.connected = True
        await self.handlers["connect"]()
        return True

    async def disconnect(self) -> None:
        self.connected = False
        await self.handlers["disconnect"]("client disconnect")

    async def shutdown(self) -> None:
        self.shutdown_calls += 1
        if self.connected:
            await self.disconnect()

    async def call(self, event: str, data: dict[str, Any], timeout: float) -> dict[str, Any]:
        self.calls.append((event, data, timeout))
        if self.terminal is not None:
            terminal = dict(self.terminal)
            terminal["data"] = dict(terminal.get("data") or {})
            terminal["data"]["message_id"] = data["messages"][0]["message_id"]
            await self.handlers["chat_service"](terminal)
        return dict(self.ack)

    async def emit_service(self, payload: dict[str, Any]) -> None:
        await self.handlers["chat_service"](payload)


def _claim() -> DataBusClaim:
    return DataBusClaim(
        tenant="tenant-a",
        project="project-a",
        bundle_id="problem-board@1-0",
        session_id="session-a",
        expires_at=2_000_000_000,
        federated_token="secret-token",
        partition_ref="work:worker-stream:abc",
    )


async def _client(
    socket: _Socket,
    *,
    outcome_timeout: float = 0.1,
    lifecycle_labels: dict[str, str | int | bool] | None = None,
) -> FederatedDataBusClient:
    client = FederatedDataBusClient(
        platform_url="https://platform.example",
        credential=_claim(),
        socket_factory=lambda: socket,
        outcome_timeout_seconds=outcome_timeout,
        lifecycle_labels=lifecycle_labels,
    )
    await client.connect()
    return client


def test_claim_repr_does_not_disclose_token() -> None:
    assert "secret-token" not in repr(_claim())


@pytest.mark.asyncio
async def test_request_separates_ingress_ack_from_correlated_terminal_result() -> None:
    socket = _Socket()
    socket.ack = {"status": "accepted", "accepted": [{"message_id": "message-1"}]}
    socket.terminal = {
        "type": "kdcube.data_bus.result",
        "data": {
            "subject": "problem_board.command.v1",
            "object_ref": "work:worker-stream:abc",
            "data": {"ok": True, "object": {"worker_name": "codex-a"}},
        },
    }
    client = await _client(socket)

    outcome = await client.request(
        subject="problem_board.command.v1",
        object_ref="work:worker-stream:abc",
        payload={"operation": "worker.heartbeat"},
        idempotency_key="heartbeat-1",
        message_id="message-1",
    )

    assert outcome.status == "ok"
    assert outcome.data["object"]["worker_name"] == "codex-a"
    assert socket.calls[0][1]["schema"] == "kdcube.data_bus.ingress.v1"
    assert await client.wait_for_event(0.01) is None
    await client.close()


@pytest.mark.asyncio
async def test_connection_lifecycle_names_each_generation_and_socket_id(caplog) -> None:
    socket = _Socket()
    with caplog.at_level("INFO", logger="app_foundation.data_bus.client"):
        client = await _client(
            socket,
            lifecycle_labels={
                "worker_name": "codex-session",
                "channel_identity": "codex:session-id",
                "replacement_epoch": 2,
            },
        )
        await socket.handlers["disconnect"]("transport error")
        socket.namespace_sid = "socketio-2"
        socket.connected = True
        await socket.handlers["connect"]()
        await socket.handlers["connect_error"](
            {"error_type": "invalid_bearer", "status": 401}
        )
        await client.close()

    assert client.connection_generation == 2
    assert client.socket_id == "socketio-2"
    assert "event=connected connection_generation=1 socket_id=socketio-1" in caplog.text
    assert (
        "event=disconnected connection_generation=1 socket_id=socketio-1 "
        "reason=transport error"
    ) in caplog.text
    assert (
        "event=reconnected connection_generation=2 socket_id=socketio-2 "
        "previous_generation=1 previous_socket_id=socketio-1"
    ) in caplog.text
    assert "event=reconnect_refused attempted_generation=3" in caplog.text
    assert (
        "current_generation=2 current_socket_id=socketio-2 "
        "connection_active=true generation_replaced=false"
    ) in caplog.text
    assert "reason=error_type=invalid_bearer status=401" in caplog.text
    assert caplog.text.count('channel_identity="codex:session-id"') == 6
    assert caplog.text.count("replacement_epoch=2") == 6
    assert caplog.text.count('worker_name="codex-session"') == 6
    assert "event=closed connection_generation=2 socket_id=socketio-2" in caplog.text

    event = await client.wait_for_event(0.1)
    assert event == {
        "type": "app_foundation.data_bus.disconnected",
        "timestamp": event["timestamp"],
        "connection_generation": 1,
        "socket_id": "socketio-1",
        "connection_active": False,
        "reason": "transport error",
    }


class _ReconnectingSocket(_Socket):
    def __init__(self) -> None:
        super().__init__()
        self.reconnect_attempts = 0
        self._stop_reconnecting = asyncio.Event()
        self.reconnect_task: asyncio.Task[None] | None = None

    async def start_reconnecting(self) -> None:
        self.connected = False
        await self.handlers["disconnect"]("transport error")

        async def reconnect() -> None:
            while not self._stop_reconnecting.is_set():
                self.reconnect_attempts += 1
                await self.handlers["connect_error"]("connection rejected")
                try:
                    await asyncio.wait_for(self._stop_reconnecting.wait(), timeout=0.001)
                except asyncio.TimeoutError:
                    pass

        self.reconnect_task = asyncio.create_task(reconnect())
        while self.reconnect_attempts == 0:
            await asyncio.sleep(0)

    async def shutdown(self) -> None:
        self.shutdown_calls += 1
        self._stop_reconnecting.set()
        if self.reconnect_task is not None:
            await self.reconnect_task


@pytest.mark.asyncio
async def test_close_awaits_a_disconnected_socket_reconnect_task() -> None:
    socket = _ReconnectingSocket()
    client = await _client(socket)
    await socket.start_reconnecting()

    await client.close()
    attempts_after_close = socket.reconnect_attempts
    await asyncio.sleep(0.005)

    assert socket.shutdown_calls == 1
    assert socket.reconnect_task is not None
    assert socket.reconnect_task.done()
    assert socket.reconnect_attempts == attempts_after_close


@pytest.mark.asyncio
async def test_request_reports_ingress_rejection_without_waiting_for_outcome() -> None:
    socket = _Socket()
    socket.ack = {
        "status": "rejected",
        "accepted": [],
        "rejected": [{"message_id": "message-1", "error": "subject denied"}],
    }
    client = await _client(socket)

    with pytest.raises(DataBusIngressRejected, match="subject denied"):
        await client.request(
            subject="problem_board.command.v1",
            object_ref="work:worker-stream:abc",
            payload={},
            idempotency_key="request-1",
            message_id="message-1",
        )
    await client.close()


@pytest.mark.asyncio
async def test_ingress_rejection_exposes_structured_status_to_domain_adapter() -> None:
    socket = _Socket()
    socket.ack = {
        "status": "rejected",
        "accepted": [],
        "rejected": [
            {
                "message_id": "message-1",
                "error": "claim expired",
                "error_type": "federated_token_expired",
                "status": 401,
            }
        ],
    }
    client = await _client(socket)

    with pytest.raises(DataBusIngressRejected) as captured:
        await client.request(
            subject="problem_board.command.v1",
            object_ref="work:worker-stream:abc",
            payload={},
            idempotency_key="request-1",
            message_id="message-1",
        )

    assert captured.value.details["error_type"] == "federated_token_expired"
    assert captured.value.details["status"] == 401
    assert captured.value.details["connection_generation"] == 1
    assert captured.value.details["socket_id"] == "socketio-1"
    assert captured.value.details["connection_active"] is True
    await client.close()


@pytest.mark.asyncio
async def test_expired_claim_never_opens_a_socket() -> None:
    socket = _Socket()
    client = FederatedDataBusClient(
        platform_url="https://platform.example",
        credential=_claim(),
        socket_factory=lambda: socket,
        clock=lambda: 2_000_000_001,
    )

    with pytest.raises(DataBusClientError, match="claim is expired") as captured:
        await client.connect()

    assert captured.value.code == "data_bus_claim_expired"
    assert socket.connect_args is None


@pytest.mark.asyncio
async def test_legacy_claim_keyword_still_opens_a_federated_session() -> None:
    socket = _Socket()
    claim = _claim()
    client = FederatedDataBusClient(
        platform_url="https://platform.example",
        claim=claim,
        socket_factory=lambda: socket,
    )

    await client.connect()

    assert client.claim is claim
    assert socket.connect_kwargs["auth"]["federated_token"] == claim.federated_token


def test_client_rejects_ambiguous_or_missing_credentials() -> None:
    claim = _claim()
    with pytest.raises(ValueError, match="either credential or claim"):
        FederatedDataBusClient(
            platform_url="https://platform.example",
            credential=claim,
            claim=claim,
        )
    with pytest.raises(ValueError, match="credential is required"):
        FederatedDataBusClient(platform_url="https://platform.example")


@pytest.mark.asyncio
async def test_a_delegated_card_is_presented_directly_and_never_expires_client_side() -> None:
    """A card is not swapped for a token, and this side does not judge it.

    A minted token carries its own lifetime, so the client can refuse a dead
    one without a round trip. A card carries none: its current state lives
    server-side and is resolved on every operation, which is what makes
    revoking it take effect instead of waiting for a token to lapse.

    So a card client connects no matter what the clock says, and it presents
    the card under its own key. Not the platform bearer key: a card arriving
    there is not a card but a malformed platform token, and it would fail
    somewhere that never names the real cause.
    """

    socket = _Socket()
    card = DelegatedCardCredential(
        tenant="demo-tenant",
        project="demo-project",
        bundle_id="problem-board@1-0",
        resource=(
            "https://platform.example/api/integrations/bundles/demo-tenant/"
            "demo-project/problem-board@1-0/public/mcp/problem_board"
        ),
        bearer_token="card-bearer-value",
    )
    client = FederatedDataBusClient(
        platform_url="https://platform.example",
        credential=card,
        socket_factory=lambda: socket,
        clock=lambda: 2_000_000_001,
    )

    await client.connect()

    auth = socket.connect_kwargs["auth"]
    assert auth["delegated_bearer_token"] == "card-bearer-value"
    assert auth["delegated_resource"] == card.resource
    assert auth["client_role"] == "service"
    assert "federated_token" not in auth
    assert "bearer_token" not in auth
    assert "authorization" not in auth


@pytest.mark.asyncio
async def test_accepted_request_without_terminal_reply_is_outcome_unknown() -> None:
    socket = _Socket()
    socket.ack = {"status": "accepted", "accepted": [{"message_id": "message-1"}]}
    client = await _client(socket, outcome_timeout=0.01)

    with pytest.raises(DataBusOutcomeUnknown) as captured:
        await client.request(
            subject="problem_board.command.v1",
            object_ref="work:worker-stream:abc",
            payload={},
            idempotency_key="request-1",
            message_id="message-1",
        )

    assert captured.value.accepted is True
    assert captured.value.message_id == "message-1"
    await client.close()


@pytest.mark.asyncio
async def test_socketio_ack_timeout_preserves_already_received_terminal_result() -> None:
    class TimedOutAckSocket(_Socket):
        async def call(self, event: str, data: dict[str, Any], timeout: float) -> dict[str, Any]:
            await super().call(event, data, timeout)
            raise SocketIOTimeoutError()

    socket = TimedOutAckSocket()
    socket.terminal = {
        "type": "kdcube.data_bus.result",
        "data": {"data": {"ok": True}},
    }
    client = await _client(socket)

    outcome = await client.request(
        subject="problem_board.command.v1",
        object_ref="work:worker-stream:abc",
        payload={},
        idempotency_key="request-1",
        message_id="message-1",
    )

    assert outcome.status == "ok"
    assert outcome.message_id == "message-1"
    assert client._pending == {}
    await client.close()


@pytest.mark.asyncio
async def test_socketio_ack_timeout_without_result_is_outcome_unknown() -> None:
    class TimedOutAckSocket(_Socket):
        async def call(self, event: str, data: dict[str, Any], timeout: float) -> dict[str, Any]:
            self.calls.append((event, data, timeout))
            raise SocketIOTimeoutError()

    socket = TimedOutAckSocket()
    client = await _client(socket)

    with pytest.raises(DataBusOutcomeUnknown) as captured:
        await client.request(
            subject="problem_board.command.v1",
            object_ref="work:worker-stream:abc",
            payload={},
            idempotency_key="request-1",
            message_id="message-1",
        )

    assert captured.value.message_id == "message-1"
    assert captured.value.accepted is False
    assert captured.value.details["connection_generation"] == 1
    assert captured.value.details["socket_id"] == "socketio-1"
    assert captured.value.details["connection_active"] is True
    assert client._pending == {}
    await client.close()


@pytest.mark.asyncio
async def test_unsolicited_service_event_wakes_event_waiter() -> None:
    socket = _Socket()
    client = await _client(socket)
    event = {
        "type": "problem_board.worker.event.v1",
        "data": {"kind": "control.available", "control_ref": "work:control:1"},
    }

    await socket.emit_service(event)

    assert await client.wait_for_event(0.1) == event
    await client.close()


@pytest.mark.asyncio
async def test_another_peers_data_bus_reply_does_not_wake_event_waiter() -> None:
    socket = _Socket()
    client = await _client(socket)

    await socket.emit_service(
        {
            "type": "kdcube.data_bus.result",
            "data": {
                "message_id": "another-peers-message",
                "subject": "problem_board.command.v1",
                "object_ref": "work:worker-stream:other",
                "data": {"ok": True},
            },
        }
    )
    await socket.emit_service(
        {
            "type": "kdcube.data_bus.accepted",
            "data": {"message_id": "another-peers-message"},
        }
    )

    assert await client.wait_for_event(0.01) is None
    await client.close()


class _RefusingSocket(_Socket):
    """A server that answers the namespace with a refusal, the way python-socketio delivers it."""

    def __init__(self, refusal: Any) -> None:
        super().__init__()
        self.refusal = refusal

    async def connect(self, *args: Any, **kwargs: Any) -> None:
        from socketio.exceptions import ConnectionError as SocketIOConnectionError

        self.connect_args = args
        self.connect_kwargs = dict(kwargs)
        await self.handlers["connect_error"](self.refusal)
        raise SocketIOConnectionError("One or more namespaces failed to connect: /")


class _UnreachableSocket(_Socket):
    async def connect(self, *args: Any, **kwargs: Any) -> None:
        from socketio.exceptions import ConnectionError as SocketIOConnectionError

        raise SocketIOConnectionError("Connection refused by the server")


@pytest.mark.asyncio
async def test_a_namespace_refusal_is_raised_as_ingress_rejected_with_the_servers_code() -> None:
    # A connect handler that raises ConnectionRefusedError(message, {"code": ...}).
    socket = _RefusingSocket(
        {"message": "delegated Card bearer was not accepted", "data": {"code": "delegated_card_bearer_rejected"}}
    )
    client = FederatedDataBusClient(
        platform_url="https://platform.example", credential=_claim(), socket_factory=lambda: socket
    )

    with pytest.raises(DataBusIngressRejected) as refusal:
        await client.connect()

    assert refusal.value.code == "delegated_card_bearer_rejected"
    assert refusal.value.message == "delegated Card bearer was not accepted"
    assert refusal.value.details["code"] == "delegated_card_bearer_rejected"
    assert not client.connected


@pytest.mark.asyncio
async def test_a_bare_server_rejection_is_still_ingress_rejected_without_a_code() -> None:
    # A connect handler that returns False: python-socketio sends only a message.
    socket = _RefusingSocket({"message": "Connection rejected by server"})
    client = FederatedDataBusClient(
        platform_url="https://platform.example", credential=_claim(), socket_factory=lambda: socket
    )

    with pytest.raises(DataBusIngressRejected) as refusal:
        await client.connect()

    assert refusal.value.code == "data_bus_connect_refused"
    assert refusal.value.message == "Connection rejected by server"


@pytest.mark.asyncio
async def test_a_transport_failure_keeps_its_own_exception() -> None:
    # No connect_error arrived, so nothing here claims the server refused anything.
    from socketio.exceptions import ConnectionError as SocketIOConnectionError

    socket = _UnreachableSocket()
    client = FederatedDataBusClient(
        platform_url="https://platform.example", credential=_claim(), socket_factory=lambda: socket
    )

    with pytest.raises(SocketIOConnectionError):
        await client.connect()


@pytest.mark.asyncio
async def test_a_refusal_from_an_earlier_attempt_does_not_leak_into_a_later_one() -> None:
    socket = _Socket()
    client = FederatedDataBusClient(
        platform_url="https://platform.example", credential=_claim(), socket_factory=lambda: socket
    )
    await socket.handlers["connect_error"]({"message": "stale"})

    await client.connect()

    assert client.connected
    await client.close()


# -- reconnect handshakes present the credential valid at reconnect time ------
#
# A relay's socket lived up to nine hours before a transport drop, and its
# delegated bearer lives one hour. The Socket.IO client reconnected with the
# handshake auth it captured at first connect, so the server refused the
# expired bearer on every attempt (2239 refused reconnects against 30 that
# worked, on one host) and the session only returned when the relay tore the
# client down and opened a new one minutes later.

_CARD_RESOURCE = (
    "https://platform.example/api/integrations/bundles/demo-tenant/"
    "demo-project/problem-board@1-0/public/mcp/problem_board"
)


def _card(bearer: str, *, bundle_id: str = "problem-board@1-0") -> DelegatedCardCredential:
    return DelegatedCardCredential(
        tenant="demo-tenant",
        project="demo-project",
        bundle_id=bundle_id,
        resource=_CARD_RESOURCE,
        bearer_token=bearer,
    )


def _refuses_expired(expired: set[str]) -> Callable[[dict[str, Any]], dict[str, Any] | None]:
    """The server side of the handshake: a bearer in ``expired`` is refused the
    way chat-ingress refuses one (``delegated bearer refused reason=token_expired``)."""

    def refuse(auth: dict[str, Any]) -> dict[str, Any] | None:
        if auth.get("delegated_bearer_token") in expired:
            return {
                "error_type": "invalid_bearer",
                "status": 401,
                "message": "delegated bearer refused reason=token_expired",
            }
        return None

    return refuse


def _bearer(payload: dict[str, Any]) -> str:
    return str(payload.get("delegated_bearer_token"))


@pytest.mark.asyncio
async def test_a_reconnect_presents_the_credential_valid_at_reconnect_time(caplog) -> None:
    socket = _Socket()
    expired: set[str] = set()
    socket.refuse = _refuses_expired(expired)
    asked: list[HandshakeAttempt] = []

    async def source(attempt: HandshakeAttempt) -> DelegatedCardCredential:
        asked.append(attempt)
        return _card("bearer-2")

    client = FederatedDataBusClient(
        platform_url="https://platform.example",
        credential=_card("bearer-1"),
        credential_source=source,
        socket_factory=lambda: socket,
    )
    with caplog.at_level("INFO", logger="app_foundation.data_bus.client"):
        await client.connect()
        assert _bearer(socket.presented[0]) == "bearer-1", "the first handshake presents the given credential"
        assert asked == [], "the source is not asked for the first handshake"

        # The bearer lapses while the socket is up, then the transport drops.
        expired.add("bearer-1")
        await socket.handlers["disconnect"]("transport error")
        assert not client.connected

        assert await socket.reconnect() is True

    assert client.connected
    assert client.connection_generation == 2
    assert _bearer(socket.presented[1]) == "bearer-2"
    assert socket.connect_calls == 1, "the same client and socket carried on: no full reopen"
    assert asked == [HandshakeAttempt(connection_generation=1, attempt=1, previous_refusal=None)]
    assert client.credential.bearer_token == "bearer-2"
    assert (
        "event=handshake attempt=1 connection_generation=1 credential=resolved after_refusal=false"
        in caplog.text
    )
    assert "event=reconnected connection_generation=2" in caplog.text
    await client.close()


@pytest.mark.asyncio
async def test_without_a_source_the_reconnect_presents_the_captured_credential_and_is_refused() -> None:
    # The failure this exists for, pinned: no source, the captured bearer again.
    socket = _Socket()
    expired: set[str] = set()
    socket.refuse = _refuses_expired(expired)
    client = FederatedDataBusClient(
        platform_url="https://platform.example",
        credential=_card("bearer-1"),
        socket_factory=lambda: socket,
    )
    await client.connect()
    expired.add("bearer-1")
    await socket.handlers["disconnect"]("transport error")

    assert await socket.reconnect() is False

    assert not client.connected
    assert _bearer(socket.presented[1]) == "bearer-1"
    await client.close()


@pytest.mark.asyncio
async def test_a_refused_reconnect_is_told_to_the_source_on_the_next_handshake() -> None:
    socket = _Socket()
    expired: set[str] = set()
    socket.refuse = _refuses_expired(expired)
    asked: list[HandshakeAttempt] = []
    bearers = iter(["bearer-2", "bearer-3", "bearer-4"])

    async def source(attempt: HandshakeAttempt) -> DelegatedCardCredential:
        asked.append(attempt)
        return _card(next(bearers))

    client = FederatedDataBusClient(
        platform_url="https://platform.example",
        credential=_card("bearer-1"),
        credential_source=source,
        socket_factory=lambda: socket,
    )
    await client.connect()
    # The server no longer holds the session behind bearer-2 either (a store
    # restart): the first reconnect is refused, and the source learns it.
    expired.update({"bearer-1", "bearer-2"})
    await socket.handlers["disconnect"]("transport error")

    assert await socket.reconnect() is False
    assert await socket.reconnect() is True

    assert [attempt.attempt for attempt in asked] == [1, 2]
    assert asked[0].previous_refusal is None
    assert asked[1] == HandshakeAttempt(
        connection_generation=1,
        attempt=2,
        previous_refusal={
            "message": "delegated bearer refused reason=token_expired",
            "code": "invalid_bearer",
            "status": 401,
        },
    )
    assert client.connection_generation == 2

    # A completed connection ends the episode: the next drop starts at
    # attempt 1 with no refusal carried over.
    await socket.handlers["disconnect"]("transport error")
    assert await socket.reconnect() is True
    assert asked[2] == HandshakeAttempt(connection_generation=2, attempt=1, previous_refusal=None)
    await client.close()


@pytest.mark.asyncio
async def test_a_failing_source_leaves_the_reconnect_to_the_previous_credential(caplog) -> None:
    # The token endpoint cannot be reached: the reconnect still happens, with
    # the credential this side already holds, and the log names the fallback.
    socket = _Socket()
    socket.refuse = _refuses_expired(set())

    async def source(attempt: HandshakeAttempt) -> DelegatedCardCredential:
        raise RuntimeError("token endpoint unreachable")

    client = FederatedDataBusClient(
        platform_url="https://platform.example",
        credential=_card("bearer-1"),
        credential_source=source,
        socket_factory=lambda: socket,
    )
    with caplog.at_level("INFO", logger="app_foundation.data_bus.client"):
        await client.connect()
        await socket.handlers["disconnect"]("transport error")
        assert await socket.reconnect() is True

    assert _bearer(socket.presented[1]) == "bearer-1"
    assert "event=handshake_credential_unavailable attempt=1 connection_generation=1" in caplog.text
    assert "credential=previous" in caplog.text
    assert "token endpoint unreachable" in caplog.text
    await client.close()


@pytest.mark.asyncio
async def test_a_source_cannot_move_the_session_to_another_bundle(caplog) -> None:
    socket = _Socket()

    async def source(attempt: HandshakeAttempt) -> DelegatedCardCredential:
        return _card("bearer-2", bundle_id="other-app@1-0")

    client = FederatedDataBusClient(
        platform_url="https://platform.example",
        credential=_card("bearer-1"),
        credential_source=source,
        socket_factory=lambda: socket,
    )
    with caplog.at_level("WARNING", logger="app_foundation.data_bus.client"):
        await client.connect()
        await socket.handlers["disconnect"]("transport error")
        assert await socket.reconnect() is True

    assert _bearer(socket.presented[1]) == "bearer-1"
    assert client.credential.bundle_id == "problem-board@1-0"
    assert "changed bundle_id for an open session" in caplog.text
    await client.close()


@pytest.mark.asyncio
async def test_wait_until_connected_waits_for_the_sockets_own_reconnect() -> None:
    socket = _Socket()
    client = await _client(socket)
    await socket.handlers["disconnect"]("transport error")
    assert not client.connected

    assert await client.wait_until_connected(0.01) is False

    async def reconnect_soon() -> None:
        await asyncio.sleep(0.02)
        await socket.reconnect()

    task = asyncio.create_task(reconnect_soon())
    assert await client.wait_until_connected(1.0) is True
    await task
    assert client.connection_generation == 2

    await client.close()
    assert await client.wait_until_connected(0.0) is False


@pytest.mark.asyncio
async def test_the_socket_receives_a_coroutine_function_as_auth() -> None:
    # python-socketio resolves a callable auth before every namespace
    # handshake and awaits a coroutine function (``_get_real_value``), which is
    # what lets a reconnect present a different credential than the first
    # connect did. A payload dict would be captured once and replayed.
    socket = _Socket()
    client = await _client(socket)

    assert inspect.iscoroutinefunction(socket.connect_auth)
    assert socket.connect_kwargs["auth"]["federated_token"] == "secret-token"
    await client.close()


def test_the_default_socket_reconnects_on_its_own_and_caps_the_delay_at_ten_seconds() -> None:
    from app_foundation.data_bus.client import _default_socket_factory

    socket = _default_socket_factory()

    assert socket.reconnection is True
    assert socket.reconnection_attempts == 0
    assert socket.reconnection_delay == 1
    assert socket.reconnection_delay_max == 10

from __future__ import annotations

from typing import Any

import pytest

from app_foundation.data_bus import (
    DelegatedCardCredential,
    DataBusClaim,
    DataBusClientError,
    DataBusIngressRejected,
    DataBusOutcomeUnknown,
    FederatedDataBusClient,
)


class _Socket:
    def __init__(self) -> None:
        self.connected = False
        self.handlers: dict[str, Any] = {}
        self.ack: dict[str, Any] = {}
        self.terminal: dict[str, Any] | None = None
        self.calls: list[tuple[str, dict[str, Any], float]] = []
        self.connect_args: tuple[Any, ...] | None = None
        self.connect_kwargs: dict[str, Any] = {}

    def on(self, event: str, handler: Any) -> None:
        self.handlers[event] = handler

    async def connect(self, *args: Any, **kwargs: Any) -> None:
        self.connect_args = args
        self.connect_kwargs = dict(kwargs)
        self.connected = True
        await self.handlers["connect"]()

    async def disconnect(self) -> None:
        self.connected = False
        await self.handlers["disconnect"]("client disconnect")

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
    socket: _Socket, *, outcome_timeout: float = 0.1
) -> FederatedDataBusClient:
    client = FederatedDataBusClient(
        platform_url="https://platform.example",
        credential=_claim(),
        socket_factory=lambda: socket,
        outcome_timeout_seconds=outcome_timeout,
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

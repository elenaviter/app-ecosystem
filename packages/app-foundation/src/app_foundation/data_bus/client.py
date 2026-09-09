"""Asynchronous request/reply client for a federated KDCube Data Bus session."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping


SocketFactory = Callable[[], Any]


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


@dataclass(frozen=True, slots=True)
class DataBusClaim:
    tenant: str
    project: str
    bundle_id: str
    session_id: str
    expires_at: int
    federated_token: str = field(repr=False)
    partition_ref: str = ""

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "DataBusClaim":
        claim = _mapping(value)
        required = {
            "tenant": str(claim.get("tenant") or "").strip(),
            "project": str(claim.get("project") or "").strip(),
            "bundle_id": str(claim.get("bundle_id") or "").strip(),
            "session_id": str(claim.get("session_id") or "").strip(),
            "federated_token": str(claim.get("federated_token") or "").strip(),
        }
        missing = sorted(key for key, item in required.items() if not item)
        if missing:
            raise ValueError(
                "Federated Data Bus claim is missing: " + ", ".join(missing)
            )
        try:
            expires_at = int(claim.get("expires_at") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("Federated Data Bus claim expiry is invalid.") from exc
        if expires_at <= 0:
            raise ValueError("Federated Data Bus claim expiry is invalid.")
        return cls(
            tenant=required["tenant"],
            project=required["project"],
            bundle_id=required["bundle_id"],
            session_id=required["session_id"],
            expires_at=expires_at,
            federated_token=required["federated_token"],
            partition_ref=str(claim.get("partition_ref") or "").strip(),
        )


@dataclass(frozen=True, slots=True)
class DataBusOutcome:
    message_id: str
    status: str
    subject: str
    object_ref: str
    data: dict[str, Any]
    error: dict[str, Any] | None
    envelope: dict[str, Any]


class DataBusClientError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.details = dict(details or {})


class DataBusIngressRejected(DataBusClientError):
    pass


class DataBusRemoteError(DataBusClientError):
    pass


class DataBusOutcomeUnknown(DataBusClientError):
    def __init__(self, *, message_id: str, accepted: bool) -> None:
        super().__init__(
            "data_bus_outcome_unknown",
            "The Data Bus operation did not return a terminal result before the timeout.",
            details={"message_id": message_id, "accepted": bool(accepted)},
        )
        self.message_id = message_id
        self.accepted = bool(accepted)


def _default_socket_factory() -> Any:
    try:
        import socketio
    except ImportError as exc:  # pragma: no cover - depends on selected package extra
        raise RuntimeError(
            "Install app-foundation with the data-bus extra to use FederatedDataBusClient."
        ) from exc
    return socketio.AsyncClient(
        reconnection=True,
        reconnection_attempts=0,
        reconnection_delay=1,
        reconnection_delay_max=30,
        logger=False,
        engineio_logger=False,
    )


class FederatedDataBusClient:
    """One bundle-scoped Socket.IO session with correlated terminal replies."""

    def __init__(
        self,
        *,
        platform_url: str,
        claim: DataBusClaim,
        socket_factory: SocketFactory | None = None,
        ingress_timeout_seconds: float = 15.0,
        outcome_timeout_seconds: float = 60.0,
        event_queue_size: int = 256,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.platform_url = str(platform_url or "").rstrip("/")
        if not self.platform_url:
            raise ValueError("platform_url is required")
        self.claim = claim
        self.socket = (socket_factory or _default_socket_factory)()
        self.ingress_timeout_seconds = max(0.1, float(ingress_timeout_seconds))
        self.outcome_timeout_seconds = max(0.1, float(outcome_timeout_seconds))
        self._clock = clock
        self._pending: dict[str, asyncio.Future[DataBusOutcome]] = {}
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=max(1, int(event_queue_size))
        )
        self._connected = asyncio.Event()
        self._closed = False
        self.socket.on("connect", self._on_connect)
        self.socket.on("disconnect", self._on_disconnect)
        self.socket.on("chat_service", self._on_service_event)

    @property
    def connected(self) -> bool:
        return bool(
            self.claim.expires_at > int(self._clock())
            and self._connected.is_set()
            and getattr(self.socket, "connected", True)
        )

    async def _on_connect(self) -> None:
        self._connected.set()

    async def _on_disconnect(self, *args: Any) -> None:
        del args
        self._connected.clear()
        self._queue_event(
            {
                "type": "app_foundation.data_bus.disconnected",
                "timestamp": int(time.time()),
            }
        )

    def _queue_event(self, event: Mapping[str, Any]) -> None:
        if self._events.full():
            try:
                self._events.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self._events.put_nowait(dict(event))

    async def _on_service_event(self, payload: Any) -> None:
        envelope = _mapping(payload)
        body = _mapping(envelope.get("data"))
        message_id = str(body.get("message_id") or "").strip()
        event_type = str(envelope.get("type") or "").strip()
        future = self._pending.get(message_id) if message_id else None
        if future is not None and event_type.startswith("kdcube.data_bus."):
            if event_type == "kdcube.data_bus.accepted":
                return
            terminal = _mapping(body.get("data"))
            if event_type == "kdcube.data_bus.error":
                outcome = DataBusOutcome(
                    message_id=message_id,
                    status="error",
                    subject=str(body.get("subject") or ""),
                    object_ref=str(body.get("object_ref") or ""),
                    data={},
                    error=terminal,
                    envelope=envelope,
                )
            else:
                outcome = DataBusOutcome(
                    message_id=message_id,
                    status=(
                        "conflict"
                        if event_type == "kdcube.data_bus.conflict"
                        else "ok"
                    ),
                    subject=str(body.get("subject") or ""),
                    object_ref=str(body.get("object_ref") or ""),
                    data=terminal,
                    error=None,
                    envelope=envelope,
                )
            if not future.done():
                future.set_result(outcome)
            return
        self._queue_event(envelope)

    async def connect(self) -> None:
        if self._closed:
            raise DataBusClientError(
                "data_bus_client_closed", "The Data Bus client is already closed."
            )
        if self.claim.expires_at <= int(self._clock()):
            raise DataBusClientError(
                "data_bus_claim_expired",
                "The federated Data Bus claim is expired.",
                details={"expires_at": self.claim.expires_at},
            )
        await self.socket.connect(
            self.platform_url,
            socketio_path="socket.io",
            transports=["websocket", "polling"],
            auth={
                "tenant": self.claim.tenant,
                "project": self.claim.project,
                "bundle_id": self.claim.bundle_id,
                "federated_token": self.claim.federated_token,
                "client_role": "service",
            },
        )
        self._connected.set()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._connected.clear()
        await self.socket.disconnect()

    async def request(
        self,
        *,
        subject: str,
        object_ref: str,
        payload: Mapping[str, Any],
        idempotency_key: str,
        message_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> DataBusOutcome:
        if not self.connected:
            raise DataBusClientError(
                "data_bus_not_connected", "The Data Bus session is not connected."
            )
        resolved_message_id = str(message_id or uuid.uuid4())
        loop = asyncio.get_running_loop()
        future: asyncio.Future[DataBusOutcome] = loop.create_future()
        self._pending[resolved_message_id] = future
        accepted = False
        try:
            try:
                ack = await self.socket.call(
                    "data_bus.publish",
                    {
                        "schema": "kdcube.data_bus.ingress.v1",
                        "bundle_id": self.claim.bundle_id,
                        "messages": [
                            {
                                "message_id": resolved_message_id,
                                "subject": str(subject),
                                "object_ref": str(object_ref),
                                "idempotency_key": str(idempotency_key),
                                "payload": dict(payload),
                                "client": {"kind": "app-foundation.data-bus"},
                            }
                        ],
                    },
                    timeout=self.ingress_timeout_seconds,
                )
            except (asyncio.TimeoutError, TimeoutError) as exc:
                raise DataBusOutcomeUnknown(
                    message_id=resolved_message_id, accepted=False
                ) from exc
            acknowledgement = _mapping(ack)
            accepted_rows = acknowledgement.get("accepted")
            accepted = bool(
                acknowledgement.get("status") in {"accepted", "partial"}
                and isinstance(accepted_rows, list)
                and any(
                    str(_mapping(row).get("message_id") or "") == resolved_message_id
                    for row in accepted_rows
                )
            )
            if not accepted:
                rejected = acknowledgement.get("rejected")
                details = {
                    "message_id": resolved_message_id,
                    "acknowledgement": acknowledgement,
                }
                message = "The Data Bus ingress rejected the operation package."
                if isinstance(rejected, list) and rejected:
                    first = _mapping(rejected[0])
                    message = str(first.get("error") or message)
                    details["rejection"] = first
                    if first.get("error_type"):
                        details["error_type"] = str(first["error_type"])
                    if first.get("status") is not None:
                        details["status"] = first["status"]
                raise DataBusIngressRejected(
                    "data_bus_ingress_rejected", message, details=details
                )
            try:
                return await asyncio.wait_for(
                    asyncio.shield(future),
                    timeout=(
                        self.outcome_timeout_seconds
                        if timeout_seconds is None
                        else max(0.1, float(timeout_seconds))
                    ),
                )
            except asyncio.TimeoutError as exc:
                raise DataBusOutcomeUnknown(
                    message_id=resolved_message_id, accepted=True
                ) from exc
        finally:
            self._pending.pop(resolved_message_id, None)
            if not future.done():
                future.cancel()

    async def wait_for_event(self, timeout_seconds: float) -> dict[str, Any] | None:
        remaining = float(self.claim.expires_at) - float(self._clock())
        if remaining <= 0:
            return None
        try:
            return await asyncio.wait_for(
                self._events.get(),
                timeout=max(0.1, min(float(timeout_seconds), remaining)),
            )
        except asyncio.TimeoutError:
            return None


__all__ = [
    "DataBusClaim",
    "DataBusClientError",
    "DataBusIngressRejected",
    "DataBusOutcome",
    "DataBusOutcomeUnknown",
    "DataBusRemoteError",
    "FederatedDataBusClient",
]

"""Asynchronous request/reply client for a federated KDCube Data Bus session."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .credentials import DataBusCredential, DelegatedCardCredential


SocketFactory = Callable[[], Any]
logger = logging.getLogger(__name__)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _connection_reason(value: Any) -> str:
    if isinstance(value, Mapping):
        parts = [
            f"{key}={str(value[key])[:160]}"
            for key in ("error_type", "status", "reason", "message", "error")
            if value.get(key) not in (None, "")
        ]
        return " ".join(parts) or "unspecified"
    return str(value or "unspecified").strip()[:512] or "unspecified"


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

    def auth_payload(self) -> dict[str, Any]:
        return {
            "tenant": self.tenant,
            "project": self.project,
            "bundle_id": self.bundle_id,
            "federated_token": self.federated_token,
            "client_role": "service",
        }


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
    def __init__(
        self,
        *,
        message_id: str,
        accepted: bool,
        connection: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(
            "data_bus_outcome_unknown",
            "The Data Bus operation did not return a terminal result before the timeout.",
            details={
                "message_id": message_id,
                "accepted": bool(accepted),
                **dict(connection or {}),
            },
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


def _is_socketio_timeout(error: BaseException) -> bool:
    try:
        from socketio.exceptions import TimeoutError as SocketIOTimeoutError
    except ImportError:  # pragma: no cover - data-bus extra is optional
        return False
    return isinstance(error, SocketIOTimeoutError)


class FederatedDataBusClient:
    """One bundle-scoped Socket.IO session with correlated terminal replies."""

    def __init__(
        self,
        *,
        platform_url: str,
        credential: DataBusCredential | None = None,
        claim: DataBusClaim | None = None,
        socket_factory: SocketFactory | None = None,
        ingress_timeout_seconds: float = 15.0,
        outcome_timeout_seconds: float = 60.0,
        event_queue_size: int = 256,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.platform_url = str(platform_url or "").rstrip("/")
        if not self.platform_url:
            raise ValueError("platform_url is required")
        if credential is not None and claim is not None:
            raise ValueError("provide either credential or claim, not both")
        resolved_credential = credential or claim
        if resolved_credential is None:
            raise ValueError("credential is required")
        self.credential = resolved_credential
        # Kept for callers written against the original federated-only API.
        self.claim = resolved_credential
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
        self._connection_generation = 0
        self._socket_id = ""
        self.socket.on("connect", self._on_connect)
        self.socket.on("connect_error", self._on_connect_error)
        self.socket.on("disconnect", self._on_disconnect)
        self.socket.on("chat_service", self._on_service_event)

    @property
    def _client_side_expiry(self) -> int:
        """Zero means this side has no expiry to check.

        A minted token carries its own lifetime, so the client can refuse to
        use a dead one without a round trip. A delegated card carries none:
        its current state lives server-side and is resolved on every operation,
        so the only honest answer here is to let the call go and be told.
        """
        return int(getattr(self.credential, "expires_at", 0) or 0)

    def _expired(self) -> bool:
        expiry = self._client_side_expiry
        return bool(expiry and expiry <= int(self._clock()))

    @property
    def connected(self) -> bool:
        return bool(
            not self._expired()
            and self._connected.is_set()
            and getattr(self.socket, "connected", True)
        )

    @property
    def connection_generation(self) -> int:
        return self._connection_generation

    @property
    def socket_id(self) -> str:
        return self._socket_id

    def _connection_evidence(self) -> dict[str, Any]:
        return {
            "connection_generation": self._connection_generation,
            "socket_id": self._socket_id,
            "connection_active": self.connected,
        }

    def _current_socket_id(self) -> str:
        get_sid = getattr(self.socket, "get_sid", None)
        if callable(get_sid):
            try:
                value = get_sid()
            except (KeyError, TypeError, ValueError):
                value = ""
            if value:
                return str(value)
        return str(getattr(self.socket, "sid", "") or "")

    async def _on_connect(self) -> None:
        previous_generation = self._connection_generation
        previous_socket_id = self._socket_id
        self._connection_generation += 1
        self._socket_id = self._current_socket_id()
        self._connected.set()
        logger.info(
            "Data Bus socket lifecycle event=%s connection_generation=%d "
            "socket_id=%s previous_generation=%d previous_socket_id=%s",
            "reconnected" if previous_generation else "connected",
            self._connection_generation,
            self._socket_id or "unassigned",
            previous_generation,
            previous_socket_id or "none",
        )

    async def _on_connect_error(self, data: Any = None) -> None:
        logger.warning(
            "Data Bus socket lifecycle event=%s attempted_generation=%d "
            "socket_id=%s current_generation=%d current_socket_id=%s "
            "connection_active=%s generation_replaced=false reason=%s",
            "reconnect_refused" if self._connection_generation else "connect_refused",
            self._connection_generation + 1,
            self._current_socket_id() or "unassigned",
            self._connection_generation,
            self._socket_id or "none",
            str(self.connected).lower(),
            _connection_reason(data),
        )

    async def _on_disconnect(self, *args: Any) -> None:
        self._connected.clear()
        reason = _connection_reason(args[0] if args else None)
        logger.info(
            "Data Bus socket lifecycle event=disconnected connection_generation=%d "
            "socket_id=%s reason=%s",
            self._connection_generation,
            self._socket_id or "unassigned",
            reason,
        )
        self._queue_event(
            {
                "type": "app_foundation.data_bus.disconnected",
                "timestamp": int(time.time()),
                **self._connection_evidence(),
                "reason": reason,
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
        if self._expired():
            if isinstance(self.credential, DataBusClaim):
                code = "data_bus_claim_expired"
                message = "The federated Data Bus claim is expired."
            else:
                code = "data_bus_credential_expired"
                message = "The Data Bus credential is expired."
            raise DataBusClientError(
                code,
                message,
                details={"expires_at": self._client_side_expiry},
            )
        await self.socket.connect(
            self.platform_url,
            socketio_path="socket.io",
            transports=["websocket", "polling"],
            auth=self.credential.auth_payload(),
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
                "data_bus_not_connected",
                "The Data Bus session is not connected.",
                details=self._connection_evidence(),
            )
        resolved_message_id = str(message_id or uuid.uuid4())
        connection = self._connection_evidence()
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
                        "bundle_id": self.credential.bundle_id,
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
            except Exception as exc:
                if (
                    not isinstance(exc, (asyncio.TimeoutError, TimeoutError))
                    and not _is_socketio_timeout(exc)
                ):
                    raise
                if future.done() and not future.cancelled():
                    return future.result()
                raise DataBusOutcomeUnknown(
                    message_id=resolved_message_id,
                    accepted=False,
                    connection=connection,
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
                    **connection,
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
                    message_id=resolved_message_id,
                    accepted=True,
                    connection=connection,
                ) from exc
        finally:
            self._pending.pop(resolved_message_id, None)
            if not future.done():
                future.cancel()

    async def wait_for_event(self, timeout_seconds: float) -> dict[str, Any] | None:
        expiry = self._client_side_expiry
        if expiry:
            remaining = float(expiry) - float(self._clock())
            if remaining <= 0:
                return None
            wait_for = min(float(timeout_seconds), remaining)
        else:
            wait_for = float(timeout_seconds)
        try:
            return await asyncio.wait_for(
                self._events.get(), timeout=max(0.1, wait_for)
            )
        except asyncio.TimeoutError:
            return None


__all__ = [
    "DataBusClaim",
    "DataBusCredential",
    "DataBusClientError",
    "DataBusIngressRejected",
    "DataBusOutcome",
    "DataBusOutcomeUnknown",
    "DataBusRemoteError",
    "DelegatedCardCredential",
    "FederatedDataBusClient",
]

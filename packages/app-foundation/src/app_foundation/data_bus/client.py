"""Asynchronous request/reply client for a federated KDCube Data Bus session."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

from .credentials import DataBusCredential, DelegatedCardCredential


SocketFactory = Callable[[], Any]
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HandshakeAttempt:
    """What the credential source is told before a reconnect handshake.

    ``connection_generation`` counts the connections this client has completed,
    so a reconnect sees at least 1. ``attempt`` counts the handshakes since the
    connection was lost, from 1. ``previous_refusal`` is the server's answer to
    the previous handshake of this episode (``code`` and ``message`` as
    ``_refusal_payload`` shapes them), or None when that handshake failed
    before the server answered, or when this is the episode's first.
    """

    connection_generation: int
    attempt: int
    previous_refusal: dict[str, Any] | None = None


CredentialSource = Callable[
    [HandshakeAttempt], "Awaitable[DataBusCredential] | DataBusCredential"
]


def _normalize_lifecycle_labels(
    value: Mapping[str, str | int | bool] | None,
) -> tuple[tuple[str, str | int | bool], ...]:
    labels: list[tuple[str, str | int | bool]] = []
    for raw_key, raw_value in sorted(dict(value or {}).items()):
        key = str(raw_key or "").strip()
        if (
            not key
            or not key[0].isalpha()
            or any(not (character.isalnum() or character == "_") for character in key)
        ):
            raise ValueError(f"invalid Data Bus lifecycle label: {raw_key!r}")
        if not isinstance(raw_value, (str, int, bool)):
            raise ValueError(f"Data Bus lifecycle label {key!r} must be scalar")
        normalized = raw_value.strip() if isinstance(raw_value, str) else raw_value
        if normalized == "":
            raise ValueError(f"Data Bus lifecycle label {key!r} must not be empty")
        labels.append((key, normalized))
    return tuple(labels)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _refusal_payload(value: Any) -> dict[str, Any]:
    """The server's refusal as a flat mapping: message, and a code when it sent one.

    A connect handler that returns False yields ``{"message": "Connection
    rejected by server"}``. One that raises ``ConnectionRefusedError(message,
    {"code": ...})`` yields ``{"message": ..., "data": {"code": ...}}``. Both
    end here as ``message`` plus ``code``, so a caller branches on the code
    when the server named one and on the refusal itself when it did not.
    """

    payload: dict[str, Any] = {"message": "", "code": ""}
    if isinstance(value, Mapping):
        nested = value.get("data") if isinstance(value.get("data"), Mapping) else {}
        payload["message"] = str(value.get("message") or value.get("error") or "")[:512]
        payload["code"] = str(
            nested.get("code") or value.get("code") or value.get("error_type") or ""
        )
        for source in (value, nested):
            for key in ("reason", "status"):
                if source.get(key) not in (None, ""):
                    payload[key] = source[key]
    elif value not in (None, ""):
        payload["message"] = str(value)[:512]
    return payload


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
    # A dropped socket reconnects on its own. The delay doubles from one
    # second and stops at ten: a channel whose runtime is up is expected back
    # within ten seconds of a transport drop, and the relay that owns the
    # session waits that long before it tears the client down and opens a new
    # one (a full reopen costs a fresh credential and minutes of backoff).
    return socketio.AsyncClient(
        reconnection=True,
        reconnection_attempts=0,
        reconnection_delay=1,
        reconnection_delay_max=10,
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
    """One bundle-scoped Socket.IO session with correlated terminal replies.

    The first handshake presents ``credential``. When the socket drops, the
    Socket.IO client reconnects on its own, and every reconnect handshake asks
    ``credential_source`` for the credential that is valid at that moment. A
    delegated bearer captured at the first connect has usually lapsed by the
    time a long-lived socket drops, so presenting it again is refused as
    expired on every attempt and the session only returns when its owner tears
    the client down and opens a new one minutes later. The source gets a
    :class:`HandshakeAttempt` naming the episode's attempt number and the
    server's refusal of the previous attempt, so it can refresh on its own
    clock, re-mint once after a refusal, and leave a second refusal to the
    owner. Without a source, every handshake presents ``credential``.
    """

    def __init__(
        self,
        *,
        platform_url: str,
        credential: DataBusCredential | None = None,
        claim: DataBusClaim | None = None,
        credential_source: CredentialSource | None = None,
        socket_factory: SocketFactory | None = None,
        ingress_timeout_seconds: float = 15.0,
        outcome_timeout_seconds: float = 60.0,
        event_queue_size: int = 256,
        clock: Callable[[], float] = time.time,
        lifecycle_labels: Mapping[str, str | int | bool] | None = None,
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
        self._credential_source = credential_source
        # Handshakes this client asked the socket to make, over its lifetime;
        # the first presents the given credential, every later one is a
        # reconnect and asks the source. The episode counters restart when a
        # connection completes.
        self._handshakes = 0
        self._episode_attempt = 0
        self._episode_refusal: dict[str, Any] | None = None
        self.socket = (socket_factory or _default_socket_factory)()
        self.ingress_timeout_seconds = max(0.1, float(ingress_timeout_seconds))
        self.outcome_timeout_seconds = max(0.1, float(outcome_timeout_seconds))
        self._clock = clock
        self._pending: dict[str, asyncio.Future[DataBusOutcome]] = {}
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=max(1, int(event_queue_size))
        )
        self._connected = asyncio.Event()
        self._close_lock = asyncio.Lock()
        self._closed = False
        self._connection_generation = 0
        self._socket_id = ""
        # The server's answer when it refuses the namespace: python-socketio
        # delivers it to connect_error and then raises a generic
        # ConnectionError from connect(), so the reason has to be caught here
        # or the caller cannot tell a refused credential from a dead network.
        self._connect_refusal: dict[str, Any] | None = None
        # Labels are diagnostic coordinates supplied by the owning product.
        # They must never contain credentials or other secret material.
        self._lifecycle_labels = _normalize_lifecycle_labels(lifecycle_labels)
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

    def _lifecycle_log_suffix(self) -> str:
        return "".join(
            f" {key}={json.dumps(value, ensure_ascii=True, separators=(',', ':'))}"
            for key, value in self._lifecycle_labels
        )

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
        self._episode_attempt = 0
        self._episode_refusal = None
        self._connected.set()
        logger.info(
            "Data Bus socket lifecycle event=%s connection_generation=%d "
            "socket_id=%s previous_generation=%d previous_socket_id=%s%s",
            "reconnected" if previous_generation else "connected",
            self._connection_generation,
            self._socket_id or "unassigned",
            previous_generation,
            previous_socket_id or "none",
            self._lifecycle_log_suffix(),
        )

    async def _on_connect_error(self, data: Any = None) -> None:
        self._connect_refusal = _refusal_payload(data)
        self._episode_refusal = dict(self._connect_refusal)
        logger.warning(
            "Data Bus socket lifecycle event=%s attempted_generation=%d "
            "socket_id=%s current_generation=%d current_socket_id=%s "
            "connection_active=%s generation_replaced=false reason=%s%s",
            "reconnect_refused" if self._connection_generation else "connect_refused",
            self._connection_generation + 1,
            self._current_socket_id() or "unassigned",
            self._connection_generation,
            self._socket_id or "none",
            str(self.connected).lower(),
            _connection_reason(data),
            self._lifecycle_log_suffix(),
        )

    async def _on_disconnect(self, *args: Any) -> None:
        self._connected.clear()
        reason = _connection_reason(args[0] if args else None)
        logger.info(
            "Data Bus socket lifecycle event=disconnected connection_generation=%d "
            "socket_id=%s reason=%s%s",
            self._connection_generation,
            self._socket_id or "unassigned",
            reason,
            self._lifecycle_log_suffix(),
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
        if event_type.startswith("kdcube.data_bus."):
            # Data Bus receipts fan out to the authenticated session so a
            # reconnecting peer can observe them. They are still replies,
            # never application push events. A client with no matching
            # in-flight request must ignore the receipt instead of waking its
            # host loop for another peer's operation.
            if future is None:
                return
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
        self._connect_refusal = None
        try:
            # The auth is a coroutine function, not a payload: python-socketio
            # resolves a callable once per namespace handshake, on this connect
            # and on every reconnect it runs itself, so each handshake can
            # present the credential that is valid at that moment.
            await self.socket.connect(
                self.platform_url,
                socketio_path="socket.io",
                transports=["websocket", "polling"],
                auth=self._handshake_auth,
            )
        except Exception as exc:
            refusal = self._connect_refusal
            if refusal is None:
                # No connect_error arrived: the transport failed before the
                # server answered. Callers already classify that as transient.
                raise
            raise DataBusIngressRejected(
                str(refusal.get("code") or "data_bus_connect_refused"),
                str(refusal.get("message") or "The Data Bus refused this connection."),
                details=refusal,
            ) from exc
        self._connected.set()

    async def _handshake_auth(self) -> dict[str, Any]:
        """The auth payload for one handshake, resolved when the transport is up.

        The first handshake presents the credential this client was built
        with. Every later one is a reconnect: it asks the credential source,
        when there is one, for the credential valid now, and presents the
        previous credential when the source fails, so a token endpoint that
        cannot be reached does not also end the reconnect.
        """

        self._handshakes += 1
        if self._handshakes == 1 or self._credential_source is None:
            return self.credential.auth_payload()
        self._episode_attempt += 1
        attempt = HandshakeAttempt(
            connection_generation=self._connection_generation,
            attempt=self._episode_attempt,
            previous_refusal=(
                dict(self._episode_refusal) if self._episode_refusal else None
            ),
        )
        presented = "resolved"
        try:
            resolved = self._credential_source(attempt)
            if inspect.isawaitable(resolved):
                resolved = await resolved
            self._adopt_credential(resolved)
        except Exception:  # noqa: BLE001 - the reconnect goes on with what it has
            presented = "previous"
            logger.warning(
                "Data Bus socket lifecycle event=handshake_credential_unavailable "
                "attempt=%d connection_generation=%d: presenting the previous "
                "credential%s",
                attempt.attempt,
                attempt.connection_generation,
                self._lifecycle_log_suffix(),
                exc_info=True,
            )
        logger.info(
            "Data Bus socket lifecycle event=handshake attempt=%d "
            "connection_generation=%d credential=%s after_refusal=%s%s",
            attempt.attempt,
            attempt.connection_generation,
            presented,
            str(attempt.previous_refusal is not None).lower(),
            self._lifecycle_log_suffix(),
        )
        return self.credential.auth_payload()

    def _adopt_credential(self, resolved: Any) -> None:
        """Make ``resolved`` the current credential, or refuse it.

        The source may renew the secret. It may not move the session to
        another tenant, project or bundle: that is a different session, and
        a handshake that presented it would fail somewhere that does not name
        the cause.
        """

        if not isinstance(resolved, DataBusCredential):
            raise TypeError(
                "the credential source returned "
                f"{type(resolved).__name__}, not a Data Bus credential"
            )
        current = self.credential
        for name in ("tenant", "project", "bundle_id"):
            if getattr(resolved, name) != getattr(current, name):
                raise ValueError(
                    f"the credential source changed {name} for an open session"
                )
        self.credential = resolved
        self.claim = resolved

    async def wait_until_connected(self, timeout_seconds: float) -> bool:
        """True when the socket is connected within ``timeout_seconds``.

        A dropped socket reconnects on its own. An owner that would otherwise
        tear this client down and open a new one waits here first, for the
        bound it accepts, and keeps the session when the socket comes back.
        """

        if self.connected:
            return True
        if self._closed:
            return False
        try:
            await asyncio.wait_for(
                self._connected.wait(), timeout=max(0.0, float(timeout_seconds))
            )
        except asyncio.TimeoutError:
            return False
        return self.connected

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._connected.clear()
            shutdown = getattr(self.socket, "shutdown", None)
            if callable(shutdown):
                # python-socketio disconnect() is a no-op while disconnected and
                # leaves its reconnect task alive. shutdown() covers both the
                # connected and reconnecting states and waits for that task to end.
                await shutdown()
            else:  # pragma: no cover - compatibility with older socket factories
                await self.socket.disconnect()
            self._closed = True
            logger.info(
                "Data Bus socket lifecycle event=closed connection_generation=%d "
                "socket_id=%s%s",
                self._connection_generation,
                self._socket_id or "unassigned",
                self._lifecycle_log_suffix(),
            )

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
    "CredentialSource",
    "DataBusClaim",
    "DataBusCredential",
    "DataBusClientError",
    "DataBusIngressRejected",
    "DataBusOutcome",
    "DataBusOutcomeUnknown",
    "DataBusRemoteError",
    "DelegatedCardCredential",
    "FederatedDataBusClient",
    "HandshakeAttempt",
]

"""Asynchronous request/reply client for a federated KDCube Data Bus session."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
import uuid
import weakref
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


def _transport_failed(error: BaseException) -> bool:
    """Whether engine.io failed to open the transport anywhere in this chain.

    python-socketio raises its ConnectionError from engine.io's when the
    WebSocket or polling transport cannot be opened; a namespace refusal from
    the server is raised without one.
    """

    seen: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in seen and len(seen) < 8:
        seen.append(current)
        kind = type(current)
        if kind.__name__ == "ConnectionError" and str(kind.__module__ or "").startswith("engineio"):
            return True
        current = current.__cause__ or current.__context__
    return False


_STATUS_CODE = re.compile(r"\bstatus code (\d{3})\b")


def _failure_chain(error: BaseException) -> str:
    """The exception types of a failure chain, outermost first (W461).

    python-socketio reports every transport failure as "Connection error";
    the chain says whether it was DNS, a refused or reset TCP connection, a
    TLS failure, a timeout or an HTTP status from the ingress.
    """

    seen: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in seen and len(seen) < 8:
        seen.append(current)
        current = current.__cause__ or current.__context__
    return ">".join(f"{type(item).__module__.split('.')[0]}.{type(item).__name__}" for item in seen)


def _failure_facts(error: BaseException) -> str:
    """Safe, allowlisted facts about a failure chain (W461).

    Exception text can carry hosts, headers or response bodies, so none of it
    is copied. What is kept: the first OS error number in the chain, and an
    HTTP status, read from a ``status``/``status_code`` attribute or from
    engine.io's "status code NNN" phrase (only the three digits).
    """

    errno_value = "-"
    status = "-"
    seen: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in seen and len(seen) < 8:
        seen.append(current)
        if errno_value == "-" and isinstance(current, OSError) and isinstance(current.errno, int):
            errno_value = str(current.errno)
        if status == "-":
            for name in ("status", "status_code"):
                value = getattr(current, name, None)
                if isinstance(value, int) and 100 <= value <= 599:
                    status = str(value)
                    break
            else:
                match = _STATUS_CODE.search(" ".join(str(arg) for arg in current.args if isinstance(arg, str)))
                if match:
                    status = match.group(1)
        current = current.__cause__ or current.__context__
    return f"error_errno={errno_value} error_status={status}"

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
    """No terminal result arrived in time; the operation may still have applied.

    ``accepted`` says only whether the ingress acknowledgement arrived. A
    missing acknowledgement is not evidence that the server did not accept or
    apply the operation, so the details call it ``ingress_ack_received``.
    """

    def __init__(
        self,
        *,
        message_id: str,
        accepted: bool,
        connection: Mapping[str, Any] | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(
            "data_bus_outcome_unknown",
            "The Data Bus operation did not return a terminal result before the timeout.",
            details={
                "message_id": message_id,
                "ingress_ack_received": bool(accepted),
                **dict(connection or {}),
                **dict(evidence or {}),
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
    # App Foundation owns reconnects. python-socketio's private reconnect loop
    # calls connect() without retaining the caller's wait_timeout, so a slow
    # namespace admission falls back to its one-second default after every
    # transport drop. Keeping that loop off lets every attempt use the same
    # explicit namespace-outcome wait as the initial connection.
    return socketio.AsyncClient(
        reconnection=False,
        logger=False,
        engineio_logger=False,
    )


def _is_socketio_timeout(error: BaseException) -> bool:
    try:
        from socketio.exceptions import TimeoutError as SocketIOTimeoutError
    except ImportError:  # pragma: no cover - data-bus extra is optional
        return False
    return isinstance(error, SocketIOTimeoutError)


_NAMESPACE_ADMISSION_TIMEOUT_SECONDS = 30.0
_RECONNECT_DELAY_SECONDS = 1.0
_RECONNECT_DELAY_MAX_SECONDS = 10.0
# An ingress acknowledgement that times out on a socket that delivered nothing
# since the request was sent marks the transport as silent, and the client
# replaces it at once instead of waiting for Engine.IO to notice. A timer that
# fired this much late or more means the event loop was not running, so the
# silence is not evidence about the transport (W448).
_SILENT_TRANSPORT_MAX_TIMER_OVERRUN_SECONDS = 1.0
# How long after a transport drop the client still calls its own reconnect a
# transient recovery. Past it, an owner that would replace the session does so.
_TRANSPORT_RECOVERY_WINDOW_SECONDS = 60.0
# A replaced transport gets this long to disconnect gracefully, and each
# release step this long, before its resources are released anyway. Beyond
# this many transports retiring at once, a new one is released at once.
_RETIRE_TRANSPORT_GRACE_SECONDS = 5.0
_MAX_RETIRING_TRANSPORTS = 4


class FederatedDataBusClient:
    """One bundle-scoped Socket.IO session with correlated terminal replies.

    The first handshake presents ``credential``. When the production socket
    drops, App Foundation reconnects it with the same namespace-admission
    bound, and every reconnect handshake asks ``credential_source`` for the
    credential that is valid at that moment. A delegated bearer captured at
    the first connect has usually lapsed by the time a long-lived socket drops,
    so presenting it again is refused as expired on every attempt and the
    session only returns when its owner tears the client down and opens a new
    one minutes later. The source gets a
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
        namespace_admission_timeout_seconds: float = _NAMESPACE_ADMISSION_TIMEOUT_SECONDS,
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
        # Whether the server refused any handshake since the last connect.
        # _episode_refusal describes only the previous attempt and is cleared
        # when the next one starts; this stays set for the whole episode.
        self._episode_refused = False
        self._socket_factory = socket_factory or _default_socket_factory
        # A custom socket factory owns its reconnect policy. The production
        # factory deliberately disables python-socketio reconnects so
        # this client can retain the namespace-admission bound on every try.
        self._owns_reconnect = socket_factory is None
        self.namespace_admission_timeout_seconds = max(
            0.1, float(namespace_admission_timeout_seconds)
        )
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
        self._reconnect_task: asyncio.Task[None] | None = None
        self._reconnect_delay_seconds = _RECONNECT_DELAY_SECONDS
        self._namespace_outcome: asyncio.Future[
            tuple[str, dict[str, Any] | None]
        ] | None = None
        # Every App Foundation-owned attempt gets a new Socket.IO client and
        # an identity captured by its callbacks. Engine.IO dispatches packet
        # handlers in background tasks, so a packet from a retired transport
        # can run after the next attempt starts. Its token must remain retired
        # instead of being reset for the new attempt.
        self.socket: Any
        self._active_socket_token: object | None = None
        self._connection_generation = 0
        self._socket_id = ""
        # Disconnects of the active transport over this client's lifetime. A
        # request compares it before and after, so a failure says whether the
        # transport dropped while it waited (W448).
        self._disconnect_count = 0
        # When the active transport last dropped (monotonic), until the next
        # connect. It bounds what transport_recovering calls transient.
        self._disconnected_at: float | None = None
        # The last packet the client saw from the server, as the socket token
        # it arrived on and a monotonic time: a namespace connect, a service
        # event or an ingress acknowledgement. Engine.IO pings are answered
        # inside python-engineio and never reach this client, so a quiet but
        # healthy socket can look silent here; only an acknowledgement that
        # times out turns that silence into a decision.
        self._last_inbound: tuple[object, float] | None = None
        # Old transports being closed after the client replaced them as
        # silent: each retirement task and the socket it owns until released.
        self._retiring_transports: dict[asyncio.Task[None], Any] = {}
        # Retired transports whose resources were released; close() must not
        # shut one down again.
        self._released_transports: weakref.WeakSet[Any] = weakref.WeakSet()
        # Monotonic start of the current namespace attempt, for the reconnect
        # handshake to report how long the transport took to open.
        self._attempt_started_at: float | None = None
        # The server's answer when it refuses the namespace: python-socketio
        # delivers it to connect_error and then raises a generic
        # ConnectionError from connect(), so the reason has to be caught here
        # or the caller cannot tell a refused credential from a dead network.
        self._connect_refusal: dict[str, Any] | None = None
        # Labels are diagnostic coordinates supplied by the owning product.
        # They must never contain credentials or other secret material.
        self._lifecycle_labels = _normalize_lifecycle_labels(lifecycle_labels)
        self._activate_socket(self._socket_factory())

    def _activate_socket(self, socket: Any) -> object:
        """Make one transport current and bind callbacks to its identity."""

        token = object()
        self.socket = socket
        self._active_socket_token = token

        async def on_connect() -> None:
            await self._on_connect(socket, token)

        async def on_connect_error(data: Any = None) -> None:
            await self._on_connect_error(socket, token, data)

        async def on_disconnect(*args: Any) -> None:
            await self._on_disconnect(socket, token, *args)

        async def on_service_event(payload: Any) -> None:
            await self._on_service_event(socket, token, payload)

        socket.on("connect", on_connect)
        socket.on("connect_error", on_connect_error)
        socket.on("disconnect", on_disconnect)
        socket.on("chat_service", on_service_event)
        return token

    def _socket_is_active(self, socket: Any, token: object) -> bool:
        return self.socket is socket and self._active_socket_token is token

    def _deactivate_socket(self, socket: Any, token: object) -> None:
        if self._socket_is_active(socket, token):
            self._active_socket_token = None

    def _socket_for_namespace_attempt(self) -> tuple[Any, object]:
        token = self._active_socket_token
        if token is None:
            token = self._activate_socket(self._socket_factory())
        return self.socket, token

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
    def transport_recovering(self) -> bool:
        """True while a plain transport drop is being repaired by this client.

        An owner that would replace the session after a failure can keep it
        instead: the client is bringing the transport back on its own, and
        receipts fan out to the re-authenticated session, so requests that
        were waiting still get their outcome. It is False whenever the drop is
        not just transport: the client is closed or connected, a custom socket
        factory owns the reconnect, the credential has expired on this side's
        clock, the server refused a handshake in this episode, no reconnect is
        running, or the drop is older than the recovery window. The owner
        still fences Card replacement, revocation and cancellation itself.
        """

        if self._closed or not self._owns_reconnect or self.connected:
            return False
        if self._expired() or self._episode_refused:
            return False
        dropped_at = self._disconnected_at
        task = self._reconnect_task
        if dropped_at is None or task is None or task.done():
            return False
        return time.monotonic() - dropped_at <= _TRANSPORT_RECOVERY_WINDOW_SECONDS

    def _note_inbound(self, token: object) -> None:
        self._last_inbound = (token, time.monotonic())

    def _inbound_since(self, token: object, since: float) -> bool:
        last = self._last_inbound
        return bool(last is not None and last[0] is token and last[1] >= since)

    async def _replace_silent_transport(
        self, socket: Any, token: object, *, silent_seconds: float
    ) -> bool:
        """Drop a transport that answered nothing, so the reconnect starts now.

        Only the transport the timed-out request was sent on is touched, and
        only while it is still the active one: a request that outlived a
        reconnect never closes the newer socket. The drop goes through the
        same path as one Engine.IO reports, so pending outcome waits keep
        waiting for the reconnected session. Closing the old socket can wait
        on a dead network, so it runs beside the request.
        """

        if not self._socket_is_active(socket, token):
            return False
        logger.warning(
            "Data Bus socket lifecycle event=silent_transport_replaced "
            "connection_generation=%d socket_id=%s silent_seconds=%.3f%s",
            self._connection_generation,
            self._socket_id or "unassigned",
            silent_seconds,
            self._lifecycle_log_suffix(),
        )
        await self._on_disconnect(socket, token, "silent transport")
        graceful = len(self._retiring_transports) < _MAX_RETIRING_TRANSPORTS
        retiring = asyncio.ensure_future(
            self._retire_transport(socket, graceful=graceful)
        )
        self._retiring_transports[retiring] = socket
        retiring.add_done_callback(
            lambda task: self._retiring_transports.pop(task, None)
        )
        return True

    async def _retire_transport(self, socket: Any, *, graceful: bool) -> None:
        """Close a replaced transport, and release its resources whatever happens.

        The client owns the old transport until its network resources are
        gone. A graceful disconnect gets a bounded time; when it times out,
        fails, is skipped because too many transports are retiring, or is
        cancelled by close(), the release still runs.
        """

        try:
            if graceful:
                await asyncio.wait_for(
                    socket.disconnect(), timeout=_RETIRE_TRANSPORT_GRACE_SECONDS
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the transport is already replaced
            logger.info(
                "Data Bus socket lifecycle event=silent_transport_close_failed "
                "error=%s%s",
                type(exc).__name__,
                self._lifecycle_log_suffix(),
            )
        finally:
            await self._release_transport(socket)

    async def _release_transport(self, socket: Any) -> None:
        """Abort a retired transport's Engine.IO session and close its resources.

        First Engine.IO's own abort, which marks the session disconnected and
        closes its WebSocket and HTTP session without waiting for the read
        loop. Whatever that leaves running is then cancelled and closed:
        closing the aiohttp session closes every connection it holds, the
        WebSocket included. So nothing of the old transport stays open even
        when its disconnect never returned. Bounded, and never raises.
        """

        try:
            self._released_transports.add(socket)
        except TypeError:  # pragma: no cover - a socket that cannot be weakly referenced
            pass
        eio = getattr(socket, "eio", None)
        if eio is None:
            return
        if getattr(eio, "state", "") == "connected":
            try:
                await asyncio.wait_for(
                    eio.disconnect(abort=True),
                    timeout=_RETIRE_TRANSPORT_GRACE_SECONDS,
                )
            except Exception as exc:  # noqa: BLE001 - the forced steps below still run
                logger.info(
                    "Data Bus socket lifecycle event=silent_transport_abort_failed "
                    "error=%s%s",
                    type(exc).__name__,
                    self._lifecycle_log_suffix(),
                )
        loops = [
            task
            for task in (
                getattr(eio, "read_loop_task", None),
                getattr(eio, "write_loop_task", None),
            )
            if isinstance(task, asyncio.Future) and not task.done()
        ]
        for task in loops:
            task.cancel()
        try:
            if loops:
                await asyncio.wait(loops, timeout=_RETIRE_TRANSPORT_GRACE_SECONDS)
            http = getattr(eio, "http", None)
            if (
                http is not None
                and not getattr(eio, "external_http", False)
                and not http.closed
            ):
                await asyncio.wait_for(
                    http.close(), timeout=_RETIRE_TRANSPORT_GRACE_SECONDS
                )
        except Exception as exc:  # noqa: BLE001 - release is best effort, never fatal
            logger.warning(
                "Data Bus socket lifecycle event=silent_transport_release_failed "
                "error=%s%s",
                type(exc).__name__,
                self._lifecycle_log_suffix(),
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

    def _wait_evidence(
        self,
        *,
        disconnects_at_start: int,
        waited_from: float,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        """The transport as a timed-out wait left it, beside the request start.

        ``connection`` in the error describes the transport when the request
        began. These fields describe it when the wait ended: whether it
        dropped meanwhile, and how late the timer fired. A timer that fires
        well after its deadline means the event loop or the process was not
        running, which no transport timeout can explain (W448).
        """

        elapsed = max(0.0, time.monotonic() - waited_from)
        return {
            "connection_generation_at_failure": self._connection_generation,
            "socket_id_at_failure": self._socket_id,
            "connection_active_at_failure": self.connected,
            "disconnected_during_request": (
                self._disconnect_count != disconnects_at_start
            ),
            "timeout_seconds": round(timeout_seconds, 3),
            "timer_overrun_seconds": round(max(0.0, elapsed - timeout_seconds), 3),
        }

    def _lifecycle_log_suffix(self) -> str:
        return "".join(
            f" {key}={json.dumps(value, ensure_ascii=True, separators=(',', ':'))}"
            for key, value in self._lifecycle_labels
        )

    def _current_socket_id(self, socket: Any | None = None) -> str:
        selected = self.socket if socket is None else socket
        get_sid = getattr(selected, "get_sid", None)
        if callable(get_sid):
            try:
                value = get_sid()
            except (KeyError, TypeError, ValueError):
                value = ""
            if value:
                return str(value)
        return str(getattr(selected, "sid", "") or "")

    async def _on_connect(self, socket: Any, token: object) -> None:
        if self._closed or not self._socket_is_active(socket, token):
            logger.info(
                "Data Bus socket lifecycle event=late_connect_ignored "
                "connection_generation=%d%s",
                self._connection_generation,
                self._lifecycle_log_suffix(),
            )
            return
        previous_generation = self._connection_generation
        previous_socket_id = self._socket_id
        self._connection_generation += 1
        self._socket_id = self._current_socket_id(socket)
        self._episode_attempt = 0
        self._episode_refusal = None
        self._episode_refused = False
        self._disconnected_at = None
        self._note_inbound(token)
        self._connected.set()
        outcome = self._namespace_outcome
        if outcome is not None and not outcome.done():
            outcome.set_result(("connected", None))
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

    async def _on_connect_error(
        self, socket: Any, token: object, data: Any = None
    ) -> None:
        if self._closed or not self._socket_is_active(socket, token):
            logger.info(
                "Data Bus socket lifecycle event=late_connect_error_ignored "
                "connection_generation=%d%s",
                self._connection_generation,
                self._lifecycle_log_suffix(),
            )
            return
        self._connect_refusal = _refusal_payload(data)
        self._episode_refusal = dict(self._connect_refusal)
        self._episode_refused = True
        outcome = self._namespace_outcome
        if outcome is not None and not outcome.done():
            outcome.set_result(("refused", dict(self._connect_refusal)))
        logger.warning(
            "Data Bus socket lifecycle event=%s attempted_generation=%d "
            "socket_id=%s current_generation=%d current_socket_id=%s "
            "connection_active=%s generation_replaced=false reason=%s%s",
            "reconnect_refused" if self._connection_generation else "connect_refused",
            self._connection_generation + 1,
            self._current_socket_id(socket) or "unassigned",
            self._connection_generation,
            self._socket_id or "none",
            str(self.connected).lower(),
            _connection_reason(data),
            self._lifecycle_log_suffix(),
        )

    async def _on_disconnect(
        self, socket: Any, token: object, *args: Any
    ) -> None:
        if not self._socket_is_active(socket, token):
            logger.info(
                "Data Bus socket lifecycle event=late_disconnect_ignored "
                "connection_generation=%d%s",
                self._connection_generation,
                self._lifecycle_log_suffix(),
            )
            return
        self._connected.clear()
        self._disconnect_count += 1
        self._disconnected_at = time.monotonic()
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
        if self._owns_reconnect:
            self._deactivate_socket(socket, token)
        if (
            self._owns_reconnect
            and self._connection_generation
            and not self._closed
            and (self._reconnect_task is None or self._reconnect_task.done())
        ):
            self._reconnect_task = asyncio.create_task(self._reconnect())

    def _queue_event(self, event: Mapping[str, Any]) -> None:
        if self._events.full():
            try:
                self._events.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self._events.put_nowait(dict(event))

    async def _on_service_event(
        self, socket: Any, token: object, payload: Any
    ) -> None:
        if not self._socket_is_active(socket, token):
            return
        self._note_inbound(token)
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
        await self._connect_namespace()

    async def _connect_namespace(self) -> None:
        """Open the transport, then own the namespace outcome and its deadline."""

        socket, socket_token = self._socket_for_namespace_attempt()
        self._connect_refusal = None
        loop = asyncio.get_running_loop()
        outcome: asyncio.Future[tuple[str, dict[str, Any] | None]] = (
            loop.create_future()
        )
        self._namespace_outcome = outcome
        self._attempt_started_at = time.monotonic()
        try:
            # The auth is a coroutine function, not a payload: python-socketio
            # resolves a callable once per namespace handshake, on this connect
            # and on every reconnect it runs itself, so each handshake can
            # present the credential that is valid at that moment.
            await socket.connect(
                self.platform_url,
                socketio_path="socket.io",
                transports=["websocket", "polling"],
                auth=self._handshake_auth,
                # Return once Engine.IO is open. App Foundation waits below
                # for its own connect/connect_error outcome, so Socket.IO
                # cannot race a late namespace callback with private timeout
                # cleanup or silently restore its one-second reconnect bound.
                wait=False,
            )
        except Exception as exc:
            if self._namespace_outcome is outcome:
                self._namespace_outcome = None
            refusal = self._connect_refusal
            self._deactivate_socket(socket, socket_token)
            logger.warning(
                "Data Bus socket lifecycle event=connect_failed attempted_generation=%d "
                "transport_failed=%s refusal=%s error_chain=%s %s%s",
                self._connection_generation + 1,
                str(_transport_failed(exc)).lower(),
                str(refusal is not None).lower(),
                _failure_chain(exc),
                _failure_facts(exc),
                self._lifecycle_log_suffix(),
            )
            if refusal is None or _transport_failed(exc):
                # The transport failed before the server answered. python-socketio
                # still fires connect_error for that, with "Connection error", so
                # the refusal it left says nothing about the server. During a
                # platform restart the ingress answers the upgrade with 404 and
                # reading it as a refusal ended the relay process (dev-main,
                # 2026-09-26 03:55Z). Callers classify this as transient.
                self._connect_refusal = None
                self._episode_refusal = None
                raise
            raise DataBusIngressRejected(
                str(refusal.get("code") or "data_bus_connect_refused"),
                str(refusal.get("message") or "The Data Bus refused this connection."),
                details=refusal,
            ) from exc
        try:
            state, refusal = await asyncio.wait_for(
                asyncio.shield(outcome),
                timeout=self.namespace_admission_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            logger.warning(
                "Data Bus socket lifecycle event=namespace_admission_timeout "
                "attempted_generation=%d timeout_seconds=%s%s",
                self._connection_generation + 1,
                self.namespace_admission_timeout_seconds,
                self._lifecycle_log_suffix(),
            )
            self._deactivate_socket(socket, socket_token)
            await socket.disconnect()
            try:
                from socketio.exceptions import ConnectionError as SocketIOConnectionError
            except ImportError:  # pragma: no cover - connect requires the extra
                raise
            raise SocketIOConnectionError(
                "One or more namespaces failed to connect"
            ) from exc
        finally:
            if self._namespace_outcome is outcome:
                self._namespace_outcome = None
        if state == "refused":
            self._deactivate_socket(socket, socket_token)
            await socket.disconnect()
            refusal = dict(refusal or {})
            raise DataBusIngressRejected(
                str(refusal.get("code") or "data_bus_connect_refused"),
                str(refusal.get("message") or "The Data Bus refused this connection."),
                details=refusal,
            )

    async def _reconnect(self) -> None:
        """Retry a dropped production socket without Socket.IO's wait default."""

        delay = self._reconnect_delay_seconds
        try:
            while not self._closed:
                await asyncio.sleep(delay)
                if self._closed:
                    return
                try:
                    await self._connect_namespace()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - reconnect stays resident
                    logger.warning(
                        "Data Bus socket lifecycle event=reconnect_attempt_failed "
                        "connection_generation=%d delay_seconds=%g error=%s%s",
                        self._connection_generation,
                        delay,
                        type(exc).__name__,
                        self._lifecycle_log_suffix(),
                    )
                    delay = min(delay * 2, _RECONNECT_DELAY_MAX_SECONDS)
                    continue
                return
        finally:
            if asyncio.current_task() is self._reconnect_task:
                self._reconnect_task = None
                # A transport can drop immediately after its namespace
                # callback, before this task unwinds. _on_disconnect cannot
                # replace a reconnect task that is still running, so close
                # that narrow handoff race here.
                if self._owns_reconnect and not self._closed and not self.connected:
                    self._reconnect_task = asyncio.create_task(self._reconnect())

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
        # python-socketio asks for the auth once Engine.IO is open, so the
        # time since the attempt began is the transport's, and the source's
        # own time is measured apart. A reconnect that takes tens of seconds
        # then says which of the two it was waiting on.
        entered = time.monotonic()
        started = self._attempt_started_at
        dropped_at = self._disconnected_at
        self._episode_attempt += 1
        attempt = HandshakeAttempt(
            connection_generation=self._connection_generation,
            attempt=self._episode_attempt,
            previous_refusal=(
                dict(self._episode_refusal) if self._episode_refusal else None
            ),
        )
        # The value describes only the immediately preceding handshake. A
        # refusal from this attempt will set it again in _on_connect_error;
        # a transport failure or timeout must leave the next attempt with no
        # claimed server answer.
        self._episode_refusal = None
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
        resolved_at = time.monotonic()
        logger.info(
            "Data Bus socket lifecycle event=handshake attempt=%d "
            "connection_generation=%d credential=%s after_refusal=%s "
            "since_disconnect_seconds=%s transport_open_seconds=%s "
            "bearer_resolve_seconds=%.3f%s",
            attempt.attempt,
            attempt.connection_generation,
            presented,
            str(attempt.previous_refusal is not None).lower(),
            "unknown" if dropped_at is None else f"{entered - dropped_at:.3f}",
            "unknown" if started is None else f"{entered - started:.3f}",
            resolved_at - entered,
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

        App Foundation reconnects its production socket after a drop. An owner
        that would otherwise tear this client down and open a new one waits
        here first, for the bound it accepts, and keeps the session when the
        socket comes back.
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
            self._closed = True
            self._connected.clear()
            reconnect_task = self._reconnect_task
            if reconnect_task is not None and reconnect_task is not asyncio.current_task():
                reconnect_task.cancel()
                await asyncio.gather(reconnect_task, return_exceptions=True)
            # Cancelling a retirement still runs its release, so every replaced
            # transport's resources are closed before close() returns.
            retiring = dict(self._retiring_transports)
            for task in retiring:
                task.cancel()
            if retiring:
                await asyncio.gather(*retiring, return_exceptions=True)
            if self.socket in self._released_transports:
                # The reconnect had not replaced the retired transport yet; its
                # retirement already released it.
                pass
            elif callable(shutdown := getattr(self.socket, "shutdown", None)):
                # python-socketio disconnect() is a no-op while disconnected and
                # leaves its reconnect task alive. shutdown() covers both the
                # connected and reconnecting states and waits for that task to end.
                await shutdown()
            else:  # pragma: no cover - compatibility with older socket factories
                await self.socket.disconnect()
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
        disconnects_at_start = self._disconnect_count
        loop = asyncio.get_running_loop()
        future: asyncio.Future[DataBusOutcome] = loop.create_future()
        self._pending[resolved_message_id] = future
        accepted = False
        sent_on = self.socket
        sent_token = self._active_socket_token
        try:
            waited_from = time.monotonic()
            try:
                ack = await sent_on.call(
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
                evidence = self._wait_evidence(
                    disconnects_at_start=disconnects_at_start,
                    waited_from=waited_from,
                    timeout_seconds=self.ingress_timeout_seconds,
                )
                evidence["silent_transport_replaced"] = False
                if (
                    self._owns_reconnect
                    and not self._closed
                    and sent_token is not None
                    and not evidence["disconnected_during_request"]
                    and evidence["timer_overrun_seconds"]
                    < _SILENT_TRANSPORT_MAX_TIMER_OVERRUN_SECONDS
                    and not self._inbound_since(sent_token, waited_from)
                ):
                    evidence["silent_transport_replaced"] = (
                        await self._replace_silent_transport(
                            sent_on,
                            sent_token,
                            silent_seconds=time.monotonic() - waited_from,
                        )
                    )
                raise DataBusOutcomeUnknown(
                    message_id=resolved_message_id,
                    accepted=False,
                    connection=connection,
                    evidence=evidence,
                ) from exc
            if self._socket_is_active(sent_on, sent_token):
                self._note_inbound(sent_token)
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
            outcome_timeout = (
                self.outcome_timeout_seconds
                if timeout_seconds is None
                else max(0.1, float(timeout_seconds))
            )
            waited_from = time.monotonic()
            try:
                return await asyncio.wait_for(
                    asyncio.shield(future), timeout=outcome_timeout
                )
            except asyncio.TimeoutError as exc:
                raise DataBusOutcomeUnknown(
                    message_id=resolved_message_id,
                    accepted=True,
                    connection=connection,
                    evidence=self._wait_evidence(
                        disconnects_at_start=disconnects_at_start,
                        waited_from=waited_from,
                        timeout_seconds=outcome_timeout,
                    ),
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

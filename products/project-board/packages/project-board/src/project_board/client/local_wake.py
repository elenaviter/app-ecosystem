"""Best-effort local wake-up for durable work in the shared field.

Mail and outbox rows remain durable in the shared field. These sockets only
shorten the wait between a durable write and the next probe; each consumer's
timer remains the recovery path when no socket is available.
"""

from __future__ import annotations

import hashlib
import os
import select
import socket
import tempfile
import time
from pathlib import Path


_WAKE_BYTE = b"1"
_RELAY_OUTBOX_IDENTITY = "relay-outbox"


def _wake_socket_path(identity: bytes) -> Path:
    token = hashlib.sha256(identity).hexdigest()[:32]
    uid = getattr(os, "getuid", lambda: 0)()
    # Unix-domain socket paths are short (104 bytes on macOS). /tmp keeps the
    # endpoint bounded even when the field itself has a deeply nested path.
    base = Path("/tmp") if os.name == "posix" else Path(tempfile.gettempdir())
    return base / f"problem-board-wake-{uid}" / f"{token}.sock"


def worker_wake_socket_path(field_root: str | Path, worker_name: str) -> Path:
    """Return the bounded, per-field and per-worker local socket path."""

    identity = f"{Path(field_root).expanduser().resolve()}\0{worker_name}".encode()
    return _wake_socket_path(identity)


def relay_outbox_wake_socket_path(field_root: str | Path) -> Path:
    """Return the bounded socket path for this field's relay outbox drain."""

    identity = (
        f"{Path(field_root).expanduser().resolve()}\0{_RELAY_OUTBOX_IDENTITY}"
    ).encode()
    return _wake_socket_path(identity)


def _notify(endpoint: Path) -> bool:
    if not hasattr(socket, "AF_UNIX"):
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sender:
            sender.setblocking(False)
            sender.sendto(_WAKE_BYTE, os.fspath(endpoint))
        return True
    except (OSError, ValueError):
        # Delivery is already durable. A missing, stale, or full notification
        # socket therefore falls back to the consumer's safety probe.
        return False


def notify_worker_watch(field_root: str | Path, worker_name: str) -> bool:
    """Wake one worker's listener, or return false when it is not attached."""

    return _notify(worker_wake_socket_path(field_root, worker_name))


def notify_relay_outbox(field_root: str | Path) -> bool:
    """Wake the field's relay outbox drain after its durable write."""

    return _notify(relay_outbox_wake_socket_path(field_root))


class _LocalWakeListener:
    """One datagram listener with a timer-only fallback."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._socket: socket.socket | None = None

    @property
    def available(self) -> bool:
        return self._socket is not None

    def __enter__(self) -> "_LocalWakeListener":
        if not hasattr(socket, "AF_UNIX"):
            return self
        candidate: socket.socket | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.path.parent, 0o700)
            # A crashed or replaced listener can leave only this filesystem
            # name. The durable store is authoritative, so replacing it loses
            # no work.
            self.path.unlink(missing_ok=True)
            candidate = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            candidate.setblocking(False)
            candidate.bind(os.fspath(self.path))
            os.chmod(self.path, 0o600)
            self._socket = candidate
        except (OSError, ValueError):
            if candidate is not None:
                candidate.close()
            self._socket = None
        return self

    def wait(self, timeout_seconds: float) -> bool:
        """Wait for a local wake or the safety timeout, then drain wake bytes."""

        timeout = max(0.0, float(timeout_seconds))
        if self._socket is None:
            time.sleep(timeout)
            return False
        try:
            readable, _, _ = select.select([self._socket], [], [], timeout)
            if not readable:
                return False
            while True:
                try:
                    self._socket.recv(64)
                except BlockingIOError:
                    break
            return True
        except OSError:
            # A transient local notification failure must never stop polling.
            return False

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        # Do not unlink here. A replacement listener deliberately unlinks and
        # rebinds this stable name; an older listener must not remove its
        # endpoint when the overlap ends. The next bind clears any stale name.

    def __exit__(self, *_exc: object) -> None:
        self.close()


class WorkerWakeListener(_LocalWakeListener):
    """One worker-scoped datagram listener with a timer-only fallback."""

    def __init__(self, field_root: str | Path, worker_name: str) -> None:
        super().__init__(worker_wake_socket_path(field_root, worker_name))


class RelayOutboxWakeListener(_LocalWakeListener):
    """One field-scoped relay outbox listener with a timer-only fallback."""

    def __init__(self, field_root: str | Path) -> None:
        super().__init__(relay_outbox_wake_socket_path(field_root))


__all__ = [
    "RelayOutboxWakeListener",
    "WorkerWakeListener",
    "notify_relay_outbox",
    "notify_worker_watch",
    "relay_outbox_wake_socket_path",
    "worker_wake_socket_path",
]

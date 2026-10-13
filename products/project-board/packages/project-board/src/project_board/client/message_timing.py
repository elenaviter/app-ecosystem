"""Strictly bounded, opt-in UUID timing capture using the existing log owner."""

from __future__ import annotations

import json
import logging
import math
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from app_foundation.data_bus.timing import RequestTiming, RequestTimingPhase

MAX_SECONDS = 900
MAX_REQUESTS = 100
MAX_RECORD_BYTES = 512
MAX_CAPTURE_BYTES = 256 * 1024
PREFIX_BYTES = 128
OPERATIONS = frozenset({"worker.heartbeat", "control.pull"})


@dataclass(frozen=True, slots=True)
class MessageTimingConfig:
    enabled: bool = False
    expires_at: str = ""

    @classmethod
    def from_mapping(cls, value: Any) -> "MessageTimingConfig":
        if value is None:
            return cls()
        if not isinstance(value, Mapping) or set(value) - {"enabled", "expires_at"}:
            raise ValueError("message timing config accepts enabled and expires_at only")
        enabled = value.get("enabled", False)
        expiry = value.get("expires_at", "")
        if type(enabled) is not bool or not isinstance(expiry, str):
            raise ValueError("message timing config requires a boolean and a UTC timestamp")
        result = cls(enabled=enabled, expires_at=expiry)
        result.expiry()
        return result

    def expiry(self) -> float:
        if not self.enabled:
            return 0.0
        try:
            value = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
            if value.tzinfo is None or value.utcoffset().total_seconds() != 0:
                raise ValueError
            expiry = value.timestamp()
            if expiry > time.time() + MAX_SECONDS:
                raise ValueError
            return expiry
        except (ValueError, OverflowError, AttributeError):
            raise ValueError("enabled message timing needs UTC expiry within 15 minutes") from None


class MessageTimingGate:
    """One process-lifetime budget shared by all channels and reconnects.

    The byte ceiling is deliberately cumulative (including the formatter's
    reserve), so diagnostic records waiting in the general log queue can never
    exceed it. A drained queue does not refill this budget. No payload enters
    this class, and it creates no writer, task, thread, timer or remote write.
    """

    def __init__(self) -> None:
        self.config = MessageTimingConfig()
        self._expiry = 0.0
        self._deadline = 0.0
        self._lock = threading.Lock()
        self._attempts: dict[str, int] = {}
        self.sampled_requests = 0
        self.admitted_bytes = 0
        self.dropped_records = 0
        self.dropped_bytes = 0

    def configure(self, config: MessageTimingConfig) -> None:
        if config == self.config:
            return
        expiry = config.expiry()
        self.config = config
        self._expiry = expiry
        self._deadline = time.monotonic() + max(0.0, min(MAX_SECONDS, expiry - time.time()))

    def observer(self, operation: str, message_id: str) -> Callable[[RequestTiming], None] | None:
        if not self.config.enabled:
            return None
        if operation not in OPERATIONS or time.time() >= self._expiry or time.monotonic() >= self._deadline:
            return None
        try:
            request_id = str(uuid.UUID(message_id))
        except (ValueError, AttributeError, TypeError):
            self.dropped_records += 1
            return None
        if not self._lock.acquire(blocking=False):
            self.dropped_records += 1
            return None
        try:
            if self.sampled_requests >= MAX_REQUESTS:
                self.dropped_records += 1
                return None
            self.sampled_requests += 1
            attempt = self._attempts.get(request_id, 0) + 1
            self._attempts[request_id] = attempt
        finally:
            self._lock.release()

        def observe(event: RequestTiming) -> None:
            if time.time() >= self._expiry or time.monotonic() >= self._deadline:
                return
            if not self._lock.acquire(blocking=False):
                self.dropped_records += 1
                return
            try:
                if (
                    type(event) is not RequestTiming
                    or event.message_id != request_id
                    or type(event.phase) is not RequestTimingPhase
                    or type(event.elapsed_seconds) not in (int, float)
                    or not math.isfinite(event.elapsed_seconds)
                    or event.elapsed_seconds < 0
                ):
                    self.dropped_records += 1
                    return
                utc = datetime.fromisoformat(event.utc.replace("Z", "+00:00"))
                if utc.tzinfo is None or utc.utcoffset().total_seconds() != 0:
                    self.dropped_records += 1
                    return
                # Construct fields explicitly; never serialize an event,
                # context, exception, arbitrary mapping or business result.
                line = json.dumps({
                    "schema": "data_bus.timing.v1",
                    "phase": event.phase.value,
                    "operation": operation,
                    "message_id": request_id,
                    "attempt": attempt,
                    "utc": utc.isoformat(timespec="microseconds"),
                    "elapsed_seconds": round(event.elapsed_seconds, 6),
                    "dropped_records": self.dropped_records,
                    "dropped_bytes": self.dropped_bytes,
                }, separators=(",", ":"), ensure_ascii=True)
                size = len(line.encode("utf-8")) + PREFIX_BYTES
                if size > MAX_RECORD_BYTES or self.admitted_bytes + size > MAX_CAPTURE_BYTES:
                    self.dropped_records += 1
                    self.dropped_bytes += size
                    return
                from .relay_logging import relay_log_owner

                owner = relay_log_owner()
                if owner is None:
                    self.dropped_records += 1
                    self.dropped_bytes += size
                    return
                record = logging.LogRecord("project_board.relay.message_timing", logging.INFO, "", 0, line, (), None)
                # Strict diagnostic admission precedes the general 4 MiB queue.
                self.admitted_bytes += size
                if not owner.submit_record(record):
                    self.dropped_records += 1
                    self.dropped_bytes += size
            except (Exception,):
                self.dropped_records += 1
            finally:
                self._lock.release()

        return observe


MESSAGE_TIMINGS = MessageTimingGate()

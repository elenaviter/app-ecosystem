"""Lifecycle, retry, and health accounting for one host-relay adapter."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from service_foundation.host_relay.contracts import (
    HostRelayAdapter,
    HostRelayEvent,
    HostRelayRetryableError,
)

Observer = Callable[[HostRelayEvent], Awaitable[None] | None]
Clock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class HostRelayPolicy:
    poll_interval_seconds: float = 60.0
    retry_initial_seconds: float = 1.0
    retry_max_seconds: float = 60.0
    retry_multiplier: float = 2.0

    def __post_init__(self) -> None:
        if self.poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be greater than zero")
        if self.retry_initial_seconds <= 0:
            raise ValueError("retry_initial_seconds must be greater than zero")
        if self.retry_max_seconds < self.retry_initial_seconds:
            raise ValueError("retry_max_seconds must be at least retry_initial_seconds")
        if self.retry_multiplier < 1:
            raise ValueError("retry_multiplier must be at least one")

    def retry_delay(self, consecutive_failures: int) -> float:
        exponent = max(0, int(consecutive_failures) - 1)
        return min(
            self.retry_max_seconds,
            self.retry_initial_seconds * (self.retry_multiplier**exponent),
        )


@dataclass(frozen=True, slots=True)
class HostRelayHealth:
    adapter_id: str
    state: str
    cycles_started: int
    cycles_succeeded: int
    consecutive_failures: int
    last_started_at: float | None
    last_succeeded_at: float | None
    last_error_code: str


class HostRelayRuntime:
    """Run one adapter without owning its domain or transport semantics."""

    def __init__(
        self,
        *,
        adapter: HostRelayAdapter,
        policy: HostRelayPolicy | None = None,
        observer: Observer | None = None,
        clock: Clock = time.time,
    ) -> None:
        adapter_id = str(getattr(adapter, "adapter_id", "") or "").strip().lower()
        if not adapter_id:
            raise ValueError("Host-relay adapters require a stable adapter_id.")
        if not callable(getattr(adapter, "poll_once", None)):
            raise TypeError("adapter does not satisfy HostRelayAdapter")
        self.adapter = adapter
        self.adapter_id = adapter_id
        self.policy = policy or HostRelayPolicy()
        self.observer = observer
        self.clock = clock
        self._cycles_started = 0
        self._cycles_succeeded = 0
        self._consecutive_failures = 0
        self._last_started_at: float | None = None
        self._last_succeeded_at: float | None = None
        self._last_error_code = ""
        self._state = "idle"

    @property
    def health(self) -> HostRelayHealth:
        return HostRelayHealth(
            adapter_id=self.adapter_id,
            state=self._state,
            cycles_started=self._cycles_started,
            cycles_succeeded=self._cycles_succeeded,
            consecutive_failures=self._consecutive_failures,
            last_started_at=self._last_started_at,
            last_succeeded_at=self._last_succeeded_at,
            last_error_code=self._last_error_code,
        )

    async def _emit(self, kind: str, details: Mapping[str, Any] | None = None) -> None:
        if self.observer is None:
            return
        emitted = self.observer(
            HostRelayEvent(
                kind=kind,
                adapter_id=self.adapter_id,
                cycle=self._cycles_started,
                timestamp=self.clock(),
                details=dict(details or {}),
            )
        )
        if inspect.isawaitable(emitted):
            await emitted

    async def run_once(self) -> dict[str, Any]:
        self._cycles_started += 1
        self._last_started_at = self.clock()
        self._state = "running"
        await self._emit("cycle.started")
        try:
            raw_result = await self.adapter.poll_once()
        except asyncio.CancelledError:
            self._state = "stopped"
            await self._emit("cycle.cancelled")
            raise
        except HostRelayRetryableError as exc:
            self._consecutive_failures += 1
            self._last_error_code = exc.code
            self._state = "degraded"
            await self._emit(
                "cycle.retryable_failure",
                {"error_code": exc.code, "retryable": True},
            )
            raise
        except Exception:
            self._consecutive_failures += 1
            self._last_error_code = "host_relay_adapter_failed"
            self._state = "failed"
            await self._emit(
                "cycle.failed",
                {"error_code": self._last_error_code, "retryable": False},
            )
            raise
        result = dict(raw_result)
        self._cycles_succeeded += 1
        self._consecutive_failures = 0
        self._last_error_code = ""
        self._last_succeeded_at = self.clock()
        self._state = "ready"
        await self._emit("cycle.succeeded", {"result_keys": sorted(result)})
        return result

    async def _wait(self, delay: float, stop_event: asyncio.Event | None) -> bool:
        if stop_event is None:
            await asyncio.sleep(delay)
            return False
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            return False
        return True

    async def run(self, *, stop_event: asyncio.Event | None = None) -> None:
        self._state = "starting"
        await self._emit("runtime.started")
        try:
            while stop_event is None or not stop_event.is_set():
                try:
                    await self.run_once()
                    delay = self.policy.poll_interval_seconds
                except HostRelayRetryableError:
                    delay = self.policy.retry_delay(self._consecutive_failures)
                if await self._wait(delay, stop_event):
                    break
        finally:
            self._state = "stopped"
            await self._emit("runtime.stopped")


__all__ = ["HostRelayHealth", "HostRelayPolicy", "HostRelayRuntime"]

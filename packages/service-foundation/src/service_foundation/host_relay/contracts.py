"""Portable contracts implemented by host-relay adapters."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


class HostRelayRetryableError(RuntimeError):
    """A classified transient failure that permits a bounded retry."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code or "host_relay_retryable_error")
        self.message = str(message or "The host relay cycle can be retried.")


@dataclass(frozen=True, slots=True)
class HostRelayEvent:
    kind: str
    adapter_id: str
    cycle: int
    timestamp: float
    details: Mapping[str, Any] = field(default_factory=dict)


class HostRelayAdapter(Protocol):
    """One domain adapter executed by a generic host-relay runtime."""

    adapter_id: str

    async def poll_once(self) -> Mapping[str, Any]: ...


class HostRelayAdapterFactory(Protocol):
    """An entry-point factory that constructs one configured adapter."""

    def __call__(self, config: Mapping[str, Any]) -> HostRelayAdapter: ...


__all__ = [
    "HostRelayAdapter",
    "HostRelayAdapterFactory",
    "HostRelayEvent",
    "HostRelayRetryableError",
]

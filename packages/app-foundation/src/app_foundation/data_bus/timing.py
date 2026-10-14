"""Payload-free, synchronous observations of a single Data Bus request.

Observers are optional local diagnostics, never authority or transport hooks.
The request owner supplies the observer; it must not block or schedule work.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable


class RequestTimingPhase(str, Enum):
    START = "client.request.start"
    SEND = "client.publish.start"
    ACK = "client.publish.accepted"
    REJECTED = "client.publish.rejected"
    OUTCOME = "client.wait.resumed"
    UNKNOWN = "client.outcome.unknown"
    CANCELLED = "client.request.cancelled"
    FAILED = "client.request.failed"
    FINISH = "client.request.finish"


@dataclass(frozen=True, slots=True)
class RequestTiming:
    message_id: str
    phase: RequestTimingPhase
    utc: str
    elapsed_seconds: float


RequestTimingObserver = Callable[[RequestTiming], None]

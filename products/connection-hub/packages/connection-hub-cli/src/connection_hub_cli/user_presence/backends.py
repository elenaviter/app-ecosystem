from __future__ import annotations

from typing import Protocol, runtime_checkable

from connection_hub_cli.user_presence.contracts import BoundHttpOperation
from connection_hub_cli.user_presence.errors import UserPresenceError
from connection_hub_cli.user_presence.operations import HttpOperationResult


def _safe_platform_name(value: str) -> str:
    candidate = str(value or "unknown").strip()
    if (
        not candidate
        or len(candidate) > 64
        or any(not (char.isalnum() or char in " ._-") for char in candidate)
    ):
        return "this platform"
    return candidate


@runtime_checkable
class UserPresenceBackend(Protocol):
    def available(self) -> bool: ...

    def execute(
        self,
        operation: BoundHttpOperation,
        *,
        body: bytes | bytearray | memoryview | str | None = None,
        timeout_seconds: float = 30.0,
    ) -> HttpOperationResult: ...


class UnavailableUserPresenceBackend:
    def __init__(self, *, platform_name: str) -> None:
        self.platform_name = _safe_platform_name(platform_name)

    def available(self) -> bool:
        return False

    def execute(
        self,
        operation: BoundHttpOperation,
        *,
        body: bytes | bytearray | memoryview | str | None = None,
        timeout_seconds: float = 30.0,
    ) -> HttpOperationResult:
        del operation, body, timeout_seconds
        raise UserPresenceError(
            "user_presence_unsupported_platform",
            f"Native user presence is not implemented for {self.platform_name}.",
        )

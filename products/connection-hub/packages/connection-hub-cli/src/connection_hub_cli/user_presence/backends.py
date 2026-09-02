from __future__ import annotations

from typing import Protocol, runtime_checkable

from connection_hub_cli.user_presence.contracts import ApprovalRequest, ApprovalResult
from connection_hub_cli.user_presence.errors import UserPresenceError


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

    def approve(self, request: ApprovalRequest) -> ApprovalResult: ...


class UnavailableUserPresenceBackend:
    def __init__(self, *, platform_name: str) -> None:
        self.platform_name = _safe_platform_name(platform_name)

    def available(self) -> bool:
        return False

    def approve(self, request: ApprovalRequest) -> ApprovalResult:
        del request
        raise UserPresenceError(
            "user_presence_unsupported_platform",
            f"Native user presence is not implemented for {self.platform_name}.",
        )

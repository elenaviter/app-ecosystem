from __future__ import annotations

from typing import Any


class UserPresenceError(RuntimeError):
    """A structured user-presence failure whose text is safe to render."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        native_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.native_status = native_status

    def __str__(self) -> str:
        return self.message

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.native_status is not None:
            value["native_status"] = self.native_status
        return value

"""Public coding-runtime account identity reported by a worker host.

The account helps a person identify which vendor login is running a worker.
It is display and change-detection metadata, never authorization authority.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .errors import DomainError


RUNTIME_ACCOUNT_FIELDS = frozenset({"account_id", "email", "organization"})
RUNTIME_ACCOUNT_MAXIMUMS = {
    "account_id": 512,
    "email": 512,
    "organization": 512,
}


def _runtime_account_text(value: Any, *, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise DomainError(
            "work_runtime_account_invalid",
            f"Runtime account {field} must be text.",
            status=400,
            details={"field": field},
        )
    text = value.strip()
    if len(text) > RUNTIME_ACCOUNT_MAXIMUMS[field] or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in text
    ):
        raise DomainError(
            "work_runtime_account_invalid",
            f"Runtime account {field} is not a bounded public value.",
            status=400,
            details={"field": field},
        )
    return text


def normalize_runtime_account(
    value: Mapping[str, Any] | None,
    *,
    required: bool = False,
) -> dict[str, str]:
    """Return the one token-free runtime account shape accepted on the wire."""

    if value is None:
        if required:
            raise DomainError(
                "work_runtime_account_unavailable",
                "The coding runtime did not report its signed-in account.",
                status=409,
            )
        return {}
    if not isinstance(value, Mapping):
        raise DomainError(
            "work_runtime_account_invalid",
            "Runtime account metadata must be an object.",
            status=400,
        )
    unexpected = sorted(str(key) for key in value if str(key) not in RUNTIME_ACCOUNT_FIELDS)
    if unexpected:
        raise DomainError(
            "work_runtime_account_invalid",
            "Runtime account metadata contains unsupported fields.",
            status=400,
            details={"fields": unexpected},
        )
    normalized = {
        field: _runtime_account_text(value.get(field), field=field)
        for field in ("account_id", "email", "organization")
    }
    if not normalized["account_id"]:
        if required:
            raise DomainError(
                "work_runtime_account_unavailable",
                "The coding runtime did not report a vendor account ID.",
                status=409,
            )
        return {}
    return normalized


def changed_runtime_account(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any] | None,
    *,
    changed_at: str,
) -> dict[str, str]:
    """Describe a real account switch; first observation is enrollment."""

    before = normalize_runtime_account(previous)
    after = normalize_runtime_account(current)
    previous_id = before.get("account_id", "")
    current_id = after.get("account_id", "")
    if not previous_id or not current_id or previous_id == current_id:
        return {}
    return {
        "previous_account_id": previous_id,
        "current_account_id": current_id,
        "changed_at": str(changed_at or ""),
    }


__all__ = [
    "RUNTIME_ACCOUNT_FIELDS",
    "changed_runtime_account",
    "normalize_runtime_account",
]

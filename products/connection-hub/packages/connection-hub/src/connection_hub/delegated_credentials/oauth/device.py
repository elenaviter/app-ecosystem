# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Host-neutral contracts for OAuth 2.0 Device Authorization Grant.

The device request is short-lived protocol state. It identifies a pending
consent request; it never carries an issued access or refresh credential.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Mapping


DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
DEVICE_REQUEST_TTL_SECONDS = 10 * 60
DEVICE_POLL_INTERVAL_SECONDS = 5
DEVICE_SLOW_DOWN_SECONDS = 5
DEVICE_USER_CODE_ATTEMPTS = 8
DEVICE_USER_CODE_WINDOW_SECONDS = 5 * 60

DEVICE_STATE_PENDING = "pending"
DEVICE_STATE_APPROVED = "approved"
DEVICE_STATE_DENIED = "denied"
DEVICE_STATE_CONSUMED = "consumed"

DEVICE_POLL_AUTHORIZATION_PENDING = "authorization_pending"
DEVICE_POLL_SLOW_DOWN = "slow_down"
DEVICE_POLL_ACCESS_DENIED = "access_denied"
DEVICE_POLL_EXPIRED = "expired_token"
DEVICE_POLL_REPLAYED = "device_code_replayed"
DEVICE_POLL_CLIENT_MISMATCH = "device_client_mismatch"
DEVICE_POLL_APPROVED = "approved"

DEVICE_TERMINAL_ERRORS = frozenset(
    {
        DEVICE_POLL_ACCESS_DENIED,
        "device_card_mismatch",
        "device_card_revision_conflict",
    }
)

_USER_CODE_ALPHABET = "23456789BCDFGHJKMNPQRTVWXY"
_FORBIDDEN_RECORD_KEYS = frozenset(
    {
        "access_token",
        "authorization",
        "device_code",
        "id_token",
        "refresh_token",
        "token",
        "user_code",
    }
)


def device_code_digest(value: str) -> str:
    candidate = str(value or "").strip()
    if not candidate:
        raise ValueError("device_code_required")
    return hashlib.sha256(candidate.encode("utf-8")).hexdigest()


def normalize_user_code(value: str) -> str:
    candidate = "".join(
        character
        for character in str(value or "").upper()
        if character not in {"-", " ", "\t", "\r", "\n"}
    )
    if len(candidate) != 8 or any(
        character not in _USER_CODE_ALPHABET for character in candidate
    ):
        raise ValueError("user_code_invalid")
    return candidate


def user_code_digest(value: str) -> str:
    return hashlib.sha256(normalize_user_code(value).encode("ascii")).hexdigest()


def generate_user_code() -> str:
    raw = "".join(secrets.choice(_USER_CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def assert_device_record_safe(value: Any, *, path: str = "record") -> None:
    """Reject credential material before a device record is serialized."""

    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key or "").strip().lower()
            if key in _FORBIDDEN_RECORD_KEYS or key.endswith("_token"):
                raise ValueError(f"device_record_secret_field:{path}.{key}")
            assert_device_record_safe(child, path=f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            assert_device_record_safe(child, path=f"{path}[{index}]")


@dataclass(frozen=True, slots=True)
class DeviceAuthorizationIssue:
    device_code: str
    user_code: str
    expires_in: int
    interval: int


@dataclass(frozen=True, slots=True)
class DevicePollResult:
    status: str
    interval: int
    authorization: dict[str, Any] | None = None

    @property
    def approved(self) -> bool:
        return self.status == DEVICE_POLL_APPROVED and self.authorization is not None


@dataclass(frozen=True, slots=True)
class DeviceUserCodeResult:
    status: str
    device_digest: str = ""
    user_digest: str = ""
    request: dict[str, Any] | None = None


def new_device_record(
    *,
    client_id: str,
    scopes: list[str] | tuple[str, ...],
    resource: str = "",
    client_metadata: Mapping[str, Any] | None = None,
    requested_access_id: str = "",
    expected_card_revision: int | None = None,
    context: Mapping[str, Any] | None = None,
    device_digest: str,
    user_digest: str,
    now: int | None = None,
    expires_in: int = DEVICE_REQUEST_TTL_SECONDS,
    interval: int = DEVICE_POLL_INTERVAL_SECONDS,
) -> dict[str, Any]:
    created_at = int(time.time() if now is None else now)
    lifetime = max(1, int(expires_in))
    poll_interval = max(1, int(interval))
    record: dict[str, Any] = {
        "schema": "connection_hub.oauth.device_request.v1",
        "state": DEVICE_STATE_PENDING,
        "client_id": str(client_id or "").strip(),
        "scopes": [str(item).strip() for item in scopes if str(item).strip()],
        "resource": str(resource or "").strip(),
        "client_metadata": dict(client_metadata or {}),
        "requested_access_id": str(requested_access_id or "").strip(),
        "context": dict(context or {}),
        "device_digest": str(device_digest or "").strip(),
        "user_digest": str(user_digest or "").strip(),
        "created_at": created_at,
        "expires_at": created_at + lifetime,
        "interval": poll_interval,
        "next_poll_at": created_at + poll_interval,
        "grant": None,
    }
    if expected_card_revision is not None:
        record["expected_card_revision"] = int(expected_card_revision)
    if not record["client_id"]:
        raise ValueError("client_id_required")
    if len(record["device_digest"]) != 64 or len(record["user_digest"]) != 64:
        raise ValueError("device_digest_invalid")
    assert_device_record_safe(record)
    return record


def encode_device_record(record: Mapping[str, Any]) -> str:
    snapshot = dict(record)
    assert_device_record_safe(snapshot)
    return json.dumps(
        snapshot,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def decode_device_record(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except Exception:
        return None
    if not isinstance(value, dict):
        return None
    try:
        assert_device_record_safe(value)
    except ValueError:
        return None
    return value


__all__ = [
    "DEVICE_GRANT_TYPE",
    "DEVICE_POLL_ACCESS_DENIED",
    "DEVICE_POLL_APPROVED",
    "DEVICE_POLL_AUTHORIZATION_PENDING",
    "DEVICE_POLL_CLIENT_MISMATCH",
    "DEVICE_POLL_EXPIRED",
    "DEVICE_POLL_INTERVAL_SECONDS",
    "DEVICE_POLL_REPLAYED",
    "DEVICE_POLL_SLOW_DOWN",
    "DEVICE_REQUEST_TTL_SECONDS",
    "DEVICE_SLOW_DOWN_SECONDS",
    "DEVICE_STATE_APPROVED",
    "DEVICE_STATE_CONSUMED",
    "DEVICE_STATE_DENIED",
    "DEVICE_STATE_PENDING",
    "DEVICE_TERMINAL_ERRORS",
    "DEVICE_USER_CODE_ATTEMPTS",
    "DEVICE_USER_CODE_WINDOW_SECONDS",
    "DeviceAuthorizationIssue",
    "DevicePollResult",
    "DeviceUserCodeResult",
    "assert_device_record_safe",
    "decode_device_record",
    "device_code_digest",
    "encode_device_record",
    "generate_user_code",
    "new_device_record",
    "normalize_user_code",
    "user_code_digest",
]

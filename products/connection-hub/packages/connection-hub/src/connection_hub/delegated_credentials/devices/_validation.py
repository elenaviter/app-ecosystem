# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Shared bounded-value validation for device wire contracts."""

from __future__ import annotations

import base64
import json
from typing import Any, Mapping

from connection_hub.delegated_credentials.devices.errors import DeviceCryptoError


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def compact_json(
    value: Mapping[str, Any],
    *,
    reason: str = "device_json_invalid",
) -> str:
    try:
        return json.dumps(
            dict(value),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise DeviceCryptoError(reason) from exc


def json_object(value: Any, *, reason: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (bytes, bytearray)):
        try:
            value = bytes(value).decode("utf-8")
        except UnicodeError as exc:
            raise DeviceCryptoError(reason) from exc
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise DeviceCryptoError(reason) from exc
        if isinstance(parsed, Mapping):
            return dict(parsed)
    raise DeviceCryptoError(reason)


def bounded_text(value: Any, *, reason: str, maximum: int = 512) -> str:
    candidate = str(value or "").strip()
    if (
        not candidate
        or len(candidate) > maximum
        or any(
            ord(character) < 0x21 or ord(character) == 0x7F
            for character in candidate
        )
    ):
        raise DeviceCryptoError(reason)
    return candidate

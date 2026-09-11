# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""The ``kst1`` session token: ``kst1.<b64url json claims>.<b64url hmac>``.

Byte-compatible with the platform session token this subpackage replaces:
compact JSON claims (no spaces, keys sorted), HMAC-SHA256 over the
encoded body with the shared session secret, both parts base64url without
padding. The signature proves the body was issued by a runtime holding the
secret and was not modified; whether the session is still alive is the
backend's answer, never the token's.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any, Mapping

TOKEN_PREFIX = "kst1"
CLAIMS_SCHEMA = "kdcube.session_token.v1"


class TokenInvalid(ValueError):
    """The token is not a well-formed, correctly signed ``kst1`` token."""


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _compact_json(data: Mapping[str, Any]) -> str:
    # Sorted keys and ASCII escapes: the platform's own encoding, so a token
    # minted here and one minted by the platform are the same bytes.
    return json.dumps(dict(data), separators=(",", ":"), sort_keys=True)


def _secret_bytes(secret: str | bytes) -> bytes:
    if isinstance(secret, bytes):
        value = secret
    else:
        value = str(secret or "").encode("utf-8")
    if not value:
        raise TokenInvalid("session secret is empty")
    return value


def encode(claims: Mapping[str, Any], *, secret: str | bytes) -> str:
    """Sign ``claims`` into a ``kst1`` token."""
    body = _b64url_encode(_compact_json(claims).encode("utf-8"))
    signature = hmac.new(_secret_bytes(secret), body.encode("ascii"), hashlib.sha256).digest()
    return f"{TOKEN_PREFIX}.{body}.{_b64url_encode(signature)}"


def decode(token: str, *, secret: str | bytes) -> dict[str, Any]:
    """The claims of a correctly signed token; ``TokenInvalid`` otherwise.
    Expiry is not checked here: the backend decides liveness."""
    parts = str(token or "").strip().split(".")
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX or not parts[1] or not parts[2]:
        raise TokenInvalid("malformed session token")
    _, body, signature = parts
    expected = hmac.new(_secret_bytes(secret), body.encode("ascii"), hashlib.sha256).digest()
    try:
        presented = _b64url_decode(signature)
    except Exception as exc:  # noqa: BLE001 - any decoding failure is one answer
        raise TokenInvalid("malformed session token signature") from exc
    if not hmac.compare_digest(expected, presented):
        raise TokenInvalid("session token signature mismatch")
    try:
        claims = json.loads(_b64url_decode(body).decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise TokenInvalid("malformed session token body") from exc
    if not isinstance(claims, dict):
        raise TokenInvalid("session token body is not an object")
    return claims


def token_hash(token: str) -> str:
    """The stable digest a store keeps instead of the token itself."""
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


__all__ = ["CLAIMS_SCHEMA", "TOKEN_PREFIX", "TokenInvalid", "decode", "encode", "token_hash"]

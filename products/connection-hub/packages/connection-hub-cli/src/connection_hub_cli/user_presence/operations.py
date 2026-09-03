from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from connection_hub_cli.user_presence.contracts import BoundHttpOperation
from connection_hub_cli.user_presence.errors import UserPresenceError

_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class HttpOperationResult:
    status_code: int
    operation_digest: str
    content_type: str
    body: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.status_code, int) or not 100 <= self.status_code <= 599:
            raise UserPresenceError(
                "invalid_operation_result",
                "The HTTP operation returned an invalid status code.",
            )
        if (
            not isinstance(self.operation_digest, str)
            or len(self.operation_digest) != 64
            or any(char not in "0123456789abcdef" for char in self.operation_digest)
        ):
            raise UserPresenceError(
                "invalid_operation_result",
                "The HTTP operation returned an invalid operation digest.",
            )
        if (
            not isinstance(self.content_type, str)
            or len(self.content_type) > 256
            or any(ord(char) < 32 or ord(char) == 127 for char in self.content_type)
        ):
            raise UserPresenceError(
                "invalid_operation_result",
                "The HTTP operation returned an invalid content type.",
            )
        if not isinstance(self.body, bytes) or len(self.body) > _MAX_RESPONSE_BYTES:
            raise UserPresenceError(
                "invalid_operation_result",
                "The HTTP operation returned an invalid or oversized body.",
            )

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "status_code": self.status_code,
            "operation_digest": self.operation_digest,
            "content_type": self.content_type,
            "body_length": len(self.body),
            "body_sha256": hashlib.sha256(self.body).hexdigest(),
        }


class _BoundRequestFailure(RuntimeError):
    pass


class _BoundResponseTooLarge(RuntimeError):
    pass


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl


def _validate_timeout_seconds(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UserPresenceError(
            "invalid_request_timeout",
            "The request timeout must be between 1 and 120 seconds.",
        )
    normalized = float(value)
    if not math.isfinite(normalized) or not 1.0 <= normalized <= 120.0:
        raise UserPresenceError(
            "invalid_request_timeout",
            "The request timeout must be between 1 and 120 seconds.",
        )
    return normalized


def _execute_bound_http(
    operation: BoundHttpOperation,
    *,
    body: bytes,
    credential: memoryview,
    timeout_seconds: float,
) -> HttpOperationResult:
    """Fixed internal executor; callers must not receive this credential seam."""

    try:
        bearer = bytes(credential).decode("ascii")
        url = f"{operation.target}{operation.path}"
        payload = body if body or operation.method not in {"GET", "HEAD"} else None
        native_request = Request(url, data=payload, method=operation.method)
        native_request.add_header("Accept", "application/json")
        if payload is not None:
            native_request.add_header("Content-Type", "application/json")
        native_request.add_header("Authorization", f"Bearer {bearer}")
        native_request.add_header("Connection", "close")
        native_request.add_header("User-Agent", "connection-hub-user-presence/2")
        opener = build_opener(ProxyHandler({}), _NoRedirects())

        try:
            response = opener.open(native_request, timeout=timeout_seconds)
        except HTTPError as exc:
            response = exc
        with response:
            response_body = response.read(_MAX_RESPONSE_BYTES + 1)
            status_code = int(response.status)
            content_type = str(response.headers.get("Content-Type") or "")
    except _BoundResponseTooLarge:
        raise
    except Exception:  # noqa: BLE001 - transport errors cross a fixed boundary
        raise _BoundRequestFailure from None
    finally:
        try:
            del bearer
        except UnboundLocalError:
            pass

    if len(response_body) > _MAX_RESPONSE_BYTES:
        raise _BoundResponseTooLarge
    return HttpOperationResult(
        status_code=status_code,
        operation_digest=operation.operation_digest,
        content_type=content_type,
        body=response_body,
    )

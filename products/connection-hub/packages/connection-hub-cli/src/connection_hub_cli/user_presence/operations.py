from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from connection_hub_cli.user_presence.contracts import ApprovalRequest
from connection_hub_cli.user_presence.errors import UserPresenceError

_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class HttpOperationResult:
    status_code: int
    request_digest: str
    content_type: str
    body: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.status_code, int) or not 100 <= self.status_code <= 599:
            raise UserPresenceError(
                "invalid_operation_result",
                "The HTTP operation returned an invalid status code.",
            )
        if (
            not isinstance(self.request_digest, str)
            or len(self.request_digest) != 64
            or any(char not in "0123456789abcdef" for char in self.request_digest)
        ):
            raise UserPresenceError(
                "invalid_operation_result",
                "The HTTP operation returned an invalid request digest.",
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
            "request_digest": self.request_digest,
            "content_type": self.content_type,
            "body_length": len(self.body),
            "body_sha256": hashlib.sha256(self.body).hexdigest(),
        }


class BoundHttpTransport(Protocol):
    def send(
        self,
        request: ApprovalRequest,
        *,
        body: bytes,
        credential: memoryview,
        timeout_seconds: float,
    ) -> HttpOperationResult: ...


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl


class UrllibBoundHttpTransport:
    """Executes one bound request without exposing or forwarding on redirect."""

    def __init__(self) -> None:
        self._opener = build_opener(ProxyHandler({}), _NoRedirects())

    def send(
        self,
        request: ApprovalRequest,
        *,
        body: bytes,
        credential: memoryview,
        timeout_seconds: float,
    ) -> HttpOperationResult:
        if not 1.0 <= timeout_seconds <= 120.0:
            raise UserPresenceError(
                "invalid_request_timeout",
                "The request timeout must be between 1 and 120 seconds.",
            )
        try:
            bearer = bytes(credential).decode("ascii")
        except (UnicodeDecodeError, ValueError):
            raise UserPresenceError(
                "protected_credential_invalid",
                "The protected credential is invalid.",
            ) from None

        url = f"{request.target}{request.path}"
        payload = body if body or request.method not in {"GET", "HEAD"} else None
        native_request = Request(url, data=payload, method=request.method)
        native_request.add_header("Accept", "application/json")
        if payload is not None:
            native_request.add_header("Content-Type", "application/json")
        native_request.add_header("Authorization", f"Bearer {bearer}")
        native_request.add_header("Connection", "close")
        native_request.add_header("User-Agent", "connection-hub-user-presence/1")

        try:
            try:
                response = self._opener.open(
                    native_request, timeout=float(timeout_seconds)
                )
            except HTTPError as exc:
                response = exc
            with response:
                response_body = response.read(_MAX_RESPONSE_BYTES + 1)
                status_code = int(response.status)
                content_type = str(response.headers.get("Content-Type") or "")
        except (OSError, URLError, ValueError):
            raise UserPresenceError(
                "bound_request_failed",
                "The approved request could not reach the selected KDCube host.",
            ) from None
        finally:
            del bearer

        if len(response_body) > _MAX_RESPONSE_BYTES:
            raise UserPresenceError(
                "bound_response_too_large",
                f"The approved request response exceeds {_MAX_RESPONSE_BYTES} bytes.",
            )
        return HttpOperationResult(
            status_code=status_code,
            request_digest=request.request_digest,
            content_type=content_type,
            body=response_body,
        )

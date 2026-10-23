"""The closed set of A2A client failure codes.

A remote agent's text never crosses this boundary: not a JSON-RPC error
message or ``data``, not an HTTP body, not a task status message. A failure is
one of these fixed codes, optionally with an App Foundation-owned ``reason``
(for example the outbound policy's denial reason).
"""

from __future__ import annotations

A2A_CARD_INVALID = "a2a_card_invalid"
A2A_VERSION_UNSUPPORTED = "a2a_version_unsupported"
REMOTE_ENDPOINT_DENIED = "remote_endpoint_denied"
REMOTE_UNAVAILABLE = "remote_unavailable"
REMOTE_AUTHORIZATION_REJECTED = "remote_authorization_rejected"
REMOTE_RESPONSE_INVALID = "remote_response_invalid"
REMOTE_RESPONSE_TOO_LARGE = "remote_response_too_large"
REMOTE_REQUEST_FAILED = "remote_request_failed"
REMOTE_TASK_NOT_FOUND = "remote_task_not_found"
REMOTE_TASK_NOT_CANCELABLE = "remote_task_not_cancelable"

A2A_CLIENT_ERROR_CODES = frozenset(
    {
        A2A_CARD_INVALID,
        A2A_VERSION_UNSUPPORTED,
        REMOTE_ENDPOINT_DENIED,
        REMOTE_UNAVAILABLE,
        REMOTE_AUTHORIZATION_REJECTED,
        REMOTE_RESPONSE_INVALID,
        REMOTE_RESPONSE_TOO_LARGE,
        REMOTE_REQUEST_FAILED,
        REMOTE_TASK_NOT_FOUND,
        REMOTE_TASK_NOT_CANCELABLE,
    }
)


class A2AClientError(RuntimeError):
    """A classified, text-free A2A client failure."""

    def __init__(self, code: str, *, reason: str = "") -> None:
        if code not in A2A_CLIENT_ERROR_CODES:
            raise ValueError(f"unknown A2A client error code: {code}")
        super().__init__(code if not reason else f"{code}: {reason}")
        self.code = code
        self.reason = reason


__all__ = [
    "A2A_CARD_INVALID",
    "A2A_CLIENT_ERROR_CODES",
    "A2A_VERSION_UNSUPPORTED",
    "A2AClientError",
    "REMOTE_AUTHORIZATION_REJECTED",
    "REMOTE_ENDPOINT_DENIED",
    "REMOTE_REQUEST_FAILED",
    "REMOTE_RESPONSE_INVALID",
    "REMOTE_RESPONSE_TOO_LARGE",
    "REMOTE_TASK_NOT_CANCELABLE",
    "REMOTE_TASK_NOT_FOUND",
    "REMOTE_UNAVAILABLE",
]

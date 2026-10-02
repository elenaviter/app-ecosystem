"""One redacted record per OAuth HTTP request, joinable to its lock spans and the proxy (W461).

On 2026-10-02 relay token refreshes took 5 to 11 s while the proxy answered
the token POST in 0.5 to 1.8 s, and nothing could say where the rest went:
the request id travelled to the proxy, but no record on this side named the
request, its profile or how long the client waited for it, so a request
could not be matched to its proxy line or to the lock spans of the same
operation.

Each request now leaves one record: its request id (the ``X-Request-ID`` the
proxy logs), the correlation of the token operation it belongs to and the
profile tag (both shared with the lock spans of that operation), the kind of
request (metadata, token, MCP probe), the method, the HTTP status, the
outcome and the client-side elapsed time. A token operation's correlation is
set where it starts (``access_token``, ``refresh_access_token``) and is
inherited by every request and span awaited inside it.

It never records a token, a credential, a header, a body, a query string, a
URL or a profile name in clear. Failures and cancellations log at WARNING,
requests of ``SLOW_SECONDS`` or longer at INFO, and every other request at
DEBUG, so a healthy relay adds no lines at the default level.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import secrets
from typing import Iterator

from connection_hub.caller.authorization import lock_spans

logger = logging.getLogger("connection_hub.oauth.requests")

SLOW_SECONDS = 1.0

_CORRELATION: contextvars.ContextVar[tuple[str, str]] = contextvars.ContextVar(
    "connection_hub_oauth_correlation", default=("-", "-")
)


@contextlib.contextmanager
def correlate(profile_name: str) -> Iterator[str]:
    """Name one token operation of ``profile_name``; its requests and spans carry the id."""

    correlation = secrets.token_hex(4)
    reset = _CORRELATION.set((lock_spans.profile_tag(profile_name), correlation))
    try:
        yield correlation
    finally:
        _CORRELATION.reset(reset)


def current() -> tuple[str, str]:
    """``(profile tag, correlation)`` of the token operation in progress, or ``("-", "-")``."""

    return _CORRELATION.get()


def kind_of(failure_code: str) -> str:
    if failure_code == "oauth_metadata_request_failed":
        return "metadata"
    if failure_code == "oauth_token_request_failed":
        return "token"
    return failure_code.removeprefix("oauth_").removesuffix("_failed") or "-"


def record(
    *,
    request_id: str,
    kind: str,
    method: str,
    status: int | None,
    outcome: str,
    elapsed_seconds: float,
) -> None:
    """Log one completed, failed or cancelled OAuth request."""

    if outcome != "ok":
        level = logging.WARNING
    elif elapsed_seconds >= SLOW_SECONDS:
        level = logging.INFO
    else:
        level = logging.DEBUG
    if not logger.isEnabledFor(level):
        return
    profile, correlation = current()
    logger.log(
        level,
        "Connection Hub OAuth request rid=%s corr=%s profile=%s kind=%s method=%s "
        "status=%s outcome=%s elapsed_ms=%d pid=%d task=%s",
        request_id,
        correlation,
        profile,
        kind,
        method,
        "-" if status is None else int(status),
        outcome,
        int(round(elapsed_seconds * 1000)),
        os.getpid(),
        lock_spans.task_tag(),
    )

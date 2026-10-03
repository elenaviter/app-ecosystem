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

W464: the client-side time is split into phases from the HTTP library's own
trace events, so a slow request says where its time went: ``queue_ms`` from
the start until the first network step (building the request, waiting for a
pooled connection, the event loop starting the work late), ``connect_ms``
(DNS and TCP), ``tls_ms``, ``send_ms``, ``wait_ms`` (until the response
headers) and ``read_ms`` (the body). A request on a reused connection has no
connect or TLS phase, and ``failed_at`` names the phase a failure or a
cancellation interrupted. On 2026-10-02 joins with the proxy left about
1.5 s per metadata request outside the server on the development host and
about 0.3 s per token request on spark1, and no record could say which phase
held it.

It never records a token, a credential, a header, a body, a query string, a
URL or a profile name in clear; the trace events' details (host, port,
request) are read for their timing only. Failures and cancellations log at WARNING,
requests of ``SLOW_SECONDS`` or longer at INFO, and every other request at
DEBUG, so a healthy relay adds no lines at the default level.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import secrets
import time
from typing import Any, Callable, Iterator, Mapping

from connection_hub.caller.authorization import lock_spans

logger = logging.getLogger("connection_hub.oauth.requests")

SLOW_SECONDS = 1.0

# The custody calls of the token operation in progress (W464): [calls, slow or
# failed calls], a list so a copied context (the custody thread) counts into it.
_CUSTODY_CALLS: contextvars.ContextVar[list[int] | None] = contextvars.ContextVar(
    "connection_hub_oauth_custody_calls", default=None
)

_CORRELATION: contextvars.ContextVar[tuple[str, str]] = contextvars.ContextVar(
    "connection_hub_oauth_correlation", default=("-", "-")
)


@contextlib.contextmanager
def correlate(profile_name: str) -> Iterator[str]:
    """Name one token operation of ``profile_name``; its requests and spans carry the id."""

    correlation = secrets.token_hex(4)
    reset = _CORRELATION.set((lock_spans.profile_tag(profile_name), correlation))
    calls = [0, 0]
    reset_calls = _CUSTODY_CALLS.set(calls)
    try:
        yield correlation
    finally:
        # The operation's exact custody call count, logged once: the per-call
        # records at the default level show only slow ones, so their last
        # seq would undercount (W464 review).
        try:
            if calls[0]:
                lock_spans.record_custody_total(calls=calls[0], notable=calls[1])
        except BaseException as broken:  # noqa: BLE001 - the count must never change the operation
            if isinstance(broken, (KeyboardInterrupt, SystemExit)):
                raise
        _CUSTODY_CALLS.reset(reset_calls)
        _CORRELATION.reset(reset)


def next_custody_call() -> int | None:
    """The 1-based number of the next custody call in this token operation, or None outside one."""

    calls = _CUSTODY_CALLS.get()
    if calls is None:
        return None
    calls[0] += 1
    return calls[0]


def note_notable_custody_call() -> None:
    """Count a slow or failed custody call of the operation in progress."""

    calls = _CUSTODY_CALLS.get()
    if calls is not None:
        calls[1] += 1


def current() -> tuple[str, str]:
    """``(profile tag, correlation)`` of the token operation in progress, or ``("-", "-")``."""

    return _CORRELATION.get()


def kind_of(failure_code: str) -> str:
    if failure_code == "oauth_metadata_request_failed":
        return "metadata"
    if failure_code == "oauth_token_request_failed":
        return "token"
    return failure_code.removeprefix("oauth_").removesuffix("_failed") or "-"


# The library's step names, by the phase they count toward.
_PHASE_OF_STEP = {
    "connect_tcp": "connect",
    "start_tls": "tls",
    "send_request_headers": "send",
    "send_request_body": "send",
    "receive_response_headers": "wait",
    "receive_response_body": "read",
}
PHASES = ("connect", "tls", "send", "wait", "read")


class RequestPhases:
    """Where one request's client-side time went, from the HTTP library's trace events.

    ``trace`` is the library's ``trace`` request extension: it is called with
    ``<layer>.<step>.started`` and ``<layer>.<step>.complete`` or ``.failed``.
    """

    def __init__(self, started: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._started = started
        self._clock = clock
        self._first_step_at: float | None = None
        self._open: dict[str, float] = {}
        self.seconds: dict[str, float] = {}
        self.failed_at = "-"

    async def trace(self, event: str, info: Mapping[str, Any]) -> None:
        self.observe(event)

    def observe(self, event: str) -> None:
        step, _, edge = event.rpartition(".")
        phase = _PHASE_OF_STEP.get(step.rpartition(".")[2])
        if phase is None:
            return
        now = self._clock()
        if self._first_step_at is None:
            self._first_step_at = now
        if edge == "started":
            self._open[step] = now
            return
        began = self._open.pop(step, None)
        if began is not None:
            self.seconds[phase] = self.seconds.get(phase, 0.0) + (now - began)
        if edge == "failed" and self.failed_at == "-":
            self.failed_at = phase

    def queue_seconds(self) -> float | None:
        """Start to first network step; ``None`` when the request never reached one."""

        if self._first_step_at is None:
            return None
        return self._first_step_at - self._started


def _ms(seconds: float | None) -> str:
    return "-" if seconds is None else str(int(round(seconds * 1000)))


def _phase_fields(phases: RequestPhases | None) -> str:
    if phases is None:
        return "queue_ms=- " + " ".join(f"{name}_ms=-" for name in PHASES) + " failed_at=-"
    return (
        f"queue_ms={_ms(phases.queue_seconds())} "
        + " ".join(f"{name}_ms={_ms(phases.seconds.get(name))}" for name in PHASES)
        + f" failed_at={phases.failed_at}"
    )


def record(
    *,
    request_id: str,
    kind: str,
    method: str,
    status: int | None,
    outcome: str,
    elapsed_seconds: float,
    phases: RequestPhases | None = None,
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
        "status=%s outcome=%s elapsed_ms=%d %s pid=%d task=%s",
        request_id,
        correlation,
        profile,
        kind,
        method,
        "-" if status is None else int(status),
        outcome,
        int(round(elapsed_seconds * 1000)),
        _phase_fields(phases),
        os.getpid(),
        lock_spans.task_tag(),
    )

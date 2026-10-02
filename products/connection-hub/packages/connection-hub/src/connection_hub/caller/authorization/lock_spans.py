"""Redacted timing for OAuth profile locks, credential custody and refresh (W461).

On 2026-10-02 relay channels on one host failed with
``oauth_profile_lock_timeout`` for hours. Two different locks raise that
code: the store-wide ``.oauth.transaction.lock`` and the per-profile
``.oauth.refresh.lock``. Nothing recorded how long either was waited for or
held, by which process or task, or how long the credential custody and the
token refresh inside them took. So no holder could be named, and a waiter
starved by its own event loop looked the same as one blocked by a real
holder.

Each span records its kind (``transaction``, ``refresh_slot``, ``custody``,
``refresh``), the operation it served, a short hash of the profile name, the
wait and hold (or run) time in milliseconds, the outcome, the process id and
the asyncio task name. It never records a token, a credential, a URL or a
profile name in clear. Failures and timeouts log at WARNING, spans of
``SLOW_SECONDS`` or longer at INFO, and every other span at DEBUG, so a
healthy relay adds no lines at the default level.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time

from connection_hub.caller.errors import AuthorizationError, CredentialError, ProfileError

logger = logging.getLogger("connection_hub.oauth.spans")

SLOW_SECONDS = 0.25
HOLD_WARN_SECONDS = 2.0


def profile_tag(profile_name: str) -> str:
    """A stable, non-reversible tag for a profile name."""

    return hashlib.sha256(str(profile_name or "").encode("utf-8")).hexdigest()[:12]


def _task_tag() -> str:
    """An opaque tag for the current task: correlates spans, carries no caller text."""

    try:
        task = asyncio.current_task()
    except RuntimeError:
        return "-"
    return "-" if task is None else f"t{id(task) & 0xFFFFFF:06x}"


def _ms(seconds: float | None) -> str:
    return "-" if seconds is None else str(int(round(seconds * 1000)))


def record(
    kind: str,
    *,
    operation: str,
    profile_name: str,
    outcome: str,
    wait_seconds: float | None = None,
    hold_seconds: float | None = None,
    task_tag: str | None = None,
) -> None:
    """Log one span: WARNING on failure, INFO when slow, DEBUG otherwise."""

    slow = any(
        value is not None and value >= SLOW_SECONDS
        for value in (wait_seconds, hold_seconds)
    )
    if outcome != "ok":
        level = logging.WARNING
    elif slow:
        level = logging.INFO
    else:
        level = logging.DEBUG
    if not logger.isEnabledFor(level):
        return
    logger.log(
        level,
        "Connection Hub OAuth span kind=%s operation=%s profile=%s outcome=%s "
        "wait_ms=%s hold_ms=%s pid=%d task=%s corr=%s",
        kind,
        operation or "-",
        profile_tag(profile_name),
        outcome,
        _ms(wait_seconds),
        _ms(hold_seconds),
        os.getpid(),
        task_tag or _task_tag(),
        _correlation(),
    )


_CALL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,39}$")


def custody_call_name(call: object) -> str:
    """The code name of a custody callable (an identifier), or ``other``; never its arguments."""

    name = getattr(call, "__name__", "")
    return name if isinstance(name, str) and _CALL_NAME.match(name) else "other"


def record_custody_call(
    call_name: str,
    *,
    seq: int | None,
    outcome: str,
    queue_seconds: float | None,
    run_seconds: float | None,
    resume_seconds: float | None,
    task_tag: str | None = None,
) -> None:
    """Log one credential custody call (W464): where its time went between the loop and the custody thread.

    ``queue`` is from submission to the thread starting it (the single custody
    thread busy with earlier calls), ``run`` the call itself on the thread
    (the native store and the work around it), ``resume`` from the call's end
    to its awaiting coroutine running again (a busy or stalled event loop).
    ``seq`` numbers the calls of one token operation. The operation's exact
    count is ``record_custody_total``, logged when it ends, because at the
    default level only slow or failed calls appear here. WARNING when not ok,
    INFO when any part is SLOW_SECONDS or longer, DEBUG otherwise.
    """

    slow = any(
        value is not None and value >= SLOW_SECONDS
        for value in (queue_seconds, run_seconds, resume_seconds)
    )
    from connection_hub.caller.authorization import request_records

    if outcome != "ok":
        level = logging.WARNING
    elif slow:
        level = logging.INFO
    else:
        level = logging.DEBUG
    if level > logging.DEBUG:
        request_records.note_notable_custody_call()
    if not logger.isEnabledFor(level):
        return
    profile, correlation = request_records.current()
    logger.log(
        level,
        "Connection Hub OAuth custody call call=%s seq=%s profile=%s outcome=%s "
        "queue_ms=%s run_ms=%s resume_ms=%s pid=%d task=%s corr=%s",
        call_name,
        "-" if seq is None else seq,
        profile,
        outcome,
        _ms(queue_seconds),
        _ms(run_seconds),
        _ms(resume_seconds),
        os.getpid(),
        task_tag or _task_tag(),
        correlation,
    )


def record_custody_total(*, calls: int, notable: int) -> None:
    """Log a token operation's exact custody call count when it ends.

    INFO when any of its calls was slow or failed, so the count stands beside
    those call records at the default level; DEBUG otherwise.
    """

    level = logging.INFO if notable else logging.DEBUG
    if not logger.isEnabledFor(level):
        return
    from connection_hub.caller.authorization import request_records

    profile, correlation = request_records.current()
    logger.log(
        level,
        "Connection Hub OAuth custody calls total=%d notable=%d profile=%s pid=%d task=%s corr=%s",
        calls,
        notable,
        profile,
        os.getpid(),
        _task_tag(),
        correlation,
    )


# The opaque task tag, also for the OAuth request records.
task_tag = _task_tag


def _correlation() -> str:
    """The token operation's correlation (request_records), shared with its HTTP request records."""

    from connection_hub.caller.authorization import request_records

    return request_records.current()[1]


def watch_hold(
    kind: str,
    *,
    operation: str,
    profile_name: str,
    wait_seconds: float,
    acquired_at: float,
) -> asyncio.TimerHandle | None:
    """Record a ``holding`` span if the lock is still held after HOLD_WARN_SECONDS.

    The caller cancels the returned handle on release. Without a running
    loop nothing is scheduled.
    """

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    task_tag = _task_tag()

    def still_held() -> None:
        record(
            kind,
            operation=operation,
            profile_name=profile_name,
            outcome="holding",
            wait_seconds=wait_seconds,
            hold_seconds=time.monotonic() - acquired_at,
            task_tag=task_tag,
        )

    return loop.call_later(HOLD_WARN_SECONDS, still_held)


_OUTCOME_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def outcome_of(exc: BaseException | None) -> str:
    """The span outcome for an exception raised inside a span, or ok.

    Never raises and never logs caller text: an error ``code`` is used only
    when it is a lowercase identifier (the product's error codes), else the
    exception class name when that is an identifier, else ``error`` (W464
    review: a ``code`` getter that raised replaced the original exception,
    and free text in ``code`` reached the log).
    """

    if exc is None:
        return "ok"
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled"
    # A code is trusted only from Connection Hub's own error types, whose
    # codes are constants in this code base. Any other exception, a provider's
    # included, is named by its class: identifier syntax alone does not make
    # an attribute safe to log (W464 review, a canary in `code`).
    if isinstance(exc, (AuthorizationError, ProfileError, CredentialError)):
        try:
            code = exc.code
        except BaseException as broken:  # noqa: BLE001 - a diagnostic read never escapes
            if isinstance(broken, (KeyboardInterrupt, SystemExit)):
                raise
            code = None
        if isinstance(code, str) and _OUTCOME_CODE.match(code):
            return code
    name = type(exc).__name__
    return name if _CALL_NAME.match(name) else "error"

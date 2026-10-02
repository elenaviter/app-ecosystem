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

logger = logging.getLogger("connection_hub.oauth.spans")

SLOW_SECONDS = 0.25


def profile_tag(profile_name: str) -> str:
    """A stable, non-reversible tag for a profile name."""

    return hashlib.sha256(str(profile_name or "").encode("utf-8")).hexdigest()[:12]


def _task_name() -> str:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        return "-"
    return task.get_name() if task is not None else "-"


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
        "wait_ms=%s hold_ms=%s pid=%d task=%s",
        kind,
        operation or "-",
        profile_tag(profile_name),
        outcome,
        _ms(wait_seconds),
        _ms(hold_seconds),
        os.getpid(),
        _task_name(),
    )


def outcome_of(exc: BaseException | None) -> str:
    """The span outcome for an exception raised inside a span, or ok."""

    if exc is None:
        return "ok"
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled"
    code = getattr(exc, "code", "")
    return str(code) if code else type(exc).__name__

"""Per-operation counters and phase timings for the board's measurement harness (W819, for W812).

W812 Option S (team vote 6/6, 2026-10-13): the D-lite counters cover the member-poll path on Connection Hub's
side too: the person control read and the secret resolves of the card-transaction participant callers. Counters
only. Nothing here caches, skips or changes what is read or decided.

With no observer set (the default) every call is a cheap no-op. A harness sets one observer for the process;
each Hub operation scope then hands it one snapshot when it ends::

    {"surface": "hub", "caller": "<hub operation>", "counts": {...}, "phases_ms": {...},
     "wall_ms": float, "cpu_ms": float}

A snapshot names the operation and counts only: never a principal, Card, key, secret or request id.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import logging
import re
import time
from typing import Any, Callable, Iterator

LOGGER = logging.getLogger("connection_hub.request_counters")

COUNT_NAMES = frozenset({
    "person_control_read", "read_authorize", "edit_authorize", "view",
    "participant_callers_build", "secret_resolve",
})
_CALLER = re.compile(r"[a-z][a-z0-9_.]{0,63}")

Observer = Callable[[dict[str, Any]], None]
_OBSERVER: Observer | None = None
_SCOPE: contextvars.ContextVar["_Scope | None"] = contextvars.ContextVar("hub_request_counters", default=None)


class _Scope:
    __slots__ = ("caller", "counts", "phases_ms", "wall", "cpu")

    def __init__(self, caller: str) -> None:
        self.caller = caller
        self.counts: dict[str, int] = {}
        self.phases_ms: dict[str, float] = {}
        self.wall = time.monotonic()
        self.cpu = time.process_time()

    def snapshot(self) -> dict[str, Any]:
        return {"surface": "hub", "caller": self.caller, "counts": dict(self.counts),
                "phases_ms": {name: round(ms, 3) for name, ms in self.phases_ms.items()},
                "wall_ms": round((time.monotonic() - self.wall) * 1000.0, 3),
                "cpu_ms": round((time.process_time() - self.cpu) * 1000.0, 3)}


def set_observer(observer: Observer | None) -> None:
    """Install the harness's observer for this process; None switches counting off."""
    global _OBSERVER
    _OBSERVER = observer


@contextlib.contextmanager
def operation_scope(caller: str) -> Iterator[None]:
    """Count one Hub operation. A scope already open on this task keeps counting; a nested one adds nothing."""
    if _OBSERVER is None or _SCOPE.get() is not None or not _CALLER.fullmatch(caller or ""):
        yield
        return
    scope = _Scope(caller)
    token = _SCOPE.set(scope)
    try:
        yield
    finally:
        _SCOPE.reset(token)
        observer = _OBSERVER
        if observer is not None:
            try:
                observer(scope.snapshot())
            except Exception:  # noqa: BLE001 - a harness failure never reaches the operation
                LOGGER.warning("request counters observer failed caller=%s", scope.caller)


def count(name: str, amount: int = 1) -> None:
    """One actual call of ``name`` in the current operation; outside a scope, nothing happens."""
    scope = _SCOPE.get()
    if scope is not None and name in COUNT_NAMES:
        scope.counts[name] = scope.counts.get(name, 0) + amount


def phase(name: str, elapsed_ms: float) -> None:
    """Add a phase's elapsed milliseconds (and count it) in the current operation."""
    scope = _SCOPE.get()
    if scope is not None and name in COUNT_NAMES:
        scope.counts[name] = scope.counts.get(name, 0) + 1
        scope.phases_ms[name] = scope.phases_ms.get(name, 0.0) + float(elapsed_ms)


def counted(caller: str):
    """Decorate an async Hub operation so its whole run is one counted scope (signature kept)."""
    def wrap(fn):
        @functools.wraps(fn)
        async def run(*args: Any, **kwargs: Any) -> Any:
            with operation_scope(caller):
                return await fn(*args, **kwargs)
        return run
    return wrap


__all__ = ["COUNT_NAMES", "count", "counted", "operation_scope", "phase", "set_observer"]

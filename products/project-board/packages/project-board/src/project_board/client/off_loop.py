"""Synchronous store calls the relay's event loop does not wait on (W456).

Every agent channel on a host shares one event loop: its Data Bus sockets
answer the server's ping there, its heartbeats and reopenings run there, and
so does every other channel's. After the fd7c266 activation on 2026-10-02 the
loop watchdog named the local session wake as the remaining blocker: a
pending-mail ``read_json`` held the loop 15.8 s, the loop lagged 23.6 s, and
every socket of the host was closed by the server's 45 s read timeout because
none could answer a ping.

``run_off_loop`` runs one synchronous call in a worker thread and keeps the
loop free while the file system is slow. ``ChannelExecutors`` gives each
channel its own single thread for these calls: a mailbox that hangs then holds
its own channel's thread, and neither another channel's calls nor the
process-wide default pool (coordinate queue scans, local-work scans, session
queue subprocesses) wait behind it.

A thread cannot be stopped: when the awaiting task is cancelled while the call
runs, the call finishes anyway. So the cancellation waits for that call to end
before it goes on. The caller's locks (a channel's notify lock) are then still
held until its write is done, and the next notification for the channel never
overlaps a write of this one. A call, once requested, runs to its end exactly
as the synchronous call did, and the writes happen in the order the
synchronous code made them. The wait ends when the call ends: a read the
operating system never completes holds its channel, and the process exit, as
any thread in the default pool would.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import Executor, ThreadPoolExecutor
from typing import Any, TypeVar

T = TypeVar("T")


# W461: a relay channel turn sets this to receive how long each of its calls
# waited for its executor thread and how long it ran there. The value is
# carried by the task's context (and by ``copy_context`` into the thread), so
# a call outside a turn reports nothing. It changes nothing about how a call is
# awaited or cancelled.
#
# W448: the observer also receives the call's label and, when the call waited
# at least HOLDER_MIN_WAIT_SECONDS, the holders: the calls that ran in the same
# thread while it waited, as (label, seconds) pairs, longest first. Each
# channel's store executor has one thread, so those are exactly the calls the
# wait was spent behind, whichever task (wake, coordinate drain, background
# refresh) submitted them.
OFF_LOOP_OBSERVER: contextvars.ContextVar[Callable[..., None] | None] = (
    contextvars.ContextVar("problem_board_off_loop_observer", default=None)
)

HOLDER_MIN_WAIT_SECONDS = 0.5
HOLDER_HISTORY = 16
HOLDERS_REPORTED = 3
LABEL_MAX_CHARS = 96
_LABEL_PATTERN = re.compile(r"[A-Za-z0-9_.<>]+")

# Per executor thread: (label, started, ended) of its latest calls, oldest first.
_THREAD_HISTORY = threading.local()


def call_label(call: Callable[..., Any]) -> str:
    """``module.Qualname`` of a callable from source names only; anything else is ``other``."""

    while isinstance(call, functools.partial):
        call = call.func
    target = getattr(call, "__func__", call)
    module = str(getattr(target, "__module__", "") or "").rsplit(".", 1)[-1]
    name = str(getattr(target, "__qualname__", "") or type(call).__qualname__)
    label = f"{module}.{name}" if module else name
    if not _LABEL_PATTERN.fullmatch(label):
        return "other"
    # Keep the end: the method name, not the module, is what tells calls apart.
    return label[-LABEL_MAX_CHARS:]


def _history() -> deque[tuple[str, float, float]]:
    history = getattr(_THREAD_HISTORY, "calls", None)
    if history is None:
        history = deque(maxlen=HOLDER_HISTORY)
        _THREAD_HISTORY.calls = history
    return history


def _holders(submitted: float, started: float) -> list[tuple[str, float]]:
    """Calls this thread ran between ``submitted`` and ``started``, by seconds held."""

    held: dict[str, float] = {}
    for label, began, ended in _history():
        overlap = min(ended, started) - max(began, submitted)
        if overlap > 0:
            held[label] = held.get(label, 0.0) + overlap
    return sorted(held.items(), key=lambda item: item[1], reverse=True)[:HOLDERS_REPORTED]


def _observe(
    observer: Callable[..., None] | None,
    submitted: float,
    started: list[float],
    label: str,
    holders: list[tuple[str, float]],
) -> None:
    if observer is None or not started:
        return
    try:
        observer(
            max(0.0, started[0] - submitted),
            max(0.0, time.monotonic() - started[0]),
            label,
            holders,
        )
    except Exception:  # noqa: BLE001 - timing evidence never fails the call it measured
        pass


async def run_off_loop(
    call: Callable[..., T],
    /,
    *args: Any,
    executor: Executor | None = None,
    **kwargs: Any,
) -> T:
    """``call(*args, **kwargs)`` in ``executor`` (default: the loop's pool); a cancellation waits for it."""

    loop = asyncio.get_running_loop()
    context = contextvars.copy_context()
    observer = OFF_LOOP_OBSERVER.get()
    started: list[float] = []
    holders: list[tuple[str, float]] = []
    label = call_label(call)

    def timed() -> T:
        began = time.monotonic()
        started.append(began)
        if began - submitted >= HOLDER_MIN_WAIT_SECONDS:
            holders.extend(_holders(submitted, began))
        try:
            return call(*args, **kwargs)
        finally:
            _history().append((label, began, time.monotonic()))

    submitted = time.monotonic()
    future = loop.run_in_executor(executor, functools.partial(context.run, timed))
    try:
        result = await asyncio.shield(future)
    except asyncio.CancelledError:
        while not future.done():
            try:
                await asyncio.wait({future})
            except asyncio.CancelledError:
                continue
        if not future.cancelled():
            # Retrieved so a failure of the abandoned call is not reported as
            # never retrieved. The caller was cancelled and acts on nothing.
            future.exception()
        raise
    except BaseException:
        _observe(observer, submitted, started, label, holders)
        raise
    _observe(observer, submitted, started, label, holders)
    return result


class ChannelExecutors:
    """One single-thread executor per channel, made on first use."""

    def __init__(self, *, thread_name_prefix: str = "problem-board-store") -> None:
        self._prefix = thread_name_prefix
        self._executors: dict[str, ThreadPoolExecutor] = {}

    def for_channel(self, worker_name: str) -> ThreadPoolExecutor:
        executor = self._executors.get(worker_name)
        if executor is None:
            executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix=f"{self._prefix}-{worker_name}"
            )
            self._executors[worker_name] = executor
        return executor

    def shutdown(self) -> None:
        """Stop taking calls; a call that is running finishes in its thread."""

        executors, self._executors = self._executors, {}
        for executor in executors.values():
            executor.shutdown(wait=False, cancel_futures=True)

"""The relay's scans for locally raised work, one at a time (W459).

``wait_for_wakeup`` watches for work raised on this machine (an outbox row, an
operator response, a coordinate request, a listener change) beside push. Its
scans read a field that held 70,009 files on dev-main, and they run off the
event loop. A wait ends as soon as any of its waiters fires and cancels the
others, but a scan already running in a thread cannot be stopped. Until
2026-10-02 every new wait then started new scans beside the abandoned ones,
and on dev-main all 20 default-pool threads were found in directory scans,
most of their time waiting for the interpreter lock.

``LocalWorkScanner`` runs these scans in one thread of its own. A wait that
asks for a scan already running joins it instead of starting another, and a
cancelled wait only stops waiting: its scan runs to its end, and its result
serves the next wait. At most one scan runs at a time, and at most one waits
per kind. A joined result can predate the caller by one scan; a change made
during that scan is seen by the next one, a quarter second later.

The scans are pure reads with no lock and no claim, so closing the relay does
not wait for one: ``close`` refuses new scans and lets a running scan finish
in its thread. Closing is final. A wait still running when the relay closes
gets ``LocalWorkScannerClosed`` from its next scan, which ends that wait the
way a failed scan does. A scan queued before the close ends the same way in
the thread without scanning, so no scan starts after the close.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable, Hashable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

T = TypeVar("T")


class LocalWorkScannerClosed(RuntimeError):
    """The relay closed its local-work scanner; no scan runs after that."""


class LocalWorkScanner:
    """One thread for the relay's local-work scans; a scan of the same kind is joined."""

    def __init__(self, *, thread_name: str = "problem-board-local-work") -> None:
        self._thread_name = thread_name
        self._executor: ThreadPoolExecutor | None = None
        self._in_flight: dict[Hashable, asyncio.Future] = {}
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def scan(self, kind: Hashable, call: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        """``call(*args, **kwargs)`` in the scanner thread, or the running scan of ``kind``."""

        if self._closed:
            raise LocalWorkScannerClosed("The relay closed its local-work scanner.")
        future = self._in_flight.get(kind)
        if future is None or future.done():
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix=self._thread_name
                )
            future = asyncio.get_running_loop().run_in_executor(
                self._executor, functools.partial(self._unless_closed, call, args, kwargs)
            )
            self._in_flight[kind] = future
            future.add_done_callback(functools.partial(self._finished, kind))
        # A waiter that is itself cancelled stays cancelled: shield keeps the
        # scan, and its result serves the next wait.
        return await asyncio.shield(future)

    def _unless_closed(self, call: Callable[..., T], args: tuple, kwargs: dict) -> T:
        """In the scanner thread: a scan queued before close() never runs."""

        if self._closed:
            raise LocalWorkScannerClosed("The relay closed its local-work scanner.")
        return call(*args, **kwargs)

    def _finished(self, kind: Hashable, future: asyncio.Future) -> None:
        if self._in_flight.get(kind) is future:
            del self._in_flight[kind]
        if not future.cancelled():
            # Retrieved here: a scan whose every waiter was cancelled must not
            # be reported as an exception never retrieved.
            future.exception()

    def close(self) -> None:
        """Refuse every later scan; a scan that is running finishes in its thread."""

        self._closed = True
        executor, self._executor = self._executor, None
        self._in_flight = {}
        if executor is not None:
            # Queued scans are not cancelled: each sees the closed flag in the
            # thread and ends its waiters as closed, so a waiter is never told
            # it was cancelled when it was not (and no Python 3.11 API is needed).
            executor.shutdown(wait=False)

"""One daemon thread that runs slow filesystem walks one at a time (W461).

On 2026-10-02 the dev-main relay ran at 94% CPU with about twenty threads in
``lstat`` and ``getdirentries``: every channel measured its agent's whole
workspace (W423) every 15 minutes with ``asyncio.to_thread``, so up to one
walk per channel ran at once on the event loop's default executor. That same
executor runs the OAuth profile file-lock acquires and the DNS lookups of
every channel, and with it saturated a free lock or a lookup waited about
6 s, enough to turn into ``oauth_profile_lock_timeout`` and connect
timeouts.

Walks submitted here run in submission order on one daemon thread of their
own, so they never occupy the default executor and never run in parallel
with each other. A daemon thread never delays process exit.
"""

from __future__ import annotations

import asyncio
import queue
import threading
from typing import Any, Callable, TypeVar

T = TypeVar("T")


def _settle(future: asyncio.Future[Any], result: Any, error: BaseException | None) -> None:
    if future.done():
        return  # the awaiting task was cancelled
    if error is not None:
        future.set_exception(error)
    else:
        future.set_result(result)


class SerialWalker:
    """Run blocking callables one at a time on a dedicated daemon thread."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._jobs: queue.SimpleQueue[tuple[Callable[..., Any], tuple[Any, ...], asyncio.AbstractEventLoop, asyncio.Future[Any]]] = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()

    def _ensure_thread(self) -> None:
        with self._start_lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
                self._thread.start()

    def _run(self) -> None:
        while True:
            func, args, loop, future = self._jobs.get()
            if future.done():
                continue  # cancelled while queued: skip the walk
            try:
                result, error = func(*args), None
            except BaseException as exc:  # noqa: BLE001 - handed to the awaiting task
                result, error = None, exc
            try:
                loop.call_soon_threadsafe(_settle, future, result, error)
            except RuntimeError:
                pass  # the loop closed while the walk ran

    def queued(self) -> int:
        """Walks waiting for the thread (approximate)."""

        return self._jobs.qsize()

    async def run(self, func: Callable[..., T], *args: Any) -> T:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = loop.create_future()
        self._ensure_thread()
        self._jobs.put((func, args, loop, future))
        return await future


__all__ = ["SerialWalker"]

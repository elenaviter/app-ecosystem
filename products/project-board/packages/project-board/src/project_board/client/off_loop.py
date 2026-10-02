"""Synchronous store calls the relay's event loop does not wait on (W456).

Every agent channel on a host shares one event loop: its Data Bus sockets
answer the server's ping there, its heartbeats and reopenings run there, and
so does every other channel's. After the fd7c266 activation on 2026-10-02 the
loop watchdog named the local session wake as the remaining blocker: a
pending-mail ``read_json`` held the loop 15.8 s, the loop lagged 23.6 s, and
every socket of the host was closed by the server's 45 s read timeout because
none could answer a ping.

``run_off_loop`` runs one synchronous call in a worker thread and keeps the
loop free while the file system is slow. A thread cannot be stopped: when the
awaiting task is cancelled while the call runs, the call finishes anyway. So
the cancellation waits for that call to end before it goes on. The caller's
locks (a channel's notify lock) are then still held until its write is done,
and the next notification for the channel never overlaps a write of this one.
A call, once requested, runs to its end exactly as the synchronous call did,
and the writes happen in the order the synchronous code made them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")


async def run_off_loop(call: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """``call(*args, **kwargs)`` in a worker thread; a cancellation waits for it."""

    task = asyncio.ensure_future(asyncio.to_thread(call, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.wait({task})
            except asyncio.CancelledError:
                continue
        if not task.cancelled():
            # Retrieved so a failure of the abandoned call is not reported as
            # never retrieved. The caller was cancelled and acts on nothing.
            task.exception()
        raise

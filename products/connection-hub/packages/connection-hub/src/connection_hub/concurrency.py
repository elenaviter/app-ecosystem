"""Run independent reads together, a few at a time (W419).

A listing that awaits one read per Card in turn takes the sum of their
latencies; gathered with a small bound it takes about the longest of them,
without opening an unbounded number of reads against one backend at once.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Iterable, TypeVar

T = TypeVar("T")

DEFAULT_LIMIT = 8


async def bounded_gather(awaitables: Iterable[Awaitable[T]], *, limit: int = DEFAULT_LIMIT) -> list[T]:
    """Await all, at most ``limit`` at once; results in input order.

    The first exception propagates, as it did when the reads ran in turn.
    """

    gate = asyncio.Semaphore(max(1, int(limit)))

    async def one(awaitable: Awaitable[T]) -> T:
        async with gate:
            return await awaitable

    return list(await asyncio.gather(*(one(awaitable) for awaitable in awaitables)))

"""The PG-mode test pool stand-in (conftest._LazyPool): one connection per loop, never leaked (Infra's questions).

1. Concurrent first acquires in one loop share one lock and open ONE connection; none is overwritten and left open.
2. Two pools used on the same loop are both closed when that loop closes (the close() wrapping is per pool).
"""
import asyncio
import os

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("W661_TEST_PG_DSN"), reason="W661_TEST_PG_DSN is not set")

import conftest


def test_concurrent_first_acquires_open_one_connection():
    pool = conftest._LazyPool(os.environ["W661_TEST_PG_DSN"])
    seen = []

    async def use():
        async with pool.acquire() as connection:
            seen.append(connection)
            await connection.fetchval("SELECT 1")

    async def main():
        await asyncio.gather(*(use() for _ in range(8)))
        return next(iter(pool._per_loop.values()))[2]

    loop = asyncio.new_event_loop()
    try:
        connection = loop.run_until_complete(main())
        assert len({id(item) for item in seen}) == 1 and seen[0] is connection
    finally:
        loop.close()
    assert connection.is_closed()  # closed inside the loop's own close()


def test_two_pools_on_one_loop_are_both_closed_with_it():
    first = conftest._LazyPool(os.environ["W661_TEST_PG_DSN"])
    second = conftest._LazyPool(os.environ["W661_TEST_PG_DSN"])

    async def main():
        opened = []
        for pool in (first, second):
            async with pool.acquire() as connection:
                opened.append(connection)
        return opened

    loop = asyncio.new_event_loop()
    try:
        opened = loop.run_until_complete(main())
        assert opened[0] is not opened[1]
    finally:
        loop.close()
    assert all(connection.is_closed() for connection in opened)

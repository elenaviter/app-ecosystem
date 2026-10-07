"""W578: the shared account-record lock on a real Redis: exclusive across holders, never yields unheld."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from connection_hub.delegated_to_kdcube.account_lock import RedisAccountLock, account_lock_prefix
from connection_hub.delegated_to_kdcube.store import AccountLockUnavailable

pytestmark = pytest.mark.skipif(not os.environ.get("REDIS_URL"), reason="needs a disposable Redis (REDIS_URL)")


def _client():
    import redis.asyncio as aioredis
    return aioredis.from_url(os.environ["REDIS_URL"])


@pytest.mark.asyncio
async def test_two_holders_of_one_account_never_overlap_and_other_accounts_do_not_wait():
    redis = _client()
    try:
        prefix = account_lock_prefix("t", f"p-{uuid.uuid4().hex}")
        first, second = RedisAccountLock(redis, prefix=prefix), RedisAccountLock(redis, prefix=prefix)
        inside, order = asyncio.Event(), []

        async def holder():
            async with first("user-1", "account-1"):
                order.append("first-in")
                inside.set()
                await asyncio.sleep(0.2)
                order.append("first-out")

        task = asyncio.create_task(holder())
        await inside.wait()
        async with second("user-1", "account-2"):  # another account: no wait
            order.append("other-account")
        async with second("user-1", "account-1"):
            order.append("second-in")
        await task
        assert order == ["first-in", "other-account", "first-out", "second-in"]
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_a_wait_that_times_out_refuses_and_never_runs_the_section():
    redis = _client()
    try:
        prefix = account_lock_prefix("t", f"p-{uuid.uuid4().hex}")
        holder = RedisAccountLock(redis, prefix=prefix)
        waiter = RedisAccountLock(redis, prefix=prefix, wait_seconds=0.2)
        ran = []
        async with holder("user-1", "account-1"):
            with pytest.raises(AccountLockUnavailable, match="account_lock_timeout"):
                async with waiter("user-1", "account-1"):
                    ran.append(True)
        assert ran == []
    finally:
        await redis.aclose()

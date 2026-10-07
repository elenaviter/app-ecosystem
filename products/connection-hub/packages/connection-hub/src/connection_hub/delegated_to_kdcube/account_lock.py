# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W578: the shared mutation lock of one connected-account record, on Redis.

Every process that writes a user's account records (the Connection Hub app
and every bundle that reaches them through the SDK client, on any host)
serializes on one Redis key per (user, account): a disconnect's incarnation
hold, its deletion, a reconnect, a status write and a refreshed credential
never interleave. The same mechanism as the refresh lock (W371): a token-owned
key set with NX and an expiry, released only by its holder. Unlike that lock
it never yields without holding: a wait that times out raises
``AccountLockUnavailable`` and the write does not happen.

The expiry bounds a holder that dies; a section that runs longer than the
expiry minus a margin is interrupted (TimeoutError), never left running unlocked.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from .store import AccountLockUnavailable

ACCOUNT_LOCK_TTL_SECONDS = 30
ACCOUNT_LOCK_WAIT_SECONDS = 10.0
# The section is interrupted this long before its key could expire, so it
# never runs on after another holder could enter (claude-main, S3b P2-2).
ACCOUNT_LOCK_MARGIN_SECONDS = 5.0
_RELEASE_SCRIPT = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
)


class RedisAccountLock:
    """``lock(user_id, account_id)``: an async context manager holding that account record's key."""

    def __init__(self, redis: Any, *, prefix: str, ttl_seconds: int = ACCOUNT_LOCK_TTL_SECONDS,
                 wait_seconds: float = ACCOUNT_LOCK_WAIT_SECONDS, poll_seconds: float = 0.05) -> None:
        if redis is None or not prefix:
            raise ValueError("a Redis client and a key prefix are required")
        self.redis, self.prefix = redis, prefix
        self.ttl_ms = int(ttl_seconds * 1000)
        self.section_seconds = max(0.1, ttl_seconds - ACCOUNT_LOCK_MARGIN_SECONDS)
        self.wait_seconds, self.poll_seconds = wait_seconds, poll_seconds

    def key(self, user_id: str, account_id: str) -> str:
        digest = hashlib.sha256(f"{user_id}\0{account_id}".encode("utf-8")).hexdigest()
        return f"{self.prefix}{digest}"

    @asynccontextmanager
    async def __call__(self, user_id: str, account_id: str) -> AsyncIterator[None]:
        name = self.key(user_id, account_id)
        token = secrets.token_hex(16)
        deadline = time.monotonic() + self.wait_seconds
        while not await self.redis.set(name, token, nx=True, px=self.ttl_ms):
            if time.monotonic() >= deadline:
                raise AccountLockUnavailable("account_lock_timeout")
            await asyncio.sleep(self.poll_seconds)
        try:
            async with asyncio.timeout(self.section_seconds):
                yield
        finally:
            await self.redis.eval(_RELEASE_SCRIPT, 1, name, token)


def account_lock_prefix(tenant: str, project: str) -> str:
    """The one key prefix the app and the SDK client both use, per tenant and project."""
    return f"connection-hub:{tenant}:{project}:account-lock:"


__all__ = ["ACCOUNT_LOCK_MARGIN_SECONDS", "ACCOUNT_LOCK_TTL_SECONDS", "ACCOUNT_LOCK_WAIT_SECONDS", "RedisAccountLock", "account_lock_prefix"]

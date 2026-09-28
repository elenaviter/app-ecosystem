# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""One refresh at a time per connected-account credential.

Some providers rotate refresh tokens: GitHub Apps issue a new refresh token on
every refresh, and "once you use a refresh token, that refresh token and the
old user access token will no longer work". Two requests for the same
person's account that both refresh would leave one of them holding a dead
refresh token and would lose the connection. The broker therefore refreshes
under this lock, re-reads the stored credential once it holds it, and uses a
refresh another holder already made instead of refreshing again.

``LocalRefreshLock`` serializes within one process. ``RedisRefreshLock``
serializes across the processes that share a Redis (the Connection Hub
workers of one deployment): SET NX PX with a random token, released only by
its holder.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Protocol

REFRESH_LOCK_TTL_SECONDS = 60
REFRESH_LOCK_WAIT_SECONDS = 45.0
_RELEASE_SCRIPT = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
)


class RefreshLock(Protocol):
    """Hold the refresh of one credential; yields False when it could not be held in time."""

    def hold(self, key: str) -> Any: ...


class LocalRefreshLock:
    """Per-key asyncio locks for one process."""

    def __init__(self, *, wait_seconds: float = REFRESH_LOCK_WAIT_SECONDS) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self.wait_seconds = wait_seconds

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[bool]:
        lock = self._locks.setdefault(key, asyncio.Lock())
        try:
            await asyncio.wait_for(lock.acquire(), timeout=self.wait_seconds)
        except asyncio.TimeoutError:
            yield False
            return
        try:
            yield True
        finally:
            lock.release()


class RedisRefreshLock:
    """A lock shared by every process on one Redis; the key expires if its holder dies."""

    def __init__(
        self,
        redis: Any,
        *,
        prefix: str = "connection_hub:refresh_lock:",
        ttl_seconds: int = REFRESH_LOCK_TTL_SECONDS,
        wait_seconds: float = REFRESH_LOCK_WAIT_SECONDS,
        poll_seconds: float = 0.2,
    ) -> None:
        self.redis = redis
        self.prefix = prefix
        self.ttl_ms = int(ttl_seconds * 1000)
        self.wait_seconds = wait_seconds
        self.poll_seconds = poll_seconds

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[bool]:
        name = f"{self.prefix}{key}"
        token = secrets.token_hex(16)
        deadline = time.monotonic() + self.wait_seconds
        acquired = False
        while True:
            if await self.redis.set(name, token, nx=True, px=self.ttl_ms):
                acquired = True
                break
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(self.poll_seconds)
        try:
            yield acquired
        finally:
            if acquired:
                await self.redis.eval(_RELEASE_SCRIPT, 1, name, token)


_DEFAULT_LOCK = LocalRefreshLock()


def default_refresh_lock() -> LocalRefreshLock:
    """The process-wide lock used when a host provides no shared one."""

    return _DEFAULT_LOCK


__all__ = [
    "LocalRefreshLock",
    "REFRESH_LOCK_TTL_SECONDS",
    "REFRESH_LOCK_WAIT_SECONDS",
    "RedisRefreshLock",
    "RefreshLock",
    "default_refresh_lock",
]

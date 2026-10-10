# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W661 Card lock, option R: the KDCube per-key Redis cluster lock, per Card, then the observed file lock.

Operator decision (10 Oct, verbatim): "R now, with P (the Postgres version check)". Design: Ops 21:33Z/21:35Z with
Spark's R-1..R-5. KDCube precedent: app/ai-app/docs/service/synch-mechanisms/critical-section-README.md, "Git Bundle
Materialization": the Redis lock (owner identity in the VALUE, a constant key segment) THEN the observed file lock,
because "the file lock is still needed even when Redis is enabled"; "Redis lock timeout fails closed".
data-bus-README.md, "Ordering And Concurrency": a short-lived Redis lock per object, extended safely, which "still
require[s] storage-level idempotency and optimistic concurrency checks" (P, the PG version check, comes under it).

- The lock is KDCube's ``observed_redis_lock_async`` (storage/observed_redis_locks.py), REUSED UNCHANGED and injected
  by the host (this package does not import the SDK). Owner value = KDCube lock metadata with a fresh per-operation
  owner_token.
- R-1: the helper treats every Redis error as "not acquired" and polls to its timeout. The client handed to it is a
  proxy that re-raises a Redis error during acquisition as ``_RedisUnavailable`` (a BaseException), which passes the
  helper's ``except Exception``: an unavailable Redis refuses AT ONCE (card_lock_unavailable); only a held key waits.
- R-2: keys carry a kind: ``kdcube:cards:lock:<tenant>:<project>:card:<subject_hash>:<access_id>`` for a Card, and
  ``...:<kind>:<id>`` for the service's other sections. The service takes them in its existing order.
- R-3: ``assert_redis_noeviction`` at startup; anything but ``noeviction`` refuses the Card lock (fail closed).
  One Redis node per deployment is assumed: a replica promotion could grant a key twice.
- R-4: renewal is an owner-checked Lua PEXPIRE in a task bound to the operation; a failure marks the operation lost.
  The write guard (``assert_card_lock_owned``) checks "not lost" and GET == owner value for every key it holds before
  each Card file write. Release is the helper's owner-checked delete in ``finally``; a failed release is left to the TTL.
- R-5: wait 30 s < PB lock_timeout 40 s < statement 45 s < idle-in-transaction 60 s < TTL 120 s (renewed every 20 s).

Residual (stated, not closed here): a stall longer than the TTL between the owner check and a write lets one stale
write land; P, the storage-level version check, closes it.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import secrets
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, AsyncIterator, Callable

from .store import CardStorageError

REDIS_LOCK_TTL_SECONDS = 120
REDIS_LOCK_RENEW_SECONDS = 20
REDIS_LOCK_POLL_SECONDS = 0.05
KEY_PREFIX = "kdcube:cards:lock"
_RENEW_LUA = ("if redis.call('get', KEYS[1]) == ARGV[1] then "
              "return redis.call('pexpire', KEYS[1], ARGV[2]) else return 0 end")
# resource_id prefix -> key kind (R-2). A Card id is globally stable; the subject scope comes from the lock path.
_KINDS = {
    "delegated-card": "card",
    "delegated-card-lifecycle": "lifecycle-txn",
    "delegated-card-issuer-update": "issuer-update-txn",
    "delegated-account-fence": "account",
    "card-collection": "collection",
}


class _RedisUnavailable(BaseException):
    """Raised through the SDK helper's ``except Exception`` so a Redis error is not mistaken for contention."""


class _FailFastRedis:
    """The client the SDK helper sees: acquisition calls (SET, GET) fail fast; the release (EVAL) is the real one."""

    def __init__(self, redis: Any) -> None:
        self._redis = redis

    async def set(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return await self._redis.set(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - every Redis error is "unavailable", never "held"
            raise _RedisUnavailable(type(exc).__name__) from exc

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return await self._redis.get(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            raise _RedisUnavailable(type(exc).__name__) from exc

    async def eval(self, *args: Any, **kwargs: Any) -> Any:
        return await self._redis.eval(*args, **kwargs)


class _Held:
    """One operation's Redis keys: the owner value per key, its hold count, and whether ownership was lost."""

    def __init__(self, redis: Any) -> None:
        self.redis = redis
        self.keys: dict[str, str] = {}
        self.counts: dict[str, int] = {}
        self.lost = False
        self.closed = False


_OPERATION: ContextVar[_Held | None] = ContextVar("w661_card_redis_lock_operation", default=None)
# R-3: the eviction policy is verified once per Redis client before its first Card lock (id -> verified).
_POLICY_VERIFIED: dict[int, bool] = {}


def card_lock_key(*, tenant: str, project: str, resource_id: str, lock_path: pathlib.Path | None = None) -> str:
    """R-2: ``kdcube:cards:lock:<tenant>:<project>:<kind>:<id>``; a Card is ``card:<subject_hash>:<access_id>``."""
    prefix, _, ident = str(resource_id).partition(":")
    kind = _KINDS.get(prefix)
    if kind is None or not ident:
        raise CardStorageError("card_lock_resource_invalid")
    if kind == "card":
        # lock path: <root>/grantors/<subject_hash>/cards/<access_id>/<lock file>
        card_dir = pathlib.Path(lock_path).parent if lock_path is not None else None
        if card_dir is None or card_dir.name != ident:
            raise CardStorageError("card_lock_resource_invalid")
        ident = f"{card_dir.parent.parent.name}:{ident}"
    return f"{KEY_PREFIX}:{tenant}:{project}:{kind}:{ident}"


async def _release_own_key(redis: Any, key: str, value: str) -> None:
    """Shielded, owner-checked delete of a key this acquisition may have set: never another owner's."""
    async def release() -> None:
        try:
            await redis.eval("if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) "
                             "else return 0 end", 1, key, value)
        except Exception:  # noqa: BLE001 - Redis unreachable: the TTL removes it
            pass
    task = asyncio.ensure_future(release())
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.wait({task}, timeout=1.0)


async def _renew(held: _Held, key: str, value: str, ttl_seconds: int, every: float) -> None:
    """R-4: extend only while this operation still owns the key; any failure marks the operation lost."""
    try:
        while True:
            await asyncio.sleep(every)
            try:
                ok = await held.redis.eval(_RENEW_LUA, 1, key, value, int(ttl_seconds * 1000))
            except Exception:  # noqa: BLE001
                ok = 0
            if not ok:
                held.lost = True
                return
    except asyncio.CancelledError:
        raise


def redis_card_mutation_lock(redis: Any, *, observed_lock: Callable[..., Any], make_metadata: Callable[..., dict],
                             tenant: str, project: str, base_lock: Any,
                             ttl_seconds: int = REDIS_LOCK_TTL_SECONDS,
                             renew_seconds: float | None = REDIS_LOCK_RENEW_SECONDS,
                             poll_seconds: float = REDIS_LOCK_POLL_SECONDS, require_noeviction: bool = True) -> Any:
    """The CardMutationLock for option R: the per-key Redis lock, then ``base_lock`` (the observed file lock)."""
    # renew_seconds=None turns renewal off: ONLY for tests that model a stalled owner (a VM pause stops renewal too).
    if renew_seconds is not None and not 0 < float(renew_seconds) < float(ttl_seconds) / 2:
        raise ValueError("card_lock_renewal_must_be_under_half_the_ttl")

    @asynccontextmanager
    async def lock(*, lock_path: pathlib.Path, resource_id: str, operation: str,
                   wait_seconds: float) -> AsyncIterator[Any]:
        from .service import CardMutationLockTimeout

        if redis is None:
            raise CardStorageError("card_lock_unavailable")
        if require_noeviction and not _POLICY_VERIFIED.get(id(redis)):
            try:
                await assert_redis_noeviction(redis)
            except CardStorageError:
                raise
            _POLICY_VERIFIED[id(redis)] = True
        key = card_lock_key(tenant=tenant, project=project, resource_id=resource_id, lock_path=lock_path)
        held, token = _OPERATION.get(), None
        if held is not None and held.closed:
            raise CardStorageError("card_lock_session_closed")  # a child task that outlived its operation
        if held is None:
            held = _Held(redis)
            token = _OPERATION.set(held)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, float(wait_seconds))
        try:
            if held.counts.get(key):  # re-entry within one operation: no second SET NX on our own key
                held.counts[key] += 1
                try:
                    async with base_lock(lock_path=lock_path, resource_id=resource_id, operation=operation,
                                         wait_seconds=max(0.0, deadline - loop.time())) as inner:
                        yield inner
                finally:
                    held.counts[key] -= 1
                return
            metadata = make_metadata(resource_id=resource_id, operation=operation,
                                     owner_token=secrets.token_hex(16), extra={"key_kind": key.split(":")[5]})
            # The helper's value is json.dumps(metadata, sort_keys=True): known here, so an acquisition that is
            # cancelled or fails while its SET NX is in flight can still remove exactly its own key (owner-checked).
            own_value = json.dumps(metadata, sort_keys=True)
            entered = False
            try:
                async with observed_lock(client=_FailFastRedis(redis), key=key, metadata=metadata,
                                         ttl_seconds=int(ttl_seconds), wait_seconds=max(0.0, float(wait_seconds)),
                                         poll_seconds=poll_seconds) as value:
                    entered = True
                    held.keys[key], held.counts[key] = value, 1
                    renewal = (asyncio.ensure_future(_renew(held, key, value, ttl_seconds, renew_seconds))
                               if renew_seconds is not None else None)
                    try:
                        async with base_lock(lock_path=lock_path, resource_id=resource_id, operation=operation,
                                             wait_seconds=max(0.0, deadline - loop.time())) as inner:
                            yield inner
                    finally:
                        if renewal is not None:
                            renewal.cancel()
                            try:
                                await renewal
                            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                                pass
                        held.keys.pop(key, None)
                        held.counts.pop(key, None)
            except BaseException as exc:
                if not entered:
                    await _release_own_key(redis, key, own_value)
                if isinstance(exc, _RedisUnavailable):
                    raise CardStorageError("card_lock_unavailable") from exc
                if isinstance(exc, TimeoutError) and not entered:
                    raise CardMutationLockTimeout("card_mutation_lock_timeout") from exc
                raise
        finally:
            if token is not None:
                held.closed = True
                _OPERATION.reset(token)

    return lock


def _assert_card_lock_held() -> _Held:
    held = _OPERATION.get()
    if held is None or held.closed or not held.keys:
        raise CardStorageError("card_lock_not_held")
    if held.lost:
        raise CardStorageError("card_lock_lost")
    return held


async def assert_card_lock_owned() -> None:
    """R-4, the write guard: before each Card file write, this operation still owns EVERY key it holds."""
    held = _assert_card_lock_held()
    for key, value in list(held.keys.items()):
        try:
            current = await held.redis.get(key)
        except Exception as exc:  # noqa: BLE001 - cannot prove ownership: refuse
            held.lost = True
            raise CardStorageError("card_lock_lost") from exc
        if isinstance(current, bytes):
            current = current.decode("utf-8", errors="replace")
        if current != value:
            held.lost = True
            raise CardStorageError("card_lock_lost")


async def assert_redis_noeviction(redis: Any) -> None:
    """R-3: a lock key with a TTL is evictable under allkeys-* and volatile-*; only noeviction keeps it."""
    try:
        found = await redis.config_get("maxmemory-policy")
    except Exception as exc:  # noqa: BLE001 - unverifiable is refused, never assumed
        raise CardStorageError("card_lock_redis_policy_unverified") from exc
    policy = found.get("maxmemory-policy") if isinstance(found, dict) else None
    if isinstance(policy, bytes):
        policy = policy.decode("utf-8", errors="replace")
    if policy != "noeviction":
        raise CardStorageError("card_lock_redis_policy_not_noeviction")


# Synchronous callers (two cleanup deletions: an effects-only in-doubt entry and an account-fence pending mark) get
# the check without the Redis round-trip: held, not closed, not marked lost by the renewal.
assert_card_lock_owned.sync = _assert_card_lock_held  # type: ignore[attr-defined]


def guard_card_store_lock_owner(store: Any) -> Any:
    """Register the owner check for every write under the store's root (lock backend redis)."""
    from ..durable_io import guard_writes_under

    guard_writes_under(store.root, assert_card_lock_owned, name="card_lock_owner")
    return store


__all__ = ["KEY_PREFIX", "REDIS_LOCK_POLL_SECONDS", "REDIS_LOCK_RENEW_SECONDS", "REDIS_LOCK_TTL_SECONDS",
           "assert_card_lock_owned", "assert_redis_noeviction", "card_lock_key", "guard_card_store_lock_owner",
           "redis_card_mutation_lock"]

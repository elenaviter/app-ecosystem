# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Card projections are served only after a sweep for the current Redis run.

Why: Redis without an append-only log can restart from an older snapshot and
lose writes the durable store kept. Reads trust a present projection without
reading the durable pointer, so a projection left behind is served as
authority, and when the lost write was a revocation a revoked Card stays
usable. Mutations repair their own Card before fencing
(``DelegatedCardService._reconcile``). Every other Card is covered here: after
a data loss, every projection is compared with its durable pointer once, and
no projection is served until that comparison has completed.

Detection is Redis's own ``run_id``. It changes on every restart, and a
restore from an older snapshot brings back the epoch value stored before the
loss, or none. The epoch key records the ``run_id`` whose sweep completed with
no failure. Until it matches the live ``run_id``:

    readers        fail closed
    store owners   try the sweep themselves (``CardProjectionReconciler``)
    the app cron   runs the sweep once a minute, so cache-only readers recover
                   without waiting for a store-owning request

Every authority read proves the run in the same Redis transaction as the
projection it reads (``DelegatedCardRuntimeCache.read_in_current_run``). There
is no in-process trust window: the first read after a restart is refused.
A Redis that reports no ``run_id`` cannot prove its run, and is refused too.

Nothing here is durable state. Losing the epoch or the lock costs one sweep.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from connection_hub.delegated_credentials.cards.cache import (
    CardCacheUnusable,
    DelegatedCardRuntimeCache,
)
from connection_hub.delegated_credentials.cards.model import (
    authority_is_usable,
    authority_projection_ttl,
)
from connection_hub.delegated_credentials.cards.store import subject_hash_for

_LOGGER = logging.getLogger("connection_hub.delegated_cards.reconcile")

RECONCILE_LOCK_SECONDS = 60
# Renew the lock every this many projections, so a long sweep keeps it.
RECONCILE_LOCK_RENEW_EVERY = 50

# Take the lock unless an owner from THIS run holds it. A lock restored from an
# older snapshot names an older run and is replaced.
_ACQUIRE_LUA = """
local held = redis.call('GET', KEYS[1])
if held then
  local prefix = ARGV[1] .. '|'
  if string.sub(held, 1, string.len(prefix)) == prefix then
    return 0
  end
end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
return 1
"""

_RENEW_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  redis.call('EXPIRE', KEYS[1], ARGV[2])
  return 1
end
return 0
"""

_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  redis.call('DEL', KEYS[1])
  return 1
end
return 0
"""

# Record the completed run only while this sweep still owns the lock.
_RECORD_EPOCH_LUA = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
  return 0
end
redis.call('SET', KEYS[2], ARGV[2])
return 1
"""


class CardProjectionReconciling(RuntimeError):
    """Projections are not trusted until the current Redis run is swept."""

    reason = "card_projection_reconciling"


@dataclass(frozen=True)
class ReconcileReport:
    checked: int
    repaired: int
    failed: int
    completed: bool


def _text(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8")
    return str(value or "")


class CardProjectionEpochGate:
    """Whether the live Redis run has a completed sweep. Needs no durable store.

    Authority reads use ``read_in_current_run`` instead, which proves the run
    and reads the projection in one transaction. This gate serves callers that
    only need the answer (the picker probe) and the sweep owner.
    """

    def __init__(self, cache: DelegatedCardRuntimeCache) -> None:
        self._cache = cache
        self._redis = cache.redis
        self._epoch_key = cache.projection_epoch_key()

    @property
    def epoch_key(self) -> str:
        return self._epoch_key

    async def run_id(self) -> str:
        info = await self._redis.info("server")
        return _text((info or {}).get("run_id")).strip()

    async def is_ready(self) -> bool:
        """True when the current run's sweep completed. Never raises: a check
        that cannot be made, including a Redis that reports no ``run_id``,
        answers False, so readers fail closed."""
        try:
            run_id = await self.run_id()
            if not run_id:
                _LOGGER.warning(
                    "[connection-hub.delegated-cards] redis reports no run_id; "
                    "card projections cannot be proven current"
                )
                return False
            return _text(await self._redis.get(self._epoch_key)) == run_id
        except Exception:
            _LOGGER.warning(
                "[connection-hub.delegated-cards] projection epoch check failed",
                exc_info=True,
            )
            return False


class CardProjectionReconciler:
    """The store-owning side: sweeps when the gate is not ready."""

    def __init__(
        self,
        *,
        cache: DelegatedCardRuntimeCache,
        store: Any,
        gate: CardProjectionEpochGate | None = None,
        lock_seconds: int = RECONCILE_LOCK_SECONDS,
    ) -> None:
        self._cache = cache
        self._redis = cache.redis
        self._store = store
        self._gate = gate or CardProjectionEpochGate(cache)
        self._lock_key = cache.reconcile_lock_key()
        self._lock_seconds = int(lock_seconds)

    async def ensure_ready(self, *, now: int | None = None) -> None:
        """Return when projections may be served; otherwise sweep, and raise
        ``CardProjectionReconciling`` when this call could not complete one."""
        if await self._gate.is_ready():
            return
        report = await self.reconcile(now=now)
        if report is None or not report.completed:
            raise CardProjectionReconciling(CardProjectionReconciling.reason)

    async def reconcile(self, *, now: int | None = None) -> ReconcileReport | None:
        """Sweep for the current run unless it is already recorded, or another
        worker of this run holds the lock (``None``). Never raises."""
        try:
            run_id = await self._gate.run_id()
            if not run_id:
                return None
            if _text(await self._redis.get(self._gate.epoch_key)) == run_id:
                return ReconcileReport(checked=0, repaired=0, failed=0, completed=True)
            token = f"{run_id}|{uuid.uuid4().hex}"
            acquired = await self._redis.eval(
                _ACQUIRE_LUA, 1, self._lock_key, run_id, token, str(self._lock_seconds)
            )
            if not int(acquired or 0):
                return None
        except Exception:
            _LOGGER.warning(
                "[connection-hub.delegated-cards] projection sweep could not start",
                exc_info=True,
            )
            return None
        try:
            report = await self._sweep(token=token, now=now)
            if report.completed:
                recorded = await self._redis.eval(
                    _RECORD_EPOCH_LUA, 2, self._lock_key, self._gate.epoch_key, token, run_id
                )
                if not int(recorded or 0):
                    report = ReconcileReport(
                        checked=report.checked,
                        repaired=report.repaired,
                        failed=report.failed,
                        completed=False,
                    )
            _LOGGER.warning(
                "[connection-hub.delegated-cards] projection sweep run_id=%s "
                "checked=%s repaired=%s failed=%s completed=%s",
                run_id, report.checked, report.repaired, report.failed, report.completed,
            )
            return report
        except Exception:
            _LOGGER.warning(
                "[connection-hub.delegated-cards] projection sweep failed", exc_info=True
            )
            return ReconcileReport(checked=0, repaired=0, failed=1, completed=False)
        finally:
            try:
                await self._redis.eval(_RELEASE_LUA, 1, self._lock_key, token)
            except Exception:
                _LOGGER.warning(
                    "[connection-hub.delegated-cards] projection sweep lock release failed",
                    exc_info=True,
                )

    async def _sweep(self, *, token: str, now: int | None) -> ReconcileReport:
        moment = int(now if now is not None else time.time())
        checked = repaired = failed = 0
        async for key in self._redis.scan_iter(match=self._cache.card_key_pattern(), count=200):
            access_id = self._cache.access_id_from_key(key)
            try:
                entry = await self._cache.read(access_id)
            except CardCacheUnusable:
                # An unusable value already denies and reloads from durable.
                continue
            if entry is None or entry.is_updating:
                # A marker belongs to an in-flight mutation.
                continue
            checked += 1
            if checked % RECONCILE_LOCK_RENEW_EVERY == 0:
                renewed = await self._redis.eval(
                    _RENEW_LUA, 1, self._lock_key, token, str(self._lock_seconds)
                )
                if not int(renewed or 0):
                    # Another owner took over; its sweep decides the epoch.
                    return ReconcileReport(checked, repaired, failed, completed=False)
            try:
                if entry.is_revoked:
                    # A tombstone names no grantor to find the durable pointer
                    # by, and one restored from before a re-consent would deny
                    # the active Card. Removing it is fail-closed: readers go
                    # to the durable revision, which denies a revoked Card.
                    removed = await self._cache.reconcile_projection(
                        access_id,
                        durable_revision=entry.card_revision + 1,
                        authority=None,
                        ttl_seconds=None,
                    )
                elif entry.authority is None:
                    continue
                else:
                    removed = await self._repair(
                        access_id, entry.card_revision, entry.authority, moment
                    )
                if removed:
                    repaired += 1
            except Exception:
                failed += 1
                _LOGGER.warning(
                    "[connection-hub.delegated-cards] projection sweep failed card=%s",
                    access_id,
                    exc_info=True,
                )
        return ReconcileReport(checked, repaired, failed, completed=failed == 0)

    async def _repair(self, access_id: str, projected: int, authority: Any, moment: int) -> bool:
        current = await self._store.read_current_authority(
            subject_hash=subject_hash_for(authority.grantor_subject), access_id=access_id
        )
        if current is None:
            # Durable storage is the source of truth: a projection with no
            # durable Card behind it is removed, unless it moved meanwhile.
            _LOGGER.warning(
                "[connection-hub.delegated-cards] removing projection without durable "
                "card=%s revision=%s",
                access_id, projected,
            )
            return await self._cache.reconcile_projection(
                access_id, durable_revision=projected + 1, authority=None, ttl_seconds=None
            )
        _, durable = current
        if durable.card_revision <= projected:
            return False
        usable = authority_is_usable(durable, moment)
        return await self._cache.reconcile_projection(
            access_id,
            durable_revision=durable.card_revision,
            authority=durable if usable else None,
            ttl_seconds=authority_projection_ttl(durable, moment) if usable else None,
        )


__all__ = [
    "CardProjectionEpochGate",
    "CardProjectionReconciler",
    "CardProjectionReconciling",
    "ReconcileReport",
]

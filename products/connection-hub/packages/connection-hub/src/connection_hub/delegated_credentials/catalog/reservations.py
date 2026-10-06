"""W502: a transaction's reservation of the ACTIVE catalog version, and the publisher's check of it.

A Card transaction whose business check evaluated the active catalog (for
example Problem Board's last-usable-administrator check) reserves exactly
that version until it is decided, so no publication can land between its
prepare and its commit (EMain/CodeApp 19:18). Both publication routes, the
Hub's deploy and KDCube's proc deployment, publish through
``ensure_delegated_catalog``, which calls ``assert_publishable`` here; the
reservations live under the catalog store root, so neither route needs the
Card store.

Ordering (both sides write first, then read the other's mark):

- reserve: write the fence, then read the publication marker and the active
  version; if a publication is pending or the version moved, remove the fence
  and refuse ``catalog_version_moved``;
- publish: write the publication marker, then refuse while any fence names
  another version, publish, and remove the marker.

So in any interleaving at least one side sees the other. A fence is removed
when its transaction is decided (or aborted unstaged); one a crash left
behind blocks publication only until the transaction is finished, by
whichever recovery drives it. A marker a crashed publisher left blocks new
reservations until the next publication run clears it. Both fail closed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from typing import Any

from service_foundation.coordination.durable_wire import canonical_json_bytes, sha256_hex

from ..durable_io import read_json_or_none, write_json_atomic

RESERVATIONS_DIRNAME = "reservations"
PUBLICATION_MARKER = "publication-pending.json"
# A marker's publisher is alive while KDCube's shared-storage runner lock for
# the catalog publication is fresh: run_once_for_shared_bundle_storage holds
# <root>/.kdcube.once/<operation>.lock and heartbeats it every <= 10 s for as
# long as the action runs, so a live publisher has NO maximum hold and a
# marker's age proves nothing (EMain #611). Same rule as its _remove_stale_lock:
# the heartbeat's mtime (else the lock's) within the lock TTL.
PUBLISHER_OPERATION = "delegated-catalog-publish"  # publisher.CATALOG_OPERATION
RUNNER_LOCK_TTL_SECONDS = 900.0  # run_once_for_shared_bundle_storage's lock_ttl_seconds default
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


class CatalogReservationRefused(RuntimeError):
    def __init__(self, reason: str, *, holders: tuple[str, ...] = (), age_seconds: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.holders = holders
        self.age_seconds = age_seconds


def catalog_version_digest(version: str, content_hash: str) -> str:
    """The digest a ``catalog-active:<digest>`` dependency key names.

    sha256 of the kernel canonical JSON of ``{"content_hash", "version"}``: the
    version AND its immutable content (CodeApp 19:29), so a same-named version
    with other content can never satisfy a reservation.
    """
    return sha256_hex(canonical_json_bytes({"content_hash": str(content_hash), "version": str(version)}))


class CatalogReservations:
    """Reservations of the active catalog version, over one catalog store."""

    def __init__(self, store: Any) -> None:
        self._store = store
        self._root = store.root / RESERVATIONS_DIRNAME

    def _path(self, transaction_id: str):
        if not _HEX64.fullmatch(str(transaction_id)) and not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}",
                                                                          str(transaction_id)):
            raise CatalogReservationRefused("catalog_reservation_invalid")
        return self._root / f"{hashlib.sha256(str(transaction_id).encode('utf-8')).hexdigest()}.json"

    async def _active_digest(self) -> str:
        active = await self._store.read_active()
        return catalog_version_digest(active.version, active.content_hash) if active is not None else ""

    async def reserve(self, *, transaction_id: str, intent_digest: str, version_digest: str) -> None:
        """Hold ``version_digest`` as the active catalog for this transaction, or refuse."""
        if not _HEX64.fullmatch(str(version_digest)) or not _HEX64.fullmatch(str(intent_digest)):
            raise CatalogReservationRefused("catalog_reservation_invalid")
        path = self._path(transaction_id)
        existing = await read_json_or_none(path)
        if isinstance(existing, dict):
            if existing.get("intent_digest") != intent_digest or existing.get("version_digest") != version_digest:
                raise CatalogReservationRefused("catalog_reservation_changed")
            return  # a replay: already held, and held fences are never published over
        await write_json_atomic(path, {"transaction_id": transaction_id, "intent_digest": intent_digest,
                                       "version_digest": version_digest})
        pending = await read_json_or_none(self._store.root / PUBLICATION_MARKER)
        if pending is not None:
            await self.release(transaction_id, intent_digest=intent_digest)  # never live: this call wrote it
            started = pending.get("started_at") if isinstance(pending, dict) else None
            age = int(time.time()) - started if type(started) is int else None
            raise CatalogReservationRefused("catalog_publication_pending", age_seconds=age)
        if await self._active_digest() != version_digest:
            await self.release(transaction_id, intent_digest=intent_digest)
            raise CatalogReservationRefused("catalog_version_moved")

    async def release(self, transaction_id: str, *, intent_digest: str) -> None:
        """Release only the fence of exactly this transaction and intent (CodeApp 19:29).

        Callers release on an authenticated terminal decision only: a decided
        receipt or an ABORT tombstone. A fence with no receipt is UNKNOWN and
        stays; a fence naming another intent is never cleared.
        """
        path = self._path(transaction_id)
        fence = await read_json_or_none(path)
        if not isinstance(fence, dict) or fence.get("intent_digest") != intent_digest:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass  # a held fence only delays publication until the next release

    async def holders(self) -> list[dict[str, Any]]:
        def read_all() -> list[dict[str, Any]]:
            if not self._root.is_dir():
                return []
            found = []
            for entry in sorted(self._root.glob("*.json")):
                try:
                    found.append(json.loads(entry.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    found.append({"transaction_id": entry.stem, "version_digest": ""})  # unreadable: held
            return found

        return await asyncio.to_thread(read_all)

    async def begin_publication(self, document: Any) -> dict[str, Any]:
        marker = {"version_digest": catalog_version_digest(document.version, document.content_hash),
                  "started_at": int(time.time()), "nonce": uuid.uuid4().hex}
        await write_json_atomic(self._store.root / PUBLICATION_MARKER, marker)
        return marker

    async def reassert_publication(self, marker: dict[str, Any]) -> bool:
        """After its fence check, the publisher confirms its own marker; True when it had to rewrite it.

        A cron that judged a dead publisher's lock stale just before this one
        took the lock may have deleted this marker (EMain #611). Rewriting it
        and checking the fences again restores the ordering: a reservation
        that slipped into the gap wrote its fence first, so the second check sees it.
        """
        path = self._store.root / PUBLICATION_MARKER
        if await read_json_or_none(path) == marker:
            return False
        await write_json_atomic(path, marker)
        return True

    async def clear_dead_publication(self) -> bool:
        """Inside the publisher's serialized section: any marker found belongs to a dead publisher."""
        path = self._store.root / PUBLICATION_MARKER
        if await read_json_or_none(path) is None and not path.exists():
            return False
        await self.end_publication()
        return True

    def publisher_alive(self, *, now: float | None = None,
                        lock_ttl_seconds: float = RUNNER_LOCK_TTL_SECONDS) -> bool:
        """Whether the catalog publication's runner lock is held and fresh (KDCube's own staleness rule)."""
        lock = self._store.root / ".kdcube.once" / f"{PUBLISHER_OPERATION}.lock"
        try:
            fresh = (lock / "heartbeat").stat().st_mtime
        except OSError:
            try:
                fresh = lock.stat().st_mtime
            except OSError:
                return False  # no lock: no publisher is in the section
        return ((time.time() if now is None else now) - fresh) <= lock_ttl_seconds

    async def clear_stale_publication(self, *, now: float | None = None,
                                      lock_ttl_seconds: float = RUNNER_LOCK_TTL_SECONDS) -> int | None:
        """Outside the section (the recovery cron): clear a dead publisher's marker; its age, or None.

        Dead means the runner lock is absent or stale, never merely an old
        marker: a live publisher heartbeats its lock however long it runs.
        """
        path = self._store.root / PUBLICATION_MARKER
        marker = await read_json_or_none(path)
        if marker is None or await asyncio.to_thread(self.publisher_alive, now=now,
                                                     lock_ttl_seconds=lock_ttl_seconds):
            return None
        started = marker.get("started_at") if isinstance(marker, dict) else None
        if await read_json_or_none(path) != marker:
            return None  # compare-and-delete: a new publisher wrote its own marker meanwhile
        await self.end_publication()
        return int((time.time() if now is None else now) - started) if type(started) is int else -1

    async def end_publication(self) -> None:
        try:
            (self._store.root / PUBLICATION_MARKER).unlink(missing_ok=True)
        except OSError:
            pass

    async def assert_publishable(self, document: Any) -> None:
        """Refuse (retryable) while a transaction holds another catalog version or content."""
        digest = catalog_version_digest(document.version, document.content_hash)
        blocking = tuple(str(fence.get("transaction_id") or "") for fence in await self.holders()
                         if fence.get("version_digest") != digest)
        if blocking:
            raise CatalogReservationRefused("catalog_reserved", holders=blocking)


__all__ = ["CatalogReservationRefused", "CatalogReservations", "PUBLICATION_MARKER", "PUBLISHER_OPERATION",
           "RESERVATIONS_DIRNAME", "RUNNER_LOCK_TTL_SECONDS", "catalog_version_digest"]

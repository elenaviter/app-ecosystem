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
from typing import Any

from service_foundation.coordination.durable_wire import canonical_json_bytes, sha256_hex

from ..durable_io import read_json_or_none, write_json_atomic

RESERVATIONS_DIRNAME = "reservations"
PUBLICATION_MARKER = "publication-pending.json"
# A publication marker older than this belongs to a publisher that died: no
# live publisher holds ensure_delegated_catalog's critical section this long.
# (Assumed bound on the shared-storage runner's hold; EMain #609.)
MARKER_STALE_SECONDS = 900
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

    async def begin_publication(self, document: Any) -> None:
        await write_json_atomic(self._store.root / PUBLICATION_MARKER,
                                {"version_digest": catalog_version_digest(document.version, document.content_hash),
                                 "started_at": int(time.time())})

    async def clear_dead_publication(self) -> bool:
        """Inside the publisher's serialized section: any marker found belongs to a dead publisher."""
        path = self._store.root / PUBLICATION_MARKER
        if await read_json_or_none(path) is None and not path.exists():
            return False
        await self.end_publication()
        return True

    async def clear_stale_publication(self, *, now: int | None = None,
                                      max_age_seconds: int = MARKER_STALE_SECONDS) -> int | None:
        """Outside the section (the recovery cron): clear a marker older than the bound; its age, or None."""
        path = self._store.root / PUBLICATION_MARKER
        marker = await read_json_or_none(path)
        if marker is None:
            return None
        started = marker.get("started_at") if isinstance(marker, dict) else None
        age = (int(time.time()) if now is None else now) - started if type(started) is int else None
        if age is not None and age <= max_age_seconds:
            return None
        await self.end_publication()  # unreadable or past the bound: no live publisher holds it
        return age if age is not None else -1

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


__all__ = ["CatalogReservationRefused", "CatalogReservations", "MARKER_STALE_SECONDS", "PUBLICATION_MARKER",
           "RESERVATIONS_DIRNAME", "catalog_version_digest"]

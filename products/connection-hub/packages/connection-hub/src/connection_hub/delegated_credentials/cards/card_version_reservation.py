# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter
"""W756: one link-only base reservation per Card, through the existing marker store.

STAGE owns it before answering. File mode uses the durable marker write; P uses
the same guarded row/CAS authority and row-era hint rules as transaction markers.
No Card value, history scan, second prepare protocol or expiring allow is added.
Only the owning transaction's completed PUBLISH or ROLLBACK releases its slot.
"""

from __future__ import annotations

from typing import Any

from service_foundation.coordination.durable_wire import canonical_json_bytes, sha256_hex

from .store import CardStorageError

SCHEMA = "connection_hub.card-version-base-reservation.v1"


def reservation_key(*, subject_hash: str, access_id: str) -> str:
    return "cvreserve-" + sha256_hex(canonical_json_bytes([subject_hash, access_id]))


async def read_reservation(store: Any, *, subject_hash: str, access_id: str) -> dict | None:
    raw = await store.read_marker_document(reservation_key(subject_hash=subject_hash, access_id=access_id))
    if raw is None:
        return None
    if (not isinstance(raw, dict) or set(raw) != {"schema", "txn", "card", "base_revision_name"}
            or raw["schema"] != SCHEMA or raw["card"] != {"subject_hash": subject_hash, "access_id": access_id}
            or type(raw["txn"]) is not str or not raw["txn"]
            or raw["base_revision_name"] is not None and type(raw["base_revision_name"]) is not str):
        raise CardStorageError("card_version_base_reservation_invalid")
    return raw


async def assert_base_available(store: Any, *, subject_hash: str, access_id: str, own_txn: str = "") -> None:
    found = await read_reservation(store, subject_hash=subject_hash, access_id=access_id)
    if found is not None and found["txn"] != own_txn:
        from .transaction_store import read_card_version_marker
        marker = await read_card_version_marker(store, found["txn"])
        if marker is not None and marker["state"] == "published":
            raise CardStorageError("card_version_effects_pending")  # exact publication/cleanup must finish
        raise CardStorageError("card_version_base_reserved")


async def claim_base(store: Any, *, subject_hash: str, access_id: str, txn: str,
                     base_revision_name: str | None) -> None:
    """All member locks are held; the transaction marker already names this slot."""
    found = await read_reservation(store, subject_hash=subject_hash, access_id=access_id)
    value = {"schema": SCHEMA, "txn": txn, "card": {"subject_hash": subject_hash, "access_id": access_id},
             "base_revision_name": base_revision_name}
    if found is not None:
        if found != value:
            raise CardStorageError("card_version_base_reserved")
        return
    await store.write_marker_document(reservation_key(subject_hash=subject_hash, access_id=access_id), value)


async def release_base(store: Any, *, subject_hash: str, access_id: str, txn: str) -> None:
    """Cleanup is idempotent and can never remove a successor's slot."""
    found = await read_reservation(store, subject_hash=subject_hash, access_id=access_id)
    if found is not None and found["txn"] == txn:
        await store.delete_marker_document(reservation_key(subject_hash=subject_hash, access_id=access_id))

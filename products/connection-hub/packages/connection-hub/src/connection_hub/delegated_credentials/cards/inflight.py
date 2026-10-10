# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W661: one per-Card file naming the Hub-local operation in flight on that Card.

Operator, 10 Oct: "never nothing is being scanned". A writer that must not publish around an in-flight
issuer update or lifecycle intent reads ``<card>/inflight.json`` = ``{kind, txn}`` directly, instead of
listing the in-flight queue folders (EMain, 16:57Z).

The file is written under the Card's mutation lock BEFORE the operation's queue entry and removed AFTER it
retires, so a Card with an unfinished operation always names it. At most one such operation is in flight per
Card: a second writer finds the file and refuses. A file left by an interrupted cleanup names a txn whose
receipt is already terminal; it is stale and is neither honoured nor needed (the receipt stays authoritative).
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..durable_io import cancellation_safe_await, read_json_or_none, write_json_atomic
from .store import CardStorageError

INFLIGHT_FILENAME = "inflight.json"
ISSUER_UPDATE = "issuer_update"
LIFECYCLE = "lifecycle"
_UNRESOLVED = {ISSUER_UPDATE: "issuer_update_preparation_unresolved", LIFECYCLE: "lifecycle_preparation_unresolved"}


def inflight_path(store: Any, *, subject_hash: str, access_id: str):
    return store.card_path(subject_hash=subject_hash, access_id=access_id) / INFLIGHT_FILENAME


async def _receipt(store: Any, kind: str, txn: str):
    if kind == ISSUER_UPDATE:
        from .update_store import read_receipt
    else:
        from .lifecycle_store import read_receipt
    return await read_receipt(store, txn)


async def read_inflight(store: Any, *, subject_hash: str, access_id: str) -> dict[str, str] | None:
    """The operation still in flight on this Card, or None. One direct read; nothing is listed."""
    if not hasattr(store, "card_path"):
        return None
    raw = await read_json_or_none(inflight_path(store, subject_hash=subject_hash, access_id=access_id))
    if raw is None:
        return None
    if (not isinstance(raw, dict) or set(raw) != {"kind", "txn"} or raw["kind"] not in _UNRESOLVED
            or type(raw["txn"]) is not str or not raw["txn"]):
        raise CardStorageError("card_inflight_invalid")
    receipt = await _receipt(store, raw["kind"], raw["txn"])
    if receipt is None or (receipt["state"] != "prepared" and receipt["serving_state"] != "pending"):
        return None  # stale: its operation already reached a terminal receipt
    return dict(raw)


async def assert_no_inflight(store: Any, *, subject_hash: str, access_id: str) -> None:
    found = await read_inflight(store, subject_hash=subject_hash, access_id=access_id)
    if found is not None:
        raise CardStorageError(_UNRESOLVED[found["kind"]])


async def claim_inflight(store: Any, *, subject_hash: str, access_id: str, kind: str, txn: str) -> None:
    """Caller holds the Card's mutation lock. Refuses while another operation is in flight on the Card."""
    if not hasattr(store, "card_path"):
        return
    found = await read_inflight(store, subject_hash=subject_hash, access_id=access_id)
    if found is not None and found != {"kind": kind, "txn": txn}:
        raise CardStorageError(_UNRESOLVED[found["kind"]])
    await write_json_atomic(inflight_path(store, subject_hash=subject_hash, access_id=access_id),
                            {"kind": kind, "txn": txn})


async def release_inflight(store: Any, *, subject_hash: str, access_id: str, txn: str) -> None:
    """After the operation's terminal receipt and queue retirement; only the file naming ``txn`` goes."""
    if not hasattr(store, "card_path"):
        return
    path = inflight_path(store, subject_hash=subject_hash, access_id=access_id)
    raw = await read_json_or_none(path)
    if not isinstance(raw, dict) or raw.get("txn") != txn:
        return
    try:
        await cancellation_safe_await(asyncio.to_thread(path.unlink, missing_ok=True))
    except OSError:
        pass  # a stale file is ignored by read_inflight: the terminal receipt stays authoritative


__all__ = ["INFLIGHT_FILENAME", "ISSUER_UPDATE", "LIFECYCLE", "assert_no_inflight", "claim_inflight",
           "inflight_path", "read_inflight", "release_inflight"]

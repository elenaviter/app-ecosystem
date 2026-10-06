"""Reader-aware atomic visibility for staged two-Card revocations.

LOW-LEVEL STORAGE ONLY. The service must hold the receipt fence and BOTH
production Card fences and enforce issuer/serving gates before calling here.
No authorization, locks, credential cleanup or cache is manufactured here.

One atomically renamed receipt is the visibility point. Pending current
pointers resolve their BEFORE revision until that receipt says committed.
Recovery aborts uncommitted staging, never compensates an authoritative revoke.
Old readers reject the new pointer schema instead of exposing staged authority.
"""

from __future__ import annotations

import dataclasses
import re
from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping

from ..durable_io import read_json_or_none, write_json_atomic
from .lifecycle import LifecycleRefused, LifecycleRequest
from .model import CARD_STATE_REVOKED, CardCurrentPointer, card_revision_name
from .store import CardStorageError

LIFECYCLE_POINTER_SCHEMA = "connection_hub.card-current-lifecycle.v1"
LIFECYCLE_RECEIPT_SCHEMA = "connection_hub.card-lifecycle-receipt.v1"


def receipt_path(store: Any, transaction_id: str):
    if type(transaction_id) is not str or not re.fullmatch(r"[0-9a-f]{64}", transaction_id):
        raise CardStorageError("lifecycle_transaction_id_invalid")
    return store.root / "lifecycle-transactions" / f"{transaction_id}.json"


def _validate_receipt(value: object, transaction_id: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema", "transaction_id", "binding", "state", "reason", "targets",
    }:
        raise CardStorageError("lifecycle_receipt_invalid")
    if (value["schema"] != LIFECYCLE_RECEIPT_SCHEMA or value["transaction_id"] != transaction_id
            or value["state"] not in ("prepared", "committed", "refused")
            or type(value["reason"]) is not str):
        raise CardStorageError("lifecycle_receipt_invalid")
    binding = value["binding"]
    try:
        if not isinstance(binding, Mapping) or set(binding) != {"actor_subject", "request"}:
            raise LifecycleRefused("binding_invalid")
        request = LifecycleRequest.from_mapping(binding["request"])
        if request.transaction_id(binding["actor_subject"]) != transaction_id:
            raise LifecycleRefused("binding_invalid")
        if request.binding(binding["actor_subject"]) != binding:
            raise LifecycleRefused("binding_invalid")
        entries = value["targets"]
        if type(entries) is not list or len(entries) != 2:
            raise LifecycleRefused("targets_invalid")
        for target, entry in zip(request.targets, entries):
            if not isinstance(entry, Mapping) or set(entry) != {"subject_hash", "access_id", "before", "after"}:
                raise LifecycleRefused("targets_invalid")
            before = CardCurrentPointer.from_mapping(entry["before"])
            after = CardCurrentPointer.from_mapping(entry["after"])
            if ((entry["subject_hash"], entry["access_id"]) != (target.subject_hash, target.access_id)
                    or before.access_id != target.access_id or after.access_id != target.access_id
                    or before.card_revision != target.expected_card_revision
                    or after.card_revision != before.card_revision + 1
                    or after.state != CARD_STATE_REVOKED):
                raise LifecycleRefused("targets_invalid")
    except (ValueError, TypeError, KeyError) as exc:
        raise CardStorageError("lifecycle_receipt_invalid") from exc
    return dict(value)


async def read_receipt(store: Any, transaction_id: str) -> dict[str, Any] | None:
    raw = await read_json_or_none(receipt_path(store, transaction_id))
    return None if raw is None else _validate_receipt(raw, transaction_id)


async def resolve_pointer(store: Any, payload: Mapping[str, Any], *,
                          subject_hash: str, access_id: str) -> CardCurrentPointer:
    if set(payload) != {"schema", "transaction_id", "before", "after"}:
        raise CardStorageError("lifecycle_pointer_invalid")
    receipt = await read_receipt(store, payload["transaction_id"])
    if receipt is None:
        raise CardStorageError("lifecycle_receipt_missing")
    entries = [entry for entry in receipt["targets"]
               if (entry["subject_hash"], entry["access_id"]) == (subject_hash, access_id)]
    if len(entries) != 1 or any(payload[name] != entries[0][name] for name in ("before", "after")):
        raise CardStorageError("lifecycle_pointer_binding_mismatch")
    name = "after" if receipt["state"] == "committed" else "before"
    return CardCurrentPointer.from_mapping(entries[0][name])


async def assert_pointer_replaceable(store: Any, *, subject_hash: str, access_id: str) -> None:
    raw = await read_json_or_none(store.current_path(subject_hash=subject_hash, access_id=access_id))
    if raw is None or raw.get("schema") != LIFECYCLE_POINTER_SCHEMA:
        return
    # Resolve even a terminal receipt to catch corrupted pointer bindings.
    await resolve_pointer(store, raw, subject_hash=subject_hash, access_id=access_id)
    receipt = await read_receipt(store, raw["transaction_id"])
    if receipt["state"] == "prepared":
        raise CardStorageError("lifecycle_preparation_unresolved")


async def abort_prepared(store: Any, *, request: LifecycleRequest, actor_subject: str,
                         reason: str = "lifecycle_preparation_interrupted") -> dict[str, Any] | None:
    """Caller holds both participant fences. Abort visibility BEFORE restoring
    pending pointers; a kill during cleanup still resolves BOTH before states.
    """
    transaction_id = request.transaction_id(actor_subject)
    receipt = await read_receipt(store, transaction_id)
    if receipt is None:
        return None
    if receipt["binding"] != request.binding(actor_subject):
        raise LifecycleRefused("issuer_lifecycle_replay_changed")
    if receipt["state"] != "prepared":
        return receipt
    refused = {**receipt, "state": "refused", "reason": reason}
    await write_json_atomic(receipt_path(store, transaction_id), refused)
    # Cleanup is not necessary for correct reads. Do not replace any pointer
    # another correctly fenced transaction has legitimately superseded.
    for entry in receipt["targets"]:
        path = store.current_path(subject_hash=entry["subject_hash"], access_id=entry["access_id"])
        raw = await read_json_or_none(path)
        if raw == {"schema": LIFECYCLE_POINTER_SCHEMA, "transaction_id": transaction_id,
                   "before": entry["before"], "after": entry["after"]}:
            await write_json_atomic(path, entry["before"])
    return refused


async def atomic_revoke(store: Any, *, request: LifecycleRequest, actor_subject: str,
                        now: datetime, before_publish: Callable[[], Awaitable[None]]) -> dict[str, Any]:
    """Fences/issuer/serving gates are mandatory SERVICE responsibilities.

    All authority preconditions are checked before staging. before_publish is
    another bounded issuer-expiry check while both fences remain held. Replay
    recovers an existing terminal outcome; it never applies another mutation.
    """
    if getattr(store, "lifecycle_publish_backend", "") != "filesystem-atomic-rename":
        raise LifecycleRefused("issuer_lifecycle_atomic_backend_unavailable")
    if not callable(before_publish) or now.utcoffset() is None:
        raise LifecycleRefused("issuer_lifecycle_commit_gate_unavailable")
    transaction_id = request.transaction_id(actor_subject)
    existing = await read_receipt(store, transaction_id)
    if existing is not None:
        if existing["binding"] != request.binding(actor_subject):
            raise LifecycleRefused("issuer_lifecycle_replay_changed")
        return await abort_prepared(store, request=request, actor_subject=actor_subject)
    authorities = []
    entries = []
    for target in request.targets:
        await assert_pointer_replaceable(store, subject_hash=target.subject_hash, access_id=target.access_id)
        current = await store.read_current_authority(subject_hash=target.subject_hash, access_id=target.access_id)
        target.assert_authority(None if current is None else current[1])
        before, authority = current
        revoked = dataclasses.replace(authority, state=CARD_STATE_REVOKED, card_revision=authority.card_revision + 1)
        content_hash = revoked.content_hash()
        after = CardCurrentPointer.for_revision(revoked, content_hash=content_hash,
            revision_name=card_revision_name(card_revision=revoked.card_revision, content_hash=content_hash, updated_at=now),
            updated_at=now)
        authorities.append(revoked)
        entries.append({"subject_hash": target.subject_hash, "access_id": target.access_id,
                        "before": before.to_dict(), "after": after.to_dict()})
    receipt = {"schema": LIFECYCLE_RECEIPT_SCHEMA, "transaction_id": transaction_id,
               "binding": request.binding(actor_subject), "state": "prepared", "reason": "", "targets": entries}
    # Intent precedes every revision/pointer write and exists across a kill.
    await write_json_atomic(receipt_path(store, transaction_id), receipt)
    try:
        for authority, entry in zip(authorities, entries):
            pointer = await store.write_revision(subject_hash=entry["subject_hash"], authority=authority, updated_at=now)
            if pointer.to_dict() != entry["after"]:
                raise CardStorageError("lifecycle_staged_revision_mismatch")
        for entry in entries:
            await write_json_atomic(store.current_path(subject_hash=entry["subject_hash"], access_id=entry["access_id"]),
                {"schema": LIFECYCLE_POINTER_SCHEMA, "transaction_id": transaction_id,
                 "before": entry["before"], "after": entry["after"]})
        await before_publish()
        committed = {**receipt, "state": "committed"}
        await write_json_atomic(receipt_path(store, transaction_id), committed)
        return committed
    except Exception:
        # An IO error can occur AFTER the rename committed. Never relabel that
        # as a no-write refusal; read the actual visibility point first.
        outcome = await read_receipt(store, transaction_id)
        if outcome is None:
            raise CardStorageError("lifecycle_commit_outcome_unknown")
        if outcome["state"] == "committed":
            return outcome
        await abort_prepared(store, request=request, actor_subject=actor_subject,
                             reason="lifecycle_preparation_failed")
        raise

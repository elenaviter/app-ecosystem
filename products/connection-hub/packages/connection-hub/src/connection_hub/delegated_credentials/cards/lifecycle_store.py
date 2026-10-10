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

import asyncio
import dataclasses
import itertools
import re
from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping

from ..durable_io import DurableStorageError, cancellation_safe_await, read_json_or_none, require_publish_before, write_json_atomic, unlink_guarded
from .lifecycle import LifecycleRefused, LifecycleRequest
from .model import CARD_STATE_REVOKED, CardCurrentPointer, card_revision_name
from .store import CardStorageError

LIFECYCLE_POINTER_SCHEMA = "connection_hub.card-current-lifecycle.v1"
LIFECYCLE_RECEIPT_SCHEMA = "connection_hub.card-lifecycle-receipt.v1"
MAX_ACTIVE_INTENTS = 128


def receipt_path(store: Any, transaction_id: str):
    if type(transaction_id) is not str or not re.fullmatch(r"[0-9a-f]{64}", transaction_id):
        raise CardStorageError("lifecycle_transaction_id_invalid")
    return store.root / "lifecycle-transactions" / f"{transaction_id}.json"


def active_intent_path(store: Any, transaction_id: str):
    return receipt_path(store, transaction_id).parent / "active" / f"{transaction_id}.json"


def _validate_receipt(value: object, transaction_id: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema", "transaction_id", "binding", "state", "reason", "targets", "serving_state",
    }:
        raise CardStorageError("lifecycle_receipt_invalid")
    if (value["schema"] != LIFECYCLE_RECEIPT_SCHEMA or value["transaction_id"] != transaction_id
            or value["state"] not in ("prepared", "committed", "refused")
            or value["serving_state"] not in ("not_required", "pending", "complete")
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
                    or type(entry["before"].get("card_revision")) is not int
                    or type(entry["after"].get("card_revision")) is not int
                    or before.state != "active"
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
    if raw is None:
        raw = await read_json_or_none(active_intent_path(store, transaction_id))
    return None if raw is None else _validate_receipt(raw, transaction_id)


async def refuse_before_prepare(store: Any, *, request: LifecycleRequest, actor_subject: str,
                               now: datetime, reason: str) -> dict[str, Any]:
    """Terminal unsupported-backend outcome, with NO active intent or Card writes.

    Caller holds receipt and both Card fences and has checked fresh authority.
    Virtual after pointers preserve the existing receipt schema but are NEVER
    written. This durable refusal lets identical retry close its reservation.
    """
    entries = []
    for target in request.targets:
        current = await store.read_current_authority(subject_hash=target.subject_hash, access_id=target.access_id)
        target.assert_authority(None if current is None else current[1])
        before, authority = current
        after_authority = dataclasses.replace(authority, state=CARD_STATE_REVOKED, card_revision=authority.card_revision + 1)
        digest = after_authority.content_hash()
        after = CardCurrentPointer.for_revision(after_authority, content_hash=digest,
            revision_name=card_revision_name(card_revision=after_authority.card_revision, content_hash=digest, updated_at=now),
            updated_at=now)
        entries.append({"subject_hash": target.subject_hash, "access_id": target.access_id,
                        "before": before.to_dict(), "after": after.to_dict()})
    transaction_id = request.transaction_id(actor_subject)
    receipt = {"schema": LIFECYCLE_RECEIPT_SCHEMA, "transaction_id": transaction_id,
               "binding": request.binding(actor_subject), "state": "refused", "reason": reason,
               "targets": entries, "serving_state": "complete"}
    _validate_receipt(receipt, transaction_id)
    await write_json_atomic(receipt_path(store, transaction_id), receipt)
    return receipt


async def _active_intents(store: Any):
    """Only unfinished preparations, not an unbounded historical receipt scan.

    A terminal record left by interrupted cleanup is harmless. Directory/read
    failures and an overloaded recovery queue fail writers closed.
    """
    if not hasattr(store, "root"):
        return  # other legacy backends cannot offer this lifecycle capability
    directory = store.root / "lifecycle-transactions" / "active"

    def paths():
        try:
            return list(itertools.islice(directory.iterdir(), MAX_ACTIVE_INTENTS + 1))
        except FileNotFoundError:
            return []

    found = await asyncio.to_thread(paths)
    if len(found) > MAX_ACTIVE_INTENTS:
        raise CardStorageError("lifecycle_recovery_queue_unavailable")
    for path in found:
        if not path.is_file() or path.suffix != ".json":
            continue  # atomic-write temporaries are not published intents
        receipt = await read_receipt(store, path.stem)
        if receipt is None:
            continue  # terminal cleanup won a race with enumeration
        if receipt["state"] == "prepared" or receipt["serving_state"] == "pending":
            yield receipt


async def _retire_active_intent(store: Any, transaction_id: str) -> None:
    # Called only AFTER an authoritative terminal receipt was published. A
    # cleanup failure does not relabel a committed outcome or restore authority.
    receipt = await read_receipt(store, transaction_id)
    if receipt is None or receipt["state"] == "prepared" or receipt["serving_state"] == "pending":
        return
    try:
        await cancellation_safe_await(asyncio.to_thread(unlink_guarded, active_intent_path(store, transaction_id)))
    except OSError:
        pass
    from .inflight import release_inflight
    for entry in receipt["targets"]:
        await release_inflight(store, subject_hash=entry["subject_hash"], access_id=entry["access_id"],
                               txn=transaction_id)


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
    from .update_store import assert_replaceable as assert_update_replaceable
    await assert_update_replaceable(store, subject_hash=subject_hash, access_id=access_id)
    # W578: no ordinary write publishes around an undecided staged transaction.
    from .transaction_store import assert_replaceable as assert_transaction_replaceable
    await assert_transaction_replaceable(store, subject_hash=subject_hash, access_id=access_id)
    # W661 D3 (Infra K2 cut): finalize the card-version txn current.json names before replacing it.
    from .transaction_store import finalize_current_version
    await finalize_current_version(store, subject_hash=subject_hash, access_id=access_id)
    # The shared intent exists BEFORE either pointer is staged, and each target Card names it in its own
    # in-flight file before that (W661: one direct read per Card, nothing listed). The update store's check
    # above read that same file, so an in-flight lifecycle intent has already refused there.
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
        await _retire_active_intent(store, transaction_id)
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
    await _retire_active_intent(store, transaction_id)
    return refused


async def atomic_revoke(store: Any, *, request: LifecycleRequest, actor_subject: str,
                        now: datetime, before_publish: Callable[[], Awaitable[None]],
                        after_prepare: Callable[[], Awaitable[None]] | None = None) -> dict[str, Any]:
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
               "binding": request.binding(actor_subject), "state": "prepared", "reason": "", "targets": entries,
               "serving_state": "pending" if after_prepare is not None else "not_required"}
    # Each target Card names the intent first (W661 per-Card in-flight read), then the intent precedes every
    # revision/pointer write and exists across a kill.
    from .inflight import LIFECYCLE, claim_inflight
    for entry in entries:
        await claim_inflight(store, subject_hash=entry["subject_hash"], access_id=entry["access_id"],
                             kind=LIFECYCLE, txn=transaction_id)
    await write_json_atomic(active_intent_path(store, transaction_id), receipt)
    try:
        if after_prepare is not None:
            await after_prepare()
        for authority, entry in zip(authorities, entries):
            await write_json_atomic(store.revision_path(subject_hash=entry["subject_hash"],
                access_id=entry["access_id"], revision_name=entry["after"]["revision_name"]).with_suffix(".lifecycle.json"),
                {"transaction_id": transaction_id})
            pointer = await store.write_revision(subject_hash=entry["subject_hash"], authority=authority, updated_at=now)
            if pointer.to_dict() != entry["after"]:
                raise CardStorageError("lifecycle_staged_revision_mismatch")
        for entry in entries:
            current = await store.read_current(subject_hash=entry["subject_hash"], access_id=entry["access_id"])
            if current is None or current.to_dict() != entry["before"]:
                raise LifecycleRefused("issuer_lifecycle_revision_moved")
            await write_json_atomic(store.current_path(subject_hash=entry["subject_hash"], access_id=entry["access_id"]),
                {"schema": LIFECYCLE_POINTER_SCHEMA, "transaction_id": transaction_id,
                 "before": entry["before"], "after": entry["after"]})
        deadline = await before_publish()
        committed = {**receipt, "state": "committed"}
        if after_prepare is not None:
            with require_publish_before(deadline):
                await write_json_atomic(receipt_path(store, transaction_id), committed)
        else:
            await write_json_atomic(receipt_path(store, transaction_id), committed)
        await _retire_active_intent(store, transaction_id)
        return committed
    except Exception as exc:
        # An IO error can occur AFTER the rename committed. Never relabel that
        # as a no-write refusal; read the actual visibility point first.
        outcome = await read_receipt(store, transaction_id)
        if outcome is None:
            raise CardStorageError("lifecycle_commit_outcome_unknown")
        if outcome["state"] == "committed":
            await _retire_active_intent(store, transaction_id)
            return outcome
        await abort_prepared(store, request=request, actor_subject=actor_subject,
                             reason="lifecycle_preparation_failed")
        if isinstance(exc, DurableStorageError) and exc.reason.startswith("issuer_decision_expir"):
            raise LifecycleRefused(exc.reason) from exc
        raise


async def mark_serving_complete(store: Any, receipt: dict[str, Any]) -> dict[str, Any]:
    """Caller still holds receipt and BOTH production Card fences."""
    if receipt["state"] not in ("committed", "refused"):
        raise CardStorageError("lifecycle_serving_outcome_invalid")
    completed = {**receipt, "serving_state": "complete"}
    try:
        await write_json_atomic(receipt_path(store, receipt["transaction_id"]), completed)
    except Exception:
        actual = await read_receipt(store, receipt["transaction_id"])
        if actual != completed:
            raise
    await _retire_active_intent(store, receipt["transaction_id"])
    return completed

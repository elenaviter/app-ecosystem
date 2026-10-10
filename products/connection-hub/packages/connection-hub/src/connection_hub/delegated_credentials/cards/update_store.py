"""Storage-only intent/visibility protocol for one widening issuer update.

The service owns authorization, the receipt fence and the real Card fence.
A prepared pointer reads BEFORE; one expiry-checked receipt rename publishes
AFTER. An interrupted preparation is terminally refused, never compensated
after commit. Credential handles are neither read nor written here.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any, Mapping

from ..durable_io import cancellation_safe_await, read_json_or_none, require_publish_before, write_json_atomic, unlink_guarded
from ..issuer_gate import change_digest
from ..issuer_update import IssuerUpdateQuery, IssuerUpdateRefused
from .model import CardCurrentPointer, card_revision_name
from .store import CardStorageError

UPDATE_POINTER_SCHEMA = "connection_hub.card-current-issuer-update.v1"
UPDATE_RECEIPT_SCHEMA = "connection_hub.card-issuer-update-receipt.v1"


def receipt_path(store, transaction_id):
    if type(transaction_id) is not str or not re.fullmatch(r"[0-9a-f]{64}", transaction_id):
        raise CardStorageError("issuer_update_transaction_id_invalid")
    return store.root / "issuer-updates" / f"{transaction_id}.json"


def active_path(store, transaction_id):
    return receipt_path(store, transaction_id).parent / "active" / f"{transaction_id}.json"


def validate_receipt(raw: Any, transaction_id: str) -> dict[str, Any]:
    try:
        if (not isinstance(raw, Mapping) or set(raw) != {"schema", "transaction_id", "binding", "state", "reason",
                "before", "after", "change_digest", "serving_state"}
                or raw["schema"] != UPDATE_RECEIPT_SCHEMA or raw["transaction_id"] != transaction_id
                or raw["state"] not in ("prepared", "committed", "refused")
                or raw["serving_state"] not in ("pending", "complete", "not_required")
                or type(raw["reason"]) is not str or type(raw["change_digest"]) is not str):
            raise ValueError()
        binding = raw["binding"]
        if not isinstance(binding, Mapping) or set(binding) != {"actor_subject", "request"}:
            raise ValueError()
        query = IssuerUpdateQuery.from_mapping(binding["request"])
        if query.binding(binding["actor_subject"]) != binding or query.transaction_id(binding["actor_subject"]) != transaction_id:
            raise ValueError()
        if raw["before"] is None or raw["after"] is None:
            if not (raw["before"] is raw["after"] is None and raw["state"] == "refused"
                    and raw["serving_state"] == "not_required"
                    and (raw["change_digest"] == "" or re.fullmatch(r"[0-9a-f]{64}", raw["change_digest"]))):
                raise ValueError()
        else:
            before, after = (CardCurrentPointer.from_mapping(raw[k]) for k in ("before", "after"))
            if (before.access_id != query.target.access_id or after.access_id != before.access_id
                    or type(raw["before"].get("card_revision")) is not int
                    or type(raw["after"].get("card_revision")) is not int
                    or before.to_dict() != raw["before"] or after.to_dict() != raw["after"]
                    or before.card_revision != query.target.expected_card_revision or after.card_revision != before.card_revision + 1
                    or before.content_hash != query.target.expected_authority_fingerprint
                    or before.state != "active" or after.state != "active"
                    or raw["serving_state"] == "not_required"
                    or raw["state"] == "prepared" and raw["serving_state"] != "pending"
                    or not re.fullmatch(r"[0-9a-f]{64}", raw["change_digest"])):
                raise ValueError()
        return dict(raw)
    except (KeyError, ValueError, TypeError) as exc:
        raise CardStorageError("issuer_update_receipt_invalid") from exc


async def read_receipt(store, transaction_id):
    raw = await read_json_or_none(receipt_path(store, transaction_id))
    if raw is None:
        raw = await read_json_or_none(active_path(store, transaction_id))
    return None if raw is None else validate_receipt(raw, transaction_id)


async def retire(store, receipt):
    if receipt["state"] == "prepared" or receipt["serving_state"] == "pending":
        return
    try:
        await cancellation_safe_await(asyncio.to_thread(unlink_guarded, active_path(store, receipt["transaction_id"])))
    except OSError:
        pass  # terminal receipt remains authoritative
    from .inflight import release_inflight
    target = IssuerUpdateQuery.from_mapping(receipt["binding"]["request"]).target
    await release_inflight(store, subject_hash=target.subject_hash, access_id=target.access_id,
                           txn=receipt["transaction_id"])


async def assert_replaceable(store, *, subject_hash, access_id):
    if not hasattr(store, "root"):
        return
    # W661 (EMain 16:57Z; operator: "never nothing is being scanned"): the Card's own in-flight file, one
    # direct read, replaces the listing of issuer-updates/active.
    from .inflight import assert_no_inflight
    await assert_no_inflight(store, subject_hash=subject_hash, access_id=access_id)
    raw = await read_json_or_none(store.current_path(subject_hash=subject_hash, access_id=access_id))
    if raw is not None and raw.get("schema") == UPDATE_POINTER_SCHEMA:
        await resolve_pointer(store, raw, subject_hash=subject_hash, access_id=access_id)


async def resolve_pointer(store, payload, *, subject_hash, access_id):
    if not isinstance(payload, Mapping) or set(payload) != {"schema", "transaction_id", "before", "after"}:
        raise CardStorageError("issuer_update_pointer_invalid")
    receipt = await read_receipt(store, payload["transaction_id"])
    if receipt is None:
        raise CardStorageError("issuer_update_receipt_missing")
    target = IssuerUpdateQuery.from_mapping(receipt["binding"]["request"]).target
    if ((target.subject_hash, target.access_id) != (subject_hash, access_id)
            or any(payload[k] != receipt[k] for k in ("before", "after"))):
        raise CardStorageError("issuer_update_pointer_binding_invalid")
    return CardCurrentPointer.from_mapping(receipt["after" if receipt["state"] == "committed" else "before"])


async def recover_prepared(store, receipt, *, reason="issuer_update_preparation_interrupted"):
    if receipt["state"] == "prepared":
        receipt = {**receipt, "state": "refused", "reason": reason}
        await write_json_atomic(receipt_path(store, receipt["transaction_id"]), receipt)
    return receipt


async def refuse_unprepared(store, query, actor_subject, reason, *, candidate_digest=""):
    transaction_id = query.transaction_id(actor_subject)
    receipt = {"schema": UPDATE_RECEIPT_SCHEMA, "transaction_id": transaction_id,
        "binding": query.binding(actor_subject), "state": "refused", "reason": reason,
        "before": None, "after": None, "change_digest": candidate_digest, "serving_state": "not_required"}
    validate_receipt(receipt, transaction_id)
    await write_json_atomic(receipt_path(store, transaction_id), receipt)
    return receipt


async def atomic_update(store, *, query, actor_subject, original, candidate, now,
                        after_prepare, before_publish):
    """Caller holds receipt/Card fences and provides fresh issuer/cache gates."""
    transaction_id = query.transaction_id(actor_subject)
    current = await store.read_current_authority(subject_hash=query.target.subject_hash, access_id=query.target.access_id)
    if current is None or current[1].to_dict() != original.to_dict():
        raise IssuerUpdateRefused("issuer_update_revision_moved")
    before = current[0]
    if before.content_hash != original.content_hash():
        raise IssuerUpdateRefused("issuer_update_legacy_payload_requires_migration")
    digest = candidate.content_hash()
    after = CardCurrentPointer.for_revision(candidate, content_hash=digest,
        revision_name=card_revision_name(card_revision=candidate.card_revision, content_hash=digest, updated_at=now), updated_at=now)
    receipt = {"schema": UPDATE_RECEIPT_SCHEMA, "transaction_id": transaction_id,
        "binding": query.binding(actor_subject), "state": "prepared", "reason": "",
        "before": before.to_dict(), "after": after.to_dict(), "change_digest": change_digest(candidate.to_dict()),
        "serving_state": "pending"}
    validate_receipt(receipt, transaction_id)
    from .inflight import ISSUER_UPDATE, claim_inflight
    # The Card names this update BEFORE its queue entry exists (W661: the guard reads only the Card).
    await claim_inflight(store, subject_hash=query.target.subject_hash, access_id=query.target.access_id,
                         kind=ISSUER_UPDATE, txn=transaction_id)
    await write_json_atomic(active_path(store, transaction_id), receipt)
    try:
        await after_prepare()
        path = store.revision_path(subject_hash=query.target.subject_hash, access_id=query.target.access_id,
                                   revision_name=after.revision_name)
        await write_json_atomic(path.with_suffix(".issuer-update.json"), {"transaction_id": transaction_id})
        pointer = await store.write_revision(subject_hash=query.target.subject_hash, authority=candidate, updated_at=now)
        if pointer != after:
            raise CardStorageError("issuer_update_staged_revision_mismatch")
        # Publish no authority yet: modern readers resolve this through the
        # receipt; older readers reject the unfamiliar pointer schema.
        await write_json_atomic(store.current_path(subject_hash=query.target.subject_hash, access_id=query.target.access_id),
            {"schema": UPDATE_POINTER_SCHEMA, "transaction_id": transaction_id, "before": receipt["before"], "after": receipt["after"]})
        deadline = await before_publish()
        committed = {**receipt, "state": "committed"}
        with require_publish_before(deadline):
            await write_json_atomic(receipt_path(store, transaction_id), committed)
        return committed
    except Exception as exc:
        actual = await read_receipt(store, transaction_id)
        if actual is not None and actual["state"] == "committed":
            return actual  # error after the visibility rename is still commit
        await recover_prepared(store, receipt, reason=getattr(exc, "reason", "issuer_update_preparation_failed"))
        raise


async def mark_serving_complete(store, receipt):
    if receipt["state"] not in ("committed", "refused"):
        raise CardStorageError("issuer_update_serving_outcome_invalid")
    completed = {**receipt, "serving_state": "complete"}
    try:
        await write_json_atomic(receipt_path(store, receipt["transaction_id"]), completed)
    except Exception:
        if await read_receipt(store, receipt["transaction_id"]) != completed:
            raise
    await retire(store, completed)
    return completed

"""Generic staged Card participant: stage invisibly, publish only on the recorded decision (W578).

A business transaction that spans realms (a membership change in an
application's SQL and a Card change here) needs each realm to stage its part
without exposing it, and one durable decision that every reader follows. This
module is Connection Hub's Card participant for that. It stays generic: it
binds an opaque transaction id and the digest of the coordinator's intent; it
knows nothing about roles, admins or memberships.

Protocol, per transaction and Card:

- **stage** (caller holds the Card's mutation fence): the current Card must be
  exactly ``original``; a prepared receipt is written FIRST, then the
  immutable after-revision, then a transaction pointer naming before/after.
  Readers resolving that pointer get BEFORE while the receipt is prepared.
- **decide** COMMITTED or ABORTED, exactly once, bound to the same intent
  digest: COMMITTED is the single receipt rename that makes AFTER visible;
  ABORTED leaves BEFORE. The same decision again is idempotent; a different
  one, or another intent, is refused. The decision is the coordinator's,
  recorded in its own ledger; this only materializes it.
- **state / recover** read the receipt; recovery can materialize the recorded
  decision but never change it.
- **fence**: while a receipt for a Card is prepared, an ordinary write of that
  Card is refused (``card_transaction_unresolved``), so no writer publishes
  around a staged pointer. Unknown, timeout or crash never releases it.

This module writes storage only; authorization, the coordinator's decision
ledger and the reader pinning across realms are the callers'.
"""

from __future__ import annotations

import itertools
import re
from datetime import datetime
from typing import Any, Mapping, Protocol

from ..durable_io import read_json_or_none, write_json_atomic
from ..issuer_gate import change_digest
from .model import CardAuthority, CardCurrentPointer, card_revision_name
from .store import CardStorageError

TRANSACTION_POINTER_SCHEMA = "connection_hub.card-current-transaction.v1"
TRANSACTION_RECEIPT_SCHEMA = "connection_hub.card-transaction-receipt.v1"
DECISIONS = ("committed", "aborted")
_HEX64 = re.compile(r"[0-9a-f]{64}")


class CardTransactionRefused(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _checked_id(transaction_id: Any) -> str:
    if type(transaction_id) is not str or not _HEX64.fullmatch(transaction_id):
        raise CardStorageError("card_transaction_id_invalid")
    return transaction_id


def receipt_path(store: Any, transaction_id: str):
    return store.root / "card-transactions" / f"{_checked_id(transaction_id)}.json"


MAX_ACTIVE_TRANSACTIONS = 1024


def active_path(store: Any, transaction_id: str):
    """An in-flight transaction's index entry: written before its receipt, removed once decided."""
    return store.root / "card-transactions" / "active" / f"{_checked_id(transaction_id)}.json"


def _validate(raw: Any, transaction_id: str) -> dict[str, Any]:
    try:
        if (not isinstance(raw, Mapping) or set(raw) != {
                "schema", "transaction_id", "intent_digest", "participant", "subject_hash", "access_id",
                "state", "reason", "before", "after", "change_digest"}
                or raw["schema"] != TRANSACTION_RECEIPT_SCHEMA or raw["transaction_id"] != transaction_id
                or raw["state"] not in ("prepared", *DECISIONS)
                or not _HEX64.fullmatch(str(raw["intent_digest"])) or not _HEX64.fullmatch(str(raw["change_digest"]))
                or type(raw["participant"]) is not str or not raw["participant"]
                or type(raw["reason"]) is not str):
            raise ValueError()
        before, after = (CardCurrentPointer.from_mapping(raw[name]) for name in ("before", "after"))
        if (before.access_id != raw["access_id"] or after.access_id != raw["access_id"]
                or before.to_dict() != raw["before"] or after.to_dict() != raw["after"]
                or after.card_revision != before.card_revision + 1):
            raise ValueError()
        return dict(raw)
    except (KeyError, ValueError, TypeError) as exc:
        raise CardStorageError("card_transaction_receipt_invalid") from exc


async def read_receipt(store: Any, transaction_id: str) -> dict[str, Any] | None:
    raw = await read_json_or_none(receipt_path(store, transaction_id))
    return None if raw is None else _validate(raw, transaction_id)


class TransactionDecisionPort(Protocol):
    """The coordinator's authoritative decision for one staged transaction (CodeApp, 11:14).

    Bound on the store INSTANCE by the hosting composition, never a module
    global. Returns "committed", "aborted" or "undecided" for the exact
    receipt (transaction id, intent digest, participant); it verifies the
    coordinator's durable record, never an asserted string.
    """

    async def decision(self, receipt: Mapping[str, Any]) -> str: ...


def bind_transaction_decisions(store: Any, port: TransactionDecisionPort | None) -> None:
    store._card_transaction_decisions = port


async def _authoritative_state(store: Any, receipt: Mapping[str, Any]) -> str:
    """The decision a reader must follow: the local receipt once decided, else the coordinator's."""

    if receipt["state"] in DECISIONS:
        return receipt["state"]
    port = getattr(store, "_card_transaction_decisions", None)
    if port is None:
        raise CardStorageError("card_transaction_undecided")
    try:
        decision = await port.decision(dict(receipt))
    except Exception as exc:  # noqa: BLE001 - an unreachable coordinator decides nothing
        raise CardStorageError("card_transaction_undecided") from exc
    if decision not in DECISIONS:
        raise CardStorageError("card_transaction_undecided")
    return decision


async def resolve_pointer(store: Any, payload: Any, *, subject_hash: str, access_id: str,
                          consult_decision: bool = True) -> CardCurrentPointer:
    """The pointer a reader uses, by the coordinator's recorded decision.

    COMMITTED reads AFTER and ABORTED reads BEFORE, even before the local
    receipt materializes it; an undecided or unreachable decision refuses,
    only for this staged Card. Internal validation passes
    ``consult_decision=False`` and gets BEFORE while prepared.
    """

    if not isinstance(payload, Mapping) or set(payload) != {"schema", "transaction_id", "before", "after"}:
        raise CardStorageError("card_transaction_pointer_invalid")
    receipt = await read_receipt(store, payload["transaction_id"])
    if receipt is None:
        raise CardStorageError("card_transaction_receipt_missing")
    if ((receipt["subject_hash"], receipt["access_id"]) != (subject_hash, access_id)
            or any(payload[name] != receipt[name] for name in ("before", "after"))):
        raise CardStorageError("card_transaction_pointer_binding_invalid")
    if not consult_decision:
        return CardCurrentPointer.from_mapping(receipt["after" if receipt["state"] == "committed" else "before"])
    state = await _authoritative_state(store, receipt)
    return CardCurrentPointer.from_mapping(receipt["after" if state == "committed" else "before"])


def marker_path(store: Any, *, subject_hash: str, access_id: str):
    """The per-Card marker naming its prepared transaction; it fences from the first write."""
    return store.current_path(subject_hash=subject_hash, access_id=access_id).with_name("card-transaction.json")


async def _prepared_marker(store: Any, *, subject_hash: str, access_id: str) -> dict[str, Any] | None:
    raw = await read_json_or_none(marker_path(store, subject_hash=subject_hash, access_id=access_id))
    if not isinstance(raw, Mapping) or not isinstance(raw.get("transaction_id"), str):
        return None
    receipt = await read_receipt(store, raw["transaction_id"])
    if receipt is None or receipt["state"] != "prepared":
        return None
    if (receipt["subject_hash"], receipt["access_id"]) != (subject_hash, access_id):
        raise CardStorageError("card_transaction_marker_binding_invalid")
    return receipt


async def _clear_marker(store: Any, receipt: Mapping[str, Any]) -> None:
    path = marker_path(store, subject_hash=receipt["subject_hash"], access_id=receipt["access_id"])
    raw = await read_json_or_none(path)
    if isinstance(raw, Mapping) and raw.get("transaction_id") == receipt["transaction_id"]:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass  # the decided receipt already releases the fence
    if receipt["state"] in DECISIONS:
        try:
            active_path(store, receipt["transaction_id"]).unlink(missing_ok=True)
        except OSError:
            pass  # a stale entry only lists a decided transaction, which recovery skips


async def _retire_pointer(store: Any, receipt: Mapping[str, Any]) -> None:
    """Replace this decided transaction's pointer by the plain pointer of its decided side.

    The decided receipt stays as the audit and idempotency record, but a
    settled Card is then read like any other: one pointer read, no receipt
    read and no coordinator call (Root and Ops hot-path rule, 11:57).
    """

    path = store.current_path(subject_hash=receipt["subject_hash"], access_id=receipt["access_id"])
    raw = await read_json_or_none(path)
    if (isinstance(raw, Mapping) and raw.get("schema") == TRANSACTION_POINTER_SCHEMA
            and raw.get("transaction_id") == receipt["transaction_id"]):
        side = receipt["after"] if receipt["state"] == "committed" else receipt["before"]
        await write_json_atomic(path, dict(side))


async def list_in_doubt(store: Any) -> list[dict[str, Any]]:
    """Every transaction this participant has begun preparing and not yet decided.

    Recovery's view of the in-doubt Cards: a prepared receipt, or an index
    entry whose receipt was never written (a stage that crashed first, which
    is reported as ``unstaged`` and holds no fence). Bounded: more in-flight
    transactions than MAX_ACTIVE_TRANSACTIONS fails closed rather than
    returning a partial list.
    """

    import asyncio

    directory = store.root / "card-transactions" / "active"

    def names():
        try:
            return [path.stem for path in itertools.islice(directory.iterdir(), MAX_ACTIVE_TRANSACTIONS + 1)
                    if path.is_file() and path.suffix == ".json"]
        except FileNotFoundError:
            return []

    found = await asyncio.to_thread(names)
    if len(found) > MAX_ACTIVE_TRANSACTIONS:
        raise CardStorageError("card_transaction_recovery_queue_unavailable")
    in_doubt = []
    for transaction_id in sorted(found):
        if not _HEX64.fullmatch(transaction_id):
            continue  # an atomic-write temporary, not an entry
        receipt = await read_receipt(store, transaction_id)
        if receipt is None:
            in_doubt.append({"transaction_id": transaction_id, "state": "unstaged"})
        elif receipt["state"] == "prepared":
            in_doubt.append({key: receipt[key] for key in (
                "transaction_id", "intent_digest", "participant", "subject_hash", "access_id", "state")})
    return in_doubt


async def assert_replaceable(store: Any, *, subject_hash: str, access_id: str) -> None:
    """Refuse an ordinary write of a Card with a prepared (undecided) transaction.

    The per-Card marker is written right after the prepared receipt, so the
    fence holds from the first durable step, before the pointer exists
    (Ops F1, 11:13).
    """

    if await _prepared_marker(store, subject_hash=subject_hash, access_id=access_id) is not None:
        raise CardStorageError("card_transaction_unresolved")
    raw = await read_json_or_none(store.current_path(subject_hash=subject_hash, access_id=access_id))
    if raw is None or raw.get("schema") != TRANSACTION_POINTER_SCHEMA:
        return
    await resolve_pointer(store, raw, subject_hash=subject_hash, access_id=access_id, consult_decision=False)
    receipt = await read_receipt(store, raw["transaction_id"])
    if receipt["state"] == "prepared":
        raise CardStorageError("card_transaction_unresolved")


async def _is_staged(store: Any, receipt: Mapping[str, Any]) -> bool:
    raw = await read_json_or_none(store.current_path(subject_hash=receipt["subject_hash"], access_id=receipt["access_id"]))
    return (isinstance(raw, Mapping) and raw.get("schema") == TRANSACTION_POINTER_SCHEMA
            and raw.get("transaction_id") == receipt["transaction_id"]
            and raw.get("before") == receipt["before"] and raw.get("after") == receipt["after"])


def revision_marker_path(store: Any, *, subject_hash: str, access_id: str, revision_name: str):
    """Binds a staged revision to its transaction: history shows it only once committed."""
    return store.revision_path(subject_hash=subject_hash, access_id=access_id,
                               revision_name=revision_name).with_suffix(".card-transaction.json")


async def revision_is_committed(store: Any, marker: Any, *, subject_hash: str, access_id: str,
                                revision_name: str) -> bool:
    if not isinstance(marker, Mapping) or set(marker) != {"transaction_id"}:
        raise CardStorageError("card_transaction_revision_binding_invalid")
    receipt = await read_receipt(store, marker["transaction_id"])
    if receipt is None:
        return False  # a stage that crashed before its receipt: never committed
    if ((receipt["subject_hash"], receipt["access_id"], receipt["after"].get("revision_name"))
            != (subject_hash, access_id, revision_name)):
        raise CardStorageError("card_transaction_revision_binding_invalid")
    # The same decision current readers follow: COMMITTED (recorded by the
    # coordinator, even before local FINISH) shows it; ABORTED or undecided
    # hides it, so history fails closed while pending.
    try:
        return await _authoritative_state(store, receipt) == "committed"
    except CardStorageError:
        return False


async def _write_staged(store: Any, receipt: Mapping[str, Any], candidate: CardAuthority, now: datetime) -> None:
    # The revision marker first: a staged AFTER never appears in history or a
    # by-name revision read before its transaction commits, and an aborted one
    # never appears at all.
    await write_json_atomic(revision_marker_path(store, subject_hash=receipt["subject_hash"],
                                                 access_id=receipt["access_id"],
                                                 revision_name=receipt["after"]["revision_name"]),
                            {"transaction_id": receipt["transaction_id"]})
    pointer = await store.write_revision(subject_hash=receipt["subject_hash"], authority=candidate, updated_at=now)
    if pointer.to_dict() != receipt["after"]:
        raise CardStorageError("card_transaction_staged_revision_mismatch")
    await write_json_atomic(store.current_path(subject_hash=receipt["subject_hash"], access_id=receipt["access_id"]),
                            {"schema": TRANSACTION_POINTER_SCHEMA, "transaction_id": receipt["transaction_id"],
                             "before": receipt["before"], "after": receipt["after"]})


async def stage(store: Any, *, transaction_id: str, intent_digest: str, participant: str, subject_hash: str,
                original: CardAuthority, candidate: CardAuthority, now: datetime) -> dict[str, Any]:
    """Stage ``candidate`` behind a transaction pointer; nothing becomes visible. Caller holds the fence.

    A replay of a prepared transaction RESUMES its missing steps, but only
    while the Card is still exactly the receipt's BEFORE; otherwise it is
    refused and the coordinator must abort (Ops F1).
    """

    _checked_id(transaction_id)
    if not _HEX64.fullmatch(str(intent_digest or "")) or not str(participant or "").strip():
        raise CardTransactionRefused("card_transaction_intent_invalid")
    port = getattr(store, "_card_transaction_decisions", None)
    existing = await read_receipt(store, transaction_id)
    if existing is not None and existing["state"] == "prepared" and port is not None:
        try:
            if await port.decision(dict(existing)) == "aborted":
                raise CardTransactionRefused("card_transaction_aborted")
        except CardTransactionRefused:
            raise
        except Exception:  # noqa: BLE001 - an unknown decision does not block resuming its own staging
            pass
    if existing is not None:
        if (existing["intent_digest"] != intent_digest
                or existing["change_digest"] != change_digest(candidate.to_dict())
                or existing["access_id"] != original.access_id or existing["subject_hash"] != subject_hash):
            raise CardTransactionRefused("card_transaction_replay_changed")
        if existing["state"] != "prepared" or await _is_staged(store, existing):
            return existing
        raw = await read_json_or_none(store.current_path(subject_hash=subject_hash, access_id=original.access_id))
        current = None if raw is None else (
            await resolve_pointer(store, raw, subject_hash=subject_hash, access_id=original.access_id,
                                  consult_decision=False)
            if raw.get("schema") == TRANSACTION_POINTER_SCHEMA else CardCurrentPointer.from_mapping(raw))
        if current is None or current.to_dict() != existing["before"]:
            raise CardTransactionRefused("card_transaction_revision_moved")
        await _write_staged(store, existing, candidate, now)
        return existing
    # A fresh stage passes the FULL shared fence: an unresolved issuer UPDATE
    # or lifecycle intent awaiting recovery is never overwritten (Ops F2).
    from .lifecycle_store import assert_pointer_replaceable
    await assert_pointer_replaceable(store, subject_hash=subject_hash, access_id=original.access_id)
    current = await store.read_current_authority(subject_hash=subject_hash, access_id=original.access_id)
    if current is None or current[1].to_dict() != original.to_dict():
        raise CardTransactionRefused("card_transaction_revision_moved")
    if (candidate.access_id != original.access_id or candidate.card_revision != original.card_revision + 1):
        raise CardTransactionRefused("card_transaction_candidate_invalid")
    before = current[0]
    digest = candidate.content_hash()
    after = CardCurrentPointer.for_revision(candidate, content_hash=digest, revision_name=card_revision_name(
        card_revision=candidate.card_revision, content_hash=digest, updated_at=now), updated_at=now)
    receipt = {"schema": TRANSACTION_RECEIPT_SCHEMA, "transaction_id": transaction_id,
               "intent_digest": intent_digest, "participant": participant.strip(), "subject_hash": subject_hash,
               "access_id": original.access_id, "state": "prepared", "reason": "",
               "before": before.to_dict(), "after": after.to_dict(), "change_digest": change_digest(candidate.to_dict())}
    _validate(receipt, transaction_id)
    # 1. The prepared receipt, then 2. the per-Card marker: the Card is fenced
    #    from here on, even before 3. the after-revision and 4. the pointer.
    #    A crash between 1 and 2 leaves the Card unfenced, so an ordinary
    #    write can still land. That stays safe: the moved Card makes a
    #    replay refuse (card_transaction_revision_moved) and a commit refuse
    #    (card_transaction_not_staged), so the coordinator can only abort
    #    (Ops 11:20, non-blocking a). The same crash also lets a lifecycle
    #    intent prepare before a replay: a replay then passes only while the
    #    pointer is still BEFORE, and the lifecycle can only stay blocked
    #    until this transaction is decided and then abort its own pointer
    #    (Ops 11:36, non-blocking N2).
    # 0. The in-flight index entry first, so recovery can always find it.
    await write_json_atomic(active_path(store, transaction_id), {"transaction_id": transaction_id})
    await write_json_atomic(receipt_path(store, transaction_id), receipt)
    await write_json_atomic(marker_path(store, subject_hash=subject_hash, access_id=original.access_id),
                            {"transaction_id": transaction_id})
    await _write_staged(store, receipt, candidate, now)
    return receipt


async def decide(store: Any, *, transaction_id: str, intent_digest: str, decision: str,
                 reason: str = "") -> dict[str, Any]:
    """Materialize the coordinator's recorded decision, exactly once.

    COMMITTED requires this transaction's pointer to be in place, so a
    commit never reports AFTER published when it is not (Ops F1). The receipt
    carries no expiry of its own: a presumed abort after the original expiry
    is the coordinator's decision, materialized here like any other.
    Production callers go through DelegatedCardService.decide_transaction,
    which also keeps the serving projection consistent (Ops F3).
    """

    if decision not in DECISIONS:
        raise CardTransactionRefused("card_transaction_decision_invalid")
    receipt = await read_receipt(store, transaction_id)
    if receipt is None:
        raise CardTransactionRefused("card_transaction_unknown")
    if receipt["intent_digest"] != intent_digest:
        raise CardTransactionRefused("card_transaction_intent_mismatch")
    if receipt["state"] in DECISIONS:
        if receipt["state"] != decision:
            raise CardTransactionRefused("card_transaction_decision_conflict")
        await _retire_pointer(store, receipt)
        await _clear_marker(store, receipt)
        return receipt
    if decision == "committed" and not await _is_staged(store, receipt):
        raise CardTransactionRefused("card_transaction_not_staged")
    # Local decide only MATERIALIZES the coordinator's recorded decision; it is
    # never a second, independent business decision (CodeApp, 11:14).
    port = getattr(store, "_card_transaction_decisions", None)
    if port is None:
        raise CardTransactionRefused("card_transaction_decision_unverified")
    try:
        recorded = await port.decision(dict(receipt))
    except Exception as exc:  # noqa: BLE001
        raise CardTransactionRefused("card_transaction_decision_unverified") from exc
    if recorded != decision:
        raise CardTransactionRefused("card_transaction_decision_not_recorded")
    decided = {**receipt, "state": decision, "reason": str(reason or "")[:128]}
    _validate(decided, transaction_id)
    # The one visibility point: the receipt rename. COMMITTED readers get AFTER.
    await write_json_atomic(receipt_path(store, transaction_id), decided)
    await _retire_pointer(store, decided)
    await _clear_marker(store, decided)
    return decided


async def state(store: Any, *, transaction_id: str) -> dict[str, Any] | None:
    """The recorded state; recovery reads this and may only materialize it, never change it."""

    return await read_receipt(store, transaction_id)


__all__ = ["CardTransactionRefused", "DECISIONS", "TRANSACTION_POINTER_SCHEMA", "TRANSACTION_RECEIPT_SCHEMA",
           "TransactionDecisionPort", "active_path", "assert_replaceable", "bind_transaction_decisions", "decide",
           "list_in_doubt", "marker_path",
           "read_receipt", "resolve_pointer", "stage", "state"]

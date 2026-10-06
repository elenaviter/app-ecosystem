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


def _effects_valid(effects: Any) -> bool:
    if not isinstance(effects, list) or not effects or len(effects) > MAX_EFFECTS:
        return False
    seen = set()
    for effect in effects:
        if (not isinstance(effect, Mapping) or set(effect) != {"kind", "key", "payload"}
                or not isinstance(effect["kind"], str) or not effect["kind"]
                or not isinstance(effect["key"], str) or not effect["key"]
                or len(effect["key"].encode("utf-8")) > MAX_EFFECT_KEY_BYTES
                # Keys become part of an invocation id: printable ASCII only,
                # refused here by name rather than failing a later hook.
                or any(not 33 <= ord(char) <= 126 for char in effect["key"])
                or not isinstance(effect["payload"], Mapping) or (effect["kind"], effect["key"]) in seen):
            return False
        seen.add((effect["kind"], effect["key"]))
    return True


MAX_EFFECTS = 32
# "<64-hex transaction id>:<key>" must fit a 256-byte invocation id (the
# policy change id); a longer key is refused by name, never truncated.
MAX_EFFECT_KEY_BYTES = 256 - 65


def effects_path(store: Any, transaction_id: str):
    """Which of a committed transaction's effects are applied: its readiness record."""
    return store.root / "card-transactions" / "effects" / f"{_checked_id(transaction_id)}.json"


async def pending_effects(store: Any, receipt: Mapping[str, Any]) -> list[tuple[int, Mapping[str, Any]]]:
    effects = receipt.get("effects") or []
    if not effects:
        return []
    raw = await read_json_or_none(effects_path(store, receipt["transaction_id"]))
    applied = set(raw.get("applied") or []) if isinstance(raw, Mapping) else set()
    return [(index, effect) for index, effect in enumerate(effects) if index not in applied]


async def effect_outcomes(store: Any, transaction_id: str) -> dict[str, str]:
    """Each applied effect's recorded result by index: its digest or a named no-op."""
    raw = await read_json_or_none(effects_path(store, transaction_id))
    return dict(raw.get("outcomes") or {}) if isinstance(raw, Mapping) else {}


async def apply_effects(store: Any, receipt: Mapping[str, Any], apply: Any) -> None:
    """Apply a COMMITTED transaction's recorded effects, each exactly once, then retire it.

    Effects are the writer's non-Card changes (a grant binding, an invocation
    policy, a credential lifetime): recorded in the prepared receipt, never
    applied before the decision (W580 F1/F3/F4). ``apply(kind, key, payload,
    transaction_id=...)`` must be idempotent by (transaction_id, kind, key):
    a crash after it and before the applied record re-applies it once more.
    Until every effect is applied the Card stays fenced and readers refuse
    (card_effects_pending), never serving AFTER without its effects.
    """

    if receipt["state"] != "committed":
        raise CardTransactionRefused("card_transaction_not_committed")
    for index, effect in await pending_effects(store, receipt):
        outcome = await apply(effect["kind"], effect["key"], dict(effect["payload"]),
                              transaction_id=receipt["transaction_id"])
        raw = await read_json_or_none(effects_path(store, receipt["transaction_id"]))
        raw = dict(raw) if isinstance(raw, Mapping) else {}
        applied = sorted(set(raw.get("applied") or []) | {index})
        # The applier's named result (an effect digest, or a named no-op such
        # as no_active_credentials) is kept per effect, never flattened.
        outcomes = dict(raw.get("outcomes") or {})
        outcomes[str(index)] = outcome if isinstance(outcome, str) and outcome else "applied"
        await write_json_atomic(effects_path(store, receipt["transaction_id"]),
                                {"applied": applied, "outcomes": outcomes})
    await _retire_pointer(store, receipt)
    await _clear_marker(store, receipt)


def tombstone_path(store: Any, transaction_id: str):
    """An ABORT finished for a transaction this participant never durably prepared."""
    return store.root / "card-transactions" / "aborted" / f"{_checked_id(transaction_id)}.json"


async def abort_unstaged(store: Any, transaction_id: str) -> dict[str, Any]:
    """FINISH(aborted) of a transaction with no prepared receipt: an idempotent abort tombstone.

    The coordinator finishes an ABORT on every intent participant, including
    one whose prepare reply was lost (W581 F1); a stage that crashed before
    its receipt also leaves only an index entry. The tombstone makes any late
    stage of this transaction refuse, and the index entry is released.
    """

    if await read_receipt(store, transaction_id) is not None:
        raise CardTransactionRefused("card_transaction_prepared")  # finish it through decide
    tombstone = {"transaction_id": transaction_id, "state": "aborted"}
    await write_json_atomic(tombstone_path(store, transaction_id), tombstone)
    await _release_catalog(store, transaction_id)  # a stage that crashed after its catalog fence
    try:
        active_path(store, transaction_id).unlink(missing_ok=True)
    except OSError:
        pass  # an unstaged entry holds no fence; recovery lists it until removed
    return tombstone


def active_path(store: Any, transaction_id: str):
    """An in-flight transaction's index entry: written before its receipt, removed once decided."""
    return store.root / "card-transactions" / "active" / f"{_checked_id(transaction_id)}.json"


def _reads_valid(reads: Any, subject_hash: Any, access_id: Any) -> bool:
    """W502 read reservations: unchanged Cards (revision >= 1) or absent ones (0), never the candidate."""
    if not isinstance(reads, list) or not reads:
        return False
    seen = set()
    for read in reads:
        if (not isinstance(read, Mapping) or set(read) != {"subject_hash", "access_id", "revision"}
                or not _HEX64.fullmatch(str(read["subject_hash"])) or type(read["access_id"]) is not str
                or not read["access_id"] or type(read["revision"]) is not int or read["revision"] < 0):
            return False
        key = (read["subject_hash"], read["access_id"])
        if key in seen or key == (subject_hash, access_id):
            return False
        seen.add(key)
    return True


def read_fence_path(store: Any, *, subject_hash: str, access_id: str):
    """A dependency Card's read fence: no write lands on it while its transaction is prepared (W502)."""
    return store.current_path(subject_hash=subject_hash, access_id=access_id).with_name("card-read-fence.json")


async def _live_read_fence(store: Any, *, subject_hash: str, access_id: str) -> dict[str, Any] | None:
    raw = await read_json_or_none(read_fence_path(store, subject_hash=subject_hash, access_id=access_id))
    if not isinstance(raw, Mapping) or not isinstance(raw.get("transaction_id"), str):
        return None
    receipt = await read_receipt(store, raw["transaction_id"])
    if receipt is None or receipt["state"] != "prepared":
        return None  # a fence of an absent or decided transaction holds nothing
    if not any((read["subject_hash"], read["access_id"]) == (subject_hash, access_id)
               for read in receipt.get("reads", ())):
        raise CardStorageError("card_transaction_read_fence_binding_invalid")
    return receipt


async def _reserve_reads(store: Any, transaction_id: str, reads: list[dict[str, Any]]) -> None:
    """Verify every dependency at its expected revision and fence it for this transaction.

    The caller holds each dependency Card's mutation section, so no write can
    interleave; fences are written BEFORE the receipt, and are live only once
    the prepared receipt exists, so a crash never leaves a live receipt with an
    unfenced dependency.
    """
    from .lifecycle_store import assert_pointer_replaceable
    for read in reads:
        owner = await _live_read_fence(store, subject_hash=read["subject_hash"], access_id=read["access_id"])
        if owner is not None and owner["transaction_id"] != transaction_id:
            raise CardTransactionRefused("card_dependency_reserved")
        if owner is None:  # a replay of this transaction already holds it
            try:
                await assert_pointer_replaceable(store, subject_hash=read["subject_hash"], access_id=read["access_id"])
            except CardStorageError as exc:
                raise CardTransactionRefused("card_dependency_reserved") from exc
        current = await store.read_current_authority(subject_hash=read["subject_hash"], access_id=read["access_id"])
        revision = 0 if current is None else current[1].card_revision
        if revision != read["revision"]:
            raise CardTransactionRefused("card_dependency_moved")
        await write_json_atomic(read_fence_path(store, subject_hash=read["subject_hash"],
                                                access_id=read["access_id"]), {"transaction_id": transaction_id})


async def _release_reads(store: Any, receipt: Mapping[str, Any]) -> None:
    for read in receipt.get("reads", ()):
        path = read_fence_path(store, subject_hash=read["subject_hash"], access_id=read["access_id"])
        raw = await read_json_or_none(path)
        if isinstance(raw, Mapping) and raw.get("transaction_id") == receipt["transaction_id"]:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass  # a decided transaction's fence holds nothing


def _validate(raw: Any, transaction_id: str) -> dict[str, Any]:
    try:
        base = {"schema", "transaction_id", "intent_digest", "participant", "subject_hash", "access_id",
                "state", "reason", "before", "after", "change_digest"}
        optional = set(raw) - base if isinstance(raw, Mapping) else set()
        if (not isinstance(raw, Mapping) or not base <= set(raw)
                or not optional <= {"effects", "reads", "catalog"}
                or ("catalog" in raw and not _HEX64.fullmatch(str(raw["catalog"])))
                or ("effects" in raw and not _effects_valid(raw["effects"]))
                or ("reads" in raw and not _reads_valid(raw["reads"], raw.get("subject_hash"), raw.get("access_id")))
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


def bind_catalog_reservations(store: Any, reservations: Any) -> None:
    """W502: the catalog's ``CatalogReservations``; a transaction naming a catalog version reserves it."""
    store._catalog_reservations = reservations


async def _reserve_catalog(store: Any, transaction_id: str, intent_digest: str, catalog: str) -> None:
    reservations = getattr(store, "_catalog_reservations", None)
    if reservations is None:
        raise CardTransactionRefused("card_catalog_reservation_unavailable")
    from ..catalog.reservations import CatalogReservationRefused
    try:
        await reservations.reserve(transaction_id=transaction_id, intent_digest=intent_digest,
                                   version_digest=catalog)
    except CatalogReservationRefused as exc:
        raise CardTransactionRefused(exc.reason) from None


async def _release_catalog(store: Any, transaction_id: str) -> None:
    reservations = getattr(store, "_catalog_reservations", None)
    if reservations is not None:
        await reservations.release(transaction_id)


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
    if state == "committed" and receipt.get("effects") and (
            receipt["state"] != "committed" or await pending_effects(store, receipt)):
        # Readiness fence: AFTER is never served without its effects.
        raise CardStorageError("card_effects_pending")
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
        await _release_reads(store, receipt)
        if receipt.get("catalog"):
            await _release_catalog(store, receipt["transaction_id"])
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
    if receipt["state"] == "committed":
        # A committed AFTER is an ordinary revision now: drop its marker so a
        # settled read never touches the receipt (Ops N1/N4). An aborted
        # revision keeps its marker and stays out of history for good.
        marker = revision_marker_path(store, subject_hash=receipt["subject_hash"], access_id=receipt["access_id"],
                                      revision_name=receipt["after"]["revision_name"])
        raw = await read_json_or_none(marker)
        if isinstance(raw, Mapping) and raw.get("transaction_id") == receipt["transaction_id"]:
            try:
                marker.unlink(missing_ok=True)
            except OSError:
                pass  # it still resolves through the committed receipt


async def list_in_doubt(store: Any) -> list[dict[str, Any]]:
    """Every transaction this participant has begun preparing and not yet finished.

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
        else:
            # A decided receipt whose entry survived (a crash before cleanup)
            # still needs FINISH re-driven to retire its pointer (Ops N3).
            entry = {key: receipt[key] for key in (
                "transaction_id", "intent_digest", "participant", "subject_hash", "access_id", "state")}
            if receipt["state"] in DECISIONS:
                entry["needs_finish"] = True
            in_doubt.append(entry)
    return in_doubt


async def assert_replaceable(store: Any, *, subject_hash: str, access_id: str) -> None:
    """Refuse an ordinary write of a Card with a prepared (undecided) transaction.

    The per-Card marker is written right after the prepared receipt, so the
    fence holds from the first durable step, before the pointer exists
    (Ops F1, 11:13).
    """

    if await _prepared_marker(store, subject_hash=subject_hash, access_id=access_id) is not None:
        raise CardStorageError("card_transaction_unresolved")
    if await _live_read_fence(store, subject_hash=subject_hash, access_id=access_id) is not None:
        raise CardStorageError("card_transaction_unresolved")  # a prepared transaction depends on it
    raw = await read_json_or_none(store.current_path(subject_hash=subject_hash, access_id=access_id))
    if raw is None or raw.get("schema") != TRANSACTION_POINTER_SCHEMA:
        return
    await resolve_pointer(store, raw, subject_hash=subject_hash, access_id=access_id, consult_decision=False)
    receipt = await read_receipt(store, raw["transaction_id"])
    if receipt["state"] == "prepared":
        raise CardStorageError("card_transaction_unresolved")
    if receipt["state"] == "committed" and await pending_effects(store, receipt):
        raise CardStorageError("card_effects_pending")  # no writer builds on an unfinished commit


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
                original: CardAuthority, candidate: CardAuthority, now: datetime,
                effects: Any = (), reads: Any = (), catalog: str = "") -> dict[str, Any]:
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
    if existing is None:
        # A late stage never opens a fence the coordinator will not finish:
        # refuse once an ABORT tombstone exists or a decision is recorded.
        if await read_json_or_none(tombstone_path(store, transaction_id)) is not None:
            raise CardTransactionRefused("card_transaction_aborted")
        if port is not None:
            try:
                recorded = await port.decision({"transaction_id": transaction_id, "intent_digest": intent_digest})
            except Exception:  # noqa: BLE001 - an unknown decision does not block a first stage
                recorded = "undecided"
            if recorded in DECISIONS:
                raise CardTransactionRefused("card_transaction_late_stage")
    if existing is not None and existing["state"] == "prepared" and port is not None:
        try:
            if await port.decision(dict(existing)) == "aborted":
                raise CardTransactionRefused("card_transaction_aborted")
        except CardTransactionRefused:
            raise
        except Exception:  # noqa: BLE001 - an unknown decision does not block resuming its own staging
            pass
    recorded_effects = [{"kind": e["kind"], "key": e["key"], "payload": dict(e["payload"])} for e in (effects or ())]
    if recorded_effects and not _effects_valid(recorded_effects):
        raise CardTransactionRefused("card_transaction_effects_invalid")
    recorded_reads = [{"subject_hash": r["subject_hash"], "access_id": r["access_id"], "revision": r["revision"]}
                      for r in (reads or ())]
    if recorded_reads and not _reads_valid(recorded_reads, subject_hash, original.access_id):
        raise CardTransactionRefused("card_transaction_reads_invalid")
    if catalog and not _HEX64.fullmatch(str(catalog)):
        raise CardTransactionRefused("card_transaction_reads_invalid")
    if existing is not None and existing["state"] in DECISIONS:
        # A decided transaction is never staged again (Ops N2).
        raise CardTransactionRefused("card_transaction_aborted" if existing["state"] == "aborted"
                                     else "card_transaction_late_stage")
    if existing is not None:
        if (existing["intent_digest"] != intent_digest
                or existing.get("effects", []) != recorded_effects
                or existing.get("reads", []) != recorded_reads
                or existing.get("catalog", "") != (catalog or "")
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
        if recorded_reads:
            await _reserve_reads(store, transaction_id, recorded_reads)  # a crash may have left one unwritten
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
    if recorded_effects:
        receipt["effects"] = recorded_effects
    if recorded_reads:
        receipt["reads"] = recorded_reads
    if catalog:
        receipt["catalog"] = catalog
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
    if recorded_reads:
        await _reserve_reads(store, transaction_id, recorded_reads)  # fenced before the receipt makes them live
    if catalog:
        # The active catalog version, held before the receipt like the Card reads; a
        # refusal leaves only the index entry (an unstaged stage), never a receipt.
        await _reserve_catalog(store, transaction_id, intent_digest, catalog)
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
        if not (decision == "committed" and await pending_effects(store, receipt)):
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
    if decision == "committed" and decided.get("effects"):
        return decided  # FINISH applies its effects (apply_effects), then retires
    await _retire_pointer(store, decided)
    await _clear_marker(store, decided)
    return decided


async def state(store: Any, *, transaction_id: str) -> dict[str, Any] | None:
    """The recorded state; recovery reads this and may only materialize it, never change it."""

    return await read_receipt(store, transaction_id)


__all__ = ["CardTransactionRefused", "DECISIONS", "TRANSACTION_POINTER_SCHEMA", "TRANSACTION_RECEIPT_SCHEMA",
           "TransactionDecisionPort", "abort_unstaged", "active_path", "read_fence_path", "apply_effects", "assert_replaceable", "bind_transaction_decisions", "decide",
           "effect_outcomes", "effects_path", "list_in_doubt", "marker_path", "pending_effects",
           "read_receipt", "resolve_pointer", "stage", "state"]

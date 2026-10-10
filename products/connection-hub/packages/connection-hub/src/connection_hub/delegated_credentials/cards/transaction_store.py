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

from ..durable_io import cancellation_safe_await, read_json_or_none, write_json_atomic
from ..issuer_gate import change_digest
from .model import CardAuthority, CardCurrentPointer, card_authority_payload_hash, card_revision_name
from .store import VERSION_RECORD_KEY, CardStorageError

TRANSACTION_POINTER_SCHEMA = "connection_hub.card-current-transaction.v1"
TRANSACTION_RECEIPT_SCHEMA = "connection_hub.card-transaction-receipt.v1"
# W578 card groups: one aggregate receipt per transaction over its member receipts.
GROUP_RECEIPT_SCHEMA = "connection_hub.card-transaction-group.v1"
# W578 read sets: a transaction that writes no Card, only holds Cards, absences and the catalog.
READ_SET_RECEIPT_SCHEMA = "connection_hub.card-transaction-read-set.v1"
# W502 lane D: a read set held by reference to a Hub-sealed collection; the receipt holds no reads.
READ_COLLECTION_RECEIPT_SCHEMA = "connection_hub.card-transaction-read-collection.v1"
# W578: a transaction that changes no Card but applies bounded effects (card_effects.py).
EFFECTS_RECEIPT_SCHEMA = "connection_hub.card-transaction-effects.v1"
MAX_GROUP_MEMBERS = 8
DECISIONS = ("committed", "aborted")
_HEX64 = re.compile(r"[0-9a-f]{64}")


class CardTransactionRefused(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def member_transaction_id(transaction_id: str, index: int) -> str:
    """A group member's storage id: derived from the group's transaction id, never asked of a coordinator."""
    import hashlib
    return hashlib.sha256(f"{_checked_id(transaction_id)}:member:{int(index)}".encode("ascii")).hexdigest()


def _decision_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """What a decision port is asked about: a group member answers by its GROUP's transaction id."""
    group = receipt.get("group")
    return {**receipt, "transaction_id": group["transaction_id"]} if isinstance(group, Mapping) else dict(receipt)


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
    if is_effects_receipt(receipt):
        return  # no Card: no pointer to retire and no serving marker
    await _retire_pointer(store, receipt)
    await _clear_marker(store, receipt)


def tombstone_path(store: Any, transaction_id: str):
    """An ABORT finished for a transaction this participant never durably prepared."""
    return store.root / "card-transactions" / "aborted" / f"{_checked_id(transaction_id)}.json"


async def abort_unstaged(store: Any, transaction_id: str, *, intent_digest: str = "") -> dict[str, Any]:
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
    # A stage that crashed after its catalog fence: the ABORT is the authenticated
    # terminal decision, so this exact intent's fence is released (CodeApp 19:29).
    await _release_catalog(store, transaction_id, intent_digest)
    try:
        active_path(store, transaction_id).unlink(missing_ok=True)
    except OSError:
        pass  # an unstaged entry holds no fence; recovery lists it until removed
    return tombstone


def active_path(store: Any, transaction_id: str):
    """An in-flight transaction's index entry: written before its receipt, removed once decided."""
    return store.root / "card-transactions" / "active" / f"{_checked_id(transaction_id)}.json"


def _active_entry(transaction_id: str, collection_id: str) -> dict[str, Any]:
    """The index entry names the sealed collection the transaction holds ("" for none), so retention can
    protect it even while the entry is unstaged (no receipt yet)."""
    return {"transaction_id": transaction_id, "collection_id": collection_id}


async def protected_collections(store: Any) -> set[str] | None:
    """Every sealed collection an in-flight transaction names, or None when that cannot be known.

    Read from the bounded active index: the entry's own ``collection_id``, else
    its receipt (a read-collection receipt, an ordinary receipt's ``collection``,
    or a group aggregate followed to its lead). An entry without the field and
    without a receipt (written before entries named collections), a group whose
    lead cannot be read, or more entries than the bound: None, and retention
    deletes nothing (fails closed).
    """
    import asyncio

    from .update_store import UPDATE_RECEIPT_SCHEMA

    directory = store.root / "card-transactions" / "active"

    def names():
        try:
            return [path.stem for path in itertools.islice(directory.iterdir(), MAX_ACTIVE_TRANSACTIONS + 1)
                    if path.is_file() and path.suffix == ".json"]
        except FileNotFoundError:
            return []

    found = await asyncio.to_thread(names)
    if len(found) > MAX_ACTIVE_TRANSACTIONS:
        return None
    protected: set[str] = set()
    try:
        for transaction_id in sorted(found):
            if not _HEX64.fullmatch(transaction_id):
                continue  # an atomic-write temporary, not an entry
            raw = await read_json_or_none(active_path(store, transaction_id))
            if raw is None:
                continue  # removed since listed: decided
            if isinstance(raw, Mapping) and type(raw.get("collection_id")) is str:
                if raw["collection_id"]:
                    protected.add(raw["collection_id"])
                continue
            if isinstance(raw, Mapping) and raw.get("schema") == UPDATE_RECEIPT_SCHEMA:
                continue  # an issuer update's entry is its own receipt and holds no collection
            receipt = await read_receipt(store, transaction_id)
            if receipt is None:
                return None
            if is_group_receipt(receipt):
                receipt = await read_receipt(store, receipt["members"][0]["transaction_id"])
                if receipt is None:
                    return None
            collection_id = _receipt_collection_id(receipt)
            if collection_id:
                protected.add(collection_id)
    except CardStorageError:
        return None
    return protected


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
    collection_id = _receipt_collection_id(receipt)
    if collection_id:
        # Bound through the sealed collection's own leaf, never a scan of an N-element receipt (lane D).
        from .card_read_collection import leaf
        if (raw.get("collection_id") != collection_id
                or await leaf(store, collection_id, subject_hash=subject_hash, access_id=access_id) is None):
            raise CardStorageError("card_transaction_read_fence_binding_invalid")
        return receipt
    if not any((read["subject_hash"], read["access_id"]) == (subject_hash, access_id)
               for read in receipt.get("reads", ())):
        raise CardStorageError("card_transaction_read_fence_binding_invalid")
    return receipt


async def _reserve_reads(store: Any, transaction_id: str, reads: list[dict[str, Any]], *,
                         collection_id: str = "") -> None:
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
                                                access_id=read["access_id"]),
                                {"transaction_id": transaction_id, **({"collection_id": collection_id}
                                                                      if collection_id else {})})


async def _release_reads(store: Any, receipt: Mapping[str, Any], reads: Any = None) -> None:
    for read in (receipt.get("reads", ()) if reads is None else reads):
        path = read_fence_path(store, subject_hash=read["subject_hash"], access_id=read["access_id"])
        raw = await read_json_or_none(path)
        if isinstance(raw, Mapping) and raw.get("transaction_id") == receipt["transaction_id"]:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass  # a decided transaction's fence holds nothing


def _group_ref_valid(group: Any, transaction_id: str) -> bool:
    """A member receipt's group reference: its group's id, its index and the group's lead member id."""
    return (isinstance(group, Mapping) and set(group) == {"transaction_id", "index", "lead"}
            and type(group["transaction_id"]) is str and _HEX64.fullmatch(group["transaction_id"]) is not None
            and type(group["index"]) is int and 0 <= group["index"] < MAX_GROUP_MEMBERS
            and transaction_id == member_transaction_id(group["transaction_id"], group["index"])
            and group["lead"] == member_transaction_id(group["transaction_id"], 0))


def _validate(raw: Any, transaction_id: str) -> dict[str, Any]:
    if isinstance(raw, Mapping) and raw.get("schema") == GROUP_RECEIPT_SCHEMA:
        return _validate_group(raw, transaction_id)
    if isinstance(raw, Mapping) and raw.get("schema") == READ_SET_RECEIPT_SCHEMA:
        return _validate_read_set(raw, transaction_id)
    if isinstance(raw, Mapping) and raw.get("schema") == EFFECTS_RECEIPT_SCHEMA:
        return _validate_effects(raw, transaction_id)
    if isinstance(raw, Mapping) and raw.get("schema") == READ_COLLECTION_RECEIPT_SCHEMA:
        return _validate_read_collection(raw, transaction_id)
    try:
        base = {"schema", "transaction_id", "intent_digest", "participant", "subject_hash", "access_id",
                "state", "reason", "before", "after", "change_digest"}
        optional = set(raw) - base if isinstance(raw, Mapping) else set()
        if (not isinstance(raw, Mapping) or not base <= set(raw)
                or not optional <= {"effects", "reads", "catalog", "group", "collection"}
                or ("group" in raw and not _group_ref_valid(raw["group"], transaction_id))
                # A group's reads (or collection), catalog and effects ride on its lead member only.
                or ("group" in raw and raw["group"]["index"] != 0
                    and any(name in raw for name in ("effects", "reads", "catalog", "collection")))
                # W502 lane D: a write group's dependencies by reference, never with enumerated reads.
                or ("collection" in raw and ("reads" in raw or not _collection_ref_valid(raw["collection"])))
                or ("catalog" in raw and not _HEX64.fullmatch(str(raw["catalog"])))
                or ("effects" in raw and not _effects_valid(raw["effects"]))
                or ("reads" in raw and not _reads_valid(raw["reads"], raw.get("subject_hash"), raw.get("access_id")))
                or raw["schema"] != TRANSACTION_RECEIPT_SCHEMA or raw["transaction_id"] != transaction_id
                or raw["state"] not in ("prepared", *DECISIONS)
                or not _HEX64.fullmatch(str(raw["intent_digest"])) or not _HEX64.fullmatch(str(raw["change_digest"]))
                or type(raw["participant"]) is not str or not raw["participant"]
                or type(raw["reason"]) is not str):
            raise ValueError()
        after = CardCurrentPointer.from_mapping(raw["after"])
        if after.access_id != raw["access_id"] or after.to_dict() != raw["after"]:
            raise ValueError()
        if raw["before"] is None:
            # W578: an absent original, only for a group member creating a newly minted id.
            if "group" not in raw or after.card_revision != 1:
                raise ValueError()
        else:
            before = CardCurrentPointer.from_mapping(raw["before"])
            if (before.access_id != raw["access_id"] or before.to_dict() != raw["before"]
                    or after.card_revision != before.card_revision + 1):
                raise ValueError()
        return dict(raw)
    except (KeyError, ValueError, TypeError) as exc:
        raise CardStorageError("card_transaction_receipt_invalid") from exc


def _validate_group(raw: Any, transaction_id: str) -> dict[str, Any]:
    """The aggregate receipt: proves the complete member set once ``staged`` (W578 card groups)."""
    try:
        if (set(raw) != {"schema", "transaction_id", "intent_digest", "participant", "state", "reason", "staged",
                         "members"}
                or raw["transaction_id"] != transaction_id or raw["state"] not in ("prepared", *DECISIONS)
                or not _HEX64.fullmatch(str(raw["intent_digest"])) or type(raw["staged"]) is not bool
                or type(raw["participant"]) is not str or not raw["participant"] or type(raw["reason"]) is not str
                or type(raw["members"]) is not list or not 1 <= len(raw["members"]) <= MAX_GROUP_MEMBERS):
            raise ValueError()
        keys = []
        for index, member in enumerate(raw["members"]):
            if (not isinstance(member, Mapping) or set(member) != {"transaction_id", "subject_hash", "access_id"}
                    or member["transaction_id"] != member_transaction_id(transaction_id, index)
                    or not _HEX64.fullmatch(str(member["subject_hash"]))
                    or type(member["access_id"]) is not str or not member["access_id"]):
                raise ValueError()
            keys.append((member["subject_hash"], member["access_id"]))
        if len(set(keys)) != len(keys) or keys != sorted(keys):
            raise ValueError()
        if raw["state"] == "committed" and not raw["staged"]:
            raise ValueError()  # a commit is only ever of a complete group
        return dict(raw)
    except (KeyError, ValueError, TypeError) as exc:
        raise CardStorageError("card_transaction_receipt_invalid") from exc


def _validate_read_set(raw: Any, transaction_id: str) -> dict[str, Any]:
    """A read set's receipt: its held reads and catalog, and its decision; it names no target Card."""
    try:
        if (set(raw) != {"schema", "transaction_id", "intent_digest", "participant", "state", "reason", "reads",
                         "catalog"}
                or raw["transaction_id"] != transaction_id or raw["state"] not in ("prepared", *DECISIONS)
                or not _HEX64.fullmatch(str(raw["intent_digest"])) or type(raw["reason"]) is not str
                or type(raw["participant"]) is not str or not raw["participant"]
                or type(raw["catalog"]) is not str or (raw["catalog"] and not _HEX64.fullmatch(raw["catalog"]))
                or type(raw["reads"]) is not list or (not raw["reads"] and not raw["catalog"])
                or (raw["reads"] and not _reads_valid(raw["reads"], None, None))):
            raise ValueError()
        return dict(raw)
    except (KeyError, ValueError, TypeError) as exc:
        raise CardStorageError("card_transaction_receipt_invalid") from exc


def _validate_read_collection(raw: Any, transaction_id: str) -> dict[str, Any]:
    """A by-reference read set's receipt: its collection ref, root, count and catalog, and its decision only."""
    try:
        if (set(raw) != {"schema", "transaction_id", "intent_digest", "participant", "state", "reason",
                         "collection_id", "root", "count", "catalog"}
                or raw["transaction_id"] != transaction_id or raw["state"] not in ("prepared", *DECISIONS)
                or not _HEX64.fullmatch(str(raw["intent_digest"])) or type(raw["reason"]) is not str
                or type(raw["participant"]) is not str or not raw["participant"]
                or type(raw["collection_id"]) is not str or not re.fullmatch(r"[0-9a-f]{32}", raw["collection_id"])
                or type(raw["root"]) is not str or not _HEX64.fullmatch(raw["root"])
                or type(raw["count"]) is not int or raw["count"] < 1
                or type(raw["catalog"]) is not str or (raw["catalog"] and not _HEX64.fullmatch(raw["catalog"]))):
            raise ValueError()
        return dict(raw)
    except (KeyError, ValueError, TypeError) as exc:
        raise CardStorageError("card_transaction_receipt_invalid") from exc


def _collection_ref_valid(value: Any) -> bool:
    return (isinstance(value, Mapping) and set(value) == {"collection_id", "root", "count"}
            and type(value["collection_id"]) is str and re.fullmatch(r"[0-9a-f]{32}", value["collection_id"]) is not None
            and type(value["root"]) is str and _HEX64.fullmatch(value["root"]) is not None
            and type(value["count"]) is int and value["count"] >= 1)


def _receipt_collection_id(receipt: Mapping[str, Any]) -> str:
    """The sealed collection a receipt holds its read fences through ("" when it enumerates its reads)."""
    if is_read_collection_receipt(receipt):
        return receipt["collection_id"]
    return (receipt.get("collection") or {}).get("collection_id", "")


async def _collection_reads(store: Any, receipt: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The fences to release. A DECIDED receipt's fences hold nothing (``_live_read_fence``), so once
    retention deleted its collection (wholly or partly) their release is inert and skipped; every other
    FINISH duty still runs. A prepared receipt's collection is protected; missing, it refuses."""
    from .card_read_collection import resolve_collection
    try:
        _, reads = await resolve_collection(store, _receipt_collection_id(receipt))
    except CardStorageError as exc:
        if receipt["state"] in DECISIONS and str(exc) in ("card_read_collection_unknown",
                                                          "card_read_collection_incomplete"):
            return []
        raise CardTransactionRefused(str(exc)) from exc
    return reads


async def _require_collection_before_decision(store: Any, receipt: Mapping[str, Any]) -> None:
    """A PREPARED receipt's collection must resolve BEFORE its decision is written: one that is missing
    refuses and the receipt stays in doubt; only an already decided receipt tolerates its absence."""
    if _receipt_collection_id(receipt):
        from .card_read_collection import resolve_collection
        try:
            await resolve_collection(store, _receipt_collection_id(receipt))
        except CardStorageError as exc:
            raise CardTransactionRefused(str(exc)) from exc


def is_read_collection_receipt(receipt: Mapping[str, Any] | None) -> bool:
    return isinstance(receipt, Mapping) and receipt.get("schema") == READ_COLLECTION_RECEIPT_SCHEMA


def _validate_effects(raw: Any, transaction_id: str) -> dict[str, Any]:
    """An effects-only receipt: its owner, its exact effects and its decision; it names no Card."""
    try:
        if (set(raw) != {"schema", "transaction_id", "intent_digest", "participant", "state", "reason",
                         "subject_hash", "effects"}
                or raw["transaction_id"] != transaction_id or raw["state"] not in ("prepared", *DECISIONS)
                or not _HEX64.fullmatch(str(raw["intent_digest"])) or not _HEX64.fullmatch(str(raw["subject_hash"]))
                or type(raw["reason"]) is not str or type(raw["participant"]) is not str or not raw["participant"]
                or not _effects_valid(raw["effects"])):
            raise ValueError()
        return dict(raw)
    except (KeyError, ValueError, TypeError) as exc:
        raise CardStorageError("card_transaction_receipt_invalid") from exc


def is_effects_receipt(receipt: Mapping[str, Any] | None) -> bool:
    return isinstance(receipt, Mapping) and receipt.get("schema") == EFFECTS_RECEIPT_SCHEMA


def is_read_set_receipt(receipt: Mapping[str, Any] | None) -> bool:
    return isinstance(receipt, Mapping) and receipt.get("schema") == READ_SET_RECEIPT_SCHEMA


def is_group_receipt(receipt: Mapping[str, Any] | None) -> bool:
    return isinstance(receipt, Mapping) and receipt.get("schema") == GROUP_RECEIPT_SCHEMA


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


async def _release_catalog(store: Any, transaction_id: str, intent_digest: str) -> None:
    """Only on an authenticated terminal decision, and only this exact intent's fence."""
    reservations = getattr(store, "_catalog_reservations", None)
    if reservations is not None and intent_digest:
        await reservations.release(transaction_id, intent_digest=intent_digest)


async def _authoritative_state(store: Any, receipt: Mapping[str, Any]) -> str:
    """The decision a reader must follow: the local receipt once decided, else the coordinator's."""

    if receipt["state"] in DECISIONS:
        return receipt["state"]
    port = getattr(store, "_card_transaction_decisions", None)
    if port is None:
        raise CardStorageError("card_transaction_undecided")
    try:
        decision = await port.decision(_decision_receipt(receipt))
    except Exception as exc:  # noqa: BLE001 - an unreachable coordinator decides nothing
        raise CardStorageError("card_transaction_undecided") from exc
    if decision not in DECISIONS:
        raise CardStorageError("card_transaction_undecided")
    return decision


async def _effects_outstanding(store: Any, receipt: Mapping[str, Any]) -> bool:
    """A committed transaction whose effects are not all applied (a group's live on its lead member)."""
    group = receipt.get("group")
    lead = receipt
    if isinstance(group, Mapping) and group["index"] != 0:
        lead = await read_receipt(store, group["lead"])
        if lead is None:
            raise CardStorageError("card_transaction_group_lead_missing")
    return bool(lead.get("effects")) and (lead["state"] != "committed" or bool(await pending_effects(store, lead)))


def _pointer_or_absent(side: Any) -> CardCurrentPointer | None:
    return None if side is None else CardCurrentPointer.from_mapping(side)


async def resolve_pointer(store: Any, payload: Any, *, subject_hash: str, access_id: str,
                          consult_decision: bool = True) -> CardCurrentPointer | None:
    """The pointer a reader uses, by the coordinator's recorded decision.

    COMMITTED reads AFTER and ABORTED reads BEFORE, even before the local
    receipt materializes it; an undecided or unreachable decision refuses,
    only for this staged Card. Internal validation passes
    ``consult_decision=False`` and gets BEFORE while prepared. A group member
    created from an absent original reads absent (None) on that side.
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
        return _pointer_or_absent(receipt["after" if receipt["state"] == "committed" else "before"])
    state = await _authoritative_state(store, receipt)
    if state == "committed" and await _effects_outstanding(store, receipt):
        # Readiness fence: AFTER is never served without its effects, for any member of a group.
        raise CardStorageError("card_effects_pending")
    return _pointer_or_absent(receipt["after" if state == "committed" else "before"])


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
        await _release_reads(store, receipt, await _collection_reads(store, receipt) if "collection" in receipt else None)
        if receipt.get("catalog"):
            await _release_catalog(store, receipt["transaction_id"], receipt["intent_digest"])
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
        if side is None:
            # An aborted create of a newly minted id: the slot is absent again. The
            # staged revision keeps its marker, so it never appears in history.
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                raise CardStorageError("card_transaction_retire_failed") from exc
        else:
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
        elif is_read_set_receipt(receipt) or is_read_collection_receipt(receipt):
            entry = {key: receipt[key] for key in ("transaction_id", "intent_digest", "participant", "state")}
            entry["read_set"] = True
            if receipt["state"] in DECISIONS:
                entry["needs_finish"] = True
            in_doubt.append(entry)
        elif is_effects_receipt(receipt):
            entry = {key: receipt[key] for key in ("transaction_id", "intent_digest", "participant", "state")}
            entry["effects_only"] = True
            if receipt["state"] in DECISIONS:
                entry["needs_finish"] = True
            in_doubt.append(entry)
        elif is_group_receipt(receipt):
            # A card group is recovered by its own id; its members never have entries.
            entry = {key: receipt[key] for key in ("transaction_id", "intent_digest", "participant", "state")}
            entry["group"] = True
            if receipt["state"] in DECISIONS:
                entry["needs_finish"] = True
            elif not receipt["staged"]:
                entry["staged"] = False  # never prepared as a whole: only an ABORT can finish it
            in_doubt.append(entry)
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
    if receipt["state"] == "committed" and await _effects_outstanding(store, receipt):
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
                original: CardAuthority | None, candidate: CardAuthority, now: datetime,
                effects: Any = (), reads: Any = (), catalog: str = "",
                group: Mapping[str, Any] | None = None, collection: Mapping[str, Any] | None = None,
                collection_reads: Any = ()) -> dict[str, Any]:
    """Stage ``candidate`` behind a transaction pointer; nothing becomes visible. Caller holds the fence.

    W502 lane D: ``collection`` ({collection_id, root, count}) holds this
    member's dependencies by reference instead of ``reads``; the caller passes
    the resolved ``collection_reads`` (``resolve_collection``), which are fenced
    with this transaction AND collection; the receipt records only the ref.

    A replay of a prepared transaction RESUMES its missing steps, but only
    while the Card is still exactly the receipt's BEFORE; otherwise it is
    refused and the coordinator must abort (Ops F1).

    W578: ``group`` stages one member of a card group under its group's
    decision (``{transaction_id, index, lead}``, ``transaction_id`` here being
    the member's derived id). Only a group member may have an absent
    ``original`` (a newly minted id): it stages candidate revision 1 into a
    slot with no current pointer and no committed history.
    """

    _checked_id(transaction_id)
    if group is not None and not _group_ref_valid(group, transaction_id):
        raise CardTransactionRefused("card_transaction_group_invalid")
    if original is None and group is None:
        raise CardTransactionRefused("card_transaction_candidate_invalid")
    access_id = candidate.access_id
    decision_key = {"transaction_id": group["transaction_id"] if group else transaction_id,
                    "intent_digest": intent_digest}
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
                recorded = await port.decision(decision_key)
            except Exception:  # noqa: BLE001 - an unknown decision does not block a first stage
                recorded = "undecided"
            if recorded in DECISIONS:
                raise CardTransactionRefused("card_transaction_late_stage")
    if existing is not None and (is_group_receipt(existing) or is_read_set_receipt(existing)):
        raise CardTransactionRefused("card_transaction_replay_changed")
    if existing is not None and existing["state"] == "prepared" and port is not None:
        try:
            if await port.decision(_decision_receipt(existing)) == "aborted":
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
    if recorded_reads and not _reads_valid(recorded_reads, subject_hash, access_id):
        raise CardTransactionRefused("card_transaction_reads_invalid")
    if catalog and not _HEX64.fullmatch(str(catalog)):
        raise CardTransactionRefused("card_transaction_reads_invalid")
    held = recorded_reads
    recorded_collection = None
    if collection is not None:
        recorded_collection = {key: collection.get(key) for key in ("collection_id", "root", "count")} \
            if isinstance(collection, Mapping) else None
        held = [{"subject_hash": r["subject_hash"], "access_id": r["access_id"], "revision": r["revision"]}
                for r in (collection_reads or ())]
        if (recorded_reads or not _collection_ref_valid(recorded_collection)
                or len(held) != recorded_collection["count"] or not _reads_valid(held, subject_hash, access_id)):
            raise CardTransactionRefused("card_transaction_reads_invalid")
    if existing is not None and existing["state"] in DECISIONS:
        # A decided transaction is never staged again (Ops N2).
        raise CardTransactionRefused("card_transaction_aborted" if existing["state"] == "aborted"
                                     else "card_transaction_late_stage")
    if existing is not None:
        if (existing["intent_digest"] != intent_digest
                or existing.get("effects", []) != recorded_effects
                or existing.get("reads", []) != recorded_reads
                or existing.get("collection") != recorded_collection
                or existing.get("catalog", "") != (catalog or "")
                or existing["change_digest"] != change_digest(candidate.to_dict())
                or existing.get("group") != (dict(group) if group else None)
                or existing["access_id"] != access_id or existing["subject_hash"] != subject_hash):
            raise CardTransactionRefused("card_transaction_replay_changed")
        if existing["state"] != "prepared" or await _is_staged(store, existing):
            return existing
        raw = await read_json_or_none(store.current_path(subject_hash=subject_hash, access_id=access_id))
        current = None if raw is None else (
            await resolve_pointer(store, raw, subject_hash=subject_hash, access_id=access_id,
                                  consult_decision=False)
            if raw.get("schema") == TRANSACTION_POINTER_SCHEMA else CardCurrentPointer.from_mapping(raw))
        if (current.to_dict() if current is not None else None) != existing["before"]:
            raise CardTransactionRefused("card_transaction_revision_moved")
        if held:  # a crash may have left one unwritten
            await _reserve_reads(store, transaction_id, held,
                                 collection_id=recorded_collection["collection_id"] if recorded_collection else "")
        await _write_staged(store, existing, candidate, now)
        return existing
    # A fresh stage passes the FULL shared fence: an unresolved issuer UPDATE
    # or lifecycle intent awaiting recovery is never overwritten (Ops F2).
    from .lifecycle_store import assert_pointer_replaceable
    await assert_pointer_replaceable(store, subject_hash=subject_hash, access_id=access_id)
    current = await store.read_current_authority(subject_hash=subject_hash, access_id=access_id)
    if original is None:
        # An absent original: no current Card. W661 (operator, 10 Oct: "never nothing is being scanned";
        # an invitation "IS new card ... UPSERT. overwrite"): no history scan and no "slot used" refusal.
        if current is not None:
            raise CardTransactionRefused("card_transaction_base_moved")
        if candidate.card_revision != 1:
            raise CardTransactionRefused("card_transaction_candidate_invalid")
        before = None
    else:
        if current is None or current[1].to_dict() != original.to_dict():
            raise CardTransactionRefused("card_transaction_revision_moved")
        if candidate.access_id != original.access_id or candidate.card_revision != original.card_revision + 1:
            raise CardTransactionRefused("card_transaction_candidate_invalid")
        before = current[0]
    digest = candidate.content_hash()
    after = CardCurrentPointer.for_revision(candidate, content_hash=digest, revision_name=card_revision_name(
        card_revision=candidate.card_revision, content_hash=digest, updated_at=now), updated_at=now)
    receipt = {"schema": TRANSACTION_RECEIPT_SCHEMA, "transaction_id": transaction_id,
               "intent_digest": intent_digest, "participant": participant.strip(), "subject_hash": subject_hash,
               "access_id": access_id, "state": "prepared", "reason": "",
               "before": before.to_dict() if before is not None else None, "after": after.to_dict(),
               "change_digest": change_digest(candidate.to_dict())}
    if group is not None:
        receipt["group"] = dict(group)
    if recorded_effects:
        receipt["effects"] = recorded_effects
    if recorded_reads:
        receipt["reads"] = recorded_reads
    if recorded_collection is not None:
        receipt["collection"] = recorded_collection
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
    # 0. The in-flight index entry first, so recovery can always find it. A
    #    group member has none: recovery finds it through its group's entry,
    #    and a coordinator is only ever asked about the group's id.
    if group is None:
        await write_json_atomic(active_path(store, transaction_id), _active_entry(
            transaction_id, recorded_collection["collection_id"] if recorded_collection else ""))
    if held:  # fenced before the receipt makes them live
        await _reserve_reads(store, transaction_id, held,
                             collection_id=recorded_collection["collection_id"] if recorded_collection else "")
    if catalog:
        # The active catalog version, held before the receipt like the Card reads; a
        # refusal leaves only the index entry (an unstaged stage), never a receipt.
        await _reserve_catalog(store, transaction_id, intent_digest, catalog)
    await write_json_atomic(receipt_path(store, transaction_id), receipt)
    await write_json_atomic(marker_path(store, subject_hash=subject_hash, access_id=access_id),
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
    if is_group_receipt(receipt):
        raise CardTransactionRefused("card_transaction_is_group")  # finish_group materializes a group
    if is_read_set_receipt(receipt):
        raise CardTransactionRefused("card_transaction_is_read_set")  # finish_read_set releases a read set
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
    if decision == "committed" and isinstance(receipt.get("group"), Mapping):
        # EMain F1, the store's own line: a member commits only as part of a group staged as a whole.
        aggregate = await read_receipt(store, receipt["group"]["transaction_id"])
        if not is_group_receipt(aggregate) or not aggregate["staged"]:
            raise CardTransactionRefused("card_transaction_not_staged")
    # Local decide only MATERIALIZES the coordinator's recorded decision; it is
    # never a second, independent business decision (CodeApp, 11:14).
    port = getattr(store, "_card_transaction_decisions", None)
    if port is None:
        raise CardTransactionRefused("card_transaction_decision_unverified")
    try:
        recorded = await port.decision(_decision_receipt(receipt))
    except Exception as exc:  # noqa: BLE001
        raise CardTransactionRefused("card_transaction_decision_unverified") from exc
    if recorded != decision:
        raise CardTransactionRefused("card_transaction_decision_not_recorded")
    await _require_collection_before_decision(store, receipt)
    decided = {**receipt, "state": decision, "reason": str(reason or "")[:128]}
    _validate(decided, transaction_id)
    # The one visibility point: the receipt rename. COMMITTED readers get AFTER.
    await write_json_atomic(receipt_path(store, transaction_id), decided)
    if decision == "committed" and decided.get("effects"):
        return decided  # FINISH applies its effects (apply_effects), then retires
    await _retire_pointer(store, decided)
    await _clear_marker(store, decided)
    return decided


# ── W578 card groups: several Cards under ONE transaction decision ──────────
#
# A group's aggregate receipt lives at the group's own receipt path and is
# written BEFORE any member, with ``staged`` false; each member is then staged
# as an ordinary Card receipt under a derived id (``member_transaction_id``)
# carrying ``group`` (no index entry of its own, decisions asked by the
# group's id); only after every member is staged is the aggregate marked
# ``staged``, which is what a prepare acknowledgement reports. Finish
# materializes every member through ``decide`` and writes the aggregate's
# decision LAST, so a crash at any point leaves an aggregate that recovery
# finishes again, idempotently, by the group's id. Readers of any member
# follow the coordinator's one recorded decision for the group.


async def begin_group(store: Any, *, transaction_id: str, intent_digest: str, participant: str,
                      members: list[tuple[str, str]], collection_id: str = "") -> dict[str, Any]:
    """Write (or replay) the aggregate receipt naming every member, before any member is staged."""
    _checked_id(transaction_id)
    if not _HEX64.fullmatch(str(intent_digest or "")) or not str(participant or "").strip():
        raise CardTransactionRefused("card_transaction_intent_invalid")
    keys = [(str(subject_hash), str(access_id)) for subject_hash, access_id in members]
    if not 1 <= len(keys) <= MAX_GROUP_MEMBERS or len(set(keys)) != len(keys) or keys != sorted(keys):
        raise CardTransactionRefused("card_transaction_group_invalid")
    receipt = {"schema": GROUP_RECEIPT_SCHEMA, "transaction_id": transaction_id, "intent_digest": intent_digest,
               "participant": participant.strip(), "state": "prepared", "reason": "", "staged": False,
               "members": [{"transaction_id": member_transaction_id(transaction_id, index),
                            "subject_hash": subject_hash, "access_id": access_id}
                           for index, (subject_hash, access_id) in enumerate(keys)]}
    _validate_group(receipt, transaction_id)
    existing = await read_receipt(store, transaction_id)
    if existing is not None:
        if not is_group_receipt(existing) or any(
                existing[name] != receipt[name] for name in ("intent_digest", "participant", "members")):
            raise CardTransactionRefused("card_transaction_replay_changed")
        if existing["state"] in DECISIONS:
            raise CardTransactionRefused("card_transaction_aborted" if existing["state"] == "aborted"
                                         else "card_transaction_late_stage")
        return existing
    if await read_json_or_none(tombstone_path(store, transaction_id)) is not None:
        raise CardTransactionRefused("card_transaction_aborted")
    port = getattr(store, "_card_transaction_decisions", None)
    if port is not None:
        try:
            recorded = await port.decision({"transaction_id": transaction_id, "intent_digest": intent_digest})
        except Exception:  # noqa: BLE001 - an unknown decision does not block a first stage
            recorded = "undecided"
        if recorded in DECISIONS:
            raise CardTransactionRefused("card_transaction_late_stage")
    # The group's entry names its lead's collection, so retention protects it before the lead is staged.
    await write_json_atomic(active_path(store, transaction_id), _active_entry(transaction_id, collection_id))
    await write_json_atomic(receipt_path(store, transaction_id), receipt)
    return receipt


def group_member_ref(group: Mapping[str, Any], index: int) -> dict[str, Any]:
    """The ``group`` reference a member receipt carries."""
    return {"transaction_id": group["transaction_id"], "index": index,
            "lead": member_transaction_id(group["transaction_id"], 0)}


async def complete_group(store: Any, *, transaction_id: str, intent_digest: str) -> dict[str, Any]:
    """Mark the aggregate staged once EVERY member's receipt is prepared and its pointer in place."""
    receipt = await read_receipt(store, transaction_id)
    if not is_group_receipt(receipt) or receipt["intent_digest"] != intent_digest:
        raise CardTransactionRefused("card_transaction_group_invalid")
    if receipt["state"] != "prepared":
        raise CardTransactionRefused("card_transaction_aborted" if receipt["state"] == "aborted"
                                     else "card_transaction_late_stage")
    if receipt["staged"]:
        return receipt
    for index, member in enumerate(receipt["members"]):
        local = await read_receipt(store, member["transaction_id"])
        if (local is None or local.get("group") != group_member_ref(receipt, index)
                or local["intent_digest"] != intent_digest or local["state"] != "prepared"
                or (local["subject_hash"], local["access_id"]) != (member["subject_hash"], member["access_id"])
                or not await _is_staged(store, local)):
            raise CardTransactionRefused("card_transaction_group_incomplete")
    staged = {**receipt, "staged": True}
    _validate_group(staged, transaction_id)
    await write_json_atomic(receipt_path(store, transaction_id), staged)
    return staged


async def finish_group(store: Any, *, transaction_id: str, intent_digest: str, decision: str,
                       reason: str = "") -> dict[str, Any]:
    """Record the aggregate's decision AFTER every member materialized it; idempotent.

    The caller (``DelegatedCardService.decide_group_transaction``) decides each
    member through ``decide`` first. COMMITTED requires a staged group whose
    every member is committed; ABORTED requires every member that was ever
    staged to be aborted (a member never reached has no receipt).
    """
    if decision not in DECISIONS:
        raise CardTransactionRefused("card_transaction_decision_invalid")
    receipt = await read_receipt(store, transaction_id)
    if not is_group_receipt(receipt):
        raise CardTransactionRefused("card_transaction_unknown")
    if receipt["intent_digest"] != intent_digest:
        raise CardTransactionRefused("card_transaction_intent_mismatch")
    if receipt["state"] in DECISIONS:
        if receipt["state"] != decision:
            raise CardTransactionRefused("card_transaction_decision_conflict")
        await _clear_group(store, receipt)
        return receipt
    if decision == "committed" and not receipt["staged"]:
        raise CardTransactionRefused("card_transaction_not_staged")
    port = getattr(store, "_card_transaction_decisions", None)
    if port is None:
        raise CardTransactionRefused("card_transaction_decision_unverified")
    try:
        recorded = await port.decision({"transaction_id": transaction_id, "intent_digest": intent_digest})
    except Exception as exc:  # noqa: BLE001
        raise CardTransactionRefused("card_transaction_decision_unverified") from exc
    if recorded != decision:
        raise CardTransactionRefused("card_transaction_decision_not_recorded")
    for member in receipt["members"]:
        local = await read_receipt(store, member["transaction_id"])
        if local is None and decision == "aborted":
            continue
        if local is None or local["state"] != decision:
            raise CardTransactionRefused("card_transaction_group_members_pending")
    decided = {**receipt, "state": decision, "reason": str(reason or "")[:128]}
    _validate_group(decided, transaction_id)
    await write_json_atomic(receipt_path(store, transaction_id), decided)
    await _clear_group(store, decided)
    return decided


async def _clear_group(store: Any, receipt: Mapping[str, Any]) -> None:
    if receipt["state"] in DECISIONS:
        try:
            active_path(store, receipt["transaction_id"]).unlink(missing_ok=True)
        except OSError:
            pass  # a stale entry lists a decided group, which recovery re-finishes idempotently


# ── W578 read sets: hold Cards, absences and the catalog under one decision, write nothing ──


async def prepare_read_set(store: Any, *, transaction_id: str, intent_digest: str, participant: str,
                           reads: Any, catalog: str = "") -> dict[str, Any]:
    """Verify and fence every read (and reserve the catalog) BEFORE the receipt; idempotent replay.

    The caller holds every read Card's mutation section. Each read must be
    exactly at its revision (0: absent); it is then fenced for this
    transaction, so no write lands on it until the decision. Nothing is
    staged and nothing becomes visible.
    """
    _checked_id(transaction_id)
    if not _HEX64.fullmatch(str(intent_digest or "")) or not str(participant or "").strip():
        raise CardTransactionRefused("card_transaction_intent_invalid")
    recorded_reads = sorted(({"subject_hash": r["subject_hash"], "access_id": r["access_id"], "revision": r["revision"]}
                             for r in (reads or ())), key=lambda r: (r["subject_hash"], r["access_id"]))
    if (recorded_reads and not _reads_valid(recorded_reads, None, None)) or (not recorded_reads and not catalog) \
            or (catalog and not _HEX64.fullmatch(str(catalog))):
        raise CardTransactionRefused("card_transaction_reads_invalid")
    receipt = {"schema": READ_SET_RECEIPT_SCHEMA, "transaction_id": transaction_id, "intent_digest": intent_digest,
               "participant": participant.strip(), "state": "prepared", "reason": "", "reads": recorded_reads,
               "catalog": catalog or ""}
    existing = await read_receipt(store, transaction_id)
    if existing is not None:
        if not is_read_set_receipt(existing) or any(
                existing[name] != receipt[name] for name in ("intent_digest", "participant", "reads", "catalog")):
            raise CardTransactionRefused("card_transaction_replay_changed")
        if existing["state"] in DECISIONS:
            raise CardTransactionRefused("card_transaction_aborted" if existing["state"] == "aborted"
                                         else "card_transaction_late_stage")
        return existing
    if await read_json_or_none(tombstone_path(store, transaction_id)) is not None:
        raise CardTransactionRefused("card_transaction_aborted")
    port = getattr(store, "_card_transaction_decisions", None)
    if port is not None:
        try:
            recorded = await port.decision({"transaction_id": transaction_id, "intent_digest": intent_digest})
        except Exception:  # noqa: BLE001 - an unknown decision does not block a first prepare
            recorded = "undecided"
        if recorded in DECISIONS:
            raise CardTransactionRefused("card_transaction_late_stage")
    await write_json_atomic(active_path(store, transaction_id), _active_entry(transaction_id, ""))
    if recorded_reads:
        await _reserve_reads(store, transaction_id, recorded_reads)  # live once the receipt exists
    if catalog:
        await _reserve_catalog(store, transaction_id, intent_digest, catalog)
    await write_json_atomic(receipt_path(store, transaction_id), receipt)
    return receipt


async def prepare_read_collection(store: Any, *, transaction_id: str, intent_digest: str, participant: str,
                                  collection_id: str, root: str, count: int, catalog: str = "",
                                  reads: Any = None) -> dict[str, Any]:
    """Fence every leaf of one sealed collection, then write the bounded receipt; idempotent replay.

    The caller holds every leaf Card's mutation section and passes the reads it
    resolved from the same collection (``resolve_collection``). The collection
    must still be exactly the referenced one: its header's root, count and
    catalog. Each leaf is verified at its revision and fenced with this
    transaction AND collection; the receipt, written last, names only the ref.
    """
    from .card_read_collection import collection_root, load_header
    _checked_id(transaction_id)
    if not _HEX64.fullmatch(str(intent_digest or "")) or not str(participant or "").strip():
        raise CardTransactionRefused("card_transaction_intent_invalid")
    try:
        header = await load_header(store, collection_id)
    except CardStorageError as exc:
        raise CardTransactionRefused(str(exc)) from exc
    reads = list(reads or ())
    if (header is None or header["root"] != root or header["count"] != count or header["catalog"] != catalog
            or len(reads) != count or collection_root(reads, catalog) != root):
        raise CardTransactionRefused("card_read_collection_moved")
    receipt = {"schema": READ_COLLECTION_RECEIPT_SCHEMA, "transaction_id": transaction_id,
               "intent_digest": intent_digest, "participant": participant.strip(), "state": "prepared",
               "reason": "", "collection_id": collection_id, "root": root, "count": count, "catalog": catalog or ""}
    existing = await read_receipt(store, transaction_id)
    if existing is not None:
        if not is_read_collection_receipt(existing) or any(existing[name] != receipt[name] for name in (
                "intent_digest", "participant", "collection_id", "root", "count", "catalog")):
            raise CardTransactionRefused("card_transaction_replay_changed")
        if existing["state"] in DECISIONS:
            raise CardTransactionRefused("card_transaction_aborted" if existing["state"] == "aborted"
                                         else "card_transaction_late_stage")
        return existing
    if await read_json_or_none(tombstone_path(store, transaction_id)) is not None:
        raise CardTransactionRefused("card_transaction_aborted")
    port = getattr(store, "_card_transaction_decisions", None)
    if port is not None:
        try:
            recorded = await port.decision({"transaction_id": transaction_id, "intent_digest": intent_digest})
        except Exception:  # noqa: BLE001 - an unknown decision does not block a first prepare
            recorded = "undecided"
        if recorded in DECISIONS:
            raise CardTransactionRefused("card_transaction_late_stage")
    await write_json_atomic(active_path(store, transaction_id), _active_entry(transaction_id, collection_id))
    if reads:
        await _reserve_reads(store, transaction_id, reads, collection_id=collection_id)  # live once the receipt exists
    if catalog:
        await _reserve_catalog(store, transaction_id, intent_digest, catalog)
    _validate_read_collection(receipt, transaction_id)
    await write_json_atomic(receipt_path(store, transaction_id), receipt)
    return receipt


async def finish_read_set(store: Any, *, transaction_id: str, intent_digest: str, decision: str,
                          reason: str = "") -> dict[str, Any]:
    """Release a read set's fences after the coordinator's recorded decision; idempotent; writes no Card."""
    if decision not in DECISIONS:
        raise CardTransactionRefused("card_transaction_decision_invalid")
    receipt = await read_receipt(store, transaction_id)
    if not is_read_set_receipt(receipt) and not is_read_collection_receipt(receipt):
        raise CardTransactionRefused("card_transaction_unknown")
    if receipt["intent_digest"] != intent_digest:
        raise CardTransactionRefused("card_transaction_intent_mismatch")
    if receipt["state"] in DECISIONS:
        if receipt["state"] != decision:
            raise CardTransactionRefused("card_transaction_decision_conflict")
        await _release_read_set(store, receipt)
        return receipt
    port = getattr(store, "_card_transaction_decisions", None)
    if port is None:
        raise CardTransactionRefused("card_transaction_decision_unverified")
    try:
        recorded = await port.decision({"transaction_id": transaction_id, "intent_digest": intent_digest})
    except Exception as exc:  # noqa: BLE001
        raise CardTransactionRefused("card_transaction_decision_unverified") from exc
    if recorded != decision:
        raise CardTransactionRefused("card_transaction_decision_not_recorded")
    await _require_collection_before_decision(store, receipt)
    decided = {**receipt, "state": decision, "reason": str(reason or "")[:128]}
    _validate(decided, transaction_id)
    await write_json_atomic(receipt_path(store, transaction_id), decided)
    await _release_read_set(store, decided)
    return decided


async def _release_read_set(store: Any, receipt: Mapping[str, Any]) -> None:
    if is_read_collection_receipt(receipt):
        # The same sealed collection names every fence (a deleted one only for an already decided receipt).
        await _release_reads(store, receipt, await _collection_reads(store, receipt))
    else:
        await _release_reads(store, receipt)
    if receipt.get("catalog"):
        await _release_catalog(store, receipt["transaction_id"], receipt["intent_digest"])
    try:
        active_path(store, receipt["transaction_id"]).unlink(missing_ok=True)
    except OSError:
        pass  # a stale entry lists a decided read set, which recovery re-finishes idempotently


async def prepare_effects(store: Any, *, transaction_id: str, intent_digest: str, participant: str,
                          subject_hash: str, effects: Any) -> dict[str, Any]:
    """W578: the receipt of an effects-only transaction; idempotent replay; writes no Card.

    The caller holds the section of every account the effects delete and has
    checked this transaction holds each account's fence. The receipt is the
    record the effect applier binds to; STAGE's preparation of each effect (the
    incarnation hold) runs after it, and a recorded ABORT releases it.
    """
    _checked_id(transaction_id)
    if (not _HEX64.fullmatch(str(intent_digest or "")) or not str(participant or "").strip()
            or not _HEX64.fullmatch(str(subject_hash or ""))):
        raise CardTransactionRefused("card_transaction_intent_invalid")
    recorded = [{"kind": e["kind"], "key": e["key"], "payload": dict(e["payload"])} for e in (effects or ())]
    if not _effects_valid(recorded):
        raise CardTransactionRefused("card_transaction_effects_invalid")
    receipt = {"schema": EFFECTS_RECEIPT_SCHEMA, "transaction_id": transaction_id, "intent_digest": intent_digest,
               "participant": participant.strip(), "state": "prepared", "reason": "", "subject_hash": subject_hash,
               "effects": recorded}
    existing = await read_receipt(store, transaction_id)
    if existing is not None:
        if not is_effects_receipt(existing) or any(
                existing[name] != receipt[name] for name in ("intent_digest", "participant", "subject_hash",
                                                             "effects")):
            raise CardTransactionRefused("card_transaction_replay_changed")
        if existing["state"] in DECISIONS:
            raise CardTransactionRefused("card_transaction_aborted" if existing["state"] == "aborted"
                                         else "card_transaction_late_stage")
        return existing
    if await read_json_or_none(tombstone_path(store, transaction_id)) is not None:
        raise CardTransactionRefused("card_transaction_aborted")
    port = getattr(store, "_card_transaction_decisions", None)
    if port is not None:
        try:
            decided = await port.decision({"transaction_id": transaction_id, "intent_digest": intent_digest})
        except Exception:  # noqa: BLE001 - an unknown decision does not block a first prepare
            decided = "undecided"
        if decided in DECISIONS:
            raise CardTransactionRefused("card_transaction_late_stage")
    await write_json_atomic(active_path(store, transaction_id), _active_entry(transaction_id, ""))
    await write_json_atomic(receipt_path(store, transaction_id), receipt)
    return receipt


async def decide_effects(store: Any, *, transaction_id: str, intent_digest: str, decision: str,
                         reason: str = "") -> dict[str, Any]:
    """Record the coordinator's decision on an effects-only receipt; idempotent; applies nothing itself."""
    if decision not in DECISIONS:
        raise CardTransactionRefused("card_transaction_decision_invalid")
    receipt = await read_receipt(store, transaction_id)
    if not is_effects_receipt(receipt):
        raise CardTransactionRefused("card_transaction_unknown")
    if receipt["intent_digest"] != intent_digest:
        raise CardTransactionRefused("card_transaction_intent_mismatch")
    if receipt["state"] in DECISIONS:
        if receipt["state"] != decision:
            raise CardTransactionRefused("card_transaction_decision_conflict")
        return receipt
    port = getattr(store, "_card_transaction_decisions", None)
    if port is None:
        raise CardTransactionRefused("card_transaction_decision_unverified")
    try:
        recorded = await port.decision({"transaction_id": transaction_id, "intent_digest": intent_digest})
    except Exception as exc:  # noqa: BLE001
        raise CardTransactionRefused("card_transaction_decision_unverified") from exc
    if recorded != decision:
        raise CardTransactionRefused("card_transaction_decision_not_recorded")
    decided = {**receipt, "state": decision, "reason": str(reason or "")[:128]}
    _validate_effects(decided, transaction_id)
    await write_json_atomic(receipt_path(store, transaction_id), decided)
    return decided


def retire_effects(store: Any, transaction_id: str) -> None:
    """The effects-only transaction is finished: its in-doubt entry goes (a stale one is re-finished)."""
    try:
        active_path(store, transaction_id).unlink(missing_ok=True)
    except OSError:
        pass


async def state(store: Any, *, transaction_id: str) -> dict[str, Any] | None:
    """The recorded state; recovery reads this and may only materialize it, never change it."""

    return await read_receipt(store, transaction_id)


__all__ = ["finalize_current_version", "CARD_VERSION_MARKER_SCHEMA", "card_version_marker_path", "card_version_publish", "card_version_revision_committed",
           "card_version_rollback", "card_version_stage", "card_version_txn_id", "read_card_version_marker",
           "CardTransactionRefused", "DECISIONS", "GROUP_RECEIPT_SCHEMA", "TRANSACTION_POINTER_SCHEMA",
           "TRANSACTION_RECEIPT_SCHEMA", "begin_group", "complete_group", "finish_group", "group_member_ref",
           "is_group_receipt", "member_transaction_id", "READ_SET_RECEIPT_SCHEMA", "finish_read_set",
           "is_read_set_receipt", "prepare_read_set", "EFFECTS_RECEIPT_SCHEMA", "decide_effects",
           "is_effects_receipt", "prepare_effects", "retire_effects",
           "TransactionDecisionPort", "abort_unstaged", "active_path", "protected_collections", "read_fence_path", "apply_effects", "assert_replaceable", "bind_transaction_decisions", "decide",
           "effect_outcomes", "effects_path", "list_in_doubt", "marker_path", "pending_effects",
           "read_receipt", "resolve_pointer", "stage", "state"]


# ---------------------------------------------------------------------------------------------------
# W661 contract v6: STAGE / PUBLISH / ROLLBACK of Card versions, PB to Hub (the reduced core).
#
# Operator, 10 Oct: "each card has it data ONCE. others LINK"; "no any memeory. we are distributed
# system"; "never nothing is being scanned"; "that error must call callbacl also which will rollback the
# garbage"; D2: "not yet final version does not work until theres final arrives. before that olf
# version works and is active."
#
# Contract v6.2 (EMain, 16:3xZ): ROLLBACK deletes the not-yet-final versions AND the txn marker, so nothing
# of a failed save remains; an absent marker is answered unknown_txn and nothing is written.
#
# Every function below runs while the caller (DelegatedCardService) holds the mutation lock of EVERY
# member Card, taken in sorted order: a flock that never expires or is stolen, so two operations on one
# Card never interleave and a delayed call cannot act after a later one. current.json is the one truth;
# a staged version is a revision file that no current.json names and whose `.card-version.json` marker
# keeps it out of history until its txn is published. The txn marker holds links only: card ids,
# versions, revision names, content hashes and the request digest; never a Card field.
# ---------------------------------------------------------------------------------------------------

CARD_VERSION_MARKER_SCHEMA = "connection_hub.card-version-txn.v1"
CARD_VERSION_STATES = ("staging", "staged", "published", "rolled_back")
# EMain 16:35Z: v6.2 leaves NO marker after ROLLBACK; this one-line switch keeps a terminal `rolled_back`
# marker instead, until Infra's K4 probe decides whether a late call can exist.
KEEP_ROLLBACK_MARKER = False
_TXN = re.compile(r"[a-z0-9][a-z0-9-]{30,126}[a-z0-9]")


def card_version_txn_id(txn: Any) -> str:
    if type(txn) is not str or not _TXN.fullmatch(txn):
        raise CardTransactionRefused("card_version_txn_invalid")
    return txn


def card_version_marker_path(store: Any, txn: str):
    return store.root / "card-versions" / f"{card_version_txn_id(txn)}.json"


def card_version_revision_marker_path(store: Any, *, subject_hash: str, access_id: str, revision_name: str):
    """Beside a staged revision: names its txn, so the revision is committed only once that txn is."""
    return store.revision_path(subject_hash=subject_hash, access_id=access_id,
                               revision_name=revision_name).with_suffix(".card-version.json")


async def read_card_version_marker(store: Any, txn: str) -> dict[str, Any] | None:
    raw = await read_json_or_none(card_version_marker_path(store, txn))
    if raw is None:
        return None
    if (not isinstance(raw, Mapping) or raw.get("schema") != CARD_VERSION_MARKER_SCHEMA
            or raw.get("txn") != txn or raw.get("state") not in CARD_VERSION_STATES
            or not isinstance(raw.get("members"), list)):
        raise CardStorageError("card_version_marker_invalid")
    return dict(raw)


async def _write_card_version_marker(store: Any, marker: Mapping[str, Any]) -> None:
    await write_json_atomic(card_version_marker_path(store, marker["txn"]), dict(marker))


async def card_version_revision_committed(store: Any, marker: Any, *, subject_hash: str, access_id: str,
                                          revision_name: str) -> bool:
    """A staged revision is history only once its txn is published (never while staged or rolled back)."""
    if not isinstance(marker, Mapping) or set(marker) != {"txn"}:
        raise CardStorageError("card_version_revision_binding_invalid")
    txn = await read_card_version_marker(store, marker["txn"])
    if txn is None:
        # D2 (EMain 17:1xZ): PUBLISH deletes the marker once done, and ROLLBACK deletes a txn's files BEFORE its
        # marker, so a version file whose txn marker is gone is published history.
        return True
    if txn["state"] == "staged":
        # Infra 17:2xZ red 3: current.json naming this version is the truth (D3): a PUBLISH stopped between its
        # pointer write and its marker write has published it, so readers must not see a missing revision.
        current = await store.read_current(subject_hash=subject_hash, access_id=access_id)
        return current is not None and current.revision_name == revision_name and any(
            (m["subject_hash"], m["access_id"], m["revision_name"]) == (subject_hash, access_id, revision_name)
            for m in txn["members"])
    if txn["state"] != "published":
        return False
    return any((m["subject_hash"], m["access_id"], m["revision_name"]) == (subject_hash, access_id, revision_name)
               for m in txn["members"])


async def _mark_published(store: Any, marker: dict[str, Any]) -> None:
    """Every member's current.json names this txn's version: the txn IS published. Record it, drop the sidecars."""
    marker["state"] = "published"
    await _write_card_version_marker(store, marker)
    for m in marker["members"]:
        path = card_version_revision_marker_path(store, subject_hash=m["subject_hash"], access_id=m["access_id"],
                                                 revision_name=m["revision_name"])
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass  # it still resolves as committed: published marker, or no marker at all


async def _finish_published(store: Any, marker: dict[str, Any], run_effect: Any) -> None:
    """Each unrecorded effect once; once every effect is recorded the marker goes (D2: only the version stays)."""
    await _run_effects(store, marker, run_effect)
    try:
        card_version_marker_path(store, marker["txn"]).unlink(missing_ok=True)
    except OSError as exc:
        raise CardStorageError("card_version_marker_cleanup_failed") from exc
    from ..durable_io import _fsync_directory
    _fsync_directory(card_version_marker_path(store, marker["txn"]).parent)


async def _current_txn(store: Any, current: Any, *, subject_hash: str, access_id: str) -> str:
    """The card-version txn that wrote the current version: its record, else its sidecar; "" for older writers."""
    record = await store.read_version_record(subject_hash=subject_hash, access_id=access_id,
                                             revision_name=current.revision_name) \
        if hasattr(store, "read_version_record") else None
    if record and type(record.get("txn")) is str:
        return record["txn"]
    sidecar = await read_json_or_none(card_version_revision_marker_path(
        store, subject_hash=subject_hash, access_id=access_id, revision_name=current.revision_name))
    return sidecar["txn"] if isinstance(sidecar, Mapping) and type(sidecar.get("txn")) is str else ""


async def finalize_current_version(store: Any, *, subject_hash: str, access_id: str, own_txn: str = "") -> str:
    """D3 (EMain, Infra's K2 cut): before building on a Card, finalize the txn its current.json names.

    current.json naming a version means that txn IS published, even if its PUBLISH stopped before the
    `published` marker. Every writer (STAGE, PUBLISH, any pointer writer) records that here, so that txn's
    later ROLLBACK answers already_published and never deletes a version a successor built on. One direct
    read of current.json, the version's record and the txn marker; nothing is listed. A group whose other
    members do not name their versions (a partial group, phase 2) is refused card_version_unresolved, never
    guessed. The finalized marker is left `published` for its own txn to clean up. Returns the txn current
    names ("" if none).
    """
    current = await store.read_current(subject_hash=subject_hash, access_id=access_id)
    if current is None:
        return ""
    txn = await _current_txn(store, current, subject_hash=subject_hash, access_id=access_id)
    if not txn or txn == own_txn:
        return txn
    marker = await read_card_version_marker(store, txn)
    if marker is None or marker["state"] not in ("staged", "published"):
        return txn
    if marker["state"] == "staged":
        for m in marker["members"]:
            named = await store.read_current(subject_hash=m["subject_hash"], access_id=m["access_id"])
            if named is None or named.revision_name != m["revision_name"]:
                raise CardStorageError("card_version_unresolved")
        await _mark_published(store, marker)
    if any(str(index) not in marker["effect_outcomes"] for index in range(len(marker["effects"]))):
        # Infra 17:37Z, EMain 17:43Z: never run another txn's effects, always refuse before admitting the next
        # base. Only that txn's own PUBLISH retry or ROLLBACK runs them (a later operation finishing earlier work
        # is open with the operator, W693 Q4).
        raise CardStorageError("card_version_effects_pending")
    # The marker stays `published`: only the txn's own PUBLISH retry or ROLLBACK (which PB's error path always
    # sends) deletes it, so that ROLLBACK answers already_published even without its links (Infra red 2).
    return txn


def _member_link(subject_hash: str, access_id: str, base: int | None, pointer: CardCurrentPointer,
                 observed: CardCurrentPointer | None) -> dict[str, Any]:
    return {"subject_hash": subject_hash, "access_id": access_id, "base_version": base,
            "observed_version": observed.card_revision if observed is not None else None,
            "observed_revision_name": observed.revision_name if observed is not None else None,
            "version": pointer.card_revision, "revision_name": pointer.revision_name,
            "content_hash": pointer.content_hash, "pointer": pointer.to_dict()}


def _answer(marker: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [{"subject_hash": m["subject_hash"], "access_id": m["access_id"], "version": m["version"],
             "checksum": m["content_hash"]} for m in marker["members"]]


def _card_version_binding(binding: Any) -> dict[str, str] | None:
    """The authenticated scope and caller that staged a txn (Infra W691 finding 2); None for internal callers."""
    if binding is None:
        return None
    if (not isinstance(binding, Mapping) or set(binding) != {"scope", "caller"}
            or not all(type(value) is str and value for value in binding.values())):
        raise CardTransactionRefused("card_version_request_invalid")
    return dict(binding)


def _require_binding(marker: Mapping[str, Any], binding: Any) -> None:
    """Every replay, PUBLISH and ROLLBACK names the binding its STAGE recorded, else it touches nothing."""
    if marker.get("binding") != _card_version_binding(binding):
        raise CardTransactionRefused("txn_scope_mismatch")


async def card_version_stage(store: Any, *, txn: str, request_digest: str, catalog: Any,
                             members: Any, effects: Any = (), now: datetime, request_id: str = "",
                             actor: Any = None, prepare: Any = None, binding: Any = None,
                             active_catalog: Any = None) -> list[dict[str, Any]]:
    """STAGE: write each member's next version (not yet final) and the txn marker `staged`.

    ``members`` are ``(subject_hash, access_id, base_version, candidate)``: ``base_version`` is the
    version PB authorized against, ``None`` for an invitation upsert ("UPSERT. overwrite"), which takes
    whatever exists on the stable id. ``candidate`` is the new Card value (piece 2 builds it). ``now`` is
    the request's time: the revision name derives from it, so a retry after a crash between the version
    write and the marker write rewrites the SAME file instead of leaving a stray second version (EMain,
    16:35Z: "the old resume path that writes again goes"). Order (Infra K4, 16:45Z): base checks, the
    marker `staging` naming every file, ``prepare()`` (piece 2's effect preparation), the files, the marker
    `staged`. A refusal of ``prepare()`` writes no Card file; ROLLBACK clears the `staging` marker.
    ``catalog`` is PB's catalog link and ``actor`` = {subject, kind}: who and when, links only.
    """
    card_version_txn_id(txn)
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise CardTransactionRefused("card_version_request_invalid")
    if type(request_digest) is not str or not _HEX64.fullmatch(request_digest) or type(request_id) is not str:
        raise CardTransactionRefused("card_version_request_invalid")
    if isinstance(catalog, Mapping):
        catalog = dict(catalog)
        if not all(type(key) is str and type(value) is str for key, value in catalog.items()):
            raise CardTransactionRefused("card_version_request_invalid")
    elif type(catalog) is not str:
        raise CardTransactionRefused("card_version_request_invalid")
    actor = dict(actor) if isinstance(actor, Mapping) else None
    if actor is not None and (set(actor) != {"subject", "kind"} or not all(type(v) is str for v in actor.values())):
        raise CardTransactionRefused("card_version_request_invalid")
    binding = _card_version_binding(binding)
    members = list(members)
    if not 1 <= len(members) <= MAX_GROUP_MEMBERS or len({(m[0], m[1]) for m in members}) != len(members):
        raise CardTransactionRefused("card_version_members_invalid")
    existing = await read_card_version_marker(store, txn)
    if existing is not None:
        _require_binding(existing, binding)
        if existing["state"] not in ("staging", "staged"):
            raise CardTransactionRefused("txn_closed")
        if existing["request_digest"] != request_digest or existing.get("at") != now.isoformat():
            raise CardTransactionRefused("stage_txn_conflict")
        if existing["state"] == "staged":
            for m in existing["members"]:  # a same-txn retry: the same answer, once every file is still there
                if await read_json_or_none(store.revision_path(subject_hash=m["subject_hash"], access_id=m["access_id"],
                                                               revision_name=m["revision_name"])) is None:
                    raise CardStorageError("card_version_staged_revision_missing")
            return _answer(existing)
        # `staging`: an earlier attempt stopped mid-write. Its file names are recorded and deterministic, so
        # the retry finishes the SAME files (it never adds a second version).
        by_card = {(m[0], m[1]): m[3] for m in members}
        if set(by_card) != {(m["subject_hash"], m["access_id"]) for m in existing["members"]}:
            raise CardTransactionRefused("stage_txn_conflict")
        if prepare is not None:
            # Infra 17:2xZ red 1: a `staging` marker is no evidence that preparation succeeded; the retry
            # completes it (piece 2's preparation is idempotent per txn) before any file or `staged`.
            await cancellation_safe_await(prepare())
        await _write_staged_versions(store, existing, by_card, now)
        existing["state"] = "staged"
        await _write_card_version_marker(store, existing)
        return _answer(existing)
    if active_catalog is not None:
        # D1 (EMain): the ACTIVE catalog, read directly under the Card locks, must be PB's catalog.
        active = await active_catalog()
        if not isinstance(catalog, Mapping) or active is None or dict(active) != {
                "version": catalog.get("version"), "content_hash": catalog.get("content_hash")}:
            raise CardTransactionRefused("stage_catalog_moved")
    checked = []
    for subject_hash, access_id, base_version, candidate in members:
        if not isinstance(candidate, CardAuthority) or candidate.access_id != access_id:
            raise CardTransactionRefused("card_version_candidate_invalid")
        if await finalize_current_version(store, subject_hash=subject_hash, access_id=access_id) == txn:
            raise CardTransactionRefused("txn_closed")  # this txn already published (its marker is gone, D2)
        current = await store.read_current(subject_hash=subject_hash, access_id=access_id)
        if base_version is None:
            expected = (current.card_revision if current is not None else 0) + 1
        else:
            if type(base_version) is not int or base_version < 1:
                raise CardTransactionRefused("card_version_base_invalid")
            if current is None or current.card_revision != base_version:
                raise CardTransactionRefused("card_changed")
            expected = base_version + 1
        if candidate.card_revision != expected:
            # An upsert built on a revision that has since moved: the Card changed (Spark App, 17:1xZ).
            raise CardTransactionRefused("card_changed" if base_version is None else "card_version_candidate_invalid")
        content_hash = candidate.content_hash()
        pointer = CardCurrentPointer.for_revision(candidate, content_hash=content_hash, updated_at=now,
                                                  revision_name=card_revision_name(card_revision=candidate.card_revision,
                                                                                   content_hash=content_hash, updated_at=now,
                                                                                   txn=txn))
        checked.append(_member_link(subject_hash, access_id, base_version, pointer, current))
    # Infra K4 (16:45Z): the marker FIRST, as `staging`, naming every file this STAGE may write, so a
    # cancellation or failure at ANY later point leaves files that ROLLBACK can locate ("that error must call
    # callbacl also which will rollback the garbage").
    marker = {"schema": CARD_VERSION_MARKER_SCHEMA, "txn": txn, "state": "staging", "request_digest": request_digest,
              "request_id": request_id, "actor": actor, "binding": binding, "at": now.isoformat(), "catalog": catalog,
              "members": checked,
              "effects": [dict(effect) for effect in effects], "effect_outcomes": {}}
    await _write_card_version_marker(store, marker)
    if prepare is not None:
        # A refusal here leaves only the `staging` marker: PB's error path calls ROLLBACK (contract K2), which
        # releases every effect (also a partly prepared one) and removes the marker. No Card file is written.
        await cancellation_safe_await(prepare())  # a started preparation finishes before the locks go (K4)
    await _write_staged_versions(store, marker, {(m[0], m[1]): m[3] for m in members}, now)
    marker["state"] = "staged"
    await _write_card_version_marker(store, marker)
    return _answer(marker)


async def _write_staged_versions(store: Any, marker: Mapping[str, Any], candidates: Mapping[tuple[str, str], Any],
                                 now: datetime) -> None:
    for m in marker["members"]:
        candidate = candidates[(m["subject_hash"], m["access_id"])]
        if not isinstance(candidate, CardAuthority) or candidate.content_hash() != m["content_hash"]:
            raise CardTransactionRefused("stage_txn_conflict")
        # The revision marker before the file: the file is never readable as history before its txn publishes.
        await write_json_atomic(card_version_revision_marker_path(store, subject_hash=m["subject_hash"],
                                                                  access_id=m["access_id"],
                                                                  revision_name=m["revision_name"]), {"txn": marker["txn"]})
        record = {"txn": marker["txn"], "actor": marker.get("actor"), "at": marker["at"],
                  "catalog": marker.get("catalog"), "binding": marker.get("binding")}
        # D2: who and when are fields of the version record ("each card version -> one record").
        pointer = await store.write_revision(subject_hash=m["subject_hash"], authority=candidate, updated_at=now,
                                             record=record, txn=marker["txn"])
        if pointer.revision_name != m["revision_name"]:
            raise CardStorageError("card_version_revision_mismatch")


async def _release_and_forget(store: Any, marker: Mapping[str, Any], *, release: Any) -> None:
    """Delete every file the txn may have written (missing ones are fine), then the marker (or close it)."""
    if release is not None:  # piece 2: a prepared effect (an agent row's re-wrap) is discarded first
        for effect in marker["effects"]:
            await cancellation_safe_await(release(dict(effect), dict(marker)))
    from ..durable_io import _fsync_directory
    folders = set()
    for m in marker["members"]:
        revision = store.revision_path(subject_hash=m["subject_hash"], access_id=m["access_id"],
                                       revision_name=m["revision_name"])
        for path in (revision, revision.with_suffix(".card-version.json")):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                raise CardStorageError("card_version_rollback_failed") from exc
        folders.add(revision.parent)
    for folder in sorted(folders):  # Spark App F3: the deletions are durable before the marker goes
        _fsync_directory(folder)
    if KEEP_ROLLBACK_MARKER:
        await _write_card_version_marker(store, {**marker, "state": "rolled_back"})
        return
    try:
        card_version_marker_path(store, marker["txn"]).unlink(missing_ok=True)
    except OSError as exc:
        raise CardStorageError("card_version_rollback_failed") from exc
    _fsync_directory(card_version_marker_path(store, marker["txn"]).parent)


async def _run_effects(store: Any, marker: dict[str, Any], run_effect: Any) -> None:
    """Each recorded effect once, in order; its named outcome recorded in the marker before the next.

    ``run_effect(effect, marker)`` is piece 2's executor (idempotent by (txn, kind, key), W580); it returns
    the effect's named outcome. A raise leaves the marker `published` with that effect unrecorded.
    """
    for index, effect in enumerate(marker["effects"]):
        key = str(index)
        if key in marker["effect_outcomes"]:
            continue
        if run_effect is None:
            raise CardTransactionRefused("effects_pending")
        outcome = await cancellation_safe_await(run_effect(dict(effect), dict(marker)))
        if type(outcome) is not str or not outcome:
            raise CardTransactionRefused("effects_pending")
        marker["effect_outcomes"][key] = outcome
        await _write_card_version_marker(store, marker)


async def card_version_publish(store: Any, *, txn: str, run_effect: Any = None,
                               binding: Any = None) -> list[dict[str, Any]]:
    """PUBLISH: finalize what current names, the base fence, each member's current.json, the marker `published`,
    then each effect once; once every effect is recorded the marker is deleted (D2)."""
    marker = await read_card_version_marker(store, txn)
    if marker is None:
        raise CardTransactionRefused("txn_unknown")  # D5: before any binding check
    _require_binding(marker, binding)
    if marker["state"] == "rolled_back":
        raise CardTransactionRefused("txn_closed")
    if marker["state"] == "staging":
        raise CardTransactionRefused("txn_not_staged")  # STAGE never answered; only ROLLBACK or its retry apply
    if marker["state"] == "staged":
        for m in marker["members"]:
            await finalize_current_version(store, subject_hash=m["subject_hash"], access_id=m["access_id"],
                                           own_txn=txn)
            current = await store.read_current(subject_hash=m["subject_hash"], access_id=m["access_id"])
            observed = current.revision_name if current is not None else None
            if observed == m["revision_name"]:
                continue  # this txn's own earlier write (a PUBLISH that stopped before its marker)
            if observed != m["observed_revision_name"]:
                raise CardTransactionRefused("card_changed")  # the lost-update fence
        for m in marker["members"]:
            current = await store.read_current(subject_hash=m["subject_hash"], access_id=m["access_id"])
            if current is not None and current.revision_name == m["revision_name"]:
                continue  # written by this txn's earlier PUBLISH
            await store.advance_current(subject_hash=m["subject_hash"],
                                        pointer=CardCurrentPointer.from_mapping(m["pointer"]))
        await _mark_published(store, marker)
    await _finish_published(store, marker, run_effect)
    return _answer(marker)


async def card_version_rollback(store: Any, *, txn: str, run_effect: Any = None, release: Any = None,
                                binding: Any = None, links: Any = (), at: Any = None) -> str:
    """ROLLBACK: remove the not-yet-final versions; never a published one. Also the probe after a lost reply.

    ``links`` are STAGE's answer {subject_hash, access_id, version, checksum} and ``at`` the request's time:
    with the marker gone (D2), the version file name is a pure function of them
    (card_revision_<utc_stamp(at)>_<version:08d>_<checksum[:12]>_<sha256(txn)[:12]>.json), read directly,
    never listed.
    """
    marker = await read_card_version_marker(store, txn)
    if marker is None:
        if await _published_by_links(store, txn=txn, links=links, at=at, binding=binding):
            return "already_published"
        if KEEP_ROLLBACK_MARKER:
            await _write_card_version_marker(store, {"schema": CARD_VERSION_MARKER_SCHEMA, "txn": txn,
                                                     "state": "rolled_back", "request_digest": "", "catalog": "",
                                                     "members": [], "effects": [], "effect_outcomes": {}})
        return "unknown_txn"  # contract v6.2: nothing of a failed save remains, so nothing is written
    _require_binding(marker, binding)
    if marker["state"] == "rolled_back":
        return "rolled_back"
    if marker["state"] == "staged":
        for m in marker["members"]:
            current = await store.read_current(subject_hash=m["subject_hash"], access_id=m["access_id"])
            if current is not None and current.revision_name == m["revision_name"]:
                # A PUBLISH stopped between its pointer write and its marker write: it IS published.
                await card_version_publish(store, txn=txn, run_effect=run_effect, binding=binding)
                return "already_published"
    if marker["state"] in ("staging", "staged"):
        await _release_and_forget(store, marker, release=release)
        return "rolled_back"
    await _finish_published(store, marker, run_effect)  # published: finish what the lost PUBLISH left
    return "already_published"


async def _published_by_links(store: Any, *, txn: str, links: Any, at: Any, binding: Any) -> bool:
    """The marker is gone: is each linked version file there and written by this txn? (D2, one read per link)"""
    links = list(links or ())
    if not links or not isinstance(at, datetime) or at.utcoffset() is None:
        return False
    found = []
    for link in links:
        try:
            name = card_revision_name(card_revision=int(link["version"]), content_hash=str(link["checksum"]),
                                      updated_at=at, txn=txn)
            payload = await read_json_or_none(store.revision_path(subject_hash=link["subject_hash"],
                                                                  access_id=link["access_id"], revision_name=name))
            version, checksum = int(link["version"]), str(link["checksum"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CardTransactionRefused("card_version_request_invalid") from exc
        record = payload.get(VERSION_RECORD_KEY) if isinstance(payload, dict) else None
        found.append(isinstance(record, dict) and record.get("txn") == txn)
        if not found[-1]:
            continue
        if record.get("binding") != _card_version_binding(binding):
            raise CardTransactionRefused("txn_scope_mismatch")
        # Infra 17:52Z: the name carries only checksum[:12]; the link is exact only if the Card in that same file
        # has the full checksum and version. A different link is never confirmed: fail closed.
        card = {key: value for key, value in payload.items() if key != VERSION_RECORD_KEY}
        if card_authority_payload_hash(card) != checksum or card.get("card_revision") != version:
            raise CardTransactionRefused("card_version_link_mismatch")
    return all(found)

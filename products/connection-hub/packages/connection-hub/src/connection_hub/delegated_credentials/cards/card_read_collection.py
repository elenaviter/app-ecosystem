"""W502 lane D: a Hub-owned sealed collection of Card read dependencies, held by reference.

A project-wide step (the zero cutover, a migration) depends on every person's
My Card, Control Card, authoritative absences and the distinct chain Cards. The
W578 read set carries that list in the initiator's decision intent, so the
intent grows with the project. Here the Hub keeps the list once, as its OWN
sealed collection, and the intent holds only a bounded reference:

- **registration** (``CardReadCollectionOperation``, authenticated exactly like
  ``card_census_read``): the initiator names the scope and the persons from its
  own fenced membership; the Hub derives each person's descriptors from its own
  Card store (the census identity path) and seals them. Completeness of the
  person list stays the initiator's, as for the census.
- **storage**: ``card-collections/<collection_id>/`` holds one leaf per
  descriptor (``leaves/<sha256(subject_hash:access_id)>.json``) and, written
  LAST, ``header.json``: scope, actor, request, deadline, count, root and
  catalog. A collection without its header is not sealed and resolves to
  nothing.
- **root** is ``sha256(canonical {schema: card-read-set.v1, catalog, reads})``
  over the descriptors sorted by (subject_hash, access_id): byte-identical to
  the W578 read-set candidate digest, so an initiator computes it from its own
  census and binds by equality.

A descriptor is ``{subject_hash, access_id, revision}``: revision 0 is an
authoritative absence, >= 1 a present Card at that revision. Nothing here
fences or prepares; ``transaction_store`` does that from a resolved collection.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from service_foundation.coordination.durable_wire import canonical_json_bytes, sha256_hex

from ..durable_io import list_child_names, read_json_or_none, write_json_atomic
from .store import CardStorageError

COLLECTION_HEADER_SCHEMA = "connection-hub.card-read-collection-header.v1"
COLLECTION_LEAF_SCHEMA = "connection-hub.card-read-collection-leaf.v1"
ROOT_SCHEMA = "connection-hub.card-read-set.v1"  # the W578 read-set candidate schema: root == its digest
# Two descriptors per person plus distinct chain Cards, for the census's 500 persons (card_read_set's bound).
MAX_COLLECTION_READS = 2 * 500 + 24
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[0-9a-f]{32}\Z")
_HEADER_FIELDS = frozenset({"schema", "collection_id", "scope", "actor_subject", "request_id", "deadline",
                            "count", "root", "catalog"})


def collection_root(reads: Sequence[Mapping[str, Any]], catalog: str = "") -> str:
    """The root over sorted descriptors: the W578 read-set candidate digest of the same reads."""
    return sha256_hex(canonical_json_bytes({
        "schema": ROOT_SCHEMA, "catalog": catalog,
        "reads": sorted((dict(read) for read in reads), key=lambda read: (read["subject_hash"], read["access_id"]))}))


def descriptors_valid(reads: Any) -> bool:
    """Canonical descriptors: sorted, unique by (subject_hash, access_id), revision >= 0, bounded."""
    if type(reads) is not list or len(reads) > MAX_COLLECTION_READS:
        return False
    keys = []
    for read in reads:
        if (not isinstance(read, Mapping) or set(read) != {"subject_hash", "access_id", "revision"}
                or type(read["subject_hash"]) is not str or not _HEX64.fullmatch(read["subject_hash"])
                or type(read["access_id"]) is not str or not read["access_id"] or len(read["access_id"]) > 256
                or type(read["revision"]) is not int or read["revision"] < 0):
            return False
        keys.append((read["subject_hash"], read["access_id"]))
    return keys == sorted(set(keys)) and len(keys) == len(reads)


def _collection_dir(store: Any, collection_id: str):
    if type(collection_id) is not str or not _ID.fullmatch(collection_id):
        raise CardStorageError("card_read_collection_id_invalid")
    return store.root / "card-collections" / collection_id


def leaf_name(subject_hash: str, access_id: str) -> str:
    return sha256_hex(f"{subject_hash}:{access_id}".encode("utf-8"))


def leaf_path(store: Any, collection_id: str, *, subject_hash: str, access_id: str):
    return _collection_dir(store, collection_id) / "leaves" / f"{leaf_name(subject_hash, access_id)}.json"


def header_path(store: Any, collection_id: str):
    return _collection_dir(store, collection_id) / "header.json"


def validate_header(raw: Any, collection_id: str) -> dict[str, Any]:
    if (not isinstance(raw, Mapping) or set(raw) != _HEADER_FIELDS or raw["schema"] != COLLECTION_HEADER_SCHEMA
            or raw["collection_id"] != collection_id
            or any(type(raw[name]) is not str or not raw[name] for name in ("scope", "actor_subject", "request_id"))
            or type(raw["deadline"]) is not int or raw["deadline"] <= 0
            or type(raw["count"]) is not int or not 0 <= raw["count"] <= MAX_COLLECTION_READS
            or type(raw["root"]) is not str or not _HEX64.fullmatch(raw["root"])
            or type(raw["catalog"]) is not str or (raw["catalog"] and not _HEX64.fullmatch(raw["catalog"]))
            or (raw["count"] == 0 and not raw["catalog"])):
        raise CardStorageError("card_read_collection_header_invalid")
    return dict(raw)


async def seal_collection(store: Any, *, collection_id: str, scope: str, actor_subject: str, request_id: str,
                          deadline: int, reads: Sequence[Mapping[str, Any]], catalog: str = "") -> dict[str, Any]:
    """Write every leaf, then the header (the seal). Idempotent for identical content; refuses any other."""
    reads = sorted((dict(read) for read in reads), key=lambda read: (read.get("subject_hash"), read.get("access_id")))
    if not descriptors_valid(reads):
        raise CardStorageError("card_read_collection_reads_invalid")
    header = validate_header({"schema": COLLECTION_HEADER_SCHEMA, "collection_id": collection_id, "scope": scope,
                              "actor_subject": actor_subject, "request_id": request_id, "deadline": deadline,
                              "count": len(reads), "root": collection_root(reads, catalog), "catalog": catalog},
                             collection_id)
    existing = await read_json_or_none(header_path(store, collection_id))
    if existing is not None:
        if validate_header(existing, collection_id) != header:
            raise CardStorageError("card_read_collection_conflict")
        return header
    for read in reads:
        await write_json_atomic(leaf_path(store, collection_id, subject_hash=read["subject_hash"],
                                          access_id=read["access_id"]),
                                {"schema": COLLECTION_LEAF_SCHEMA, "collection_id": collection_id, **read})
    await write_json_atomic(header_path(store, collection_id), header)
    return header


async def load_header(store: Any, collection_id: str) -> dict[str, Any] | None:
    raw = await read_json_or_none(header_path(store, collection_id))
    return None if raw is None else validate_header(raw, collection_id)


async def leaf(store: Any, collection_id: str, *, subject_hash: str, access_id: str) -> dict[str, Any] | None:
    """One sealed leaf, or None when the collection names no such dependency."""
    raw = await read_json_or_none(leaf_path(store, collection_id, subject_hash=subject_hash, access_id=access_id))
    if raw is None:
        return None
    if (not isinstance(raw, Mapping) or set(raw) != {"schema", "collection_id", "subject_hash", "access_id", "revision"}
            or raw["schema"] != COLLECTION_LEAF_SCHEMA or raw["collection_id"] != collection_id
            or (raw["subject_hash"], raw["access_id"]) != (subject_hash, access_id)
            or type(raw["revision"]) is not int or raw["revision"] < 0):
        raise CardStorageError("card_read_collection_leaf_invalid")
    return dict(raw)


async def resolve_collection(store: Any, collection_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The sealed header and its exact descriptors; a missing, partial, extra or altered leaf refuses by name."""
    header = await load_header(store, collection_id)
    if header is None:
        raise CardStorageError("card_read_collection_unknown")
    names = await list_child_names(_collection_dir(store, collection_id) / "leaves")
    names = [name for name in names if name.endswith(".json")]
    if len(names) != header["count"]:
        raise CardStorageError("card_read_collection_incomplete")
    reads = []
    for name in sorted(names):
        raw = await read_json_or_none(_collection_dir(store, collection_id) / "leaves" / name)
        if not isinstance(raw, Mapping) or leaf_name(str(raw.get("subject_hash")), str(raw.get("access_id"))) + ".json" != name:
            raise CardStorageError("card_read_collection_leaf_invalid")
        checked = await leaf(store, collection_id, subject_hash=raw["subject_hash"], access_id=raw["access_id"])
        reads.append({key: checked[key] for key in ("subject_hash", "access_id", "revision")})
    reads.sort(key=lambda read: (read["subject_hash"], read["access_id"]))
    if not descriptors_valid(reads) or collection_root(reads, header["catalog"]) != header["root"]:
        raise CardStorageError("card_read_collection_root_mismatch")
    return header, reads


__all__ = ["COLLECTION_HEADER_SCHEMA", "COLLECTION_LEAF_SCHEMA", "MAX_COLLECTION_READS", "collection_root",
           "descriptors_valid", "header_path", "leaf", "leaf_path", "load_header", "resolve_collection",
           "seal_collection", "validate_header"]

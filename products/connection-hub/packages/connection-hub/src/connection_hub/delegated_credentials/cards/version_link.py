# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W661 scope B: a Card version held by LINK, never by a copy of its body.

Operator rule: "each card has it data ONCE. others LINK". A record that has to name a Card version
(the OAuth issuance plan, lane 4; the Hub's intent v2, lane 3) stores only

    {"card_revision": int, "revision_name": str, "content_hash": str}

and the record itself names the Card (subject hash and access id). ``load_version`` reads that one
version file directly by name (never by listing), strips its ``version_record``, and refuses unless
the full content hash, the access id and the revision match. ``store.read_revision`` stays the serving
read and keeps hiding staged files; this helper is not a generic visibility bypass.

A version that does not exist yet (a planned candidate) is written once as its own version file
(``write_hidden_version``), hidden behind a ``.card-transaction.json`` marker that names its tag. A
tag is either the staging tag of a plan (``staging_tag``: no receipt exists for it, so the file is
never history) or a real transaction id. ``adopt_hidden_version`` hands a staged file to its real
transaction by moving only its marker, never rewriting the file. ``load_version(..., marker=tag)``
requires that exact marker, so a candidate is only ever read as the one its record names.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import pathlib
from datetime import datetime
from typing import Any, Mapping

from connection_hub.delegated_credentials.durable_io import write_json_atomic
from .model import CardAuthority, CardCurrentPointer, CardRecordError, card_authority_payload_hash, card_revision_name
from .store import VERSION_RECORD_KEY

LINK_KEYS = frozenset({"card_revision", "revision_name", "content_hash"})
_HEX64 = frozenset("0123456789abcdef")


def is_version_link(value: Any) -> bool:
    """Whether ``value`` has the link's exact shape (a stored Card body never does)."""
    return (isinstance(value, Mapping) and set(value) == LINK_KEYS
            and type(value["card_revision"]) is int and value["card_revision"] >= 1
            and type(value["revision_name"]) is str and bool(value["revision_name"])
            and "/" not in value["revision_name"] and value["revision_name"].endswith(".json")
            and type(value["content_hash"]) is str and len(value["content_hash"]) == 64
            and set(value["content_hash"]) <= _HEX64)


def version_link(*, card_revision: int, revision_name: str, content_hash: str) -> dict[str, Any]:
    link = {"card_revision": card_revision, "revision_name": revision_name, "content_hash": content_hash}
    if not is_version_link(link):
        raise CardRecordError("version_link_invalid")
    return link


def pointer_link(pointer: CardCurrentPointer) -> dict[str, Any]:
    """The link of the version a ``current.json`` pointer names (a committed version)."""
    return version_link(card_revision=pointer.card_revision, revision_name=pointer.revision_name,
                        content_hash=pointer.content_hash)


STAGING_TAG_PREFIX = "stg-"


def staging_tag(scope: str, request_id: str) -> str:
    """The tag of one planned (pre-begin) version: ``stg-`` plus SHA-256 of a length-prefixed encoding.

    Structurally outside the transaction-id namespace (those are 64 lowercase hex, never ``stg-``), and
    unambiguous: each field is length-prefixed, so no (scope, request_id) pair encodes like another.
    """
    if type(scope) is not str or not scope or type(request_id) is not str or not request_id:
        raise CardRecordError("version_link_tag_invalid")
    encoded = b"card-version-staging-tag.v1"
    for field in (scope, request_id):
        raw = field.encode("utf-8")
        encoded += len(raw).to_bytes(8, "big") + raw
    return STAGING_TAG_PREFIX + hashlib.sha256(encoded).hexdigest()


def is_staging_tag(value: Any) -> bool:
    return type(value) is str and value.startswith(STAGING_TAG_PREFIX)


_ABSENT = object()


def _read_text_or_absent(path: Any) -> Any:
    try:
        return pathlib.Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return _ABSENT


async def _read_present(path: Any) -> Any:
    """The parsed object, or ``_ABSENT`` only when the file is confirmed absent, in ONE read (no window
    between "read" and "exists" for a concurrent writer). A present JSON ``null`` (or any non-object) is
    returned as it is, so callers refuse it as corruption, never as absence."""
    text = await asyncio.to_thread(_read_text_or_absent, path)
    if text is _ABSENT:
        return _ABSENT
    try:
        return json.loads(text)
    except ValueError:
        return text  # present but not JSON: corruption, refused by the caller


def _marker_path(store: Any, *, subject_hash: str, access_id: str, revision_name: str) -> Any:
    from .transaction_store import revision_marker_path

    return revision_marker_path(store, subject_hash=subject_hash, access_id=access_id, revision_name=revision_name)


def planned_link(*, authority: CardAuthority, at: datetime, tag: str) -> dict[str, Any]:
    """The link ``write_hidden_version(authority, at, tag)`` writes, computed WITHOUT writing (for a
    manifest recorded before the file)."""
    content_hash = authority.content_hash()
    return version_link(card_revision=authority.card_revision, content_hash=content_hash,
                        revision_name=card_revision_name(card_revision=authority.card_revision,
                                                         content_hash=content_hash, updated_at=at, txn=tag))


async def write_hidden_version(store: Any, *, subject_hash: str, authority: CardAuthority, at: datetime,
                               tag: str) -> dict[str, Any]:
    """Write ``authority`` once as its own txn-tagged version file, hidden by ``tag``; return its link.

    It reads what is already there FIRST and never overwrites an immutable version:
    - nothing there: the marker, then the file (as ``transaction_store._write_staged`` does), so the file
      is never visible as history, even for a moment;
    - the marker names ``tag`` and the file is absent (a crash between the two): the file;
    - the file with the same content: reused as it is; the marker is NOT recreated when it is gone (an
      adopted and committed replay stays committed);
    - a marker naming another owner refuses ``version_link_owner_conflict``; a file with other content
      refuses ``revision_content_hash_mismatch``.
    ``at`` is the caller's own time, so a retry names the same file.
    """
    content_hash = authority.content_hash()
    revision_name = card_revision_name(card_revision=authority.card_revision, content_hash=content_hash,
                                       updated_at=at, txn=tag)
    link = version_link(card_revision=authority.card_revision, revision_name=revision_name,
                        content_hash=content_hash)
    marker_path = _marker_path(store, subject_hash=subject_hash, access_id=authority.access_id,
                               revision_name=revision_name)
    file_path = store.revision_path(subject_hash=subject_hash, access_id=authority.access_id,
                                    revision_name=revision_name)
    marker = await _read_present(marker_path)
    payload = await _read_present(file_path)
    if marker is not _ABSENT and marker != {"transaction_id": tag}:
        raise CardRecordError("version_link_owner_conflict")
    if payload is not _ABSENT:
        if not isinstance(payload, dict):
            raise CardRecordError("revision_content_hash_mismatch")  # a present null or non-object
        payload.pop(VERSION_RECORD_KEY, None)
        if card_authority_payload_hash(payload) != content_hash:
            raise CardRecordError("revision_content_hash_mismatch")
        return link
    if marker is _ABSENT:
        await write_json_atomic(marker_path, {"transaction_id": tag})
    written = await store.write_revision(subject_hash=subject_hash, authority=authority, updated_at=at, txn=tag)
    if written.revision_name != revision_name:
        raise CardRecordError("version_link_name_mismatch")
    return link


async def adopt_hidden_version(store: Any, *, subject_hash: str, access_id: str, link: Any, from_tag: str,
                               to_transaction_id: str) -> dict[str, Any]:
    """Hand a hidden version to its real transaction: the SAME file, only its marker moves.

    For STAGE (lane 3) to reuse a planned candidate instead of writing it again. The file must still be
    exactly the link's content; the marker must name ``from_tag`` (or already ``to_transaction_id``, a
    retry). For a first-consent group member, ``to_transaction_id`` is the member's derived id
    (``transaction_store.member_transaction_id(real_txn, index)``). Refuses
    ``version_link_owner_conflict`` for any other owner, and never writes the version file.
    """
    marker_path = _marker_path(store, subject_hash=subject_hash, access_id=access_id,
                               revision_name=link["revision_name"] if isinstance(link, Mapping) else "")
    marker = await _read_present(marker_path)
    if marker == {"transaction_id": to_transaction_id}:
        await load_version(store, subject_hash=subject_hash, access_id=access_id, link=link,
                           marker=to_transaction_id)
        return link
    if marker != {"transaction_id": from_tag}:
        raise CardRecordError("version_link_owner_conflict")
    await load_version(store, subject_hash=subject_hash, access_id=access_id, link=link, marker=from_tag)
    await write_json_atomic(marker_path, {"transaction_id": to_transaction_id})
    return link


async def unlink_guarded_any(path: Any) -> None:
    """One guarded deletion: ``durable_io.unlink_guarded_async`` where R provides it (its async guard checks
    the Card lock's Redis owner), else the synchronous ``unlink_guarded``. Either way the store's writer
    guard decides first."""
    from connection_hub.delegated_credentials import durable_io

    unlink_async = getattr(durable_io, "unlink_guarded_async", None)
    if callable(unlink_async):
        await unlink_async(path)
    else:
        durable_io.unlink_guarded(path)


async def discard_hidden_version(store: Any, *, subject_hash: str, access_id: str, link: Any, tag: str) -> bool:
    """Delete a planned version whose plan ended before any transaction adopted it; True if it was there.

    Only a file still owned by exactly ``tag`` (a staging tag) and still exactly the link's content is
    deleted: the version file FIRST, then its marker, both through ``unlink_guarded_any`` (the store's
    writer guard decides; under R, the Card lock's owner too), so a crash never leaves an unmarked, history-visible file. An adopted file (any other
    owner) refuses ``version_link_owner_conflict``; a changed file refuses; nothing is repaired.
    """
    if not is_staging_tag(tag) or not is_version_link(link):
        raise CardRecordError("version_link_invalid")
    marker_path = _marker_path(store, subject_hash=subject_hash, access_id=access_id,
                               revision_name=link["revision_name"])
    file_path = store.revision_path(subject_hash=subject_hash, access_id=access_id,
                                    revision_name=link["revision_name"])
    marker = await _read_present(marker_path)
    if marker is _ABSENT:
        if await _read_present(file_path) is not _ABSENT:
            raise CardRecordError("version_link_owner_conflict")  # unmarked: history, never ours
        return False
    if marker != {"transaction_id": tag}:
        raise CardRecordError("version_link_owner_conflict")
    present = await _read_present(file_path)
    if present is not _ABSENT:
        await load_version(store, subject_hash=subject_hash, access_id=access_id, link=link, marker=tag)
        await unlink_guarded_any(file_path)
    await unlink_guarded_any(marker_path)
    return present is not _ABSENT


async def load_version(store: Any, *, subject_hash: str, access_id: str, link: Any,
                       marker: str | None = None, owners: Any = (), allow_unmarked: bool = False) -> CardAuthority:
    """The Card a link names, read directly by name; refuses unless content hash, card and revision match.

    ``marker``: an uncommitted candidate's tag. When given, the version file's ``.card-transaction.json``
    marker must name exactly that tag (``version_link_marker_mismatch``; ``version_link_marker_absent``
    when there is none). ``owners`` accepts one of several exact owners instead (a staging tag, or the
    transaction that adopted it); ``allow_unmarked`` also accepts a file whose marker is gone, which the
    caller allows only with evidence that its decision committed. With no owner named, only an unmarked
    file is accepted (never a marked one).
    """
    if not is_version_link(link):
        raise CardRecordError("version_link_invalid")
    accepted = {marker} if marker is not None else set(owners or ())
    found = await _read_present(_marker_path(store, subject_hash=subject_hash, access_id=access_id,
                                             revision_name=link["revision_name"]))
    if found is _ABSENT:
        if accepted and not allow_unmarked:
            raise CardRecordError("version_link_marker_absent")
    elif not (accepted and isinstance(found, Mapping) and set(found) == {"transaction_id"}
              and found["transaction_id"] in accepted):
        # Spark N2: with no owner named, only an unmarked version is accepted; a marked file never.
        raise CardRecordError("version_link_marker_mismatch")
    payload = await _read_present(store.revision_path(subject_hash=subject_hash, access_id=access_id,
                                                      revision_name=link["revision_name"]))
    if payload is _ABSENT:
        raise CardRecordError("version_link_missing")
    if not isinstance(payload, dict):
        raise CardRecordError("revision_content_hash_mismatch")
    payload.pop(VERSION_RECORD_KEY, None)
    if card_authority_payload_hash(payload) != link["content_hash"]:
        raise CardRecordError("revision_content_hash_mismatch")
    authority = CardAuthority.from_mapping(payload)
    if authority.card_revision != link["card_revision"] or authority.access_id != access_id:
        raise CardRecordError("revision_number_mismatch")
    return authority


__all__ = ["LINK_KEYS", "STAGING_TAG_PREFIX", "unlink_guarded_any", "adopt_hidden_version", "discard_hidden_version", "is_staging_tag",
           "is_version_link", "load_version", "planned_link", "pointer_link", "staging_tag", "version_link",
           "write_hidden_version"]

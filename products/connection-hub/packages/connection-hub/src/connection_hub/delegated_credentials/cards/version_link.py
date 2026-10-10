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

import hashlib
import json
from datetime import datetime
from typing import Any, Mapping

from connection_hub.delegated_credentials.durable_io import path_is_file, read_json_or_none, write_json_atomic
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


def staging_tag(scope: str, request_id: str) -> str:
    """A 64-hex tag for one planned version: SHA-256 of canonical tagged fields.

    Real transaction ids are 256 random bits, so a collision is negligible, not impossible; the marker
    and content checks below refuse rather than overwrite if a file ever has another owner.
    """
    if type(scope) is not str or not scope or type(request_id) is not str or not request_id:
        raise CardRecordError("version_link_tag_invalid")
    canonical = json.dumps({"kind": "card-version-staging-tag.v1", "scope": scope, "request_id": request_id},
                           sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


_ABSENT = object()


async def _read_present(path: Any) -> Any:
    """The parsed object, or ``_ABSENT`` only when the file is confirmed absent. A present JSON ``null``
    (or any non-object) is returned as it is, so callers refuse it as corruption, never as absence."""
    value = await read_json_or_none(path)
    if value is None and not await path_is_file(path):
        return _ABSENT
    return value


def _marker_path(store: Any, *, subject_hash: str, access_id: str, revision_name: str) -> Any:
    from .transaction_store import revision_marker_path

    return revision_marker_path(store, subject_hash=subject_hash, access_id=access_id, revision_name=revision_name)


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


async def load_version(store: Any, *, subject_hash: str, access_id: str, link: Any,
                       marker: str | None = None, owners: Any = (), allow_unmarked: bool = False) -> CardAuthority:
    """The Card a link names, read directly by name; refuses unless content hash, card and revision match.

    ``marker``: an uncommitted candidate's tag. When given, the version file's ``.card-transaction.json``
    marker must name exactly that tag (``version_link_marker_mismatch``). ``owners`` accepts one of several
    exact owners instead (a staging tag, or the transaction that adopted it); ``allow_unmarked`` also
    accepts a file whose marker is gone (a committed version after FINISH), never a different owner.
    """
    if not is_version_link(link):
        raise CardRecordError("version_link_invalid")
    accepted = {marker} if marker is not None else set(owners or ())
    if accepted or allow_unmarked:
        found = await _read_present(_marker_path(store, subject_hash=subject_hash, access_id=access_id,
                                                 revision_name=link["revision_name"]))
        if found is _ABSENT:
            if not allow_unmarked:
                raise CardRecordError("version_link_marker_mismatch")
        elif not (isinstance(found, Mapping) and set(found) == {"transaction_id"}
                  and found["transaction_id"] in accepted):
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


__all__ = ["LINK_KEYS", "adopt_hidden_version", "is_version_link", "load_version", "pointer_link", "staging_tag", "version_link",
           "write_hidden_version"]

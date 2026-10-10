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
tag is either the staging tag of a plan (``staging_tag``: no transaction ever has it, so the file is
never history) or a real transaction id. ``load_version(..., marker=tag)`` then requires that exact
marker, so a candidate is only ever read as the one its record names.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Mapping

from connection_hub.delegated_credentials.durable_io import read_json_or_none, write_json_atomic
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
    """A 64-hex tag for one planned version; never a real transaction id (those are random)."""
    return hashlib.sha256(f"{scope}:{request_id}".encode()).hexdigest()


def _marker_path(store: Any, *, subject_hash: str, access_id: str, revision_name: str) -> Any:
    from .transaction_store import revision_marker_path

    return revision_marker_path(store, subject_hash=subject_hash, access_id=access_id, revision_name=revision_name)


async def write_hidden_version(store: Any, *, subject_hash: str, authority: CardAuthority, at: datetime,
                               tag: str) -> dict[str, Any]:
    """Write ``authority`` once as its own txn-tagged version file, hidden by ``tag``; return its link.

    The marker comes first (as ``transaction_store._write_staged`` does), so the file is never visible as
    history, even for a moment. ``at`` is the caller's own time, so a retry names the same file; a retry
    that finds the file already written with the same content reuses it.
    """
    content_hash = authority.content_hash()
    revision_name = card_revision_name(card_revision=authority.card_revision, content_hash=content_hash,
                                       updated_at=at, txn=tag)
    await write_json_atomic(_marker_path(store, subject_hash=subject_hash, access_id=authority.access_id,
                                         revision_name=revision_name), {"transaction_id": tag})
    link = version_link(card_revision=authority.card_revision, revision_name=revision_name,
                        content_hash=content_hash)
    try:
        existing = await load_version(store, subject_hash=subject_hash, access_id=authority.access_id, link=link,
                                      marker=tag)
    except CardRecordError:
        existing = None
    if existing is None:
        written = await store.write_revision(subject_hash=subject_hash, authority=authority, updated_at=at, txn=tag)
        if written.revision_name != revision_name:
            raise CardRecordError("version_link_name_mismatch")
    return link


async def load_version(store: Any, *, subject_hash: str, access_id: str, link: Any,
                       marker: str | None = None) -> CardAuthority:
    """The Card a link names, read directly by name; refuses unless content hash, card and revision match.

    ``marker``: an uncommitted candidate's tag. When given, the version file's ``.card-transaction.json``
    marker must name exactly that tag (``version_link_marker_mismatch``).
    """
    if not is_version_link(link):
        raise CardRecordError("version_link_invalid")
    if marker is not None:
        found = await read_json_or_none(_marker_path(store, subject_hash=subject_hash, access_id=access_id,
                                                     revision_name=link["revision_name"]))
        if found != {"transaction_id": marker}:
            raise CardRecordError("version_link_marker_mismatch")
    payload = await read_json_or_none(store.revision_path(subject_hash=subject_hash, access_id=access_id,
                                                          revision_name=link["revision_name"]))
    if not isinstance(payload, dict):
        raise CardRecordError("version_link_missing")
    payload.pop(VERSION_RECORD_KEY, None)
    if card_authority_payload_hash(payload) != link["content_hash"]:
        raise CardRecordError("revision_content_hash_mismatch")
    authority = CardAuthority.from_mapping(payload)
    if authority.card_revision != link["card_revision"] or authority.access_id != access_id:
        raise CardRecordError("revision_number_mismatch")
    return authority


__all__ = ["LINK_KEYS", "is_version_link", "load_version", "pointer_link", "staging_tag", "version_link",
           "write_hidden_version"]

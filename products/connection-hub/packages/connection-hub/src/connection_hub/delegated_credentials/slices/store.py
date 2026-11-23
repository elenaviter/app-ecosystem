# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W40: durable conversation slices beside the Agent Card they narrow.

Layout under the shared bundle-storage Card root (design 2026-11-20, amended
2026-11-23):

    delegated-cards/v1/grantors/<subject_hash>/slices/<agent_card_access_id>/<conversation_key>/
        revisions/slice_revision_<NNNNNNNN>.json

``<conversation_key>`` is a digest of the conversation id, so no client-chosen
id reaches a path. A revision file is created exclusively: two writers that
expect the same revision cannot both commit, which is the slice's
compare-and-set. The current slice is its highest revision. A slice holds only
what the Hub guard decides (operations, resources, named-service operations,
resource families), never a credential, an account or a model choice.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from connection_hub.delegated_credentials.cards.store import subject_hash_for
from connection_hub.delegated_credentials.durable_io import list_child_names, read_json_or_none

SLICES_DIRNAME = "slices"
REVISIONS_DIRNAME = "revisions"
SLICE_SCHEMA = "connection-hub.conversation-slice.v1"
SLICE_STATE_ACTIVE = "active"
SLICE_STATE_DELETING = "deleting"
SLICE_STATES = frozenset({SLICE_STATE_ACTIVE, SLICE_STATE_DELETING})
SELECTION_CATEGORIES = ("resource_grants", "resource_operations", "named_service_operations", "resource_families")
_REVISION_FILE = re.compile(r"^slice_revision_([0-9]{8})\.json$")
_ACCESS_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class SliceStoreError(RuntimeError):
    def __init__(self, reason: str, *, retryable: bool = False) -> None:
        super().__init__(reason)
        self.reason, self.retryable = reason, retryable


class SliceConflict(SliceStoreError):
    """The slice moved: another writer committed the expected next revision first."""

    def __init__(self, current_revision: int) -> None:
        super().__init__("conversation_slice_revision_conflict", retryable=True)
        self.current_revision = current_revision


def conversation_key(conversation_id: str) -> str:
    conversation = str(conversation_id or "").strip()
    if not conversation:
        raise SliceStoreError("conversation_id_required")
    return hashlib.sha256(conversation.encode("utf-8")).hexdigest()


def _clean_selection(value: Mapping[str, Any] | None) -> dict[str, Any]:
    selection = dict(value or {})
    unknown = set(selection) - set(SELECTION_CATEGORIES)
    if unknown:
        # Model, instruction, skills, presentation and cache policy stay in KDCube.
        raise SliceStoreError("conversation_slice_category_not_guarded")
    return json.loads(json.dumps(selection, sort_keys=True))


@dataclass(frozen=True)
class ConversationSlice:
    """One conversation's narrowing of one Agent Card."""

    grantor_subject: str
    card_access_id: str
    conversation_id: str
    card_generation: int
    base_card_revision: int
    selection: Mapping[str, Any] = field(default_factory=dict)
    pending: Mapping[str, Any] | None = None
    state: str = SLICE_STATE_ACTIVE
    slice_revision: int = 0

    def __post_init__(self) -> None:
        if not str(self.grantor_subject or "").strip():
            raise SliceStoreError("conversation_slice_grantor_required")
        if not _ACCESS_ID.fullmatch(str(self.card_access_id or "")):
            raise SliceStoreError("conversation_slice_card_invalid")
        conversation_key(self.conversation_id)
        if self.state not in SLICE_STATES:
            raise SliceStoreError("conversation_slice_state_invalid")
        object.__setattr__(self, "selection", _clean_selection(self.selection))
        if self.pending is not None:
            object.__setattr__(self, "pending", _clean_selection(self.pending))

    def to_document(self) -> dict[str, Any]:
        return {
            "schema": SLICE_SCHEMA,
            "grantor_subject": self.grantor_subject,
            "card_access_id": self.card_access_id,
            "conversation_id": self.conversation_id,
            "card_generation": int(self.card_generation),
            "base_card_revision": int(self.base_card_revision),
            "selection": dict(self.selection),
            "pending": dict(self.pending) if self.pending is not None else None,
            "state": self.state,
            "slice_revision": int(self.slice_revision),
        }

    @classmethod
    def from_document(cls, value: Any) -> "ConversationSlice":
        if not isinstance(value, Mapping) or value.get("schema") != SLICE_SCHEMA:
            raise SliceStoreError("conversation_slice_document_invalid")
        return cls(
            grantor_subject=str(value.get("grantor_subject") or ""),
            card_access_id=str(value.get("card_access_id") or ""),
            conversation_id=str(value.get("conversation_id") or ""),
            card_generation=int(value.get("card_generation") or 0),
            base_card_revision=int(value.get("base_card_revision") or 0),
            selection=value.get("selection") or {},
            pending=value.get("pending"),
            state=str(value.get("state") or ""),
            slice_revision=int(value.get("slice_revision") or 0),
        )


class BundleStorageConversationSliceStore:
    """Slices under ``<cards root>/grantors/<subject_hash>/slices``."""

    def __init__(self, root: pathlib.Path | str) -> None:
        self._root = pathlib.Path(root)

    def _directory(self, grantor_subject: str, card_access_id: str, conversation_id: str) -> pathlib.Path:
        if not _ACCESS_ID.fullmatch(str(card_access_id or "")):
            raise SliceStoreError("conversation_slice_card_invalid")
        return (self._root / "grantors" / subject_hash_for(grantor_subject) / SLICES_DIRNAME / card_access_id
                / conversation_key(conversation_id) / REVISIONS_DIRNAME)

    async def _revisions(self, directory: pathlib.Path) -> list[int]:
        names = await list_child_names(directory)
        return sorted(int(match.group(1)) for name in names if (match := _REVISION_FILE.fullmatch(name)))

    async def read(self, *, grantor_subject: str, card_access_id: str,
                   conversation_id: str) -> ConversationSlice | None:
        """The current slice (its highest revision), or None when none exists."""
        directory = self._directory(grantor_subject, card_access_id, conversation_id)
        revisions = await self._revisions(directory)
        if not revisions:
            return None
        document = await read_json_or_none(directory / f"slice_revision_{revisions[-1]:08d}.json")
        current = ConversationSlice.from_document(document)
        if (current.grantor_subject, current.card_access_id, current.conversation_id, current.slice_revision) != (
                grantor_subject, card_access_id, conversation_id, revisions[-1]):
            raise SliceStoreError("conversation_slice_document_mismatch")
        return current

    async def write(self, candidate: ConversationSlice, *, expected_revision: int) -> ConversationSlice:
        """Commit ``candidate`` as revision ``expected_revision + 1``; exclusive create is the CAS."""
        directory = self._directory(candidate.grantor_subject, candidate.card_access_id, candidate.conversation_id)
        revisions = await self._revisions(directory)
        current = revisions[-1] if revisions else 0
        if current != int(expected_revision):
            raise SliceConflict(current)
        committed = ConversationSlice(**{**candidate.__dict__, "slice_revision": current + 1})
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"slice_revision_{current + 1:08d}.json"
        payload = json.dumps(committed.to_document(), sort_keys=True).encode("utf-8")
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise SliceConflict(current + 1) from None
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return committed

    async def remove(self, *, grantor_subject: str, card_access_id: str, conversation_id: str) -> int:
        """Remove every revision of one slice (after its conversation's messages are gone)."""
        directory = self._directory(grantor_subject, card_access_id, conversation_id)
        removed = 0
        for revision in await self._revisions(directory):
            try:
                os.unlink(directory / f"slice_revision_{revision:08d}.json")
                removed += 1
            except FileNotFoundError:
                pass
        return removed


__all__ = [
    "BundleStorageConversationSliceStore",
    "ConversationSlice",
    "SELECTION_CATEGORIES",
    "SLICE_STATE_ACTIVE",
    "SLICE_STATE_DELETING",
    "SliceConflict",
    "SliceStoreError",
    "conversation_key",
]

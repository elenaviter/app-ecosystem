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
PREPARED_DIRNAME = "prepared"
SLICE_SCHEMA = "connection-hub.conversation-slice.v1"
SLICE_STATE_ACTIVE = "active"
SLICE_STATE_DELETING = "deleting"
SLICE_STATES = frozenset({SLICE_STATE_ACTIVE, SLICE_STATE_DELETING})
SELECTION_CATEGORIES = ("resource_grants", "resource_operations", "named_service_operations", "resource_families")
_REVISION_FILE = re.compile(r"^slice_revision_([0-9]{8})\.json$")
_ACCESS_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_CHANGE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")



class SliceStoreError(RuntimeError):
    def __init__(self, reason: str, *, retryable: bool = False) -> None:
        super().__init__(reason)
        self.reason, self.retryable = reason, retryable


class SlicePrepared(SliceStoreError):
    """A Card transaction prepared a change of this slice and is undecided (review E1)."""

    def __init__(self) -> None:
        super().__init__("conversation_slice_prepared", retryable=True)


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
    state: str = SLICE_STATE_ACTIVE
    slice_revision: int = 0
    # The Card-transaction change that wrote this revision ("" for a direct
    # write), so a replayed COMMIT finds its own revision exactly (W582).
    change_id: str = ""
    # The conversation's deletion epoch when this slice was seeded: the start
    # of its latest completed deletion (0 when never deleted). A conversation
    # created again under the same id after a deletion has a later epoch, so
    # it never inherits this slice.
    conversation_epoch: int = 0

    def __post_init__(self) -> None:
        if not str(self.grantor_subject or "").strip():
            raise SliceStoreError("conversation_slice_grantor_required")
        if not _ACCESS_ID.fullmatch(str(self.card_access_id or "")):
            raise SliceStoreError("conversation_slice_card_invalid")
        conversation_key(self.conversation_id)
        if self.state not in SLICE_STATES:
            raise SliceStoreError("conversation_slice_state_invalid")
        object.__setattr__(self, "selection", _clean_selection(self.selection))

    def to_document(self) -> dict[str, Any]:
        return {
            "schema": SLICE_SCHEMA,
            "grantor_subject": self.grantor_subject,
            "card_access_id": self.card_access_id,
            "conversation_id": self.conversation_id,
            "card_generation": int(self.card_generation),
            "base_card_revision": int(self.base_card_revision),
            "selection": dict(self.selection),
            "state": self.state,
            "slice_revision": int(self.slice_revision),
            "change_id": self.change_id,
            "conversation_epoch": int(self.conversation_epoch),
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
            state=str(value.get("state") or ""),
            slice_revision=int(value.get("slice_revision") or 0),
            change_id=str(value.get("change_id") or ""),
            conversation_epoch=int(value.get("conversation_epoch") or 0),
        )


class BundleStorageConversationSliceStore:
    """Slices under ``<cards root>/grantors/<subject_hash>/slices``."""

    def __init__(self, root: pathlib.Path | str) -> None:
        # A marker of a crashed transaction is released by the Hub's existing
        # recovery cron (card-transaction-recover), whose FINISH applies or
        # releases this effect (design 2026-11-24, point 4); never here.
        self._root = pathlib.Path(root)

    async def _markers(self, prepared_directory: pathlib.Path, *, own: str = "") -> list[str]:
        """Prepared markers other than ``own``."""
        names = [name[:-5] for name in await list_child_names(prepared_directory) if name.endswith(".json")]
        return [name for name in names if name != own]

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

    def _prepared_directory(self, revisions_directory: pathlib.Path) -> pathlib.Path:
        return revisions_directory.parent / PREPARED_DIRNAME

    async def prepared_changes(self, *, grantor_subject: str, card_access_id: str, conversation_id: str) -> list[str]:
        directory = self._prepared_directory(self._directory(grantor_subject, card_access_id, conversation_id))
        return sorted(await self._markers(directory))

    async def prepared_candidates(self, *, grantor_subject: str, card_access_id: str,
                                  conversation_id: str) -> list[ConversationSlice]:
        """The slices undecided Card transactions prepared (option c reads them; an unreadable one refuses)."""
        directory = self._prepared_directory(self._directory(grantor_subject, card_access_id, conversation_id))
        out = []
        for change_id in sorted(await self._markers(directory)):
            document = await read_json_or_none(directory / f"{change_id}.json")
            if not isinstance(document, Mapping):
                raise SliceStoreError("conversation_slice_marker_unreadable", retryable=True)
            out.append(ConversationSlice.from_document(document.get("slice")))
        return out

    async def write_when_open(self, candidate: ConversationSlice, *, expected_revision: int,
                              wait_seconds: float = 60.0, poll_seconds: float = 0.25) -> ConversationSlice:
        """A direct write that waits, bounded, for an undecided transaction's marker (design point 3)."""
        import asyncio
        import time

        deadline = time.monotonic() + max(0.0, wait_seconds)
        while True:
            try:
                return await self.write(candidate, expected_revision=expected_revision)
            except SlicePrepared:
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(poll_seconds)

    async def write(self, candidate: ConversationSlice, *, expected_revision: int,
                    _change_id: str = "") -> ConversationSlice:
        """Commit ``candidate`` as revision ``expected_revision + 1``; exclusive create is the CAS.

        A direct write refuses while a Card transaction holds a prepared change
        of this slice (review E1); only that change's COMMIT may write then.
        """
        directory = self._directory(candidate.grantor_subject, candidate.card_access_id, candidate.conversation_id)
        if await self._markers(self._prepared_directory(directory), own=_change_id):
            raise SlicePrepared()
        revisions = await self._revisions(directory)
        current = revisions[-1] if revisions else 0
        if current != int(expected_revision):
            raise SliceConflict(current)
        committed = ConversationSlice(**{**candidate.__dict__, "slice_revision": current + 1,
                                         "change_id": _change_id})
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

    # -- Card-transaction effect (design 2026-11-23): STAGE, COMMIT, ABORT --------

    def _marker(self, candidate: ConversationSlice, change_id: str) -> pathlib.Path:
        if not _CHANGE_ID.fullmatch(str(change_id or "")):
            raise SliceStoreError("conversation_slice_change_invalid")
        directory = self._directory(candidate.grantor_subject, candidate.card_access_id, candidate.conversation_id)
        return self._prepared_directory(directory) / f"{change_id}.json"

    async def prepare(self, candidate: ConversationSlice, *, expected_revision: int, change_id: str) -> None:
        """STAGE: pin the expected revision and close the slice to every other writer."""
        marker = self._marker(candidate, change_id)
        if marker.exists():
            return  # replay of this same STAGE
        directory = self._directory(candidate.grantor_subject, candidate.card_access_id, candidate.conversation_id)
        if await self._markers(marker.parent, own=change_id):
            raise SlicePrepared()
        revisions = await self._revisions(directory)
        if (revisions[-1] if revisions else 0) != int(expected_revision):
            raise SliceConflict(revisions[-1] if revisions else 0)
        marker.parent.mkdir(parents=True, exist_ok=True)
        document = {"expected_revision": int(expected_revision), "slice": candidate.to_document()}
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, json.dumps(document, sort_keys=True).encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    async def commit_prepared(self, candidate: ConversationSlice, *, change_id: str) -> ConversationSlice:
        """COMMIT: write the prepared revision once; a replay finds it written and returns it."""
        marker = self._marker(candidate, change_id)
        prepared = await read_json_or_none(marker)
        written = await self._revision_written_by(candidate, change_id)
        if written is not None:
            committed = written  # an earlier COMMIT of this change wrote it (W582 exact replay)
        elif prepared is None:
            raise SliceStoreError("conversation_slice_prepare_missing")
        else:
            committed = await self.write(candidate, expected_revision=int(prepared["expected_revision"]),
                                         _change_id=change_id)
        try:
            os.unlink(marker)
        except FileNotFoundError:
            pass
        return committed

    async def _revision_written_by(self, candidate: ConversationSlice, change_id: str) -> ConversationSlice | None:
        directory = self._directory(candidate.grantor_subject, candidate.card_access_id, candidate.conversation_id)
        # Newest first, bounded: a COMMIT is replayed soon after it wrote.
        for revision in reversed((await self._revisions(directory))[-64:]):
            document = await read_json_or_none(directory / f"slice_revision_{revision:08d}.json")
            if isinstance(document, Mapping) and document.get("change_id") == change_id:
                return ConversationSlice.from_document(document)
        return None

    async def release(self, candidate: ConversationSlice, *, change_id: str) -> None:
        """ABORT (or an expired, undecided transaction's recovery): remove only this change's marker."""
        try:
            os.unlink(self._marker(candidate, change_id))
        except FileNotFoundError:
            pass

    async def cards_with_conversation(self, *, grantor_subject: str, conversation_id: str) -> list[str]:
        """The Agent Cards of this owner that hold a slice of the conversation (A1 delete)."""
        base = self._root / "grantors" / subject_hash_for(grantor_subject) / SLICES_DIRNAME
        key = conversation_key(conversation_id)
        out = []
        for card_access_id in await list_child_names(base):
            if _ACCESS_ID.fullmatch(card_access_id) and key in await list_child_names(base / card_access_id):
                out.append(card_access_id)
        return sorted(out)

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
    "SlicePrepared",
    "SliceStoreError",
    "conversation_key",
]

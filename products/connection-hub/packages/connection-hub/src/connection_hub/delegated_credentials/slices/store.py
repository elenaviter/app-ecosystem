# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Durable conversation slices beside the Agent Card they narrow.

Layout under the shared bundle-storage Card root:

    delegated-cards/v1/grantors/<subject_hash>/slices/<agent_card_access_id>/<conversation_key>/
        revisions/slice_revision_<NNNNNNNN>.json

``<conversation_key>`` is a digest of the conversation id, so no client-chosen
id reaches a path. The current slice is its highest revision. A slice holds only
what the Hub guard decides (operations, resources, named-service operations,
resource families), never a credential, an account or a model choice.

Every write runs in the slice's section of the Card mutation lock (Redis with
the observed file lock, or the file lock alone, as configured for Cards). The
storage check underneath does not depend on that lock: revision N+1 is first
claimed by creating ``slice_revision_N+1.claim`` exclusively (``O_EXCL``), so two
writers that expect revision N cannot both commit even when the lock fails.
The claimant then writes the revision to a temporary file, makes it durable and
moves it onto ``slice_revision_N+1.json``. Readers list only ``.json`` files, so
they never see a partial revision. Nothing needs hard links.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Mapping

from connection_hub.delegated_credentials.cards.store import subject_hash_for
from connection_hub.delegated_credentials.durable_io import list_child_names, read_json_or_none

SLICES_DIRNAME = "slices"
REVISIONS_DIRNAME = "revisions"
PREPARED_DIRNAME = "prepared"
SLICE_SCHEMA = "connection-hub.conversation-slice.v1"
SLICE_STATE_ACTIVE = "active"
SLICE_STATES = frozenset({SLICE_STATE_ACTIVE})
SELECTION_CATEGORIES = ("resource_grants", "resource_operations", "named_service_operations", "resource_families")
_REVISION_FILE = re.compile(r"^slice_revision_([0-9]{8})\.json$")
_CLAIM_FILE = re.compile(r"^slice_revision_([0-9]{8})\.claim$")
# A claim without its revision older than this was left by a writer that
# stopped between claiming and writing (the Card lock TTL is 120 s).
STALE_CLAIM_SECONDS = 180.0
_ACCESS_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_CHANGE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")



class SliceStoreError(RuntimeError):
    def __init__(self, reason: str, *, retryable: bool = False) -> None:
        super().__init__(reason)
        self.reason, self.retryable = reason, retryable


class SlicePrepared(SliceStoreError):
    """A Card transaction prepared a change of this slice and is undecided."""

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
    # write), so a replayed COMMIT finds its own revision exactly.
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


def _write_durable(path: pathlib.Path, document: Mapping[str, Any]) -> None:
    """Replace ``path`` with the complete ``document``: a durable temporary file moved over it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(6).hex()}.tmp")
    payload = json.dumps(dict(document), sort_keys=True).encode("utf-8")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
    finally:
        _unlink_quietly(temporary)
        _fsync_directory(path.parent)


def _claim(path: pathlib.Path) -> bool:
    """Create the empty claim ``path`` exclusively; False when it already exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)
    return True


def _unlink_quietly(path: pathlib.Path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _fsync_directory(directory: pathlib.Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


SliceSection = Callable[[str, str, str], Any]


class BundleStorageConversationSliceStore:
    """Slices under ``<cards root>/grantors/<subject_hash>/slices``.

    ``section(subject_hash, card_access_id, conversation_key)`` is the slice's
    section of the Card mutation lock (``DelegatedCardService.conversation_slice_section``).
    A store without one still refuses a lost update through its exclusive claim.
    """

    def __init__(self, root: pathlib.Path | str, *, section: SliceSection | None = None) -> None:
        # A marker of a crashed transaction is released by the Hub's existing
        # recovery cron (card-transaction-recover), whose FINISH applies or
        # releases this effect; never here.
        self._root = pathlib.Path(root)
        self._section = section

    @asynccontextmanager
    async def _held(self, candidate: "ConversationSlice") -> AsyncIterator[None]:
        if self._section is None:
            yield
            return
        async with self._section(subject_hash_for(candidate.grantor_subject), candidate.card_access_id,
                                 conversation_key(candidate.conversation_id)):
            yield

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

    async def _claims(self, directory: pathlib.Path) -> list[int]:
        names = await list_child_names(directory)
        return sorted(int(match.group(1)) for name in names if (match := _CLAIM_FILE.fullmatch(name)))

    async def read(self, *, grantor_subject: str, card_access_id: str,
                   conversation_id: str) -> ConversationSlice | None:
        """The current slice (its highest revision), or None when none exists."""
        directory = self._directory(grantor_subject, card_access_id, conversation_id)
        for _attempt in range(2):
            revisions = await self._revisions(directory)
            if not revisions:
                return None
            document = await read_json_or_none(directory / f"slice_revision_{revisions[-1]:08d}.json")
            if document is not None:
                break
            # The highest revision went away between the listing and the read
            # (a withdrawn write): list once more.
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
        """The slices undecided Card transactions prepared (the guard reads them; an unreadable one refuses)."""
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
        """A direct write that waits, bounded, for an undecided transaction's marker."""
        import asyncio

        deadline = time.monotonic() + max(0.0, wait_seconds)
        while True:
            try:
                return await self.write(candidate, expected_revision=expected_revision)
            except SlicePrepared:
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(poll_seconds)

    async def write(self, candidate: ConversationSlice, *, expected_revision: int) -> ConversationSlice:
        """Commit ``candidate`` as revision ``expected_revision + 1``.

        A direct write refuses while a Card transaction holds a prepared change
        of this slice; only that change's COMMIT may write then.
        """
        async with self._held(candidate):
            return await self._write(candidate, expected_revision=expected_revision, change_id="")

    async def _write(self, candidate: ConversationSlice, *, expected_revision: int,
                     change_id: str) -> ConversationSlice:
        directory = self._directory(candidate.grantor_subject, candidate.card_access_id, candidate.conversation_id)
        if await self._markers(self._prepared_directory(directory), own=change_id):
            raise SlicePrepared()
        revisions = await self._revisions(directory)
        current = revisions[-1] if revisions else 0
        if current != int(expected_revision):
            raise SliceConflict(current)
        claim = directory / f"slice_revision_{current + 1:08d}.claim"
        if not _claim(claim):
            if not self._stale(claim):
                raise SliceConflict(current + 1)
            # Its writer stopped between claiming and writing: the claim is
            # removed and this write claims again.
            _unlink_quietly(claim)
            if not _claim(claim):
                raise SliceConflict(current + 1)
        committed = ConversationSlice(**{**candidate.__dict__, "slice_revision": current + 1,
                                         "change_id": change_id})
        _write_durable(directory / f"slice_revision_{current + 1:08d}.json", committed.to_document())
        return committed

    @staticmethod
    def _stale(claim: pathlib.Path) -> bool:
        try:
            age = time.time() - claim.stat().st_mtime
        except FileNotFoundError:
            return True
        return age > STALE_CLAIM_SECONDS and not claim.with_suffix(".json").exists()

    # -- Card-transaction effect: STAGE, COMMIT, ABORT --------

    def _marker(self, candidate: ConversationSlice, change_id: str) -> pathlib.Path:
        if not _CHANGE_ID.fullmatch(str(change_id or "")):
            raise SliceStoreError("conversation_slice_change_invalid")
        directory = self._directory(candidate.grantor_subject, candidate.card_access_id, candidate.conversation_id)
        return self._prepared_directory(directory) / f"{change_id}.json"

    async def prepare(self, candidate: ConversationSlice, *, expected_revision: int, change_id: str) -> None:
        """STAGE: pin the expected revision and close the slice to every other writer."""
        marker = self._marker(candidate, change_id)
        async with self._held(candidate):
            if marker.exists():
                return  # replay of this same STAGE
            directory = self._directory(candidate.grantor_subject, candidate.card_access_id,
                                        candidate.conversation_id)
            if await self._markers(marker.parent, own=change_id):
                raise SlicePrepared()
            revisions = await self._revisions(directory)
            current = revisions[-1] if revisions else 0
            if current != int(expected_revision) or (await self._claims(directory))[-1:] > [current]:
                raise SliceConflict(current)
            _write_durable(marker, {"expected_revision": int(expected_revision), "slice": candidate.to_document()})

    async def commit_prepared(self, candidate: ConversationSlice, *, change_id: str) -> ConversationSlice:
        """COMMIT: write the prepared revision once; a replay finds it written and returns it."""
        marker = self._marker(candidate, change_id)
        async with self._held(candidate):
            prepared = await read_json_or_none(marker)
            written = await self._revision_written_by(candidate, change_id)
            if written is not None:
                committed = written  # an earlier COMMIT of this change wrote it (an exact replay)
            elif prepared is None:
                raise SliceStoreError("conversation_slice_prepare_missing")
            else:
                committed = await self._write(candidate, expected_revision=int(prepared["expected_revision"]),
                                              change_id=change_id)
            _unlink_quietly(marker)
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
        async with self._held(candidate):
            _unlink_quietly(self._marker(candidate, change_id))


__all__ = [
    "BundleStorageConversationSliceStore",
    "ConversationSlice",
    "SELECTION_CATEGORIES",
    "SLICE_STATE_ACTIVE",
    "SliceConflict",
    "SlicePrepared",
    "SliceStoreError",
    "conversation_key",
]

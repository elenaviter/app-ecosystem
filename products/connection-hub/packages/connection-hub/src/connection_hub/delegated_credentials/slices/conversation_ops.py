# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W40: the slice writes KDCube asks for, outside any Card transaction.

- the conversation picker replaces the conversation's selection, or parks it
  as a deferred change the cache policy applies later (A2);
- a deferred change is promoted by the runtime's trigger only: it names the
  turn by its token and the Hub applies exactly the stored change;
- deleting a conversation first marks its slices deleting (no token, no
  seed), then KDCube deletes the messages, then the slices are removed (A1).

Each is a direct slice write with the slice's own compare-and-set; one that
meets an undecided Card transaction waits, bounded, then refuses retryably.
"""

from __future__ import annotations

import dataclasses
import time
from typing import Any, Mapping

from connection_hub.delegated_credentials.slices.store import (
    SELECTION_CATEGORIES, SLICE_STATE_ACTIVE, SLICE_STATE_DELETING, ConversationSlice, SliceConflict,
    SlicePrepared, SliceStoreError,
)
from connection_hub.delegated_credentials.slices.turn_operation import seed_selection_of
from connection_hub.delegated_credentials.slices.turns import TurnRefused, card_generation_of

PICKER_WAIT_SECONDS = 60.0


def _refusal(reason: str, *, retryable: bool = False, status: int = 409, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": reason, "retryable": retryable, "status": status, **extra}


async def _card(service: Any, user: Mapping[str, Any], application: str, agent_id: str) -> tuple[Any, Any]:
    from connection_hub.delegated_credentials.automation_access import _subject_from_user, card_authority_from_record
    from connection_hub.delegated_credentials.cards.identity import resident_client_id, stable_resident_access_id

    grantor = _subject_from_user(user)
    if not grantor:
        return None, _refusal("delegated_access_requires_authenticated_user", status=401)
    loaded = await service._load_record_any_state(
        stable_resident_access_id(grantor, resident_client_id(application, agent_id)), grantor_subject=grantor)
    if loaded is None or loaded[1] != "active":
        return None, _refusal("agent_capability_card_not_active", retryable=True)
    return card_authority_from_record(loaded[0]), None


def _guarded(selection: Any) -> dict[str, Any]:
    selection = dict(selection or {})
    if set(selection) - set(SELECTION_CATEGORIES):
        raise SliceStoreError("conversation_slice_category_not_guarded")
    return selection


async def _seeded(service: Any, store: Any, card: Any, application: str, conversation_id: str) -> ConversationSlice:
    """The current slice; a picker write before the first turn seeds it after the registry check."""
    current = await store.read(grantor_subject=card.grantor_subject, card_access_id=card.access_id,
                               conversation_id=conversation_id)
    if current is not None and current.card_generation == card_generation_of(card):
        return current
    registry = getattr(service, "_conversation_registry", None)
    if registry is None:
        raise TurnRefused("conversation_slices_unavailable", retryable=True, status=503)
    try:
        registered = await registry.conversation(owner_subject=card.grantor_subject, conversation_id=conversation_id)
    except Exception:
        raise TurnRefused("conversation_registry_unavailable", retryable=True, status=503) from None
    if registered is None or registered.owner_subject != card.grantor_subject:
        raise TurnRefused("conversation_not_owned")
    if registered.application != application:
        raise TurnRefused("conversation_application_mismatch")
    seeded = ConversationSlice(grantor_subject=card.grantor_subject, card_access_id=card.access_id,
                               conversation_id=conversation_id, card_generation=card_generation_of(card),
                               base_card_revision=card.card_revision, selection=seed_selection_of(card))
    return await store.write(seeded, expected_revision=current.slice_revision if current else 0)


async def update_conversation_selection(
    service: Any, user: Mapping[str, Any], *, application: str, agent_id: str, conversation_id: str,
    selection: Mapping[str, Any], expected_slice_revision: int | None = None, deferred: bool = False,
    wait_seconds: float = PICKER_WAIT_SECONDS,
) -> dict[str, Any]:
    """The conversation picker: replace the selection now, or park it as the deferred change (A2)."""

    card, refused = await _card(service, user, application, agent_id)
    if refused:
        return refused
    store, _tokens = service._conversation_slice_parts()
    if store is None:
        return _refusal("conversation_slices_unavailable", retryable=True, status=503)
    try:
        wanted = _guarded(selection)
        current = await _seeded(service, store, card, application, conversation_id)
        if current.state != SLICE_STATE_ACTIVE:
            return _refusal("conversation_deleting")
        if expected_slice_revision is not None and int(expected_slice_revision) != current.slice_revision:
            return _refusal("conversation_slice_revision_conflict", retryable=True,
                            current_revision=current.slice_revision)
        candidate = (dataclasses.replace(current, pending=wanted, change_id="") if deferred
                     else dataclasses.replace(current, selection=wanted, pending=None, change_id=""))
        written = await store.write_when_open(candidate, expected_revision=current.slice_revision,
                                              wait_seconds=wait_seconds)
    except TurnRefused as exc:
        return exc.to_dict()
    except SlicePrepared:
        # The authority-update window (design 2026-11-24): retry later.
        return _refusal("agent_authority_updating", retryable=True, status=409)
    except SliceConflict as exc:
        return _refusal("conversation_slice_revision_conflict", retryable=True, current_revision=exc.current_revision)
    except SliceStoreError as exc:
        return _refusal(exc.reason, retryable=exc.retryable)
    return {"ok": True, "slice_revision": written.slice_revision, "selection": dict(written.selection),
            "pending": dict(written.pending) if written.pending is not None else None}


async def promote_conversation_pending(service: Any, user: Mapping[str, Any], *, application: str, agent_id: str,
                                       turn_token: str) -> dict[str, Any]:
    """A2: the runtime only triggers; the Hub applies exactly the stored deferred change."""

    card, refused = await _card(service, user, application, agent_id)
    if refused:
        return refused
    store, tokens = service._conversation_slice_parts()
    if store is None or tokens is None:
        return _refusal("conversation_slices_unavailable", retryable=True, status=503)
    try:
        claims = tokens.verify(turn_token, card_access_id=card.access_id, now=int(time.time()),
                               card_generation=card_generation_of(card))
        current = await store.read(grantor_subject=card.grantor_subject, card_access_id=card.access_id,
                                   conversation_id=claims.conversation_id)
        if current is None or current.pending is None:
            return _refusal("conversation_slice_no_pending")
        if current.state != SLICE_STATE_ACTIVE:
            return _refusal("conversation_deleting")
        written = await store.write_when_open(
            dataclasses.replace(current, selection=dict(current.pending), pending=None, change_id=""),
            expected_revision=current.slice_revision, wait_seconds=PICKER_WAIT_SECONDS)
    except TurnRefused as exc:
        return exc.to_dict()
    except SlicePrepared:
        return _refusal("agent_authority_updating", retryable=True)
    except SliceConflict as exc:
        return _refusal("conversation_slice_revision_conflict", retryable=True, current_revision=exc.current_revision)
    return {"ok": True, "slice_revision": written.slice_revision}


async def mark_conversation_deleting(service: Any, user: Mapping[str, Any], *, conversation_id: str) -> dict[str, Any]:
    """A1 step 1: every slice of the owner's conversation becomes deleting; nothing is deleted yet."""

    from connection_hub.delegated_credentials.automation_access import _subject_from_user

    grantor = _subject_from_user(user)
    store, _ = service._conversation_slice_parts()
    if not grantor or store is None:
        return _refusal("conversation_slices_unavailable", retryable=True, status=503)
    marked = 0
    try:
        for card_access_id in await store.cards_with_conversation(grantor_subject=grantor,
                                                                  conversation_id=conversation_id):
            current = await store.read(grantor_subject=grantor, card_access_id=card_access_id,
                                       conversation_id=conversation_id)
            if current is None or current.state == SLICE_STATE_DELETING:
                continue
            await store.write_when_open(dataclasses.replace(current, state=SLICE_STATE_DELETING, change_id=""),
                                        expected_revision=current.slice_revision, wait_seconds=PICKER_WAIT_SECONDS)
            marked += 1
    except (SlicePrepared, SliceConflict, SliceStoreError):
        # Nothing of the conversation is deleted while this refuses (A1).
        return _refusal("agent_authority_updating", retryable=True, status=503)
    return {"ok": True, "marked": marked}


async def remove_conversation_slices(service: Any, user: Mapping[str, Any], *, conversation_id: str) -> dict[str, Any]:
    """A1 step 3, after KDCube deleted the messages: remove every slice marked deleting."""

    from connection_hub.delegated_credentials.automation_access import _subject_from_user

    grantor = _subject_from_user(user)
    store, _ = service._conversation_slice_parts()
    if not grantor or store is None:
        return _refusal("conversation_slices_unavailable", retryable=True, status=503)
    removed = 0
    for card_access_id in await store.cards_with_conversation(grantor_subject=grantor, conversation_id=conversation_id):
        current = await store.read(grantor_subject=grantor, card_access_id=card_access_id,
                                   conversation_id=conversation_id)
        if current is not None and current.state != SLICE_STATE_DELETING:
            return _refusal("conversation_not_marked_deleting")
        removed += await store.remove(grantor_subject=grantor, card_access_id=card_access_id,
                                      conversation_id=conversation_id)
    return {"ok": True, "removed_revisions": removed}


__all__ = ["mark_conversation_deleting", "promote_conversation_pending", "remove_conversation_slices",
           "update_conversation_selection"]

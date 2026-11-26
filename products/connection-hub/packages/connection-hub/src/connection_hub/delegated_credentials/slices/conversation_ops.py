# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""The conversation picker's write: a slice change outside any Card transaction.

The picker replaces the conversation's selection; it applies from the next
call, as KDCube applies a capability change from the next message. It is a
direct slice write with the slice's own compare-and-set; one that meets an
undecided Card transaction waits, bounded, then refuses retryably.

Deleting a conversation needs nothing from here: while KDCube deletes it,
its registry reports no such conversation, so no slice is seeded, and a
conversation created again under the same id has a later deletion epoch, so
it never inherits a slice left from the deleted one.
"""

from __future__ import annotations

import dataclasses
import time
from typing import Any, Mapping

from connection_hub.delegated_credentials.slices.store import (
    SELECTION_CATEGORIES, SLICE_STATE_ACTIVE, ConversationSlice, SliceConflict,
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


async def _seeded(service: Any, store: Any, card: Any, application: str, conversation_id: str,
                  conversation_epoch: int | None = None) -> ConversationSlice:
    """The current slice; a picker write before the first turn seeds it after the registry check.

    A slice seeded under another deletion epoch belongs to an earlier
    conversation with this id and is seeded again.
    """
    from connection_hub.delegated_credentials.slices.turns import slice_epoch_differs

    current = await store.read(grantor_subject=card.grantor_subject, card_access_id=card.access_id,
                               conversation_id=conversation_id)
    if (current is not None and current.card_generation == card_generation_of(card)
            and not slice_epoch_differs(current, conversation_epoch)):
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
                               base_card_revision=card.card_revision, selection=seed_selection_of(card),
                               conversation_epoch=int(getattr(registered, "epoch", 0) or 0))
    return await store.write(seeded, expected_revision=current.slice_revision if current else 0)


async def update_conversation_selection(
    service: Any, user: Mapping[str, Any], *, application: str, agent_id: str, conversation_id: str,
    selection: Mapping[str, Any] | None = None, expected_slice_revision: int | None = None,
    wait_seconds: float = PICKER_WAIT_SECONDS, selected_capabilities: Mapping[str, Any] | None = None,
    conversation_epoch: int | None = None,
) -> dict[str, Any]:
    """The conversation picker: replace this conversation's guarded selection."""

    card, refused = await _card(service, user, application, agent_id)
    if refused:
        return refused
    store, _tokens = service._conversation_slice_parts()
    if store is None:
        return _refusal("conversation_slices_unavailable", retryable=True, status=503)
    try:
        if selection is None and selected_capabilities is None:
            return _refusal("conversation_selection_required", status=400)
        if selected_capabilities is not None:
            # The picker sends KDCube's capability selection; the Hub converts it.
            selection = await slice_selection_from_capabilities(service, card, selected_capabilities)
        wanted = _guarded(selection)
        current = await _seeded(service, store, card, application, conversation_id, conversation_epoch)
        if current.state != SLICE_STATE_ACTIVE:
            return _refusal("conversation_deleting")
        if expected_slice_revision is not None and int(expected_slice_revision) != current.slice_revision:
            return _refusal("conversation_slice_revision_conflict", retryable=True,
                            current_revision=current.slice_revision)
        candidate = dataclasses.replace(current, selection=wanted, change_id="")
        written = await store.write_when_open(candidate, expected_revision=current.slice_revision,
                                              wait_seconds=wait_seconds)
    except TurnRefused as exc:
        return exc.to_dict()
    except SlicePrepared:
        # The authority-update window: retry later.
        return _refusal("agent_authority_updating", retryable=True, status=409)
    except SliceConflict as exc:
        return _refusal("conversation_slice_revision_conflict", retryable=True, current_revision=exc.current_revision)
    except SliceStoreError as exc:
        return _refusal(exc.reason, retryable=exc.retryable)
    except Exception as exc:  # noqa: BLE001 - a refusal from the conversion (Control, catalog)
        reason = getattr(exc, "reason", "") or "conversation_selection_unavailable"
        return _refusal(str(reason), retryable=bool(getattr(exc, "retryable", True)))
    return {"ok": True, "slice_revision": written.slice_revision, "selection": dict(written.selection)}


__all__ = ["slice_selection_from_capabilities",
           "update_conversation_selection"]


async def slice_selection_from_capabilities(service: Any, card: Any, selected_capabilities: Mapping[str, Any]) -> dict:
    """The conversation picker's capability selection (KDCube's format) as guarded slice categories.

    The same conversion the Agent Card picker uses (``selected_standard_maps``):
    the live shared Control's authority bounds the visible choices; resource
    families come from the selection itself.
    """
    from connection_hub.delegated_credentials.agent_capability_policy import (
        AGENT_CAPABILITY_AUTHORITY_PROPERTY, AgentCapabilityPolicy,
    )
    from connection_hub.delegated_credentials.automation_access import record_from_card
    from connection_hub.delegated_credentials.descriptor_agent_selection import selected_standard_maps

    record = record_from_card(card) if not hasattr(card, "access_token") else card
    control = await service._descriptor_control_for_resident(record, grantor_subject=card.grantor_subject)
    authority = AgentCapabilityPolicy.from_property(dict(control.properties or {}).get(AGENT_CAPABILITY_AUTHORITY_PROPERTY))
    selected = AgentCapabilityPolicy.from_property(dict(selected_capabilities)).intersection(authority)
    active = await service._active_catalog()
    config = await service._catalog_config(active, owner_subject=card.grantor_subject)
    grants, operations, named = selected_standard_maps(resident=record, control=control, authority=authority,
                                                       selected=selected, config=config)
    out: dict[str, Any] = {
        "resource_grants": {key: sorted(values) for key, values in grants.items()},
        "resource_operations": {key: sorted(values) for key, values in operations.items()},
    }
    stored = named.to_stored()
    if isinstance(stored, Mapping):
        out["named_service_operations"] = dict(stored)
    families = selected.capabilities.get("resource_families")
    if families is not None:
        out["resource_families"] = sorted(families)
    return out

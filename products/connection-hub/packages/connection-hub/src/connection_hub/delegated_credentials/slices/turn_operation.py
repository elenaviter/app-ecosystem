# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W40: ``agent_capability.turn``, the Hub side of a resident agent's turn start.

The runtime names only the application, agent, conversation and turn; the
signed-in user is the host's binding. The Hub finds that user's Agent Card,
computes the seed itself from the Card's defaults (review B-1; never a
runtime-supplied selection), checks the conversation once in KDCube's
registry when it seeds, and answers the turn token. The token carries no
authority of its own: every call still presents the Card bearer.
"""

from __future__ import annotations

import time
from typing import Any, Mapping

from connection_hub.delegated_credentials.slices.guard import card_default_selection
from connection_hub.delegated_credentials.slices.store import SELECTION_CATEGORIES
from connection_hub.delegated_credentials.slices.turns import TurnRefused, begin_turn, card_generation_of


def seed_selection_of(card: Any) -> dict[str, Any]:
    """The Card's defaults as a slice seed; a legacy Card seeds from its own standard authority."""
    defaults = card_default_selection(card)
    if defaults is not None:
        return {key: value for key, value in defaults.items() if key in SELECTION_CATEGORIES}
    named = card.named_service_operations
    seed: dict[str, Any] = {
        "resource_grants": {key: list(values) for key, values in card.resource_grants.items()},
        "resource_operations": {key: list(values) for key, values in card.resource_operations.items()},
    }
    stored = None if named.is_unknown else named.to_stored()
    if isinstance(stored, Mapping):
        seed["named_service_operations"] = dict(stored)
    return seed


async def begin_agent_turn(service: Any, user: Mapping[str, Any], *, application: str, agent_id: str,
                           conversation_id: str, turn_id: str, turn_token: str = "",
                           now: int | None = None) -> dict[str, Any]:
    from connection_hub.delegated_credentials.cards.identity import (
        resident_client_id, stable_resident_access_id,
    )
    from connection_hub.delegated_credentials.automation_access import _subject_from_user

    grantor = _subject_from_user(user)
    if not grantor:
        return {"ok": False, "error": "delegated_access_requires_authenticated_user", "status": 401}
    registry = getattr(service, "_conversation_registry", None)
    store, tokens = service._conversation_slice_parts()
    if registry is None or store is None or tokens is None:
        return {"ok": False, "error": "conversation_slices_unavailable", "retryable": True, "status": 503}
    loaded = await service._load_record_any_state(
        stable_resident_access_id(grantor, resident_client_id(application, agent_id)), grantor_subject=grantor)
    if loaded is None or loaded[1] != "active":
        return {"ok": False, "error": "agent_capability_card_not_active", "retryable": True, "status": 409}
    from connection_hub.delegated_credentials.automation_access import card_authority_from_record

    card = card_authority_from_record(loaded[0])
    try:
        token, conversation = await begin_turn(
            store=store, registry=registry, tokens=tokens, card=card, application=application,
            conversation_id=conversation_id, turn_id=turn_id, seed_selection=seed_selection_of(card),
            card_generation=card_generation_of(card), now=int(time.time()) if now is None else now,
            existing_token=turn_token)
    except TurnRefused as exc:
        return exc.to_dict()
    return {"ok": True, "turn_token": token, "conversation_id": conversation.conversation_id,
            "slice_revision": conversation.slice_revision}


__all__ = ["begin_agent_turn", "seed_selection_of"]

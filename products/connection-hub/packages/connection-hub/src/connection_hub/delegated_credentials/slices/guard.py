# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""What a call may do in one conversation: slice AND Agent Card AND Control.

The guard first composes the Agent Card with its live Control as before
(``effective_card_authority``); this module then narrows that result by the
conversation's slice. A slice never widens: it only removes. A category the
slice carries narrows to it; the resource-family switch removes the user's own
connectors of a family the conversation turned off, and a
resource admitted through a family that stays on is governed by that switch,
not by the slice's exact resource list.
"""

from __future__ import annotations

import dataclasses
from fnmatch import fnmatchcase
from typing import Any, Mapping

from connection_hub.delegated_credentials.agent_capability_policy import AGENT_CAPABILITY_METADATA_PROPERTY
from connection_hub.delegated_credentials.cards.model import CardAuthority, NamedServiceSelection
from connection_hub.delegated_credentials.controls.effective import intersect_card_authority_selection
from connection_hub.delegated_credentials.slices.store import SLICE_STATE_ACTIVE, ConversationSlice


class SliceGuardRefused(RuntimeError):
    def __init__(self, reason: str, *, retryable: bool = False) -> None:
        super().__init__(reason)
        self.reason, self.retryable = reason, retryable


def _family_patterns(control: CardAuthority) -> dict[str, tuple[str, ...]]:
    metadata = dict(control.properties or {}).get(AGENT_CAPABILITY_METADATA_PROPERTY)
    entries = metadata.get("entries") if isinstance(metadata, Mapping) else None
    families = entries.get("resource_families") if isinstance(entries, Mapping) else None
    if not isinstance(families, Mapping):
        return {}
    out: dict[str, tuple[str, ...]] = {}
    for family_id, raw in families.items():
        patterns = raw.get("resource_patterns") if isinstance(raw, Mapping) else None
        if isinstance(patterns, (list, tuple)):
            out[str(family_id)] = tuple(str(pattern) for pattern in patterns)
    return out


def narrow_by_slice(effective: CardAuthority, control: CardAuthority, conversation: ConversationSlice) -> CardAuthority:
    """``effective`` (Agent Card AND Control) narrowed by this conversation's slice."""

    if conversation.state != SLICE_STATE_ACTIVE:
        raise SliceGuardRefused("conversation_deleting")
    if conversation.card_access_id != effective.access_id or conversation.grantor_subject != effective.grantor_subject:
        raise SliceGuardRefused("conversation_slice_card_mismatch")
    return narrow_by_selection(effective, control, conversation.selection)


def card_default_selection(card: CardAuthority) -> Mapping[str, Any] | None:
    """The Card's defaults as a selection, or None for a legacy Card."""
    from connection_hub.delegated_credentials.agent_capability_policy import (
        AGENT_CAPABILITY_DEFAULT_STANDARD_PROPERTY, AGENT_CAPABILITY_SELECTION_PROPERTY,
    )

    properties = dict(card.properties or {})
    standard = properties.get(AGENT_CAPABILITY_DEFAULT_STANDARD_PROPERTY)
    if not isinstance(standard, Mapping):
        return None
    selection = {key: standard[key] for key in ("resource_grants", "resource_operations", "named_service_operations")
                 if isinstance(standard.get(key), Mapping)}
    chosen = properties.get(AGENT_CAPABILITY_SELECTION_PROPERTY)
    capabilities = chosen.get("capabilities") if isinstance(chosen, Mapping) else None
    if isinstance(capabilities, Mapping) and isinstance(capabilities.get("resource_families"), (list, tuple)):
        selection["resource_families"] = list(capabilities["resource_families"])
    return selection


def narrow_by_selection(effective: CardAuthority, control: CardAuthority, selection: Mapping[str, Any]) -> CardAuthority:
    """``effective`` narrowed by a guarded selection (a slice's, or the Card's defaults)."""
    families = _family_patterns(control)
    allowed_families = set(selection.get("resource_families", families.keys()))

    def family_of(resource: str) -> str | None:
        for family_id, patterns in sorted(families.items()):
            if any(fnmatchcase(resource, pattern) for pattern in patterns):
                return family_id
        return None

    # The family switch: a family turned off in this conversation closes its resources.
    grants = {resource: claims for resource, claims in effective.resource_grants.items()
              if (family := family_of(resource)) is None or family in allowed_families}
    family_resources = {resource for resource in grants if family_of(resource) is not None}

    ceiling_grants = dict(grants)
    if "resource_grants" in selection:
        chosen = dict(selection["resource_grants"])
        ceiling_grants = {resource: (claims if resource in family_resources else tuple(chosen.get(resource, ())))
                          for resource, claims in grants.items()
                          if resource in family_resources or resource in chosen}
    ceiling_operations = {resource: tuple(effective.resource_operations.get(resource, ()))
                          for resource in ceiling_grants}
    if "resource_operations" in selection:
        chosen_ops = dict(selection["resource_operations"])
        ceiling_operations = {resource: (operations if resource in family_resources
                                         else tuple(chosen_ops.get(resource, ())))
                              for resource, operations in ceiling_operations.items()}
    named = effective.named_service_operations
    if "named_service_operations" in selection:
        named = NamedServiceSelection.from_stored(dict(selection["named_service_operations"]), present=True)
    ceiling = dataclasses.replace(effective, resource_grants=ceiling_grants, resource_operations=ceiling_operations,
                                  named_service_operations=named)
    narrowed = intersect_card_authority_selection(dataclasses.replace(effective, resource_grants=grants), ceiling)
    return narrowed


async def _verify(tokens: Any, token: str, **expected: Any) -> Any:
    """A ``TurnTokens`` sealer, or a ``TurnTokenSource`` (cached key, reread on a failed seal)."""
    import inspect

    verified = tokens.verify(token, **expected)
    return await verified if inspect.isawaitable(verified) else verified


async def effective_for_turn(*, effective: CardAuthority, control: CardAuthority, slices: Any, tokens: Any,
                             turn_token: str, now: int, registry: Any = None) -> CardAuthority:
    """The guard's entry: no token keeps the Card's authority (a call outside any conversation)."""

    if not str(turn_token or "").strip():
        # A call outside any conversation gets the Card's
        # defaults, not every grant it was ever given. A legacy Card has none.
        defaults = card_default_selection(effective)
        return effective if defaults is None else narrow_by_selection(effective, control, defaults)
    from connection_hub.delegated_credentials.slices.turns import card_generation_of

    generation = card_generation_of(effective)
    claims = await _verify(tokens, turn_token, card_access_id=effective.access_id, now=now, card_generation=generation)
    if claims.mode == "per_call":
        # A fixed-header runtime's token lives up to the turn cap; it admits a call
        # only while that turn is alive (state row in_progress for it AND the lock held).
        if registry is None:
            raise SliceGuardRefused("conversation_registry_unavailable", retryable=True)
        try:
            alive = await registry.turn_alive(owner_subject=effective.grantor_subject,
                                              conversation_id=claims.conversation_id, turn_id=claims.turn_id)
        except Exception:  # noqa: BLE001 - an unreadable registry refuses, retryably
            raise SliceGuardRefused("conversation_registry_unavailable", retryable=True) from None
        if not alive:
            raise SliceGuardRefused("turn_not_alive")
    scope = dict(grantor_subject=effective.grantor_subject, card_access_id=effective.access_id,
                 conversation_id=claims.conversation_id)
    conversation = await slices.read(**scope)
    if conversation is None:
        # Fail closed: a token names a conversation whose slice the token's turn seeded.
        raise SliceGuardRefused("conversation_slice_missing", retryable=True)
    if conversation.card_generation != generation:
        # An older generation's slice is never reused; the next turn reseeds it.
        raise SliceGuardRefused("conversation_slice_generation_stale", retryable=True)
    narrowed = narrow_by_slice(effective, control, conversation)
    # While a Card transaction is undecided, the
    # call is never refused for it; it gets the committed slice AND every
    # prepared one, so a consent (widening) applies only once committed and a
    # narrowing applies at once. A prepared deleting slice refuses.
    try:
        prepared = await slices.prepared_candidates(**scope)
    except Exception as exc:  # noqa: BLE001 - an unreadable marker fails closed
        raise SliceGuardRefused(getattr(exc, "reason", "conversation_slice_marker_unreadable"), retryable=True) from None
    for candidate in prepared:
        narrowed = narrow_by_slice(narrowed, control, candidate)
    return narrowed


__all__ = ["SliceGuardRefused", "card_default_selection", "effective_for_turn", "narrow_by_selection",
           "narrow_by_slice"]


# -- The request path (KDCube's delegated surface guard calls this) ------------


async def narrow_live_composition_for_turn(composition: Any, *, card_store: Any, turn_token: str,
                                           resolvers: Any = None, now: int | None = None) -> Any:
    """The live composition with its Effective Card narrowed by the conversation the token names.

    Every refusal is an authorization failure (``LiveGrantCardError``): the
    guard denies the call, never falls back to the Card's wider authority.
    """

    import time

    from connection_hub.delegated_credentials.live_grant import LiveGrantCardError
    from connection_hub.delegated_credentials.slices.store import BundleStorageConversationSliceStore
    from connection_hub.delegated_credentials.slices.turns import TurnRefused

    root = getattr(card_store, "root", None) or getattr(card_store, "_root", None)
    if not str(turn_token or "").strip():
        if composition.control_card is None or card_default_selection(composition.effective_card) is None:
            return composition
        effective = await effective_for_turn(effective=composition.effective_card, control=composition.control_card,
                                             slices=None, tokens=None, turn_token="", now=0)
        return dataclasses.replace(composition, effective_card=effective)
    tokens = getattr(resolvers, "turn_tokens", None)
    registry = getattr(resolvers, "conversation_registry", None)
    if tokens is None or root is None or composition.control_card is None:
        raise LiveGrantCardError("conversation_slices_unavailable")
    try:
        effective = await effective_for_turn(
            effective=composition.effective_card, control=composition.control_card,
            slices=BundleStorageConversationSliceStore(root), tokens=tokens, turn_token=turn_token,
            now=int(time.time()) if now is None else now, registry=registry)
    except (SliceGuardRefused, TurnRefused) as exc:
        raise LiveGrantCardError(exc.reason) from None
    return dataclasses.replace(composition, effective_card=effective)


__all__ += ["narrow_live_composition_for_turn"]

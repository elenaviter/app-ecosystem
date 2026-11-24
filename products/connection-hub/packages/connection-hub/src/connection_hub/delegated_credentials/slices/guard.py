# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W40: what a call may do in one conversation: slice AND Agent Card AND Control.

The guard first composes the Agent Card with its live Control as before
(``effective_card_authority``); this module then narrows that result by the
conversation's slice. A slice never widens: it only removes. A category the
slice carries narrows to it; the resource-family switch removes the user's own
connectors of a family the conversation turned off (review #464, nit 3), and a
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
    selection = conversation.selection
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


async def effective_for_turn(*, effective: CardAuthority, control: CardAuthority, slices: Any, tokens: Any,
                             turn_token: str, now: int) -> CardAuthority:
    """The guard's entry: no token keeps the Card's authority (a call outside any conversation)."""

    if not str(turn_token or "").strip():
        return effective
    claims = tokens.verify(turn_token, card_access_id=effective.access_id, now=now)
    scope = dict(grantor_subject=effective.grantor_subject, card_access_id=effective.access_id,
                 conversation_id=claims.conversation_id)
    conversation = await slices.read(**scope)
    if conversation is None:
        # Fail closed: a token names a conversation whose slice the token's turn seeded.
        raise SliceGuardRefused("conversation_slice_missing", retryable=True)
    narrowed = narrow_by_slice(effective, control, conversation)
    # Option c (design 2026-11-24): while a Card transaction is undecided, the
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


__all__ = ["SliceGuardRefused", "effective_for_turn", "narrow_by_slice"]


# -- The request path (KDCube's delegated surface guard calls this) ------------

_TURN_TOKENS: Any = None


def bind_turn_tokens(tokens: Any) -> None:
    """Bind the Hub's turn-token sealer for this process (from the Hub's own secret, at startup)."""
    global _TURN_TOKENS
    _TURN_TOKENS = tokens


async def narrow_live_composition_for_turn(composition: Any, *, card_store: Any, turn_token: str,
                                           now: int | None = None) -> Any:
    """The live composition with its Effective Card narrowed by the conversation the token names.

    Every refusal is an authorization failure (``LiveGrantCardError``): the
    guard denies the call, never falls back to the Card's wider authority.
    """

    import time

    from connection_hub.delegated_credentials.live_grant import LiveGrantCardError
    from connection_hub.delegated_credentials.slices.store import BundleStorageConversationSliceStore
    from connection_hub.delegated_credentials.slices.turns import TurnRefused

    if not str(turn_token or "").strip():
        return composition
    root = getattr(card_store, "root", None) or getattr(card_store, "_root", None)
    if _TURN_TOKENS is None or root is None or composition.control_card is None:
        raise LiveGrantCardError("conversation_slices_unavailable")
    try:
        effective = await effective_for_turn(
            effective=composition.effective_card, control=composition.control_card,
            slices=BundleStorageConversationSliceStore(root), tokens=_TURN_TOKENS, turn_token=turn_token,
            now=int(time.time()) if now is None else now)
    except (SliceGuardRefused, TurnRefused) as exc:
        raise LiveGrantCardError(exc.reason) from None
    return dataclasses.replace(composition, effective_card=effective)


__all__ += ["bind_turn_tokens", "narrow_live_composition_for_turn"]

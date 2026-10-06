# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Bounded current Control dependencies; no read-side Card persistence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from connection_hub.delegated_credentials.cards.model import CardAuthority
from connection_hub.delegated_credentials.controls.effective import (
    ControlCardMismatch, compose_card_authority_selection, effective_card_authority,
)
from connection_hub.delegated_credentials.controls.project_person_composition import (
    compose_with_project_held_control, project_held_control,
)


@dataclass(frozen=True)
class ControlHierarchy:
    effective_card: CardAuthority
    control_card: CardAuthority | None
    effective_control_card: CardAuthority | None
    dependencies: tuple[CardAuthority, ...]


def compose_resolved_control_hierarchy(
    card: CardAuthority, dependencies: tuple[CardAuthority, ...],
) -> ControlHierarchy:
    """Validate and compose an already current, ordered dependency vector."""

    chain = [card, *dependencies]
    if len(dependencies) > 32:
        raise ControlCardMismatch("control_card_chain_too_deep")
    seen = {(card.grantor_subject, card.access_id)}
    for child, parent in zip(chain, chain[1:]):
        coordinate = (parent.grantor_subject, parent.access_id)
        if coordinate in seen:
            raise ControlCardMismatch("control_card_chain_cycle")
        seen.add(coordinate)
        if project_held_control(child):
            compose_with_project_held_control(child, parent)
        else:
            effective_card_authority(child, parent)
    if chain[-1].control_card is not None:
        raise ControlCardMismatch("control_card_unresolvable")

    effective = chain[-1]
    ancestors: list[CardAuthority] = []
    effective_control = None
    for child in reversed(chain[:-1]):
        parent = effective
        if child is card:
            effective_control = parent
        effective = (compose_with_project_held_control(child, parent)
                     if project_held_control(child) else effective_card_authority(child, parent))
        for ancestor in ancestors:
            effective = compose_card_authority_selection(effective, ancestor, ceiling_only=True)
        ancestors.append(parent)
    return ControlHierarchy(effective, dependencies[0] if dependencies else None,
                            effective_control, dependencies)


async def compose_control_hierarchy(
    card: CardAuthority, *,
    load_control: Callable[..., Awaitable[CardAuthority | None]],
    max_depth: int = 32,
) -> ControlHierarchy:
    """Resolve exact current coordinates and retain transitive AND ceilings.

    Every edge is validated against the raw current Card, including special
    exact-identity cross-owner edges. Dependencies are re-read before return;
    any concurrent change refuses this attempt, never serves a stale snapshot.
    This is read validation, not an atomic business-transaction commit gate.
    """

    chain = [card]
    coordinates = [(card.grantor_subject, card.access_id)]
    seen = set(coordinates)
    observed_hashes: list[str] = []
    while chain[-1].control_card is not None:
        if len(chain) > max_depth:
            raise ControlCardMismatch("control_card_chain_too_deep")
        child = chain[-1]
        binding = child.control_card
        held = project_held_control(child)
        subject = held.grantor_subject if held else binding.holder_subject or child.grantor_subject
        coordinate = (subject, binding.control_id)
        if coordinate in seen:
            raise ControlCardMismatch("control_card_chain_cycle")
        try:
            parent = await load_control(binding.control_id, grantor_subject=subject)
        except Exception as exc:
            raise ControlCardMismatch("control_card_lookup_unavailable") from exc
        if parent is None:
            raise ControlCardMismatch("control_card_unresolvable")
        if parent.grantor_subject != subject or parent.access_id != binding.control_id:
            raise ControlCardMismatch("control_card_grantor_mismatch")
        # Validate the edge BEFORE consuming or following parent authority.
        if held:
            compose_with_project_held_control(child, parent)
        else:
            effective_card_authority(child, parent)
        chain.append(parent)
        observed_hashes.append(parent.content_hash())
        coordinates.append(coordinate)
        seen.add(coordinate)

    result = compose_resolved_control_hierarchy(card, tuple(chain[1:]))
    for (subject, access_id), observed_hash in zip(coordinates[1:], observed_hashes):
        try:
            current = await load_control(access_id, grantor_subject=subject)
        except Exception as exc:
            raise ControlCardMismatch("control_card_lookup_unavailable") from exc
        if current is None or current.content_hash() != observed_hash:
            raise ControlCardMismatch("control_card_dependency_changed")
    return result

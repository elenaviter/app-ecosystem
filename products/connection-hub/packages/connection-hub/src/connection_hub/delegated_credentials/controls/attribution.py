# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Request-local attribution for authority composed from two Cards."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from connection_hub.delegated_credentials.cards.model import (
    CONTROL_COMPOSITION_AND,
    CONTROL_COMPOSITION_OR,
    CardAuthority,
)

CARD_ROLE_CALLER = "caller"
CARD_ROLE_CONTROL = "control"
CALLER_ONLY_COMPOSITION = "caller_only"


@dataclass(frozen=True)
class ResolvedCardComposition:
    """The three authority views produced by one live Card resolution.

    Caller and Control authority stay request-local. Only ``effective_card`` is
    projected into the delegated credential used by downstream services.
    """

    caller_card: CardAuthority
    effective_card: CardAuthority
    control_card: CardAuthority | None = None

    @property
    def mode(self) -> str:
        if self.control_card is None:
            return CALLER_ONLY_COMPOSITION
        return self.control_card.composition_mode or CONTROL_COMPOSITION_AND


@dataclass(frozen=True)
class CardReference:
    """Bounded, non-secret coordinates for one authority participant."""

    role: str
    access_id: str
    card_revision: int
    label: str = ""
    issuer_ref: str = ""
    issuer_kind: str = ""
    issuer_label: str = ""

    def to_public_dict(self) -> dict[str, object]:
        return {
            key: value
            for key, value in {
                "role": self.role,
                "access_id": self.access_id,
                "card_revision": self.card_revision,
                "label": self.label,
                "issuer_ref": self.issuer_ref,
                "issuer_kind": self.issuer_kind,
                "issuer_label": self.issuer_label,
            }.items()
            if value not in ("", 0, None)
        }


@dataclass(frozen=True)
class CardBoundaryAttribution:
    """Which Card or Cards make an effective capability unavailable."""

    composition_mode: str
    caller_permits: bool
    effective_permits: bool
    control_permits: bool | None
    blocking_cards: tuple[CardReference, ...]
    reliable: bool = True

    @property
    def single_blocker(self) -> CardReference | None:
        if self.reliable and len(self.blocking_cards) == 1:
            return self.blocking_cards[0]
        return None

    @property
    def control_is_single_blocker(self) -> bool:
        blocker = self.single_blocker
        return blocker is not None and blocker.role == CARD_ROLE_CONTROL

    @property
    def caller_is_single_blocker(self) -> bool:
        blocker = self.single_blocker
        return blocker is not None and blocker.role == CARD_ROLE_CALLER

    def to_public_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "composition_mode": self.composition_mode,
            "caller_permits": self.caller_permits,
            "effective_permits": self.effective_permits,
            "blocking_cards": [card.to_public_dict() for card in self.blocking_cards],
        }
        if self.control_permits is not None:
            payload["control_permits"] = self.control_permits
        return payload


def _reference(
    role: str,
    authority: CardAuthority,
    *,
    composition: ResolvedCardComposition,
) -> CardReference:
    binding = composition.caller_card.control_card
    if role == CARD_ROLE_CONTROL and binding is not None:
        return CardReference(
            role=role,
            access_id=authority.access_id,
            card_revision=int(authority.card_revision or 0),
            label=authority.label,
            issuer_ref=authority.issuer_ref or binding.issuer_ref,
            issuer_kind=authority.issuer_kind or binding.issuer_kind,
            issuer_label=authority.issuer_label or binding.issuer_label,
        )
    return CardReference(
        role=role,
        access_id=authority.access_id,
        card_revision=int(authority.card_revision or 0),
        label=authority.label,
        issuer_ref=authority.issuer_ref,
        issuer_kind=authority.issuer_kind,
        issuer_label=authority.issuer_label,
    )


def attribute_card_boundary(
    composition: ResolvedCardComposition,
    *,
    permits: Callable[[CardAuthority], bool],
) -> CardBoundaryAttribution:
    """Attribute one effective denial without guessing across composition modes."""

    caller_permits = bool(permits(composition.caller_card))
    effective_permits = bool(permits(composition.effective_card))
    control = composition.control_card
    if control is None:
        reliable = caller_permits == effective_permits
        blockers = (
            (_reference(CARD_ROLE_CALLER, composition.caller_card, composition=composition),)
            if reliable and not effective_permits
            else ()
        )
        return CardBoundaryAttribution(
            composition_mode=CALLER_ONLY_COMPOSITION,
            caller_permits=caller_permits,
            effective_permits=effective_permits,
            control_permits=None,
            blocking_cards=blockers,
            reliable=reliable,
        )

    control_permits = bool(permits(control))
    mode = composition.mode
    expected_effective = (
        caller_permits or control_permits
        if mode == CONTROL_COMPOSITION_OR
        else caller_permits and control_permits
    )
    reliable = mode in {CONTROL_COMPOSITION_AND, CONTROL_COMPOSITION_OR} and (
        expected_effective == effective_permits
    )
    blockers: list[CardReference] = []
    if reliable and not effective_permits:
        if not caller_permits:
            blockers.append(
                _reference(
                    CARD_ROLE_CALLER,
                    composition.caller_card,
                    composition=composition,
                )
            )
        if not control_permits:
            blockers.append(
                _reference(CARD_ROLE_CONTROL, control, composition=composition)
            )
    return CardBoundaryAttribution(
        composition_mode=mode,
        caller_permits=caller_permits,
        effective_permits=effective_permits,
        control_permits=control_permits,
        blocking_cards=tuple(blockers),
        reliable=reliable,
    )


__all__ = [
    "CALLER_ONLY_COMPOSITION",
    "CARD_ROLE_CALLER",
    "CARD_ROLE_CONTROL",
    "CardBoundaryAttribution",
    "CardReference",
    "ResolvedCardComposition",
    "attribute_card_boundary",
]

# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W41 migration: from per-user descriptor Control copies to the one shared Control.

Design (applications journal, 2026-11-20, section 6): the deployment publishes
the shared Control; this migration then

1. lists every per-user copy and how it differs from the shared Control (in
   practice an administrator's own copy). Copies are listed, never carried
   over: an ordinary user's copy equals the descriptor ceiling, and carrying an
   administrator's override over would newly narrow every user;
2. rebinds every Agent Card still bound to its owner's copy to the shared
   Control, through the owner's own descriptor Card edit (validated, re-pinned,
   one transaction per Card);
3. revokes each copy no Card is bound to any more.

A dry run (``apply=False``) reads only and returns the report. The apply is
idempotent: a second run finds nothing to rebind or revoke. A Card the run
misses is rebound by its owner's next sync anyway. The report carries ids,
counts and field names only, never a credential.
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from typing import Any

from connection_hub.delegated_credentials.agent_capability_control import (
    AGENT_DESCRIPTOR_CARD_EDIT_PROVENANCE_KEY,
    AGENT_DESCRIPTOR_ISSUER_KIND,
)
from connection_hub.delegated_credentials.controls.descriptor_control import (
    descriptor_control_coordinates,
    is_application_authority_subject,
)
from connection_hub.delegated_credentials.legacy_binding_repair import _active_cards

_COMPARED = ("resource_grants", "resource_operations", "named_service_operations", "properties")


def _is_descriptor_control(card: Any) -> bool:
    return card.source == "control" and card.issuer_kind == AGENT_DESCRIPTOR_ISSUER_KIND


def _differs(copy: Any, shared: Any | None) -> list[str]:
    if shared is None:
        return ["shared_control_not_published"]
    return [name for name in _COMPARED if getattr(copy, name) != getattr(shared, name)]


async def migrate_descriptor_controls(host: Any, store: Any, *, apply: bool) -> dict[str, Any]:
    """Report (and with ``apply`` rebind and revoke) the per-user descriptor Control copies."""

    from connection_hub.delegated_credentials.automation_access import record_from_card

    counts: Counter = Counter()
    cards = await _active_cards(store, counts)
    shared = {card.issuer_ref: card for card in cards
              if _is_descriptor_control(card) and is_application_authority_subject(card.grantor_subject)}
    copies = [card for card in cards
              if _is_descriptor_control(card) and not is_application_authority_subject(card.grantor_subject)]
    bound: dict[str, list[Any]] = {}
    for card in cards:
        binding = card.control_card
        if (card.source == "agent" and binding is not None and binding.issuer_kind == AGENT_DESCRIPTOR_ISSUER_KIND
                and binding.holder_subject in ("", card.grantor_subject)):
            bound.setdefault(binding.control_id, []).append(card)

    report = []
    for copy in sorted(copies, key=lambda c: (c.issuer_ref, c.grantor_subject)):
        target = shared.get(copy.issuer_ref)
        row = {
            "agent": copy.issuer_ref,
            "owner": copy.grantor_subject,
            "control_id": copy.access_id,
            "administrator_edited": dict(copy.provenance or {}).get(AGENT_DESCRIPTOR_CARD_EDIT_PROVENANCE_KEY) is True,
            "differs_from_shared": _differs(copy, target),
            "bound_agent_cards": len(bound.get(copy.access_id, ())),
        }
        report.append(row)
        if not apply:
            continue
        if target is None:
            counts["skipped_shared_not_published"] += 1
            continue
        try:
            expected = descriptor_control_coordinates(copy.issuer_ref)
        except ValueError:
            counts["skipped_invalid_agent"] += 1
            continue
        if (target.grantor_subject, target.access_id) != (expected.holder_subject, expected.control_id):
            counts["skipped_shared_not_canonical"] += 1
            continue
        remaining = 0
        for card in bound.get(copy.access_id, ()):
            try:
                # The stored record, with its credential handles: the edit keeps them.
                loaded = await host._load_record_any_state(card.access_id, grantor_subject=card.grantor_subject)
                if loaded is None or loaded[1] != "active":
                    counts["rebind_skipped_not_active"] += 1
                    continue
                original = loaded[0]
                rebound = dataclasses.replace(original, card_revision=original.card_revision + 1,
                    control_card=dataclasses.replace(original.control_card, control_id=target.access_id,
                                                     holder_subject=target.grantor_subject,
                                                     control_revision=target.card_revision))
                await host._persist_descriptor_agent_edit(
                    original, rebound, control=record_from_card(target),
                    active_catalog=await host._active_catalog(), actor_subject=card.grantor_subject,
                    request_id=f"w41-migration-{card.access_id}")
                counts["agent_cards_rebound"] += 1
            except Exception as exc:  # noqa: BLE001 - counted; the owner's next sync rebinds it
                counts["rebind_error_" + type(exc).__name__] += 1
                remaining += 1
        if remaining:
            counts["copies_kept_still_bound"] += 1
            continue
        try:
            answer = await host.control_card_revoke({"user_id": copy.grantor_subject}, control_id=copy.access_id)
        except Exception as exc:  # noqa: BLE001
            counts["revoke_error_" + type(exc).__name__] += 1
            continue
        counts["copies_revoked" if answer.get("ok") is True else "revoke_refused"] += 1

    return {
        "ok": True,
        "applied": bool(apply),
        "shared_controls": len(shared),
        "per_user_copies": len(copies),
        "administrator_edited_copies": sum(1 for row in report if row["administrator_edited"]),
        "copies": report,
        "counts": dict(sorted(counts.items())),
    }


__all__ = ["migrate_descriptor_controls"]

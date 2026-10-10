# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter
"""P0, 10 Oct 2026: bind legacy project Cards the way a new Card is bound, once, at bundle load.

Live 11:07Z: every Control Card save was refused card_plan_update_scope_invalid. The person's
Control C was issued before W502 bound C under the project's Control P, so it has no
``control_card``. ``existing_card_selection_plan._target_identity`` requires that binding, and stays
strict. A My Card issued before the holder coordinate fails the same check. Live probe 11:2xZ: three
projects, each with its one P; all 7 person Controls unbound; 4 My Cards with an older holder.

For every project that has exactly one active root P, this binds each active unbound C under that P
through ``project_control_binding.bind_project_control``, the same fenced attach that creation,
redemption and the W502 repair use. A C bound under another P is left alone. It then refreshes each
active My Card's pointer to its C with ``ProjectIdentityLifecycle._repair_my_card_binding``, the same
repair ``ensure`` makes. Both write a new revision only when something is missing, so a second load
changes nothing. Only counts are logged. The Hub, not the project host, names P here: the one active
root application Control of that project. Zero or several roots bind nothing.
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from typing import Any

from connection_hub.delegated_credentials import project_control_binding
from connection_hub.delegated_credentials.cards.model import CARD_STATE_ACTIVE
from connection_hub.delegated_credentials.controls.project_person import (
    PROJECT_PERSON_CONTROL_ISSUER_KIND,
    ProjectPersonControlIdentity,
)
from connection_hub.delegated_credentials.controls.project_person_composition import PROJECT_CONTROL_ISSUER_KIND
from connection_hub.delegated_credentials.project_authorization import ProjectControlLocator
from connection_hub.delegated_credentials.project_identity_lifecycle import (
    PROJECT_PERSON_MY_CARD_ISSUER_KIND,
    ProjectIdentityLifecycle,
    ProjectPersonCardIdentity,
)

LOGGER = logging.getLogger(__name__)
PROJECT_PREFIX = "work:project:"


def _is_root_p(card: Any) -> bool:
    return (card.state == CARD_STATE_ACTIVE and card.issuer_kind == PROJECT_CONTROL_ISSUER_KIND
            and str(card.issuer_ref).startswith(PROJECT_PREFIX) and card.control_card is None)


async def _active_cards(store: Any) -> list[Any]:
    cards = []
    for subject_hash in await store.list_grantor_hashes():
        for access_id in await store.list_card_ids(subject_hash=subject_hash):
            found = await store.read_current_authority(subject_hash=subject_hash, access_id=access_id)
            if found is not None and found[1].state == CARD_STATE_ACTIVE:
                cards.append(found[1])
    return cards


async def repair_legacy_project_bindings(host: Any, store: Any) -> dict[str, int]:
    """Bind every legacy C under its project's one P and refresh every My pointer; counts only."""

    from connection_hub.delegated_credentials.automation_access import (
        card_authority_from_record,
        record_from_card,
    )

    counts: Counter = Counter()
    cards = await _active_cards(store)
    roots: dict[str, list[Any]] = defaultdict(list)
    for card in cards:
        if _is_root_p(card):
            roots[card.issuer_ref].append(card)
    for card in cards:
        if card.issuer_kind != PROJECT_PERSON_CONTROL_ISSUER_KIND or card.control_card is not None:
            continue
        identity = ProjectPersonControlIdentity.from_authority(card)
        project_roots = roots.get(identity.project_ref, [])
        if len(project_roots) != 1:
            counts["c_skipped_no_single_p"] += 1
            continue
        p = project_roots[0]
        outcome = await project_control_binding.bind_project_control(
            host, identity, ProjectControlLocator(control_id=p.access_id, holder_subject=p.grantor_subject))
        counts["c_" + str(outcome.get("outcome") or "refused")] += 1

    lifecycle = ProjectIdentityLifecycle(host=host, authority_from_record=card_authority_from_record,
                                         record_from_authority=record_from_card)
    for card in cards:
        if card.issuer_kind != PROJECT_PERSON_MY_CARD_ISSUER_KIND:
            continue
        mine = ProjectPersonCardIdentity.from_my_card(card)
        control_identity = ProjectPersonControlIdentity.build(project_ref=mine.project_ref,
                                                              target_subject=mine.person_subject)
        loaded = await host._load_record_any_state(control_identity.control_id,
                                                   grantor_subject=control_identity.project_subject)
        if loaded is None or loaded[1] != CARD_STATE_ACTIVE:
            counts["my_skipped_no_active_c"] += 1
            continue
        control = card_authority_from_record(loaded[0])
        if control.control_card is None:
            counts["my_skipped_unbound_c"] += 1
            continue
        my_loaded = await host._load_record_any_state(card.access_id, grantor_subject=card.grantor_subject)
        if my_loaded is None or my_loaded[1] != CARD_STATE_ACTIVE:
            counts["my_skipped_not_active"] += 1
            continue
        before = my_loaded[0]
        identity = ProjectPersonCardIdentity.build(project_ref=mine.project_ref, person_subject=mine.person_subject,
                                                   control_revision=control.card_revision)
        after = await lifecycle._repair_my_card_binding(  # noqa: SLF001 - the one repair ensure() makes
            before, card_authority_from_record(before), identity=identity, control=control)
        counts["my_repaired" if after is not before else "my_already_bound"] += 1
    LOGGER.info("[connection-hub] legacy project binding repair %s",
                " ".join(f"{key}={value}" for key, value in sorted(counts.items())) or "nothing_to_do")
    return dict(counts)


__all__ = ["repair_legacy_project_bindings"]

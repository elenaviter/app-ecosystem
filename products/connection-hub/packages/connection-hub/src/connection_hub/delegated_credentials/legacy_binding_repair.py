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

import json
import logging
import os
import pathlib
import time
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
# A durable "nothing left" marker beside the Card store (operator rule: no work that grows with the
# population on every load). Written only when a run left nothing outstanding; a later load reads this
# one file and no Card. Any refusal, error or skip leaves it unwritten, so the next load retries.
MARKER_NAME = "legacy-binding-repair.complete.json"
MARKER_SCHEMA = "connection-hub.legacy-binding-repair.v1"
DONE_KEYS = frozenset({"c_bound", "c_already_bound", "my_repaired", "my_already_bound"})


def _marker_path(store: Any) -> pathlib.Path:
    return pathlib.Path(store.root) / MARKER_NAME


def _complete(store: Any) -> bool:
    try:
        value = json.loads(_marker_path(store).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(value, dict) and value.get("schema") == MARKER_SCHEMA


def _write_marker(store: Any, counts: dict[str, int]) -> None:
    path = _marker_path(store)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps({"schema": MARKER_SCHEMA, "completed_at": int(time.time()),
                                     "counts": dict(sorted(counts.items()))}, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _is_root_p(card: Any) -> bool:
    return (card.state == CARD_STATE_ACTIVE and card.issuer_kind == PROJECT_CONTROL_ISSUER_KIND
            and str(card.issuer_ref).startswith(PROJECT_PREFIX) and card.control_card is None)


async def _active_cards(store: Any, counts: Counter) -> list[Any]:
    cards = []
    for subject_hash in await store.list_grantor_hashes():
        for access_id in await store.list_card_ids(subject_hash=subject_hash):
            try:
                found = await store.read_current_authority(subject_hash=subject_hash, access_id=access_id)
            except Exception as exc:  # noqa: BLE001 - an unreadable Card is counted, never repaired
                counts["read_error_" + type(exc).__name__] += 1
                continue
            if found is not None and found[1].state == CARD_STATE_ACTIVE:
                cards.append(found[1])
    return cards


async def repair_legacy_project_bindings(host: Any, store: Any) -> dict[str, int]:
    """Bind every legacy C under its project's one P and refresh every My pointer; counts only.

    Runs under LEGACY_BINDING_REPAIR, W578's switch for these two writes only. Each Card is repaired on
    its own: an error is counted by class and the next Card is still repaired.
    """

    from connection_hub.delegated_credentials.automation_access import LEGACY_BINDING_REPAIR

    if _complete(store):
        LOGGER.info("[connection-hub] legacy project binding repair already complete")
        return {"already_complete": 1}
    token = LEGACY_BINDING_REPAIR.set(True)
    try:
        counts = await _repair(host, store)
    finally:
        LEGACY_BINDING_REPAIR.reset(token)
    if set(counts) <= DONE_KEYS:
        _write_marker(store, counts)
    LOGGER.info("[connection-hub] legacy project binding repair %s",
                " ".join(f"{key}={value}" for key, value in sorted(counts.items())) or "nothing_to_do")
    return counts


async def _repair(host: Any, store: Any) -> dict[str, int]:
    from connection_hub.delegated_credentials.automation_access import (
        card_authority_from_record,
        record_from_card,
    )

    counts: Counter = Counter()
    cards = await _active_cards(store, counts)
    roots: dict[str, list[Any]] = defaultdict(list)
    for card in cards:
        if _is_root_p(card):
            roots[card.issuer_ref].append(card)
    for card in cards:
        if card.issuer_kind != PROJECT_PERSON_CONTROL_ISSUER_KIND or card.control_card is not None:
            continue
        try:
            identity = ProjectPersonControlIdentity.from_authority(card)
            project_roots = roots.get(identity.project_ref, [])
            if len(project_roots) != 1:
                counts["c_skipped_no_single_p"] += 1
                continue
            p = project_roots[0]
            outcome = await project_control_binding.bind_project_control(
                host, identity, ProjectControlLocator(control_id=p.access_id, holder_subject=p.grantor_subject))
        except Exception as exc:  # noqa: BLE001 - counted by class; the next Card is still repaired
            counts["c_error_" + type(exc).__name__] += 1
            continue
        counts["c_" + str(outcome.get("outcome") or "refused")] += 1
        if outcome.get("ok") is not True and isinstance(outcome.get("error"), str):
            counts["c_refused_" + outcome["error"]] += 1

    lifecycle = ProjectIdentityLifecycle(host=host, authority_from_record=card_authority_from_record,
                                         record_from_authority=record_from_card)
    for card in cards:
        if card.issuer_kind != PROJECT_PERSON_MY_CARD_ISSUER_KIND:
            continue
        try:
            counts[await _repair_my(host, lifecycle, card, card_authority_from_record, roots)] += 1
        except Exception as exc:  # noqa: BLE001 - counted by class; the next Card is still repaired
            counts["my_error_" + type(exc).__name__] += 1
    return dict(counts)


async def _repair_my(host: Any, lifecycle: ProjectIdentityLifecycle, card: Any, authority_of: Any,
                     roots: dict[str, list[Any]]) -> str:
    mine = ProjectPersonCardIdentity.from_my_card(card)
    # The same eligibility as C (codex-infra review): exactly one root P, and the C bound to that P.
    project_roots = roots.get(mine.project_ref, [])
    if len(project_roots) != 1:
        return "my_skipped_no_single_p"
    control_identity = ProjectPersonControlIdentity.build(project_ref=mine.project_ref,
                                                          target_subject=mine.person_subject)
    loaded = await host._load_record_any_state(control_identity.control_id,
                                               grantor_subject=control_identity.project_subject)
    if loaded is None or loaded[1] != CARD_STATE_ACTIVE:
        return "my_skipped_no_active_c"
    control = authority_of(loaded[0])
    if control.control_card is None:
        return "my_skipped_unbound_c"
    if (control.control_card.control_id != project_roots[0].access_id
            or control.control_card.issuer_ref != mine.project_ref):
        return "my_skipped_c_not_under_root_p"
    my_loaded = await host._load_record_any_state(card.access_id, grantor_subject=card.grantor_subject)
    if my_loaded is None or my_loaded[1] != CARD_STATE_ACTIVE:
        return "my_skipped_not_active"
    before = my_loaded[0]
    identity = ProjectPersonCardIdentity.build(project_ref=mine.project_ref, person_subject=mine.person_subject,
                                               control_revision=control.card_revision)
    after = await lifecycle._repair_my_card_binding(  # noqa: SLF001 - the one repair ensure() makes
        before, authority_of(before), identity=identity, control=control)
    return "my_repaired" if after is not before else "my_already_bound"


__all__ = ["repair_legacy_project_bindings"]

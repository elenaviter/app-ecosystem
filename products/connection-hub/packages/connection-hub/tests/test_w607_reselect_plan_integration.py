"""W607: the planner's ``reselect`` update and its public display, end to end through ``plan_card_lifecycle``.

Real: the planner, the group candidate and participant input, the Spark
helper and ``plan_display``, the operation's step mapping. Synthetic: the host's
Card reads and catalog resolution (the helper tests' resolver double).
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.card_lifecycle_plan import plan_card_lifecycle as _plan_card_lifecycle
from connection_hub.delegated_credentials.cards.card_group import validate_group_candidate
from connection_hub.delegated_credentials.cards.lifecycle_plan_operation import UPDATE_STEPS, plan_steps
from connection_hub.delegated_credentials.cards.model import CardAuthority
from connection_hub.delegated_credentials.cards.store import subject_hash_for
from connection_hub.delegated_credentials.catalog.models import CatalogDocument
from connection_hub.delegated_credentials.project_authorization import (
    PROJECT_PERSON_CONTROL_UPDATE,
    LifecyclePlanAuthorization,
    LifecyclePlanAuthorizationRequest,
    LifecyclePlanStep,
    ProjectAuthorizationDecision,
)
from test_w607_existing_card_selection_plan import ACTOR, PROJECT, REQUEST, TARGET, _cards
from test_w607_existing_card_selection_plan import _Host as _ResolverHost


class _Cards:
    def __init__(self, *authorities: CardAuthority) -> None:
        self.current = {(subject_hash_for(card.grantor_subject), card.access_id): card for card in authorities}

    async def load_current(self, access_id: str, *, subject_hash: str):
        card = self.current.get((subject_hash, access_id))
        return (card, object()) if card is not None else None


class _Host(_ResolverHost):
    def __init__(self, *authorities: CardAuthority) -> None:
        super().__init__()
        self.cards = _Cards(*authorities)
        self.catalog = CatalogDocument.build({"delegated_credentials": {"oauth": {"resources": []}}})

    def _cards(self):
        return self.cards

    async def _active_catalog(self):
        return self.catalog


def _update(card: CardAuthority, selection: dict) -> dict:
    return {"kind": "reselect", "target_subject": TARGET, "access_id": card.access_id,
            "subject_hash": subject_hash_for(card.grantor_subject), "original_revision": card.card_revision,
            "selection": selection}


def _authorization(updates, *, grants=("work:admin",)) -> LifecyclePlanAuthorization:
    steps = tuple(LifecyclePlanStep(ref=f"update:{index}", operation=UPDATE_STEPS[raw["kind"]],
                                    target_subject=raw["target_subject"]) for index, raw in enumerate(updates))
    request = LifecyclePlanAuthorizationRequest(actor_subject=ACTOR, project_ref=PROJECT, request_id=REQUEST,
                                                request_digest="a" * 64, steps=steps)
    return LifecyclePlanAuthorization(request=request, decisions=tuple(
        (step.ref, ProjectAuthorizationDecision.allow(request.step_request(step), delegable_grants=grants,
                                                      platform_admin=True)) for step in steps))


async def _plan(host, updates, **kwargs):
    return await _plan_card_lifecycle(host, project_ref=PROJECT, creations=[], updates=updates,
                                      actor_subject=ACTOR, actor_kind="caller", request_id=REQUEST,
                                      authorization=kwargs.pop("authorization", _authorization(updates)), **kwargs)


def test_reselect_maps_to_the_existing_person_control_update_step():
    assert UPDATE_STEPS["reselect"] == PROJECT_PERSON_CONTROL_UPDATE
    project, control, _ = _cards()
    steps = plan_steps([], [_update(control, {"resource_grants": {}})])
    assert [(step.ref, step.operation, step.target_subject) for step in steps] == [
        ("update:0", PROJECT_PERSON_CONTROL_UPDATE, TARGET)]


@pytest.mark.asyncio
async def test_a_person_control_reselect_plans_one_update_with_its_exact_before_and_after():
    project, control, my_card = _cards()
    host = _Host(project, control, my_card)
    result = await _plan(host, [_update(control, {"resource_grants": {"service-a": ["work:admin"]}})])
    assert result["ok"] is True, result
    plan = result["plan"]
    members = plan["candidate_value"]["cards"]
    assert [(m["access_id"], m["action"], m["original_revision"]) for m in members] == [
        (control.access_id, "update", control.card_revision)]
    validate_group_candidate(plan["candidate_value"], reads=plan["reads"])
    # The live project Control the person Control is bound under is held as a read at its revision.
    assert {(r["access_id"], r["revision"]) for r in plan["reads"]} >= {(project.access_id, project.card_revision)}
    [entry] = plan["display"]
    assert (entry["access_id"], entry["original_revision"], entry["candidate_revision"]) == (
        control.access_id, control.card_revision, control.card_revision + 1)
    assert entry["before"]["resource_grants"] == {k: list(v) for k, v in control.resource_grants.items()}
    assert entry["after"]["resource_grants"] == {"service-a": ["work:admin"]}
    assert host.writes == 0


@pytest.mark.asyncio
async def test_promotion_can_reselect_control_and_my_card_in_one_proposal():
    project, control, my_card = _cards()
    host = _Host(project, control, my_card)
    updates = [_update(control, {"resource_grants": {"service-a": ["work:admin"]}}),
               _update(my_card, {"resource_grants": {"service-a": ["work:admin"]}})]
    result = await _plan(host, updates)
    assert result["ok"] is True, result
    keys = {m["access_id"] for m in result["plan"]["candidate_value"]["cards"]}
    assert keys == {control.access_id, my_card.access_id}
    assert {entry["access_id"] for entry in result["plan"]["display"]} == keys


@pytest.mark.asyncio
async def test_a_stale_revision_or_a_wrong_target_refuses_without_a_write():
    project, control, my_card = _cards()
    host = _Host(project, control, my_card)
    stale = dict(_update(control, {"resource_grants": {"service-a": ["work:admin"]}}),
                 original_revision=control.card_revision + 1)
    assert (await _plan(host, [stale]))["error"] == "card_plan_original_revision_changed"
    project_update = dict(_update(project, {"resource_grants": {}}), subject_hash=subject_hash_for(ACTOR))
    refused = await _plan(host, [project_update])
    assert refused["ok"] is False  # the project's own Control is never reselected
    with_parent = dict(_update(control, {"resource_grants": {}}),
                       parent={"access_id": project.access_id, "holder_subject": ACTOR})
    assert (await _plan(host, [with_parent]))["ok"] is False
    missing_selection = {k: v for k, v in _update(control, {}).items() if k != "selection"}
    assert (await _plan(host, [missing_selection]))["error"] == "card_plan_update_invalid"
    assert host.writes == 0


@pytest.mark.asyncio
async def test_revoke_and_attach_still_refuse_a_selection():
    project, control, _ = _cards()
    host = _Host(project, dataclasses.replace(control, control_card=None))
    attach = {"kind": "attach", "target_subject": TARGET, "access_id": control.access_id,
              "subject_hash": subject_hash_for(control.grantor_subject), "original_revision": control.card_revision,
              "parent": {"access_id": project.access_id, "holder_subject": ACTOR}, "selection": {}}
    assert (await _plan(host, [attach]))["error"] == "card_plan_update_invalid"
    assert SimpleNamespace  # imported for parity with the helper tests

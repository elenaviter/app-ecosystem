# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W813: a deleted project's own Control P is retired by the host's project.delete decision.

Operator, 2026-10-13: "i have to be able to delete the project. and the warning must be shown before that."
The project's Cards end in fenced, signed chunks of at most eight members (the group cap): P first, as the
live boundary, then its agents detach and its people and invitations are revoked under the same attempt. The
children's kinds are the existing ones; they still plan against a P this retirement has already revoked.
"""

from __future__ import annotations

import dataclasses

import pytest

from connection_hub.delegated_credentials.card_lifecycle_plan import PROJECT_RETIREMENT_PROVENANCE
from connection_hub.delegated_credentials.cards.card_group import validate_group_candidate
from connection_hub.delegated_credentials.cards.lifecycle_plan_operation import UPDATE_STEPS
from connection_hub.delegated_credentials.cards.model import CARD_STATE_ACTIVE, CARD_STATE_REVOKED, CardAuthority
from connection_hub.delegated_credentials.cards.service import replace_state
from connection_hub.delegated_credentials.cards.store import subject_hash_for
from connection_hub.delegated_credentials.project_authorization import (
    PROJECT_CONTROL_UPDATE,
    PROJECT_DELETE,
    ProjectControlLocator,
)
from connection_hub.delegated_credentials.project_identity_lifecycle import new_project_person_my_card
from test_w578_card_lifecycle_plan import (
    CREATOR, PROJECT, REQUEST, _authorization, _c, _Host, _p, plan_card_lifecycle,
)


def _retire(p: CardAuthority, **changes) -> dict:
    value = {"kind": "retire_project_control", "target_subject": PROJECT, "access_id": p.access_id,
             "subject_hash": subject_hash_for(p.grantor_subject), "original_revision": p.card_revision}
    value.update(changes)
    return value


def _locator(p: CardAuthority) -> ProjectControlLocator:
    return ProjectControlLocator(control_id=p.access_id, holder_subject=p.grantor_subject)


async def _plan(host, updates, *, locator=None, **auth):
    authorization = _authorization([], updates, locator_overrides={
        f"update:{index}": locator for index in range(len(updates))}, **auth)
    return await plan_card_lifecycle(host, project_ref=PROJECT, creations=[], updates=updates,
                                     actor_subject=CREATOR, actor_kind="caller", request_id=REQUEST,
                                     authorization=authorization)


def test_p_is_retired_only_by_the_project_delete_operation():
    assert UPDATE_STEPS["retire_project_control"] == PROJECT_DELETE == "project.delete"
    assert UPDATE_STEPS["reselect_project_control"] == PROJECT_CONTROL_UPDATE != PROJECT_DELETE


@pytest.mark.asyncio
async def test_p_is_revoked_at_its_exact_revision_with_only_its_state_and_an_audit_changing():
    p = _p()
    host = _Host(p)
    result = await _plan(host, [_retire(p)], locator=_locator(p))
    assert result["ok"] is True, result
    [member] = result["plan"]["candidate_value"]["cards"]
    assert member["access_id"] == p.access_id and member["action"] == "revoke"
    assert member["original_revision"] == p.card_revision
    after = CardAuthority.from_mapping(member["candidate"])
    assert after.state == CARD_STATE_REVOKED and after.card_revision == p.card_revision + 1
    audit = member["candidate"]["provenance"][PROJECT_RETIREMENT_PROVENANCE]
    assert audit["project_ref"] == PROJECT and audit["actor_subject"] == CREATOR and audit["request_id"] == REQUEST
    for field in ("resource_grants", "resource_operations", "named_service_operations", "control_card",
                  "grantor_subject", "issuer_kind", "issuer_ref"):
        assert getattr(after, field) == getattr(p, field)
    validate_group_candidate(result["plan"]["candidate_value"], reads=result["plan"]["reads"])
    assert host.writes == 0


@pytest.mark.asyncio
async def test_a_stale_revision_or_read_current_is_refused():
    p = _p()
    host = _Host(p)
    assert (await _plan(host, [_retire(p, original_revision=p.card_revision + 1)], locator=_locator(p)))["error"] \
        == "card_plan_original_revision_changed"
    assert (await _plan(host, [_retire(p, original_revision=0)], locator=_locator(p)))["error"] \
        == "card_plan_revision_invalid"


@pytest.mark.asyncio
async def test_p_must_be_the_one_the_hosts_link_names():
    p = _p()
    host = _Host(p)
    missing = await _plan(host, [_retire(p)], locator=None)
    assert missing["error"] == "project_control_locator_missing"
    wrong = await _plan(host, [_retire(p)], locator=ProjectControlLocator(control_id=p.access_id,
                                                                           holder_subject="someone-else"))
    assert wrong["error"] == "project_control_locator_mismatch"


@pytest.mark.asyncio
async def test_a_person_control_is_never_retired_as_p():
    p = _p()
    c = _c(p)
    host = _Host(p, c)
    result = await _plan(host, [_retire(c)], locator=_locator(p))
    assert result["ok"] is False and result["error"] in {"project_control_not_exact", "project_control_not_root"}


@pytest.mark.asyncio
async def test_the_target_must_be_the_project_itself():
    p = _p()
    host = _Host(p)
    result = await _plan(host, [_retire(p, target_subject="person-1")], locator=_locator(p))
    assert result == {"ok": False, "error": "card_plan_update_invalid", "status": 400}


@pytest.mark.asyncio
async def test_a_replay_after_publish_is_already_applied_but_only_for_this_projects_retirement():
    p = _p()
    revoked = replace_state(p, CARD_STATE_REVOKED)
    stamped = dataclasses.replace(revoked, provenance={
        **dict(revoked.provenance or {}),
        PROJECT_RETIREMENT_PROVENANCE: {"schema": "connection-hub.project-retirement.v1", "project_ref": PROJECT,
                                        "actor_subject": CREATOR, "request_id": REQUEST, "at": 1}})
    replay = await _plan(_Host(stamped), [_retire(stamped)], locator=_locator(stamped))
    assert replay == {"ok": False, "error": "card_plan_already_applied", "status": 409}
    # A P revoked any other way is not this retirement's success.
    other = await _plan(_Host(revoked), [_retire(revoked)], locator=_locator(revoked))
    assert other["ok"] is False and other["error"] == "card_plan_revoke_not_active"
    # Nor is a P another request revoked (a different attempt or chunk id).
    foreign = dataclasses.replace(stamped, provenance={
        **dict(stamped.provenance or {}),
        PROJECT_RETIREMENT_PROVENANCE: {**stamped.provenance[PROJECT_RETIREMENT_PROVENANCE], "request_id": "other"}})
    other_request = await _plan(_Host(foreign), [_retire(foreign)], locator=_locator(foreign))
    assert other_request["ok"] is False and other_request["error"] == "card_plan_revoke_not_active"


@pytest.mark.asyncio
async def test_people_are_still_removed_after_p_was_retired():
    """P first is the live boundary; the same attempt then ends the people under the revoked P."""
    p = _p()
    c = _c(p)
    my = new_project_person_my_card(control_card=c, now=100)
    revoked_p = replace_state(p, CARD_STATE_REVOKED)
    host = _Host(revoked_p, c, my)
    updates = [
        {"kind": "remove_person", "target_subject": "person-1", "access_id": card.access_id,
         "subject_hash": subject_hash_for(card.grantor_subject), "original_revision": card.card_revision}
        for card in (c, my)
    ]
    result = await plan_card_lifecycle(host, project_ref=PROJECT, creations=[], updates=updates,
                                       actor_subject=CREATOR, actor_kind="caller", request_id=REQUEST,
                                       authorization=_authorization([], updates))
    assert result["ok"] is True, result
    assert {member["access_id"] for member in result["plan"]["candidate_value"]["cards"]} == {c.access_id,
                                                                                              my.access_id}
    assert all(CardAuthority.from_mapping(member["candidate"]).state == CARD_STATE_REVOKED
               for member in result["plan"]["candidate_value"]["cards"])
    assert c.state == my.state == CARD_STATE_ACTIVE


def test_a_ninth_step_is_refused_by_the_cap():
    """The eight-step / eight-member cap stands: a project retires in chunks, never one oversized plan."""
    from connection_hub.delegated_credentials.project_authorization import ProjectAuthorizationError

    p = _p()
    with pytest.raises(ProjectAuthorizationError) as caught:
        _authorization([], [_retire(p)] * 9)
    assert caught.value.reason == "lifecycle_plan_steps_invalid"

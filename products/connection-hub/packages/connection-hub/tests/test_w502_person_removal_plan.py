# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W502: an active person leaves a project through ONE generic decision.

Today's direct removal revokes the person's My Card (the identity edge) and
then their Control by a second write. The ``remove_person`` PLAN step plans
both as revoke members of one group, so staging and the one decision end them
together or not at all. Only the state moves: no selection or preference is
rewritten. The planner itself writes nothing.
"""

from __future__ import annotations

import dataclasses
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from connection_hub.delegated_credentials.card_lifecycle_plan import CardLifecyclePlanRefused
from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.card_group import validate_group_candidate
from connection_hub.delegated_credentials.cards.lifecycle_plan_operation import UPDATE_STEPS
from connection_hub.delegated_credentials.cards.model import CARD_STATE_ACTIVE, CARD_STATE_REVOKED, CardAuthority
from connection_hub.delegated_credentials.cards.service import DelegatedCardService, replace_state
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore, subject_hash_for
from connection_hub.delegated_credentials.controls.model import new_credentialless_card
from connection_hub.delegated_credentials.controls.project_invitation import (
    PROJECT_INVITATION_CONTROL_ISSUER_KIND,
    ProjectInvitationControlIdentity,
    bind_project_invitation_control,
)
from connection_hub.delegated_credentials.controls.project_person import PROJECT_PERSON_CONTROL_AUDIT_PROVENANCE
from connection_hub.delegated_credentials.project_authorization import PROJECT_PERSON_CONTROL_REVOKE
from connection_hub.delegated_credentials.project_identity_lifecycle import new_project_person_my_card
from test_card_service import _Cache
from test_card_transaction_store import Decisions
from test_w578_card_lifecycle_plan import (
    CREATOR, PROJECT, REQUEST, TARGET, _authorization, _c, _Host, _p, plan_card_lifecycle,
)


OTHER = "https://other.example.test/mcp"


def _person():
    p = _p()
    c = _c(p)
    my = new_project_person_my_card(control_card=c, now=100)
    # The person's own choices, which removal must keep exactly as stored.
    my = dataclasses.replace(my, resource_grants={OTHER: ("other:read",)},
                             resource_operations={OTHER: ("search",)}, account_scope={OTHER: {"accounts": ["a-1"]}})
    return p, c, my


def _other_persons_my(p):
    from connection_hub.delegated_credentials.controls.project_person import ProjectPersonControlIdentity
    from connection_hub.delegated_credentials.card_lifecycle_plan import build_project_person_control
    other_c = build_project_person_control(
        identity=ProjectPersonControlIdentity.build(project_ref=PROJECT, target_subject="person-2"),
        catalog_version="catalog-v1", actor_subject=CREATOR, request_id=REQUEST, parent=p, now=100)
    return new_project_person_my_card(control_card=other_c, now=100)


def _remove(card: CardAuthority, *, target: str = TARGET, **changes) -> dict:
    value = {"kind": "remove_person", "target_subject": target, "access_id": card.access_id,
             "subject_hash": subject_hash_for(card.grantor_subject), "original_revision": card.card_revision}
    value.update(changes)
    return value


async def _plan(host, updates):
    return await plan_card_lifecycle(host, project_ref=PROJECT, creations=[], updates=updates,
                                     actor_subject=CREATOR, actor_kind="caller", request_id=REQUEST)


def test_the_step_is_the_person_control_revoke_and_invitation_revoke_is_unchanged():
    assert UPDATE_STEPS["remove_person"] == PROJECT_PERSON_CONTROL_REVOKE
    assert UPDATE_STEPS["revoke"] != PROJECT_PERSON_CONTROL_REVOKE


@pytest.mark.asyncio
async def test_a_person_s_control_and_my_are_planned_as_one_group_and_only_their_state_moves():
    p, c, my = _person()
    host = _Host(p, c, my)
    result = await _plan(host, [_remove(c), _remove(my)])
    assert result["ok"] is True, result
    members = {member["access_id"]: member for member in result["plan"]["candidate_value"]["cards"]}
    assert set(members) == {c.access_id, my.access_id}
    for card in (c, my):
        member = members[card.access_id]
        assert member["action"] == "revoke" and member["original_revision"] == card.card_revision
        assert member["original_absent"] is False
        after = CardAuthority.from_mapping(member["candidate"])
        assert after.state == CARD_STATE_REVOKED and after.card_revision == card.card_revision + 1
    # My keeps every selection and preference; only state and revision move.
    my_after = members[my.access_id]["candidate"]
    assert my_after["resource_operations"] == {OTHER: ["search"]}  # the fixture really holds choices
    assert my_after == replace_state(my, CARD_STATE_REVOKED).to_dict()
    # The Control records the same "revoked" audit the direct removal writes.
    audit = members[c.access_id]["candidate"]["provenance"][PROJECT_PERSON_CONTROL_AUDIT_PROVENANCE]
    assert audit["action"] == "revoked" and audit["actor_subject"] == CREATOR and audit["request_id"] == REQUEST
    control_after = CardAuthority.from_mapping(members[c.access_id]["candidate"])
    for field in ("resource_grants", "resource_operations", "named_service_operations", "account_scope",
                  "control_card", "grantor_subject"):
        assert getattr(control_after, field) == getattr(c, field)
    validate_group_candidate(result["plan"]["candidate_value"], reads=result["plan"]["reads"])
    assert host.writes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("only", ["control", "my"])
async def test_removing_one_card_while_the_other_is_active_refuses(only):
    p, c, my = _person()
    host = _Host(p, c, my)
    result = await _plan(host, [_remove(c if only == "control" else my)])
    assert result == {"ok": False, "error": "card_plan_remove_person_incomplete", "status": 409}


@pytest.mark.asyncio
async def test_an_already_revoked_partner_is_not_a_member():
    p, c, my = _person()
    host = _Host(p, c, replace_state(my, CARD_STATE_REVOKED))
    result = await _plan(host, [_remove(c)])
    assert result["ok"] is True, result
    assert [member["access_id"] for member in result["plan"]["candidate_value"]["cards"]] == [c.access_id]
    # The revoked partner is held as a present read at its exact revision.
    assert {"subject_hash": subject_hash_for(my.grantor_subject), "access_id": my.access_id,
            "revision": my.card_revision + 1} in result["plan"]["reads"]


@pytest.mark.asyncio
async def test_a_revoked_card_another_person_an_invitation_or_a_stale_revision_refuses():
    p, c, my = _person()
    invitation = ProjectInvitationControlIdentity.build(
        project_ref=PROJECT, invitation_ref="invite-1", target_email="person@example.test")
    pending = bind_project_invitation_control(new_credentialless_card(
        grantor_subject=invitation.project_subject, catalog_version="catalog-v1",
        control_id=invitation.control_id, issuer_ref=invitation.invitation_ref,
        issuer_kind=PROJECT_INVITATION_CONTROL_ISSUER_KIND, now=100), identity=invitation)
    revoked_c = replace_state(c, CARD_STATE_REVOKED)
    cases = [
        (_Host(p, revoked_c, my), [_remove(revoked_c), _remove(my)], "card_plan_revoke_not_active"),
        (_Host(p, c, my), [_remove(c, target="person-2"), _remove(my, target="person-2")],
         "card_plan_update_scope_invalid"),
        (_Host(p, pending), [_remove(pending, target=invitation.invitation_ref)], "card_plan_update_scope_invalid"),
        (_Host(p, c, my), [_remove(c, original_revision=c.card_revision + 1), _remove(my)],
         "card_plan_original_revision_changed"),
        (_Host(p, c, my), [_remove(c, parent={"ref": "p"}), _remove(my)], "card_plan_update_invalid"),
        (_Host(p, c, my, _other_persons_my(p)), [_remove(c), _remove(_other_persons_my(p))],
         "card_plan_update_scope_invalid"),
        (_Host(p, c, my), [_remove(c), _remove(c)], "card_plan_update_invalid"),
    ]
    for host, updates, error in cases:
        result = await _plan(host, updates)
        assert result["ok"] is False and result["error"] == error, (error, result)
        assert host.writes == 0


def _control_of(person: str, project: str, parent):
    from connection_hub.delegated_credentials.controls.project_person import ProjectPersonControlIdentity
    from connection_hub.delegated_credentials.card_lifecycle_plan import build_project_person_control
    return build_project_person_control(
        identity=ProjectPersonControlIdentity.build(project_ref=project, target_subject=person),
        catalog_version="catalog-v1", actor_subject=CREATOR, request_id=REQUEST, parent=parent, now=100)


@pytest.mark.asyncio
async def test_a_removal_of_one_person_cannot_name_another_persons_or_projects_card_or_a_non_person_card():
    """Main 15:46: under A's removal decision, only A's own person Cards in this project may be revoked."""
    p, c, my = _person()
    b_control = _control_of("person-2", PROJECT, p)
    other_project_control = _control_of(TARGET, "work:project:elsewhere", None)
    cases = {
        "another person's Control": (_Host(p, c, my, b_control), [_remove(b_control), _remove(my)]),
        "A's Control in another project": (_Host(p, c, my, other_project_control),
                                           [_remove(other_project_control), _remove(my)]),
        "the project Control (not a person Card)": (_Host(p, c, my), [_remove(p), _remove(c), _remove(my)]),
    }
    for name, (host, updates) in cases.items():
        result = await _plan(host, updates)
        assert result == {"ok": False, "error": "card_plan_update_scope_invalid", "status": 403}, (name, result)
        assert host.writes == 0


@pytest.mark.asyncio
async def test_the_step_needs_the_hosts_person_revoke_decision_for_that_person():
    p, c, my = _person()
    host = _Host(p, c, my)
    updates = [_remove(c), _remove(my)]
    forged = _authorization([], updates, targets={"update:0": "person-2"})
    result = await plan_card_lifecycle(host, authorization=forged, project_ref=PROJECT, creations=[],
                                       updates=updates, actor_subject=CREATOR, actor_kind="caller",
                                       request_id=REQUEST)
    assert result["ok"] is False and result["status"] == 403, result


@asynccontextmanager
async def _mutation_lock(**kwargs):
    yield


class _Reservations:
    def __init__(self):
        self.held = []

    async def reserve(self, **kwargs):
        self.held.append(kwargs)

    async def release(self, transaction_id, *, intent_digest):
        self.held = [item for item in self.held if item["transaction_id"] != transaction_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["committed", "aborted"])
async def test_one_decision_ends_both_cards_together_or_neither(tmp_path, decision):
    p, c, my = _person()
    result = await _plan(_Host(p, c, my), [_remove(c), _remove(my)])
    assert result["ok"] is True, result
    plan = result["plan"]
    store = BundleStorageDelegatedCardStore(tmp_path)
    decisions = Decisions()
    tx.bind_transaction_decisions(store, decisions)
    reservations = _Reservations()
    tx.bind_catalog_reservations(store, reservations)
    service = DelegatedCardService(store=store, cache=_Cache(), mutation_lock=_mutation_lock)
    for card in (p, c, my):
        await service.commit(card, subject_hash=subject_hash_for(card.grantor_subject), expected_revision=0, now=100)
    originals = {card.access_id: card for card in (c, my)}
    members = [(member["subject_hash"], originals[member["access_id"]],
                CardAuthority.from_mapping(member["candidate"]), member["action"])
               for member in plan["candidate_value"]["cards"]]
    group_id, intent = "e" * 64, "f" * 64
    staged = await service.stage_group_transaction(
        transaction_id=group_id, intent_digest=intent, participant="project", members=members,
        now=datetime.fromtimestamp(100, timezone.utc), reads=plan["reads"], catalog=plan["catalog_digest"])
    assert staged["staged"] is True, staged
    decisions.recorded[group_id] = decision
    decided = await service.decide_group_transaction(transaction_id=group_id, intent_digest=intent,
                                                     decision=decision, now=100)
    assert decided["state"] == decision
    states = set()
    for card in (c, my):
        stored = await store.read_current_authority(subject_hash=subject_hash_for(card.grantor_subject),
                                                    access_id=card.access_id)
        states.add(stored[1].state)
    assert states == ({CARD_STATE_REVOKED} if decision == "committed" else {CARD_STATE_ACTIVE})
    assert not reservations.held

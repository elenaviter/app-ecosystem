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


# W502, operator 7 Oct 2026: a removed person invited again is "newly invited" like anyone, with
# fresh Cards. Their C and My ids are fixed, so the new Cards are created over the revoked ones,
# freshly built, at the next revision; nothing of the revoked Cards is carried over.

def _join_creations(p):
    from test_w578_card_lifecycle_plan import _c_request, _my_request
    return [_c_request({"access_id": p.access_id, "holder_subject": CREATOR}), _my_request()]


async def _rejoin(host, p):
    return await plan_card_lifecycle(host, project_ref=PROJECT, creations=_join_creations(p),
                                     actor_subject=CREATOR, actor_kind="caller", request_id=REQUEST)


@pytest.mark.asyncio
async def test_a_removed_person_newly_invited_gets_fresh_cards_at_the_next_revision():
    p, c, my = _person()
    revoked_c, revoked_my = replace_state(c, CARD_STATE_REVOKED), replace_state(my, CARD_STATE_REVOKED)
    result = await _rejoin(_Host(p, revoked_c, revoked_my), p)
    assert result["ok"] is True, result
    members = {member["access_id"]: member for member in result["plan"]["candidate_value"]["cards"]}
    assert set(members) == {c.access_id, my.access_id}
    first_join = await _rejoin(_Host(p), p)  # what a first join builds, for comparison
    fresh = {member["access_id"]: member["candidate"] for member in first_join["plan"]["candidate_value"]["cards"]}
    for revoked in (revoked_c, revoked_my):
        member = members[revoked.access_id]
        assert member["action"] == "recreate" and member["original_absent"] is False
        assert member["original_revision"] == revoked.card_revision
        after = CardAuthority.from_mapping(member["candidate"])
        assert after.state == CARD_STATE_ACTIVE and after.card_revision == revoked.card_revision + 1
    # Nothing of the person's earlier choices comes back: My is the first-join My.
    my_after = members[my.access_id]["candidate"]
    assert my_after["account_scope"] == fresh[my.access_id]["account_scope"] != my.to_dict()["account_scope"]
    assert my_after["resource_grants"] == fresh[my.access_id]["resource_grants"]
    # My binds the recreated Control at its new revision.
    assert my_after["control_card"]["control_revision"] == revoked_c.card_revision + 1
    audit = members[c.access_id]["candidate"]["provenance"][PROJECT_PERSON_CONTROL_AUDIT_PROVENANCE]
    assert audit["action"] == "created" and audit["after_revision"] == revoked_c.card_revision + 1
    validate_group_candidate(result["plan"]["candidate_value"], reads=result["plan"]["reads"])


@pytest.mark.asyncio
async def test_an_active_person_card_is_overwritten_but_a_project_control_still_refuses_creation():
    """W661 (operator, 10 Oct): an invitation "IS new card. even if something existed fine. we simply now
    UPSERT. overwrite". A person's still-active C and My are created again at their next revision."""
    p, c, my = _person()
    result = await _rejoin(_Host(p, c, my), p)
    assert result["ok"] is True, result
    members = {member["access_id"]: member for member in result["plan"]["candidate_value"]["cards"]}
    for active in (c, my):
        member = members[active.access_id]
        assert member["action"] == "recreate" and member["original_revision"] == active.card_revision
        assert CardAuthority.from_mapping(member["candidate"]).card_revision == active.card_revision + 1
    validate_group_candidate(result["plan"]["candidate_value"], reads=result["plan"]["reads"])
    revoked_p = replace_state(p, CARD_STATE_REVOKED)
    from test_w578_card_lifecycle_plan import _p_request
    result = await plan_card_lifecycle(_Host(revoked_p), project_ref=PROJECT, creations=[_p_request()],
                                       actor_subject=CREATOR, actor_kind="caller", request_id=REQUEST)
    assert result["ok"] is False and result["error"] == "card_plan_target_exists", result


@pytest.mark.asyncio
async def test_remove_invite_again_remove_again_continues_one_revision_chain(tmp_path):
    p, c, my = _person()
    store = BundleStorageDelegatedCardStore(tmp_path)
    decisions = Decisions()
    tx.bind_transaction_decisions(store, decisions)
    reservations = _Reservations()
    tx.bind_catalog_reservations(store, reservations)
    service = DelegatedCardService(store=store, cache=_Cache(), mutation_lock=_mutation_lock)
    for card in (p, c, my):
        await service.commit(card, subject_hash=subject_hash_for(card.grantor_subject), expected_revision=0, now=100)

    class _StoreHost(_Host):
        def __init__(self):
            super().__init__()
            self.cards.load_current = self._load

        async def _load(self, access_id, *, subject_hash):
            stored = await store.read_current_authority(subject_hash=subject_hash, access_id=access_id)
            return (stored[1], object()) if stored is not None else None

    async def run(plan, group_id):
        current = {}
        for member in plan["candidate_value"]["cards"]:
            stored = await store.read_current_authority(subject_hash=member["subject_hash"],
                                                        access_id=member["access_id"])
            current[member["access_id"]] = stored[1]
        members = [(m["subject_hash"], current[m["access_id"]], CardAuthority.from_mapping(m["candidate"]), m["action"])
                   for m in plan["candidate_value"]["cards"]]
        staged = await service.stage_group_transaction(
            transaction_id=group_id, intent_digest="f" * 64, participant="project", members=members,
            now=datetime.fromtimestamp(100, timezone.utc), reads=plan["reads"], catalog=plan["catalog_digest"])
        assert staged["staged"] is True, staged
        decisions.recorded[group_id] = "committed"
        await service.decide_group_transaction(transaction_id=group_id, intent_digest="f" * 64,
                                               decision="committed", now=100)

    seen = {c.access_id: [], my.access_id: []}

    async def record():
        for card in (c, my):
            stored = await store.read_current_authority(subject_hash=subject_hash_for(card.grantor_subject),
                                                        access_id=card.access_id)
            seen[card.access_id].append(stored[1].card_revision)
            yield stored[1]

    for step, group_id in (("remove", "1" * 64), ("rejoin", "2" * 64), ("remove", "3" * 64)):
        host = _StoreHost()
        if step == "remove":
            current = [card async for card in record()]
            result = await _plan(host, [_remove(card) for card in current])
        else:
            [card async for card in record()]
            result = await _rejoin(host, p)
        assert result["ok"] is True, (step, result)
        await run(result["plan"], group_id)
    states = [card async for card in record()]
    assert all(card.state == CARD_STATE_REVOKED for card in states)
    for revisions in seen.values():
        assert revisions == sorted(set(revisions))  # strictly increasing: (access_id, revision) never repeats
        assert revisions[-1] == revisions[0] + 3


@pytest.mark.asyncio
async def test_staging_refuses_a_recreate_that_is_not_over_a_revoked_card(tmp_path):
    """A stored ACTIVE Card is never replaced by a 'recreate' member, whatever the plan says."""
    p, c, my = _person()
    store = BundleStorageDelegatedCardStore(tmp_path)
    decisions = Decisions()
    tx.bind_transaction_decisions(store, decisions)
    tx.bind_catalog_reservations(store, _Reservations())
    service = DelegatedCardService(store=store, cache=_Cache(), mutation_lock=_mutation_lock)
    for card in (p, c):
        await service.commit(card, subject_hash=subject_hash_for(card.grantor_subject), expected_revision=0, now=100)
    fresh = dataclasses.replace(c, card_revision=c.card_revision + 1)
    with pytest.raises(Exception) as caught:
        await service.stage_group_transaction(
            transaction_id="9" * 64, intent_digest="f" * 64, participant="project",
            members=[(subject_hash_for(c.grantor_subject), c, fresh, "recreate")],
            now=datetime.fromtimestamp(100, timezone.utc), reads=[], catalog="")
    assert "card_group_recreate_invalid" in str(caught.value)
    stored = await store.read_current_authority(subject_hash=subject_hash_for(c.grantor_subject), access_id=c.access_id)
    assert stored[1].to_dict() == c.to_dict()


@pytest.mark.asyncio
@pytest.mark.parametrize("revision_step", [0, 2])
async def test_staging_refuses_a_recreate_not_at_the_revoked_cards_next_revision(tmp_path, revision_step):
    """Main 16:07: the stored revoked original admits only its exact next revision."""
    p, c, my = _person()
    store = BundleStorageDelegatedCardStore(tmp_path)
    decisions = Decisions()
    tx.bind_transaction_decisions(store, decisions)
    tx.bind_catalog_reservations(store, _Reservations())
    service = DelegatedCardService(store=store, cache=_Cache(), mutation_lock=_mutation_lock)
    revoked = replace_state(c, CARD_STATE_REVOKED)
    await service.commit(p, subject_hash=subject_hash_for(p.grantor_subject), expected_revision=0, now=100)
    await service.commit(c, subject_hash=subject_hash_for(c.grantor_subject), expected_revision=0, now=100)
    await service.commit(revoked, subject_hash=subject_hash_for(c.grantor_subject),
                         expected_revision=c.card_revision, now=100)
    stored = await store.read_current_authority(subject_hash=subject_hash_for(c.grantor_subject), access_id=c.access_id)
    assert stored[1].state == CARD_STATE_REVOKED
    fresh = dataclasses.replace(c, card_revision=revoked.card_revision + revision_step)
    with pytest.raises(Exception) as caught:
        await service.stage_group_transaction(
            transaction_id="8" * 64, intent_digest="f" * 64, participant="project",
            members=[(subject_hash_for(c.grantor_subject), stored[1], fresh, "recreate")],
            now=datetime.fromtimestamp(100, timezone.utc), reads=[], catalog="")
    assert "card_group_recreate_invalid" in str(caught.value)



@pytest.mark.asyncio
async def test_w661_a_removal_with_base_zero_ends_the_cards_the_hub_reads_now():
    """W661 P4: PB reads no Card, so it sends original_revision 0; the Hub pins the version it loaded."""
    p, c, my = _person()
    result = await _plan(_Host(p, c, my), [_remove(c, original_revision=0), _remove(my, original_revision=0)])
    assert result["ok"] is True, result
    members = {member["access_id"]: member for member in result["plan"]["candidate_value"]["cards"]}
    for card in (c, my):
        assert members[card.access_id]["original_revision"] == card.card_revision
        assert CardAuthority.from_mapping(members[card.access_id]["candidate"]).state == CARD_STATE_REVOKED


@pytest.mark.asyncio
async def test_w661_v63_a_person_already_removed_is_already_applied_and_nothing_is_written():
    """v6.3 item 6: the same-request retry after PUBLISH; both Cards already revoked, base 0."""
    p, c, my = _person()
    revoked = [replace_state(c, CARD_STATE_REVOKED), replace_state(my, CARD_STATE_REVOKED)]
    host = _Host(p, *revoked)
    result = await _plan(host, [_remove(card, original_revision=0) for card in revoked])
    assert result["ok"] is False and result["error"] == "card_plan_already_applied", result
    assert host.writes == 0


@pytest.mark.asyncio
async def test_w661_v63_a_half_removed_person_stages_only_the_active_card():
    p, c, my = _person()
    host = _Host(p, c, replace_state(my, CARD_STATE_REVOKED))
    result = await _plan(host, [_remove(c, original_revision=0), _remove(my, original_revision=0)])
    assert result["ok"] is True, result
    assert [member["access_id"] for member in result["plan"]["candidate_value"]["cards"]] == [c.access_id]


@pytest.mark.asyncio
async def test_w661_v63_an_already_revoked_card_is_still_checked_as_this_persons():
    p, c, my = _person()
    revoked_c, revoked_my = replace_state(c, CARD_STATE_REVOKED), replace_state(my, CARD_STATE_REVOKED)
    other = replace_state(_other_persons_my(p), CARD_STATE_REVOKED)
    for host, updates in ((_Host(p, c, my, other), [_remove(other, original_revision=0)]),
                          (_Host(p, revoked_c, revoked_my), [_remove(revoked_c, target="person-2", original_revision=0)])):
        result = await _plan(host, updates)
        assert result == {"ok": False, "error": "card_plan_update_scope_invalid", "status": 403}, result
    # An explicit revision keeps the old rule: a revoked Card is not a removal target.
    result = await _plan(_Host(p, revoked_c, revoked_my), [_remove(revoked_c), _remove(revoked_my)])
    assert result["error"] == "card_plan_revoke_not_active"

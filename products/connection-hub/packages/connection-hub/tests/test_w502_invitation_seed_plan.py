# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W502: an invitation's redemption planned as one group, as the direct bind builds it.

The person's C is built from the pending invitation Card (its selection, label
and link, an admin's later edit included), carries the claim marker and the
"bound_from_invitation" audit, and is bound under P. My carries the same marker.
The pending Card is revoked in the same request. The host's decision for the C
step must carry the verified-session email digest equal to the Card's target.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

import pytest

from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.card_group import validate_group_candidate
from connection_hub.delegated_credentials.cards.lifecycle_plan_operation import CREATION_STEPS, UPDATE_STEPS
from connection_hub.delegated_credentials.cards.model import CARD_STATE_ACTIVE, CARD_STATE_REVOKED, CardAuthority
from connection_hub.delegated_credentials.cards.service import DelegatedCardService, replace_state
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore, subject_hash_for
from connection_hub.delegated_credentials.controls.model import new_credentialless_card
from connection_hub.delegated_credentials.controls.project_invitation import (
    PROJECT_INVITATION_CONTROL_ISSUER_KIND,
    PROJECT_INVITATION_CONTROL_PROPERTY,
    ProjectInvitationControlIdentity,
    bind_project_invitation_control,
)
from connection_hub.delegated_credentials.controls.project_person import (
    PROJECT_PERSON_CONTROL_AUDIT_PROVENANCE,
    ProjectPersonControlIdentity,
)
from connection_hub.delegated_credentials.project_authorization import (
    LifecyclePlanAuthorization,
    LifecyclePlanAuthorizationRequest,
    LifecyclePlanStep,
    ProjectAuthorizationDecision,
    ProjectControlLocator,
)
from connection_hub.delegated_credentials.project_invitation_binding import normalized_email_digest
from connection_hub.delegated_credentials.project_invitation_claim import PROJECT_INVITATION_BINDING_PROVENANCE
from test_card_service import _Cache
from test_card_transaction_store import Decisions
from test_w502_person_removal_plan import _mutation_lock, _Reservations
from test_w578_card_lifecycle_plan import CREATOR, PROJECT, REQUEST, TARGET, _Host, _p
from connection_hub.delegated_credentials.card_lifecycle_plan import plan_card_lifecycle

EMAIL = "Person@Example.test"
RESOURCE = "https://board.example.test/mcp/problem_board"


def _pending(*, edited: bool = True):
    invitation = ProjectInvitationControlIdentity.build(
        project_ref=PROJECT, invitation_ref="invite-1", target_email=EMAIL)
    card = bind_project_invitation_control(new_credentialless_card(
        grantor_subject=invitation.project_subject, catalog_version="catalog-v1",
        control_id=invitation.control_id, issuer_ref=invitation.invitation_ref,
        issuer_kind=PROJECT_INVITATION_CONTROL_ISSUER_KIND, issuer_label="Invited to Quickstart", now=100,
    ), identity=invitation)
    if edited:
        # An admin's edit after inviting: the person must get exactly this selection.
        card = dataclasses.replace(card, card_revision=card.card_revision + 1,
                                   resource_grants={RESOURCE: ("work:read",)},
                                   resource_operations={RESOURCE: ("project.plan.read",)})
    return invitation, card


def _requests(p, pending, inv, *, revoke=True, **identity_changes):
    identity = {"target_subject": TARGET, "seed_origin": "invitation",
                "invitation": {"invitation_ref": inv.invitation_ref, "control_id": pending.access_id,
                               "original_revision": pending.card_revision}}
    identity.update(identity_changes)
    creations = [
        {"ref": "c", "kind": "project_person_control", "identity": identity, "selection": {},
         "parent": {"access_id": p.access_id, "holder_subject": CREATOR}},
        {"ref": "my", "kind": "project_person_my_card", "identity": {"person_subject": TARGET},
         "selection": {}, "parent": {"ref": "c"}},
    ]
    updates = [{"kind": "revoke", "target_subject": inv.invitation_ref, "access_id": pending.access_id,
                "subject_hash": subject_hash_for(pending.grantor_subject),
                "original_revision": pending.card_revision}] if revoke else []
    return creations, updates


def _authorization(creations, updates, *, evidence):
    steps = [LifecyclePlanStep(ref=raw["ref"], operation=CREATION_STEPS[raw["kind"]][0],
                               target_subject=raw["identity"][CREATION_STEPS[raw["kind"]][1]])
             for raw in creations]
    steps += [LifecyclePlanStep(ref=f"update:{index}", operation=UPDATE_STEPS[raw["kind"]],
                                target_subject=raw["target_subject"]) for index, raw in enumerate(updates)]
    request = LifecyclePlanAuthorizationRequest(actor_subject=TARGET, project_ref=PROJECT, request_id=REQUEST,
                                                request_digest="a" * 64, steps=tuple(steps))
    locator = ProjectControlLocator(control_id=_p().access_id, holder_subject=CREATOR)
    return LifecyclePlanAuthorization(request=request, decisions=tuple(
        (step.ref, ProjectAuthorizationDecision.allow(
            request.step_request(step), delegable_grants=("work:admin", "work:read"), platform_admin=False,
            project_control=locator, evidence=evidence if step.ref == "c" else None))
        for step in steps))


async def _plan(host, creations, updates, *, evidence):
    return await plan_card_lifecycle(host, project_ref=PROJECT, creations=creations, updates=updates,
                                     actor_subject=TARGET, actor_kind="caller", request_id=REQUEST,
                                     authorization=_authorization(creations, updates, evidence=evidence))


def _evidence(invitation, email=EMAIL):
    return {"invitation_ref": invitation.invitation_ref, "invitation_email_digest": normalized_email_digest(email)}


@pytest.mark.asyncio
async def test_redemption_builds_the_persons_cards_from_the_pending_invitation_card():
    p = _p()
    invitation, pending = _pending()
    creations, updates = _requests(p, pending, invitation)
    host = _Host(p, pending)
    result = await _plan(host, creations, updates, evidence=_evidence(invitation))
    assert result["ok"] is True, result
    members = {member["access_id"]: member for member in result["plan"]["candidate_value"]["cards"]}
    person = ProjectPersonControlIdentity.build(project_ref=PROJECT, target_subject=TARGET)
    control = CardAuthority.from_mapping(members[person.control_id]["candidate"])
    # The admin's later edit to the pending Card is what the person gets.
    assert control.resource_grants == pending.resource_grants
    assert control.resource_operations == pending.resource_operations
    assert control.issuer_label == pending.issuer_label and control.manage_url == pending.manage_url
    # As the direct bind does: the pending Card's properties seed C (the builder merges them).
    assert (control.properties or {}).get(PROJECT_INVITATION_CONTROL_PROPERTY) == (
        pending.properties or {}).get(PROJECT_INVITATION_CONTROL_PROPERTY)
    assert control.control_card.control_id == p.access_id and control.control_card.control_revision == p.card_revision
    marker = control.provenance[PROJECT_INVITATION_BINDING_PROVENANCE]
    assert marker == {"schema": marker["schema"], "project_ref": PROJECT, "invitation_ref": "invite-1",
                      "pending_control_id": pending.access_id, "control_id": person.control_id,
                      "person_subject": TARGET, "target_email_digest": normalized_email_digest(EMAIL),
                      "request_id": REQUEST, "bound_at": marker["bound_at"]}
    assert control.provenance[PROJECT_PERSON_CONTROL_AUDIT_PROVENANCE]["action"] == "bound_from_invitation"
    my = next(CardAuthority.from_mapping(m["candidate"]) for m in members.values()
              if m["candidate"]["issuer_kind"] == "project-person")
    assert my.provenance[PROJECT_INVITATION_BINDING_PROVENANCE] == marker
    assert my.resource_operations == control.resource_operations
    revoked = members[pending.access_id]
    assert revoked["action"] == "revoke" and revoked["candidate"]["state"] == CARD_STATE_REVOKED
    validate_group_candidate(result["plan"]["candidate_value"], reads=result["plan"]["reads"])
    assert host.writes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["other_email", "no_evidence", "other_invitation_in_evidence", "not_consumed",
                                  "stale_pending", "pending_revoked", "selection_supplied", "label_supplied",
                                  "missing_invitation"])
async def test_a_redemption_that_is_not_exactly_this_claim_refuses(case):
    p = _p()
    invitation, pending = _pending()
    evidence = _evidence(invitation)
    kwargs = {}
    if case == "other_email":
        evidence = _evidence(invitation, "someone.else@example.test")
    elif case == "no_evidence":
        evidence = None
    elif case == "other_invitation_in_evidence":
        evidence = {**evidence, "invitation_ref": "invite-2"}
    elif case == "not_consumed":
        kwargs["revoke"] = False
    elif case == "label_supplied":
        kwargs["label"] = "Something else"
    elif case == "missing_invitation":
        kwargs["invitation"] = None
    creations, updates = _requests(p, pending, invitation, **kwargs)
    stored = pending
    if case == "stale_pending":
        creations[0]["identity"]["invitation"]["original_revision"] -= 1
    elif case == "pending_revoked":
        stored = replace_state(pending, CARD_STATE_REVOKED)
        creations, updates = _requests(p, stored, invitation, revoke=False)
    elif case == "selection_supplied":
        creations[0]["selection"] = {"resource_operations": {RESOURCE: ["project.plan.read"]}}
    host = _Host(p, stored)
    result = await _plan(host, creations, updates, evidence=evidence)
    assert result["ok"] is False, (case, result)
    assert result["error"] == {
        "other_email": "project_invitation_binding_email_mismatch",
        "no_evidence": "project_invitation_binding_email_mismatch",
        "other_invitation_in_evidence": "project_invitation_binding_email_mismatch",
        "not_consumed": "card_plan_invitation_not_consumed",
        "stale_pending": "card_plan_original_revision_changed",
        "pending_revoked": "project_invitation_control_not_active",
        "selection_supplied": "card_plan_invitation_seed_invalid",
        "label_supplied": "card_plan_invitation_seed_invalid",
        "missing_invitation": "card_plan_invitation_seed_invalid",
    }[case], (case, result)
    assert host.writes == 0


@pytest.mark.asyncio
async def test_a_removed_person_newly_invited_gets_fresh_cards_from_the_invitation(tmp_path):
    p = _p()
    invitation, pending = _pending()
    first = await _plan(_Host(p, pending), *_requests(p, pending, invitation), evidence=_evidence(invitation))
    person = ProjectPersonControlIdentity.build(project_ref=PROJECT, target_subject=TARGET)
    built = {m["access_id"]: CardAuthority.from_mapping(m["candidate"]) for m in first["plan"]["candidate_value"]["cards"]}
    revoked = [replace_state(card, CARD_STATE_REVOKED) for access_id, card in built.items()
               if access_id != pending.access_id]
    result = await _plan(_Host(p, pending, *revoked), *_requests(p, pending, invitation),
                         evidence=_evidence(invitation))
    assert result["ok"] is True, result
    members = {m["access_id"]: m for m in result["plan"]["candidate_value"]["cards"]}
    for card in revoked:
        member = members[card.access_id]
        assert member["action"] == "recreate" and member["original_revision"] == card.card_revision
        assert member["candidate"]["card_revision"] == card.card_revision + 1
    audit = members[person.control_id]["candidate"]["provenance"][PROJECT_PERSON_CONTROL_AUDIT_PROVENANCE]
    assert audit["action"] == "bound_from_invitation"  # the fresh Card keeps how it was made


@pytest.mark.asyncio
async def test_one_decision_creates_the_persons_cards_and_ends_the_invitation_card(tmp_path):
    p = _p()
    invitation, pending = _pending()
    result = await _plan(_Host(p, pending), *_requests(p, pending, invitation), evidence=_evidence(invitation))
    assert result["ok"] is True, result
    plan = result["plan"]
    store = BundleStorageDelegatedCardStore(tmp_path)
    decisions = Decisions()
    tx.bind_transaction_decisions(store, decisions)
    reservations = _Reservations()
    tx.bind_catalog_reservations(store, reservations)
    service = DelegatedCardService(store=store, cache=_Cache(), mutation_lock=_mutation_lock)
    await service.commit(p, subject_hash=subject_hash_for(p.grantor_subject), expected_revision=0, now=100)
    first = dataclasses.replace(pending, card_revision=1, resource_grants={}, resource_operations={})
    await service.commit(first, subject_hash=subject_hash_for(pending.grantor_subject), expected_revision=0, now=100)
    await service.commit(pending, subject_hash=subject_hash_for(pending.grantor_subject), expected_revision=1, now=100)
    originals = {pending.access_id: pending}
    members = [(m["subject_hash"], originals.get(m["access_id"]), CardAuthority.from_mapping(m["candidate"]),
                m["action"]) for m in plan["candidate_value"]["cards"]]
    group_id = "7" * 64
    staged = await service.stage_group_transaction(
        transaction_id=group_id, intent_digest="f" * 64, participant="project", members=members,
        now=datetime.fromtimestamp(100, timezone.utc), reads=plan["reads"], catalog=plan["catalog_digest"])
    assert staged["staged"] is True, staged
    decisions.recorded[group_id] = "committed"
    await service.decide_group_transaction(transaction_id=group_id, intent_digest="f" * 64,
                                           decision="committed", now=100)
    states = {}
    for subject_hash, _original, candidate, _action in members:
        stored = await store.read_current_authority(subject_hash=subject_hash, access_id=candidate.access_id)
        states[candidate.access_id] = stored[1].state
    person = ProjectPersonControlIdentity.build(project_ref=PROJECT, target_subject=TARGET)
    assert states[pending.access_id] == CARD_STATE_REVOKED
    assert states[person.control_id] == CARD_STATE_ACTIVE
    assert sorted(states.values()).count(CARD_STATE_ACTIVE) == 2


def test_a_pending_card_of_another_project_is_never_a_seed_here():
    """Main 16:39: the helper refuses another project's ACTIVE pending Card even with matching claim values.

    Through the planner such a Card is not even found (it is keyed under its own
    project's subject); this pins the helper's own project check directly.
    """
    from connection_hub.delegated_credentials.card_lifecycle_plan import (
        CardLifecyclePlanRefused, invitation_seeded_person_control,
    )
    other = ProjectInvitationControlIdentity.build(
        project_ref="work:project:elsewhere", invitation_ref="invite-1", target_email=EMAIL)
    pending = bind_project_invitation_control(new_credentialless_card(
        grantor_subject=other.project_subject, catalog_version="catalog-v1", control_id=other.control_id,
        issuer_ref=other.invitation_ref, issuer_kind=PROJECT_INVITATION_CONTROL_ISSUER_KIND, now=100,
    ), identity=other)
    decision = dict(_authorization(*_requests(_p(), pending, other), evidence=_evidence(other)).decisions)["c"]
    with pytest.raises(CardLifecyclePlanRefused) as caught:
        invitation_seeded_person_control(pending, invitation_ref="invite-1", project_ref=PROJECT,
                                         target_subject=TARGET, decision=decision, actor_subject=TARGET,
                                         request_id=REQUEST, parent=_p(), now=100)
    assert str(caught.value) == "card_plan_update_scope_invalid"


@pytest.mark.asyncio
async def test_a_host_decision_for_another_person_refuses_the_seed():
    p = _p()
    invitation, pending = _pending()
    creations, updates = _requests(p, pending, invitation)
    authorization = _authorization(creations, updates, evidence=_evidence(invitation))
    request = authorization.request
    steps = tuple(dataclasses.replace(step, target_subject="person-2") if step.ref == "c" else step
                  for step in request.steps)
    forged_request = dataclasses.replace(request, steps=steps)
    forged = LifecyclePlanAuthorization(request=forged_request, decisions=tuple(
        (step.ref, ProjectAuthorizationDecision.allow(forged_request.step_request(step), delegable_grants=(),
                                                      platform_admin=False, project_control=dict(
                                                          authorization.decisions)[step.ref].project_control,
                                                      evidence=_evidence(invitation) if step.ref == "c" else None))
        for step in steps))
    host = _Host(p, pending)
    result = await plan_card_lifecycle(host, project_ref=PROJECT, creations=creations, updates=updates,
                                       actor_subject=TARGET, actor_kind="caller", request_id=REQUEST,
                                       authorization=forged)
    assert result["ok"] is False and result["status"] == 403, result
    assert host.writes == 0

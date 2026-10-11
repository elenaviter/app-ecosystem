"""W746: a link-only join's STAGE carries the redeemer's verified email digest on the invitation seed, and only there.

The planner binds the pending invitation Card to the redeemer by decision.evidence["invitation_email_digest"]
(card_lifecycle_plan, project_invitation_binding_email_mismatch). The legacy PLAN callback supplied that evidence;
the card_version STAGE built plain allow decisions, so a link-only join could never pass on the real Hub (found in
the W741 review). PB now sends identity.invitation.email_digest on the seed creation; stage_authorization lifts it
into that one step's evidence and the planner sees the identity it always checked. The end-to-end proof (real Hub,
redeem commits, a wrong digest writes nothing) is Problem Board's test_w746_link_only_join_postgres.
"""
from __future__ import annotations

import copy

import pytest

from connection_hub.delegated_credentials.cards.lifecycle_plan_operation import _Refused
from connection_hub.delegated_credentials.cards.participant_card_version import (
    invitation_seed_evidence, stage_authorization,
)

PROJECT, PERSON, DIGEST = "work:project:synthetic", "synthetic-joiner", "e" * 64
LOCATOR = {"control_id": "project-control-1", "holder_subject": "synthetic-owner"}


def _seed(**invitation):
    return {"ref": "c", "kind": "project_person_control",
            "identity": {"target_subject": PERSON, "seed_origin": "invitation",
                         "invitation": {"invitation_ref": "invite-1", "control_id": "invitation-control-1",
                                        "original_revision": 1, **invitation}},
            "selection": {}, "parent": dict(LOCATOR)}


MY = {"ref": "my", "kind": "project_person_my_card", "identity": {"person_subject": PERSON}, "selection": {},
      "parent": {"ref": "c"}}
REVOKE = {"kind": "revoke", "target_subject": "invite-1", "access_id": "invitation-control-1",
          "subject_hash": "a" * 64, "original_revision": 1}


def _stage(creations, updates=()):
    return {"scope": PROJECT, "request_id": "join-plan:synthetic", "actor_subject": PERSON,
            "delegable_grants": ["work:review"], "project_control": dict(LOCATOR),
            "creations": list(creations), "updates": list(updates)}


def _evidence(authorization):
    return {ref: dict(decision.evidence) for ref, decision in authorization.decisions}


def test_the_seed_digest_becomes_that_steps_evidence_and_leaves_the_identity():
    creations, seeds = invitation_seed_evidence([_seed(email_digest=DIGEST), MY])
    assert creations[0]["identity"]["invitation"] == {"invitation_ref": "invite-1",
                                                      "control_id": "invitation-control-1", "original_revision": 1}
    assert creations[1] is MY
    assert seeds == {"c": {"invitation_ref": "invite-1", "invitation_email_digest": DIGEST}}
    evidence = _evidence(stage_authorization(_stage([_seed(email_digest=DIGEST), MY], [REVOKE]), "d" * 64))
    assert evidence == {"c": {"source": "card-version-stage", "request_digest": "d" * 64,
                              "invitation_ref": "invite-1", "invitation_email_digest": DIGEST},
                        "my": {}, "update:0": {}}


@pytest.mark.parametrize("creation", [
    {**MY, "identity": {"person_subject": PERSON, "email_digest": DIGEST}},
    {**MY, "identity": {"person_subject": PERSON, "invitation": {"email_digest": DIGEST}}},
    {"ref": "c", "kind": "project_person_control",  # a person Control that is not an invitation seed
     "identity": {"target_subject": PERSON, "invitation": {"email_digest": DIGEST}}, "selection": {},
     "parent": dict(LOCATOR)},
    {"ref": "c", "kind": "project_person_control",
     "identity": {"target_subject": PERSON, "seed_origin": "migration", "email_digest": DIGEST}, "selection": {},
     "parent": dict(LOCATOR)},
])
def test_a_digest_anywhere_but_the_invitation_seed_is_refused_by_name(creation):
    with pytest.raises(_Refused) as refused:
        invitation_seed_evidence([creation])
    assert (refused.value.code, refused.value.status) == ("card_plan_invitation_email_digest_misplaced", 400)
    with pytest.raises(_Refused):
        stage_authorization(_stage([creation]), "d" * 64)


@pytest.mark.parametrize("digest", ["", "E" * 64, "e" * 63, 7, None])
def test_a_malformed_seed_digest_is_refused(digest):
    with pytest.raises(_Refused) as refused:
        invitation_seed_evidence([_seed(email_digest=digest)])
    assert refused.value.code == "card_plan_invitation_seed_invalid"


@pytest.mark.parametrize("creations,updates", [
    ([_seed(), MY], [REVOKE]),  # a join STAGE without the digest (as before W746)
    ([MY], []),
    ([], [REVOKE]),
])
def test_every_other_stage_body_is_unchanged_byte_for_byte(creations, updates):
    data = _stage(copy.deepcopy(creations), copy.deepcopy(updates))
    before = copy.deepcopy(data)
    planned, seeds = invitation_seed_evidence(data["creations"])
    assert planned == before["creations"] and seeds == {}
    authorization = stage_authorization(data, "d" * 64)
    assert data == before  # nothing in the request is changed in place
    assert all(dict(decision.evidence) == {} for _, decision in authorization.decisions)

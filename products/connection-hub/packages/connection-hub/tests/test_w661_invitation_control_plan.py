"""W661 P4 (EMain 20:02Z): an invitation's pending Control Card as a STAGE creation.

``project_invitation_control`` builds exactly the Card the direct create builds
(project_invitation_pending): credentialless, AND, issued to the invitation, bound
to its identity, snapshot ``created`` and audited. Real planner; W578's host.
"""
from __future__ import annotations

import pytest

from connection_hub.delegated_credentials.cards.model import CARD_STATE_ACTIVE, CardAuthority
from connection_hub.delegated_credentials.cards.store import subject_hash_for
from connection_hub.delegated_credentials.controls.project_invitation import (
    PROJECT_INVITATION_CONTROL_ISSUER_KIND, ProjectInvitationControlIdentity,
)
from test_w578_card_lifecycle_plan import PROJECT, REQUEST, _Host, _authorization, plan_card_lifecycle

EMAIL = "invitee@example.test"


def _invite(**identity):
    return {"ref": "inv", "kind": "project_invitation_control",
            "identity": {"invitation_ref": "invite-1", "target_email": EMAIL, "label": EMAIL, **identity},
            "selection": {}, "parent": None}


async def _plan(host, creation, **kwargs):
    return await plan_card_lifecycle(host, project_ref=PROJECT, creations=[creation], updates=[],
                                     actor_subject="creator-1", actor_kind="caller", request_id=REQUEST,
                                     **({"authorization": _authorization([creation], **kwargs)} if kwargs else {}))


@pytest.mark.asyncio
async def test_the_pending_invitation_card_is_created_as_the_direct_create_builds_it():
    host = _Host()
    plan = await _plan(host, _invite())
    assert plan["ok"] is True, plan
    [member] = plan["plan"]["candidate_value"]["cards"]
    identity = ProjectInvitationControlIdentity.build(project_ref=PROJECT, invitation_ref="invite-1",
                                                      target_email=EMAIL)
    card = CardAuthority.from_mapping(member["candidate"])
    assert member["action"] == "create" and member["access_id"] == identity.control_id
    assert member["subject_hash"] == subject_hash_for(identity.project_subject)
    assert card.issuer_kind == PROJECT_INVITATION_CONTROL_ISSUER_KIND and card.issuer_ref == "invite-1"
    assert card.issuer_label == EMAIL and card.state == CARD_STATE_ACTIVE and card.card_revision == 1
    assert ProjectInvitationControlIdentity.from_authority(card) == identity  # bound to the invitation
    assert host.writes == 0  # planning writes nothing


@pytest.mark.asyncio
@pytest.mark.parametrize("change, error", [
    ({"target_email": ""}, "card_plan_invitation_identity_invalid"),
    ({"surprise": 1}, "card_plan_creation_identity_invalid"),
])
async def test_a_malformed_invitation_identity_is_refused(change, error):
    creation = _invite()
    creation["identity"] = {**creation["identity"], **change}
    plan = await _plan(_Host(), creation)
    assert plan["ok"] is False and plan["error"] == error, plan


@pytest.mark.asyncio
async def test_the_step_must_be_authorized_for_this_invitation_and_has_no_parent():
    other = await _plan(_Host(), _invite(), targets={"inv": "invite-2"})
    # The generic step binding refuses it first; the kind's own target check is the second fence.
    assert other["ok"] is False and other["error"] in {"card_plan_authorization_invalid",
                                                       "card_plan_authorization_target_mismatch"}, other
    parented = _invite()
    parented["parent"] = {"access_id": "p", "holder_subject": "creator-1"}
    plan = await _plan(_Host(), parented)
    assert plan["ok"] is False, plan


@pytest.mark.asyncio
async def test_an_existing_invitation_card_is_never_overwritten():
    identity = ProjectInvitationControlIdentity.build(project_ref=PROJECT, invitation_ref="invite-1",
                                                      target_email=EMAIL)
    first = await _plan(_Host(), _invite())
    existing = CardAuthority.from_mapping(first["plan"]["candidate_value"]["cards"][0]["candidate"])
    assert existing.access_id == identity.control_id
    again = await _plan(_Host(existing), _invite())
    assert again["ok"] is False and again["error"] == "card_plan_target_exists", again

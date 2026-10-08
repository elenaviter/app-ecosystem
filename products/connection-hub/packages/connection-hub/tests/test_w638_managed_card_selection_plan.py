"""W638: which Cards the project-Control and agent-Card PLAN kinds may target, and under which decision."""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from connection_hub.delegated_credentials.cards.model import CardAuthority, ControlCardBinding, NamedServiceSelection
from connection_hub.delegated_credentials.cards.lifecycle_plan_operation import UPDATE_STEPS
from connection_hub.delegated_credentials.controls.model import control_card_id_for_issuer
from connection_hub.delegated_credentials.managed_card_selection_plan import (
    PROJECT_AGENT_CARD_UPDATE, PROJECT_CONTROL_UPDATE, build_managed_card_selection_update, managed_target_kind,
)
from connection_hub.delegated_credentials.project_authorization import (
    PROJECT_PERSON_CONTROL_OPERATIONS, ProjectAuthorizationDecision,
)
from service_foundation.coordination.durable_decision_log import DecisionRefused

PROJECT, CREATOR, RESOURCE = "work:project:synthetic", "synthetic-creator", "https://board.example.test/mcp"


def project_control():
    return CardAuthority(access_id=control_card_id_for_issuer("application", PROJECT, grantor_subject=CREATOR),
        client_id="control-card:synthetic", grantor_subject=CREATOR, delegate_subject="", source="control",
        card_kind="control", issuer_ref=PROJECT, issuer_kind="application", card_revision=4,
        catalog_version="synthetic-catalog", identity_scope="grantor", composition_mode="and",
        named_service_operations=NamedServiceSelection.none(), resource_grants={RESOURCE: ("work:admin",)},
        resource_operations={RESOURCE: ("project.cards.manage",)})


def agent_card(parent):
    return replace(parent, access_id="agent-card-1", grantor_subject="synthetic-owner", issuer_kind="", issuer_ref="",
        source="agent", card_kind="agent", control_card=ControlCardBinding(control_id=parent.access_id,
            issuer_kind="application", issuer_ref=PROJECT, control_revision=parent.card_revision,
            holder_subject=CREATOR))


def test_the_kinds_are_registered_and_their_operations_admitted():
    assert UPDATE_STEPS["reselect_project_control"] == PROJECT_CONTROL_UPDATE
    assert UPDATE_STEPS["reselect_agent_card"] == PROJECT_AGENT_CARD_UPDATE
    assert {PROJECT_CONTROL_UPDATE, PROJECT_AGENT_CARD_UPDATE} <= PROJECT_PERSON_CONTROL_OPERATIONS


def test_only_the_scopes_own_control_and_a_card_directly_under_it_are_targets():
    p = project_control()
    assert managed_target_kind(p, project_ref=PROJECT, kind="project_control") == "project_control"
    assert managed_target_kind(agent_card(p), project_ref=PROJECT, kind="agent_card") == "agent_card"
    for card, kind in ((p, "agent_card"), (agent_card(p), "project_control"),
                       (p, "project_control_other"), (replace(p, issuer_ref="work:project:other"), "project_control"),
                       (replace(p, state="revoked"), "project_control")):
        with pytest.raises(DecisionRefused):
            managed_target_kind(card, project_ref=PROJECT, kind=kind)
    foreign = replace(agent_card(p), control_card=replace(agent_card(p).control_card, control_id="someone-else"))
    with pytest.raises(DecisionRefused):
        managed_target_kind(foreign, project_ref=PROJECT, kind="agent_card")


@pytest.mark.parametrize("mismatch", ["operation", "target", "actor", "request", "project", "denied"])
def test_the_decision_must_be_for_exactly_this_operation_target_actor_and_request(mismatch):
    p = project_control()
    fields = dict(allowed=True, operation=PROJECT_CONTROL_UPDATE, target_subject=p.access_id, actor_subject="alice",
                  request_id="plan-1", project_ref=PROJECT)
    changed = {"operation": ("operation", PROJECT_AGENT_CARD_UPDATE), "target": ("target_subject", "x"),
               "actor": ("actor_subject", "mallory"), "request": ("request_id", "plan-2"),
               "project": ("project_ref", "work:project:other"), "denied": ("allowed", False)}[mismatch]
    fields[changed[0]] = changed[1]

    class Decision(ProjectAuthorizationDecision):
        def __init__(self):  # a plain value holder: only the compared fields matter here
            object.__setattr__(self, "__dict__", dict(fields, delegable_grants=(), platform_admin=False))

    class Host:
        async def _resolve_card_authority(self, **kwargs):
            pytest.fail("a mismatched decision must refuse before any resolution")

    with pytest.raises(DecisionRefused, match="card_plan_authorization_invalid"):
        asyncio.run(build_managed_card_selection_update(Host(), original=p, kind="project_control",
            selection={"resource_operations": {RESOURCE: []}}, active=None, decision=Decision(),
            project_ref=PROJECT, actor_subject="alice", request_id="plan-1", now=1_800_000_000))


def test_a_pending_invitations_control_of_this_project_is_a_target_and_nothing_else_is():
    from connection_hub.delegated_credentials.controls.model import new_credentialless_card
    from connection_hub.delegated_credentials.controls.project_invitation import (
        PROJECT_INVITATION_CONTROL_ISSUER_KIND, ProjectInvitationControlIdentity, bind_project_invitation_control,
    )
    invitation = ProjectInvitationControlIdentity.build(project_ref=PROJECT, invitation_ref="invite-1",
                                                         target_email="someone@example.test")
    card = bind_project_invitation_control(new_credentialless_card(grantor_subject=invitation.project_subject,
        catalog_version="synthetic", control_id=invitation.control_id, issuer_ref=invitation.invitation_ref,
        issuer_kind=PROJECT_INVITATION_CONTROL_ISSUER_KIND, issuer_label="Invited", now=1_800_000_000),
        identity=invitation)
    assert managed_target_kind(card, project_ref=PROJECT, kind="invitation_control") == "invitation_control"
    with pytest.raises(DecisionRefused):
        managed_target_kind(card, project_ref="work:project:other", kind="invitation_control")
    with pytest.raises(DecisionRefused):
        managed_target_kind(project_control(), project_ref=PROJECT, kind="invitation_control")
    assert UPDATE_STEPS["reselect_invitation_control"] == "project.invitation_control.update"

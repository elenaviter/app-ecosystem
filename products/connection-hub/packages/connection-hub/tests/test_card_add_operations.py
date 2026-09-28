"""Add operations to a Card, keeping everything else (W371).

Operator ruling, 2026-09-28: "Use GitHub" is on for every agent that works on
a project, including one added to a project, and the person can still untick
it. The coordinator's choice (B): add only that operation, so a deliberate
narrowing of the agent's Card survives. The write is generic and knows no
project; a project path authorizes the actor before it.
"""

from __future__ import annotations

import pytest

from connection_hub.delegated_credentials.automation_access import (
    OPERATIONS_ADDED_AUDIT_PROVENANCE,
    OPERATIONS_ADDED_AUDIT_SCHEMA,
)
from test_card_profile_lever import USER, _worker_card
from test_oauth_dcr_client_independence import DECLARED_RESOURCE, GRANTOR


@pytest.mark.asyncio
async def test_an_offered_operation_is_added_with_its_grant_and_the_rest_stays():
    service, _, card = await _worker_card()

    added = await service.add_operations(USER, access_id=card.access_id, operations=["worker.heartbeat"], request_id="req-add")

    assert added["ok"] is True, added
    access = added["access"]
    assert access["access_id"] == card.access_id
    assert sorted(access["resource_operations"][DECLARED_RESOURCE]) == ["project.plan.item", "worker.heartbeat"]
    assert set(access["resource_grants"][DECLARED_RESOURCE]) == {"work:observe", "work:relay"}
    assert "assignment.assign" not in access["resource_operations"][DECLARED_RESOURCE], "nothing else is added"
    assert added["added"] == [{"resource": DECLARED_RESOURCE, "operations": ["worker.heartbeat"]}]
    audit = access["provenance"][OPERATIONS_ADDED_AUDIT_PROVENANCE]
    assert audit["schema"] == OPERATIONS_ADDED_AUDIT_SCHEMA
    assert audit["action"] == "operations_added"
    assert audit["actor_subject"] == GRANTOR and audit["request_id"] == "req-add"
    assert audit["after_revision"] == audit["before_revision"] + 1 == access["card_revision"]


@pytest.mark.asyncio
async def test_an_operation_already_there_writes_nothing():
    service, _, card = await _worker_card()

    same = await service.add_operations(USER, access_id=card.access_id, operations=["project.plan.item"])

    assert same == {"ok": True, "changed": False, "access_id": card.access_id, "added": []}


@pytest.mark.asyncio
async def test_a_narrowed_card_keeps_its_narrowing():
    """A person unticked what the profile gave; adding one operation does not bring it back."""

    service, _, card = await _worker_card()
    raised = await service.apply_authorization_profile(USER, access_id=card.access_id, profile="coordinator")
    narrowed = await service.update_access(
        USER,
        access_id=card.access_id,
        resource_grants={DECLARED_RESOURCE: ["work:observe"]},
        resource_operations={DECLARED_RESOURCE: ["project.plan.item"]},
        expected_card_revision=raised["access"]["card_revision"],
    )
    assert narrowed["ok"] is True, narrowed

    added = await service.add_operations(USER, access_id=card.access_id, operations=["worker.heartbeat"])

    assert sorted(added["access"]["resource_operations"][DECLARED_RESOURCE]) == ["project.plan.item", "worker.heartbeat"]
    assert "assignment.assign" not in added["access"]["resource_operations"][DECLARED_RESOURCE]


@pytest.mark.asyncio
async def test_refusals_are_named():
    service, _, card = await _worker_card()

    unknown = await service.add_operations(USER, access_id=card.access_id, operations=["not.an.operation"])
    empty = await service.add_operations(USER, access_id=card.access_id, operations=[])
    other = await service.add_operations(
        {"user_id": "someone-else", "roles": ["kdcube:role:registered"]}, access_id=card.access_id, operations=["worker.heartbeat"]
    )
    stale = await service.add_operations(
        USER, access_id=card.access_id, operations=["worker.heartbeat"], expected_card_revision=card.card_revision - 1
    )

    assert unknown["error"] == "delegated_access_operation_not_offered"
    assert empty["error"] == "delegated_access_operations_required"
    assert other["error"] in {"delegated_access_not_found", "delegated_access_not_owned"}
    assert stale["ok"] is False and stale["error"] == "delegated_access_precondition_failed"


@pytest.mark.asyncio
async def test_a_project_admin_adds_through_the_project_path_under_the_owners_key():
    from connection_hub.delegated_credentials.project_agent_card_access import (
        PROJECT_AGENT_CARD_AUDIT_PROVENANCE,
        AgentCardDecision,
        ProjectAgentCardAccess,
    )

    service, _, card = await _worker_card()

    class Port:
        async def authorize_agent_card(self, *, access_id, project_ref, action):
            return AgentCardDecision(
                allowed=True, via="project_admin", grantor_subject=GRANTOR, access_id=access_id,
                project_ref=project_ref, action=action,
            )

    admin = {"user_id": "second-admin", "roles": ["kdcube:role:registered"]}
    added = await ProjectAgentCardAccess(service, Port()).add_operations(
        admin, access_id=card.access_id, project_ref="work:project:one", operations=["worker.heartbeat"], request_id="req-w371",
    )

    assert added["ok"] is True, added
    provenance = added["access"]["provenance"]
    assert provenance[OPERATIONS_ADDED_AUDIT_PROVENANCE]["actor_subject"] == "second-admin"
    assert provenance[PROJECT_AGENT_CARD_AUDIT_PROVENANCE]["actor_subject"] == "second-admin"
    assert added["access"]["grantor_subject"] == GRANTOR, "still the owner's Card"

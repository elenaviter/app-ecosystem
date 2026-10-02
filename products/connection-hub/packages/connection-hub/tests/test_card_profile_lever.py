"""The Card lever: re-apply a descriptor authorization profile in place (W313 step 2).

Operator, 2026-09-24: "simply some agent can be assigned with coordinator
role. and have some quick lever to make its card suitable", and "likewise the
lever which downcast it to worker later on to default worker grants". Until
now the only raise was revoking the Card and consenting a new one, which
changed the access id and therefore the agent's principal.
"""

from __future__ import annotations

import pytest

from connection_hub.delegated_credentials.automation_access import (
    AUTHORIZATION_PROFILE_AUDIT_PROVENANCE,
    AUTHORIZATION_PROFILE_AUDIT_SCHEMA,
)
from connection_hub.delegated_credentials.cards.store import subject_hash_for
from test_oauth_dcr_client_independence import (
    DECLARED_RESOURCE,
    CONCRETE_RESOURCE,
    GRANTOR,
    _GrantStore,
    _Persistence,
    _service,
)

BOARD_CONNECTIONS = {
    "delegated_credentials": {
        "oauth": {
            "enabled": True,
            "capabilities": [
                {"grant": "work:observe", "label": "Observe", "delegable_roles": ["kdcube:role:registered"]},
                {"grant": "work:relay", "label": "Relay", "delegable_roles": ["kdcube:role:registered"]},
                {"grant": "work:coordinate", "label": "Coordinate", "delegable_roles": ["kdcube:role:registered"]},
            ],
            "resources": [
                {
                    "resource": DECLARED_RESOURCE,
                    "label": "Problem Board",
                    "identity_scope": "grantor",
                    "grants": ["work:observe", "work:relay", "work:coordinate"],
                    "tools": {
                        "project.plan.item": {"label": "Read an item", "grants": ["work:observe"]},
                        "worker.heartbeat": {"label": "Heartbeat", "grants": ["work:relay"]},
                        "assignment.assign": {"label": "Assign", "grants": ["work:coordinate"]},
                    },
                    "authorization_profiles": {
                        "worker": {
                            "scope": "work:profile:worker",
                            "label": "Worker",
                            "operations": ["project.plan.item", "worker.heartbeat"],
                        },
                        "coordinator": {
                            "scope": "work:profile:coordinator",
                            "label": "Coordinator",
                            "operations": ["*"],
                        },
                    },
                },
            ],
        }
    }
}

USER = {"user_id": GRANTOR, "roles": ["kdcube:role:registered"]}


async def _worker_card():
    persistence = _Persistence()
    service = _service(_GrantStore({}), persistence, connections=BOARD_CONNECTIONS)
    card = await service.record_oauth_grant(
        grantor_subject=GRANTOR,
        client_id="dcr-claude-ops",
        scopes=["work:observe"],
        operations=["project.plan.item"],
        resource=CONCRETE_RESOURCE,
    )
    assert card is not None
    return service, persistence, card


def _stored(persistence, access_id):
    subject_hash = subject_hash_for(GRANTOR)
    for (stored_hash, stored_id), record in getattr(persistence, "current", {}).items():
        if stored_hash == subject_hash and stored_id == access_id:
            return record
    return None


@pytest.mark.asyncio
async def test_coordinator_raises_the_card_in_place_and_worker_returns_it_to_the_default_set():
    service, persistence, card = await _worker_card()

    raised = await service.apply_authorization_profile(USER, access_id=card.access_id, profile="coordinator", request_id="req-raise")
    assert raised["ok"] is True, raised
    access = raised["access"]
    assert access["access_id"] == card.access_id  # same Card, same principal
    assert sorted(access["resource_operations"][DECLARED_RESOURCE]) == [
        "assignment.assign", "project.plan.item", "worker.heartbeat",
    ]
    assert set(access["resource_grants"][DECLARED_RESOURCE]) == {"work:observe", "work:relay", "work:coordinate"}
    assert raised["profile"] == "coordinator"
    audit = access["provenance"][AUTHORIZATION_PROFILE_AUDIT_PROVENANCE]
    assert audit["schema"] == AUTHORIZATION_PROFILE_AUDIT_SCHEMA
    assert audit["profile"] == "coordinator"
    assert audit["actor_subject"] == GRANTOR
    assert audit["request_id"] == "req-raise"
    assert audit["after_revision"] == audit["before_revision"] + 1 == access["card_revision"]

    lowered = await service.apply_authorization_profile(USER, access_id=card.access_id, profile="worker")
    assert lowered["ok"] is True, lowered
    # The declared worker set, not what the Card held before the raise.
    assert sorted(lowered["access"]["resource_operations"][DECLARED_RESOURCE]) == ["project.plan.item", "worker.heartbeat"]
    assert "work:coordinate" not in lowered["access"]["resource_grants"][DECLARED_RESOURCE]
    assert lowered["access"]["access_id"] == card.access_id
    assert lowered["access"]["provenance"][AUTHORIZATION_PROFILE_AUDIT_PROVENANCE]["profile"] == "worker"


@pytest.mark.asyncio
async def test_an_undeclared_profile_is_refused_naming_the_declared_ones():
    service, _, card = await _worker_card()
    refused = await service.apply_authorization_profile(USER, access_id=card.access_id, profile="admin")
    assert refused == {
        "ok": False,
        "error": "delegated_access_profile_not_declared",
        "status": 409,
        "profile": "admin",
        "available_profiles": ["coordinator", "worker"],
    }


@pytest.mark.asyncio
async def test_a_stale_revision_is_refused_and_changes_nothing():
    service, _, card = await _worker_card()
    first = await service.apply_authorization_profile(USER, access_id=card.access_id, profile="coordinator")
    assert first["ok"] is True
    stale = await service.apply_authorization_profile(
        USER, access_id=card.access_id, profile="worker", expected_card_revision=first["access"]["card_revision"] - 1
    )
    assert stale["ok"] is False
    assert stale["error"] == "delegated_access_precondition_failed"
    assert "assignment.assign" in stale["access"]["resource_operations"][DECLARED_RESOURCE]


@pytest.mark.asyncio
async def test_only_the_grantor_moves_its_card():
    service, _, card = await _worker_card()
    other = {"user_id": "someone-else", "roles": ["kdcube:role:registered"]}
    refused = await service.apply_authorization_profile(other, access_id=card.access_id, profile="coordinator")
    assert refused["ok"] is False
    assert refused["error"] in {"delegated_access_not_found", "delegated_access_not_owned"}
    missing = await service.apply_authorization_profile(USER, access_id=card.access_id, profile="")
    assert missing == {"ok": False, "error": "delegated_access_profile_required"}


@pytest.mark.asyncio
async def test_a_project_admin_raises_another_persons_agent_card_through_the_project_path():
    """W319 (and the W313 limit it closes): the project host authorizes, the
    Card changes under its owner's key, and both audits name the admin."""

    from connection_hub.delegated_credentials.project_agent_card_access import (
        PROJECT_AGENT_CARD_AUDIT_PROVENANCE,
        AgentCardDecision,
        ProjectAgentCardAccess,
    )

    service, persistence, card = await _worker_card()

    class Port:
        async def authorize_agent_card(self, *, access_id, project_ref, action):
            return AgentCardDecision(
                allowed=True, via="project_admin", grantor_subject=GRANTOR, access_id=access_id,
                project_ref=project_ref, action=action,
            )

    admin = {"user_id": "second-admin", "roles": ["kdcube:role:registered"]}
    refused = await service.apply_authorization_profile(admin, access_id=card.access_id, profile="coordinator")
    assert refused["ok"] is False, "directly, only the grantor moves its Card"

    access = ProjectAgentCardAccess(service, Port())
    raised = await access.apply_profile(
        admin, access_id=card.access_id, project_ref="work:project:one", profile="coordinator", request_id="req-w319",
    )
    assert raised["ok"] is True, raised
    provenance = raised["access"]["provenance"]
    assert provenance[AUTHORIZATION_PROFILE_AUDIT_PROVENANCE]["actor_subject"] == "second-admin"
    assert provenance[PROJECT_AGENT_CARD_AUDIT_PROVENANCE]["actor_subject"] == "second-admin"
    assert provenance[PROJECT_AGENT_CARD_AUDIT_PROVENANCE]["via"] == "project_admin"
    assert raised["access"]["grantor_subject"] == GRANTOR, "still the owner's Card"


# W420 (claude-main's finding, 2026-09-30): an unscoped profile apply resets
# every Card resource that declares the profile, so a second service that also
# declares "worker" lost other.write on a Problem Board worker refresh. The
# board's levers now scope the apply to their own resource.
OTHER_RESOURCE = "https://other.example.test/mcp"
TWO_SERVICES = {
    "delegated_credentials": {
        "oauth": {
            "enabled": True,
            "capabilities": [
                *BOARD_CONNECTIONS["delegated_credentials"]["oauth"]["capabilities"],
                {"grant": "other:read", "label": "Other read", "delegable_roles": ["kdcube:role:registered"]},
                {"grant": "other:write", "label": "Other write", "delegable_roles": ["kdcube:role:registered"]},
            ],
            "resources": [
                *BOARD_CONNECTIONS["delegated_credentials"]["oauth"]["resources"],
                {
                    "resource": OTHER_RESOURCE,
                    "label": "Other service",
                    "identity_scope": "grantor",
                    "grants": ["other:read", "other:write"],
                    "tools": {
                        "other.read": {"label": "Read", "grants": ["other:read"]},
                        "other.write": {"label": "Write", "grants": ["other:write"]},
                    },
                    "authorization_profiles": {
                        "worker": {"scope": "other:profile:worker", "label": "Other worker", "operations": ["other.read"]},
                    },
                },
            ],
        }
    }
}


async def _two_service_card():
    persistence = _Persistence()
    service = _service(_GrantStore({}), persistence, connections=TWO_SERVICES)
    card = await service.record_oauth_grant(
        grantor_subject=GRANTOR,
        client_id="dcr-claude-ops",
        scopes=["work:observe", "other:read", "other:write"],
        resource=CONCRETE_RESOURCE,
        # A client that carries one credential to several services.
        client_metadata={"kdcube_credential_use": "multi_resource"},
        resource_grants={DECLARED_RESOURCE: ["work:observe"], OTHER_RESOURCE: ["other:read", "other:write"]},
        resource_operations={DECLARED_RESOURCE: ["project.plan.item"], OTHER_RESOURCE: ["other.read", "other.write"]},
    )
    assert card is not None
    assert sorted(card.resource_operations[OTHER_RESOURCE]) == ["other.read", "other.write"]
    return service, card


@pytest.mark.asyncio
async def test_an_unscoped_worker_apply_resets_every_resource_that_declares_worker():
    """The generic path, unchanged for unscoped callers: this is the loss the scope prevents."""

    service, card = await _two_service_card()
    applied = await service.apply_authorization_profile(USER, access_id=card.access_id, profile="worker")
    assert applied["ok"] is True, applied
    assert applied["access"]["resource_operations"][OTHER_RESOURCE] == ["other.read"]


@pytest.mark.asyncio
async def test_a_scoped_worker_apply_resets_only_the_named_resource_and_keeps_the_other_service():
    service, card = await _two_service_card()
    applied = await service.apply_authorization_profile(
        USER, access_id=card.access_id, profile="worker", resources=[DECLARED_RESOURCE],
    )
    assert applied["ok"] is True, applied
    operations = applied["access"]["resource_operations"]
    assert sorted(operations[DECLARED_RESOURCE]) == ["project.plan.item", "worker.heartbeat"]
    # The other service declares "worker" too, and keeps its write.
    assert sorted(operations[OTHER_RESOURCE]) == ["other.read", "other.write"]
    assert sorted(applied["access"]["resource_grants"][OTHER_RESOURCE]) == ["other:read", "other:write"]
    audit = applied["access"]["provenance"][AUTHORIZATION_PROFILE_AUDIT_PROVENANCE]
    assert audit["resource_scope"] == [DECLARED_RESOURCE]
    assert [row["resource"] for row in audit["applied"]] == [DECLARED_RESOURCE]
    # A repeat is idempotent in effect: the same selection again.
    again = await service.apply_authorization_profile(
        USER, access_id=card.access_id, profile="worker", resources=[DECLARED_RESOURCE],
    )
    assert again["ok"] is True
    assert sorted(again["access"]["resource_operations"][OTHER_RESOURCE]) == ["other.read", "other.write"]


@pytest.mark.asyncio
async def test_a_scope_the_card_does_not_hold_or_an_empty_scope_is_refused_and_changes_nothing():
    service, card = await _two_service_card()
    absent = await service.apply_authorization_profile(
        USER, access_id=card.access_id, profile="worker", resources=["https://absent.example.test/mcp"],
    )
    assert absent == {
        "ok": False,
        "error": "delegated_access_profile_resource_not_on_card",
        "status": 409,
        "profile": "worker",
        "resources": ["https://absent.example.test/mcp"],
    }
    empty = await service.apply_authorization_profile(USER, access_id=card.access_id, profile="worker", resources=[])
    assert empty["error"] == "delegated_access_profile_resource_scope_empty"
    still = await service.apply_authorization_profile(
        USER, access_id=card.access_id, profile="worker", resources=[DECLARED_RESOURCE],
    )
    assert still["access"]["card_revision"] == card.card_revision + 1, "the refusals wrote nothing"

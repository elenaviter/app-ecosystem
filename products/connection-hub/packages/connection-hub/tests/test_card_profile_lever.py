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

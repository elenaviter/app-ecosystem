# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W562: a project's Control Card is changed only through the project (W502).

The operator's scope, as quoted in
``test_a_control_card_not_held_by_a_project_stays_editable_by_its_creator``:
"only Control Cards that belong to a Problem Board project are edited through
the project". A project's Control Card is created with ``issuer_kind="project"``
and stored under its creator. These tests ask whether the creator's plain
Connection Hub path (``control_card_update`` / ``control_card_revoke``) still
changes or revokes it with no project question, and keep the creator's plain
path working for a Control Card that no project holds.

Every verdict reads the stored record in the Card persistence, not only the
call's ``ok``. The shared ``_Persistence`` fake has no ``forget`` (revoke);
this file gives its own instance one, here only.
"""

from __future__ import annotations

import asyncio
import dataclasses
from types import MethodType

import pytest

from connection_hub.delegated_credentials.cards.model import CARD_STATE_ACTIVE, CARD_STATE_REVOKED
from test_agent_capability_control_sync import NAMED_RESOURCE, _service, subject_hash_for

PROJECT = "work:project:one"
CREATOR = "owner-one"


async def _forget(self, authority, *, subject_hash, revoked_authority=None):
    """Test-local revoke for this file's persistence instance: the record turns revoked."""

    held = self.records.get(authority.access_id)
    if held is None or subject_hash_for(held[0].grantor_subject) != subject_hash:
        raise AssertionError("forget of a record this owner does not hold")
    revoked = revoked_authority or dataclasses.replace(held[0], state=CARD_STATE_REVOKED)
    self.records[authority.access_id] = (revoked, held[1])
    self.persisted.append(f"forget:{authority.access_id}")


def _card(issuer_kind: str, issuer_ref: str):
    service, persistence = _service(named_services=True)
    persistence.forget = MethodType(_forget, persistence)
    creator = {"user_id": CREATOR, "roles": ["kdcube:role:super-admin"], "permissions": []}
    made = asyncio.run(service.control_card_create(
        creator, issuer_ref=issuer_ref, issuer_kind=issuer_kind, issuer_label="One",
    ))
    assert made["ok"] is True, made
    return service, persistence, creator, made["control_card"]["access_id"]


def _stored(persistence, control_id):
    return persistence.records[control_id][0]


@pytest.mark.parametrize("kind,ref", [("project", PROJECT)])
def test_the_creator_cannot_widen_a_projects_control_card_on_the_plain_path(kind, ref):
    service, persistence, creator, control_id = _card(kind, ref)
    before = _stored(persistence, control_id)
    assert before.issuer_kind == "project" and before.state == CARD_STATE_ACTIVE

    changed = asyncio.run(service.control_card_update(
        creator, control_id=control_id, resource_grants={NAMED_RESOURCE: ["named_services:use", "slack:read"]},
    ))

    after = _stored(persistence, control_id)
    stored_changed = after != before
    assert not stored_changed and changed.get("ok") is not True, (
        f"plain path on a project's Control Card: ok={changed.get('ok')}, "
        f"stored revision {before.card_revision}->{after.card_revision}, stored changed={stored_changed}"
    )
    assert changed["error"] == "project_control_card_managed" and changed["status"] == 403


def test_the_creator_cannot_revoke_a_projects_control_card_on_the_plain_path():
    service, persistence, creator, control_id = _card("project", PROJECT)
    before = _stored(persistence, control_id)

    revoked = asyncio.run(service.control_card_revoke(creator, control_id=control_id))

    after = _stored(persistence, control_id)
    assert after.state == CARD_STATE_ACTIVE and revoked.get("ok") is not True, (
        f"plain path revoke on a project's Control Card: ok={revoked.get('ok')}, "
        f"stored state {before.state}->{after.state}"
    )
    assert revoked["error"] == "project_control_card_managed" and revoked["status"] == 403


def test_positive_control_the_creator_edits_a_control_card_no_project_holds():
    service, persistence, creator, control_id = _card("application", "agent:resident:helper")
    before = _stored(persistence, control_id)

    changed = asyncio.run(service.control_card_update(
        creator, control_id=control_id, resource_grants={NAMED_RESOURCE: ["named_services:use", "slack:read"]},
    ))

    after = _stored(persistence, control_id)
    assert changed.get("ok") is True, changed
    assert after.card_revision == before.card_revision + 1 and after != before


def test_positive_control_the_creator_revokes_a_control_card_no_project_holds():
    service, persistence, creator, control_id = _card("application", "agent:resident:helper")

    revoked = asyncio.run(service.control_card_revoke(creator, control_id=control_id))

    assert revoked.get("ok") is True, revoked
    assert _stored(persistence, control_id).state == CARD_STATE_REVOKED


@pytest.mark.parametrize("operation", ["update", "revoke"])
@pytest.mark.parametrize("kind", ["project", "application"])
def test_raw_delegated_access_aliases_enforce_the_same_stored_project_boundary(operation, kind):
    service, persistence, creator, control_id = _card(kind, PROJECT)
    before = _stored(persistence, control_id)
    if operation == "update":
        result = asyncio.run(service.update_access(
            creator, access_id=control_id,
            resource_grants={NAMED_RESOURCE: ["named_services:use", "slack:read"]},
        ))
    else:
        result = asyncio.run(service.revoke_access(creator, access_id=control_id))
    after = _stored(persistence, control_id)
    if kind == "project":
        assert result["error"] == "project_control_card_managed" and result["status"] == 403
        assert after == before
    else:
        assert result["ok"] is True and after != before


@pytest.mark.parametrize("roles", [[], ["kdcube:role:registered"], ["kdcube:role:super-admin"]])
def test_creator_roles_and_audit_callbacks_cannot_substitute_for_a_project_decision(roles):
    service, persistence, creator, control_id = _card("project", PROJECT)
    creator = {**creator, "roles": roles}
    before = _stored(persistence, control_id)
    persisted = list(persistence.persisted)

    async def unexpected(*args, **kwargs):
        raise AssertionError("refused creator path reached catalog, migration or persistence")

    service._active_catalog = unexpected
    service._ensure_control_snapshot = unexpected
    service._forget_record = unexpected
    changed = asyncio.run(service.update_access(
        creator, access_id=control_id, resource_grants={},
        _platform_admin=True, _delegable_grants=["named_services:use", "slack:read"],
        _record_transform=lambda previous, candidate: candidate,
    ))
    revoked = asyncio.run(service.revoke_access(creator, access_id=control_id))
    for result in (changed, revoked):
        assert result["error"] == "project_control_card_managed" and result["status"] == 403
    assert _stored(persistence, control_id) == before
    assert persistence.persisted == persisted


@pytest.mark.parametrize("changes", [
    {"allowed": False}, {"allowed": "yes"}, {"project_ref": "work:project:other"},
    {"control_id": "other-card"}, {"grantor_subject": "other-person"},
    {"action": "read"}, {"via": "project_member"}, {"via": "platform_admin"},
])
def test_project_control_answers_are_bound_to_the_exact_stored_card(changes):
    from connection_hub.delegated_credentials.project_control_card_access import (
        ProjectControlCardDecision,
    )

    service, persistence, creator, control_id = _card("project", PROJECT)
    before = _stored(persistence, control_id)
    decision = ProjectControlCardDecision(**{
        "allowed": True, "via": "project_admin", "grantor_subject": CREATOR,
        "control_id": control_id, "project_ref": PROJECT, "action": "write", **changes,
    })
    result = asyncio.run(service.control_card_update(
        creator, control_id=control_id, resource_grants={},
        _project_authorization=decision,
    ))
    assert result["error"] == "project_control_card_managed" and result["status"] == 403
    assert _stored(persistence, control_id) == before


def test_a_client_mapping_is_not_a_typed_project_host_answer():
    service, persistence, creator, control_id = _card("project", PROJECT)
    before = _stored(persistence, control_id)
    result = asyncio.run(service.control_card_update(
        creator, control_id=control_id, resource_grants={},
        _project_authorization={
            "allowed": True, "via": "project_admin", "grantor_subject": CREATOR,
            "control_id": control_id, "project_ref": PROJECT, "action": "write",
        },
    ))
    assert result["error"] == "project_control_card_managed"
    assert _stored(persistence, control_id) == before


def _identity_card(kind):
    from connection_hub.delegated_credentials.controls.project_invitation import (
        PROJECT_INVITATION_CONTROL_PROPERTY, ProjectInvitationControlIdentity,
    )
    from connection_hub.delegated_credentials.controls.project_person import (
        PROJECT_PERSON_CONTROL_PROPERTY, ProjectPersonControlIdentity,
    )
    from connection_hub.delegated_credentials.project_authorization import (
        PROJECT_INVITATION_CONTROL_UPDATE, PROJECT_PERSON_CONTROL_UPDATE,
        ProjectAuthorizationDecision, ProjectAuthorizationRequest,
    )

    service, persistence, creator, original_id = _card("application", "helper")
    original, handles = persistence.records[original_id]
    if kind == "person":
        identity = ProjectPersonControlIdentity.build(project_ref=PROJECT, target_subject="person-one")
        marker = PROJECT_PERSON_CONTROL_PROPERTY
        issuer_kind, issuer_ref = "project", PROJECT
        operation, target = PROJECT_PERSON_CONTROL_UPDATE, identity.target_subject
    else:
        identity = ProjectInvitationControlIdentity.build(
            project_ref=PROJECT, invitation_ref="work:invitation:one", target_email="one@example.test",
        )
        marker = PROJECT_INVITATION_CONTROL_PROPERTY
        issuer_kind, issuer_ref = "project-invitation", identity.invitation_ref
        operation, target = PROJECT_INVITATION_CONTROL_UPDATE, identity.invitation_ref
    bound = dataclasses.replace(
        original, access_id=identity.control_id, grantor_subject=identity.project_subject,
        issuer_kind=issuer_kind, issuer_ref=issuer_ref, composition_mode="and",
        properties={**original.properties, marker: identity.to_property()},
    )
    persistence.records[bound.access_id] = (bound, handles)
    # The lifecycle uses the project partition only after validating its real actor.
    partition_principal = {**creator, "user_id": identity.project_subject}
    request = ProjectAuthorizationRequest.build(
        actor_subject=CREATOR, project_ref=PROJECT, target_subject=target,
        operation=operation, request_id="request-one",
    )
    decision = ProjectAuthorizationDecision.allow(request, delegable_grants=["named_services:use"])
    return service, persistence, partition_principal, bound.access_id, decision


@pytest.mark.parametrize("kind", ["person", "invitation"])
@pytest.mark.parametrize("changes", [
    {"allowed": False, "reason": "refused", "delegable_grants": ()},
    {"allowed": "yes"}, {"project_ref": "work:project:other"},
    {"target_subject": "other-target"}, {"operation": "project.person.control.revoke"},
    {"actor_subject": ""}, {"request_id": ""}, {"platform_admin": "yes"},
    {"evidence": []}, {"reason": "an allow cannot carry a denial"},
])
def test_person_and_invitation_answers_cannot_change_another_stored_identity(kind, changes):
    service, persistence, principal, control_id, decision = _identity_card(kind)
    before = _stored(persistence, control_id)
    persisted = list(persistence.persisted)

    async def unexpected(*args, **kwargs):
        raise AssertionError("mismatched project answer reached catalog or persistence")

    service._active_catalog = unexpected
    service._ensure_control_snapshot = unexpected
    result = asyncio.run(service.update_access(
        principal, access_id=control_id, resource_grants={},
        _project_authorization=dataclasses.replace(decision, **changes),
    ))
    assert result["error"] == "project_control_card_managed" and result["status"] == 403
    assert _stored(persistence, control_id) == before
    assert persistence.persisted == persisted


@pytest.mark.parametrize("kind", ["person", "invitation"])
def test_exact_person_and_invitation_answers_preserve_the_authorized_write(kind):
    service, persistence, principal, control_id, decision = _identity_card(kind)
    before = _stored(persistence, control_id)
    result = asyncio.run(service.update_access(
        principal, access_id=control_id, label="Authorized edit",
        resource_grants={NAMED_RESOURCE: ["named_services:use"]},
        _delegable_grants=decision.delegable_grants, _project_authorization=decision,
    ))
    after = _stored(persistence, control_id)
    assert result["ok"] is True, result
    assert after.card_revision == before.card_revision + 1
    assert after.label == "Authorized edit"
    for marker in ("connection_hub.project_person_control", "connection_hub.project_invitation_control"):
        assert after.properties.get(marker) == before.properties.get(marker)


@pytest.mark.asyncio
async def test_first_project_card_creation_retains_the_declared_profile(tmp_path):
    from test_resident_profile_cards import (
        MEMORIES, USER, _Harness, _connections_with_authorization_profiles,
    )

    harness = _Harness(tmp_path, connections=_connections_with_authorization_profiles())
    made = await harness.service.control_card_create(
        USER, issuer_ref=PROJECT, issuer_kind="project", initial_profile="coordinator",
    )
    assert made["ok"] is True and made["created"] is True, made
    stored = await harness.card(made["authority"]["access_id"])
    assert stored.resource_operations == {MEMORIES: ("search", "write")}
    assert stored.provenance["control_card_initial_selection"]["profile"] == "coordinator"
    assert harness.persistence.persist_calls == 1


@pytest.mark.asyncio
async def test_legacy_empty_project_card_start_requires_an_authorized_project_path(tmp_path):
    from test_resident_profile_cards import (
        USER, _Harness, _connections_with_authorization_profiles,
    )

    harness = _Harness(tmp_path, connections=_connections_with_authorization_profiles())
    made = await harness.service.control_card_create(USER, issuer_ref=PROJECT, issuer_kind="project")
    before = await harness.card(made["authority"]["access_id"])
    writes = harness.persistence.persist_calls
    result = await harness.service.control_card_create(
        USER, issuer_ref=PROJECT, issuer_kind="project", initial_profile="coordinator",
    )
    assert result["error"] == "project_control_card_managed" and result["status"] == 403
    assert await harness.card(before.access_id) == before
    assert harness.persistence.persist_calls == writes

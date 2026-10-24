"""W562: a project's Control Card is changed only through the project (W502).

The operator's scope, as quoted in
``test_a_control_card_not_held_by_a_project_stays_editable_by_its_creator``:
"only Control Cards that belong to a Problem Board project are edited through
the project". A project's Control Card is created with ``issuer_kind="project"``
and stored under its creator. These tests ask whether the creator's plain
Connection Hub path still changes or revokes it with no project question.
"""

from __future__ import annotations

import asyncio

from connection_hub.delegated_credentials.project_control_card_access import ProjectControlCardAccess
from test_project_control_card_access import PROJECT, _real_card, _real_port


def test_the_creator_cannot_widen_a_projects_control_card_on_the_plain_path():
    service, creator, control_id, resource = _real_card()
    before = asyncio.run(service.control_card_get(creator, control_id=control_id))["access"]
    assert before["issuer_kind"] == "project"

    changed = asyncio.run(service.control_card_update(
        creator, control_id=control_id, resource_grants={resource: ["named_services:use", "slack:read"]},
    ))

    assert changed.get("ok") is not True, "the plain path changed a project's Control Card"
    after = asyncio.run(service.control_card_get(creator, control_id=control_id))["access"]
    assert after.get("resource_grants") == before.get("resource_grants")


def test_the_refusal_names_the_project_path():
    service, creator, control_id, resource = _real_card()

    changed = asyncio.run(service.control_card_update(
        creator, control_id=control_id, resource_grants={resource: ["named_services:use"]},
    ))

    assert changed["error"] == "card_transactions_direct_write_refused"
    assert changed["status"] == 409 and changed["retryable"] is False
    assert "through its project" in changed["message"]


def test_the_creator_cannot_widen_it_from_their_own_card_list_either():
    # The Hub screen saves a Card opened from the creator's own list through delegated_access_update.
    service, creator, control_id, resource = _real_card()
    before = asyncio.run(service.control_card_get(creator, control_id=control_id))["access"]

    changed = asyncio.run(service.update_access(
        creator, access_id=control_id, resource_grants={resource: ["named_services:use", "slack:read"]},
    ))

    assert changed["error"] == "card_transactions_direct_write_refused"
    after = asyncio.run(service.control_card_get(creator, control_id=control_id))["access"]
    assert after.get("resource_grants") == before.get("resource_grants")


def test_the_creator_still_edits_it_through_the_project():
    service, creator, control_id, resource = _real_card()
    access = ProjectControlCardAccess(service, _real_port(control_id, via="owner"))

    changed = asyncio.run(access.update(
        creator, control_id=control_id, project_ref=PROJECT, request_id="req-owner",
        resource_grants={resource: ["named_services:use", "slack:read"]},
    ))

    assert changed["ok"] is True, changed
    stored = asyncio.run(service.control_card_get(creator, control_id=control_id))["access"]
    assert stored["resource_grants"][resource] == ["named_services:use", "slack:read"]


def test_a_project_admin_still_edits_it_through_the_project():
    service, creator, control_id, resource = _real_card()
    admin = {"user_id": "root", "roles": ["kdcube:role:super-admin"], "permissions": []}
    access = ProjectControlCardAccess(service, _real_port(control_id, via="project_admin"))

    changed = asyncio.run(access.update(
        admin, control_id=control_id, project_ref=PROJECT, request_id="req-admin",
        resource_grants={resource: ["named_services:use", "slack:read"]},
    ))

    assert changed["ok"] is True, changed
    stored = asyncio.run(service.control_card_get(creator, control_id=control_id))["access"]
    assert stored["resource_grants"][resource] == ["named_services:use", "slack:read"]


def test_a_card_written_since_its_creation_does_not_restart_from_a_profile_on_the_creators_path(tmp_path):
    # claude-e-app's F1 (review of #908): the creation's first selection is the only write on the creator's
    # path, and only while the Card is at its creation revision. The project path itself cannot leave a Card
    # without grants (delegated_access_requires_resource_grants), so the "emptied" Card is set in the store, as
    # any other writer could leave it; it then looks unstarted again (no grants, no start stamp).
    import dataclasses
    from datetime import datetime, timedelta, timezone

    from connection_hub.delegated_credentials.issuer_gate import IssuerRegistry
    from connection_hub.delegated_credentials.project_control_card_access import ProjectControlCardDecision
    from test_project_control_card_access import Port
    from test_resident_profile_cards import GRANTOR, MEMORIES, USER, _connections_with_authorization_profiles, _Harness

    class Issuer:  # the same always-allowing independent issuer _real_card() registers
        issuer_kind = "project"
        adapter_id = "independent-fixture-issuer"

        async def prepare_context(self, request, **kwargs):
            return "fixture-exact-request-context"

        async def decide(self, request):
            return True, "fixture", "fixture-policy-v1", datetime.now(timezone.utc) + timedelta(seconds=30)

        async def finalize_context(self, request, *, outcome):
            return True

    h = _Harness(tmp_path, connections=_connections_with_authorization_profiles())
    persistence = h.service._cards()

    async def persist_guarded(authority, handles, *, subject_hash, expected_revision, before_commit):
        # As _real_card(): the fake store's guarded commit, with no suspension before the write.
        await before_commit()
        await persistence.persist(authority, handles, subject_hash=subject_hash, expected_revision=expected_revision)

    persistence.persist_guarded = persist_guarded
    h.service._issuers = IssuerRegistry()
    h.service._issuers.register(Issuer())
    made = asyncio.run(h.service.control_card_create(USER, issuer_ref=PROJECT, issuer_kind="project", issuer_label="One"))
    assert made["ok"] is True, made
    control_id = made["control_card"]["access_id"]

    def allow(action):
        return ProjectControlCardDecision(allowed=True, via="owner", grantor_subject=GRANTOR, control_id=control_id,
                                          project_ref=PROJECT, action=action, evidence={"role": "owner"})

    project = ProjectControlCardAccess(h.service, Port({action: allow(action) for action in ("read", "write")}))
    given = asyncio.run(project.update(USER, control_id=control_id, project_ref=PROJECT, request_id="req-give",
                                       resource_grants={MEMORIES: ["memories:read"]}))
    assert given["ok"] is True, given
    authority, handles = h.persistence.cards[control_id]
    h.persistence.cards[control_id] = (dataclasses.replace(
        authority, resource_grants={}, resource_operations={}, card_revision=authority.card_revision + 1), handles)
    before = asyncio.run(h.service.control_card_get(USER, control_id=control_id))["access"]
    assert before["card_revision"] > 1 and not any((before.get("resource_grants") or {}).values())

    restarted = asyncio.run(h.service.control_card_create(
        USER, issuer_ref=PROJECT, issuer_kind="project", issuer_label="One", initial_profile="coordinator"))

    assert restarted.get("error") == "card_transactions_direct_write_refused" and restarted.get("status") == 409, restarted
    after = asyncio.run(h.service.control_card_get(USER, control_id=control_id))["access"]
    assert after.get("resource_grants") == before.get("resource_grants")
    assert after.get("card_revision") == before.get("card_revision")


def test_a_fresh_project_control_card_still_starts_from_a_profile(tmp_path):
    # The creation's own first selection keeps the creator's path: nothing has written the Card yet.
    from datetime import datetime, timedelta, timezone

    from connection_hub.delegated_credentials.issuer_gate import IssuerRegistry
    from test_resident_profile_cards import USER, _connections_with_authorization_profiles, _Harness

    class Issuer:
        issuer_kind = "project"
        adapter_id = "independent-fixture-issuer"

        async def prepare_context(self, request, **kwargs):
            return "fixture-exact-request-context"

        async def decide(self, request):
            return True, "fixture", "fixture-policy-v1", datetime.now(timezone.utc) + timedelta(seconds=30)

        async def finalize_context(self, request, *, outcome):
            return True

    h = _Harness(tmp_path, connections=_connections_with_authorization_profiles())
    persistence = h.service._cards()

    async def persist_guarded(authority, handles, *, subject_hash, expected_revision, before_commit):
        await before_commit()
        await persistence.persist(authority, handles, subject_hash=subject_hash, expected_revision=expected_revision)

    persistence.persist_guarded = persist_guarded
    h.service._issuers = IssuerRegistry()
    h.service._issuers.register(Issuer())

    made = asyncio.run(h.service.control_card_create(
        USER, issuer_ref=PROJECT, issuer_kind="project", issuer_label="One", initial_profile="coordinator"))

    assert made["ok"] is True and made.get("started_from", {}).get("profile") == "coordinator", made


# control_card_revoke on the plain path is not exercised here: this harness's
# persistence fake has no `forget` (AttributeError at automation_access.py:2288),
# so a revoke test would fail for the fixture, not for the route.

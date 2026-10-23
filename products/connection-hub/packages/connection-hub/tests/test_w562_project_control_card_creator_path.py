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


# control_card_revoke on the plain path is not exercised here: this harness's
# persistence fake has no `forget` (AttributeError at automation_access.py:2288),
# so a revoke test would fail for the fixture, not for the route.

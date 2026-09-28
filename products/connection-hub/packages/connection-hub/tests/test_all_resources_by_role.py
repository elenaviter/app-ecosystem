"""'All resources' is offered by the signed-in person's role on every Card screen (W379).

The operator, 2026-09-28 ~20:20Z: "it should not be only for admin. it must
show all the resources that are allowed to be shown according to the role of
logged in user!" The descriptor governs: the "All platform and application
APIs" row (resource ``*``) is no longer admin_only, and every row is offered
to a person who may delegate at least one of its grants. It is a W155
regression: the project routes (a project's Control Card, a person's own Card
in a project, an agent's Card through its project) read a Card under its
owner, with no roles, so the row was closed there even to the deployment's
administrator. Each route now offers by the signed-in person's own role, and
a save beyond it is still refused by name.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.automation_access import role_closed
from connection_hub.delegated_credentials.project_authorization import (
    PROJECT_PERSON_CONTROL_READ,
    ProjectAuthorizationDecision,
    ProjectAuthorizationRequest,
    ViewerAuthority,
    with_viewer_authority,
)
from connection_hub.delegated_credentials.project_control_card_access import (
    ProjectControlCardAccess,
)
from test_project_control_card_access import CREATOR, PROJECT, _real_port
from test_resident_profile_cards import _Harness

REGISTERED = "kdcube:role:registered"
PAID = "kdcube:role:paid"
SUPER = "kdcube:role:super-admin"
ALL_ROLES = [REGISTERED, PAID, "kdcube:role:privileged", SUPER]
BOARD = "*/api/integrations/bundles/*/*/problem-board@1-0/public/mcp/problem_board*"

REGISTERED_USER = {"user_id": "user-registered", "roles": [REGISTERED], "permissions": []}
ADMIN = {"user_id": "admin-1", "roles": [SUPER], "permissions": []}
OUTSIDE = {"user_id": "user-outside", "roles": ["kdcube:role:guest"], "permissions": []}


def _connections():
    """The deployed row as W379 aligns it: no admin_only, the roles decide."""

    return {
        "delegated_credentials": {
            "oauth": {
                "enabled": True,
                "capabilities": [
                    {"grant": REGISTERED, "label": "Registered", "delegable_roles": ALL_ROLES},
                    {"grant": PAID, "label": "Paid", "delegable_roles": [PAID, SUPER]},
                    {"grant": SUPER, "label": "Super-admin", "delegable_roles": [SUPER]},
                    {"grant": "work:review", "label": "Review", "delegable_roles": ALL_ROLES},
                ],
                "resources": [
                    {"resource": "*", "label": "All platform and application APIs",
                     "grants": [REGISTERED, PAID, SUPER], "tools": {}},
                    {"resource": BOARD, "label": "Problem Board",
                     "grants": ["work:review"], "tools": {"review.accept": {"grants": ["work:review"]}}},
                ],
            }
        }
    }


def test_a_row_is_closed_only_to_a_role_that_may_delegate_none_of_its_grants():
    row = SimpleNamespace(grants=(REGISTERED, SUPER))
    assert role_closed(row, {REGISTERED}) is False
    assert role_closed(row, {"work:review"}) is True
    assert role_closed(SimpleNamespace(grants=()), set()) is False, "a row that costs nothing stays offered"


@pytest.mark.asyncio
async def test_the_catalog_offers_all_resources_by_the_signed_in_role(tmp_path):
    h = _Harness(tmp_path, connections=_connections())

    registered = {row["resource"]: row for row in await h.service.resource_options(REGISTERED_USER)}
    admin = {row["resource"]: row for row in await h.service.resource_options(ADMIN)}
    outside = {row["resource"]: row for row in await h.service.resource_options(OUTSIDE)}

    assert registered["*"]["grants"] == [REGISTERED], "only the roles this person may delegate"
    assert admin["*"]["grants"] == [REGISTERED, PAID, SUPER]
    assert "*" not in outside, "a role outside the row's grants is not offered it"


def _control_card(h):
    made = asyncio.run(h.service.control_card_create(
        {"user_id": CREATOR, "roles": [SUPER], "permissions": []},
        issuer_ref=PROJECT, issuer_kind="project", issuer_label="One",
    ))
    assert made["ok"] is True, made
    return made["control_card"]["access_id"]


@pytest.mark.parametrize(("viewer", "offered"), [(REGISTERED_USER, True), (ADMIN, True), (OUTSIDE, False)])
def test_a_projects_control_card_offers_all_resources_by_the_readers_role(tmp_path, viewer, offered):
    h = _Harness(tmp_path, connections=_connections())
    control_id = _control_card(h)
    access = ProjectControlCardAccess(h.service, _real_port(control_id))

    opened = asyncio.run(access.get(viewer, control_id=control_id, project_ref=PROJECT))

    assert opened["ok"] is True, opened
    offers = {offer["resource"]: offer for offer in opened["control_card"]["resource_offers"]}
    assert ("*" in offers and offers["*"]["compatible"]) is offered


def test_saving_all_resources_beyond_the_editors_role_is_refused_by_name(tmp_path):
    h = _Harness(tmp_path, connections=_connections())
    control_id = _control_card(h)
    access = ProjectControlCardAccess(h.service, _real_port(control_id))

    beyond = asyncio.run(access.update(
        REGISTERED_USER, control_id=control_id, project_ref=PROJECT, request_id="req-beyond",
        resource_grants={"*": [REGISTERED, SUPER]},
    ))

    assert beyond["ok"] is False and beyond["error"] == "delegated_access_grants_not_delegable"
    assert SUPER in beyond["grants"] and SUPER in beyond["message"]


def _person_decision(grants=("work:review",)):
    request = ProjectAuthorizationRequest.build(
        actor_subject="admin-1", project_ref=PROJECT, target_subject="admin-1",
        operation=PROJECT_PERSON_CONTROL_READ, request_id="req-read",
    )
    return ProjectAuthorizationDecision.allow(request, delegable_grants=grants)


def test_a_persons_card_adds_the_signed_in_role_to_the_projects_answer():
    """The board answers a project's grants and knows no platform roles."""

    decision = with_viewer_authority(
        _person_decision(), ViewerAuthority(grants=(REGISTERED, SUPER), platform_admin=True)
    )
    assert set(decision.delegable_grants) == {"work:review", REGISTERED, SUPER}
    assert decision.platform_admin is True
    refused = ProjectAuthorizationDecision.deny(
        ProjectAuthorizationRequest.build(
            actor_subject="x", project_ref=PROJECT, target_subject="x",
            operation=PROJECT_PERSON_CONTROL_READ, request_id="r",
        ),
        reason="project_actor_role_not_administrative",
    )
    assert with_viewer_authority(refused, ViewerAuthority(grants=(SUPER,), platform_admin=True)) is refused


@pytest.mark.parametrize(("viewer", "expected"), [
    (REGISTERED_USER, {REGISTERED, "work:review"}),
    (ADMIN, {REGISTERED, PAID, SUPER, "work:review"}),
    (OUTSIDE, set()),
])
def test_the_person_card_routes_pass_the_signed_in_role(tmp_path, viewer, expected):
    h = _Harness(tmp_path, connections=_connections())
    seen = {}

    class Lifecycle:
        async def get(self, **kwargs):
            seen.update(kwargs)
            return {"ok": True}

    h.service._project_person_controls = Lifecycle()
    asyncio.run(h.service.project_person_control_get(
        viewer, project_ref=PROJECT, target_subject=viewer["user_id"], request_id="req",
    ))

    assert set(seen["viewer"].grants) == expected
    assert seen["viewer"].platform_admin is (viewer is ADMIN)


def test_an_agent_card_through_its_project_uses_the_actors_admin_fact():
    from connection_hub.delegated_credentials.project_agent_card_access import (
        AgentCardDecision,
        ProjectAgentCardAccess,
    )
    from test_project_agent_card_access import ACCESS, Host, Port

    decision = AgentCardDecision(allowed=True, via="project_admin", grantor_subject="owner-one",
                                 access_id=ACCESS, project_ref=PROJECT, action="write")
    host = Host()
    access = ProjectAgentCardAccess(host, Port({(PROJECT, "write"): decision}))

    asyncio.run(access.update(ADMIN, access_id=ACCESS, project_ref=PROJECT, resource_grants={"*": [SUPER]}))
    asyncio.run(access.update(REGISTERED_USER, access_id=ACCESS, project_ref=PROJECT, resource_grants={"*": [REGISTERED]}))

    platform = [call[1]["_platform_admin"] for call in host.calls if call[0] == "update_access"]
    assert platform == [True, False], "never a fixed False"


# -- W379 follow-up (operator, 2026-09-28 ~20:27Z) -----------------------------
# "the role which the user can pick in the list of roles, for the card holder
# to derive, start from the role which the logged in user has. i.e. if admin
# the list contain all possible roles. but if registered then only that".

PRIVILEGED = "kdcube:role:privileged"
LADDER = [REGISTERED, PAID, PRIVILEGED, SUPER]


def _deployed_ladder():
    """The deployed descriptor's role grants: each is delegable by itself and the roles above it."""

    return {
        "delegated_credentials": {
            "oauth": {
                "enabled": True,
                "capabilities": [
                    {"grant": role, "label": role, "delegable_roles": LADDER[index:]}
                    for index, role in enumerate(LADDER)
                ] + [
                    {"grant": "work:review", "label": "Review", "delegable_roles": LADDER},
                    {"grant": "deployment:manage", "label": "Manage", "delegable_roles": [SUPER]},
                ],
                "resources": [
                    {"resource": "*", "label": "All platform and application APIs",
                     "grants": LADDER, "tools": {}},
                    {"resource": BOARD, "label": "Problem Board", "grants": ["work:review", "deployment:manage"],
                     "tools": {"review.accept": {"grants": ["work:review"]},
                               "deployment.redeploy": {"grants": ["deployment:manage"]}}},
                    {"resource": "urn:kdcube:management:deployment:*:*", "label": "Deployment",
                     "grants": ["deployment:manage"], "tools": {}},
                ],
            }
        }
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(("role", "roles_offered"), [
    (REGISTERED, [REGISTERED]),
    (PAID, [REGISTERED, PAID]),
    (PRIVILEGED, [REGISTERED, PAID, PRIVILEGED]),
    (SUPER, LADDER),
])
async def test_the_role_picker_starts_from_the_signed_in_persons_role(tmp_path, role, roles_offered):
    """Every Card editor draws a row's role choices from resource_options for the signed-in person."""

    h = _Harness(tmp_path, connections=_deployed_ladder())
    person = {"user_id": f"user-{role}", "roles": [role], "permissions": []}

    rows = {row["resource"]: row for row in await h.service.resource_options(person)}

    assert rows["*"]["grants"] == roles_offered


@pytest.mark.asyncio
async def test_rows_and_operations_restricted_to_a_role_are_offered_only_to_it(tmp_path):
    h = _Harness(tmp_path, connections=_deployed_ladder())

    registered = {row["resource"]: row for row in await h.service.resource_options(REGISTERED_USER)}
    admin = {row["resource"]: row for row in await h.service.resource_options(ADMIN)}

    assert "urn:kdcube:management:deployment:*:*" not in registered
    assert "urn:kdcube:management:deployment:*:*" in admin
    assert [op["name"] for op in registered[BOARD]["operations"]] == ["review.accept"]
    assert sorted(op["name"] for op in admin[BOARD]["operations"]) == ["deployment.redeploy", "review.accept"]


def test_a_control_card_saved_through_its_project_is_bounded_by_the_editors_role(tmp_path):
    h = _Harness(tmp_path, connections=_deployed_ladder())
    control_id = _control_card(h)
    access = ProjectControlCardAccess(h.service, _real_port(control_id))
    paid = {"user_id": "user-paid", "roles": [PAID], "permissions": []}

    beyond = asyncio.run(access.update(
        paid, control_id=control_id, project_ref=PROJECT, request_id="req-paid",
        resource_grants={"*": [PAID, PRIVILEGED]},
    ))

    assert beyond["ok"] is False and beyond["error"] == "delegated_access_grants_not_delegable"
    assert PRIVILEGED in beyond["grants"] and PAID not in beyond["grants"]

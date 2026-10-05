"""Operations a person's Control Card is not offered (W360, operator review 2026-09-26).

A service marks an operation ``person_card: false`` when it decides that
operation for a person by role alone (for Problem Board: the coordinator
levers and the people and project Control Card operations). Connection Hub
publishes the mark to the Card editor, which leaves those operations off a
project person's Control Card. It is presentation: an unmarked operation is
offered as before, and the mark never changes a descriptor digest.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.automation_access import AutomationAccessService
from connection_hub.delegated_credentials.catalog.descriptors import (
    operation_descriptor_digest,
    resource_row_digest,
)
from connection_hub.delegated_credentials.oauth.config import (
    _parse_resources,
    oauth_delegated_config_from_connections,
)
from connection_hub.named_service_boundary import NamespaceBoundaryPolicy
from connection_hub.operation_groups import offered_on_person_card, without_grouping

RESOURCE = {
    "resource": "problem_board",
    "label": "Problem Board",
    "tools": {
        "review.assign": {"label": "Route a review", "grants": ["work:review"], "group": "review"},
        "project.coordinator.hand_over": {
            "label": "Hand the coordinator role over",
            "grants": ["work:review"],
            "group": "coordinator",
            "person_card": False,
        },
        "project.people.invite": {"label": "Invite people", "grants": ["work:review"], "person_card": "false"},
    },
}

NAMESPACE = {
    "label": "Work",
    "tools": {
        "action": {
            "grants": ["work:review"],
            "operations": {
                "object.action.review.assign": {"grants": ["work:review"], "group": "review"},
                "object.action.project.coordinator.make": {"grants": ["work:review"], "person_card": False},
            },
        },
        "levers": {"grants": ["work:review"], "person_card": False},
    },
}


def test_only_an_explicit_false_leaves_an_operation_off_a_persons_card() -> None:
    assert offered_on_person_card({}) is True
    assert offered_on_person_card({"person_card": True}) is True
    assert offered_on_person_card({"person_card": "true"}) is True
    assert offered_on_person_card(None) is True
    assert offered_on_person_card({"person_card": False}) is False
    assert offered_on_person_card({"person_card": "false"}) is False


def test_an_outer_tool_keeps_its_mark() -> None:
    (resource,) = _parse_resources([RESOURCE])
    assert {tool.name: tool.person_card for tool in resource.tools} == {
        "review.assign": True,
        "project.coordinator.hand_over": False,
        "project.people.invite": False,
    }


def test_the_mark_never_changes_a_descriptor_digest() -> None:
    (marked,) = _parse_resources([RESOURCE])
    plain = dict(RESOURCE, tools={
        name: {key: value for key, value in tool.items() if key != "person_card"}
        for name, tool in RESOURCE["tools"].items()
    })
    (unmarked,) = _parse_resources([plain])
    assert resource_row_digest(marked) == resource_row_digest(unmarked)
    assert [operation_descriptor_digest(tool) for tool in marked.tools] == [
        operation_descriptor_digest(tool) for tool in unmarked.tools
    ]
    with_named = dict(RESOURCE, named_services={"namespaces": {"work": NAMESPACE}})
    without_mark = dict(RESOURCE, named_services=without_grouping({"namespaces": {"work": NAMESPACE}}))
    assert "person_card" not in str(without_mark["named_services"])
    assert resource_row_digest(_parse_resources([with_named])[0]) == resource_row_digest(
        _parse_resources([without_mark])[0]
    )


def test_a_named_service_publishes_the_mark_on_tools_and_operations() -> None:
    public = NamespaceBoundaryPolicy.from_config("work", NAMESPACE).to_public_dict()
    operations = public["tools"]["action"]["operations"]
    assert "person_card" not in operations["object.action.review.assign"]
    assert operations["object.action.project.coordinator.make"]["person_card"] is False
    assert "person_card" not in public["tools"]["action"]
    assert public["tools"]["levers"]["person_card"] is False


@pytest.mark.asyncio
async def test_the_card_editor_option_carries_the_mark() -> None:
    connections = {
        "delegated_credentials": {
            "oauth": {
                "enabled": True,
                "capabilities": [
                    {"grant": "work:review", "label": "Review", "delegable_roles": ["kdcube:role:registered"]},
                ],
                "resources": [dict(RESOURCE, named_services={"namespaces": {"work": NAMESPACE}})],
            }
        }
    }

    class _Resolver:
        async def resolve_active(self):
            return SimpleNamespace(version="catalog-1", connections=connections)

    service = AutomationAccessService(
        redis=object(),
        tenant="tenant-a",
        project="project-a",
        config=oauth_delegated_config_from_connections(connections),
        catalog_resolver=_Resolver(),
    )
    (option,) = await service.resource_options(
        {"user_id": "user-1", "roles": ["kdcube:role:registered"], "permissions": []},
        _delegable_grants=["work:review"],
    )
    assert {row["name"]: row.get("person_card", True) for row in option["operations"]} == {
        "review.assign": True,
        "project.coordinator.hand_over": False,
        "project.people.invite": False,
    }
    (namespace,) = option["named_services"]
    assert namespace["tools"]["action"]["operations"]["object.action.project.coordinator.make"]["person_card"] is False


@pytest.mark.asyncio
async def test_project_person_display_catalog_is_complete_when_viewer_cannot_delegate_admin() -> None:
    marked_admin = dict(RESOURCE["tools"]["project.coordinator.hand_over"], grants=["work:admin"])
    resource = dict(RESOURCE, tools={
        **RESOURCE["tools"],
        "project.coordinator.hand_over": marked_admin,
    })
    connections = {"delegated_credentials": {"oauth": {
        "enabled": True,
        "capabilities": [
            {"grant": "work:review", "delegable_roles": ["kdcube:role:registered"]},
            {"grant": "work:admin", "delegable_roles": ["kdcube:role:admin"]},
        ],
        "resources": [resource],
    }}}

    class _Resolver:
        async def resolve_active(self):
            return SimpleNamespace(version="catalog-1", connections=connections)

    service = AutomationAccessService(
        redis=object(), tenant="tenant-a", project="project-a",
        config=oauth_delegated_config_from_connections(connections),
        catalog_resolver=_Resolver(),
    )
    filtered = await service.resource_options(
        {"user_id": "viewer", "roles": ["kdcube:role:registered"], "permissions": []},
        _delegable_grants=["work:review"],
    )
    assert "project.coordinator.hand_over" not in {
        tool["name"] for tool in filtered[0]["operations"]
    }

    display = await service.person_role_display_catalog(
        owner_subject="project:synthetic", card_resources=["problem_board", "other"],
    )
    assert display is not None
    assert len(display) == 1
    assert {tool["name"] for tool in display[0]["operations"]} == {
        "project.coordinator.hand_over", "project.people.invite",
    }
    assert all("grants" not in tool for tool in display[0]["operations"])

"""Operation groups declared in the service catalog (W260, operator ruling 2026-09-26).

A Card editor groups a service's operations by what the service declares
beside them, so no client builds its own grouping. Grouping is presentation:
it never changes a descriptor digest, so regrouping never raises drift.
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
from connection_hub.operation_groups import parse_operation_groups, without_grouping

RESOURCE = {
    "resource": "problem_board",
    "label": "Problem Board",
    "operation_groups": {
        "work": {"label": "Work", "order": 20},
        "review": {"label": "Review", "order": 10},
        "people": "People",
    },
    "tools": {
        "review.approve": {"label": "Approve a review", "grants": ["work:review"], "group": "review"},
        "work.report": {"label": "Report work", "grants": ["work:observe"], "group": "work"},
        "project.people.list": {"label": "List people", "grants": ["work:observe"]},
    },
}


def test_groups_sort_by_order_then_declaration_and_a_bare_string_is_the_label() -> None:
    assert parse_operation_groups(RESOURCE["operation_groups"]) == (
        {"group": "people", "label": "People", "order": 2.0},
        {"group": "review", "label": "Review", "order": 10.0},
        {"group": "work", "label": "Work", "order": 20.0},
    )
    assert parse_operation_groups(None) == ()
    assert parse_operation_groups({"": "x", "a": {"order": "bad"}}) == ({"group": "a", "label": "a", "order": 1.0},)


def test_a_resource_keeps_each_operations_group_and_its_group_map() -> None:
    (resource,) = _parse_resources([RESOURCE])
    groups = {tool.name: tool.group for tool in resource.tools}
    assert groups == {"review.approve": "review", "work.report": "work", "project.people.list": ""}
    assert [group["group"] for group in resource.operation_groups] == ["people", "review", "work"]


def test_grouping_never_changes_a_descriptor_digest() -> None:
    (grouped,) = _parse_resources([RESOURCE])
    plain = {key: value for key, value in RESOURCE.items() if key != "operation_groups"}
    plain["tools"] = {
        name: {key: value for key, value in tool.items() if key != "group"}
        for name, tool in RESOURCE["tools"].items()
    }
    (ungrouped,) = _parse_resources([plain])
    assert resource_row_digest(grouped) == resource_row_digest(ungrouped)
    assert [operation_descriptor_digest(tool) for tool in grouped.tools] == [
        operation_descriptor_digest(tool) for tool in ungrouped.tools
    ]


def test_named_service_grouping_is_published_and_kept_out_of_the_row_digest() -> None:
    namespace = {
        "label": "Work",
        "operation_groups": {"plan": {"label": "Plan", "order": 1}},
        "tools": {
            "plan": {
                "grants": ["work:coordinate"],
                "group": "plan",
                "operations": {"item.update": {"grants": ["work:coordinate"], "group": "plan"}},
            }
        },
    }
    public = NamespaceBoundaryPolicy.from_config("work", namespace).to_public_dict()
    assert public["operation_groups"] == [{"group": "plan", "label": "Plan", "order": 1.0}]
    assert public["tools"]["plan"]["group"] == "plan"
    assert public["tools"]["plan"]["operations"]["item.update"]["group"] == "plan"

    assert without_grouping({"namespaces": {"work": namespace}}) == {
        "namespaces": {
            "work": {
                "label": "Work",
                "tools": {"plan": {"grants": ["work:coordinate"], "operations": {"item.update": {"grants": ["work:coordinate"]}}}},
            }
        }
    }
    grouped = dict(RESOURCE, named_services={"namespaces": {"work": namespace}})
    ungrouped = dict(RESOURCE, named_services=without_grouping({"namespaces": {"work": namespace}}))
    assert resource_row_digest(_parse_resources([grouped])[0]) == resource_row_digest(_parse_resources([ungrouped])[0])



@pytest.mark.asyncio
async def test_the_card_editor_option_carries_each_operations_group_and_the_group_order() -> None:
    connections = {
        "delegated_credentials": {
            "oauth": {
                "enabled": True,
                "capabilities": [
                    {"grant": "work:review", "label": "Review", "delegable_roles": ["kdcube:role:registered"]},
                    {"grant": "work:observe", "label": "Observe", "delegable_roles": ["kdcube:role:registered"]},
                ],
                "resources": [RESOURCE],
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
        _delegable_grants=["work:review", "work:observe"],
    )
    assert {row["name"]: row.get("group", "") for row in option["operations"]} == {
        "review.approve": "review", "work.report": "work", "project.people.list": "",
    }
    assert [group["group"] for group in option["operation_groups"]] == ["people", "review", "work"]

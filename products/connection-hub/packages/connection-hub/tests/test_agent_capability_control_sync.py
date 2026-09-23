from __future__ import annotations

import dataclasses
import time

import pytest

from connection_hub.delegated_credentials import agent_capability_sync
from connection_hub.delegated_credentials.agent_capability_control import (
    AGENT_DESCRIPTOR_ACCEPTANCE_KIND,
)
from connection_hub.delegated_credentials.agent_capability_sync import (
    AGENT_CAPABILITY_CARD_LEASE_SECONDS,
)
from connection_hub.delegated_credentials.agent_capability_policy import (
    AGENT_CAPABILITY_AUTHORITY_PROPERTY,
    AGENT_CAPABILITY_DEFAULTS_PROPERTY,
    AGENT_CAPABILITY_METADATA_PROPERTY,
    AGENT_CAPABILITY_SELECTION_PROPERTY,
    AGENT_CAPABILITY_METADATA_SCHEMA,
    AGENT_CAPABILITY_POLICY_SCHEMA,
    AGENT_DESCRIPTOR_CONTROL_PROPERTY,
    CAPABILITY_ALLOWED_SELECTED,
    CAPABILITY_ALLOWED_UNSELECTED,
    CAPABILITY_NOT_ALLOWED,
)
from connection_hub.delegated_credentials.application_resources import (
    application_resource,
)
from connection_hub.delegated_credentials.application_operation_policy import (
    APPLICATION_OPERATIONS_PROPERTY,
)
from connection_hub.delegated_credentials.automation_access import (
    AutomationAccessService,
    _descriptor_agent_resource_options,
    _descriptor_control_resource_options,
)
from connection_hub.delegated_credentials.cards.model import (
    CARD_STATE_ACTIVE,
    CardAuthority,
    CardCredentialHandles,
    authority_is_credentialless,
    authority_is_usable,
)
from connection_hub.delegated_credentials.cards.service import CardConflict
from connection_hub.delegated_credentials.cards.store import subject_hash_for
from connection_hub.delegated_credentials.catalog.models import CatalogDocument
from connection_hub.delegated_credentials.controls.effective import (
    effective_card_authority,
)
from connection_hub.delegated_credentials.conversation_target_policy import (
    conversation_targets,
)
from connection_hub.delegated_credentials.oauth.config import (
    oauth_delegated_config_from_connections,
)

TENANT = "tenant-a"
PROJECT = "project-a"
APPLICATION = "problem-board@1-0"
AGENT = "worker"
OWNER = "user-1"
RESOURCE = application_resource(
    tenant=TENANT,
    project=PROJECT,
    application=APPLICATION,
    agent=AGENT,
)
NAMED_RESOURCE = "https://example.test/mcp/named-services"
REVIEW_RESOURCE = "https://example.test/mcp/review"
OTHER_MCP_RESOURCE = "https://example.test/mcp/unselected"
REMOTE_MCP_ROOT = "*/api/integrations/bundles/*/*/connection-hub@1-0/public/mcp/remote_mcp_proxy*"
REMOTE_MCP_RESOURCE = "urn:connection-hub:remote-mcp:mcp_0123456789abcdef01234567"


class _Persistence:
    def __init__(self) -> None:
        self.records: dict[str, tuple[CardAuthority, CardCredentialHandles]] = {}
        self.persisted: list[str] = []

    def _owned(self, authority: CardAuthority, subject_hash: str) -> bool:
        return subject_hash_for(authority.grantor_subject) == subject_hash

    async def load(self, access_id: str, *, subject_hash: str):
        held = self.records.get(access_id)
        if held is None:
            return None
        authority, handles = held
        if (
            not self._owned(authority, subject_hash)
            or authority.state != CARD_STATE_ACTIVE
            or (
                not authority_is_credentialless(authority)
                and authority.expires_at <= int(time.time())
            )
        ):
            return None
        return authority, handles

    async def load_current(self, access_id: str, *, subject_hash: str):
        held = self.records.get(access_id)
        if held is None or not self._owned(held[0], subject_hash):
            return None
        return held

    async def current_revision(self, access_id: str, *, subject_hash: str) -> int:
        held = await self.load_current(access_id, subject_hash=subject_hash)
        return held[0].card_revision if held is not None else 0

    async def persist(
        self,
        authority: CardAuthority,
        handles: CardCredentialHandles,
        *,
        subject_hash: str,
        expected_revision: int,
    ) -> None:
        if subject_hash_for(authority.grantor_subject) != subject_hash:
            raise AssertionError("wrong owner")
        current = self.records.get(authority.access_id)
        current_revision = current[0].card_revision if current is not None else 0
        if expected_revision != current_revision:
            raise CardConflict(
                "card_revision_moved",
                current_revision=current_revision,
            )
        self.records[authority.access_id] = (authority, handles)
        self.persisted.append(authority.access_id)


def _policy(*tools: str) -> dict:
    return {
        "schema": AGENT_CAPABILITY_POLICY_SCHEMA,
        "resource": RESOURCE,
        "capabilities": {"tools": list(tools)},
    }


def _policy_with_capabilities(**capabilities: list[str]) -> dict:
    return {
        "schema": AGENT_CAPABILITY_POLICY_SCHEMA,
        "resource": RESOURCE,
        "capabilities": capabilities,
    }


def _policy_with_targets(*targets: str) -> dict:
    return {
        "schema": AGENT_CAPABILITY_POLICY_SCHEMA,
        "resource": RESOURCE,
        "capabilities": {"conversation_targets": list(targets)},
    }


def _policy_with_named_operations(*operations: str) -> dict:
    return {
        "schema": AGENT_CAPABILITY_POLICY_SCHEMA,
        "resource": RESOURCE,
        "capabilities": {
            "named_services": ["slack"],
            "named_service_operations": [
                f"slack/{operation}" for operation in operations
            ],
        },
    }


def _policy_with_mcp_tools(*tools: str) -> dict:
    return {
        "schema": AGENT_CAPABILITY_POLICY_SCHEMA,
        "resource": RESOURCE,
        "capabilities": {
            "mcp_servers": ["review"],
            "mcp_tools": [f"review/{tool}" for tool in tools],
        },
    }


def _service(
    *,
    application_apis: bool = False,
    named_services: bool = False,
    review_mcp: bool = False,
) -> tuple[AutomationAccessService, _Persistence]:
    resources = []
    if application_apis:
        resources.append(
            {
                "resource": "*",
                "label": "Application APIs",
                "grants": ["kdcube:role:super-admin"],
            }
        )
    if named_services:
        resources.append(
            {
                "resource": NAMED_RESOURCE,
                "label": "Named services",
                "grants": [
                    "named_services:use",
                    "slack:read",
                    "slack:post",
                ],
                "named_services": {
                    "namespaces": {
                        "slack": {
                            "tools": {
                                "objects": {
                                    "grants": ["named_services:use"],
                                    "operations": {
                                        "object.list": {
                                            "grants": [
                                                "named_services:use",
                                                "slack:read",
                                            ],
                                        },
                                        "object.action.post_message": {
                                            "grants": [
                                                "named_services:use",
                                                "slack:post",
                                            ],
                                        },
                                    }
                                }
                            }
                        }
                    }
                },
                "tools": [
                    {
                        "name": "named_services_call",
                        "grants": ["named_services:use"],
                    }
                ],
            }
        )
    if review_mcp:
        resources.append(
            {
                "resource": REVIEW_RESOURCE,
                "label": "Review MCP",
                "grants": ["review:use"],
                "tools": [
                    {"name": "review_accept", "grants": ["review:use"]},
                    {"name": "review_cancel", "grants": ["review:use"]},
                ],
            }
        )
    grants = sorted(
        {
            grant
            for resource in resources
            for grant in resource.get("grants", ())
        }
    )
    document = CatalogDocument.build(
        {
            "delegated_credentials": {
                "oauth": {
                    "enabled": True,
                    "capabilities": [
                        {
                            "grant": grant,
                            "label": grant,
                            "delegable_roles": ["kdcube:role:super-admin"],
                        }
                        for grant in grants
                    ],
                    "resources": resources,
                }
            }
        }
    )

    class _Resolver:
        async def resolve_active(self):
            return document

    persistence = _Persistence()
    service = AutomationAccessService(
        redis=None,
        tenant=TENANT,
        project=PROJECT,
        config=oauth_delegated_config_from_connections(document.connections),
        catalog_resolver=_Resolver(),
        card_persistence=persistence,
    )
    return service, persistence


async def _sync(
    service: AutomationAccessService,
    *,
    revision: str,
    authority: tuple[str, ...],
    catalog: tuple[str, ...],
    selection: tuple[str, ...] | None = None,
    replace_selection: bool = False,
) -> dict:
    return await service.sync_agent_capability_control(
        {"user_id": OWNER},
        application=APPLICATION,
        agent_id=AGENT,
        descriptor_revision=revision,
        descriptor_payload={"revision": revision, "tools": list(catalog)},
        capability_authority=_policy(*authority),
        capability_catalog=_policy(*catalog),
        selected_capabilities=(_policy(*selection) if selection is not None else None),
        replace_selection=replace_selection,
        conversation_target_resources=(
            application_resource(
                tenant=TENANT,
                project=PROJECT,
                application="*",
                agent="*",
            ),
        ),
        issuer_label="Problem Board worker",
    )


@pytest.mark.asyncio
async def test_descriptor_control_offers_only_descriptor_serializable_services() -> None:
    service, _persistence = _service()
    created = await _sync(
        service,
        revision="descriptor-r1",
        authority=("tool.old",),
        catalog=("tool.old",),
        selection=("tool.old",),
    )
    properties = created["control_card"]["properties"]
    options = [
        {"resource": "*", "kind": "catalog", "label": "All platform and application APIs"},
        {"resource": "urn:kdcube:management:deployment:*:*", "kind": "catalog", "label": "Deployment"},
        {"resource": NAMED_RESOURCE, "kind": "catalog", "named_services": [{"namespace": "slack"}]},
        {"resource": REVIEW_RESOURCE, "kind": "catalog"},
        {"resource": REMOTE_MCP_ROOT, "kind": "catalog", "resource_selection": True},
        {"resource": REMOTE_MCP_RESOURCE, "kind": "remote_mcp"},
    ]

    offered = _descriptor_control_resource_options(properties, options)

    assert [row["resource"] for row in offered] == [
        NAMED_RESOURCE,
        REVIEW_RESOURCE,
        REMOTE_MCP_ROOT,
    ]


@pytest.mark.asyncio
async def test_descriptor_agent_offers_exact_control_resources_and_allowed_user_mcp() -> None:
    service, _persistence = _service()
    created = await _sync(
        service,
        revision="descriptor-r1",
        authority=("tool.old",),
        catalog=("tool.old",),
        selection=("tool.old",),
    )
    control = dict(created["control_card"])
    properties = dict(control["properties"])
    properties[AGENT_CAPABILITY_AUTHORITY_PROPERTY] = _policy_with_capabilities(
        resource_families=["user_external_mcp"],
    )
    properties[AGENT_CAPABILITY_METADATA_PROPERTY] = {
        "schema": AGENT_CAPABILITY_METADATA_SCHEMA,
        "resource": RESOURCE,
        "entries": {
            "resource_families": {
                "user_external_mcp": {
                    "title": "My MCP connectors",
                    "resource_patterns": ["urn:connection-hub:remote-mcp:*"],
                }
            }
        },
    }
    control["properties"] = properties
    control["resource_grants"] = {
        NAMED_RESOURCE: ["named_services:use"],
        REVIEW_RESOURCE: ["review:use"],
    }
    options = [
        {"resource": "*", "kind": "catalog"},
        {"resource": NAMED_RESOURCE, "kind": "catalog"},
        {"resource": REVIEW_RESOURCE, "kind": "catalog"},
        {"resource": OTHER_MCP_RESOURCE, "kind": "catalog"},
        {
            "resource": REMOTE_MCP_ROOT,
            "kind": "catalog",
            "grants": ["external_mcp:use"],
            "resource_selection": True,
            "selectable_resources": [REMOTE_MCP_RESOURCE],
        },
        {"resource": REMOTE_MCP_RESOURCE, "kind": "remote_mcp"},
    ]

    offered, roots = _descriptor_agent_resource_options(control, options)

    assert [row["resource"] for row in offered] == [
        NAMED_RESOURCE,
        REVIEW_RESOURCE,
        REMOTE_MCP_ROOT,
        REMOTE_MCP_RESOURCE,
    ]
    assert roots == [REMOTE_MCP_ROOT]


@pytest.mark.asyncio
async def test_descriptor_agent_does_not_offer_catalog_wildcard_wider_than_control() -> None:
    service, _persistence = _service()
    created = await _sync(
        service,
        revision="descriptor-r1",
        authority=("tool.old",),
        catalog=("tool.old",),
        selection=("tool.old",),
    )
    exact = application_resource(
        tenant=TENANT,
        project=PROJECT,
        application="one",
        agent="api",
    )
    wildcard = application_resource(
        tenant=TENANT,
        project=PROJECT,
        application="*",
        agent="*",
    )
    other = application_resource(
        tenant=TENANT,
        project=PROJECT,
        application="two",
        agent="api",
    )
    control = dict(created["control_card"])
    control["resource_grants"] = {exact: ["application:use"]}

    offered, roots = _descriptor_agent_resource_options(
        control,
        [
            {"resource": wildcard, "kind": "catalog"},
            {"resource": exact, "kind": "catalog"},
            {"resource": other, "kind": "catalog"},
        ],
    )

    assert [row["resource"] for row in offered] == [exact]
    assert roots == []


@pytest.mark.asyncio
async def test_descriptor_sync_is_stable_and_new_capabilities_are_unselected() -> None:
    service, persistence = _service()

    created = await _sync(
        service,
        revision="descriptor-r1",
        authority=("tool.old", "tool.new"),
        catalog=("tool.old", "tool.new", "tool.blocked"),
        selection=("tool.old", "tool.blocked"),
    )

    assert created["ok"] is True
    assert created["control_changed"] is True
    assert created["card_changed"] is True
    assert created["projection"]["capabilities"] == {"tools": ["tool.old"]}
    assert created["states"] == {
        "tools": {
            "tool.blocked": CAPABILITY_NOT_ALLOWED,
            "tool.new": CAPABILITY_ALLOWED_UNSELECTED,
            "tool.old": CAPABILITY_ALLOWED_SELECTED,
        }
    }
    assert len(persistence.records) == 2
    assert len(persistence.persisted) == 2
    resident_id = created["card"]["access_id"]
    control_id = created["control_card"]["access_id"]
    resident = persistence.records[resident_id][0]
    assert resident.control_card is not None
    assert resident.control_card.control_id == control_id
    assert resident.resource_acceptance[RESOURCE].kind == (
        AGENT_DESCRIPTOR_ACCEPTANCE_KIND
    )

    replay = await _sync(
        service,
        revision="descriptor-r1",
        authority=("tool.old", "tool.new"),
        catalog=("tool.old", "tool.new", "tool.blocked"),
    )

    assert replay["ok"] is True
    assert replay["control_changed"] is False
    assert replay["card_changed"] is False
    assert len(persistence.persisted) == 2

    added = await _sync(
        service,
        revision="descriptor-r2",
        authority=("tool.old", "tool.new", "tool.future"),
        catalog=("tool.old", "tool.new", "tool.future", "tool.blocked"),
    )

    assert added["control_changed"] is True
    assert added["card_changed"] is False
    assert added["states"]["tools"]["tool.future"] == (CAPABILITY_ALLOWED_UNSELECTED)
    assert added["projection"]["capabilities"] == {"tools": ["tool.old"]}


@pytest.mark.asyncio
async def test_agent_card_selection_update_is_revision_checked_and_ceiling_bounded() -> None:
    service, _persistence = _service()
    created = await _sync(
        service,
        revision="descriptor-r1",
        authority=("tool.old", "tool.new"),
        catalog=("tool.old", "tool.new", "tool.outside"),
        selection=("tool.old",),
    )

    changed = await service.update_agent_capability_selection(
        {"user_id": OWNER},
        access_id=created["card"]["access_id"],
        selected_capabilities=_policy("tool.new", "tool.outside"),
        expected_card_revision=created["card"]["card_revision"],
    )

    assert changed["ok"] is True
    assert changed["card_changed"] is True
    assert changed["selection"]["capabilities"] == {"tools": ["tool.new"]}
    assert changed["projection"] == changed["selection"]
    assert changed["access"]["card_revision"] == created["card"]["card_revision"] + 1

    stale = await service.update_agent_capability_selection(
        {"user_id": OWNER},
        access_id=created["card"]["access_id"],
        selected_capabilities=_policy("tool.old"),
        expected_card_revision=created["card"]["card_revision"],
    )

    assert stale["ok"] is False
    assert stale["error"] == "agent_capability_card_revision_conflict"
    assert stale["status"] == 409


@pytest.mark.asyncio
async def test_expired_capability_lease_renews_the_same_card_and_selection(
    monkeypatch,
) -> None:
    service, persistence = _service()
    created_at = 2_000_000_000
    monkeypatch.setattr(agent_capability_sync.time, "time", lambda: created_at)
    created = await _sync(
        service,
        revision="descriptor-r1",
        authority=("tool.old", "tool.new"),
        catalog=("tool.old", "tool.new"),
        selection=("tool.old",),
    )
    access_id = created["card"]["access_id"]
    first_revision = created["card"]["card_revision"]
    first_authority, first_handles = persistence.records[access_id]
    assert first_handles.empty is True
    assert created["card"]["expires_at"] == (
        created_at + AGENT_CAPABILITY_CARD_LEASE_SECONDS
    )

    renewed_at = created_at + AGENT_CAPABILITY_CARD_LEASE_SECONDS + 1
    assert authority_is_usable(first_authority, renewed_at) is False
    monkeypatch.setattr(agent_capability_sync.time, "time", lambda: renewed_at)
    renewed = await _sync(
        service,
        revision="descriptor-r1",
        authority=("tool.old", "tool.new"),
        catalog=("tool.old", "tool.new"),
    )

    assert renewed["card_changed"] is True
    assert renewed["card"]["access_id"] == access_id
    assert renewed["card"]["card_revision"] == first_revision + 1
    assert renewed["card"]["expires_at"] == (
        renewed_at + AGENT_CAPABILITY_CARD_LEASE_SECONDS
    )
    assert renewed["selection"]["capabilities"] == {"tools": ["tool.old"]}
    assert renewed["projection"]["capabilities"] == {"tools": ["tool.old"]}
    renewed_authority, renewed_handles = persistence.records[access_id]
    assert authority_is_usable(renewed_authority, renewed_at) is True
    assert renewed_handles.empty is True


@pytest.mark.asyncio
async def test_effective_conversation_targets_come_from_capability_projection() -> None:
    service, persistence = _service()
    target = application_resource(
        tenant=TENANT,
        project=PROJECT,
        application="*",
        agent="*",
    )

    selected = await service.sync_agent_capability_control(
        {"user_id": OWNER},
        application=APPLICATION,
        agent_id=AGENT,
        descriptor_revision="descriptor-r1",
        descriptor_payload={"revision": "descriptor-r1"},
        capability_authority=_policy_with_targets(target),
        capability_catalog=_policy_with_targets(target),
        selected_capabilities=_policy_with_targets(target),
        replace_selection=True,
        conversation_target_resources=(target,),
    )

    resident = persistence.records[selected["card"]["access_id"]][0]
    control = persistence.records[selected["control_card"]["access_id"]][0]
    effective = effective_card_authority(resident, control)

    assert selected["projection"]["capabilities"] == {
        "conversation_targets": [target]
    }
    assert conversation_targets(effective.properties) == (target,)

    cleared = await service.sync_agent_capability_control(
        {"user_id": OWNER},
        application=APPLICATION,
        agent_id=AGENT,
        descriptor_revision="descriptor-r1",
        descriptor_payload={"revision": "descriptor-r1"},
        capability_authority=_policy_with_targets(target),
        capability_catalog=_policy_with_targets(target),
        selected_capabilities=_policy_with_targets(),
        replace_selection=True,
        conversation_target_resources=(target,),
    )
    effective = effective_card_authority(
        persistence.records[cleared["card"]["access_id"]][0],
        persistence.records[cleared["control_card"]["access_id"]][0],
    )
    assert conversation_targets(effective.properties) == ()


@pytest.mark.asyncio
async def test_removed_capability_is_hidden_then_restored_from_user_selection() -> None:
    service, _persistence = _service()
    await _sync(
        service,
        revision="descriptor-r1",
        authority=("tool.old", "tool.new"),
        catalog=("tool.old", "tool.new"),
        selection=("tool.old",),
    )

    removed = await _sync(
        service,
        revision="descriptor-r2",
        authority=("tool.new",),
        catalog=("tool.old", "tool.new"),
    )
    assert removed["selection"]["capabilities"] == {"tools": ["tool.old"]}
    assert removed["projection"]["capabilities"] == {"tools": []}
    assert removed["states"]["tools"]["tool.old"] == CAPABILITY_NOT_ALLOWED

    restored = await _sync(
        service,
        revision="descriptor-r3",
        authority=("tool.old", "tool.new"),
        catalog=("tool.old", "tool.new"),
    )
    assert restored["projection"]["capabilities"] == {"tools": ["tool.old"]}
    assert restored["states"]["tools"]["tool.old"] == (CAPABILITY_ALLOWED_SELECTED)


@pytest.mark.asyncio
async def test_picker_replacement_changes_visible_entries_only() -> None:
    service, _persistence = _service()
    await _sync(
        service,
        revision="descriptor-r1",
        authority=("tool.old", "tool.new"),
        catalog=("tool.old", "tool.new", "tool.hidden"),
        selection=("tool.old", "tool.hidden"),
    )

    changed = await _sync(
        service,
        revision="descriptor-r1",
        authority=("tool.old", "tool.new"),
        catalog=("tool.old", "tool.new", "tool.hidden"),
        selection=("tool.new", "tool.outside"),
        replace_selection=True,
    )

    assert changed["control_changed"] is False
    assert changed["card_changed"] is True
    assert changed["selection"]["capabilities"] == {
        "tools": ["tool.hidden", "tool.new"]
    }
    assert changed["projection"]["capabilities"] == {"tools": ["tool.new"]}

    replay = await _sync(
        service,
        revision="descriptor-r1",
        authority=("tool.old", "tool.new"),
        catalog=("tool.old", "tool.new", "tool.hidden"),
        selection=("tool.new", "tool.outside"),
        replace_selection=True,
    )
    assert replay["card_changed"] is False


@pytest.mark.asyncio
async def test_descriptor_ceiling_does_not_require_the_user_to_hold_its_roles() -> None:
    service, persistence = _service(application_apis=True)
    operation = (
        "urn:kdcube:application-operation:problem-board%401-0:"
        "api.operations.post.agent_capabilities"
    )

    result = await service.sync_agent_capability_control(
        {"user_id": OWNER, "roles": []},
        application=APPLICATION,
        agent_id=AGENT,
        descriptor_revision="descriptor-r1",
        descriptor_payload={"revision": "descriptor-r1"},
        capability_authority=_policy("tool.old"),
        selected_capabilities=_policy("tool.old"),
        resource_grants={"*": ["kdcube:role:registered"]},
        resource_operations={"*": [operation]},
        properties={
            APPLICATION_OPERATIONS_PROPERTY: {
                "schema": "kdcube.application_operations.v2",
                "mode": "selected",
                "default_role": "kdcube:role:registered",
                "operation_roles": {},
            }
        },
    )

    assert result["ok"] is True, result
    control = persistence.records[result["control_card"]["access_id"]][0]
    assert control.resource_grants == {"*": ("kdcube:role:registered",)}
    assert control.resource_operations == {"*": (operation,)}


@pytest.mark.asyncio
async def test_agent_card_uses_standard_selected_authority_inside_control() -> None:
    service, persistence = _service(named_services=True)
    control_operations = {
        NAMED_RESOURCE: {
            "slack": ["object.list", "object.action.post_message"],
        }
    }
    selected_operations = {
        NAMED_RESOURCE: {"slack": ["object.list"]},
    }

    result = await service.sync_agent_capability_control(
        {"user_id": OWNER},
        application=APPLICATION,
        agent_id=AGENT,
        descriptor_revision="descriptor-r1",
        descriptor_payload={"revision": "descriptor-r1"},
        capability_authority=_policy("tool.old"),
        selected_capabilities=_policy("tool.old"),
        resource_grants={
            NAMED_RESOURCE: [
                "named_services:use",
                "slack:read",
                "slack:post",
            ]
        },
        named_service_operations=control_operations,
        selected_resource_grants={
            NAMED_RESOURCE: ["named_services:use", "slack:read"],
        },
        selected_named_service_operations=selected_operations,
    )

    assert result["ok"] is True, result
    control = persistence.records[result["control_card"]["access_id"]][0]
    resident = persistence.records[result["card"]["access_id"]][0]
    assert control.named_service_operations.to_stored() == control_operations
    assert control.account_scope == {}
    assert resident.resource_grants == {
        NAMED_RESOURCE: ("named_services:use", "slack:read"),
    }
    assert resident.named_service_operations.to_stored() == selected_operations
    assert resident.account_scope == {}
    assert NAMED_RESOURCE in resident.resource_acceptance
    assert RESOURCE in resident.resource_acceptance

    narrowed = await service.sync_agent_capability_control(
        {"user_id": OWNER},
        application=APPLICATION,
        agent_id=AGENT,
        descriptor_revision="descriptor-r1",
        descriptor_payload={"revision": "descriptor-r1"},
        capability_authority=_policy("tool.old"),
        selected_capabilities=_policy("tool.old"),
        replace_selection=True,
        resource_grants={
            NAMED_RESOURCE: [
                "named_services:use",
                "slack:read",
                "slack:post",
            ]
        },
        named_service_operations=control_operations,
        selected_resource_grants={},
        selected_named_service_operations={},
    )

    assert narrowed["card_changed"] is True
    updated = persistence.records[result["card"]["access_id"]][0]
    assert updated.resource_grants == {}
    assert updated.named_service_operations.is_none
    assert RESOURCE in updated.resource_acceptance
    assert NAMED_RESOURCE not in updated.resource_acceptance


@pytest.mark.asyncio
async def test_descriptor_request_projects_named_services_into_both_cards() -> None:
    service, persistence = _service(named_services=True)
    authority = _policy_with_named_operations(
        "object.list",
        "object.action.post_message",
    )
    selected = _policy_with_named_operations("object.list")
    descriptor_payload = {
        "revision": "descriptor-r1",
        "standard_authority": {
            "resources": [],
            "named_services": [
                {"namespace": "slack", "operations": ["object"]},
            ],
            "resource_families": [],
        },
    }

    result = await service.sync_agent_capability_control(
        {"user_id": OWNER},
        application=APPLICATION,
        agent_id=AGENT,
        descriptor_revision="descriptor-r1",
        descriptor_payload=descriptor_payload,
        capability_authority=authority,
        capability_catalog=authority,
        selected_capabilities=selected,
    )

    assert result["ok"] is True, result
    control = persistence.records[result["control_card"]["access_id"]][0]
    resident = persistence.records[result["card"]["access_id"]][0]
    assert control.resource_grants == {
        NAMED_RESOURCE: (
            "named_services:use",
            "slack:post",
            "slack:read",
        ),
    }
    assert control.resource_operations == {
        NAMED_RESOURCE: ("named_services_call",),
    }
    assert control.named_service_operations.to_stored() == {
        NAMED_RESOURCE: {
            "slack": ["object.action.post_message", "object.list"],
        },
    }
    assert resident.resource_grants == {
        NAMED_RESOURCE: ("named_services:use", "slack:read"),
    }
    assert resident.resource_operations == {
        NAMED_RESOURCE: ("named_services_call",),
    }
    assert resident.named_service_operations.to_stored() == {
        NAMED_RESOURCE: {"slack": ["object.list"]},
    }


@pytest.mark.asyncio
async def test_descriptor_mcp_selection_never_expands_sibling_tools_by_claim() -> None:
    service, persistence = _service(review_mcp=True)
    authority = _policy_with_mcp_tools("review_accept", "review_cancel")
    selected = _policy_with_mcp_tools("review_accept")
    descriptor_payload = {
        "revision": "descriptor-r1",
        "standard_authority": {
            "resources": [{
                "server_id": "review",
                "resource": REVIEW_RESOURCE,
                "grants": ["review:use"],
                "operations": ["review_accept", "review_cancel"],
            }],
            "named_services": [],
            "resource_families": [],
        },
    }

    result = await service.sync_agent_capability_control(
        {"user_id": OWNER},
        application=APPLICATION,
        agent_id=AGENT,
        descriptor_revision="descriptor-r1",
        descriptor_payload=descriptor_payload,
        capability_authority=authority,
        capability_catalog=authority,
        selected_capabilities=selected,
    )

    assert result["ok"] is True, result
    control = persistence.records[result["control_card"]["access_id"]][0]
    resident = persistence.records[result["card"]["access_id"]][0]
    assert control.resource_operations == {
        REVIEW_RESOURCE: ("review_accept", "review_cancel"),
    }
    assert resident.resource_grants == {REVIEW_RESOURCE: ("review:use",)}
    assert resident.resource_operations == {REVIEW_RESOURCE: ("review_accept",)}


@pytest.mark.asyncio
async def test_live_control_defaults_seed_a_recreated_agent_card() -> None:
    service, persistence = _service()
    created = await _sync(
        service,
        revision="descriptor-r1",
        authority=("tool.old", "tool.new"),
        catalog=("tool.old", "tool.new"),
        selection=("tool.old",),
    )
    control_id = created["control_card"]["access_id"]
    resident_id = created["card"]["access_id"]
    control, handles = persistence.records[control_id]
    control_properties = dict(control.properties)
    control_properties[AGENT_CAPABILITY_DEFAULTS_PROPERTY] = _policy("tool.new")
    persistence.records[control_id] = (
        dataclasses.replace(
            control,
            card_revision=control.card_revision + 1,
            properties=control_properties,
        ),
        handles,
    )
    del persistence.records[resident_id]

    recreated = await _sync(
        service,
        revision="descriptor-r1",
        authority=("tool.old", "tool.new"),
        catalog=("tool.old", "tool.new"),
        selection=("tool.old",),
    )

    assert recreated["ok"] is True, recreated
    assert recreated["control_changed"] is False
    assert recreated["selection"]["capabilities"] == {"tools": ["tool.new"]}
    assert recreated["projection"] == recreated["selection"]


@pytest.mark.asyncio
async def test_new_control_revision_fills_only_missing_single_choice_defaults() -> None:
    service, _persistence = _service()
    authority = _policy_with_capabilities(
        tools=["tool.default"],
        models=["anthropic/haiku", "anthropic/sonnet"],
        instruction_profiles=["extra-lite", "full"],
    )
    created = await service.sync_agent_capability_control(
        {"user_id": OWNER},
        application=APPLICATION,
        agent_id=AGENT,
        descriptor_revision="descriptor-r1",
        descriptor_payload={"revision": "descriptor-r1"},
        capability_authority=authority,
        capability_catalog=authority,
        selected_capabilities=_policy_with_capabilities(
            tools=[],
            models=["anthropic/haiku"],
        ),
    )
    assert created["ok"] is True, created

    changed = await service.sync_agent_capability_control(
        {"user_id": OWNER},
        application=APPLICATION,
        agent_id=AGENT,
        descriptor_revision="descriptor-r2",
        descriptor_payload={
            "revision": "descriptor-r2",
            "capability_defaults": _policy_with_capabilities(
                tools=["tool.default"],
                models=["anthropic/sonnet"],
                instruction_profiles=["extra-lite"],
            ),
        },
        capability_authority=authority,
        capability_catalog=authority,
        selected_capabilities=_policy_with_capabilities(
            tools=[],
            models=["anthropic/haiku"],
        ),
    )

    assert changed["ok"] is True, changed
    control = _persistence.records[changed["control_card"]["access_id"]][0]
    assert control.properties[AGENT_CAPABILITY_DEFAULTS_PROPERTY][
        "capabilities"
    ] == {
        "instruction_profiles": ["extra-lite"],
        "models": ["anthropic/sonnet"],
        "tools": ["tool.default"],
    }
    assert changed["selection"]["capabilities"] == {
        "instruction_profiles": ["extra-lite"],
        "models": ["anthropic/haiku"],
        "tools": [],
    }
    assert changed["projection"] == changed["selection"]


@pytest.mark.asyncio
async def test_standard_agent_card_edit_updates_the_runtime_projection() -> None:
    service, persistence = _service(named_services=True)
    operations = {
        NAMED_RESOURCE: {
            "slack": ["object.list", "object.action.post_message"],
        }
    }
    policy = _policy_with_named_operations(
        "object.list",
        "object.action.post_message",
    )
    created = await service.sync_agent_capability_control(
        {"user_id": OWNER},
        application=APPLICATION,
        agent_id=AGENT,
        descriptor_revision="descriptor-r1",
        descriptor_payload={"revision": "descriptor-r1"},
        capability_authority=policy,
        selected_capabilities=policy,
        resource_grants={
            NAMED_RESOURCE: [
                "named_services:use",
                "slack:read",
                "slack:post",
            ]
        },
        named_service_operations=operations,
        selected_resource_grants={
            NAMED_RESOURCE: [
                "named_services:use",
                "slack:read",
                "slack:post",
            ]
        },
        selected_named_service_operations=operations,
    )
    access_id = created["card"]["access_id"]

    changed = await service.update_access(
        {"user_id": OWNER, "roles": ["kdcube:role:super-admin"]},
        access_id=access_id,
        resource_grants={
            NAMED_RESOURCE: ["named_services:use", "slack:read"],
        },
        resource_operations={},
        named_service_operations={
            NAMED_RESOURCE: {"slack": ["object.list"]},
        },
        account_scope={},
        expected_card_revision=created["card"]["card_revision"],
        expected_catalog_version=created["card"]["catalog_version"],
        properties=created["card"]["properties"],
    )

    assert changed["ok"] is True, changed
    updated = persistence.records[access_id][0]
    selection = updated.properties[AGENT_CAPABILITY_SELECTION_PROPERTY]
    assert selection["capabilities"]["named_services"] == ["slack"]
    assert selection["capabilities"]["named_service_operations"] == [
        "slack/object.list"
    ]
    assert RESOURCE in updated.resource_acceptance


@pytest.mark.asyncio
async def test_only_an_administrator_may_edit_a_descriptor_control_card() -> None:
    service, _persistence = _service()
    created = await _sync(
        service,
        revision="descriptor-r1",
        authority=("tool.old",),
        catalog=("tool.old",),
        selection=("tool.old",),
    )

    refused = await service.update_access(
        {"user_id": OWNER, "roles": ["kdcube:role:registered"]},
        access_id=created["control_card"]["access_id"],
        resource_grants={},
        expected_card_revision=created["control_card"]["card_revision"],
    )

    assert refused == {
        "ok": False,
        "error": "platform_admin_required",
        "status": 403,
    }


@pytest.mark.asyncio
async def test_non_admin_grantor_may_edit_a_non_descriptor_control_card() -> None:
    service, persistence = _service(named_services=True)
    descriptor_operations = {
        NAMED_RESOURCE: {"slack": ["object.list"]},
    }
    created = await service.sync_agent_capability_control(
        {"user_id": OWNER},
        application=APPLICATION,
        agent_id=AGENT,
        descriptor_revision="descriptor-r1",
        descriptor_payload={"revision": "descriptor-r1"},
        capability_authority=_policy_with_named_operations("object.list"),
        resource_grants={
            NAMED_RESOURCE: ["named_services:use", "slack:read"],
        },
        named_service_operations=descriptor_operations,
    )
    control_id = created["control_card"]["access_id"]
    descriptor_control_card, handles = persistence.records[control_id]
    ordinary_properties = dict(descriptor_control_card.properties)
    ordinary_properties.pop(AGENT_DESCRIPTOR_CONTROL_PROPERTY)
    ordinary_acceptance = {
        resource: acceptance
        for resource, acceptance in descriptor_control_card.resource_acceptance.items()
        if acceptance.kind != AGENT_DESCRIPTOR_ACCEPTANCE_KIND
    }
    ordinary_control_card = dataclasses.replace(
        descriptor_control_card,
        issuer_kind="application",
        properties=ordinary_properties,
        resource_acceptance=ordinary_acceptance,
    )
    persistence.records[control_id] = (ordinary_control_card, handles)

    changed = await service.update_access(
        {
            "user_id": OWNER,
            "roles": ["kdcube:role:registered"],
            "permissions": ["named_services:use", "slack:read"],
        },
        access_id=control_id,
        resource_grants={
            NAMED_RESOURCE: ["named_services:use", "slack:read"],
        },
        resource_operations={},
        named_service_operations=descriptor_operations,
        account_scope={},
        expected_card_revision=ordinary_control_card.card_revision,
        expected_catalog_version=ordinary_control_card.catalog_version,
        composition_mode="and",
        properties=ordinary_properties,
    )

    assert changed["ok"] is True, changed


@pytest.mark.asyncio
async def test_same_descriptor_sync_preserves_the_administrator_preset() -> None:
    service, persistence = _service(named_services=True)
    descriptor_operations = {
        NAMED_RESOURCE: {
            "slack": ["object.list", "object.action.post_message"],
        }
    }
    created = await service.sync_agent_capability_control(
        {"user_id": OWNER},
        application=APPLICATION,
        agent_id=AGENT,
        descriptor_revision="descriptor-r1",
        descriptor_payload={"revision": "descriptor-r1"},
        capability_authority=_policy_with_named_operations(
            "object.list",
            "object.action.post_message",
        ),
        resource_grants={
            NAMED_RESOURCE: [
                "named_services:use",
                "slack:read",
                "slack:post",
            ]
        },
        named_service_operations=descriptor_operations,
    )
    control_id = created["control_card"]["access_id"]

    changed = await service.update_access(
        {"user_id": OWNER, "roles": ["kdcube:role:super-admin"]},
        access_id=control_id,
        resource_grants={
            NAMED_RESOURCE: ["named_services:use", "slack:read"],
        },
        resource_operations={},
        named_service_operations={
            NAMED_RESOURCE: {"slack": ["object.list"]},
        },
        account_scope={},
        expected_card_revision=created["control_card"]["card_revision"],
        expected_catalog_version=created["control_card"]["catalog_version"],
        composition_mode="and",
        properties=created["control_card"]["properties"],
    )
    assert changed["ok"] is True, changed

    replay = await service.sync_agent_capability_control(
        {"user_id": OWNER},
        application=APPLICATION,
        agent_id=AGENT,
        descriptor_revision="descriptor-r1",
        descriptor_payload={"revision": "descriptor-r1"},
        capability_authority=_policy_with_named_operations(
            "object.list",
            "object.action.post_message",
        ),
        resource_grants={
            NAMED_RESOURCE: [
                "named_services:use",
                "slack:read",
                "slack:post",
            ]
        },
        named_service_operations=descriptor_operations,
    )

    assert replay["ok"] is True, replay
    assert replay["control_changed"] is False
    control = persistence.records[control_id][0]
    assert control.resource_grants == {
        NAMED_RESOURCE: ("named_services:use", "slack:read"),
    }
    assert control.named_service_operations.to_stored() == {
        NAMED_RESOURCE: {"slack": ["object.list"]},
    }


@pytest.mark.asyncio
async def test_descriptor_control_always_limits_the_agent_card() -> None:
    service, _persistence = _service()
    created = await _sync(
        service,
        revision="descriptor-r1",
        authority=("tool.old",),
        catalog=("tool.old",),
        selection=("tool.old",),
    )

    refused = await service.update_access(
        {"user_id": OWNER, "roles": ["kdcube:role:super-admin"]},
        access_id=created["control_card"]["access_id"],
        resource_grants={},
        expected_card_revision=created["control_card"]["card_revision"],
        composition_mode="or",
    )

    assert refused == {
        "ok": False,
        "error": "agent_descriptor_control_requires_and",
        "status": 400,
    }


@pytest.mark.asyncio
async def test_metadata_only_agent_card_still_uses_the_standard_save() -> None:
    service, persistence = _service()
    created = await _sync(
        service,
        revision="descriptor-r1",
        authority=("tool.old", "tool.new"),
        catalog=("tool.old", "tool.new"),
        selection=("tool.old",),
    )
    access_id = created["card"]["access_id"]
    properties = dict(created["card"]["properties"])
    properties[AGENT_CAPABILITY_SELECTION_PROPERTY] = _policy("tool.new")

    changed = await service.update_access(
        {"user_id": OWNER},
        access_id=access_id,
        resource_grants={},
        resource_operations={},
        named_service_operations={},
        account_scope={},
        expected_card_revision=created["card"]["card_revision"],
        expected_catalog_version=created["card"]["catalog_version"],
        properties=properties,
    )

    assert changed["ok"] is True, changed
    updated = persistence.records[access_id][0]
    assert updated.resource_grants == {}
    assert updated.properties[AGENT_CAPABILITY_SELECTION_PROPERTY] == _policy(
        "tool.new"
    )
    assert RESOURCE in updated.resource_acceptance

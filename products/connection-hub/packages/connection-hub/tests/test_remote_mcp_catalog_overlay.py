from __future__ import annotations

from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.automation_access import (
    AutomationAccessService,
)
from connection_hub.delegated_credentials.oauth.config import (
    oauth_delegated_config,
)
from connection_hub.remote_mcp import (
    EXTERNAL_MCP_GRANT,
    RemoteMCPDiscovery,
    RemoteMCPConnector,
    remote_mcp_resource_rows,
)


class _CatalogResolver:
    async def resolve_active(self):
        return SimpleNamespace(
            version="delegated_catalog_fixture",
            connections={
                "delegated_credentials": {
                    "oauth": {
                        "enabled": True,
                        "capabilities": [
                            {
                                "grant": EXTERNAL_MCP_GRANT,
                                "label": "Use external MCP",
                                "delegable_roles": ["kdcube:role:registered"],
                            }
                        ],
                        "resources": [],
                    }
                }
            },
        )


def _config():
    return oauth_delegated_config(
        SimpleNamespace(
            state=SimpleNamespace(
                oauth_delegated_config={
                    "enabled": True,
                    "capabilities": [
                        {
                            "grant": EXTERNAL_MCP_GRANT,
                            "label": "Use external MCP",
                            "delegable_roles": ["kdcube:role:registered"],
                        }
                    ],
                    "resources": [],
                }
            )
        )
    )


def _connector() -> RemoteMCPConnector:
    discovery = RemoteMCPDiscovery.build(
        connector_id="mcp_0123456789abcdef01234567",
        tools=[
            {
                "name": "records.search",
                "description": "Search records",
                "input_schema": {"type": "object"},
            },
            {
                "name": "records.delete",
                "description": "Delete one record",
                "input_schema": {"type": "object"},
            },
        ],
    )
    return RemoteMCPConnector(
        connector_id="mcp_0123456789abcdef01234567",
        owner_subject="user-1",
        label="Customer records",
        endpoint="https://mcp.example.test/mcp",
        transport="streamable-http",
        resource="urn:connection-hub:remote-mcp:mcp_0123456789abcdef01234567",
        revision=1,
        state="active",
        credential_mode="none",
        tools=discovery.tools,
        descriptor_digest=discovery.descriptor_digest,
        descriptor_revision=1,
        descriptor_state="accepted",
        created_at=1,
        updated_at=1,
        last_checked_at=1,
    )


@pytest.mark.asyncio
async def test_user_owned_connector_is_offered_as_an_exact_card_resource():
    requested_owners: list[str] = []

    async def overlay(owner_subject: str):
        requested_owners.append(owner_subject)
        return remote_mcp_resource_rows([_connector()])

    service = AutomationAccessService(
        redis=object(),
        tenant="tenant-a",
        project="project-a",
        config=_config(),
        catalog_resolver=_CatalogResolver(),
        resource_overlay_provider=overlay,
    )
    resources = await service.resource_options(
        {
            "user_id": "user-1",
            "roles": ["kdcube:role:registered"],
            "permissions": [],
        }
    )

    assert requested_owners == ["user-1"]
    assert resources == [
        {
                "resource": "urn:connection-hub:remote-mcp:mcp_0123456789abcdef01234567",
                "label": "External MCP: Customer records",
                "kind": "remote_mcp",
                "provider_id": "remote_mcp",
                "identity_scope": "grantor",
            "grants": [EXTERNAL_MCP_GRANT],
            "admin_only": False,
            "operations": [
                {
                    "name": "records.delete",
                    "label": "records.delete",
                    "description": "Delete one record",
                    "grants": [EXTERNAL_MCP_GRANT],
                },
                {
                    "name": "records.search",
                    "label": "records.search",
                    "description": "Search records",
                    "grants": [EXTERNAL_MCP_GRANT],
                },
            ],
        }
    ]


@pytest.mark.asyncio
async def test_connector_overlay_is_scoped_to_an_authenticated_owner():
    calls: list[str] = []

    async def overlay(owner_subject: str):
        calls.append(owner_subject)
        return remote_mcp_resource_rows([_connector()])

    service = AutomationAccessService(
        redis=object(),
        tenant="tenant-a",
        project="project-a",
        config=_config(),
        catalog_resolver=_CatalogResolver(),
        resource_overlay_provider=overlay,
    )

    assert await service.resource_options({}) == []
    assert calls == []


@pytest.mark.asyncio
async def test_oauth_consent_catalog_includes_only_the_authenticated_owners_overlay():
    calls: list[str] = []

    async def overlay(owner_subject: str):
        calls.append(owner_subject)
        return remote_mcp_resource_rows([_connector()])

    service = AutomationAccessService(
        redis=object(),
        tenant="tenant-a",
        project="project-a",
        config=_config(),
        catalog_resolver=_CatalogResolver(),
        resource_overlay_provider=overlay,
    )

    config = await service.oauth_consent_config(grantor_subject="user-1")

    assert config is not None
    assert calls == ["user-1"]
    assert config.resource_config(_connector().resource).label == (
        "External MCP: Customer records"
    )

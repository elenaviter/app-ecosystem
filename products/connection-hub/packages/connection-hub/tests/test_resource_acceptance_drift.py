# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Per-resource accepted descriptor state and the drift judged against it."""

from __future__ import annotations

import copy
from datetime import datetime, timezone

from connection_hub.delegated_credentials.cards.model import (
    CARD_AUTHORITY_SCHEMA,
    CARD_AUTHORITY_SCHEMA_V2,
    CardAuthority,
    NamedServiceSelection,
)
from connection_hub.delegated_credentials.catalog.descriptors import (
    RESOURCE_KIND_CATALOG,
    RESOURCE_KIND_REMOTE_MCP,
    ResourceAcceptance,
    next_resource_acceptance,
    resource_descriptor_state,
    resource_row_digest,
    row_acceptance,
)
from connection_hub.delegated_credentials.catalog.drift import (
    DRIFT_CHANGED,
    DRIFT_CURRENT,
    DRIFT_NO_RELEVANT_CHANGE,
    EFFECT_SUSPENDED,
    card_drift,
)
from connection_hub.delegated_credentials.catalog.models import CatalogDocument
from connection_hub.delegated_credentials.oauth.config import (
    oauth_delegated_config_from_connections,
)
from connection_hub.remote_mcp import (
    EXTERNAL_MCP_GRANT,
    RemoteMCPConnector,
    RemoteMCPDiscovery,
    remote_mcp_resource_rows,
)

MEMORIES = "https://host/api/mcp/memories*"
TASKS = "https://host/api/mcp/tasks*"
CONNECTOR_ID = "mcp_0123456789abcdef01234567"
NOW = 1_780_000_000


def _connections(*, tasks_delete_description="Delete a task", extra_resource=False):
    resources = [
        {
            "resource": MEMORIES,
            "label": "Memories",
            "grants": ["memories:read", "memories:write"],
            "tools": {
                "search": {"grants": ["memories:read"], "description": "Search memories"},
                "write": {"grants": ["memories:write"], "description": "Write a memory"},
            },
        },
        {
            "resource": TASKS,
            "label": "Tasks",
            "grants": ["tasks:use"],
            "tools": {
                "search": {"grants": ["tasks:use"], "description": "Search tasks"},
                "delete": {"grants": ["tasks:use"], "description": tasks_delete_description},
            },
        },
    ]
    if extra_resource:
        resources.append(
            {
                "resource": "https://host/api/mcp/mail*",
                "label": "Mail",
                "grants": ["mail:read"],
                "tools": {"read": {"grants": ["mail:read"]}},
            }
        )
    return {
        "delegated_credentials": {
            "oauth": {
                "enabled": True,
                "capabilities": [
                    {"grant": g, "label": g, "delegable_roles": ["kdcube:role:registered"]}
                    for g in ("memories:read", "memories:write", "tasks:use", "mail:read", EXTERNAL_MCP_GRANT)
                ],
                "resources": resources,
            }
        }
    }


def _document(connections, *, stamp: int) -> CatalogDocument:
    return CatalogDocument.build(
        connections, created_at=datetime.fromtimestamp(stamp, tz=timezone.utc)
    )


def _connector(*, search_description="Search records") -> RemoteMCPConnector:
    discovery = RemoteMCPDiscovery.build(
        connector_id=CONNECTOR_ID,
        tools=[
            {"name": "search", "description": search_description, "input_schema": {"type": "object"}},
            {"name": "delete", "description": "Delete one record", "input_schema": {"type": "object"}},
        ],
    )
    return RemoteMCPConnector(
        connector_id=CONNECTOR_ID,
        owner_subject="user-1",
        label="Customer records",
        endpoint="https://mcp.example.test/mcp",
        transport="streamable-http",
        resource=f"urn:connection-hub:remote-mcp:{CONNECTOR_ID}",
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


def _config_with_overlay(document: CatalogDocument, connector: RemoteMCPConnector | None):
    config = oauth_delegated_config_from_connections(document.connections)
    if connector is None:
        return config
    from dataclasses import replace

    return replace(
        config, resources=tuple(config.resources) + remote_mcp_resource_rows([connector])
    )


def _card(*, resource_grants, resource_operations, catalog_version, acceptance=None, schema=CARD_AUTHORITY_SCHEMA):
    authority = CardAuthority(
        access_id="agent-0123456789abcdef",
        client_id="kdcube-agent:workspace@1-0:lg-react",
        grantor_subject="user-1",
        delegate_subject="integration:kdcube-agent:workspace@1-0:lg-react:user-1",
        source="agent",
        label="lg-react",
        card_revision=3,
        catalog_version=catalog_version,
        resource_grants=resource_grants,
        resource_operations=resource_operations,
        named_service_operations=NamedServiceSelection.none(),
        created_at=NOW,
        expires_at=NOW + 3600,
        resource_acceptance=acceptance or {},
    )
    if schema == CARD_AUTHORITY_SCHEMA:
        return authority
    # A revision written before v3: no acceptance field at all.
    payload = authority.to_dict()
    payload["schema"] = schema
    payload.pop("resource_acceptance")
    payload.pop("provenance")
    return CardAuthority.from_mapping(payload)


def test_row_digest_follows_the_row_not_the_catalog_version():
    before = _document(_connections(), stamp=NOW)
    after = _document(_connections(extra_resource=True), stamp=NOW + 10)
    assert before.version != after.version
    tasks_before = oauth_delegated_config_from_connections(before.connections).card_selector_config(TASKS)
    tasks_after = oauth_delegated_config_from_connections(after.connections).card_selector_config(TASKS)
    assert resource_row_digest(tasks_before) == resource_row_digest(tasks_after)
    changed = _document(_connections(tasks_delete_description="Delete permanently"), stamp=NOW + 20)
    tasks_changed = oauth_delegated_config_from_connections(changed.connections).card_selector_config(TASKS)
    assert resource_row_digest(tasks_changed) != resource_row_digest(tasks_before)
    accepted = row_acceptance(tasks_before, catalog_version=before.version)
    assert accepted.kind == RESOURCE_KIND_CATALOG
    assert accepted.revision == before.version
    assert set(accepted.operations) == {"search", "delete"}
    assert accepted.grants == ("tasks:use",)


def test_connector_row_carries_its_own_descriptor_authority():
    connector = _connector()
    row = remote_mcp_resource_rows([connector])[0]
    accepted = row_acceptance(row, catalog_version="delegated_catalog_whatever")
    assert accepted.kind == RESOURCE_KIND_REMOTE_MCP
    assert accepted.revision == "1"
    assert accepted.digest == connector.descriptor_digest
    assert accepted.operations == {tool.name: tool.descriptor_digest for tool in connector.tools}
    # The connector's revision, not the catalog version, is the accepted revision.
    assert accepted.revision != "delegated_catalog_whatever"


def test_v3_round_trips_acceptance_and_v2_reads_without_it():
    document = _document(_connections(), stamp=NOW)
    config = oauth_delegated_config_from_connections(document.connections)
    acceptance = {
        TASKS: row_acceptance(config.card_selector_config(TASKS), catalog_version=document.version)
    }
    card = _card(
        resource_grants={TASKS: ("tasks:use",)},
        resource_operations={TASKS: ("search",)},
        catalog_version=document.version,
        acceptance=acceptance,
    )
    payload = card.to_dict()
    assert payload["schema"] == CARD_AUTHORITY_SCHEMA
    assert payload["resource_acceptance"][TASKS]["kind"] == RESOURCE_KIND_CATALOG
    restored = CardAuthority.from_mapping(payload)
    assert restored.resource_acceptance[TASKS] == acceptance[TASKS]
    assert restored.content_hash() == card.content_hash()
    legacy = _card(
        resource_grants={TASKS: ("tasks:use",)},
        resource_operations={TASKS: ("search",)},
        catalog_version=document.version,
        schema=CARD_AUTHORITY_SCHEMA_V2,
    )
    assert legacy.resource_acceptance == {}
    assert legacy.to_dict()["schema"] == CARD_AUTHORITY_SCHEMA


def test_unrelated_catalog_change_leaves_an_unchanged_connector_current():
    """The report's regression: a static catalog bump used to make every
    connector grant read as newly available, because the baseline document
    never contained the owner-overlay row."""
    connector = _connector()
    before = _document(_connections(), stamp=NOW)
    after = _document(_connections(extra_resource=True), stamp=NOW + 10)
    row = _config_with_overlay(before, connector).card_selector_config(connector.resource)
    card = _card(
        resource_grants={connector.resource: (EXTERNAL_MCP_GRANT,), TASKS: ("tasks:use",)},
        resource_operations={connector.resource: ("search",), TASKS: ("search",)},
        catalog_version=before.version,
        acceptance={
            connector.resource: row_acceptance(row, catalog_version=before.version),
            TASKS: row_acceptance(
                oauth_delegated_config_from_connections(before.connections).card_selector_config(TASKS),
                catalog_version=before.version,
            ),
        },
    )
    drift = card_drift(
        card=card,
        active=after,
        baseline=before,
        active_config=_config_with_overlay(after, connector),
    )
    assert drift["status"] == DRIFT_NO_RELEVANT_CHANGE
    assert drift["resources"][connector.resource]["status"] == "current"
    assert drift["resources"][TASKS]["status"] == "current"
    assert drift["added"] == {"claims": [], "outer_operations": [], "named_service_operations": []}

    # A card written before acceptance existed: still no spurious additions,
    # because the connector is unknown to the baseline document.
    legacy = _card(
        resource_grants={connector.resource: (EXTERNAL_MCP_GRANT,)},
        resource_operations={connector.resource: ("search",)},
        catalog_version=before.version,
        schema=CARD_AUTHORITY_SCHEMA_V2,
    )
    legacy_drift = card_drift(
        card=legacy, active=after, baseline=before, active_config=_config_with_overlay(after, connector)
    )
    assert legacy_drift["added"]["outer_operations"] == []
    assert legacy_drift["resources"][connector.resource]["status"] == "unknown"


def test_changed_selected_tool_is_suspended_and_new_sibling_stays_ungranted():
    original = _connector()
    changed = _connector(search_description="Search and return full bodies")
    before = _document(_connections(), stamp=NOW)
    row_before = _config_with_overlay(before, original).card_selector_config(original.resource)
    card = _card(
        resource_grants={original.resource: (EXTERNAL_MCP_GRANT,)},
        resource_operations={original.resource: ("search",)},
        catalog_version=before.version,
        acceptance={original.resource: row_acceptance(row_before, catalog_version=before.version)},
    )
    # The connector accepted the change on its side (descriptor revision 2)
    # and grew a third tool; the card has not reviewed either.
    grown = RemoteMCPDiscovery.build(
        connector_id=CONNECTOR_ID,
        tools=[
            {"name": "search", "description": "Search and return full bodies", "input_schema": {"type": "object"}},
            {"name": "delete", "description": "Delete one record", "input_schema": {"type": "object"}},
            {"name": "export", "description": "Export everything", "input_schema": {"type": "object"}},
        ],
    )
    from dataclasses import replace

    live = replace(
        changed,
        tools=grown.tools,
        descriptor_digest=grown.descriptor_digest,
        descriptor_revision=2,
        revision=3,
    )
    drift = card_drift(
        card=card, active=before, baseline=before, active_config=_config_with_overlay(before, live)
    )
    state = drift["resources"][original.resource]
    assert drift["status"] == DRIFT_CHANGED
    assert state["status"] == "changed"
    assert state["changed_operations"] == ["search"]
    assert state["added_operations"] == ["export"]
    assert state["accepted_revision"] == "1" and state["current_revision"] == "2"
    assert drift["changed"]["outer_operations"] == [
        {
            "resource": original.resource,
            "operation": "search",
            "was_selected": True,
            "effect": EFFECT_SUSPENDED,
            "accepted_digest": original.descriptor_digest,
            "current_digest": grown.descriptor_digest,
        }
    ]
    added = drift["added"]["outer_operations"]
    assert added == [{"resource": original.resource, "operation": "export", "selected": False}]
    # The unselected sibling `delete` changed nothing and is not reported.
    assert "delete" not in state["changed_operations"]


def test_save_accepts_only_the_named_changed_operations():
    original = _connector()
    before = _document(_connections(), stamp=NOW)
    row_before = _config_with_overlay(before, original).card_selector_config(original.resource)
    prior = {original.resource: row_acceptance(row_before, catalog_version=before.version)}
    both_changed = RemoteMCPDiscovery.build(
        connector_id=CONNECTOR_ID,
        tools=[
            {"name": "search", "description": "Search v2", "input_schema": {"type": "object"}},
            {"name": "delete", "description": "Delete v2", "input_schema": {"type": "object"}},
        ],
    )
    from dataclasses import replace

    live = replace(
        original, tools=both_changed.tools, descriptor_digest=both_changed.descriptor_digest, descriptor_revision=2
    )
    row_now = _config_with_overlay(before, live).card_selector_config(original.resource)
    row_for = lambda resource: row_now if resource == original.resource else None  # noqa: E731

    # An ordinary save (nothing accepted) keeps both selected tools suspended.
    kept = next_resource_acceptance(
        resources=[original.resource],
        row_for=row_for,
        catalog_version=before.version,
        selected_operations={original.resource: ["search", "delete"]},
        previous=prior,
    )[original.resource]
    assert kept.operations["search"] == prior[original.resource].operations["search"]
    assert kept.operations["delete"] == prior[original.resource].operations["delete"]
    assert kept.digest == prior[original.resource].digest
    assert kept.revision == "1"

    # Accepting `search` alone advances that one; `delete` stays suspended and
    # so does the row-level evidence.
    partial = next_resource_acceptance(
        resources=[original.resource],
        row_for=row_for,
        catalog_version=before.version,
        selected_operations={original.resource: ["search", "delete"]},
        previous=prior,
        accepted_operations={original.resource: ["search"]},
    )[original.resource]
    live_ops = {tool.name: tool.descriptor_digest for tool in live.tools}
    assert partial.operations["search"] == live_ops["search"]
    assert partial.operations["delete"] == prior[original.resource].operations["delete"]
    assert partial.revision == "1"
    state = resource_descriptor_state(
        accepted=partial, row=row_now, catalog_version=before.version, selected_operations=["search", "delete"]
    )
    assert state["changed_operations"] == ["delete"]

    # Accepting both makes the resource current again.
    full = next_resource_acceptance(
        resources=[original.resource],
        row_for=row_for,
        catalog_version=before.version,
        selected_operations={original.resource: ["search", "delete"]},
        previous=prior,
        accepted_operations={original.resource: ["search", "delete"]},
    )[original.resource]
    assert full.operations == live_ops
    assert full.digest == live.descriptor_digest and full.revision == "2"
    assert resource_descriptor_state(
        accepted=full, row=row_now, catalog_version=before.version, selected_operations=["search", "delete"]
    )["status"] == "current"

    # An unselected changed tool is never suspended: it is not granted.
    unselected = next_resource_acceptance(
        resources=[original.resource],
        row_for=row_for,
        catalog_version=before.version,
        selected_operations={original.resource: ["search"]},
        previous=prior,
        accepted_operations={original.resource: ["search"]},
    )[original.resource]
    assert unselected.operations["delete"] == live_ops["delete"]
    assert unselected.revision == "2"


def test_static_row_change_is_scoped_to_its_own_resource():
    before = _document(_connections(), stamp=NOW)
    after = _document(_connections(tasks_delete_description="Delete permanently"), stamp=NOW + 5)
    config_before = oauth_delegated_config_from_connections(before.connections)
    card = _card(
        resource_grants={MEMORIES: ("memories:read",), TASKS: ("tasks:use",)},
        resource_operations={MEMORIES: ("search",), TASKS: ("search", "delete")},
        catalog_version=before.version,
        acceptance={
            MEMORIES: row_acceptance(config_before.card_selector_config(MEMORIES), catalog_version=before.version),
            TASKS: row_acceptance(config_before.card_selector_config(TASKS), catalog_version=before.version),
        },
    )
    drift = card_drift(card=card, active=after, baseline=before)
    assert drift["status"] == DRIFT_CHANGED
    assert drift["resources"][MEMORIES]["status"] == "current"
    assert drift["resources"][TASKS]["status"] == "changed"
    assert drift["resources"][TASKS]["changed_operations"] == ["delete"]
    # Equal operation names stay qualified: memories `search` is untouched by
    # the tasks change.
    assert drift["resources"][MEMORIES]["changed_operations"] == []
    same = card_drift(card=card, active=before, baseline=before)
    assert same["status"] == DRIFT_CURRENT


def test_acceptance_survives_a_deep_copy_of_the_card_payload():
    document = _document(_connections(), stamp=NOW)
    config = oauth_delegated_config_from_connections(document.connections)
    acceptance = ResourceAcceptance.from_mapping(
        row_acceptance(config.card_selector_config(TASKS), catalog_version=document.version).to_dict()
    )
    card = _card(
        resource_grants={TASKS: ("tasks:use",)},
        resource_operations={TASKS: ("search",)},
        catalog_version=document.version,
        acceptance={TASKS: acceptance},
    )
    twin = CardAuthority.from_mapping(copy.deepcopy(card.to_dict()))
    assert twin == card

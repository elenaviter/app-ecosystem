from __future__ import annotations

import pytest

from connection_hub.delegated_to_kdcube.consent_demand import (
    PENDING_DEMANDS_REGISTRY_KEY,
    author_consent_granted_events,
    read_pending_consent,
    record_consent_demand,
)


_NAMED_SERVICES_RESOURCE = "*/kdcube-services@1-0/public/mcp/named_services*"


class MemoryPropertyStore:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str, str], object] = {}

    async def get_user_prop(
        self,
        key: str,
        *,
        user_id: str,
        bundle_id: str,
        default: object = None,
    ) -> object:
        return self.values.get((user_id, bundle_id, key), default)

    async def set_user_prop(
        self,
        key: str,
        value: object,
        *,
        user_id: str,
        bundle_id: str,
    ) -> None:
        self.values[(user_id, bundle_id, key)] = value

    async def delete_user_prop(
        self,
        key: str,
        *,
        user_id: str,
        bundle_id: str,
    ) -> None:
        self.values.pop((user_id, bundle_id, key), None)


class LaneSource:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.published: list[dict] = []

    async def publish(self, **event: object) -> None:
        if self.fail:
            raise RuntimeError("lane unavailable")
        self.published.append(event)


def demand(**overrides: object) -> dict:
    value = {
        "user_id": "user-1",
        "bundle_id": "workspace@test",
        "conversation_id": "conv-1",
        "provider_id": "slack",
        "provider_label": "Slack",
        "connector_app_id": "demo",
        "claims": ["slack:post"],
        "tool_name": "slack.post_message",
        "tenant": "tenant-1",
        "project": "project-1",
        "agent_id": "main",
        "connection_hub_bundle_id": "connection-hub@1-0",
    }
    value.update(overrides)
    return value


@pytest.mark.asyncio
async def test_demand_records_a_complete_conversation_address() -> None:
    store = MemoryPropertyStore()

    assert await record_consent_demand(**demand(), property_store=store) is True

    registry = store.values[
        ("user-1", "connection-hub@1-0", PENDING_DEMANDS_REGISTRY_KEY)
    ]
    entry = registry["demands"][0]
    assert entry["conversation_id"] == "conv-1"
    assert entry["tenant"] == "tenant-1"
    assert entry["project"] == "project-1"
    assert entry["bundle_id"] == "workspace@test"
    assert entry["agent_id"] == "main"


@pytest.mark.asyncio
async def test_grant_authors_a_passive_event_and_clears_the_demand() -> None:
    store = MemoryPropertyStore()
    await record_consent_demand(**demand(), property_store=store)
    source = LaneSource()

    authored = await author_consent_granted_events(
        redis=None,
        user_id="user-1",
        provider_id="slack",
        granted_claims=["slack:post"],
        connector_app_id="demo",
        account_id="account-1",
        connection_hub_bundle_id="connection-hub@1-0",
        source_factory=lambda _entry: source,
        property_store=store,
    )

    assert authored == 1
    assert len(source.published) == 1
    event = source.published[0]
    assert event["kind"] == "external_event"
    assert event["task_payload"] is None
    assert event["payload"]["event"]["type"] == "connections.consent.granted"
    assert await read_pending_consent(
        user_id="user-1",
        bundle_id="workspace@test",
        conversation_id="conv-1",
        property_store=store,
    ) == []


@pytest.mark.asyncio
async def test_operation_only_grant_authors_the_exact_pending_event() -> None:
    store = MemoryPropertyStore()
    await record_consent_demand(
        **demand(
            provider_id="kdcube",
            connector_app_id="kdcube-agent:workspace@1-0:main",
            claims=[],
            tool_name="slack",
            resource=_NAMED_SERVICES_RESOURCE,
            namespace="slack",
            operation="object.action.upload_file",
        ),
        property_store=store,
    )
    source = LaneSource()

    authored = await author_consent_granted_events(
        redis=None,
        user_id="user-1",
        provider_id="kdcube",
        granted_claims=[],
        granted_named_service_operations={
            "slack": ["object.action.upload_file"]
        },
        granted_resource=_NAMED_SERVICES_RESOURCE,
        connector_app_id="kdcube-agent:workspace@1-0:main",
        account_id="agent-1",
        connection_hub_bundle_id="connection-hub@1-0",
        source_factory=lambda _entry: source,
        property_store=store,
    )

    assert authored == 1
    payload = source.published[0]["payload"]
    assert payload["claims"] == []
    assert payload["namespace"] == "slack"
    assert payload["operation"] == "object.action.upload_file"
    assert payload["resource"] == _NAMED_SERVICES_RESOURCE
    assert payload["named_service_operations"] == {
        "slack": ["object.action.upload_file"]
    }
    assert "operation object.action.upload_file in slack" in payload["text"]
    assert await read_pending_consent(
        user_id="user-1",
        bundle_id="workspace@test",
        conversation_id="conv-1",
        property_store=store,
    ) == []


@pytest.mark.asyncio
async def test_operation_only_grant_does_not_close_a_different_operation() -> None:
    store = MemoryPropertyStore()
    await record_consent_demand(
        **demand(
            provider_id="kdcube",
            connector_app_id="kdcube-agent:workspace@1-0:main",
            claims=[],
            tool_name="slack",
            resource=_NAMED_SERVICES_RESOURCE,
            namespace="slack",
            operation="object.action.upload_file",
        ),
        property_store=store,
    )
    source = LaneSource()

    authored = await author_consent_granted_events(
        redis=None,
        user_id="user-1",
        provider_id="kdcube",
        granted_claims=[],
        granted_named_service_operations={
            "slack": ["object.action.post_message"]
        },
        granted_resource=_NAMED_SERVICES_RESOURCE,
        connector_app_id="kdcube-agent:workspace@1-0:main",
        connection_hub_bundle_id="connection-hub@1-0",
        source_factory=lambda _entry: source,
        property_store=store,
    )

    assert authored == 0
    assert source.published == []
    registry = store.values[
        ("user-1", "connection-hub@1-0", PENDING_DEMANDS_REGISTRY_KEY)
    ]
    assert registry["demands"][0]["operation"] == "object.action.upload_file"


@pytest.mark.asyncio
async def test_operation_only_grant_does_not_close_a_claim_demand() -> None:
    store = MemoryPropertyStore()
    await record_consent_demand(
        **demand(
            provider_id="kdcube",
            connector_app_id="kdcube-agent:workspace@1-0:main",
            claims=["named_services:use"],
            tool_name="slack",
        ),
        property_store=store,
    )
    source = LaneSource()

    authored = await author_consent_granted_events(
        redis=None,
        user_id="user-1",
        provider_id="kdcube",
        granted_claims=[],
        granted_named_service_operations={
            "slack": ["object.action.upload_file"]
        },
        connector_app_id="kdcube-agent:workspace@1-0:main",
        connection_hub_bundle_id="connection-hub@1-0",
        source_factory=lambda _entry: source,
        property_store=store,
    )

    assert authored == 0
    assert source.published == []


@pytest.mark.asyncio
async def test_claim_only_grant_does_not_close_an_operation_demand() -> None:
    store = MemoryPropertyStore()
    await record_consent_demand(
        **demand(
            provider_id="kdcube",
            connector_app_id="kdcube-agent:workspace@1-0:main",
            claims=[],
            tool_name="slack",
            resource=_NAMED_SERVICES_RESOURCE,
            namespace="slack",
            operation="object.action.upload_file",
        ),
        property_store=store,
    )
    source = LaneSource()

    authored = await author_consent_granted_events(
        redis=None,
        user_id="user-1",
        provider_id="kdcube",
        granted_claims=["named_services:use"],
        granted_resource=_NAMED_SERVICES_RESOURCE,
        connector_app_id="kdcube-agent:workspace@1-0:main",
        connection_hub_bundle_id="connection-hub@1-0",
        source_factory=lambda _entry: source,
        property_store=store,
    )

    assert authored == 0
    assert source.published == []


@pytest.mark.asyncio
async def test_same_tool_operation_demands_remain_independent() -> None:
    store = MemoryPropertyStore()
    shared = {
        "provider_id": "kdcube",
        "connector_app_id": "kdcube-agent:workspace@1-0:main",
        "claims": [],
        "tool_name": "slack",
        "resource": _NAMED_SERVICES_RESOURCE,
        "namespace": "slack",
    }
    await record_consent_demand(
        **demand(**shared, operation="object.action.upload_file"),
        property_store=store,
    )
    await record_consent_demand(
        **demand(**shared, operation="object.schema"),
        property_store=store,
    )
    first_source = LaneSource()

    first_authored = await author_consent_granted_events(
        redis=None,
        user_id="user-1",
        provider_id="kdcube",
        granted_claims=[],
        granted_named_service_operations={
            "slack": ["object.action.upload_file"]
        },
        granted_resource=_NAMED_SERVICES_RESOURCE,
        connector_app_id="kdcube-agent:workspace@1-0:main",
        connection_hub_bundle_id="connection-hub@1-0",
        source_factory=lambda _entry: first_source,
        property_store=store,
    )

    assert first_authored == 1
    registry = store.values[
        ("user-1", "connection-hub@1-0", PENDING_DEMANDS_REGISTRY_KEY)
    ]
    assert [entry["operation"] for entry in registry["demands"]] == [
        "object.schema"
    ]
    pending = await read_pending_consent(
        user_id="user-1",
        bundle_id="workspace@test",
        conversation_id="conv-1",
        property_store=store,
    )
    assert pending[0]["tools"] == ["slack"]

    second_source = LaneSource()
    second_authored = await author_consent_granted_events(
        redis=None,
        user_id="user-1",
        provider_id="kdcube",
        granted_claims=[],
        granted_named_service_operations={"slack": ["object.schema"]},
        granted_resource=_NAMED_SERVICES_RESOURCE,
        connector_app_id="kdcube-agent:workspace@1-0:main",
        connection_hub_bundle_id="connection-hub@1-0",
        source_factory=lambda _entry: second_source,
        property_store=store,
    )

    assert second_authored == 1
    assert await read_pending_consent(
        user_id="user-1",
        bundle_id="workspace@test",
        conversation_id="conv-1",
        property_store=store,
    ) == []


@pytest.mark.asyncio
async def test_operation_grant_closes_only_the_matching_resource_demand() -> None:
    store = MemoryPropertyStore()
    client_id = "kdcube-agent:workspace@1-0:main"
    resource_a = "*/apps/a/public/mcp/named_services*"
    resource_b = "*/apps/b/public/mcp/named_services*"
    shared = {
        "provider_id": "kdcube",
        "connector_app_id": client_id,
        "claims": [],
        "tool_name": "slack",
        "namespace": "slack",
        "operation": "object.schema",
    }
    await record_consent_demand(
        **demand(**shared, resource=resource_a),
        property_store=store,
    )
    await record_consent_demand(
        **demand(**shared, resource=resource_b, conversation_id="conv-2"),
        property_store=store,
    )
    source = LaneSource()

    authored = await author_consent_granted_events(
        redis=None,
        user_id="user-1",
        provider_id="kdcube",
        granted_claims=[],
        granted_named_service_operations={"slack": ["object.schema"]},
        granted_resource=resource_a,
        connector_app_id=client_id,
        connection_hub_bundle_id="connection-hub@1-0",
        source_factory=lambda _entry: source,
        property_store=store,
    )

    assert authored == 1
    assert source.published[0]["payload"]["resource"] == resource_a
    registry = store.values[
        ("user-1", "connection-hub@1-0", PENDING_DEMANDS_REGISTRY_KEY)
    ]
    assert [entry["resource"] for entry in registry["demands"]] == [resource_b]


@pytest.mark.asyncio
async def test_failed_event_publish_keeps_the_demand_for_retry() -> None:
    store = MemoryPropertyStore()
    await record_consent_demand(**demand(), property_store=store)

    authored = await author_consent_granted_events(
        redis=None,
        user_id="user-1",
        provider_id="slack",
        granted_claims=["slack:post"],
        connection_hub_bundle_id="connection-hub@1-0",
        source_factory=lambda _entry: LaneSource(fail=True),
        property_store=store,
    )

    assert authored == 0
    registry = store.values[
        ("user-1", "connection-hub@1-0", PENDING_DEMANDS_REGISTRY_KEY)
    ]
    assert len(registry["demands"]) == 1

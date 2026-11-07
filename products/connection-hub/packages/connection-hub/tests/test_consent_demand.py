from __future__ import annotations

import pytest

from connection_hub.delegated_to_kdcube.consent_demand import (
    PENDING_DEMANDS_REGISTRY_KEY,
    author_consent_granted_events as _author_consent_granted_events,
    pending_consent_delta,
    read_pending_consent,
    record_consent_demand,
)


_NAMED_SERVICES_RESOURCE = "*/kdcube-services@1-0/public/mcp/named_services*"


async def _no_account_requirements(**_kwargs):
    return []


async def author_consent_granted_events(**kwargs):
    # Existing event-routing cases use a service with no provider account.
    kwargs.setdefault("requirements_loader", _no_account_requirements)
    return await _author_consent_granted_events(**kwargs)


def test_provider_preflight_cannot_announce_an_unverified_agent_card_binding():
    provider = {"provider_id": "google", "claims": ["slides:read"], "tools": ["slides.list"]}
    agent = {**provider, "provider_id": "kdcube", "account_provider_id": "google",
             "account_claim": "slides:read", "account_id": "work"}
    assert pending_consent_delta([agent], []) == []
    # Even a failed provider preflight uses the provider id, not the agent
    # grant's kdcube key; their difference cannot prove Card consent.
    assert pending_consent_delta([agent], [provider]) == []
    assert pending_consent_delta([provider], []) == [provider]


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
async def test_once_grant_event_names_the_committed_invocation_policy() -> None:
    store = MemoryPropertyStore()
    resource = "urn:connection-hub:remote-mcp:mcp_0123456789abcdef01234567"
    await record_consent_demand(
        **demand(
            provider_id="kdcube",
            connector_app_id="claude-code",
            claims=["external_mcp:use"],
            tool_name="external_ops_delete",
            resource=resource,
            outer_operation="delete",
        ),
        property_store=store,
    )
    source = LaneSource()

    authored = await author_consent_granted_events(
        redis=None,
        user_id="user-1",
        provider_id="kdcube",
        granted_claims=["external_mcp:use"],
        granted_resource_operations={resource: ["delete"]},
        granted_resource=resource,
        connector_app_id="claude-code",
        account_id="access-1",
        invocation_policy_mode="once",
        invocation_policy_revision=2,
        invocation_change_id="invoke-delete-1",
        connection_hub_bundle_id="connection-hub@1-0",
        source_factory=lambda _entry: source,
        property_store=store,
    )

    assert authored == 1
    payload = source.published[0]["payload"]
    assert "operation delete for one invocation" in payload["text"]
    assert payload["invocation_policy"] == {
        "mode": "once",
        "revision": 2,
        "change_id": "invoke-delete-1",
    }
    nested = payload["event"]["payload"]["event"]
    assert nested["invocation_policy"] == payload["invocation_policy"]


@pytest.mark.asyncio
async def test_outer_operation_grant_authors_only_the_matching_demand() -> None:
    store = MemoryPropertyStore()
    resource = "https://service.example/mcp"
    client_id = "kdcube-agent:workspace@1-0:main"
    await record_consent_demand(
        **demand(
            provider_id="kdcube",
            connector_app_id=client_id,
            claims=[],
            tool_name="records_export",
            resource=resource,
            outer_operation="records_export",
        ),
        property_store=store,
    )
    source = LaneSource()

    wrong = await author_consent_granted_events(
        redis=None,
        user_id="user-1",
        provider_id="kdcube",
        granted_claims=[],
        granted_resource_operations={resource: ["records_delete"]},
        granted_resource=resource,
        connector_app_id=client_id,
        connection_hub_bundle_id="connection-hub@1-0",
        source_factory=lambda _entry: source,
        property_store=store,
    )
    authored = await author_consent_granted_events(
        redis=None,
        user_id="user-1",
        provider_id="kdcube",
        granted_claims=[],
        granted_resource_operations={resource: ["records_export"]},
        granted_resource=resource,
        connector_app_id=client_id,
        connection_hub_bundle_id="connection-hub@1-0",
        source_factory=lambda _entry: source,
        property_store=store,
    )

    assert wrong == 0
    assert authored == 1
    payload = source.published[0]["payload"]
    assert payload["outer_operation"] == "records_export"
    assert payload["resource_operations"] == {resource: ["records_export"]}
    assert "operation records_export" in payload["text"]


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


@pytest.mark.asyncio
@pytest.mark.parametrize("account_only", [False, True])
async def test_hosted_consent_completes_only_after_requested_account_is_bound(account_only):
    store, source = MemoryPropertyStore(), LaneSource()
    client = "kdcube-agent:workspace@1-0:main"
    await record_consent_demand(**demand(provider_id="kdcube", connector_app_id=client,
        claims=["slides:read"], resource=_NAMED_SERVICES_RESOURCE, tool_name="slides.list",
        namespace="" if account_only else "slides", operation="" if account_only else "object.list",
        **({"account_provider_id": "google", "account_claim": "slides:read", "account_id": "work"}
           if account_only else {})), property_store=store)

    async def requirements(**_kwargs):
        return [{"provider_id": "google", "claims_by_operation": {"object.list": ["slides:read"]}}]

    arguments = dict(redis=None, user_id="user-1", provider_id="kdcube", connector_app_id=client,
        granted_claims=[] if account_only else ["slides:read"], granted_resource=_NAMED_SERVICES_RESOURCE,
        granted_named_service_operations={} if account_only else {"slides": ["object.list"]}, requirements_loader=requirements,
        connection_hub_bundle_id="connection-hub@1-0", source_factory=lambda _entry: source,
        property_store=store)
    # Resource/operation approval, another account's unrelated claim, and an
    # absent binding cannot announce success for this provider requirement.
    for scope in ({}, {"google": {"work": ["slides:write"]}}):
        assert await _author_consent_granted_events(**arguments, granted_account_scope=scope) == 0
        assert source.published == []
        assert len(store.values[("user-1", "connection-hub@1-0", PENDING_DEMANDS_REGISTRY_KEY)]["demands"]) == 1
    assert await _author_consent_granted_events(**arguments,
        granted_account_scope={"google": {"work": ["slides:read"]}}) == 1
    assert len(source.published) == 1


@pytest.mark.asyncio
async def test_hosted_operation_demand_stays_pending_when_requirements_cannot_be_read():
    store, source = MemoryPropertyStore(), LaneSource()
    client = "kdcube-agent:workspace@1-0:main"
    await record_consent_demand(**demand(provider_id="kdcube", connector_app_id=client,
        claims=[], resource=_NAMED_SERVICES_RESOURCE, namespace="slides", operation="object.list"),
        property_store=store)
    assert await _author_consent_granted_events(redis=None, user_id="user-1", provider_id="kdcube",
        connector_app_id=client, granted_claims=[], granted_resource=_NAMED_SERVICES_RESOURCE,
        granted_named_service_operations={"slides": ["object.list"]}, source_factory=lambda _entry: source,
        property_store=store) == 0
    assert source.published == []


@pytest.mark.asyncio
async def test_account_approval_closes_only_the_requested_account_for_the_same_tool():
    store, source = MemoryPropertyStore(), LaneSource()
    client = "kdcube-agent:workspace@1-0:main"
    for account in ("work", "personal"):
        assert await record_consent_demand(**demand(provider_id="kdcube", connector_app_id=client,
            claims=["slides:read"], resource=_NAMED_SERVICES_RESOURCE, tool_name="slides.list",
            account_provider_id="google", account_claim="slides:read", account_id=account),
            property_store=store)
    assert await _author_consent_granted_events(redis=None, user_id="user-1", provider_id="kdcube",
        connector_app_id=client, granted_claims=[], granted_resource=_NAMED_SERVICES_RESOURCE,
        granted_account_scope={"google": {"work": ["slides:read"]}},
        source_factory=lambda _entry: source, property_store=store) == 1
    pending = store.values[("user-1", "connection-hub@1-0", PENDING_DEMANDS_REGISTRY_KEY)]["demands"]
    assert [entry["account_id"] for entry in pending] == ["personal"]
    assert len(source.published) == 1


@pytest.mark.asyncio
async def test_ambiguous_legacy_provider_vocabulary_does_not_prove_account_completion():
    store, source = MemoryPropertyStore(), LaneSource()
    client = "kdcube-agent:workspace@1-0:main"
    await record_consent_demand(**demand(provider_id="kdcube", connector_app_id=client,
        claims=["named_services:use"], resource=_NAMED_SERVICES_RESOURCE,
        namespace="slides", operation="object.list"), property_store=store)

    async def requirements(**_kwargs):
        return [{"provider_id": "google", "claims": ["slides:read", "slides:write"]}]

    assert await _author_consent_granted_events(redis=None, user_id="user-1", provider_id="kdcube",
        connector_app_id=client, granted_claims=["named_services:use"],
        granted_resource=_NAMED_SERVICES_RESOURCE,
        granted_named_service_operations={"slides": ["object.list"]},
        requirements_loader=requirements, source_factory=lambda _entry: source,
        property_store=store) == 0
    assert source.published == []

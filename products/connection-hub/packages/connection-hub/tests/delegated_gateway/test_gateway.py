from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from connection_hub.delegated_gateway import (
    ACCESS_DESCRIBE_TOOL,
    DISCOVER_REQUESTABLE,
    DelegatedGatewayError,
    DelegatedMCPGateway,
    DelegatedMCPProviderRegistry,
    MemoryGatewayAuditSink,
    ProviderCallResult,
    RequestableResource,
)
from connection_hub.delegated_gateway.models import (
    DESCRIPTOR_CHANGED,
    DESCRIPTOR_CURRENT,
)

from .fakes import (
    NOW,
    FakeProvider,
    FakeRequestableReader,
    MemoryPolicy,
    MutableCardReader,
    caller,
    card,
    operation_digest,
    provider_tool,
    resource,
)


def gateway_for(
    current_card,
    *providers,
    policy=None,
    audit=None,
    requestable=None,
    **limits,
):
    cards = MutableCardReader(current_card)
    policy = policy or MemoryPolicy()
    audit = audit or MemoryGatewayAuditSink()
    gateway = DelegatedMCPGateway(
        cards=cards,
        providers=DelegatedMCPProviderRegistry(providers),
        invocation_policy=policy,
        audit=audit,
        requestable=requestable,
        clock=lambda: NOW,
        **limits,
    )
    return gateway, cards, policy, audit


@pytest.mark.asyncio
async def test_one_card_aggregates_two_external_resources_and_managed_provider():
    external_a = resource("urn:external:a", operations=("search",))
    external_b = resource("urn:external:b", operations=("search", "read"))
    managed = resource(
        "urn:managed:knowledge",
        kind="managed_kdcube_mcp",
        operations=("search",),
    )
    external_provider = FakeProvider.for_resources(
        "external", "fake_external", external_a, external_b
    )
    managed_provider = FakeProvider.for_resources(
        "managed", "managed_kdcube_mcp", managed
    )
    gateway, _cards, _policy, _audit = gateway_for(
        card(external_b, managed, external_a), external_provider, managed_provider
    )

    tools = await gateway.list_tools(caller())
    routes = [tool.route for tool in tools if tool.route is not None]

    assert [tool.name for tool in tools] == sorted(tool.name for tool in tools)
    assert tools[0].name.startswith("ch_") or tools[0].name == ACCESS_DESCRIBE_TOOL
    assert {(route.resource_id, route.operation) for route in routes} == {
        ("urn:external:a", "search"),
        ("urn:external:b", "search"),
        ("urn:external:b", "read"),
        ("urn:managed:knowledge", "search"),
    }
    equal_names = [
        tool.name
        for tool in tools
        if tool.route is not None and tool.route.operation == "search"
    ]
    assert len(equal_names) == len(set(equal_names)) == 3


@pytest.mark.asyncio
async def test_list_has_only_granted_current_tools_and_is_deterministic():
    current = resource("urn:external:a", operations=("search", "read"))
    provider = FakeProvider.for_resources("external", "fake_external", current)
    provider.tools[current.resource_id] = (
        provider_tool("ungranted"),
        provider_tool("read", "changed"),
        provider_tool("search"),
    )
    provider.descriptors[current.resource_id] = replace(
        provider.descriptors[current.resource_id],
        operation_digests={
            "ungranted": operation_digest("ungranted"),
            "read": operation_digest("read", "changed"),
            "search": operation_digest("search"),
        },
    )
    gateway, _cards, _policy, _audit = gateway_for(card(current), provider)

    first = await gateway.list_tools(caller())
    second = await gateway.list_tools(caller())
    listed = [tool.route.operation for tool in first if tool.route is not None]

    assert listed == ["search"]
    assert [tool.to_mcp_dict() for tool in first] == [
        tool.to_mcp_dict() for tool in second
    ]


@pytest.mark.asyncio
async def test_live_card_edit_changes_next_list_and_cached_removed_tool_is_denied():
    first = resource("urn:external:a", operations=("search",))
    second = resource("urn:external:b", operations=("read",))
    provider = FakeProvider.for_resources("external", "fake_external", first, second)
    gateway, cards, _policy, _audit = gateway_for(card(first), provider)
    old_tool = next(
        tool for tool in await gateway.list_tools(caller()) if tool.route is not None
    )

    cards.current = card(second, revision=2)
    new_tools = await gateway.list_tools(caller())

    assert old_tool.name not in {tool.name for tool in new_tools}
    with pytest.raises(DelegatedGatewayError) as raised:
        await gateway.call_tool(
            caller(),
            tool_name=old_tool.name,
            arguments={"value": "cached"},
            invocation_id="edit-1",
        )
    assert raised.value.reason == "tool_not_in_current_card"
    assert provider.calls == []
    assert cards.reads == 3


@pytest.mark.asyncio
async def test_call_routes_exact_resource_operation_and_preserves_arguments():
    first = resource("urn:external:a", operations=("search",))
    second = resource("urn:external:b", operations=("search",))
    provider = FakeProvider.for_resources("external", "fake_external", first, second)
    gateway, _cards, policy, audit = gateway_for(card(first, second), provider)
    selected = next(
        tool
        for tool in await gateway.list_tools(caller())
        if tool.route is not None and tool.route.resource_id == second.resource_id
    )

    result = await gateway.call_tool(
        caller(),
        tool_name=selected.name,
        arguments={"value": "exact", "nested": {"count": 2}},
        invocation_id="route-1",
    )

    assert provider.calls == [
        {
            "resource_id": second.resource_id,
            "operation": "search",
            "arguments": {"value": "exact", "nested": {"count": 2}},
            "invocation_id": "route-1",
        }
    ]
    assert result.resource_id == second.resource_id
    assert len(policy.begins) == len(policy.completions) == 1
    assert [event.phase for event in audit.events] == ["admission", "complete"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("card_value", "descriptor_change", "expected"),
    [
        (lambda entry: card(entry, status="revoked"), None, "card_revoked"),
        (lambda entry: card(entry, expires_at=NOW), None, "card_expired"),
        (
            lambda entry: card(entry),
            {"state": DESCRIPTOR_CHANGED},
            "descriptor_changed",
        ),
        (
            lambda entry: card(entry),
            {
                "state": DESCRIPTOR_CURRENT,
                "unavailable_reason": "connected_account_missing",
            },
            "connected_account_missing",
        ),
    ],
)
async def test_revocation_expiry_drift_and_account_readiness_fail_closed(
    card_value, descriptor_change, expected
):
    entry = resource("urn:external:a", operations=("search",))
    provider = FakeProvider.for_resources("external", "fake_external", entry)
    gateway, cards, _policy, _audit = gateway_for(card(entry), provider)
    selected = next(
        tool for tool in await gateway.list_tools(caller()) if tool.route is not None
    )
    cards.current = card_value(entry)
    if descriptor_change:
        provider.descriptors[entry.resource_id] = replace(
            provider.descriptors[entry.resource_id], **descriptor_change
        )

    with pytest.raises(DelegatedGatewayError) as raised:
        await gateway.call_tool(
            caller(),
            tool_name=selected.name,
            arguments={},
            invocation_id="deny-1",
        )

    assert raised.value.reason == expected
    assert provider.calls == []


@pytest.mark.asyncio
async def test_disabled_resource_and_provider_failure_do_not_dispatch():
    enabled = resource("urn:external:a")
    provider = FakeProvider.for_resources("external", "fake_external", enabled)
    gateway, cards, _policy, _audit = gateway_for(card(enabled), provider)
    selected = next(
        tool for tool in await gateway.list_tools(caller()) if tool.route is not None
    )

    cards.current = card(replace(enabled, state="disabled"), revision=2)
    with pytest.raises(DelegatedGatewayError) as disabled:
        await gateway.call_tool(
            caller(), tool_name=selected.name, arguments={}, invocation_id="disabled"
        )
    assert disabled.value.reason == "tool_not_in_current_card"

    cards.current = card(enabled, revision=3)
    provider.fail_descriptor.add(enabled.resource_id)
    with pytest.raises(DelegatedGatewayError) as unavailable:
        await gateway.call_tool(
            caller(), tool_name=selected.name, arguments={}, invocation_id="missing"
        )
    assert unavailable.value.reason == "resource_provider_unavailable"
    assert provider.calls == []


@pytest.mark.asyncio
async def test_once_exhaustion_and_same_invocation_replay_have_one_effect():
    once = resource("urn:external:a", policy_mode="once", policy_remaining=1)
    provider = FakeProvider.for_resources("external", "fake_external", once)
    gateway, _cards, _policy, _audit = gateway_for(card(once), provider)
    tool = next(
        item for item in await gateway.list_tools(caller()) if item.route is not None
    )

    first = await gateway.call_tool(
        caller(),
        tool_name=tool.name,
        arguments={"value": "one"},
        invocation_id="once-1",
    )
    replay = await gateway.call_tool(
        caller(),
        tool_name=tool.name,
        arguments={"value": "one"},
        invocation_id="once-1",
    )
    with pytest.raises(DelegatedGatewayError) as conflict:
        await gateway.call_tool(
            caller(),
            tool_name=tool.name,
            arguments={"value": "changed"},
            invocation_id="once-1",
        )
    with pytest.raises(DelegatedGatewayError) as exhausted:
        await gateway.call_tool(
            caller(), tool_name=tool.name, arguments={}, invocation_id="once-2"
        )

    assert first.replay is False
    assert replay.replay is True
    assert conflict.value.reason == "delegated_invocation_id_conflict"
    assert exhausted.value.reason == "delegated_invocation_limit_exhausted"
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_invocation_id_and_unknown_policy_failure_are_sanitized():
    entry = resource("urn:external:a")
    provider = FakeProvider.for_resources("external", "fake_external", entry)
    gateway, _cards, policy, _audit = gateway_for(card(entry), provider)
    tool = next(
        item for item in await gateway.list_tools(caller()) if item.route is not None
    )

    with pytest.raises(DelegatedGatewayError) as invalid:
        await gateway.call_tool(
            caller(),
            tool_name=tool.name,
            arguments={},
            invocation_id="line\nbreak",
        )
    assert invalid.value.reason == "delegated_invocation_id_required"
    assert policy.begins == []

    class SecretPolicy:
        async def begin(self, request):
            del request
            from connection_hub.delegated_gateway import GatewayInvocationDecision

            return GatewayInvocationDecision(
                dispatch=False, reason="secret_marker_disguised_as_code"
            )

        async def complete(self, request, *, result):
            raise AssertionError((request, result))

    secret_gateway, _cards, _policy, _audit = gateway_for(
        card(entry), provider, policy=SecretPolicy()
    )
    with pytest.raises(DelegatedGatewayError) as denied:
        await secret_gateway.call_tool(
            caller(), tool_name=tool.name, arguments={}, invocation_id="policy-1"
        )
    assert denied.value.reason == "invocation_policy_denied"
    assert "secret_marker" not in repr(denied.value.to_dict())


@pytest.mark.asyncio
async def test_always_allows_distinct_invocations_and_replays_each_id():
    always = resource("urn:external:a", policy_mode="always")
    provider = FakeProvider.for_resources("external", "fake_external", always)
    gateway, _cards, _policy, _audit = gateway_for(card(always), provider)
    tool = next(
        item for item in await gateway.list_tools(caller()) if item.route is not None
    )

    for invocation in ("always-1", "always-2"):
        await gateway.call_tool(
            caller(), tool_name=tool.name, arguments={}, invocation_id=invocation
        )
    replay = await gateway.call_tool(
        caller(), tool_name=tool.name, arguments={}, invocation_id="always-1"
    )

    assert len(provider.calls) == 2
    assert replay.replay is True


@pytest.mark.asyncio
async def test_provider_failure_is_secret_safe_and_does_not_hide_other_provider():
    failed = resource("urn:external:a", operations=("search",))
    healthy = resource(
        "urn:managed:a", kind="managed_kdcube_mcp", operations=("search",)
    )
    external = FakeProvider.for_resources("external", "fake_external", failed)
    managed = FakeProvider.for_resources("managed", "managed_kdcube_mcp", healthy)
    external.secret_marker = "SUPER-SECRET-MARKER"
    external.fail_list.add(failed.resource_id)
    gateway, _cards, _policy, audit = gateway_for(
        card(failed, healthy), external, managed
    )

    tools = await gateway.list_tools(caller())
    assert {tool.route.resource_id for tool in tools if tool.route is not None} == {
        healthy.resource_id
    }

    external.fail_list.clear()
    failed_tool = next(
        tool
        for tool in await gateway.list_tools(caller())
        if tool.route is not None and tool.route.resource_id == failed.resource_id
    )
    external.fail_call.add(failed.resource_id)
    result = await gateway.call_tool(
        caller(),
        tool_name=failed_tool.name,
        arguments={},
        invocation_id="secret-failure",
    )

    public = result.to_public_dict()
    assert public["is_error"] is True
    assert public["structured_content"]["reason"] == "delegated_mcp_provider_failed"
    assert "SUPER-SECRET-MARKER" not in repr(public)
    assert "SUPER-SECRET-MARKER" not in repr(audit.events)


@pytest.mark.asyncio
async def test_access_describe_is_complete_current_and_secret_free():
    entry = resource("urn:external:a", operations=("search", "read"))
    provider = FakeProvider.for_resources("external", "fake_external", entry)
    gateway, cards, _policy, _audit = gateway_for(card(entry), provider)

    payload = await gateway.describe_access(caller())
    via_tool = await gateway.call_tool(
        caller(),
        tool_name=ACCESS_DESCRIBE_TOOL,
        arguments={"include_requestable": False},
        invocation_id="",
    )

    assert payload["schema"] == "connection_hub.delegated_gateway.access.v1"
    assert payload["caller"] == {
        "type": "resident",
        "profile_id": "workspace:researcher",
        "access_id": "agent-access-1",
    }
    assert payload["card"]["revision"] == 1
    assert payload["resources"][0]["provider_id"] == "external"
    assert payload["resources"][0]["operations"] == ["read", "search"]
    assert payload["resources"][0]["accepted_descriptor"]["digest"]
    assert payload["resources"][0]["current_descriptor"]["digest"]
    assert "owner-secret-subject" not in repr(payload)
    assert "owner-secret-subject" not in repr(via_tool.to_public_dict())
    assert (
        via_tool.card_revision == via_tool.result.structured_content["card"]["revision"]
    )
    assert cards.reads == 2


@pytest.mark.asyncio
async def test_requestable_discovery_requires_capability_and_applies_all_bounds():
    granted = resource("urn:req:granted")
    provider = FakeProvider.for_resources("external", "fake_external", granted)
    candidates = (
        RequestableResource(
            resource_id="urn:req:allowed-one",
            kind="fake_external",
            display_label="Allowed",
            identity_scope="grantor",
            owner_subject="owner-secret-subject",
            allowed_profile_ids=("workspace:researcher",),
            recovery={"href": "/connections/request/allowed-one"},
        ),
        RequestableResource(
            resource_id="urn:req:cross-owner",
            kind="fake_external",
            display_label="Cross owner",
            identity_scope="grantor",
            owner_subject="other-owner",
        ),
        RequestableResource(
            resource_id="urn:req:wrong-scope",
            kind="fake_external",
            display_label="Wrong scope",
            identity_scope="service-account",
            owner_subject="owner-secret-subject",
        ),
        RequestableResource(
            resource_id="urn:req:wrong-profile",
            kind="fake_external",
            display_label="Wrong profile",
            identity_scope="grantor",
            owner_subject="owner-secret-subject",
            allowed_profile_ids=("workspace:other",),
        ),
        RequestableResource(
            resource_id="urn:outside:ceiling",
            kind="fake_external",
            display_label="Outside",
            identity_scope="grantor",
            owner_subject="owner-secret-subject",
        ),
        RequestableResource(
            resource_id="urn:req:granted",
            kind="fake_external",
            display_label="Already granted",
            identity_scope="grantor",
            owner_subject="owner-secret-subject",
        ),
    )
    requestable = FakeRequestableReader(candidates)
    gateway, _cards, _policy, _audit = gateway_for(
        card(granted, capabilities=(DISCOVER_REQUESTABLE,)),
        provider,
        requestable=requestable,
    )

    denied = await gateway.describe_access(
        caller(resource_ceiling=("urn:req:*",)), include_requestable=True
    )
    assert denied["requestable_discovery"] == "not_permitted"
    assert requestable.calls == 0

    allowed = await gateway.describe_access(
        caller(
            capabilities=(DISCOVER_REQUESTABLE,),
            resource_ceiling=("urn:req:*",),
        ),
        include_requestable=True,
    )
    assert allowed["requestable_discovery"] == "permitted"
    assert [item["resource_id"] for item in allowed["requestable_resources"]] == [
        "urn:req:allowed-one"
    ]
    assert "owner-secret-subject" not in repr(allowed)


@pytest.mark.asyncio
async def test_malformed_requestable_inventory_fails_closed_without_leaking():
    entry = resource("urn:req:granted")
    provider = FakeProvider.for_resources("external", "fake_external", entry)

    class MalformedRequestableReader:
        async def list_requestable(self, *, caller, card):
            del caller, card
            return [RuntimeError("REQUESTABLE-SECRET-MARKER")]

    gateway, _cards, _policy, _audit = gateway_for(
        card(entry, capabilities=(DISCOVER_REQUESTABLE,)),
        provider,
        requestable=MalformedRequestableReader(),
    )

    payload = await gateway.describe_access(
        caller(capabilities=(DISCOVER_REQUESTABLE,)), include_requestable=True
    )

    assert payload["requestable_discovery"] == "unavailable"
    assert payload["requestable_resources"] == []
    assert "REQUESTABLE-SECRET-MARKER" not in repr(payload)


@pytest.mark.asyncio
async def test_argument_result_inventory_and_audit_limits_are_fail_closed():
    entry = resource("urn:external:a")
    provider = FakeProvider.for_resources("external", "fake_external", entry)
    gateway, _cards, policy, _audit = gateway_for(
        card(entry), provider, max_argument_bytes=40, max_result_bytes=120
    )
    tool = next(
        item for item in await gateway.list_tools(caller()) if item.route is not None
    )

    with pytest.raises(DelegatedGatewayError) as oversized_arguments:
        await gateway.call_tool(
            caller(),
            tool_name=tool.name,
            arguments={"value": "x" * 100},
            invocation_id="large-args",
        )
    assert oversized_arguments.value.reason == "tool_arguments_too_large"
    assert policy.begins == []
    assert provider.calls == []

    with pytest.raises(DelegatedGatewayError) as not_json:
        await gateway.call_tool(
            caller(),
            tool_name=tool.name,
            arguments={"value": object()},
            invocation_id="not-json",
        )
    assert not_json.value.reason == "tool_arguments_not_json"
    assert policy.begins == []

    provider.results[(entry.resource_id, "search")] = ProviderCallResult.from_value(
        {"payload": "y" * 500}
    )
    result = await gateway.call_tool(
        caller(), tool_name=tool.name, arguments={}, invocation_id="large-result"
    )
    assert result.result.is_error is True
    assert (
        result.result.structured_content["reason"] == "delegated_mcp_result_too_large"
    )

    tiny, _cards, _policy, _audit = gateway_for(card(entry), provider, max_tools=1)
    with pytest.raises(DelegatedGatewayError) as inventory:
        await tiny.list_tools(caller())
    assert inventory.value.reason == "tool_inventory_limit_exceeded"

    class BrokenAudit:
        async def record(self, event):
            del event
            raise RuntimeError("audit secret")

    blocked, _cards, blocked_policy, _audit = gateway_for(
        card(entry), provider, audit=BrokenAudit()
    )
    with pytest.raises(DelegatedGatewayError) as no_audit:
        await blocked.call_tool(
            caller(), tool_name=tool.name, arguments={}, invocation_id="audit-down"
        )
    assert no_audit.value.reason == "audit_unavailable"
    assert blocked_policy.begins == []


@pytest.mark.asyncio
async def test_provider_admission_precedes_policy_and_sanitizes_failures():
    entry = resource("urn:external:a")
    provider = FakeProvider.for_resources("external", "fake_external", entry)
    gateway, _cards, policy, _audit = gateway_for(card(entry), provider)
    tool = next(
        item for item in await gateway.list_tools(caller()) if item.route is not None
    )

    provider.denied_admission[entry.resource_id] = "operation_not_consented"
    with pytest.raises(DelegatedGatewayError) as denied:
        await gateway.call_tool(
            caller(), tool_name=tool.name, arguments={}, invocation_id="deny-1"
        )
    assert denied.value.reason == "operation_not_consented"
    assert policy.begins == []
    assert provider.calls == []

    provider.denied_admission[entry.resource_id] = "secret_marker_must_not_escape"
    with pytest.raises(DelegatedGatewayError) as sanitized:
        await gateway.call_tool(
            caller(), tool_name=tool.name, arguments={}, invocation_id="deny-2"
        )
    assert sanitized.value.reason == "provider_admission_denied"
    assert "secret_marker_must_not_escape" not in repr(sanitized.value.to_dict())

    provider.denied_admission.clear()
    provider.fail_admission.add(entry.resource_id)
    provider.secret_marker = "SUPER-SECRET-ADMISSION"
    with pytest.raises(DelegatedGatewayError) as unavailable:
        await gateway.call_tool(
            caller(), tool_name=tool.name, arguments={}, invocation_id="deny-3"
        )
    assert unavailable.value.reason == "resource_provider_unavailable"
    assert "SUPER-SECRET-ADMISSION" not in repr(unavailable.value.to_dict())
    assert policy.begins == []
    assert provider.calls == []


@pytest.mark.asyncio
async def test_client_cancellation_does_not_cancel_effect_or_policy_completion():
    entry = resource("urn:external:a", policy_mode="once", policy_remaining=1)
    provider = FakeProvider.for_resources("external", "fake_external", entry)
    provider.call_started = asyncio.Event()
    provider.call_release = asyncio.Event()
    gateway, _cards, policy, _audit = gateway_for(card(entry), provider)
    tool = next(
        item for item in await gateway.list_tools(caller()) if item.route is not None
    )

    pending = asyncio.create_task(
        gateway.call_tool(
            caller(), tool_name=tool.name, arguments={}, invocation_id="cancel-1"
        )
    )
    await provider.call_started.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert gateway.inflight_count == 1
    assert await gateway.drain(timeout=0) is False
    provider.call_release.set()
    for _attempt in range(20):
        if policy.completions:
            break
        await asyncio.sleep(0)

    replay = await gateway.call_tool(
        caller(), tool_name=tool.name, arguments={}, invocation_id="cancel-1"
    )
    assert replay.replay is True
    assert len(provider.calls) == 1
    assert len(policy.completions) == 1
    assert await gateway.drain(timeout=1) is True

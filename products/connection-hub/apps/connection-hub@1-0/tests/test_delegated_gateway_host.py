from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from connection_hub.delegated_credentials.cards.read_model import (
    CardOperationView,
    CardResourceView,
)
from connection_hub.delegated_credentials.cards.read_model import (
    DelegatedCardView as CardReadView,
)
from connection_hub.delegated_gateway import (
    AcceptedDescriptor,
    DelegatedCardView,
    DelegatedResourceEntry,
    GatewayCaller,
    GatewayInvocationRequest,
    GatewayProviderContext,
    InvocationPolicyView,
    ProviderCallResult,
)


def _module():
    path = Path(__file__).resolve().parents[1] / "surfaces" / "delegated_gateway_host.py"
    spec = importlib.util.spec_from_file_location("delegated_gateway_host_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _read_card(*, client_id: str = "client-1") -> CardReadView:
    operation = CardOperationView(
        name="search",
        state="current",
        accepted_digest="a" * 64,
        current_digest="a" * 64,
        policy={
            "authority": {
                "access_id": "access-1",
                "resource": "https://host/api/integrations/bundles/*/*/knowledge@1-0/public/mcp/knowledge*",
                "surface": "outer",
                "operation": "search",
            },
            "mode": "always",
            "state": "available",
            "revision": 2,
        },
    )
    return CardReadView(
        access_id="access-1",
        client_id=client_id,
        caller_kind="oauth",
        grantor_subject="owner-1",
        delegate_subject="integration:client-1:owner-1",
        source="oauth",
        label="Client",
        card_revision=3,
        catalog_version="catalog-1",
        state="active",
        created_at=1,
        expires_at=2_000_000_000,
        identity_scope="grantor",
        resources=(
            CardResourceView(
                resource="https://host/api/integrations/bundles/*/*/knowledge@1-0/public/mcp/knowledge*",
                kind="catalog",
                provider="",
                label="Knowledge",
                state="current",
                identity_scope="grantor",
                grants=("knowledge:read",),
                operations=(operation,),
                accepted_revision="catalog-1",
                current_revision="catalog-1",
                accepted_digest="b" * 64,
                current_digest="b" * 64,
            ),
        ),
    )


class _AccessService:
    def __init__(self, card):
        self.card = card
        self.reads = []

    async def card_for_access_id(self, *, grantor_subject, access_id):
        self.reads.append((grantor_subject, access_id))
        return self.card

    async def resource_options(self, user):
        assert user["user_id"] == "owner-1"
        return []


def _request(*, client_id: str = "client-1"):
    return SimpleNamespace(
        state=SimpleNamespace(
            delegated_credential={
                "credential": {
                    "subject": "integration:client-1:owner-1",
                    "attrs": {
                        "client_id": client_id,
                        "grantor_subject": "owner-1",
                        "identity_scope": "grantor",
                    },
                },
                "grant_record": {
                    "client_id": client_id,
                    "registry_access_id": "access-1",
                    "card_revision": 3,
                    "grantor_authority": {"grantor_roles": ["kdcube:role:registered"]},
                },
            }
        )
    )


@pytest.mark.asyncio
async def test_hosted_binding_uses_exact_request_card_and_reloads_live_authority():
    module = _module()
    access = _AccessService(_read_card())
    binding = await module.build_hosted_gateway_binding(
        request=_request(),
        access_service=access,
        remote_mcp_service=SimpleNamespace(),
        invocation_policy_service=SimpleNamespace(),
        tenant="tenant-1",
        project="project-1",
        connections={
            "delegated_credentials": {
                "gateway": {
                    "requestable_discovery": {"caller_types": ["oauth"]}
                }
            }
        },
        managed_dispatch=lambda **_kwargs: None,
    )

    assert binding.caller.access_id == "access-1"
    assert binding.caller.caller_profile_id == "client-1"
    assert binding.caller.capabilities == ("discover_requestable",)
    current = await binding.gateway._cards.read_current(binding.caller)
    assert current.resources[0].provider_metadata["bundle_id"] == "knowledge@1-0"
    assert access.reads == [("owner-1", "access-1"), ("owner-1", "access-1")]


@pytest.mark.asyncio
async def test_hosted_binding_rejects_credential_for_another_card_client():
    module = _module()
    with pytest.raises(Exception, match="card_caller_mismatch"):
        await module.build_hosted_gateway_binding(
            request=_request(client_id="client-other"),
            access_service=_AccessService(_read_card(client_id="client-1")),
            remote_mcp_service=SimpleNamespace(),
            invocation_policy_service=SimpleNamespace(),
            tenant="tenant-1",
            project="project-1",
            connections={},
        )


def _gateway_context():
    resource = DelegatedResourceEntry(
        resource_id="https://host/api/integrations/bundles/*/*/knowledge@1-0/public/mcp/knowledge*",
        kind="catalog",
        display_label="Knowledge",
        endpoint_relation="same_kdcube:knowledge@1-0:public:mcp:knowledge",
        grants=("knowledge:read",),
        operations=("search",),
        accepted_descriptor=AcceptedDescriptor(
            revision="catalog-1",
            digest="b" * 64,
            operation_digests={"search": "a" * 64},
        ),
        identity_scope="grantor",
        invocation_policies={
            "search": InvocationPolicyView(
                mode="always", state="available", revision=2
            )
        },
        provider_metadata={
            "gateway_compatible": True,
            "bundle_id": "knowledge@1-0",
            "endpoint_alias": "knowledge",
            "route": "public",
            "current_revision": "catalog-1",
            "current_digest": "b" * 64,
            "current_operation_digests": {"search": "a" * 64},
        },
    )
    card = DelegatedCardView(
        caller_type="oauth",
        caller_profile_id="client-1",
        access_id="access-1",
        card_revision=3,
        status="active",
        expires_at=2_000_000_000,
        source="oauth",
        identity_scope="grantor",
        grantor_subject="owner-1",
        resources=(resource,),
    )
    caller = GatewayCaller(
        caller_type="oauth",
        access_id="access-1",
        caller_profile_id="client-1",
        client_id="client-1",
    )
    return GatewayProviderContext(caller=caller, card=card, resource=resource)


@pytest.mark.asyncio
async def test_managed_host_uses_target_list_for_admission_then_calls_exact_tool():
    module = _module()
    calls = []

    async def dispatch(**kwargs):
        calls.append(kwargs)
        message = kwargs["message"]
        if message["method"] == "tools/list":
            value = {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "tools": [
                        {
                            "name": "search",
                            "description": "Search knowledge",
                            "inputSchema": {"type": "object"},
                        }
                    ]
                },
            }
        else:
            value = {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "structuredContent": {"ok": True},
                    "content": [{"type": "text", "text": "ok"}],
                    "isError": False,
                },
            }
        return {"status_code": 200, "body": json.dumps(value).encode()}

    host = module.HostedManagedKDCubeMCPHost(dispatch=dispatch)
    context = _gateway_context()
    descriptor = await host.current_descriptor(context)
    tools = await host.list_tools(context)
    admission = await host.admit_call(
        context, operation="search", arguments={"query": "q"}, invocation_id="i-1"
    )
    result = await host.call_tool(
        context, operation="search", arguments={"query": "q"}, invocation_id="i-1"
    )

    assert descriptor.operation_digests == {"search": "a" * 64}
    assert [tool.operation for tool in tools] == ["search"]
    assert admission.allowed is True
    assert result.structured_content == {"ok": True}
    assert [call["message"]["method"] for call in calls] == [
        "tools/list",
        "tools/call",
    ]
    assert calls[1]["message"]["params"]["name"] == "search"
    assert calls[1]["message"]["params"]["_meta"] == {
        "connection_hub/invocation_id": "i-1"
    }


class _PolicyService:
    def __init__(self):
        self.completed = []
        self.policy = SimpleNamespace(
            mode="once", state="consumed", revision=4, remaining=0
        )

    async def get(self, **_kwargs):
        return self.policy

    async def begin(self, **_kwargs):
        return SimpleNamespace(
            dispatch=False,
            replay=True,
            result={
                "structured_content": {"ok": True},
                "content": [],
                "is_error": False,
            },
            result_is_error=False,
            reason="delegated_invocation_replayed",
            retryable=False,
            policy=self.policy,
            invocation=SimpleNamespace(),
        )

    async def complete(self, **kwargs):
        self.completed.append(kwargs)


@pytest.mark.asyncio
async def test_policy_adapter_replays_stored_provider_result_and_completes_public_shape():
    module = _module()
    context = _gateway_context()
    request = GatewayInvocationRequest(
        caller=context.caller,
        card=context.card,
        resource=context.resource,
        provider_id="managed_kdcube_mcp",
        operation="search",
        tool_name="qualified-search",
        invocation_id="i-1",
        request_digest="c" * 64,
        authority_revision="d" * 64,
    )
    service = _PolicyService()
    adapter = module.HostedInvocationPolicy(service)

    decision = await adapter.begin(request)
    assert decision.replay is True
    assert decision.result.structured_content == {"ok": True}
    assert decision.public_policy.remaining == 0

    result = ProviderCallResult.from_value({"ok": True})
    await adapter.complete(request, result=result)
    assert service.completed[0]["result"] == result.to_public_dict()
    assert service.completed[0]["owner_subject"] == "owner-1"


def test_managed_locator_and_card_metadata_are_resource_specific():
    module = _module()
    resource = _read_card().resources[0]
    locator = module.managed_surface_locator(resource.resource)
    metadata = module.gateway_resource_metadata(
        resource,
        tenant="tenant-1",
        project="project-1",
        access_id="access-1",
    )

    assert locator.bundle_id == "knowledge@1-0"
    assert locator.endpoint_alias == "knowledge"
    assert metadata.provider_metadata["current_operation_digests"] == {
        "search": "a" * 64
    }
    assert all("access-1" in item.href for item in metadata.recovery)

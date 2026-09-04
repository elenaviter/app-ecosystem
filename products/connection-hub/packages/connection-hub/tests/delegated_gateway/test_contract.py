from __future__ import annotations

from dataclasses import replace

import pytest
from connection_hub.delegated_gateway import (
    AcceptedDescriptor,
    DelegatedGatewayError,
    DelegatedMCPProviderRegistry,
    DelegatedResourceEntry,
    GatewayCaller,
    GatewayContractError,
    GatewayToolRoute,
    ProviderCallAdmission,
    QualifiedToolNameIndex,
    qualified_tool_name,
)

from .fakes import FakeProvider, resource


def test_qualified_name_is_stable_across_labels_and_origins_and_reversible():
    accepted = AcceptedDescriptor(
        revision="r1",
        digest="a" * 64,
        operation_digests={"same tool": "b" * 64},
    )
    first = resource("urn:test:one", operations=("same tool",), label="First")
    route = GatewayToolRoute(
        resource_id=first.resource_id,
        resource_kind=first.kind,
        operation="same tool",
        accepted_descriptor_identity=accepted.operation_identity("same tool"),
        provider_id="provider",
    )
    name = qualified_tool_name(route)
    index = QualifiedToolNameIndex([route])

    assert index.resolve(name) == route
    assert index.name_for(route) == name
    assert len(name) <= 128


def test_equal_upstream_names_are_collision_free_and_descriptor_identity_moves_name():
    first = resource("urn:test:one", operations=("search",))
    second = resource("urn:test:two", operations=("search",))
    routes = [
        GatewayToolRoute(
            resource_id=item.resource_id,
            resource_kind=item.kind,
            operation="search",
            accepted_descriptor_identity=item.accepted_descriptor.operation_identity(
                "search"
            ),
            provider_id="provider",
        )
        for item in (first, second)
    ]
    names = [qualified_tool_name(route) for route in routes]

    assert names[0] != names[1]
    changed = GatewayToolRoute(
        resource_id=first.resource_id,
        resource_kind=first.kind,
        operation="search",
        accepted_descriptor_identity="f" * 64,
        provider_id="provider",
    )
    assert qualified_tool_name(changed) != names[0]


def test_hash_collision_is_rejected_instead_of_ambiguously_routed():
    first = GatewayToolRoute(
        resource_id="urn:test:one",
        resource_kind="fake",
        operation="search",
        accepted_descriptor_identity="a" * 64,
    )
    second = GatewayToolRoute(
        resource_id="urn:test:two",
        resource_kind="fake",
        operation="search",
        accepted_descriptor_identity="b" * 64,
    )
    index = QualifiedToolNameIndex(hash_text=lambda _value: "c" * 64)
    index.add(first)

    with pytest.raises(GatewayContractError, match="qualified_tool_name_collision"):
        index.add(second)


def test_registry_rejects_ambiguous_kind_and_unknown_kind_fails_closed():
    entry = resource("urn:test:one")
    first = FakeProvider.for_resources("first", "fake_external", entry)
    second = FakeProvider.for_resources("second", "fake_external", entry)

    with pytest.raises(GatewayContractError, match="provider_kind_ambiguous"):
        DelegatedMCPProviderRegistry([first, second])

    registry = DelegatedMCPProviderRegistry()
    with pytest.raises(DelegatedGatewayError) as raised:
        registry.provider_for(entry)
    assert raised.value.to_dict()["code"] == "resource_provider_not_found"

    registry = DelegatedMCPProviderRegistry([first])
    with pytest.raises(DelegatedGatewayError) as mismatch:
        registry.provider_for(replace(entry, provider_id="another"))
    assert mismatch.value.reason == "resource_provider_mismatch"


def test_public_provider_metadata_rejects_credential_shaped_fields():
    with pytest.raises(GatewayContractError, match="provider_metadata_not_public"):
        DelegatedResourceEntry(
            resource_id="urn:test:one",
            kind="fake_external",
            display_label="One",
            endpoint_relation="gateway",
            grants=("mcp:use",),
            operations=("search",),
            accepted_descriptor=resource("urn:test:one").accepted_descriptor,
            identity_scope="grantor",
            provider_metadata={"access_token": "must-not-cross"},
        )


def test_resource_selectors_accept_descriptor_globs_but_caller_ids_do_not():
    selector = "*/api/integrations/bundles/*/*/knowledge@1-0/public/mcp/knowledge*"
    entry = replace(resource("urn:test:one"), resource_id=selector)

    assert entry.resource_id == selector
    with pytest.raises(GatewayContractError, match="access_id_invalid"):
        GatewayCaller(
            caller_type="oauth",
            access_id="access-*",
            caller_profile_id="client-1",
        )


def test_contract_records_reject_malformed_nested_authority_and_admission():
    entry = resource("urn:test:one")

    with pytest.raises(GatewayContractError, match="invocation_policy_invalid"):
        DelegatedResourceEntry(
            resource_id=entry.resource_id,
            kind=entry.kind,
            display_label=entry.display_label,
            endpoint_relation=entry.endpoint_relation,
            grants=entry.grants,
            operations=entry.operations,
            accepted_descriptor=entry.accepted_descriptor,
            identity_scope=entry.identity_scope,
            invocation_policies={"search": object()},
        )

    with pytest.raises(GatewayContractError, match="provider_admission_flags_invalid"):
        ProviderCallAdmission(allowed="yes")

    with pytest.raises(GatewayContractError, match="provider_admission_reason_missing"):
        ProviderCallAdmission(allowed=False)

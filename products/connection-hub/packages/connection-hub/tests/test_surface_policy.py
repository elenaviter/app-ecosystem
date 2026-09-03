from __future__ import annotations

import json

from connection_hub.authority_registry import CredentialEnvelope
from connection_hub.delegated_credentials.catalog.authorization import (
    ActiveCatalogCapabilities,
)
from connection_hub.delegated_credentials.catalog.models import CatalogDocument
from connection_hub.delegated_credentials.oauth.surface_policy import (
    authorize_credential_boundary,
    authorize_mcp_capabilities,
    authorize_rest_capabilities,
    extract_mcp_tool_calls,
    managed_mcp_auth_policy,
    managed_rest_auth_policy,
)

RESOURCE = "https://app.example/mcp"
OTHER_RESOURCE = "https://other.example/mcp"


def _catalog(*, tools: dict | None = None) -> ActiveCatalogCapabilities:
    return ActiveCatalogCapabilities(
        CatalogDocument.build(
            {
                "delegated_credentials": {
                    "oauth": {
                        "resources": [
                            {
                                "resource": RESOURCE,
                                "grants": ["records:read", "records:write"],
                                "tools": tools
                                if tools is not None
                                else {
                                    "records.read": {
                                        "grants": ["records:read"]
                                    },
                                    "records.write": {
                                        "grants": ["records:write"]
                                    },
                                },
                            }
                        ]
                    }
                }
            }
        )
    )


def _envelope(*, grants: tuple[str, ...] = ("records:read",)) -> CredentialEnvelope:
    return CredentialEnvelope(
        credential_kind="delegated_client_access",
        issuer_authority_id="delegated_client",
        subject="integration:cli:user-1",
        attrs={
            "resource_grants": {RESOURCE: list(grants)},
            "scopes": list(grants),
        },
    )


def _boundary(*, grants: tuple[str, ...] = ("records:read",), operations=None):
    record = {
        "registry_access_id": "access-1",
        "card_revision": 4,
        "catalog_version": "catalog-before",
        "operations": list(["records.read"] if operations is None else operations),
    }
    return (
        authorize_credential_boundary(
            authority_id="delegated_client",
            required_roles=(),
            required_permissions=(),
            user_roles=("user",),
            user_permissions=grants,
            envelope=_envelope(grants=grants),
            grant_record=record,
            request_resource=RESOURCE,
        ),
        record,
    )


def test_policy_and_tool_call_parsing_are_transport_neutral() -> None:
    policy = managed_mcp_auth_policy(
        {
            "mode": "managed",
            "authority_id": "delegated_client",
            "tools": {"records.read": {"grants": ["records:read"]}},
        }
    )

    assert policy is not None
    assert policy.tool_policies["records.read"].grants == ("records:read",)
    assert extract_mcp_tool_calls(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "records.read"},
            }
        ).encode()
    ) == [(7, "records.read")]


def test_mcp_intersects_the_card_with_the_active_catalog() -> None:
    boundary, record = _boundary()
    policy = managed_mcp_auth_policy(
        {"mode": "managed", "selected_tool_grants": True}
    )
    assert policy is not None

    allowed = authorize_mcp_capabilities(
        boundary=boundary,
        policy=policy,
        catalog=_catalog(),
        grant_record=record,
        request_resource=RESOURCE,
        user_roles=("user",),
        user_permissions=("records:read",),
        tool_calls=[(1, "records.read")],
    )
    removed = authorize_mcp_capabilities(
        boundary=boundary,
        policy=policy,
        catalog=_catalog(tools={"records.write": {"grants": ["records:write"]}}),
        grant_record=record,
        request_resource=RESOURCE,
        user_roles=("user",),
        user_permissions=("records:read",),
        tool_calls=[(1, "records.read")],
    )

    assert allowed.allowed
    assert allowed.available_grants == frozenset({"records:read"})
    assert not removed.allowed
    assert removed.denial is not None
    assert removed.denial.is_rpc
    assert removed.denial.payload["error"]["code"] == (
        "delegated_capability_no_longer_available"
    )


def test_rest_requires_the_operation_selected_on_the_card() -> None:
    boundary, record = _boundary(operations=[])
    policy = managed_rest_auth_policy(
        {
            "mode": "managed",
            "selected_operation_grants": True,
            "operations": {"records.read": {"grants": ["records:read"]}},
        }
    )
    assert policy is not None

    decision = authorize_rest_capabilities(
        boundary=boundary,
        policy=policy,
        catalog=_catalog(),
        grant_record=record,
        request_resource=RESOURCE,
        operation="records.read",
        user_roles=("user",),
        user_permissions=("records:read",),
    )

    assert not decision.allowed
    assert decision.denial is not None
    assert decision.denial.reason == "operation_not_consented"


def test_rest_missing_operation_recovers_operation_and_its_claim_together() -> None:
    boundary, record = _boundary(operations=[])
    policy = managed_rest_auth_policy(
        {
            "mode": "managed",
            "selected_operation_grants": True,
            "operations": {"records.write": {"grants": ["records:write"]}},
        }
    )
    assert policy is not None

    decision = authorize_rest_capabilities(
        boundary=boundary,
        policy=policy,
        catalog=_catalog(),
        grant_record=record,
        request_resource=RESOURCE,
        operation="records.write",
        user_roles=("user",),
        user_permissions=("records:read",),
    )

    assert not decision.allowed
    assert decision.denial is not None
    assert decision.denial.reason == "operation_not_consented"
    assert decision.denial.required_grants == frozenset({"records:write"})
    assert decision.denial.missing_grants == frozenset({"records:write"})
    assert decision.denial.available_grants == frozenset({"records:read"})


def test_mcp_missing_operation_recovers_operation_and_its_claim_together() -> None:
    boundary, record = _boundary(operations=[])
    policy = managed_mcp_auth_policy(
        {"mode": "managed", "selected_tool_grants": True}
    )
    assert policy is not None

    decision = authorize_mcp_capabilities(
        boundary=boundary,
        policy=policy,
        catalog=_catalog(),
        grant_record=record,
        request_resource=RESOURCE,
        user_roles=("user",),
        user_permissions=("records:read",),
        tool_calls=[(7, "records.write")],
    )

    assert not decision.allowed
    assert decision.denial is not None
    assert decision.denial.reason == "operation_not_consented"
    assert decision.denial.rpc_id == 7
    assert decision.denial.payload is not None
    assert decision.denial.payload["ret"]["required_grants"] == [
        "records:write"
    ]
    assert decision.denial.payload["ret"]["missing_grants"] == [
        "records:write"
    ]


def test_equal_tool_names_are_authorized_only_on_the_selected_resource() -> None:
    envelope = CredentialEnvelope(
        credential_kind="delegated_client_access",
        issuer_authority_id="delegated_client",
        subject="integration:cli:user-1",
        attrs={
            "resource_grants": {
                RESOURCE: ["records:read"],
                OTHER_RESOURCE: ["records:read"],
            },
            "scopes": ["records:read"],
        },
    )
    record = {
        "operations": ["search"],
        "resource_operations": {
            RESOURCE: ["search"],
            OTHER_RESOURCE: [],
        },
    }

    first = authorize_credential_boundary(
        authority_id="delegated_client",
        required_roles=(),
        required_permissions=(),
        user_roles=("user",),
        user_permissions=("records:read",),
        envelope=envelope,
        grant_record=record,
        request_resource=RESOURCE,
    )
    second = authorize_credential_boundary(
        authority_id="delegated_client",
        required_roles=(),
        required_permissions=(),
        user_roles=("user",),
        user_permissions=("records:read",),
        envelope=envelope,
        grant_record=record,
        request_resource=OTHER_RESOURCE,
    )

    assert first.allowed and first.granted_operations == frozenset({"search"})
    assert second.allowed and second.granted_operations == frozenset()


def test_wildcard_role_row_does_not_steal_the_resource_match() -> None:
    """The steuer-automation live failure: a card carries the all-resource
    row ``*`` (role grants only, no operations) beside a specific door row
    holding every tool. First-match selection judged the request by ``*`` and
    refused tools the specific row plainly granted; the boundary must judge
    by the MOST SPECIFIC matching row."""
    door = "https://host.example/api/integrations/bundles/t/p/kdcube-services@1-0/public/mcp/productivity"
    pattern = "*/api/integrations/bundles/*/*/kdcube-services@1-0/public/mcp/productivity*"
    envelope = CredentialEnvelope(
        credential_kind="delegated_client_access",
        issuer_authority_id="delegated_client",
        subject="integration:automation:card-1:user-1",
        attrs={
            # dict order puts the wildcard row FIRST, the shape that failed.
            "resource_grants": {
                "*": ["kdcube:role:super-admin"],
                pattern: ["mail:read", "mail:draft"],
            },
            "resource_operations": {
                "*": [],
                pattern: ["productivity_mail_search", "productivity_mail_draft"],
            },
            "scopes": ["kdcube:role:super-admin", "mail:read", "mail:draft"],
        },
    )
    decision = authorize_credential_boundary(
        authority_id="delegated_client",
        required_roles=(),
        required_permissions=(),
        user_roles=("user",),
        user_permissions=("mail:read",),
        envelope=envelope,
        grant_record={"operations": []},
        request_resource=door,
    )
    assert decision.allowed
    assert decision.matched_resource == pattern
    assert decision.granted_operations == frozenset(
        {"productivity_mail_search", "productivity_mail_draft"}
    )
    # An exact row beats every pattern.
    exact_envelope = CredentialEnvelope(
        credential_kind="delegated_client_access",
        issuer_authority_id="delegated_client",
        subject="integration:automation:card-1:user-1",
        attrs={
            "resource_grants": {"*": [], pattern: [], door: ["mail:read"]},
            "resource_operations": {door: ["productivity_mail_search"]},
            "scopes": ["mail:read"],
        },
    )
    exact = authorize_credential_boundary(
        authority_id="delegated_client",
        required_roles=(),
        required_permissions=(),
        user_roles=("user",),
        user_permissions=("mail:read",),
        envelope=exact_envelope,
        grant_record={"operations": []},
        request_resource=door,
    )
    assert exact.allowed and exact.matched_resource == door

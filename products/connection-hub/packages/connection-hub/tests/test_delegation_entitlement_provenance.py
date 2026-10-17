"""Delegated protocol writes preserve links, never entitlement snapshots."""
from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from connection_hub.authority_inventory import (
    AuthorityDelegationEdge, delegation_edges_provenance, delegation_provenance,
)
from connection_hub.delegated_credentials.credential_view import DelegatedCredentialView
from connection_hub.delegated_credentials.oauth.store import GrantStore
from connection_hub.hub.resolver import resolve_delegated_identity_scope

ADMIN = "kdcube:role:super-admin"
SNAPSHOT = {"grantor_roles", "roles", "platform_roles", "permissions",
            "grantor_permissions", "platform_permissions", "economics_budget_bypass", "budget_bypass"}


def legacy_authority():
    return {
        "schema": "connection_hub.grantor_authority.v1",
        "grantor_roles": [ADMIN], "grantor_permissions": ["*"],
        "economics_budget_bypass": True,
        "delegation_edges": [{
            "authority_id": "platform", "identity_ref": "platform:owner",
            "user_id": "owner", "grants": ["records:read"],
            "roles": [ADMIN], "permissions": ["*"], "economics_budget_bypass": True,
        }],
    }


def assert_provenance(record):
    authority = record["grantor_authority"]
    assert not SNAPSHOT.intersection(authority)
    edges = record.get("delegation_edges") or authority["delegation_edges"]
    assert edges[0]["user_id"] == "owner"
    assert edges[0]["grants"] == ["records:read"]
    assert not SNAPSHOT.intersection(edges[0])


def test_legacy_sanitizer_preserves_identity_grants_and_input():
    legacy = legacy_authority()
    before = copy.deepcopy(legacy)
    out = delegation_provenance(legacy)
    assert_provenance({"grantor_authority": out})
    assert legacy == before


def test_edge_serializer_does_not_capture_platform_entitlements():
    edge = AuthorityDelegationEdge(
        authority_id="platform", identity_ref="platform:owner", user_id="owner",
        grants=("records:read",), roles=(ADMIN,), permissions=("*",),
        economics_budget_bypass=True,
    ).to_dict()
    assert edge["grants"] == ["records:read"]
    assert not SNAPSHOT.intersection(edge)


class CapturingRedis:
    def __init__(self):
        self.writes = []

    async def setex(self, key, ttl, value):
        self.writes.append(json.loads(value))


@pytest.mark.parametrize("operation", ["code", "refresh", "access"])
def test_new_protocol_records_sanitize_even_legacy_input(operation):
    async def invoke():
        redis = CapturingRedis()
        store = GrantStore(redis, "test", "test")
        authority = legacy_authority()
        kwargs = {
            "grantor_authority": authority,
            "delegation_edges": authority["delegation_edges"],
        }
        if operation == "code":
            await store.create_auth_code(
                client_id="client", redirect_uri="http://127.0.0.1/callback",
                code_challenge="synthetic", sub="owner", scopes=["records:read"], **kwargs,
            )
        elif operation == "refresh":
            await store.create_refresh_token(client_id="client", sub="owner", scopes=["records:read"], **kwargs)
        else:
            await store.bind_access_grant("synthetic-access", ["records_read"], 60, **kwargs)
        assert len(redis.writes) == 1
        assert_provenance(redis.writes[0])
    asyncio.run(invoke())


def test_legacy_refresh_rotation_drops_snapshots_in_durable_replacement():
    async def invoke():
        replacements = []

        async def rotate(token, replacement, **kwargs):
            replacements.append(replacement)
            return "synthetic-successor"

        store = GrantStore(None, "test", "test", authority_store=SimpleNamespace(rotate_refresh_token=rotate))
        authority = legacy_authority()
        record = {
            "client_id": "client", "sub": "owner", "scopes": ["records:read"],
            "grantor_authority": authority, "delegation_edges": authority["delegation_edges"],
        }
        state = SimpleNamespace(token="synthetic-old", raw="generation-1", record=record)
        assert await store.rotate_refresh_token("synthetic-old", state=state) == "synthetic-successor"
        assert len(replacements) == 1
        assert_provenance(replacements[0])
        assert record["grantor_authority"]["grantor_roles"] == [ADMIN]
    asyncio.run(invoke())


def test_data_scope_projection_cannot_mint_billing_roles_from_caller_fields():
    result = resolve_delegated_identity_scope(
        object(), credential={
            "credential_kind": "delegated_client_access", "issuer_authority_id": "delegated_client",
            "subject": "integration:client:owner",
            "attrs": {"grantor_subject": "owner", "identity_scope": "grantor"},
        },
        grantor_authority=legacy_authority(),
    )
    assert result["ok"]
    assert result["memory_user_ids"] == ["owner"]
    assert not SNAPSHOT.intersection(result["economics"])


def test_credential_view_does_not_present_captured_roles_as_current():
    view = DelegatedCredentialView.from_parts(
        {"subject": "integration:client:owner", "attrs": {"grantor_subject": "owner"}},
        {"grantor_authority": legacy_authority()},
    )
    assert view.grantor_roles == ()

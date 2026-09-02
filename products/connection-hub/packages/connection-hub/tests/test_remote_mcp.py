from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager

import pytest

from connection_hub.delegated_credentials.credential_view import (
    DelegatedCredentialView,
)
from connection_hub.remote_mcp import (
    AUTH_BEARER,
    AUTH_HEADER,
    AUTH_OAUTH,
    CONNECTOR_DISABLED,
    DESCRIPTOR_ACCEPTED,
    DESCRIPTOR_DRIFTED,
    EXTERNAL_MCP_GRANT,
    OAUTH_CLIENT_SOURCE_PROVISIONED,
    BundleStorageRemoteMCPConnectorStore,
    BundleStorageRemoteMCPOAuthStateStore,
    RemoteMCPConnectorConflict,
    RemoteMCPConnectorService,
    RemoteMCPDiscovery,
    RemoteMCPOAuthCredential,
    RemoteMCPProvisionedOAuthClient,
    RemoteMCPOAuthStateError,
    RemoteMCPEndpointDenied,
    RemoteMCPEndpointPolicy,
    RemoteMCPProxy,
    RemoteMCPProxyError,
    RemoteMCPRecordError,
)
from connection_hub.invocation_policy import (
    POLICY_ONCE,
    SURFACE_OUTER,
    BundleStorageInvocationPolicyStore,
    InvocationAuthority,
    InvocationPolicyService,
)


@asynccontextmanager
async def _lock(**_kwargs):
    yield {}


class _Secrets:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    async def set(self, *, owner_subject: str, secret_ref: str, value: str) -> None:
        self.values[(owner_subject, secret_ref)] = value

    async def get(self, *, owner_subject: str, secret_ref: str) -> str | None:
        return self.values.get((owner_subject, secret_ref))

    async def delete(self, *, owner_subject: str, secret_ref: str) -> None:
        self.values.pop((owner_subject, secret_ref), None)


class _Transport:
    def __init__(self) -> None:
        self.tools = [
            {
                "name": "search",
                "description": "Search records",
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
            {
                "name": "delete",
                "description": "Delete a record",
                "input_schema": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                },
            },
        ]
        self.calls: list[dict] = []
        self.headers_seen: list[dict[str, str]] = []
        self.revocations: list[dict] = []

    async def discover(
        self,
        *,
        connector_id: str,
        endpoint: str,
        transport: str,
        headers,
    ):
        del endpoint, transport
        self.headers_seen.append(dict(headers))
        return RemoteMCPDiscovery.build(
            connector_id=connector_id,
            tools=self.tools,
            server_name="Fixture MCP",
            server_version="1.0.0",
            protocol_version="2026-07-28",
        )

    async def call_tool(
        self,
        *,
        connector_id: str,
        endpoint: str,
        transport: str,
        headers,
        tool_name: str,
        arguments,
    ):
        del endpoint, transport
        self.headers_seen.append(dict(headers))
        call = {
            "connector_id": connector_id,
            "tool_name": tool_name,
            "arguments": dict(arguments),
        }
        self.calls.append(call)
        return {"ok": True, **call}

    async def revoke_credential(self, **kwargs):
        self.revocations.append(dict(kwargs))
        return True


async def _public_resolver(_host: str, _port: int):
    return ("93.184.216.34",)


def _service(tmp_path, *, transport=None, secrets=None):
    transport = transport or _Transport()
    secrets = secrets or _Secrets()
    return (
        RemoteMCPConnectorService(
            store=BundleStorageRemoteMCPConnectorStore(tmp_path),
            secret_store=secrets,
            transport=transport,
            endpoint_policy=RemoteMCPEndpointPolicy(resolver=_public_resolver),
            mutation_lock=_lock,
        ),
        transport,
        secrets,
    )


def _oauth_credential(*, expires_at: int = 0) -> RemoteMCPOAuthCredential:
    return RemoteMCPOAuthCredential(
        access_token="oauth-access-token",
        refresh_token="oauth-refresh-token",
        expires_at=expires_at,
        scope="mcp:tools",
        resource="https://mcp.example.test/mcp",
        authorization_server="https://auth.example.test",
        token_endpoint="https://auth.example.test/token",
        revocation_endpoint="https://auth.example.test/revoke",
        client_id="registered-client",
        client_secret="registered-secret",
        token_endpoint_auth_method="client_secret_post",
        client_source=OAUTH_CLIENT_SOURCE_PROVISIONED,
        issued_at=1_788_200_000,
    )


def test_oauth_credential_rejects_malformed_times_with_stable_error():
    payload = _oauth_credential().to_dict()
    payload["expires_at"] = "tomorrow"

    with pytest.raises(RemoteMCPRecordError) as failure:
        RemoteMCPOAuthCredential.from_mapping(payload)

    assert failure.value.reason == "oauth_credential_time_invalid"


def test_provisioned_oauth_client_validates_secret_and_stays_out_of_repr():
    client = RemoteMCPProvisionedOAuthClient.from_mapping(
        {
            "client_id": "provider-console-client",
            "client_secret": "provider-console-secret",
            "token_endpoint_auth_method": "client_secret_basic",
        }
    )

    assert client.to_client_info() == {
        "client_id": "provider-console-client",
        "client_secret": "provider-console-secret",
        "token_endpoint_auth_method": "client_secret_basic",
    }
    assert "provider-console-secret" not in repr(client)

    with pytest.raises(RemoteMCPRecordError) as missing:
        RemoteMCPProvisionedOAuthClient.from_mapping(
            {
                "client_id": "provider-console-client",
                "token_endpoint_auth_method": "client_secret_post",
            }
        )
    assert missing.value.reason == "oauth_client_secret_missing"

    with pytest.raises(RemoteMCPRecordError) as unexpected:
        RemoteMCPProvisionedOAuthClient.from_mapping(
            {
                "client_id": "public-client",
                "client_secret": "must-not-be-sent",
                "token_endpoint_auth_method": "none",
            }
        )
    assert unexpected.value.reason == "oauth_client_secret_unexpected"


def test_oauth_credential_preserves_client_source_and_reads_older_bundle():
    credential = _oauth_credential()

    assert RemoteMCPOAuthCredential.from_json(
        credential.to_json()
    ).client_source == OAUTH_CLIENT_SOURCE_PROVISIONED

    older = credential.to_dict()
    older.pop("client_source")
    assert RemoteMCPOAuthCredential.from_mapping(older).client_source == ""


def _view(
    connector,
    *,
    owner="user-1",
    client_id="claude-code",
    operations=("search",),
    grants=None,
):
    return DelegatedCredentialView(
        client_id=client_id,
        registry_access_id="access-1",
        card_revision=4,
        grantor_user_id=owner,
        resource_grants={
            connector.resource: tuple(grants or (EXTERNAL_MCP_GRANT,))
        },
        resource_operations={connector.resource: tuple(operations)},
        present=True,
    )


@pytest.mark.asyncio
async def test_proxy_once_replays_without_redispatch_and_rejects_changed_arguments(
    tmp_path,
):
    service, transport, _secrets = _service(tmp_path / "connectors")
    connector = await service.create(
        owner_subject="user-1",
        label="Operations",
        endpoint="https://mcp.example.test/mcp",
        now=1_788_200_000,
    )
    policy_service = InvocationPolicyService(
        store=BundleStorageInvocationPolicyStore(tmp_path / "policies"),
        mutation_lock=_lock,
    )
    await policy_service.set_policy(
        owner_subject="user-1",
        authority=InvocationAuthority(
            access_id="access-1",
            resource=connector.resource,
            surface=SURFACE_OUTER,
            operation="search",
        ),
        mode=POLICY_ONCE,
        now=1_788_200_001,
    )
    proxy = RemoteMCPProxy(service, invocation_policies=policy_service)
    proxy_name = connector.tool_map()["search"].proxy_name

    first = await proxy.call(
        view=_view(connector),
        proxy_name=proxy_name,
        arguments={"query": "failed jobs"},
        invocation_id="invoke-1",
    )
    replay = await proxy.call(
        view=_view(connector),
        proxy_name=proxy_name,
        arguments={"query": "failed jobs"},
        invocation_id="invoke-1",
    )
    with pytest.raises(RemoteMCPProxyError) as changed:
        await proxy.call(
            view=_view(connector),
            proxy_name=proxy_name,
            arguments={"query": "different"},
            invocation_id="invoke-1",
        )
    with pytest.raises(RemoteMCPProxyError) as exhausted:
        await proxy.call(
            view=_view(connector),
            proxy_name=proxy_name,
            arguments={"query": "another"},
            invocation_id="invoke-2",
        )

    assert first == replay
    assert [item["tool_name"] for item in transport.calls] == ["search"]
    assert changed.value.reason == "delegated_invocation_id_conflict"
    assert exhausted.value.reason == "delegated_invocation_limit_exhausted"
    assert exhausted.value.to_dict()["available_choices"] == [
        "allow_once",
        "allow_always",
    ]


@pytest.mark.asyncio
async def test_proxy_descriptor_denial_happens_before_once_consumption(tmp_path):
    service, transport, _secrets = _service(tmp_path / "connectors")
    connector = await service.create(
        owner_subject="user-1",
        label="Operations",
        endpoint="https://mcp.example.test/mcp",
        now=1_788_200_000,
    )
    policy_service = InvocationPolicyService(
        store=BundleStorageInvocationPolicyStore(tmp_path / "policies"),
        mutation_lock=_lock,
    )
    authority = InvocationAuthority(
        access_id="access-1",
        resource=connector.resource,
        surface=SURFACE_OUTER,
        operation="search",
    )
    await policy_service.set_policy(
        owner_subject="user-1",
        authority=authority,
        mode=POLICY_ONCE,
        now=1_788_200_001,
    )
    transport.tools[0] = {
        **transport.tools[0],
        "description": "Changed without owner acceptance",
    }
    proxy = RemoteMCPProxy(service, invocation_policies=policy_service)

    with pytest.raises(RemoteMCPProxyError) as denied:
        await proxy.call(
            view=_view(connector),
            proxy_name=connector.tool_map()["search"].proxy_name,
            arguments={"query": "failed jobs"},
            invocation_id="invoke-1",
        )

    assert denied.value.reason == "operation_descriptor_changed"
    current = await policy_service.get(owner_subject="user-1", authority=authority)
    assert current is not None and current.remaining == 1
    assert transport.calls == []


@pytest.mark.asyncio
async def test_connector_secret_stays_out_of_durable_and_public_records(tmp_path):
    service, transport, secrets = _service(tmp_path)
    connector = await service.create(
        owner_subject="user-1",
        label="Paid search",
        endpoint="https://mcp.example.test/api/mcp",
        credential_mode=AUTH_BEARER,
        credential_value="super-secret-upstream-token",
        now=1_788_200_000,
    )

    assert connector.credential_present is True
    assert connector.to_public_dict()["credential_present"] is True
    assert "credential_ref" not in connector.to_public_dict()
    assert "super-secret-upstream-token" not in json.dumps(connector.to_dict())
    assert list(secrets.values.values()) == ["super-secret-upstream-token"]
    assert transport.headers_seen == [
        {"Authorization": "Bearer super-secret-upstream-token"}
    ]

    durable_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in tmp_path.rglob("*.json")
    )
    assert "super-secret-upstream-token" not in durable_text


@pytest.mark.asyncio
async def test_oauth_connector_keeps_token_bundle_only_in_secret_store(tmp_path):
    service, transport, secrets = _service(tmp_path)
    oauth = _oauth_credential(expires_at=1_788_203_600)

    connector = await service.create(
        owner_subject="user-1",
        label="OAuth records",
        endpoint="https://mcp.example.test/mcp",
        credential_mode=AUTH_OAUTH,
        credential_value=oauth.to_json(),
        now=1_788_200_000,
    )

    assert connector.credential_mode == AUTH_OAUTH
    assert connector.credential_present is True
    assert transport.headers_seen == [
        {"Authorization": "Bearer oauth-access-token"}
    ]
    assert list(secrets.values.values()) == [oauth.to_json()]
    assert "oauth-access-token" not in json.dumps(connector.to_dict())
    assert "oauth-refresh-token" not in json.dumps(connector.to_public_dict())
    durable_text = "\n".join(
        path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.json")
    )
    assert "oauth-access-token" not in durable_text
    assert "registered-secret" not in durable_text

    resolved = await service.resolve_credential(
        owner_subject="user-1",
        connector_id=connector.connector_id,
    )
    resolved_oauth = RemoteMCPOAuthCredential.from_json(resolved.value)
    assert resolved_oauth.client_source == OAUTH_CLIENT_SOURCE_PROVISIONED
    assert "registered-secret" not in repr(resolved)


@pytest.mark.asyncio
async def test_oauth_reauthorization_revokes_the_replaced_upstream_grant(tmp_path):
    service, transport, secrets = _service(tmp_path)
    first = _oauth_credential(expires_at=1_788_203_600)
    connector = await service.create(
        owner_subject="user-1",
        label="OAuth records",
        endpoint="https://mcp.example.test/mcp",
        credential_mode=AUTH_OAUTH,
        credential_value=first.to_json(),
        now=1_788_200_000,
    )
    second = RemoteMCPOAuthCredential(
        **{
            **first.__dict__,
            "access_token": "replacement-access-token",
            "refresh_token": "replacement-refresh-token",
        }
    )

    updated = await service.replace_credential(
        owner_subject="user-1",
        connector_id=connector.connector_id,
        expected_revision=connector.revision,
        credential_mode=AUTH_OAUTH,
        credential_value=second.to_json(),
        now=1_788_200_100,
    )

    assert updated.revision == connector.revision + 1
    assert len(transport.revocations) == 1
    revoked = transport.revocations[0]["credential"]
    assert RemoteMCPOAuthCredential.from_json(revoked.value).access_token == (
        "oauth-access-token"
    )
    assert list(secrets.values.values()) == [second.to_json()]


@pytest.mark.asyncio
async def test_failed_oauth_reauthorization_revokes_only_the_new_grant(tmp_path):
    service, transport, secrets = _service(tmp_path)
    first = _oauth_credential(expires_at=1_788_203_600)
    connector = await service.create(
        owner_subject="user-1",
        label="OAuth records",
        endpoint="https://mcp.example.test/mcp",
        credential_mode=AUTH_OAUTH,
        credential_value=first.to_json(),
        now=1_788_200_000,
    )
    second = RemoteMCPOAuthCredential(
        **{
            **first.__dict__,
            "access_token": "replacement-access-token",
            "refresh_token": "replacement-refresh-token",
        }
    )

    with pytest.raises(RemoteMCPConnectorConflict) as failure:
        await service.replace_credential(
            owner_subject="user-1",
            connector_id=connector.connector_id,
            expected_revision=connector.revision + 1,
            credential_mode=AUTH_OAUTH,
            credential_value=second.to_json(),
            now=1_788_200_100,
        )

    assert failure.value.reason == "connector_revision_moved"
    assert len(transport.revocations) == 1
    revoked = transport.revocations[0]["credential"]
    assert RemoteMCPOAuthCredential.from_json(revoked.value).access_token == (
        "replacement-access-token"
    )
    assert list(secrets.values.values()) == [first.to_json()]


@pytest.mark.asyncio
async def test_failed_oauth_creation_revokes_the_new_upstream_grant(tmp_path):
    class _FailingTransport(_Transport):
        async def discover(self, **_kwargs):
            raise RuntimeError("fixture discovery failed")

    transport = _FailingTransport()
    service, _transport, secrets = _service(tmp_path, transport=transport)
    oauth = _oauth_credential(expires_at=1_788_203_600)

    with pytest.raises(RuntimeError, match="fixture discovery failed"):
        await service.create(
            owner_subject="user-1",
            label="OAuth records",
            endpoint="https://mcp.example.test/mcp",
            credential_mode=AUTH_OAUTH,
            credential_value=oauth.to_json(),
            now=1_788_200_000,
        )

    assert len(transport.revocations) == 1
    assert secrets.values == {}


@pytest.mark.asyncio
async def test_oauth_state_is_single_use_and_keeps_transaction_in_user_secret(
    tmp_path,
):
    secrets_store = _Secrets()
    states = BundleStorageRemoteMCPOAuthStateStore(
        tmp_path, secret_store=secrets_store
    )
    transaction = {
        "owner_subject": "user-1",
        "code_verifier": "verifier-that-must-remain-secret",
        "client_secret": "dynamic-registration-secret",
        "endpoint": "https://mcp.example.test/mcp",
    }

    handle = await states.create(
        owner_subject="user-1",
        transaction=transaction,
        ttl_seconds=900,
        now=1_788_200_000,
    )

    durable_text = "\n".join(
        path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.json")
    )
    assert handle.state not in durable_text
    assert "verifier-that-must-remain-secret" not in durable_text
    assert "dynamic-registration-secret" not in durable_text
    assert len(secrets_store.values) == 1

    consumed = await states.consume(state=handle.state, now=1_788_200_001)
    assert consumed == transaction
    assert secrets_store.values == {}
    with pytest.raises(RemoteMCPOAuthStateError) as replay:
        await states.consume(state=handle.state, now=1_788_200_002)
    assert replay.value.reason == "oauth_state_missing_or_used"


@pytest.mark.asyncio
async def test_expired_oauth_state_is_purged_with_its_secret(tmp_path):
    secrets_store = _Secrets()
    states = BundleStorageRemoteMCPOAuthStateStore(
        tmp_path, secret_store=secrets_store
    )
    await states.create(
        owner_subject="user-1",
        transaction={"code_verifier": "secret"},
        ttl_seconds=60,
        now=int(time.time()) - 120,
    )

    assert await states.purge_expired(now=int(time.time())) == 1
    assert secrets_store.values == {}
    assert list(states.root.glob("*.json")) == []


@pytest.mark.asyncio
async def test_proxy_intersects_owner_resource_grant_and_exact_operation(tmp_path):
    service, transport, _secrets = _service(tmp_path)
    connector = await service.create(
        owner_subject="user-1",
        label="Operations",
        endpoint="https://mcp.example.test/mcp",
        now=1_788_200_000,
    )
    proxy = RemoteMCPProxy(service)
    search = connector.tool_map()["search"]

    visible = await proxy.list_authorized(_view(connector))
    assert [item.tool.name for item in visible] == ["search"]
    result = await proxy.call(
        view=_view(connector),
        proxy_name=search.proxy_name,
        arguments={"query": "failed jobs"},
    )
    assert result["tool_name"] == "search"

    with pytest.raises(RemoteMCPProxyError) as absent_operation:
        await proxy.call(
            view=_view(connector, operations=()),
            proxy_name=search.proxy_name,
            arguments={"query": "failed jobs"},
        )
    assert absent_operation.value.reason == "operation_not_consented"
    assert absent_operation.value.consent_required is True

    with pytest.raises(RemoteMCPProxyError) as absent_grant:
        await proxy.call(
            view=_view(connector, grants=("something-else",)),
            proxy_name=search.proxy_name,
            arguments={"query": "failed jobs"},
        )
    assert absent_grant.value.reason == "connector_grant_not_consented"

    with pytest.raises(Exception):
        await proxy.call(
            view=_view(connector, owner="user-2"),
            proxy_name=search.proxy_name,
            arguments={"query": "failed jobs"},
        )
    assert [call["tool_name"] for call in transport.calls] == ["search"]


@pytest.mark.asyncio
async def test_ungranted_tool_names_exact_card_and_allow_once_or_always(tmp_path):
    service, transport, _secrets = _service(tmp_path)
    connector = await service.create(
        owner_subject="user-1",
        label="Operations",
        endpoint="https://mcp.example.test/mcp",
        now=1_788_200_000,
    )
    grant_urls: list[dict] = []

    def grant_url(view, decision, invocation_id):
        grant_urls.append(
            {
                "access_id": view.registry_access_id,
                "resource": decision.resource,
                "operation": decision.tool.name,
                "invocation_id": invocation_id,
            }
        )
        return "https://hub.example/grant?invocation_policy=choose"

    proxy = RemoteMCPProxy(service, grant_url_builder=grant_url)
    delete = connector.tool_map()["delete"]

    with pytest.raises(RemoteMCPProxyError) as denied:
        await proxy.call(
            view=_view(connector, operations=("search",)),
            proxy_name=delete.proxy_name,
            arguments={"id": "record-1"},
            invocation_id="invoke-delete-1",
        )

    payload = denied.value.to_dict()
    consent = payload["consent"]
    assert payload["code"] == "operation_not_consented"
    assert payload["access_id"] == "access-1"
    assert payload["card_revision"] == 4
    assert payload["available_choices"] == ["allow_once", "allow_always"]
    assert consent["kind"] == "delegated_agent_grant"
    assert consent["agent_client_id"] == "claude-code"
    assert consent["access_id"] == "access-1"
    assert consent["resource"] == connector.resource
    assert consent["outer_operation"] == "delete"
    assert consent["invocation_policy"] == "choose"
    assert consent["invocation_change_id"] == "invoke-delete-1"
    assert consent["connection_hub_url"] == (
        "https://hub.example/grant?invocation_policy=choose"
    )
    assert consent["grant"] == {
        "operation": "delegated_agent_grant_create",
        "payload": {
            "client_id": "claude-code",
            "access_id": "access-1",
            "resource": connector.resource,
            "claims": [EXTERNAL_MCP_GRANT],
            "resource_operations": {connector.resource: ["delete"]},
            "invocation_change_id": "invoke-delete-1",
        },
    }
    assert grant_urls == [
        {
            "access_id": "access-1",
            "resource": connector.resource,
            "operation": "delete",
            "invocation_id": "invoke-delete-1",
        }
    ]
    assert transport.calls == []


@pytest.mark.asyncio
async def test_changed_tool_descriptor_fails_closed_until_owner_accepts(tmp_path):
    service, transport, _secrets = _service(tmp_path)
    connector = await service.create(
        owner_subject="user-1",
        label="Operations",
        endpoint="https://mcp.example.test/mcp",
        now=1_788_200_000,
    )
    proxy = RemoteMCPProxy(service)
    old_proxy_name = connector.tool_map()["search"].proxy_name
    transport.tools[0] = {
        **transport.tools[0],
        "description": "Search and return private record bodies",
    }

    with pytest.raises(RemoteMCPProxyError) as changed:
        await proxy.call(
            view=_view(connector),
            proxy_name=old_proxy_name,
            arguments={"query": "failed jobs"},
        )
    assert changed.value.reason == "operation_descriptor_changed"
    assert transport.calls == []

    refreshed = await service.refresh(
        owner_subject="user-1",
        connector_id=connector.connector_id,
        expected_revision=connector.revision,
        now=1_788_200_010,
    )
    assert refreshed.descriptor_state == DESCRIPTOR_DRIFTED
    assert refreshed.drift["changed"] == ("search",)
    assert refreshed.descriptor_digest == connector.descriptor_digest

    accepted = await service.accept_descriptor(
        owner_subject="user-1",
        connector_id=connector.connector_id,
        expected_revision=refreshed.revision,
        now=1_788_200_020,
    )
    assert accepted.descriptor_state == DESCRIPTOR_ACCEPTED
    assert accepted.descriptor_revision == 2
    assert accepted.descriptor_digest != connector.descriptor_digest
    assert await proxy.call(
        view=_view(accepted),
        proxy_name=accepted.tool_map()["search"].proxy_name,
        arguments={"query": "failed jobs"},
    )


@pytest.mark.asyncio
async def test_disable_revoke_and_revision_precondition_are_live(tmp_path):
    service, _transport, _secrets = _service(tmp_path)
    connector = await service.create(
        owner_subject="user-1",
        label="Operations",
        endpoint="https://mcp.example.test/mcp",
        credential_mode=AUTH_HEADER,
        credential_header="X-API-Key",
        credential_value="key-value",
        now=1_788_200_000,
    )
    disabled = await service.set_enabled(
        owner_subject="user-1",
        connector_id=connector.connector_id,
        enabled=False,
        expected_revision=connector.revision,
        now=1_788_200_010,
    )
    assert disabled.state == CONNECTOR_DISABLED
    with pytest.raises(RemoteMCPConnectorConflict) as stale:
        await service.set_enabled(
            owner_subject="user-1",
            connector_id=connector.connector_id,
            enabled=True,
            expected_revision=connector.revision,
        )
    assert stale.value.reason == "connector_revision_moved"
    assert stale.value.current_revision == disabled.revision

    deleted = await service.delete(
        owner_subject="user-1",
        connector_id=connector.connector_id,
        expected_revision=disabled.revision,
        now=1_788_200_020,
    )
    assert deleted.credential_present is False
    assert await service.list(owner_subject="user-1") == []


@pytest.mark.asyncio
async def test_endpoint_policy_defaults_to_public_https():
    async def private_resolver(_host: str, _port: int):
        return ("127.0.0.1",)

    policy = RemoteMCPEndpointPolicy(resolver=private_resolver)
    with pytest.raises(RemoteMCPEndpointDenied) as http:
        await policy.validate("http://example.test/mcp")
    assert http.value.reason == "endpoint_scheme_not_allowed"
    with pytest.raises(RemoteMCPEndpointDenied) as private:
        await policy.validate("https://internal.example.test/mcp")
    assert private.value.reason == "endpoint_private_network_forbidden"
    with pytest.raises(RemoteMCPEndpointDenied) as userinfo:
        await policy.validate("https://token@example.test/mcp")
    assert userinfo.value.reason == "endpoint_userinfo_forbidden"

    local_fixture = RemoteMCPEndpointPolicy(
        allow_http=True,
        allowed_hosts=frozenset({"fixture.local"}),
        resolver=private_resolver,
    )
    assert (
        await local_fixture.validate("http://fixture.local:8080/mcp")
        == "http://fixture.local:8080/mcp"
    )


@pytest.mark.asyncio
async def test_endpoint_policy_rejects_any_non_global_connect_address():
    async def mixed_resolver(_host: str, _port: int):
        return ("93.184.216.34", "100.64.0.1")

    policy = RemoteMCPEndpointPolicy(resolver=mixed_resolver)

    with pytest.raises(RemoteMCPEndpointDenied) as denied:
        await policy.connect_addresses("rebinder.example", 443)

    assert denied.value.reason == "endpoint_private_network_forbidden"

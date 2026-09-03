from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from connection_hub_cli.authorization.client import OAuthClient
from connection_hub_cli.authorization.discovery import (
    HttpxOAuthTransport,
    OAuthDiscovery,
)
from connection_hub_cli.authorization.models import (
    AuthorizationServerMetadata,
    OAuthClientRegistration,
)
from connection_hub_cli.authorization.pkce import generate_pkce
from connection_hub_cli.errors import AuthorizationError


class _Transport:
    def __init__(self) -> None:
        self.gets: list[str] = []
        self.posts: list[tuple[str, dict]] = []
        self.forms: list[tuple[str, dict]] = []
        self.values: dict[str, dict] = {}

    async def get_json(self, url: str):
        self.gets.append(url)
        return self.values[url]

    async def post_json(self, url: str, payload):
        self.posts.append((url, dict(payload)))
        return self.values[url]

    async def post_form(self, url: str, payload):
        self.forms.append((url, dict(payload)))
        return self.values[url]


def _server_metadata() -> AuthorizationServerMetadata:
    return AuthorizationServerMetadata.from_mapping(
        {
            "issuer": "https://auth.example.test",
            "authorization_endpoint": (
                "https://auth.example.test/oauth/authorize?existing=1"
            ),
            "token_endpoint": "https://auth.example.test/oauth/token",
            "registration_endpoint": "https://auth.example.test/oauth/register",
            "revocation_endpoint": "https://auth.example.test/oauth/revoke",
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "authorization_response_iss_parameter_supported": True,
        },
        expected_issuer="https://auth.example.test",
    )


@pytest.mark.asyncio
async def test_discovery_binds_resource_and_issuer() -> None:
    transport = _Transport()
    resource_metadata = (
        "https://runtime.example.test/.well-known/oauth-protected-resource"
        "?resource=https%3A%2F%2Fruntime.example.test%2Fmanagement"
    )
    server_metadata = "https://auth.example.test/.well-known/oauth-authorization-server"
    transport.values[resource_metadata] = {
        "resource": "https://runtime.example.test/management",
        "authorization_servers": ["https://auth.example.test"],
    }
    transport.values[server_metadata] = {
        "issuer": "https://auth.example.test",
        "authorization_endpoint": "https://auth.example.test/oauth/authorize",
        "token_endpoint": "https://auth.example.test/oauth/token",
        "registration_endpoint": "https://auth.example.test/oauth/register",
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "response_types_supported": ["code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    }

    result = await OAuthDiscovery(transport=transport).discover(
        protected_resource_metadata_url=resource_metadata,
        expected_resource="https://runtime.example.test/management",
    )

    assert result.protected_resource.resource.endswith("/management")
    assert result.authorization_server.issuer == "https://auth.example.test"
    assert transport.gets == [resource_metadata, server_metadata]


@pytest.mark.asyncio
async def test_dcr_authorization_exchange_and_refresh() -> None:
    transport = _Transport()
    transport.values["https://auth.example.test/oauth/register"] = {
        "client_id": "native-client",
        "redirect_uris": ["http://127.0.0.1:9123/callback"],
        "token_endpoint_auth_method": "none",
    }
    transport.values["https://auth.example.test/oauth/token"] = {
        "access_token": "first-access",
        "refresh_token": "first-refresh",
        "token_type": "Bearer",
        "expires_in": 3600,
        "access_id": "access_cli",
    }
    client = OAuthClient(transport=transport)
    metadata = _server_metadata()
    registration = await client.register_native_client(
        metadata=metadata,
        redirect_uri="http://127.0.0.1:9123/callback",
    )
    pkce = generate_pkce()

    authorize_url = client.authorization_url(
        metadata=metadata,
        client=registration,
        redirect_uri="http://127.0.0.1:9123/callback",
        resource="urn:kdcube:management:deployment:demo-tenant:demo-project",
        pkce=pkce,
        scope="management.read",
        extra_parameters={"audience": "demo-tenant/demo-project"},
    )
    query = parse_qs(urlsplit(authorize_url).query)
    assert query["existing"] == ["1"]
    assert query["code_challenge"] == [pkce.code_challenge]
    assert query["audience"] == ["demo-tenant/demo-project"]

    token = await client.exchange_code(
        metadata=metadata,
        client=registration,
        redirect_uri="http://127.0.0.1:9123/callback",
        resource="urn:kdcube:management:deployment:demo-tenant:demo-project",
        code="authorization-code",
        code_verifier=pkce.code_verifier,
        scope="management.read",
        now=1000,
    )
    assert token.access_id == "access_cli"
    assert transport.posts[0][1]["application_type"] == "native"
    assert transport.forms[0][1]["code_verifier"] == pkce.code_verifier

    transport.values["https://auth.example.test/oauth/token"] = {
        "access_token": "second-access",
        "refresh_token": "second-refresh",
        "token_type": "Bearer",
        "expires_in": 3600,
    }
    refreshed = await client.refresh(
        metadata=metadata,
        client=registration,
        resource="urn:kdcube:management:deployment:demo-tenant:demo-project",
        refresh_token=token.refresh_token,
        now=2000,
    )
    assert refreshed.refresh_token == "second-refresh"
    assert transport.forms[-1][1]["grant_type"] == "refresh_token"

    transport.values["https://auth.example.test/oauth/revoke"] = {}
    await client.revoke(
        metadata=metadata,
        client=registration,
        token=refreshed.refresh_token,
        token_type_hint="refresh_token",
    )
    assert transport.forms[-1] == (
        "https://auth.example.test/oauth/revoke",
        {
            "token": "second-refresh",
            "token_type_hint": "refresh_token",
            "client_id": "native-client",
        },
    )


@pytest.mark.asyncio
async def test_provisioned_public_client_avoids_registration() -> None:
    transport = _Transport()
    registration = await OAuthClient(transport=transport).register_native_client(
        metadata=_server_metadata(),
        redirect_uri="http://127.0.0.1:9123/callback",
        provisioned_client_id="provisioned-client",
    )
    assert registration.client_id == "provisioned-client"
    assert transport.posts == []


def test_authorization_extensions_cannot_replace_security_parameters() -> None:
    client = OAuthClient(transport=_Transport())
    registration = OAuthClientRegistration(
        client_id="native-client",
        redirect_uris=("http://127.0.0.1:9123/callback",),
    )
    with pytest.raises(AuthorizationError) as raised:
        client.authorization_url(
            metadata=_server_metadata(),
            client=registration,
            redirect_uri="http://127.0.0.1:9123/callback",
            resource="https://runtime.example.test/management",
            pkce=generate_pkce(),
            extra_parameters={"redirect_uri": "https://attacker.example/callback"},
        )
    assert raised.value.code == "oauth_parameter_reserved"


def test_authorization_endpoint_cannot_preload_security_parameters() -> None:
    client = OAuthClient(transport=_Transport())
    server = _server_metadata()
    metadata = AuthorizationServerMetadata(
        issuer=server.issuer,
        authorization_endpoint=(
            "https://auth.example.test/oauth/authorize?state=attacker-state"
        ),
        token_endpoint=server.token_endpoint,
        registration_endpoint=server.registration_endpoint,
        revocation_endpoint=server.revocation_endpoint,
        scopes_supported=server.scopes_supported,
        supports_refresh=server.supports_refresh,
        authorization_response_issuer_required=(
            server.authorization_response_issuer_required
        ),
    )
    registration = OAuthClientRegistration(
        client_id="native-client",
        redirect_uris=("http://127.0.0.1:9123/callback",),
    )

    with pytest.raises(AuthorizationError) as raised:
        client.authorization_url(
            metadata=metadata,
            client=registration,
            redirect_uri="http://127.0.0.1:9123/callback",
            resource="https://runtime.example.test/management",
            pkce=generate_pkce(),
        )

    assert raised.value.code == "oauth_authorization_endpoint_parameter_conflict"


@pytest.mark.asyncio
async def test_http_transport_never_returns_provider_error_body() -> None:
    import httpx2

    marker = "provider-secret-marker"

    def handler(_request):
        return httpx2.Response(400, json={"error_description": marker})

    transport = HttpxOAuthTransport(transport=httpx2.MockTransport(handler))
    with pytest.raises(AuthorizationError) as raised:
        await transport.post_form(
            "https://auth.example.test/oauth/token",
            {"grant_type": "authorization_code", "code": marker},
        )
    assert marker not in str(raised.value)
    assert marker not in raised.value.message
    assert raised.value.__cause__ is None


@pytest.mark.asyncio
async def test_http_transport_does_not_chain_backend_secret() -> None:
    import httpx2

    marker = "transport-secret-marker"

    def handler(_request):
        raise RuntimeError(marker)

    transport = HttpxOAuthTransport(transport=httpx2.MockTransport(handler))
    with pytest.raises(AuthorizationError) as raised:
        await transport.post_form(
            "https://auth.example.test/oauth/token",
            {"grant_type": "authorization_code", "code": marker},
        )
    assert marker not in str(raised.value)
    assert raised.value.__cause__ is None

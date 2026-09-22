from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx2
import pytest
from connection_hub.delegated_credentials.cards.identity import (
    CARD_KIND_AUTOMATION,
    CARD_KIND_CONNECTOR,
)
from connection_hub_cli.authorization.discovery import (
    MAX_OAUTH_RESPONSE_BYTES,
    McpOAuthDiscoveryResult,
    McpOAuthEndpointDiscovery,
)
from connection_hub_cli.authorization.flow import BrowserAuthorizationGrant
from connection_hub_cli.authorization.device import DeviceAuthorizationGrant
from connection_hub_cli.authorization.models import (
    AuthorizationServerMetadata,
    OAuthClientRegistration,
    OAuthTokenSet,
    ProtectedResourceMetadata,
)
from connection_hub_cli.authorization.profile_session import (
    OAuthProfileSessionService,
)
from connection_hub_cli.errors import AuthorizationError, ProfileError
from connection_hub_cli.models import CallerProfile, ProbeResult, ProfileOAuthMetadata
from connection_hub_cli.profiles import ProfileService
from connection_hub_cli.state import InstallationStore, ProfileStore

ENDPOINT = "https://hub.example.test/mcp"
METADATA_URL = "https://hub.example.test/.well-known/oauth-protected-resource"
ISSUER = "https://hub.example.test/oauth"


class _TokenStore:
    def __init__(self) -> None:
        self.values: dict[str, OAuthTokenSet] = {}
        self.events: list[str] = []

    def put(self, credential_ref: str, token: OAuthTokenSet) -> None:
        self.events.append("token.put")
        self.values[credential_ref] = token

    def get(self, credential_ref: str) -> OAuthTokenSet | None:
        return self.values.get(credential_ref)

    def remove(self, credential_ref: str) -> bool:
        self.events.append("token.remove")
        return self.values.pop(credential_ref, None) is not None


class _StaticStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def put(self, credential_ref: str, value: str) -> None:
        self.values[credential_ref] = value

    def get(self, credential_ref: str) -> str | None:
        return self.values.get(credential_ref)

    def remove(self, credential_ref: str) -> bool:
        return self.values.pop(credential_ref, None) is not None


class _MetadataTransport:
    def __init__(self, values) -> None:
        self.values = values

    async def get_json(self, url: str):
        value = self.values.get(url)
        if value is None:
            raise AuthorizationError("missing", "missing")
        return value

    async def post_json(self, url: str, payload):
        raise AssertionError((url, payload))

    async def post_form(self, url: str, payload):
        raise AssertionError((url, payload))


def _server() -> AuthorizationServerMetadata:
    return AuthorizationServerMetadata(
        issuer=ISSUER,
        authorization_endpoint=f"{ISSUER}/authorize",
        token_endpoint=f"{ISSUER}/token",
        registration_endpoint=f"{ISSUER}/register",
        revocation_endpoint=f"{ISSUER}/revoke",
        scopes_supported=("mcp",),
        supports_refresh=True,
        authorization_response_issuer_required=False,
    )


def _located() -> McpOAuthDiscoveryResult:
    return McpOAuthDiscoveryResult(
        endpoint=ENDPOINT,
        protected_resource_metadata_url=METADATA_URL,
        protected_resource=ProtectedResourceMetadata(
            resource=ENDPOINT,
            authorization_server=ISSUER,
            scopes_supported=("mcp",),
        ),
        authorization_server=_server(),
        scope="mcp",
    )


def _metadata() -> ProfileOAuthMetadata:
    return ProfileOAuthMetadata(
        protected_resource_metadata_url=METADATA_URL,
        resource=ENDPOINT,
        issuer=ISSUER,
        token_endpoint=f"{ISSUER}/token",
        revocation_endpoint=f"{ISSUER}/revoke",
        client_id="native-client",
        client_source="dcr",
        client_metadata_url=None,
        scope="mcp",
        card_kind=CARD_KIND_CONNECTOR,
    )


def _profile(*, now: str = "2026-09-04T00:00:00+00:00") -> CallerProfile:
    return CallerProfile.create_oauth(
        name="agent",
        endpoint=ENDPOINT,
        access_id="access-agent",
        oauth=_metadata(),
        credential_ref="a" * 32,
        now=now,
    )


def _token(
    access: str = "access-secret",
    refresh: str = "refresh-secret",
    *,
    expires_at: int = 2_000_000_000,
    card_kind: str = CARD_KIND_CONNECTOR,
) -> OAuthTokenSet:
    return OAuthTokenSet(
        access_token=access,
        refresh_token=refresh,
        expires_at=expires_at,
        scope="mcp",
        access_id="access-agent",
        card_kind=card_kind,
    )


def _other_card_token() -> OAuthTokenSet:
    return OAuthTokenSet(
        access_token="other-access-secret",
        refresh_token="other-refresh-secret",
        expires_at=2_000_000_000,
        scope="mcp",
        access_id="access-other",
        card_kind=CARD_KIND_CONNECTOR,
    )


class _EndpointDiscovery:
    # default_scope is what a caller's own operations need. The real discovery
    # yields it to a server challenge and otherwise prefers it over the whole
    # advertised set, so the stub accepts it to stay call-compatible.
    async def discover(self, endpoint: str, *, default_scope: str = ""):
        assert endpoint == ENDPOINT
        return _located()


class _Discovery:
    async def discover(self, **kwargs):
        assert kwargs == {
            "protected_resource_metadata_url": METADATA_URL,
            "expected_resource": ENDPOINT,
        }
        return SimpleNamespace(
            protected_resource=_located().protected_resource,
            authorization_server=_server(),
        )


class _Authorization:
    def __init__(self, token: OAuthTokenSet | None = None) -> None:
        self.token = token or _token()
        self.calls: list[dict] = []

    async def authorize_discovered(self, **kwargs):
        self.calls.append(dict(kwargs))
        return BrowserAuthorizationGrant(
            protected_resource_metadata_url=METADATA_URL,
            discovered=kwargs["discovered"],
            registration=OAuthClientRegistration(
                client_id="native-client",
                redirect_uris=("http://127.0.0.1/callback",),
                source="dcr",
            ),
            token=self.token,
        )


class _DeviceAuthorization:
    def __init__(self, token: OAuthTokenSet | None = None) -> None:
        self.token = token or _token()
        self.calls: list[dict] = []

    async def authorize_discovered(self, **kwargs):
        self.calls.append(dict(kwargs))
        return DeviceAuthorizationGrant(
            protected_resource_metadata_url=METADATA_URL,
            discovered=kwargs["discovered"],
            registration=OAuthClientRegistration(
                client_id=str(kwargs.get("provisioned_client_id") or "native-client"),
                redirect_uris=("http://127.0.0.1/callback",),
                source="provisioned",
            ),
            token=self.token,
        )


class _OAuth:
    def __init__(self, replacement: OAuthTokenSet | None = None) -> None:
        self.replacement = replacement or _token(
            "refreshed-access",
            "rotated-refresh",
        )
        self.refresh_calls = 0
        self.refresh_kwargs: list[dict] = []
        self.events: list[str] = []
        self.refresh_error: Exception | None = None

    async def refresh(self, **kwargs):
        self.refresh_calls += 1
        self.refresh_kwargs.append(dict(kwargs))
        await asyncio.sleep(0.01)
        if self.refresh_error:
            raise self.refresh_error
        return self.replacement

    async def revoke(self, **_kwargs):
        self.events.append("server.revoke")


def _service(
    tmp_path,
    *,
    oauth: _OAuth | None = None,
    authorization: _Authorization | None = None,
    device_authorization: _DeviceAuthorization | None = None,
    probe=None,
):
    profiles = ProfileStore(tmp_path / "profiles.json")
    credentials = _TokenStore()

    async def default_probe(**_kwargs):
        return ProbeResult(tool_count=3, server_name="hub", server_version="1")

    service = OAuthProfileSessionService(
        profiles=profiles,
        credentials=credentials,
        endpoint_discovery=_EndpointDiscovery(),
        discovery=_Discovery(),
        authorization=authorization or _Authorization(),
        device_authorization=device_authorization,
        oauth=oauth or _OAuth(),
        probe=probe or default_probe,
    )
    return service, profiles, credentials


@pytest.mark.asyncio
async def test_discovers_protected_resource_from_the_mcp_challenge() -> None:
    resource_payload = {
        "resource": ENDPOINT,
        "authorization_servers": [ISSUER],
        "scopes_supported": ["mcp"],
    }
    server_url = f"{ISSUER}/.well-known/oauth-authorization-server"
    metadata = _MetadataTransport(
        {
            METADATA_URL: resource_payload,
            server_url: {
                "issuer": ISSUER,
                "authorization_endpoint": f"{ISSUER}/authorize",
                "token_endpoint": f"{ISSUER}/token",
                "registration_endpoint": f"{ISSUER}/register",
                "revocation_endpoint": f"{ISSUER}/revoke",
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "response_types_supported": ["code"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
            },
        }
    )

    async def challenge(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            401,
            headers={
                "WWW-Authenticate": (
                    f'Bearer resource_metadata="{METADATA_URL}", scope="mcp"'
                )
            },
            request=request,
        )

    discovery = McpOAuthEndpointDiscovery(
        transport=metadata,
        http_transport=httpx2.MockTransport(challenge),
    )

    result = await discovery.discover(ENDPOINT)

    assert result.protected_resource_metadata_url == METADATA_URL
    assert result.protected_resource.resource == ENDPOINT
    assert result.authorization_server.issuer == ISSUER
    assert result.scope == "mcp"


@pytest.mark.asyncio
async def test_mcp_oauth_discovery_requires_a_401_challenge() -> None:
    async def no_challenge(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"jsonrpc": "2.0"}, request=request)

    discovery = McpOAuthEndpointDiscovery(
        transport=_MetadataTransport({}),
        http_transport=httpx2.MockTransport(no_challenge),
    )

    with pytest.raises(AuthorizationError) as raised:
        await discovery.discover(ENDPOINT)

    assert raised.value.code == "oauth_challenge_not_advertised"


@pytest.mark.asyncio
async def test_mcp_oauth_discovery_rejects_metadata_for_another_resource() -> None:
    metadata = _MetadataTransport(
        {
            METADATA_URL: {
                "resource": "https://other.example.test/mcp",
                "authorization_servers": [ISSUER],
            }
        }
    )

    async def challenge(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            401,
            headers={"WWW-Authenticate": f'Bearer resource_metadata="{METADATA_URL}"'},
            request=request,
        )

    discovery = McpOAuthEndpointDiscovery(
        transport=metadata,
        http_transport=httpx2.MockTransport(challenge),
    )

    with pytest.raises(AuthorizationError) as raised:
        await discovery.discover(ENDPOINT)

    assert raised.value.code == "oauth_resource_mismatch"


@pytest.mark.asyncio
async def test_mcp_oauth_discovery_bounds_the_challenge_response() -> None:
    async def oversized(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            401,
            content=b"x" * (MAX_OAUTH_RESPONSE_BYTES + 1),
            request=request,
        )

    discovery = McpOAuthEndpointDiscovery(
        transport=_MetadataTransport({}),
        http_transport=httpx2.MockTransport(oversized),
    )

    with pytest.raises(AuthorizationError) as raised:
        await discovery.discover(ENDPOINT)

    assert raised.value.code == "oauth_response_too_large"


def test_legacy_profile_record_migrates_to_static_bearer_on_next_write(
    tmp_path,
) -> None:
    path = tmp_path / "profiles.json"
    path.write_text(
        json.dumps(
            {
                "schema": "connection_hub_cli.profiles.v1",
                "profiles": {
                    "agent": {
                        "name": "agent",
                        "endpoint": ENDPOINT,
                        "credential_ref": "a" * 32,
                        "access_id": "access-agent",
                        "created_at": "2026-09-03T00:00:00+00:00",
                        "updated_at": "2026-09-03T00:00:00+00:00",
                    }
                },
            }
        )
    )
    store = ProfileStore(path)

    profile = store.require("agent")
    assert profile.auth_type == "static_bearer"
    store.update(profile.with_credential_replaced())

    persisted = json.loads(path.read_text())["profiles"]["agent"]
    assert persisted["record_version"] == 3
    assert persisted["auth_type"] == "static_bearer"
    assert persisted["oauth"] is None


@pytest.mark.asyncio
async def test_authorization_stores_tokens_only_in_native_custody(tmp_path) -> None:
    service, profiles, credentials = _service(tmp_path)

    result = await service.authorize(name="agent", endpoint=ENDPOINT)

    assert result.profile.auth_type == "oauth"
    assert result.profile.access_id == "access-agent"
    assert result.profile.oauth.client_source == "dcr"
    assert credentials.values[result.profile.credential_ref] == _token()
    state = profiles.path.read_text()
    assert "access-secret" not in state
    assert "refresh-secret" not in state
    assert "access-agent" in state


@pytest.mark.asyncio
async def test_profile_authorization_forwards_client_identification_metadata(tmp_path) -> None:
    authorization = _Authorization()
    service, _profiles, _credentials = _service(
        tmp_path, authorization=authorization
    )

    await service.authorize(
        name="agent",
        endpoint=ENDPOINT,
        client_name="Connection Hub CLI · codex:session-1",
        client_metadata={
            "kdcube_agent_id": "codex:session-1",
            "kdcube_machine_id": "machine-1",
        },
    )

    call = authorization.calls[0]
    assert call["client_name"].endswith("codex:session-1")
    assert call["client_metadata"] == {
        "kdcube_agent_id": "codex:session-1",
        "kdcube_machine_id": "machine-1",
    }


@pytest.mark.asyncio
async def test_whole_card_authorization_omits_entry_resource_from_profile(tmp_path) -> None:
    authorization = _Authorization(
        _token(card_kind=CARD_KIND_AUTOMATION)
    )
    service, profiles, _credentials = _service(
        tmp_path,
        authorization=authorization,
    )

    result = await service.authorize(
        name="agent",
        endpoint=ENDPOINT,
        whole_card=True,
    )

    assert authorization.calls[0]["resource"] == ""
    assert result.profile.oauth.card_kind == CARD_KIND_AUTOMATION
    assert result.profile.oauth.resource is None
    assert result.profile.oauth.protected_resource_metadata_url is None
    persisted = json.loads(profiles.path.read_text())["profiles"]["agent"]["oauth"]
    assert "resource" not in persisted
    assert "protected_resource_metadata_url" not in persisted


@pytest.mark.asyncio
async def test_whole_card_refresh_omits_entry_resource(tmp_path) -> None:
    oauth = _OAuth(
        _token(
            "refreshed-access",
            "rotated-refresh",
            card_kind=CARD_KIND_AUTOMATION,
        )
    )
    service, profiles, credentials = _service(tmp_path, oauth=oauth)
    profile = CallerProfile.create_oauth(
        name="agent",
        endpoint=ENDPOINT,
        access_id="access-agent",
        oauth=ProfileOAuthMetadata(
            protected_resource_metadata_url=None,
            resource=None,
            issuer=ISSUER,
            token_endpoint=f"{ISSUER}/token",
            revocation_endpoint=f"{ISSUER}/revoke",
            client_id="native-client",
            client_source="dcr",
            client_metadata_url=None,
            scope="mcp",
            card_kind=CARD_KIND_AUTOMATION,
        ),
        credential_ref="a" * 32,
    )
    profiles.add(profile)
    credentials.put(
        profile.credential_ref,
        _token(expires_at=1, card_kind=CARD_KIND_AUTOMATION),
    )

    assert await service.access_token(profile.name) == "refreshed-access"
    assert oauth.refresh_kwargs[0]["resource"] is None


@pytest.mark.asyncio
async def test_reconnect_reuses_client_and_preserves_profile_card(tmp_path) -> None:
    authorization = _Authorization(
        _token("reconnected-access", "reconnected-refresh")
    )
    oauth = _OAuth()
    service, profiles, credentials = _service(
        tmp_path,
        authorization=authorization,
        oauth=oauth,
    )
    profile = _profile()
    original = _token()
    profiles.add(profile)
    credentials.put(profile.credential_ref, original)

    result = await service.reconnect(
        profile.name,
        callback_port=9124,
        timeout_seconds=15,
    )

    call = authorization.calls[0]
    assert call["provisioned_client_id"] == profile.oauth.client_id
    assert call["scope"] == profile.oauth.scope
    assert call["callback_port"] == 9124
    assert "client_metadata" not in call
    assert result.profile.name == profile.name
    assert result.profile.credential_ref == profile.credential_ref
    assert result.profile.access_id == profile.access_id
    assert result.profile.created_at == profile.created_at
    assert result.profile.oauth == profile.oauth
    assert result.profile.updated_at >= profile.updated_at
    assert credentials.values[profile.credential_ref].access_token == (
        "reconnected-access"
    )
    assert oauth.events == []


@pytest.mark.asyncio
async def test_device_reconnect_reuses_client_and_preserves_profile_card(
    tmp_path,
) -> None:
    device_authorization = _DeviceAuthorization(
        _token("device-access", "device-refresh")
    )
    service, profiles, credentials = _service(
        tmp_path,
        device_authorization=device_authorization,
    )
    profile = _profile()
    profiles.add(profile)
    credentials.put(profile.credential_ref, _token())
    prompts = []

    result = await service.reconnect(
        profile.name,
        device=True,
        device_presenter=prompts.append,
        timeout_seconds=15,
    )

    call = device_authorization.calls[0]
    assert call["provisioned_client_id"] == profile.oauth.client_id
    assert call["requested_access_id"] == profile.access_id
    assert call["scope"] == profile.oauth.scope
    assert call["presenter"] == prompts.append
    assert result.profile.name == profile.name
    assert result.profile.credential_ref == profile.credential_ref
    assert result.profile.access_id == profile.access_id
    assert len(profiles.list()) == 1
    assert credentials.values[profile.credential_ref].access_token == "device-access"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "candidate",
    [
        _other_card_token(),
        replace(_other_card_token(), access_id="invalid card id"),
    ],
)
async def test_reconnect_revokes_unusable_card_and_preserves_local_state(
    tmp_path,
    candidate,
) -> None:
    authorization = _Authorization(candidate)
    oauth = _OAuth()
    service, profiles, credentials = _service(
        tmp_path,
        authorization=authorization,
        oauth=oauth,
    )
    profile = _profile()
    original = _token()
    profiles.add(profile)
    credentials.put(profile.credential_ref, original)

    with pytest.raises(AuthorizationError) as raised:
        await service.reconnect(profile.name)

    assert raised.value.code == "oauth_reconnect_card_mismatch"
    assert raised.value.details["expected_access_id"] == "access-agent"
    assert profiles.require(profile.name) == profile
    assert credentials.values[profile.credential_ref] == original
    assert oauth.events == ["server.revoke"]


@pytest.mark.asyncio
async def test_reconnect_without_local_token_restores_same_card_custody(tmp_path) -> None:
    authorization = _Authorization(
        _token("reconnected-access", "reconnected-refresh")
    )
    service, profiles, credentials = _service(
        tmp_path,
        authorization=authorization,
    )
    profile = _profile()
    profiles.add(profile)

    result = await service.reconnect(profile.name)

    assert result.profile.access_id == profile.access_id
    assert credentials.values[profile.credential_ref].access_token == (
        "reconnected-access"
    )


@pytest.mark.asyncio
async def test_reconnect_probe_failure_keeps_committed_token_and_card(tmp_path) -> None:
    async def failed_probe(**_kwargs):
        raise AuthorizationError("profile_probe_unavailable", "The probe is unavailable.")

    authorization = _Authorization(
        _token("reconnected-access", "reconnected-refresh")
    )
    oauth = _OAuth()
    service, profiles, credentials = _service(
        tmp_path,
        authorization=authorization,
        oauth=oauth,
        probe=failed_probe,
    )
    profile = _profile()
    profiles.add(profile)
    credentials.put(profile.credential_ref, _token())

    with pytest.raises(AuthorizationError) as raised:
        await service.reconnect(profile.name)

    assert raised.value.code == "profile_probe_unavailable"
    assert raised.value.details == {
        "card_preserved": True,
        "credential_stored": True,
    }
    assert "same-Card credential was stored" in raised.value.message
    assert profiles.require(profile.name).access_id == profile.access_id
    assert credentials.values[profile.credential_ref].access_token == (
        "reconnected-access"
    )
    assert oauth.events == []


@pytest.mark.asyncio
async def test_reconnect_commit_failure_keeps_previous_token_and_card(
    tmp_path,
    monkeypatch,
) -> None:
    authorization = _Authorization(
        _token("reconnected-access", "reconnected-refresh")
    )
    oauth = _OAuth()
    service, profiles, credentials = _service(
        tmp_path,
        authorization=authorization,
        oauth=oauth,
    )
    profile = _profile()
    original = _token()
    profiles.add(profile)
    credentials.put(profile.credential_ref, original)

    def failed_update(_profile) -> None:
        raise AuthorizationError("profile_store_unavailable", "The store is unavailable.")

    monkeypatch.setattr(profiles, "update", failed_update)

    with pytest.raises(AuthorizationError) as raised:
        await service.reconnect(profile.name)

    assert raised.value.code == "profile_store_unavailable"
    assert raised.value.details == {
        "card_preserved": True,
        "credential_stored": False,
    }
    assert "matching caller Card was not revoked" in raised.value.message
    assert profiles.require(profile.name).access_id == profile.access_id
    assert credentials.values[profile.credential_ref] == original
    assert oauth.events == []


@pytest.mark.asyncio
async def test_reconnect_requires_recorded_oauth_client_id(tmp_path, monkeypatch) -> None:
    service, profiles, _credentials = _service(tmp_path)
    profile = _profile()
    missing_client = replace(profile.oauth, client_id="")
    malformed = replace(profile, oauth=missing_client)
    monkeypatch.setattr(profiles, "require", lambda _name: malformed)
    monkeypatch.setattr(profiles, "get", lambda _name: malformed)

    with pytest.raises(AuthorizationError) as raised:
        await service.reconnect(profile.name)

    assert raised.value.code == "oauth_reconnect_client_id_missing"


@pytest.mark.asyncio
async def test_concurrent_access_refreshes_one_time_and_preserves_access_id(
    tmp_path,
) -> None:
    oauth = _OAuth()
    service, profiles, credentials = _service(tmp_path, oauth=oauth)
    profile = _profile()
    profiles.add(profile)
    credentials.put(profile.credential_ref, _token(expires_at=1))

    first, second = await asyncio.gather(
        service.access_token(profile.name),
        service.access_token(profile.name),
    )

    assert first == second == "refreshed-access"
    assert oauth.refresh_calls == 1
    assert credentials.values[profile.credential_ref].access_id == "access-agent"


@pytest.mark.asyncio
async def test_refresh_access_token_mints_now_although_the_local_token_is_current(tmp_path) -> None:
    # The server lost the session behind this token (a store restart). The
    # local clock still trusts it, so access_token() would re-present it; the
    # caller that met the 401 asks for the refresh directly.
    oauth = _OAuth()
    service, profiles, credentials = _service(tmp_path, oauth=oauth)
    profile = _profile()
    profiles.add(profile)
    credentials.put(profile.credential_ref, _token(expires_at=10_000_000_000))

    assert await service.access_token(profile.name) == "access-secret"
    assert oauth.refresh_calls == 0

    replacement = await service.refresh_access_token(profile.name)

    assert replacement == "refreshed-access"
    assert oauth.refresh_calls == 1
    assert credentials.values[profile.credential_ref].access_token == "refreshed-access"
    assert credentials.values[profile.credential_ref].access_id == "access-agent"
    assert await service.access_token(profile.name) == "refreshed-access"


@pytest.mark.asyncio
async def test_refresh_access_token_refused_by_the_server_keeps_the_stored_token(tmp_path) -> None:
    oauth = _OAuth()
    refused = AuthorizationError(
        "oauth_token_request_failed",
        "The OAuth server rejected refresh.",
    )
    refused.status = 400
    refused.details = {"status": 400}
    oauth.refresh_error = refused
    service, profiles, credentials = _service(tmp_path, oauth=oauth)
    profile = _profile()
    original = _token(expires_at=10_000_000_000)
    profiles.add(profile)
    credentials.put(profile.credential_ref, original)

    with pytest.raises(AuthorizationError) as raised:
        await service.refresh_access_token(profile.name)

    assert raised.value.code == "oauth_token_request_failed"
    assert raised.value.status == 400
    assert credentials.values[profile.credential_ref] == original


@pytest.mark.asyncio
async def test_refresh_failure_preserves_profile_and_complete_token(tmp_path) -> None:
    oauth = _OAuth()
    oauth.refresh_error = AuthorizationError(
        "oauth_token_request_failed",
        "The OAuth server rejected refresh.",
    )
    service, profiles, credentials = _service(tmp_path, oauth=oauth)
    profile = _profile()
    original = _token(expires_at=1)
    profiles.add(profile)
    credentials.put(profile.credential_ref, original)

    with pytest.raises(AuthorizationError) as raised:
        await service.access_token(profile.name)

    assert raised.value.code == "oauth_token_request_failed"
    assert profiles.require(profile.name).access_id == "access-agent"
    assert credentials.values[profile.credential_ref] == original


@pytest.mark.asyncio
async def test_disconnect_revokes_before_local_custody_and_metadata(tmp_path) -> None:
    oauth = _OAuth()
    sessions, profiles, credentials = _service(tmp_path, oauth=oauth)
    profile = _profile()
    profiles.add(profile)
    credentials.put(profile.credential_ref, _token())
    static = _StaticStore()

    async def probe(**_kwargs):
        return ProbeResult(tool_count=0)

    service = ProfileService(
        profiles=profiles,
        installations=InstallationStore(tmp_path / "installations.json"),
        credentials=static,
        probe=probe,
        oauth_sessions=sessions,
    )

    removed = await service.disconnect(profile.name)

    assert removed.profile.access_id == "access-agent"
    assert oauth.events == ["server.revoke"]
    assert credentials.events[-1] == "token.remove"
    assert profiles.get(profile.name) is None


def test_local_oauth_removal_requires_card_and_exact_access_id(tmp_path) -> None:
    sessions, profiles, credentials = _service(tmp_path)
    profile = _profile()
    profiles.add(profile)
    credentials.put(profile.credential_ref, _token())

    async def probe(**_kwargs):
        return ProbeResult(tool_count=0)

    service = ProfileService(
        profiles=profiles,
        installations=InstallationStore(tmp_path / "installations.json"),
        credentials=_StaticStore(),
        probe=probe,
        oauth_sessions=sessions,
    )

    with pytest.raises(ProfileError) as missing_confirmation:
        service.remove(profile.name)
    assert missing_confirmation.value.code == (
        "oauth_profile_server_revocation_required"
    )

    with pytest.raises(ProfileError) as wrong_access:
        service.remove(
            profile.name,
            server_card_revoked=True,
            access_id="another-access",
        )
    assert wrong_access.value.code == ("oauth_profile_access_id_confirmation_required")

    removed = service.remove(
        profile.name,
        server_card_revoked=True,
        access_id="access-agent",
    )
    assert removed.profile.name == "agent"
    assert credentials.values == {}


def _scope_fixture(scopes_supported: list[str]):
    """One discovery fixture whose challenge scope the caller chooses."""

    resource_payload = {
        "resource": ENDPOINT,
        "authorization_servers": [ISSUER],
        "scopes_supported": scopes_supported,
    }
    server_url = f"{ISSUER}/.well-known/oauth-authorization-server"
    return _MetadataTransport(
        {
            METADATA_URL: resource_payload,
            server_url: {
                "issuer": ISSUER,
                "authorization_endpoint": f"{ISSUER}/authorize",
                "token_endpoint": f"{ISSUER}/token",
                "registration_endpoint": f"{ISSUER}/register",
                "revocation_endpoint": f"{ISSUER}/revoke",
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "response_types_supported": ["code"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
            },
        }
    )


def _challenge_with(scope: str):
    async def challenge(request: httpx2.Request) -> httpx2.Response:
        advertised = f', scope="{scope}"' if scope else ""
        return httpx2.Response(
            401,
            headers={
                "WWW-Authenticate": (
                    f'Bearer resource_metadata="{METADATA_URL}"{advertised}'
                )
            },
            request=request,
        )

    return challenge


# A deployment advertises the union of every app installed on it. A caller that
# knows which claims its own operations need should ask for those, so the
# operator approving the consent is shown a set they can actually decide on.
DEPLOYMENT_SCOPES = [
    "work:observe",
    "work:coordinate",
    "linkedin:org:post",
    "press:delete",
    "slack:post",
]


@pytest.mark.asyncio
async def test_default_scope_replaces_the_whole_advertised_set() -> None:
    discovery = McpOAuthEndpointDiscovery(
        transport=_scope_fixture(DEPLOYMENT_SCOPES),
        http_transport=httpx2.MockTransport(_challenge_with("")),
    )

    result = await discovery.discover(
        ENDPOINT, default_scope="work:observe work:coordinate"
    )

    assert result.scope == "work:observe work:coordinate"
    for foreign in ("linkedin:org:post", "press:delete", "slack:post"):
        assert foreign not in result.scope


@pytest.mark.asyncio
async def test_a_server_challenge_still_wins_over_the_caller_s_default() -> None:
    """The server naming what it needs is more specific than our declaration."""

    discovery = McpOAuthEndpointDiscovery(
        transport=_scope_fixture(DEPLOYMENT_SCOPES),
        http_transport=httpx2.MockTransport(_challenge_with("work:observe")),
    )

    result = await discovery.discover(
        ENDPOINT, default_scope="work:coordinate work:relay"
    )

    assert result.scope == "work:observe"


@pytest.mark.asyncio
async def test_without_a_default_the_advertised_set_is_still_used() -> None:
    """Callers that say nothing keep the previous behaviour exactly."""

    discovery = McpOAuthEndpointDiscovery(
        transport=_scope_fixture(DEPLOYMENT_SCOPES),
        http_transport=httpx2.MockTransport(_challenge_with("")),
    )

    result = await discovery.discover(ENDPOINT)

    assert result.scope == " ".join(DEPLOYMENT_SCOPES)


@pytest.mark.asyncio
async def test_a_blank_default_scope_does_not_produce_an_empty_request() -> None:
    discovery = McpOAuthEndpointDiscovery(
        transport=_scope_fixture(DEPLOYMENT_SCOPES),
        http_transport=httpx2.MockTransport(_challenge_with("")),
    )

    result = await discovery.discover(ENDPOINT, default_scope="   ")

    assert result.scope == " ".join(DEPLOYMENT_SCOPES)

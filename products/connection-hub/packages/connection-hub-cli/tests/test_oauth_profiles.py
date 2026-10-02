from __future__ import annotations

import asyncio
import dataclasses
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx2
import pytest
from filelock import AsyncFileLock
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
        device_authorization_endpoint=f"{ISSUER}/device_authorization",
        supports_device_authorization=True,
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
async def test_existing_browser_client_refused_for_device_recovers_with_the_same_device_login(
    tmp_path,
) -> None:
    """W414: a browser-only client refused for device login names the server
    release that migrates it; once deployed, the same device login keeps the
    same client and Card. No callback, port or tunnel is part of the path."""

    device_answers = []

    class ServerBeforeThenAfterMigration:
        calls = []

        async def authorize_discovered(self, **kwargs):
            assert kwargs["provisioned_client_id"] == "native-client"
            assert kwargs["requested_access_id"] == "access-agent"
            self.calls.append(kwargs)
            if not device_answers:
                device_answers.append("refused")
                error = AuthorizationError(
                    "oauth_token_request_failed",
                    "OAuth POST returned HTTP 400: unauthorized_client.",
                )
                error.details = {
                    "method": "POST",
                    "url": f"{ISSUER}/device_authorization",
                    "status": 400,
                    "oauth_error": "unauthorized_client",
                }
                raise error
            return await _DeviceAuthorization(
                _token("device-access", "device-refresh")
            ).authorize_discovered(**kwargs)

    device = ServerBeforeThenAfterMigration()
    service, profiles, credentials = _service(
        tmp_path,
        device_authorization=device,
    )
    profile = _profile()
    old_token = _token()
    profiles.add(profile)
    credentials.put(profile.credential_ref, old_token)

    with pytest.raises(AuthorizationError) as raised:
        await service.reconnect(
            profile.name,
            device=True,
            device_presenter=lambda _prompt: None,
        )
    assert raised.value.code == "oauth_reconnect_device_client_unauthorized"
    assert "has not deployed the Connection Hub release" in raised.value.message
    for crutch in ("callback", "tunnel", "port"):
        assert crutch not in raised.value.message, crutch
    assert profiles.require(profile.name) == profile
    assert credentials.values[profile.credential_ref] == old_token

    # The server release is deployed: the same device login, same client and Card.
    result = await service.reconnect(
        profile.name,
        device=True,
        device_presenter=lambda _prompt: None,
    )
    assert device.calls[-1]["provisioned_client_id"] == profile.oauth.client_id
    # Continuity proof: the Card's last refresh token held on this machine.
    assert device.calls[-1]["continuity_refresh_token"] == old_token.refresh_token
    assert result.profile.access_id == profile.access_id
    assert result.profile.credential_ref == profile.credential_ref
    assert credentials.values[profile.credential_ref].access_token == "device-access"


@pytest.mark.asyncio
async def test_device_login_without_card_continuity_is_refused_and_keeps_the_profile(
    tmp_path,
) -> None:
    """W414: the server re-authorizes an existing Card by device login only
    with the Card's last refresh token as proof. Its refusal is named, and the
    profile and stored credential stay as they were."""

    class ContinuityRefused:
        calls = []

        async def authorize_discovered(self, **kwargs):
            self.calls.append(kwargs)
            error = AuthorizationError("oauth_token_request_failed", "card_continuity_required")
            error.details = {
                "method": "POST",
                "url": f"{ISSUER}/device_authorization",
                "status": 400,
                "oauth_error": "card_continuity_required",
            }
            raise error

    device = ContinuityRefused()
    service, profiles, credentials = _service(tmp_path, device_authorization=device)
    profile = _profile()
    profiles.add(profile)
    held = _token()
    credentials.put(profile.credential_ref, held)

    with pytest.raises(AuthorizationError) as raised:
        await service.reconnect(profile.name, device=True, device_presenter=lambda _prompt: None)

    assert raised.value.code == "oauth_reconnect_card_continuity_required"
    assert device.calls[-1]["continuity_refresh_token"] == held.refresh_token
    assert profiles.require(profile.name) == profile
    assert credentials.values[profile.credential_ref] == held


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("oauth_error", "url"),
    [
        ("invalid_client", f"{ISSUER}/device_authorization"),
        ("unauthorized_client", f"{ISSUER}/token"),
    ],
)
async def test_other_device_refusals_keep_their_original_diagnosis(
    tmp_path, oauth_error, url
) -> None:
    class RefusedDeviceAuthorization:
        async def authorize_discovered(self, **_kwargs):
            error = AuthorizationError("oauth_token_request_failed", oauth_error)
            error.details = {
                "method": "POST",
                "url": url,
                "status": 400,
                "oauth_error": oauth_error,
            }
            raise error

    service, profiles, credentials = _service(
        tmp_path, device_authorization=RefusedDeviceAuthorization()
    )
    profile = _profile()
    profiles.add(profile)
    original = _token()
    credentials.put(profile.credential_ref, original)

    with pytest.raises(AuthorizationError) as raised:
        await service.reconnect(
            profile.name,
            device=True,
            device_presenter=lambda _prompt: None,
        )
    assert raised.value.code == "oauth_token_request_failed"
    assert profiles.require(profile.name) == profile
    assert credentials.values[profile.credential_ref] == original


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
    # The token endpoint's own refusal carries its OAuth error code (W408).
    refused.details = {"status": 400, "oauth_error": "invalid_grant"}
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
    stored = credentials.values[profile.credential_ref]
    # W408: a failure with no server answer may hide a committed rotation, so
    # the credential is kept whole together with the attempt id of this refresh.
    assert dataclasses.replace(stored, refresh_attempt="") == original
    assert stored.refresh_attempt


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


class _HangingOAuth(_OAuth):
    """A token endpoint that answers one profile's refresh only when released.

    2026-09-23 18:04: the relay opened four channels against a runtime still
    down. The first held the store-wide profile lock through a refresh that
    hung on the absent endpoint, and the other three timed out on that lock
    after ten seconds although their own tokens were readable.
    """

    def __init__(self, *, hang_for: str) -> None:
        super().__init__()
        self.hang_for = hang_for
        self.release = asyncio.Event()
        self.hanging = asyncio.Event()

    async def refresh(self, **kwargs):
        if kwargs.get("refresh_token") == self.hang_for:
            self.refresh_calls += 1
            self.refresh_kwargs.append(dict(kwargs))
            self.hanging.set()
            await self.release.wait()
            return self.replacement
        return await super().refresh(**kwargs)


def _second_profile(name: str, access_id: str, credential_ref: str) -> CallerProfile:
    return CallerProfile.create_oauth(
        name=name,
        endpoint=ENDPOINT,
        access_id=access_id,
        oauth=_metadata(),
        credential_ref=credential_ref,
        now="2026-09-04T00:00:00+00:00",
    )


@pytest.mark.asyncio
async def test_one_profiles_hung_refresh_blocks_no_other_profile(tmp_path) -> None:
    oauth = _HangingOAuth(hang_for="refresh-secret")
    service, profiles, credentials = _service(tmp_path, oauth=oauth)
    stuck = _profile()
    current = _second_profile("current", "access-current", "b" * 32)
    expiring = _second_profile("expiring", "access-expiring", "c" * 32)
    for record in (stuck, current, expiring):
        profiles.add(record)
    credentials.put(stuck.credential_ref, _token(expires_at=1))
    credentials.put(current.credential_ref, replace(_token("current-access", "current-refresh"), access_id="access-current"))
    credentials.put(expiring.credential_ref, replace(_token("old-access", "expiring-refresh", expires_at=1), access_id="access-expiring"))
    oauth.replacement = replace(oauth.replacement, access_id=None)

    stuck_read = asyncio.create_task(service.access_token(stuck.name))
    await asyncio.wait_for(oauth.hanging.wait(), 1.0)

    # Another profile's current token is read at once: the store lock is not
    # held while the first profile's refresh waits on the network.
    assert await asyncio.wait_for(service.access_token(current.name), 1.0) == "current-access"
    # And another profile's own refresh proceeds, under its own refresh slot.
    assert await asyncio.wait_for(service.access_token(expiring.name), 1.0) == "refreshed-access"
    assert credentials.values[expiring.credential_ref].access_id == "access-expiring"
    # The store-wide lock itself is free while the refresh is in flight.
    probe = AsyncFileLock(str(service._transaction_lock), timeout=0.2)
    async with probe:
        pass

    assert not stuck_read.done()
    oauth.release.set()
    assert await asyncio.wait_for(stuck_read, 1.0) == "refreshed-access"
    assert credentials.values[stuck.credential_ref].access_id == "access-agent"


@pytest.mark.asyncio
async def test_a_second_caller_of_the_hung_profile_waits_and_refreshes_once(tmp_path) -> None:
    oauth = _HangingOAuth(hang_for="refresh-secret")
    service, profiles, credentials = _service(tmp_path, oauth=oauth)
    profile = _profile()
    profiles.add(profile)
    credentials.put(profile.credential_ref, _token(expires_at=1))

    first = asyncio.create_task(service.access_token(profile.name))
    await asyncio.wait_for(oauth.hanging.wait(), 1.0)
    second = asyncio.create_task(service.access_token(profile.name))
    await asyncio.sleep(0.05)
    assert not second.done(), "the same profile's second caller waits for the one refresh"

    oauth.release.set()
    assert await asyncio.wait_for(first, 1.0) == "refreshed-access"
    assert await asyncio.wait_for(second, 1.0) == "refreshed-access"
    assert oauth.refresh_calls == 1


@pytest.mark.asyncio
async def test_a_reconnect_that_completes_during_a_refresh_is_not_overwritten_by_it(tmp_path) -> None:
    # claude-main's review of #33: the reconnect commit writes under the same
    # credential_ref and access_id, so the commit must compare the stored
    # token with the one that was refreshed, not only the profile identity.
    oauth = _HangingOAuth(hang_for="refresh-secret")
    service, profiles, credentials = _service(tmp_path, oauth=oauth)
    profile = _profile()
    profiles.add(profile)
    credentials.put(profile.credential_ref, _token(expires_at=1))

    stale = asyncio.create_task(service.access_token(profile.name))
    await asyncio.wait_for(oauth.hanging.wait(), 1.0)

    reconnected = _token("reconnected-access", "reconnected-refresh")
    await service._commit_reconnected_token(
        expected=profile,
        replacement=reconnected,
        replacement_metadata=profile.oauth,
    )
    assert credentials.values[profile.credential_ref] == reconnected

    oauth.release.set()
    assert await asyncio.wait_for(stale, 1.0) == "reconnected-access"
    assert credentials.values[profile.credential_ref] == reconnected, (
        "the refresh of the old chain must not overwrite the reconnect's credential"
    )
    assert await service.access_token(profile.name) == "reconnected-access"
    assert oauth.refresh_calls == 1


@pytest.mark.asyncio
async def test_a_caller_cancelled_during_a_refresh_still_commits_the_rotation(tmp_path) -> None:
    """2026-10-01 17:25:31Z: the token endpoint answered a relay channel's
    refresh with a rotated chain and the channel was torn down 66 ms later.
    The replacement never reached the store, the spent refresh token was
    presented at 19:05:40, and the server revoked the chain as reuse (W456)."""

    oauth = _HangingOAuth(hang_for="refresh-secret")
    service, profiles, credentials = _service(tmp_path, oauth=oauth)
    profile = _profile()
    profiles.add(profile)
    credentials.put(profile.credential_ref, _token(expires_at=1))

    caller = asyncio.create_task(service.access_token(profile.name))
    await asyncio.wait_for(oauth.hanging.wait(), 1.0)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    oauth.release.set()  # the server's answer arrives after the caller left

    for _ in range(100):
        if credentials.values[profile.credential_ref].refresh_token == "rotated-refresh":
            break
        await asyncio.sleep(0.01)
    stored = credentials.values[profile.credential_ref]
    assert stored.refresh_token == "rotated-refresh", "the rotated chain was dropped"
    assert stored.access_id == "access-agent"
    assert oauth.refresh_calls == 1
    assert await service.access_token(profile.name) == "refreshed-access"
    assert oauth.refresh_calls == 1, "the stored replacement is used, not refreshed again"


@pytest.mark.asyncio
async def test_a_store_lock_wait_during_the_commit_does_not_drop_the_rotation(
    tmp_path, monkeypatch
) -> None:
    oauth = _OAuth()
    service, profiles, credentials = _service(tmp_path, oauth=oauth)
    profile = _profile()
    profiles.add(profile)
    credentials.put(profile.credential_ref, _token(expires_at=1))
    commit = service._commit_refreshed_token
    attempts: list[int] = []

    async def busy_store_once(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise AuthorizationError(
                "oauth_profile_lock_timeout",
                "Timed out waiting for the OAuth profile lock.",
            )
        return await commit(*args, **kwargs)

    monkeypatch.setattr(service, "_commit_refreshed_token", busy_store_once)

    assert await service.access_token(profile.name) == "refreshed-access"
    assert len(attempts) == 2
    assert oauth.refresh_calls == 1, "the commit is retried, never a second refresh"
    assert credentials.values[profile.credential_ref].refresh_token == "rotated-refresh"


def _timing_out_commits(service, monkeypatch, *, timeouts: int) -> list[int]:
    """The store lock is busy for the first ``timeouts`` commit attempts."""

    commit = service._commit_refreshed_token
    attempts: list[int] = []

    async def busy_store(*args, **kwargs):
        attempts.append(1)
        if len(attempts) <= timeouts:
            raise AuthorizationError(
                "oauth_profile_lock_timeout",
                "Timed out waiting for the OAuth profile lock.",
            )
        return await commit(*args, **kwargs)

    monkeypatch.setattr(service, "_commit_refreshed_token", busy_store)
    return attempts


@pytest.mark.asyncio
async def test_a_replacement_whose_commit_attempts_all_time_out_stays_pending(
    tmp_path, monkeypatch
) -> None:
    oauth = _OAuth()
    service, profiles, credentials = _service(tmp_path, oauth=oauth)
    profile = _profile()
    profiles.add(profile)
    credentials.put(profile.credential_ref, _token(expires_at=1))
    limit = service.REFRESH_COMMIT_ATTEMPTS
    attempts = _timing_out_commits(service, monkeypatch, timeouts=limit + 1)

    with pytest.raises(AuthorizationError) as raised:
        await service.access_token(profile.name)
    assert raised.value.code == "oauth_profile_lock_timeout"
    assert len(attempts) == limit
    assert credentials.values[profile.credential_ref].refresh_token == "refresh-secret"

    # The next read commits the replacement the server already issued; it
    # never presents the spent refresh token again.
    assert await service.access_token(profile.name) == "refreshed-access"
    assert oauth.refresh_calls == 1
    assert credentials.values[profile.credential_ref].refresh_token == "rotated-refresh"
    assert await service.access_token(profile.name) == "refreshed-access"
    assert oauth.refresh_calls == 1


@pytest.mark.asyncio
async def test_a_reopened_channel_commits_the_replacement_its_predecessor_received(
    tmp_path, monkeypatch
) -> None:
    """The relay builds a new service for each channel open."""

    oauth = _OAuth()
    service, profiles, credentials = _service(tmp_path, oauth=oauth)
    profile = _profile()
    profiles.add(profile)
    credentials.put(profile.credential_ref, _token(expires_at=1))
    _timing_out_commits(service, monkeypatch, timeouts=service.REFRESH_COMMIT_ATTEMPTS)
    with pytest.raises(AuthorizationError):
        await service.access_token(profile.name)

    reopened_oauth = _OAuth()
    reopened = OAuthProfileSessionService(
        profiles=ProfileStore(tmp_path / "profiles.json"),
        credentials=credentials,
        endpoint_discovery=_EndpointDiscovery(),
        discovery=_Discovery(),
        authorization=_Authorization(),
        oauth=reopened_oauth,
        probe=service._probe,
    )
    assert await reopened.access_token(profile.name) == "refreshed-access"
    assert reopened_oauth.refresh_calls == 0, "the spent token is never presented"
    assert credentials.values[profile.credential_ref].refresh_token == "rotated-refresh"


@pytest.mark.asyncio
async def test_a_stopping_process_drains_a_refresh_in_flight(tmp_path) -> None:
    from connection_hub_cli.authorization.profile_session import drain_pending_refreshes

    oauth = _HangingOAuth(hang_for="refresh-secret")
    service, profiles, credentials = _service(tmp_path, oauth=oauth)
    profile = _profile()
    profiles.add(profile)
    credentials.put(profile.credential_ref, _token(expires_at=1))

    caller = asyncio.create_task(service.access_token(profile.name))
    await asyncio.wait_for(oauth.hanging.wait(), 1.0)
    caller.cancel()  # the channel is closed first, as on relay stop
    asyncio.get_running_loop().call_later(0.1, oauth.release.set)

    assert await drain_pending_refreshes(timeout_seconds=2.0) == 0
    assert credentials.values[profile.credential_ref].refresh_token == "rotated-refresh"


@pytest.mark.asyncio
async def test_cancelling_authorize_while_the_credential_is_written_leaves_it_with_its_profile(tmp_path) -> None:
    """W461 review: the credential and its profile are one commit, a cancellation never splits them."""

    import threading

    service, profiles, credentials = _service(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    real_put = credentials.put

    def held_put(credential_ref, token):
        if token == _token():
            entered.set()
            release.wait(2)
        real_put(credential_ref, token)

    credentials.put = held_put
    task = asyncio.create_task(service.authorize(name="agent", endpoint=ENDPOINT))
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    await asyncio.sleep(0.05)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    stored = [ref for ref, token in credentials.values.items() if token == _token()]
    profile = profiles.get("agent")
    assert profile is not None, "the commit finished before the cancellation was raised"
    assert stored == [profile.credential_ref], "no credential without its profile"

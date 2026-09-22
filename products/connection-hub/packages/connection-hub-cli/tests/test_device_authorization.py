from __future__ import annotations

from dataclasses import dataclass

import pytest

from connection_hub.delegated_credentials.oauth.device import DEVICE_GRANT_TYPE
from connection_hub_cli.authorization.client import OAuthClient
from connection_hub_cli.authorization.device import (
    DeviceAuthorizationFlow,
    DeviceAuthorizationPrompt,
)
from connection_hub_cli.authorization.discovery import OAuthDiscoveryResult
from connection_hub_cli.authorization.models import (
    AuthorizationServerMetadata,
    OAuthClientRegistration,
    OAuthTokenSet,
    ProtectedResourceMetadata,
)
from connection_hub_cli.errors import AuthorizationError


class _Transport:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.forms: list[tuple[str, dict]] = []
        self.values: dict[str, dict] = {}

    async def get_json(self, url: str):
        return self.values[url]

    async def post_json(self, url: str, payload):
        self.posts.append((url, dict(payload)))
        return self.values[url]

    async def post_form(self, url: str, payload):
        self.forms.append((url, dict(payload)))
        return self.values[url]


def _metadata() -> AuthorizationServerMetadata:
    return AuthorizationServerMetadata.from_mapping(
        {
            "issuer": "https://auth.example.test",
            "authorization_endpoint": "https://auth.example.test/oauth/authorize",
            "device_authorization_endpoint": (
                "https://auth.example.test/oauth/device_authorization"
            ),
            "token_endpoint": "https://auth.example.test/oauth/token",
            "registration_endpoint": "https://auth.example.test/oauth/register",
            "revocation_endpoint": "https://auth.example.test/oauth/revoke",
            "grant_types_supported": [
                "authorization_code",
                "refresh_token",
                DEVICE_GRANT_TYPE,
            ],
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
        },
        expected_issuer="https://auth.example.test",
    )


def _discovered() -> OAuthDiscoveryResult:
    return OAuthDiscoveryResult(
        protected_resource=ProtectedResourceMetadata(
            resource="https://runtime.example.test/mcp",
            authorization_server="https://auth.example.test",
        ),
        authorization_server=_metadata(),
    )


def test_device_prompt_validates_fields_and_hides_device_code_from_repr():
    prompt = DeviceAuthorizationPrompt.from_mapping(
        {
            "device_code": "private-device-code",
            "user_code": "BCDF-GHJK",
            "verification_uri": "https://auth.example.test/device",
            "verification_uri_complete": (
                "https://auth.example.test/device?user_code=BCDF-GHJK"
            ),
            "expires_in": 600,
            "interval": 5,
        }
    )
    assert prompt.device_code == "private-device-code"
    assert "private-device-code" not in repr(prompt)


@pytest.mark.asyncio
async def test_client_registers_and_exchanges_device_grant_without_callback_listener():
    transport = _Transport()
    transport.values["https://auth.example.test/oauth/register"] = {
        "client_id": "device-client",
        "redirect_uris": ["http://127.0.0.1/callback"],
        "token_endpoint_auth_method": "none",
    }
    transport.values["https://auth.example.test/oauth/device_authorization"] = {
        "device_code": "private-device-code",
        "user_code": "BCDF-GHJK",
        "verification_uri": "https://auth.example.test/device",
        "expires_in": 600,
        "interval": 5,
    }
    client = OAuthClient(transport=transport)
    registration = await client.register_device_client(metadata=_metadata())
    prompt = await client.request_device_authorization(
        metadata=_metadata(),
        client=registration,
        resource="https://runtime.example.test/mcp",
        scope="work:read",
        requested_access_id="aut_existing",
    )
    assert prompt.user_code == "BCDF-GHJK"
    assert DEVICE_GRANT_TYPE in transport.posts[0][1]["grant_types"]
    assert transport.forms[0][1] == {
        "client_id": "device-client",
        "resource": "https://runtime.example.test/mcp",
        "scope": "work:read",
        "access_id": "aut_existing",
    }

    transport.values["https://auth.example.test/oauth/token"] = {
        "access_token": "access-one",
        "refresh_token": "refresh-one",
        "token_type": "Bearer",
        "expires_in": 3600,
        "access_id": "aut_existing",
        "card_kind": "agent",
    }
    token = await client.exchange_device_code(
        metadata=_metadata(),
        client=registration,
        device_code=prompt.device_code,
        scope="work:read",
        now=100,
    )
    assert token.access_id == "aut_existing"
    assert transport.forms[-1][1] == {
        "grant_type": DEVICE_GRANT_TYPE,
        "device_code": "private-device-code",
        "client_id": "device-client",
    }


@dataclass
class _Clock:
    now: float = 0.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


class _ScriptedClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.polls = 0

    async def register_device_client(self, **kwargs):
        return OAuthClientRegistration(
            client_id="device-client",
            redirect_uris=("http://127.0.0.1/callback",),
        )

    async def request_device_authorization(self, **kwargs):
        return DeviceAuthorizationPrompt.from_mapping(
            {
                "device_code": "private-device-code",
                "user_code": "BCDF-GHJK",
                "verification_uri": "https://auth.example.test/device",
                "expires_in": 60,
                "interval": 5,
            }
        )

    async def exchange_device_code(self, **kwargs):
        self.polls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _oauth_failure(code: str) -> AuthorizationError:
    error = AuthorizationError("oauth_token_request_failed", "refused")
    error.details = {"oauth_error": code}
    return error


@pytest.mark.asyncio
async def test_device_flow_honors_pending_and_slow_down_before_returning_token():
    token = OAuthTokenSet(
        access_token="access",
        refresh_token="refresh",
        expires_at=3600,
        access_id="aut_one",
        card_kind="agent",
    )
    client = _ScriptedClient(
        [_oauth_failure("authorization_pending"), _oauth_failure("slow_down"), token]
    )
    clock = _Clock()
    prompts = []
    flow = DeviceAuthorizationFlow(
        client=client,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    grant = await flow.authorize_discovered(
        protected_resource_metadata_url="https://runtime.example.test/metadata",
        discovered=_discovered(),
        resource="https://runtime.example.test/mcp",
        presenter=prompts.append,
    )
    assert grant.token is token
    assert client.polls == 3
    assert clock.now == 20
    assert [prompt.user_code for prompt in prompts] == ["BCDF-GHJK"]


@pytest.mark.asyncio
async def test_device_flow_maps_denial_to_stable_client_error():
    client = _ScriptedClient([_oauth_failure("access_denied")])
    clock = _Clock()
    flow = DeviceAuthorizationFlow(
        client=client,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    with pytest.raises(AuthorizationError) as raised:
        await flow.authorize_discovered(
            protected_resource_metadata_url="https://runtime.example.test/metadata",
            discovered=_discovered(),
            resource="https://runtime.example.test/mcp",
            presenter=lambda _prompt: None,
        )
    assert raised.value.code == "oauth_device_access_denied"


@pytest.mark.asyncio
async def test_device_flow_names_restart_after_consumed_issuance_failure():
    client = _ScriptedClient(
        [_oauth_failure("device_authorization_restart_required")]
    )
    clock = _Clock()
    flow = DeviceAuthorizationFlow(
        client=client,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    with pytest.raises(AuthorizationError) as raised:
        await flow.authorize_discovered(
            protected_resource_metadata_url="https://runtime.example.test/metadata",
            discovered=_discovered(),
            resource="https://runtime.example.test/mcp",
            presenter=lambda _prompt: None,
        )

    assert raised.value.code == "oauth_device_authorization_restart_required"
    assert "restart device authorization" in str(raised.value)


@pytest.mark.asyncio
async def test_device_flow_does_not_poll_after_its_local_deadline():
    client = _ScriptedClient([])
    clock = _Clock()
    flow = DeviceAuthorizationFlow(
        client=client,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    with pytest.raises(AuthorizationError) as raised:
        await flow.authorize_discovered(
            protected_resource_metadata_url="https://runtime.example.test/metadata",
            discovered=_discovered(),
            resource="https://runtime.example.test/mcp",
            presenter=lambda _prompt: None,
            timeout_seconds=1,
        )

    assert raised.value.code == "oauth_device_expired"
    assert client.polls == 0
    assert clock.now == 1

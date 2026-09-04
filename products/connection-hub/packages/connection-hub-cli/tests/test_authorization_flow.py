from __future__ import annotations

from contextlib import asynccontextmanager
from typing import ClassVar
from urllib.parse import parse_qs, urlsplit

import pytest
from connection_hub_cli.authorization.callback import AuthorizationCallback
from connection_hub_cli.authorization.client import OAuthClient
from connection_hub_cli.authorization.discovery import OAuthDiscovery
from connection_hub_cli.authorization.flow import BrowserAuthorizationFlow
from connection_hub_cli.errors import AuthorizationError


class _Transport:
    def __init__(self) -> None:
        self.get_urls: list[str] = []
        self.forms: list[dict[str, str]] = []
        self.form_urls: list[str] = []
        self.fail_revoke = False
        self.publish_revocation = True
        self.publish_access_id = True
        self.publish_cimd = False

    async def get_json(self, url: str):
        self.get_urls.append(url)
        if url.endswith("oauth-protected-resource"):
            return {
                "resource": (
                    "urn:kdcube:management:deployment:demo-tenant:demo-project"
                ),
                "authorization_servers": ["https://runtime.example.test/oauth"],
            }
        assert url == (
            "https://runtime.example.test/oauth/.well-known/oauth-authorization-server"
        )
        metadata = {
            "issuer": "https://runtime.example.test/oauth",
            "authorization_endpoint": ("https://runtime.example.test/oauth/authorize"),
            "token_endpoint": "https://runtime.example.test/oauth/token",
            "registration_endpoint": "https://runtime.example.test/oauth/register",
            "revocation_endpoint": "https://runtime.example.test/oauth/revoke",
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "authorization_response_iss_parameter_supported": True,
        }
        if not self.publish_revocation:
            metadata.pop("revocation_endpoint")
        if self.publish_cimd:
            metadata["client_id_metadata_document_supported"] = True
        return metadata

    async def post_json(self, _url: str, payload):
        return {
            "client_id": "native-client",
            "redirect_uris": list(payload["redirect_uris"]),
            "token_endpoint_auth_method": "none",
        }

    async def post_form(self, url: str, payload):
        self.forms.append(dict(payload))
        self.form_urls.append(url)
        if url.endswith("/revoke"):
            if self.fail_revoke:
                raise RuntimeError("server-secret-marker")
            return {}
        response = {
            "access_token": "access-secret-marker",
            "refresh_token": "refresh-secret-marker",
            "expires_in": 3600,
            "token_type": "Bearer",
        }
        if self.publish_access_id:
            response["access_id"] = "access-cli"
        return response


class _Callback:
    instances: ClassVar[list[_Callback]] = []

    def __init__(self, **values) -> None:
        self.values = values
        port = values.get("port", 9123)
        self.redirect_uri = f"http://127.0.0.1:{port}/callback"
        self.closed = False
        self.__class__.instances.append(self)

    def wait(self, *, timeout_seconds: float):
        assert timeout_seconds == 5
        return AuthorizationCallback(
            code="authorization-code",
            issuer="https://runtime.example.test/oauth",
        )

    def close(self) -> None:
        self.closed = True


class _Sessions:
    def __init__(self) -> None:
        self.values = []
        self.target_keys: list[str] = []
        self.create_error: Exception | None = None
        self.credential_store_error: Exception | None = None
        self.credential_store_checks = 0

    @asynccontextmanager
    async def authorization_slot(self, target_key: str):
        self.target_keys.append(target_key)
        yield

    def verify_credential_store(self) -> None:
        self.credential_store_checks += 1
        if self.credential_store_error is not None:
            raise self.credential_store_error

    def create(self, record, token) -> None:
        if self.create_error is not None:
            raise self.create_error
        self.values.append((record, token))


@pytest.mark.asyncio
async def test_browser_flow_keeps_verifier_out_of_browser_and_stores_token() -> None:
    _Callback.instances.clear()
    transport = _Transport()
    sessions = _Sessions()
    opened: list[str] = []
    oauth = OAuthClient(transport=transport)
    flow = BrowserAuthorizationFlow(
        discovery=OAuthDiscovery(transport=transport),
        client=oauth,
        sessions=sessions,
        browser_opener=lambda url: opened.append(url) is None,
        callback_factory=_Callback,
    )

    result = await flow.authorize_and_store(
        target_key="endpoint:https://runtime.example.test:demo-tenant:demo-project",
        protected_resource_metadata_url=(
            "https://runtime.example.test/api/integrations/management/v1/"
            ".well-known/oauth-protected-resource"
        ),
        resource="urn:kdcube:management:deployment:demo-tenant:demo-project",
        timeout_seconds=5,
    )

    assert len(opened) == 1
    query = parse_qs(urlsplit(opened[0]).query)
    assert "code_verifier" not in query
    assert query["code_challenge_method"] == ["S256"]
    assert transport.forms[0]["code_verifier"]
    assert sessions.values[0][1].access_token == "access-secret-marker"
    assert sessions.target_keys == [
        "endpoint:https://runtime.example.test:demo-tenant:demo-project"
    ]
    assert sessions.credential_store_checks == 1
    assert result.session.access_id == "access-cli"
    assert "access-secret-marker" not in repr(result)
    assert _Callback.instances[0].closed is True


@pytest.mark.asyncio
async def test_credential_store_failure_stops_before_oauth_network() -> None:
    _Callback.instances.clear()
    transport = _Transport()
    sessions = _Sessions()
    sessions.credential_store_error = AuthorizationError(
        "oauth_session_store_write_failed",
        "The OAuth session could not be stored in macOS Keychain.",
    )
    flow = BrowserAuthorizationFlow(
        discovery=OAuthDiscovery(transport=transport),
        client=OAuthClient(transport=transport),
        sessions=sessions,
        browser_opener=lambda _url: True,
        callback_factory=_Callback,
    )

    with pytest.raises(AuthorizationError) as raised:
        await flow.authorize_and_store(
            target_key=(
                "endpoint:https://runtime.example.test:demo-tenant:demo-project"
            ),
            protected_resource_metadata_url=(
                "https://runtime.example.test/api/integrations/management/v1/"
                ".well-known/oauth-protected-resource"
            ),
            resource="urn:kdcube:management:deployment:demo-tenant:demo-project",
        )

    assert raised.value.code == "oauth_session_store_write_failed"
    assert sessions.credential_store_checks == 1
    assert transport.get_urls == []
    assert transport.forms == []
    assert _Callback.instances == []


@pytest.mark.asyncio
async def test_browser_flow_can_use_a_per_request_browser_opener() -> None:
    _Callback.instances.clear()
    transport = _Transport()
    sessions = _Sessions()
    default_opened: list[str] = []
    selected_opened: list[str] = []
    flow = BrowserAuthorizationFlow(
        discovery=OAuthDiscovery(transport=transport),
        client=OAuthClient(transport=transport),
        sessions=sessions,
        browser_opener=lambda url: default_opened.append(url) is None,
        callback_factory=_Callback,
    )

    await flow.authorize_and_store(
        target_key="endpoint:https://runtime.example.test:demo-tenant:demo-project",
        protected_resource_metadata_url=(
            "https://runtime.example.test/api/integrations/management/v1/"
            ".well-known/oauth-protected-resource"
        ),
        resource="urn:kdcube:management:deployment:demo-tenant:demo-project",
        timeout_seconds=5,
        browser_opener=lambda url: selected_opened.append(url) is None,
    )

    assert default_opened == []
    assert len(selected_opened) == 1
    assert "code_verifier" not in selected_opened[0]


@pytest.mark.asyncio
async def test_cimd_requires_and_uses_a_published_fixed_callback_port() -> None:
    _Callback.instances.clear()
    transport = _Transport()
    transport.publish_cimd = True
    discovery = OAuthDiscovery(transport=transport)
    discovered = await discovery.discover(
        protected_resource_metadata_url=(
            "https://runtime.example.test/oauth-protected-resource"
        ),
        expected_resource=("urn:kdcube:management:deployment:demo-tenant:demo-project"),
    )
    flow = BrowserAuthorizationFlow(
        discovery=discovery,
        client=OAuthClient(transport=transport),
        sessions=_Sessions(),
        browser_opener=lambda _url: True,
        callback_factory=_Callback,
    )

    with pytest.raises(AuthorizationError) as missing_port:
        await flow.authorize_discovered(
            protected_resource_metadata_url=(
                "https://runtime.example.test/oauth-protected-resource"
            ),
            discovered=discovered,
            client_metadata_url=("https://client.example.test/oauth/metadata.json"),
            timeout_seconds=5,
        )
    assert missing_port.value.code == "oauth_cimd_callback_port_required"
    assert _Callback.instances == []

    grant = await flow.authorize_discovered(
        protected_resource_metadata_url=(
            "https://runtime.example.test/oauth-protected-resource"
        ),
        discovered=discovered,
        client_metadata_url="https://client.example.test/oauth/metadata.json",
        callback_port=9124,
        timeout_seconds=5,
    )

    assert _Callback.instances[-1].values["port"] == 9124
    assert grant.registration.source == "cimd"


@pytest.mark.asyncio
async def test_browser_rejection_closes_callback_and_stores_nothing() -> None:
    _Callback.instances.clear()
    transport = _Transport()
    sessions = _Sessions()
    flow = BrowserAuthorizationFlow(
        discovery=OAuthDiscovery(transport=transport),
        client=OAuthClient(transport=transport),
        sessions=sessions,
        browser_opener=lambda _url: False,
        callback_factory=_Callback,
    )

    with pytest.raises(AuthorizationError) as raised:
        await flow.authorize_and_store(
            target_key=(
                "endpoint:https://runtime.example.test:demo-tenant:demo-project"
            ),
            protected_resource_metadata_url=(
                "https://runtime.example.test/api/integrations/management/v1/"
                ".well-known/oauth-protected-resource"
            ),
            resource=("urn:kdcube:management:deployment:demo-tenant:demo-project"),
            timeout_seconds=5,
        )
    assert raised.value.code == "oauth_browser_open_failed"
    assert sessions.values == []
    assert transport.forms == []
    assert _Callback.instances[0].closed is True


@pytest.mark.asyncio
async def test_authorization_requires_revocation_before_opening_browser() -> None:
    _Callback.instances.clear()
    transport = _Transport()
    transport.publish_revocation = False
    sessions = _Sessions()
    opened: list[str] = []
    flow = BrowserAuthorizationFlow(
        discovery=OAuthDiscovery(transport=transport),
        client=OAuthClient(transport=transport),
        sessions=sessions,
        browser_opener=lambda url: opened.append(url) is None,
        callback_factory=_Callback,
    )

    with pytest.raises(AuthorizationError) as raised:
        await flow.authorize_and_store(
            target_key=(
                "endpoint:https://runtime.example.test:demo-tenant:demo-project"
            ),
            protected_resource_metadata_url=(
                "https://runtime.example.test/api/integrations/management/v1/"
                ".well-known/oauth-protected-resource"
            ),
            resource=("urn:kdcube:management:deployment:demo-tenant:demo-project"),
        )

    assert raised.value.code == "oauth_revocation_unsupported"
    assert opened == []
    assert sessions.values == []
    assert _Callback.instances == []


@pytest.mark.asyncio
async def test_storage_failure_revokes_the_new_oauth_grant() -> None:
    _Callback.instances.clear()
    transport = _Transport()
    sessions = _Sessions()
    sessions.create_error = AuthorizationError(
        "oauth_session_store_write_failed",
        "The local OAuth session could not be stored.",
    )
    flow = BrowserAuthorizationFlow(
        discovery=OAuthDiscovery(transport=transport),
        client=OAuthClient(transport=transport),
        sessions=sessions,
        browser_opener=lambda _url: True,
        callback_factory=_Callback,
    )

    with pytest.raises(AuthorizationError) as raised:
        await flow.authorize_and_store(
            target_key=(
                "endpoint:https://runtime.example.test:demo-tenant:demo-project"
            ),
            protected_resource_metadata_url=(
                "https://runtime.example.test/api/integrations/management/v1/"
                ".well-known/oauth-protected-resource"
            ),
            resource=("urn:kdcube:management:deployment:demo-tenant:demo-project"),
            timeout_seconds=5,
        )

    assert raised.value.code == "oauth_session_store_write_failed"
    assert sessions.values == []
    assert transport.form_urls[-1].endswith("/revoke")
    assert transport.forms[-1] == {
        "token": "refresh-secret-marker",
        "token_type_hint": "refresh_token",
        "client_id": "native-client",
    }
    assert _Callback.instances[0].closed is True


@pytest.mark.asyncio
async def test_token_without_card_identity_is_revoked_and_not_stored() -> None:
    _Callback.instances.clear()
    transport = _Transport()
    transport.publish_access_id = False
    sessions = _Sessions()
    flow = BrowserAuthorizationFlow(
        discovery=OAuthDiscovery(transport=transport),
        client=OAuthClient(transport=transport),
        sessions=sessions,
        browser_opener=lambda _url: True,
        callback_factory=_Callback,
    )

    with pytest.raises(AuthorizationError) as raised:
        await flow.authorize_and_store(
            target_key=(
                "endpoint:https://runtime.example.test:demo-tenant:demo-project"
            ),
            protected_resource_metadata_url=(
                "https://runtime.example.test/api/integrations/management/v1/"
                ".well-known/oauth-protected-resource"
            ),
            resource="urn:kdcube:management:deployment:demo-tenant:demo-project",
            timeout_seconds=5,
        )

    assert raised.value.code == "oauth_access_id_missing"
    assert sessions.values == []
    assert transport.form_urls[-1].endswith("/revoke")
    assert _Callback.instances[0].closed is True


@pytest.mark.asyncio
async def test_storage_and_revocation_failure_is_secret_safe() -> None:
    _Callback.instances.clear()
    transport = _Transport()
    transport.fail_revoke = True
    sessions = _Sessions()
    sessions.create_error = RuntimeError("local-secret-marker")
    flow = BrowserAuthorizationFlow(
        discovery=OAuthDiscovery(transport=transport),
        client=OAuthClient(transport=transport),
        sessions=sessions,
        browser_opener=lambda _url: True,
        callback_factory=_Callback,
    )

    with pytest.raises(AuthorizationError) as raised:
        await flow.authorize_and_store(
            target_key=(
                "endpoint:https://runtime.example.test:demo-tenant:demo-project"
            ),
            protected_resource_metadata_url=(
                "https://runtime.example.test/api/integrations/management/v1/"
                ".well-known/oauth-protected-resource"
            ),
            resource=("urn:kdcube:management:deployment:demo-tenant:demo-project"),
            timeout_seconds=5,
        )

    assert raised.value.code == "oauth_session_cleanup_failed"
    assert "local-secret-marker" not in str(raised.value)
    assert "server-secret-marker" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert sessions.values == []
    assert _Callback.instances[0].closed is True

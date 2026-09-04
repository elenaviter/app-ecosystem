from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from connection_hub_cli import profile_connection
from connection_hub_cli.errors import CredentialError


class _Profiles:
    def require(self, name: str):
        return SimpleNamespace(
            name=name,
            endpoint="https://runtime.example/mcp",
            credential_ref="credential-ref",
        )


class _Credentials:
    def __init__(self, value: str | None) -> None:
        self.value = value

    def get(self, _credential_ref: str) -> str | None:
        return self.value


class _OAuthProfiles:
    def require(self, name: str):
        return SimpleNamespace(
            name=name,
            endpoint="https://runtime.example/mcp",
            credential_ref="oauth-credential-ref",
            auth_type="oauth",
        )


class _OAuthSessions:
    def __init__(self, value: str) -> None:
        self.value = value
        self.requested: list[str] = []

    async def access_token(self, profile_name: str) -> str:
        self.requested.append(profile_name)
        return self.value


@pytest.mark.asyncio
async def test_profile_connection_resolves_credential_outside_adapter_config(
    monkeypatch,
) -> None:
    received = {}
    connected = (object(), object())

    @asynccontextmanager
    async def fake_connect(**kwargs):
        received.update(kwargs)
        yield connected

    monkeypatch.setattr(profile_connection, "connect_remote_tools", fake_connect)

    async with profile_connection.connect_profile_tools(
        profile_name="problem-board",
        profiles=_Profiles(),
        credentials=_Credentials("synthetic-bearer"),
    ) as result:
        assert result == connected

    assert received == {
        "endpoint": "https://runtime.example/mcp",
        "bearer": "synthetic-bearer",
        "message_handler": None,
    }


@pytest.mark.asyncio
async def test_profile_connection_requires_credential_custody() -> None:
    with pytest.raises(CredentialError) as raised:
        async with profile_connection.connect_profile_tools(
            profile_name="problem-board",
            profiles=_Profiles(),
            credentials=_Credentials(None),
        ):
            pass

    assert raised.value.code == "credential_missing"


@pytest.mark.asyncio
async def test_profile_connection_resolves_oauth_through_the_shared_session_service(
    monkeypatch,
) -> None:
    received = {}
    connected = (object(), object())
    oauth = _OAuthSessions("fresh-oauth-access")

    @asynccontextmanager
    async def fake_connect(**kwargs):
        received.update(kwargs)
        yield connected

    monkeypatch.setattr(profile_connection, "connect_remote_tools", fake_connect)

    async with profile_connection.connect_profile_tools(
        profile_name="problem-board",
        profiles=_OAuthProfiles(),
        credentials=_Credentials(None),
        oauth_sessions=oauth,
    ) as result:
        assert result == connected

    assert oauth.requested == ["problem-board"]
    assert received["bearer"] == "fresh-oauth-access"

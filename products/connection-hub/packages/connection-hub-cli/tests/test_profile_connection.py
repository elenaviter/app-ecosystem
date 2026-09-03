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


@pytest.mark.asyncio
async def test_profile_connection_resolves_credential_outside_adapter_config(monkeypatch) -> None:
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

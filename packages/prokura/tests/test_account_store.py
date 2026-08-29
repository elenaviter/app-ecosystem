# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

from __future__ import annotations

import json

import pytest

from prokura.delegated_to_kdcube.models import ConnectedAccount
from prokura.delegated_to_kdcube.store import DelegatedToKdcubeStore


class _MemoryUserConfiguration:
    def __init__(self) -> None:
        self.props = {}
        self.secrets = {}
        self.cache_clears = []

    async def get_user_prop(self, key, **kwargs):
        return self.props.get((kwargs["user_id"], kwargs["bundle_id"], key), kwargs.get("default"))

    async def set_user_prop(self, key, value, **kwargs):
        self.props[(kwargs["user_id"], kwargs["bundle_id"], key)] = value

    async def delete_user_prop(self, key, **kwargs):
        self.props.pop((kwargs["user_id"], kwargs["bundle_id"], key), None)

    async def set_user_secret(self, key, value, **kwargs):
        self.secrets[(kwargs["user_id"], kwargs["bundle_id"], key)] = value

    async def get_secret(self, key, **kwargs):
        secret_key = key.removeprefix("u:")
        return self.secrets.get((kwargs["user_id"], kwargs["bundle_id"], secret_key))

    async def delete_user_secret(self, key, **kwargs):
        self.secrets.pop((kwargs["user_id"], kwargs["bundle_id"], key), None)

    def clear_secret_cache(self, **kwargs):
        self.cache_clears.append(kwargs)


@pytest.mark.asyncio
async def test_account_metadata_and_credentials_use_separate_host_channels():
    backend = _MemoryUserConfiguration()
    store = DelegatedToKdcubeStore(user_id="user-1", backend=backend)

    account = await store.upsert_account(
        ConnectedAccount(
            account_id="account-1",
            provider_id="mail",
            claims=("mail:read",),
            credential_id="credential-1",
        )
    )
    await store.set_credential(
        account.credential_id,
        {"access_token": "server-side", "claims": ["mail:read"]},
    )

    assert [item.account_id for item in await store.list_accounts()] == ["account-1"]
    credential = await store.get_credential("credential-1")
    assert credential["access_token"] == "server-side"
    assert backend.cache_clears == [
        {"user_id": "user-1", "bundle_id": "connection-hub@1-0"}
    ]
    assert all("server-side" not in json.dumps(value) for value in backend.props.values())

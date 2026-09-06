# SPDX-License-Identifier: MIT
"""Password-kind connectors (IMAP app passwords) need the login beside the
secret. The connect form sends the login as the account's email, so the
credential record must be completed with it, and the claim verdict must
carry the account's email for transports that read older records."""

from __future__ import annotations

import asyncio
from typing import Any

from connection_hub.delegated_to_kdcube.broker import DelegatedToKdcubeBroker
from connection_hub.delegated_to_kdcube.models import (
    ConnectorApp,
    DelegatedToKdcubeConfig,
    IntegrationProvider,
    ProviderClaim,
)
from connection_hub.delegated_to_kdcube.operations import DelegatedToKdcubeOperations
from connection_hub.delegated_to_kdcube.store import DelegatedToKdcubeStore


class _MemoryBackend:
    def __init__(self) -> None:
        self.props: dict[str, Any] = {}
        self.secrets: dict[str, str] = {}

    async def get_user_prop(self, key: str, **kwargs: Any) -> Any:
        return self.props.get(key)

    async def set_user_prop(self, key: str, value: Any, **kwargs: Any) -> None:
        self.props[key] = value

    async def delete_user_prop(self, key: str, **kwargs: Any) -> None:
        self.props.pop(key, None)

    # The host backend namespaces user secrets under "u:" on read; mirror it.
    async def set_user_secret(self, key: str, value: str, **kwargs: Any) -> None:
        self.secrets[f"u:{key}"] = value

    async def get_secret(self, key: str, **kwargs: Any) -> Any:
        return self.secrets.get(key)

    async def delete_user_secret(self, key: str, **kwargs: Any) -> None:
        self.secrets.pop(f"u:{key}", None)

    def clear_secret_cache(self, **kwargs: Any) -> None:
        return None


def _config() -> DelegatedToKdcubeConfig:
    provider = IntegrationProvider(
        provider_id="icloud_mail",
        label="iCloud Mail",
        adapter="email.imap_smtp_app_password",
        claims={
            "email:read": ProviderClaim(claim_id="email:read"),
            "email:send": ProviderClaim(claim_id="email:send"),
        },
        connector_apps={
            "app_password": ConnectorApp(
                connector_app_id="app_password", provider_id="icloud_mail", label="App password",
            ),
        },
    )
    return DelegatedToKdcubeConfig(enabled=True, providers={"icloud_mail": provider})


def _rig() -> tuple[DelegatedToKdcubeOperations, DelegatedToKdcubeBroker, DelegatedToKdcubeStore]:
    config = _config()
    store = DelegatedToKdcubeStore(user_id="user-1", backend=_MemoryBackend())
    return DelegatedToKdcubeOperations(config=config, store=store), DelegatedToKdcubeBroker(config=config, store=store), store


def test_app_password_credential_is_stored_with_its_login():
    ops, broker, store = _rig()
    result = asyncio.run(ops.connect_credential({
        "provider_id": "icloud_mail",
        "connector_app_id": "app_password",
        "email": "person@icloud.example",
        "claims": ["email:read", "email:send"],
        "app_password": "abcd-efgh-ijkl-mnop",
    }))
    account_id = result["account"]["account_id"] if isinstance(result.get("account"), dict) else result.get("account_id")
    accounts = asyncio.run(store.list_accounts(provider_id="icloud_mail"))
    account = next(a for a in accounts if a.account_id == account_id) if account_id else accounts[0]
    credential = asyncio.run(store.get_credential(account.credential_id))
    assert credential["app_password"] == "abcd-efgh-ijkl-mnop"
    assert credential["username"] == "person@icloud.example", "the login travels with the secret"

    verdict = asyncio.run(broker.ensure_claim(provider_id="icloud_mail", claim="email:read", connector_app_id="app_password", account_id=account.account_id))
    assert verdict.ok is True
    assert verdict.account_email == "person@icloud.example"
    assert verdict.to_dict()["account_email"] == "person@icloud.example"
    assert "app_password" not in verdict.to_dict()  # secrets never in the plain dict


def test_explicit_username_wins_over_email():
    ops, _, store = _rig()
    asyncio.run(ops.connect_credential({
        "provider_id": "icloud_mail",
        "connector_app_id": "app_password",
        "email": "person@icloud.example",
        "username": "person.login",
        "claims": ["email:read"],
        "app_password": "abcd-efgh-ijkl-mnop",
    }))
    account = asyncio.run(store.list_accounts(provider_id="icloud_mail"))[0]
    credential = asyncio.run(store.get_credential(account.credential_id))
    assert credential["username"] == "person.login"

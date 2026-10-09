"""A GitHub refresh of an account stored before incarnations existed keeps the rotated token.

GitHub rotates the refresh token on every refresh and invalidates the old one. ``set_account_credential``
writes a refreshed credential only for the connection (incarnation) the refresh was for; an account stored
before incarnations existed has none, so the rotated token was discarded and the next refresh presented a
dead one (live: refresh 200, nothing written, then reconnect_required). The broker now mints the legacy
incarnation once, under the account lock, before using the token. A disconnect or a reconnect while the
refresh is in flight still writes nothing, and no token reaches the logs.
"""

from __future__ import annotations

import logging
import time

import pytest

from connection_hub.delegated_to_kdcube.broker import DelegatedToKdcubeBroker
from connection_hub.delegated_to_kdcube.refresh_lock import LocalRefreshLock
from connection_hub.delegated_to_kdcube.store import DelegatedToKdcubeStore
from test_github_app_provider import CLAIM, _MemoryBackend, _RotatingGitHub, _config, _connected, _expired, \
    _patch_refresh, _secret
from test_w578_account_incarnation import _AccountLocks


async def _legacy(*, locked=True):
    backend = _MemoryBackend()
    store = DelegatedToKdcubeStore(user_id="person-1", backend=backend,
                                   account_lock=_AccountLocks() if locked else None)
    account_id = await _connected(store, _expired())
    key = store.account_prop_key(account_id)
    backend.props[key] = {**backend.props[key], "incarnation": ""}  # stored before incarnations existed
    assert (await store.get_account(account_id)).incarnation == ""
    broker = DelegatedToKdcubeBroker(config=_config(), store=store, client_secret_resolver=_secret,
                                     refresh_lock=LocalRefreshLock())
    return store, backend, broker, account_id


async def _issue(broker, account_id, *, force=False):
    if force:
        return await broker.ensure_claim(provider_id="github", claim=CLAIM, connector_app_id="app",
                                         account_id=account_id, force_refresh=True)
    return await broker.ensure_claim(provider_id="github", claim=CLAIM, connector_app_id="app",
                                     account_id=account_id)


async def _expire_stored(store, account_id):
    account = await store.get_account(account_id)
    stored = await store.get_credential(account.credential_id)
    await store.set_credential(account.credential_id, {**stored, "expires_at": int(time.time()) - 10})


@pytest.mark.asyncio
async def test_a_legacy_account_keeps_the_rotated_token_and_the_next_refresh_uses_it(monkeypatch):
    github = _RotatingGitHub("ghr_0")
    _patch_refresh(monkeypatch, github)
    store, _, broker, account_id = await _legacy()

    first = await _issue(broker, account_id)
    assert first.ok and first.credential.credential["access_token"] == "ghu_1"
    account = await store.get_account(account_id)
    assert len(account.incarnation) == 32, "minted once for the legacy record"
    assert (await store.get_credential(account.credential_id))["refresh_token"] == "ghr_1", "rotation kept"

    await _expire_stored(store, account_id)
    second = await _issue(broker, account_id)
    assert second.ok and second.credential.credential["access_token"] == "ghu_2"
    assert github.calls == 2 and github.valid_refresh == "ghr_2", "the second refresh presented ghr_1"
    assert (await store.get_account(account_id)).incarnation == account.incarnation


@pytest.mark.asyncio
async def test_a_disconnect_during_the_refresh_writes_nothing(monkeypatch):
    github = _RotatingGitHub("ghr_0")
    store, backend, broker, account_id = await _legacy()
    credential_id = (await store.get_account(account_id)).credential_id

    async def refresh_then_disconnect(self, credential, **kwargs):
        refreshed = await github.refresh(self, credential, **kwargs)
        assert await store.disconnect_account(account_id)
        return refreshed

    from connection_hub.delegated_to_kdcube.providers.github import GitHubAppAdapter
    monkeypatch.setattr(GitHubAppAdapter, "refresh_credential", refresh_then_disconnect)
    await _issue(broker, account_id)
    assert await store.get_account(account_id) is None
    assert not await store.get_credential(credential_id), "no old secret outlives the disconnect"


@pytest.mark.asyncio
async def test_a_reconnect_during_the_refresh_is_not_overwritten(monkeypatch):
    github = _RotatingGitHub("ghr_0")
    store, backend, broker, account_id = await _legacy()

    async def refresh_then_reconnect(self, credential, **kwargs):
        refreshed = await github.refresh(self, credential, **kwargs)
        await _connected(store, {"access_token": "ghu_consent", "refresh_token": "ghr_consent",
                                 "expires_at": int(time.time()) + 28800})
        return refreshed

    from connection_hub.delegated_to_kdcube.providers.github import GitHubAppAdapter
    monkeypatch.setattr(GitHubAppAdapter, "refresh_credential", refresh_then_reconnect)
    await _issue(broker, account_id)
    account = await store.get_account(account_id)
    stored = await store.get_credential(account.credential_id)
    assert stored["refresh_token"] == "ghr_consent", "the fresh consent is kept"


@pytest.mark.asyncio
async def test_no_token_length_or_digest_reaches_the_logs(monkeypatch, caplog):
    github = _RotatingGitHub("ghr_0")
    store, backend, broker, account_id = await _legacy()

    async def refresh_then_disconnect(self, credential, **kwargs):
        refreshed = await github.refresh(self, credential, **kwargs)
        await store.disconnect_account(account_id)
        return refreshed

    from connection_hub.delegated_to_kdcube.providers.github import GitHubAppAdapter
    monkeypatch.setattr(GitHubAppAdapter, "refresh_credential", refresh_then_disconnect)
    caplog.set_level(logging.DEBUG)
    await _issue(broker, account_id)
    text = caplog.text
    assert "refreshed credential not stored" in text and "reason=connection_changed" in text
    for secret in ("ghr_0", "ghr_1", "ghu_0", "ghu_1", "client-secret"):
        assert secret not in text
    assert "len=" not in text and "sha256" not in text and "digest" not in text


@pytest.mark.asyncio
async def test_without_the_account_lock_a_legacy_refresh_behaves_as_before_and_says_why(monkeypatch, caplog):
    github = _RotatingGitHub("ghr_0")
    _patch_refresh(monkeypatch, github)
    store, _, broker, account_id = await _legacy(locked=False)
    caplog.set_level(logging.WARNING)
    await _issue(broker, account_id)
    assert (await store.get_account(account_id)).incarnation == "", "nothing minted without the lock"
    assert "account incarnation unavailable" in caplog.text and "reason=incarnation_missing" in caplog.text
    assert "ghr_" not in caplog.text and "ghu_" not in caplog.text

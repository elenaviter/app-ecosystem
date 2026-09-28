# SPDX-License-Identifier: MIT
"""The GitHub App connected-account provider and single-flight refresh.

GitHub rotates a user token's refresh token on every refresh and invalidates
the old one. Concurrent requests for one person's GitHub account must share
one refresh; a refresh GitHub refuses marks the account "reconnect required"
once, without retrying.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from connection_hub.delegated_to_kdcube import adapters as adapters_module
from connection_hub.delegated_to_kdcube.broker import DelegatedToKdcubeBroker
from connection_hub.delegated_to_kdcube.models import (
    CREDENTIAL_ACTIVE,
    CREDENTIAL_RECONNECT_REQUIRED,
    REASON_RECONNECT_REQUIRED,
    ConnectorApp,
    DelegatedToKdcubeConfig,
    IntegrationProvider,
    ProviderClaim,
)
from connection_hub.delegated_to_kdcube.operations import DelegatedToKdcubeOperations
from connection_hub.delegated_to_kdcube.providers.github import (
    REPOSITORY_COVERED,
    REPOSITORY_MISSING,
    REPOSITORY_NOT_INSTALLED,
    GitHubAppAdapter,
    install_url,
    repository_coverage,
)
from connection_hub.delegated_to_kdcube.refresh_lock import LocalRefreshLock, RedisRefreshLock
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

    async def set_user_secret(self, key: str, value: str, **kwargs: Any) -> None:
        self.secrets[f"u:{key}"] = value

    async def get_secret(self, key: str, **kwargs: Any) -> Any:
        return self.secrets.get(key)

    async def delete_user_secret(self, key: str, **kwargs: Any) -> None:
        self.secrets.pop(f"u:{key}", None)

    def clear_secret_cache(self, **kwargs: Any) -> None:
        return None


CLAIM = "repositories:write"


def _config() -> DelegatedToKdcubeConfig:
    provider = IntegrationProvider(
        provider_id="github",
        label="GitHub",
        adapter="github.app",
        adapter_config={"app_slug": "example-kdcube"},
        claims={CLAIM: ProviderClaim(claim_id=CLAIM)},
        connector_apps={
            "app": ConnectorApp(connector_app_id="app", provider_id="github", client_id="Iv1.example"),
        },
    )
    return DelegatedToKdcubeConfig(enabled=True, providers={"github": provider})


async def _secret(**_: Any) -> str:
    return "client-secret"


async def _connected(store: DelegatedToKdcubeStore, credential: dict[str, Any]) -> str:
    ops = DelegatedToKdcubeOperations(config=_config(), store=store)
    result = await ops.connect_credential(
        {
            "provider_id": "github",
            "connector_app_id": "app",
            "external_subject": "1001",
            "display_name": "person",
            "claims": [CLAIM],
            "credential": {"oauth": True, **credential},
        }
    )
    return result["account"]["account_id"]


class _RotatingGitHub:
    """GitHub's refresh rule: a refresh token works once, then both old tokens are dead."""

    def __init__(self, refresh_token: str) -> None:
        self.valid_refresh = refresh_token
        self.calls = 0
        self.generation = 0

    async def refresh(self, adapter: Any, credential: dict[str, Any], **_: Any) -> dict[str, Any]:
        self.calls += 1
        presented = credential.get("refresh_token")
        await asyncio.sleep(0.01)  # a real round trip: other requests run meanwhile
        if presented != self.valid_refresh:
            raise RuntimeError("GitHub OAuth error: bad_refresh_token")
        self.generation += 1
        self.valid_refresh = f"ghr_{self.generation}"
        refreshed = dict(credential)
        refreshed.update(
            {
                "access_token": f"ghu_{self.generation}",
                "refresh_token": self.valid_refresh,
                "expires_at": int(time.time()) + 28800,
                "refreshed_at": int(time.time()),
            }
        )
        return refreshed


def _patch_refresh(monkeypatch: pytest.MonkeyPatch, github: _RotatingGitHub) -> None:
    async def refresh_credential(self: Any, credential: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return await github.refresh(self, credential, **kwargs)

    monkeypatch.setattr(GitHubAppAdapter, "refresh_credential", refresh_credential)


def _expired() -> dict[str, Any]:
    return {"access_token": "ghu_0", "refresh_token": "ghr_0", "expires_at": int(time.time()) - 10}


@pytest.mark.asyncio
async def test_concurrent_issues_share_one_refresh(monkeypatch):
    github = _RotatingGitHub("ghr_0")
    _patch_refresh(monkeypatch, github)
    store = DelegatedToKdcubeStore(user_id="person-1", backend=_MemoryBackend())
    account_id = await _connected(store, _expired())
    broker = DelegatedToKdcubeBroker(
        config=_config(), store=store, client_secret_resolver=_secret, refresh_lock=LocalRefreshLock()
    )

    verdicts = await asyncio.gather(
        *[
            broker.ensure_claim(provider_id="github", claim=CLAIM, connector_app_id="app", account_id=account_id)
            for _ in range(8)
        ]
    )

    assert [v.ok for v in verdicts] == [True] * 8
    assert github.calls == 1, "one refresh for eight concurrent issues"
    assert {v.credential.credential["access_token"] for v in verdicts} == {"ghu_1"}
    account = await store.get_account(account_id)
    stored = await store.get_credential(account.credential_id)
    assert stored["refresh_token"] == "ghr_1", "the rotated refresh token is kept"


@pytest.mark.asyncio
async def test_without_the_lock_concurrent_refreshes_lose_the_connection(monkeypatch):
    """The race the lock prevents: this is what concurrent issues did before."""

    class _NoLock:
        @asynccontextmanager
        async def hold(self, key: str):
            yield True

    github = _RotatingGitHub("ghr_0")
    _patch_refresh(monkeypatch, github)
    store = DelegatedToKdcubeStore(user_id="person-1", backend=_MemoryBackend())
    account_id = await _connected(store, _expired())
    broker = DelegatedToKdcubeBroker(config=_config(), store=store, client_secret_resolver=_secret, refresh_lock=_NoLock())

    verdicts = await asyncio.gather(
        *[
            broker.ensure_claim(provider_id="github", claim=CLAIM, connector_app_id="app", account_id=account_id)
            for _ in range(4)
        ]
    )

    assert github.calls == 4
    assert any(not v.ok for v in verdicts)


@pytest.mark.asyncio
async def test_a_refused_refresh_marks_reconnect_once_without_retry(monkeypatch):
    github = _RotatingGitHub("ghr_revoked_elsewhere")
    _patch_refresh(monkeypatch, github)
    store = DelegatedToKdcubeStore(user_id="person-1", backend=_MemoryBackend())
    account_id = await _connected(store, _expired())
    broker = DelegatedToKdcubeBroker(
        config=_config(), store=store, client_secret_resolver=_secret, refresh_lock=LocalRefreshLock()
    )

    verdict = await broker.ensure_claim(provider_id="github", claim=CLAIM, connector_app_id="app", account_id=account_id)

    assert verdict.ok is False
    assert verdict.error == REASON_RECONNECT_REQUIRED
    assert github.calls == 1, "no retry loop"
    account = await store.get_account(account_id)
    assert account.metadata.get("credential_status") == CREDENTIAL_RECONNECT_REQUIRED


@pytest.mark.asyncio
async def test_a_current_token_is_not_refreshed(monkeypatch):
    github = _RotatingGitHub("ghr_0")
    _patch_refresh(monkeypatch, github)
    store = DelegatedToKdcubeStore(user_id="person-1", backend=_MemoryBackend())
    account_id = await _connected(
        store, {"access_token": "ghu_0", "refresh_token": "ghr_0", "expires_at": int(time.time()) + 3600}
    )
    broker = DelegatedToKdcubeBroker(config=_config(), store=store, client_secret_resolver=_secret)

    verdict = await broker.ensure_claim(provider_id="github", claim=CLAIM, connector_app_id="app", account_id=account_id)

    assert verdict.ok and verdict.credential.credential["access_token"] == "ghu_0"
    assert github.calls == 0


@pytest.mark.asyncio
async def test_a_forced_refresh_after_another_holder_refreshed_uses_that_refresh(monkeypatch):
    """A 401 on a token another request already replaced does not refresh again."""

    github = _RotatingGitHub("ghr_0")
    _patch_refresh(monkeypatch, github)
    store = DelegatedToKdcubeStore(user_id="person-1", backend=_MemoryBackend())
    account_id = await _connected(store, _expired())
    broker = DelegatedToKdcubeBroker(
        config=_config(), store=store, client_secret_resolver=_secret, refresh_lock=LocalRefreshLock()
    )
    account = await store.get_account(account_id)
    stale = await store.get_credential(account.credential_id)

    first = await broker.ensure_claim(provider_id="github", claim=CLAIM, connector_app_id="app", account_id=account_id)
    again = await broker._refresh_credential_if_needed(
        provider_id="github",
        claim=CLAIM,
        connector_app_id="app",
        account_id=account_id,
        credential_id=account.credential_id,
        credential=stale,
        force=True,
    )

    assert first.ok and again["access_token"] == "ghu_1"
    assert github.calls == 1


@pytest.mark.asyncio
async def test_a_refresh_token_past_its_six_months_is_not_refreshable():
    adapter = GitHubAppAdapter()
    live = {"oauth": True, "refresh_token": "ghr_0", "refresh_token_expires_at": int(time.time()) + 60}
    dead = {"oauth": True, "refresh_token": "ghr_0", "refresh_token_expires_at": int(time.time()) - 60}
    assert adapter.credential_refreshable(live) is True
    assert adapter.credential_refreshable(dead) is False


def _mock_client(handler: Any) -> Any:
    real = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    return factory


@pytest.mark.asyncio
async def test_token_calls_ask_for_json_and_keep_the_rotated_refresh_token(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        form = parse_qs(request.content.decode())
        assert form["grant_type"] == ["refresh_token"] and form["refresh_token"] == ["ghr_0"]
        return httpx.Response(
            200,
            json={
                "access_token": "ghu_1",
                "expires_in": 28800,
                "refresh_token": "ghr_1",
                "refresh_token_expires_in": 15811200,
                "token_type": "bearer",
                "scope": "",
            },
        )

    monkeypatch.setattr(adapters_module.httpx, "AsyncClient", _mock_client(handler))
    refreshed = await GitHubAppAdapter().refresh_credential(
        {"oauth": True, "access_token": "ghu_0", "refresh_token": "ghr_0"},
        client_id="Iv1.example",
        client_secret="client-secret",
    )

    assert seen[0].headers["accept"] == "application/json"
    assert refreshed["access_token"] == "ghu_1" and refreshed["refresh_token"] == "ghr_1"
    assert refreshed["expires_at"] > time.time() + 28000
    assert refreshed["refresh_token_expires_at"] > time.time() + 15000000


@pytest.mark.asyncio
async def test_a_200_with_an_error_field_is_a_failure(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "bad_refresh_token", "error_description": "The refresh token passed is incorrect or expired."})

    monkeypatch.setattr(adapters_module.httpx, "AsyncClient", _mock_client(handler))
    with pytest.raises(RuntimeError, match="bad_refresh_token"):
        await GitHubAppAdapter().refresh_credential(
            {"oauth": True, "refresh_token": "ghr_0"}, client_id="Iv1.example", client_secret="client-secret"
        )


def test_authorization_asks_for_no_scopes():
    adapter = GitHubAppAdapter()
    assert adapter.provider_scopes_for_claims([CLAIM], {CLAIM: {"provider_scopes": ["repo"]}}) == []


def _github_api(installations: list[dict[str, Any]], repositories: dict[int, list[str]]):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer ghu_1"
        path = request.url.path
        if path == "/user/installations":
            return httpx.Response(200, json={"total_count": len(installations), "installations": installations})
        if path.startswith("/user/installations/") and path.endswith("/repositories"):
            installation_id = int(path.split("/")[3])
            repos = [{"full_name": name} for name in repositories.get(installation_id, [])]
            return httpx.Response(200, json={"total_count": len(repos), "repositories": repos})
        return httpx.Response(404, json={})

    return handler


@pytest.mark.asyncio
async def test_repository_coverage_names_each_state_and_the_link_that_fixes_it():
    installations = [
        {
            "id": 11,
            "account": {"login": "example-org", "id": 501, "type": "Organization"},
            "repository_selection": "selected",
            "html_url": "https://github.com/organizations/example-org/settings/installations/11",
        }
    ]
    handler = _github_api(installations, {11: ["example-org/app-ecosystem"]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        coverage = await repository_coverage(
            "ghu_1",
            ["example-org/app-ecosystem", "example-org/applications", "other-owner/tools"],
            app_slug="example-kdcube",
            client=client,
        )

    owners = {item["owner"]: item for item in coverage["owners"]}
    org = owners["example-org"]
    assert org["installed"] is True
    assert {r["repository"]: r["state"] for r in org["repositories"]} == {
        "example-org/app-ecosystem": REPOSITORY_COVERED,
        "example-org/applications": REPOSITORY_MISSING,
    }
    assert org["install_url"].endswith("/settings/installations/11")
    other = owners["other-owner"]
    assert other["installed"] is False
    assert other["repositories"] == [{"repository": "other-owner/tools", "state": REPOSITORY_NOT_INSTALLED}]
    assert other["install_url"] == install_url("example-kdcube")


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(self, name: str, value: str, *, nx: bool = False, px: int = 0) -> bool:
        if nx and name in self.values:
            return False
        self.values[name] = value
        return True

    async def eval(self, script: str, numkeys: int, name: str, token: str) -> int:
        if self.values.get(name) == token:
            del self.values[name]
            return 1
        return 0


@pytest.mark.asyncio
async def test_redis_lock_serializes_holders_and_releases_only_its_own():
    redis = _FakeRedis()
    lock = RedisRefreshLock(redis, poll_seconds=0.001, wait_seconds=1.0)
    order: list[str] = []

    async def holder(name: str) -> None:
        async with lock.hold("person-1:cred") as held:
            assert held
            order.append(f"{name}+")
            await asyncio.sleep(0.01)
            order.append(f"{name}-")

    await asyncio.gather(holder("a"), holder("b"))

    assert order in (["a+", "a-", "b+", "b-"], ["b+", "b-", "a+", "a-"])
    assert redis.values == {}


@pytest.mark.asyncio
async def test_redis_lock_gives_up_after_its_wait():
    redis = _FakeRedis()
    redis.values["connection_hub:refresh_lock:person-1:cred"] = "someone-else"
    lock = RedisRefreshLock(redis, poll_seconds=0.001, wait_seconds=0.01)

    async with lock.hold("person-1:cred") as held:
        assert held is False
    assert redis.values["connection_hub:refresh_lock:person-1:cred"] == "someone-else"


def test_github_app_is_a_registered_provider_type():
    assert isinstance(adapters_module.resolve_adapter("github.app"), GitHubAppAdapter)
    assert json.dumps({"state": CREDENTIAL_ACTIVE})

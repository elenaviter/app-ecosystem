# SPDX-License-Identifier: MIT
"""The My Card GitHub section (W371): link, unlink, status, commit email."""

from __future__ import annotations

import time
from typing import Any

import pytest

from connection_hub.delegated_credentials.project_identity_lifecycle import (
    MY_CARD_COMMIT_EMAIL_PROPERTY,
    MY_CARD_GITHUB_PROPERTY,
)
from connection_hub.delegated_to_kdcube.models import (
    ConnectorApp,
    DelegatedToKdcubeConfig,
    IntegrationProvider,
    ProviderClaim,
)
from connection_hub.delegated_to_kdcube.operations import DelegatedToKdcubeOperations
from connection_hub.delegated_to_kdcube.store import DelegatedToKdcubeStore
from connection_hub.project_github_key import (
    CONNECTED,
    NEEDS_RECONNECT,
    NOT_CONNECTED,
    ProjectGitHubKey,
)

from test_github_app_provider import CLAIM, _MemoryBackend

PROJECT_REF = "work:project:quickstart"
PERSON = "platform-user-2"


def _config() -> DelegatedToKdcubeConfig:
    provider = IntegrationProvider(
        provider_id="github",
        label="GitHub",
        adapter="github.app",
        adapter_config={"app_slug": "example-kdcube"},
        claims={CLAIM: ProviderClaim(claim_id=CLAIM)},
        connector_apps={"app": ConnectorApp(connector_app_id="app", provider_id="github", client_id="Iv1.example")},
    )
    return DelegatedToKdcubeConfig(enabled=True, providers={"github": provider})


class _Access:
    """My Card settings as AutomationAccessService serves them, in memory."""

    def __init__(self) -> None:
        self.properties: dict[str, Any] = {}
        self.revision = 3
        self.sets: list[dict[str, Any]] = []

    async def project_person_my_card_settings_get(self, user, *, project_ref, target_subject="", request_id):
        return {
            "ok": True,
            "project_ref": project_ref,
            "person_subject": target_subject or user["user_id"],
            "card_revision": self.revision,
            "properties": dict(self.properties),
        }

    async def project_person_my_card_settings_set(self, user, *, project_ref, changes, target_subject="", request_id):
        self.sets.append({"target_subject": target_subject, "changes": dict(changes)})
        before = dict(self.properties)
        for key, value in changes.items():
            if value is None:
                self.properties.pop(key, None)
            else:
                self.properties[key] = value
        changed = before != self.properties
        self.revision += int(changed)
        return {
            "ok": True,
            "project_ref": project_ref,
            "person_subject": target_subject or user["user_id"],
            "card_revision": self.revision,
            "changed": changed,
            "properties": dict(self.properties),
        }


async def _connect(store: DelegatedToKdcubeStore, *, subject: str = "1001", login: str = "person", **credential: Any) -> str:
    result = await DelegatedToKdcubeOperations(config=_config(), store=store).connect_credential(
        {
            "provider_id": "github",
            "connector_app_id": "app",
            "external_subject": subject,
            "display_name": login,
            "claims": [CLAIM],
            "credential": {
                "oauth": True,
                "access_token": "ghu_live",
                "refresh_token": "ghr_live",
                "expires_at": int(time.time()) + 3600,
                "refresh_token_expires_at": int(time.time()) + 86400,
                **credential,
            },
        }
    )
    return result["account"]["account_id"]


def _key(access: _Access, store: DelegatedToKdcubeStore, **kwargs: Any) -> ProjectGitHubKey:
    return ProjectGitHubKey(access=access, user={"user_id": PERSON}, config=_config(), store=store, **kwargs)


def _store() -> DelegatedToKdcubeStore:
    return DelegatedToKdcubeStore(user_id=PERSON, backend=_MemoryBackend())


@pytest.mark.asyncio
async def test_link_without_a_connected_account_says_connect_first():
    access, store = _Access(), _store()

    result = await _key(access, store).link(project_ref=PROJECT_REF, request_id="r")

    assert result["error"] == "github_not_connected"
    assert result["connect"] == {"provider_id": "github", "connector_app_id": "app", "claims": [CLAIM]}
    assert access.sets == []


@pytest.mark.asyncio
async def test_link_stores_the_account_on_my_card_and_status_reads_connected_with_coverage():
    access, store = _Access(), _store()
    account_id = await _connect(store)
    seen: dict[str, Any] = {}

    async def coverage(token, repositories, *, app_slug=""):
        seen.update(token=token, repositories=repositories, app_slug=app_slug)
        return {"owners": [{"owner": "example-org", "installed": True, "install_url": "u", "repositories": []}]}

    linked = await _key(access, store, coverage=coverage).link(project_ref=PROJECT_REF, request_id="r")
    status = await _key(access, store, coverage=coverage).status(
        project_ref=PROJECT_REF,
        repositories=["example-org/app-ecosystem.git", "not a repository", "example-org/app-ecosystem"],
        request_id="r",
    )

    assert linked["connection_state"] == CONNECTED and linked["login"] == "person"
    assert access.properties[MY_CARD_GITHUB_PROPERTY]["account_id"] == account_id
    assert "access_token" not in str(access.properties), "no token on the Card"
    assert status["connection_state"] == CONNECTED
    assert status["owners"][0]["owner"] == "example-org"
    assert seen == {"token": "ghu_live", "repositories": ["example-org/app-ecosystem"], "app_slug": "example-kdcube"}
    assert status["refresh_expires_at"] > time.time()


@pytest.mark.asyncio
async def test_link_again_with_the_same_account_writes_nothing():
    access, store = _Access(), _store()
    await _connect(store)
    key = _key(access, store)
    await key.link(project_ref=PROJECT_REF, request_id="r")

    await key.link(project_ref=PROJECT_REF, request_id="r")

    assert len(access.sets) == 1


@pytest.mark.asyncio
async def test_two_connected_accounts_ask_which_one():
    access, store = _Access(), _store()
    await _connect(store, subject="1001", login="person")
    await _connect(store, subject="2002", login="person-work")

    result = await _key(access, store).link(project_ref=PROJECT_REF, request_id="r")

    assert result["error"] == "github_account_choice_required"
    assert {item["login"] for item in result["candidates"]} == {"person", "person-work"}


@pytest.mark.asyncio
async def test_status_is_not_connected_before_a_link_and_after_unlink():
    access, store = _Access(), _store()
    await _connect(store)
    key = _key(access, store)

    before = await key.status(project_ref=PROJECT_REF, request_id="r")
    await key.link(project_ref=PROJECT_REF, request_id="r")
    unlinked = await key.unlink(project_ref=PROJECT_REF, request_id="r")
    after = await key.status(project_ref=PROJECT_REF, request_id="r")

    assert before["connection_state"] == NOT_CONNECTED and before["connect"]["provider_id"] == "github"
    assert unlinked["connection_state"] == NOT_CONNECTED and unlinked["changed"] is True
    assert after["connection_state"] == NOT_CONNECTED
    assert MY_CARD_GITHUB_PROPERTY not in access.properties


@pytest.mark.asyncio
async def test_an_expired_token_without_a_working_refresh_needs_reconnect():
    access, store = _Access(), _store()
    await _connect(store, expires_at=int(time.time()) - 10, refresh_token_expires_at=int(time.time()) - 10)
    key = _key(access, store)
    await key.link(project_ref=PROJECT_REF, request_id="r")

    status = await key.status(project_ref=PROJECT_REF, request_id="r")

    assert status["connection_state"] == NEEDS_RECONNECT
    assert status["login"] == "person"


@pytest.mark.asyncio
async def test_commit_email_is_validated_set_by_the_actor_and_cleared_when_empty():
    access, store = _Access(), _store()
    key = _key(access, store)

    bad = await key.commit_email_set(project_ref=PROJECT_REF, email="not-an-email", request_id="r")
    good = await key.commit_email_set(project_ref=PROJECT_REF, email="person@example.test", request_id="r")
    status = await key.status(project_ref=PROJECT_REF, request_id="r")
    cleared = await key.commit_email_set(project_ref=PROJECT_REF, email="", request_id="r")

    assert bad["error"] == "commit_email_invalid"
    assert good["commit_email"] == "person@example.test"
    assert access.sets[0]["changes"][MY_CARD_COMMIT_EMAIL_PROPERTY]["set_by"] == PERSON
    assert status["commit_email"] == "person@example.test"
    assert cleared["commit_email"] == "" and MY_CARD_COMMIT_EMAIL_PROPERTY not in access.properties


@pytest.mark.asyncio
async def test_an_admin_sets_another_persons_commit_email_through_the_same_path():
    access, store = _Access(), _store()

    result = await _key(access, store).commit_email_set(
        project_ref=PROJECT_REF, email="owner@example.test", target_subject="platform-owner-1", request_id="r"
    )

    assert result["person_subject"] == "platform-owner-1"
    assert access.sets[0]["target_subject"] == "platform-owner-1"

# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""A person's GitHub key and commit email on their project My Card (W371).

The "GitHub key" is the person's GitHub connected account (the `github.app`
provider, delegated_to_kdcube) linked to their My Card for one project. The
agents that person owns use it while they attend that project; the token is
issued elsewhere (the issue route) and never stored here. The commit email
is what those agents' commits carry on the project.

Both live in My Card properties (project_identity_lifecycle.py), written
through the audited, revision-fenced My Card path. This module adds the
GitHub side: which connected account, whether it still works, and which of
the project's repositories the App covers.
"""

from __future__ import annotations

import re
import time
from typing import Any, Awaitable, Callable, Iterable, Mapping

from connection_hub.delegated_credentials.project_identity_lifecycle import (
    MY_CARD_COMMIT_EMAIL_PROPERTY,
    MY_CARD_GITHUB_PROPERTY,
)
from connection_hub.delegated_to_kdcube.broker import DelegatedToKdcubeBroker
from connection_hub.delegated_to_kdcube.models import (
    REASON_RECONNECT_REQUIRED,
    DelegatedToKdcubeConfig,
    IntegrationProvider,
    as_str,
)
from connection_hub.delegated_to_kdcube.providers.github import (
    app_slug_for,
    repository_coverage,
)
from connection_hub.delegated_to_kdcube.refresh_lock import RefreshLock

GITHUB_ADAPTER = "github.app"
CONNECTED = "connected"
NEEDS_RECONNECT = "needs_reconnect"
NOT_CONNECTED = "not_connected"
_EMAIL = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")

Coverage = Callable[..., Awaitable[dict[str, Any]]]


def github_provider(config: DelegatedToKdcubeConfig) -> IntegrationProvider | None:
    """The deployment's GitHub App provider: the enabled provider on `github.app`."""

    for provider in (config.providers or {}).values():
        if provider.enabled and provider.adapter == GITHUB_ADAPTER:
            return provider
    return None


def _claim(provider: IntegrationProvider) -> str:
    return next(iter(provider.claims), "")


def _connector_app_id(provider: IntegrationProvider) -> str:
    return next((key for key, app in provider.connector_apps.items() if app.enabled), "")


def _refusal(error: str, message: str, *, status: int = 409, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": error, "message": message, "status": status, **extra}


def _repositories(values: Iterable[Any] | None) -> list[str]:
    out: list[str] = []
    for value in values or ():
        text = as_str(value).strip("/")
        if text.lower().endswith(".git"):
            text = text[:-4]
        if _REPOSITORY.fullmatch(text) and text not in out:
            out.append(text)
    return out


class ProjectGitHubKey:
    """The My Card GitHub section for the signed-in person."""

    def __init__(
        self,
        *,
        access: Any,
        user: Mapping[str, Any],
        config: DelegatedToKdcubeConfig,
        store: Any,
        client_secret_resolver: Callable[..., Any] | None = None,
        refresh_lock: RefreshLock | None = None,
        coverage: Coverage = repository_coverage,
    ) -> None:
        self.access = access
        self.user = dict(user)
        self.config = config
        self.store = store
        self.client_secret_resolver = client_secret_resolver
        self.refresh_lock = refresh_lock
        self.coverage = coverage

    def _connect_hint(self, provider: IntegrationProvider) -> dict[str, Any]:
        return {
            "provider_id": provider.provider_id,
            "connector_app_id": _connector_app_id(provider),
            "claims": [_claim(provider)] if _claim(provider) else [],
        }

    async def _settings(self, project_ref: str, request_id: str, target_subject: str = "") -> dict[str, Any]:
        return await self.access.project_person_my_card_settings_get(
            self.user, project_ref=project_ref, target_subject=target_subject, request_id=request_id
        )

    async def link(self, *, project_ref: str, account_id: str = "", request_id: str) -> dict[str, Any]:
        """Link the person's GitHub connected account to their My Card for this project."""

        provider = github_provider(self.config)
        if provider is None:
            return _refusal("github_provider_not_configured", "GitHub is not set up in Connection Hub.", status=503)
        accounts = [
            account
            for account in await self.store.list_accounts(provider_id=provider.provider_id)
            if not account_id or account.account_id == account_id
        ]
        if not accounts:
            return _refusal(
                "github_not_connected",
                "Connect GitHub first; then link it to this project.",
                connect=self._connect_hint(provider),
            )
        if len(accounts) > 1:
            return _refusal(
                "github_account_choice_required",
                "You have more than one GitHub account connected; choose one.",
                candidates=[
                    {"account_id": item.account_id, "login": item.display_name} for item in accounts
                ],
            )
        account = accounts[0]
        value = {
            "provider_id": provider.provider_id,
            "account_id": account.account_id,
            "login": as_str(account.display_name),
            "linked_at": int(time.time()),
        }
        settings = await self._settings(project_ref, request_id)
        current = (settings.get("properties") or {}).get(MY_CARD_GITHUB_PROPERTY) if settings.get("ok") else None
        if isinstance(current, Mapping) and current.get("account_id") == account.account_id:
            return await self.status(project_ref=project_ref, request_id=request_id)
        result = await self.access.project_person_my_card_settings_set(
            self.user,
            project_ref=project_ref,
            changes={MY_CARD_GITHUB_PROPERTY: value},
            request_id=request_id,
        )
        if result.get("ok") is not True:
            return result
        return await self.status(project_ref=project_ref, request_id=request_id)

    async def unlink(self, *, project_ref: str, request_id: str) -> dict[str, Any]:
        """Remove the GitHub link from My Card; the connected account itself stays."""

        result = await self.access.project_person_my_card_settings_set(
            self.user,
            project_ref=project_ref,
            changes={MY_CARD_GITHUB_PROPERTY: None},
            request_id=request_id,
        )
        if result.get("ok") is not True:
            return result
        return {
            "ok": True,
            "project_ref": result.get("project_ref"),
            "connection_state": NOT_CONNECTED,
            "changed": bool(result.get("changed")),
            "card_revision": result.get("card_revision"),
        }

    async def commit_email_set(
        self, *, project_ref: str, email: str, target_subject: str = "", request_id: str
    ) -> dict[str, Any]:
        """Set (or, with an empty email, clear) the commit email on a My Card.

        The person sets their own; a project admin may set another person's
        (the W368 migration seeds the project owner's this way).
        """

        text = as_str(email)
        if text and (len(text) > 254 or not _EMAIL.fullmatch(text)):
            return _refusal("commit_email_invalid", "That is not an email address.", status=400)
        actor = as_str(self.user.get("user_id") or self.user.get("sub") or self.user.get("subject"))
        value = {"email": text, "set_by": actor, "set_at": int(time.time())} if text else None
        result = await self.access.project_person_my_card_settings_set(
            self.user,
            project_ref=project_ref,
            changes={MY_CARD_COMMIT_EMAIL_PROPERTY: value},
            target_subject=target_subject,
            request_id=request_id,
        )
        if result.get("ok") is not True:
            return result
        stored = (result.get("properties") or {}).get(MY_CARD_COMMIT_EMAIL_PROPERTY)
        return {
            "ok": True,
            "project_ref": result.get("project_ref"),
            "person_subject": result.get("person_subject"),
            "commit_email": as_str(stored.get("email")) if isinstance(stored, Mapping) else "",
            "changed": bool(result.get("changed")),
            "card_revision": result.get("card_revision"),
        }

    async def status(
        self, *, project_ref: str, repositories: Iterable[Any] | None = None, request_id: str
    ) -> dict[str, Any]:
        """connected | needs_reconnect | not_connected, the login, and per-owner coverage."""

        provider = github_provider(self.config)
        if provider is None:
            return _refusal("github_provider_not_configured", "GitHub is not set up in Connection Hub.", status=503)
        settings = await self._settings(project_ref, request_id)
        if settings.get("ok") is not True:
            return settings
        properties = settings.get("properties") or {}
        link = properties.get(MY_CARD_GITHUB_PROPERTY)
        email = properties.get(MY_CARD_COMMIT_EMAIL_PROPERTY)
        base = {
            "ok": True,
            "project_ref": settings.get("project_ref"),
            "card_revision": settings.get("card_revision"),
            "commit_email": as_str(email.get("email")) if isinstance(email, Mapping) else "",
            "app_slug": app_slug_for(provider),
        }
        if not isinstance(link, Mapping) or not as_str(link.get("account_id")):
            return {**base, "connection_state": NOT_CONNECTED, "connect": self._connect_hint(provider)}
        account = await self.store.get_account(as_str(link.get("account_id")))
        if account is None:
            return {
                **base,
                "connection_state": NOT_CONNECTED,
                "reason": "github_account_disconnected",
                "connect": self._connect_hint(provider),
            }
        broker = DelegatedToKdcubeBroker(
            config=self.config,
            store=self.store,
            client_secret_resolver=self.client_secret_resolver,
            refresh_lock=self.refresh_lock,
        )
        verdict = await broker.ensure_claim(
            provider_id=provider.provider_id,
            claim=_claim(provider),
            connector_app_id=account.connector_app_id or _connector_app_id(provider),
            account_id=account.account_id,
        )
        base.update({"account_id": account.account_id, "login": as_str(account.display_name)})
        if not verdict.ok:
            state = NEEDS_RECONNECT if verdict.error == REASON_RECONNECT_REQUIRED else NOT_CONNECTED
            return {**base, "connection_state": state, "reason": verdict.error, "connect": self._connect_hint(provider)}
        credential = verdict.credential.credential if verdict.credential else {}
        base["refresh_expires_at"] = int(credential.get("refresh_token_expires_at") or 0)
        wanted = _repositories(repositories)
        if wanted:
            coverage = await self.coverage(
                as_str(credential.get("access_token")), wanted, app_slug=app_slug_for(provider)
            )
            base["owners"] = list(coverage.get("owners") or [])
            if coverage.get("error"):
                base["coverage_error"] = "github_unreachable"
        return {**base, "connection_state": CONNECTED}


class AgentGitHubTokenIssuer:
    """Give an attending agent its owner's GitHub token for one repository (W371).

    The caller has already authenticated the agent's Card bearer: the
    grantor (the agent's owner) and the Card's access id come from verified
    credential facts, never from the payload. The project host decides
    attendance, the Card's `project.github.use` and whether the repository
    is on the project card; this class then reads the owner's My Card and
    issues through the broker (one refresh at a time per account). The
    token is returned to the caller only; the audit line names the project,
    repository, agent and owner, and never the token.
    """

    def __init__(
        self,
        *,
        access: Any,
        config: DelegatedToKdcubeConfig,
        store_for: Callable[[str], Any],
        authorize: Callable[..., Awaitable[Mapping[str, Any]]],
        client_secret_resolver: Callable[..., Any] | None = None,
        refresh_lock: RefreshLock | None = None,
        audit: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.access = access
        self.config = config
        self.store_for = store_for
        self.authorize = authorize
        self.client_secret_resolver = client_secret_resolver
        self.refresh_lock = refresh_lock
        self.audit = audit or (lambda event: None)

    def _refused(self, event: dict[str, Any], error: str, message: str, *, status: int = 409) -> dict[str, Any]:
        self.audit({**event, "outcome": "refused", "reason": error})
        return _refusal(error, message, status=status)

    async def issue(
        self,
        *,
        grantor_subject: str,
        access_id: str,
        project_ref: str,
        repository: str,
        request_id: str = "",
    ) -> dict[str, Any]:
        wanted = _repositories([repository])
        event = {
            "event": "project_github_token",
            "project_ref": as_str(project_ref),
            "repository": wanted[0] if wanted else as_str(repository)[:200],
            "access_id": as_str(access_id),
            "owner_subject": as_str(grantor_subject),
        }
        if not wanted or not as_str(project_ref):
            return self._refused(event, "github_repository_invalid", "Name the repository as owner/name.", status=400)
        provider = github_provider(self.config)
        if provider is None:
            return self._refused(event, "github_provider_not_configured", "GitHub is not set up in Connection Hub.", status=503)
        try:
            answer = await self.authorize(
                access_id=as_str(access_id),
                grantor_subject=as_str(grantor_subject),
                project_ref=as_str(project_ref),
                repository=wanted[0],
            )
        except Exception:  # noqa: BLE001 - the project host is an availability boundary
            return self._refused(
                event, "project_github_authorization_unavailable", "The project host could not be asked.", status=503
            )
        if answer.get("ok") is not True:
            error = as_str(answer.get("error")) or "project_github_authorization_unavailable"
            return self._refused(event, error, as_str(answer.get("message")) or "The project host refused the question.", status=503)
        decision = answer.get("decision") if isinstance(answer.get("decision"), Mapping) else {}
        if decision.get("allowed") is not True:
            reason = as_str(decision.get("reason")) or "github_access_refused"
            return self._refused(event, reason, "The project does not allow this agent GitHub access here.", status=403)
        if as_str(decision.get("owner_subject")) != as_str(grantor_subject):
            return self._refused(event, "grantor_mismatch", "The project names another owner for this agent.", status=403)

        settings = await self.access.project_person_my_card_settings_get(
            {"user_id": as_str(grantor_subject)}, project_ref=as_str(project_ref), request_id=request_id
        )
        if settings.get("ok") is not True:
            return self._refused(event, as_str(settings.get("error")) or "project_identity_my_card_missing", "Your owner has no My Card on this project.")
        properties = settings.get("properties") or {}
        link = properties.get(MY_CARD_GITHUB_PROPERTY)
        email = properties.get(MY_CARD_COMMIT_EMAIL_PROPERTY)
        if not isinstance(link, Mapping) or not as_str(link.get("account_id")):
            return self._refused(event, "github_not_linked", "Your owner has not connected GitHub on this project.")
        commit_email = as_str(email.get("email")) if isinstance(email, Mapping) else ""
        if not commit_email:
            return self._refused(event, "commit_email_not_set", "Your owner has not set a commit email on this project.")
        store = self.store_for(as_str(grantor_subject))
        account = await store.get_account(as_str(link.get("account_id")))
        if account is None:
            return self._refused(event, "github_not_linked", "Your owner's GitHub connection was removed; they connect it again.")
        broker = DelegatedToKdcubeBroker(
            config=self.config,
            store=store,
            client_secret_resolver=self.client_secret_resolver,
            refresh_lock=self.refresh_lock,
        )
        verdict = await broker.ensure_claim(
            provider_id=provider.provider_id,
            claim=_claim(provider),
            connector_app_id=account.connector_app_id or _connector_app_id(provider),
            account_id=account.account_id,
        )
        if not verdict.ok or verdict.credential is None:
            return self._refused(
                event, "github_reconnect_required", "Your owner's GitHub connection needs reconnecting in Connection Hub."
            )
        credential = verdict.credential.credential
        login = as_str(account.display_name)
        self.audit({**event, "outcome": "issued", "login": login})
        return {
            "ok": True,
            "project_ref": as_str(project_ref),
            "repository": wanted[0],
            "token": as_str(credential.get("access_token")),
            "expires_at": int(credential.get("expires_at") or 0),
            "login": login,
            "commit_email": commit_email,
        }


__all__ = [
    "AgentGitHubTokenIssuer",
    "CONNECTED",
    "NEEDS_RECONNECT",
    "NOT_CONNECTED",
    "ProjectGitHubKey",
    "github_provider",
]

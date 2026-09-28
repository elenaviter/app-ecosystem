# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""GitHub App user-token adapter registration for delegated to KDCube.

A person connects GitHub by authorizing the deployment's GitHub App. The App
acts for that person with a user access token (8 hours) and a refresh token
(6 months) that GitHub rotates on every refresh. What the token can reach is
the intersection of what the person can reach and what the App was installed
on, with the App's permissions: GitHub App authorization takes no scopes.

Besides the adapter, this module reads the person's App installations so a
caller can say, per repository, whether the App covers it (``covered``), is
installed on its owner but not on it (``missing``), is not installed on its
owner (``not_installed``), or could not be read (``unknown``), with the link
that installs it. Provider config (``adapter_config``): ``app_slug`` names
the App for install links.
"""

from __future__ import annotations

import time
from typing import Any, Mapping

import httpx

from connection_hub.delegated_to_kdcube.adapters import (
    DelegatedToKdcubeAdapter,
    adapter,
)
from connection_hub.delegated_to_kdcube.models import as_dict, as_str

GITHUB_WEB = "https://github.com"
GITHUB_API = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
PAGE_SIZE = 100
MAX_PAGES = 10

REPOSITORY_COVERED = "covered"
REPOSITORY_MISSING = "missing"
REPOSITORY_NOT_INSTALLED = "not_installed"
REPOSITORY_UNKNOWN = "unknown"


def _api_headers(access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }


async def _get_json(client: httpx.AsyncClient, url: str, *, access_token: str, params: Mapping[str, Any] | None = None) -> Any:
    try:
        response = await client.get(url, headers=_api_headers(access_token), params=dict(params or {}))
    except httpx.HTTPError as exc:
        raise RuntimeError(f"GitHub request failed: {exc}") from exc
    if response.status_code >= 400:
        raise RuntimeError(f"GitHub request failed: HTTP {response.status_code}")
    try:
        return response.json()
    except Exception as exc:
        raise RuntimeError("GitHub returned a response that is not JSON") from exc


async def _paged(client: httpx.AsyncClient, url: str, *, access_token: str, field: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        data = await _get_json(client, url, access_token=access_token, params={"per_page": PAGE_SIZE, "page": page})
        batch = [item for item in (as_dict(data).get(field) or []) if isinstance(item, Mapping)]
        items.extend(dict(item) for item in batch)
        if len(batch) < PAGE_SIZE:
            break
    return items


def install_url(app_slug: str, *, target_id: int | str | None = None) -> str:
    """The page that installs the App; with ``target_id`` it opens on that owner."""

    slug = as_str(app_slug)
    if not slug:
        return ""
    url = f"{GITHUB_WEB}/apps/{slug}/installations/new"
    target = as_str(target_id)
    return f"{url}/permissions?target_id={target}" if target else url


async def list_user_installations(access_token: str, *, client: httpx.AsyncClient | None = None) -> list[dict[str, Any]]:
    """The App's installations this person can reach: owner, selection and settings link."""

    async def _run(http: httpx.AsyncClient) -> list[dict[str, Any]]:
        raw = await _paged(http, f"{GITHUB_API}/user/installations", access_token=access_token, field="installations")
        out = []
        for item in raw:
            account = as_dict(item.get("account"))
            out.append(
                {
                    "installation_id": item.get("id"),
                    "owner": as_str(account.get("login")),
                    "owner_id": account.get("id"),
                    "owner_type": as_str(account.get("type")),
                    "repository_selection": as_str(item.get("repository_selection")),
                    "settings_url": as_str(item.get("html_url")),
                }
            )
        return out

    if client is not None:
        return await _run(client)
    async with httpx.AsyncClient(timeout=30.0) as http:
        return await _run(http)


async def list_installation_repositories(
    access_token: str, installation_id: int | str, *, client: httpx.AsyncClient | None = None
) -> list[str]:
    """``owner/name`` of each repository the installation covers and this person can reach."""

    async def _run(http: httpx.AsyncClient) -> list[str]:
        raw = await _paged(
            http,
            f"{GITHUB_API}/user/installations/{installation_id}/repositories",
            access_token=access_token,
            field="repositories",
        )
        return [as_str(item.get("full_name")) for item in raw if as_str(item.get("full_name"))]

    if client is not None:
        return await _run(client)
    async with httpx.AsyncClient(timeout=30.0) as http:
        return await _run(http)


def _split_repository(repository: str) -> tuple[str, str]:
    owner, _, name = as_str(repository).strip("/").partition("/")
    return owner, name


async def repository_coverage(
    access_token: str,
    repositories: list[str],
    *,
    app_slug: str = "",
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Per owner: installed, install link, and each repository's coverage state."""

    wanted: dict[str, list[str]] = {}
    for repository in repositories:
        owner, name = _split_repository(repository)
        if owner and name:
            wanted.setdefault(owner.lower(), []).append(f"{owner}/{name}")

    async def _run(http: httpx.AsyncClient) -> dict[str, Any]:
        try:
            installations = await list_user_installations(access_token, client=http)
        except RuntimeError as exc:
            return {
                "owners": [
                    {
                        "owner": _split_repository(repos[0])[0],
                        "installed": None,
                        "install_url": install_url(app_slug),
                        "repositories": [{"repository": repo, "state": REPOSITORY_UNKNOWN} for repo in repos],
                    }
                    for repos in wanted.values()
                ],
                "error": str(exc),
            }
        by_owner = {item["owner"].lower(): item for item in installations if item.get("owner")}
        owners = []
        for key, repos in wanted.items():
            installation = by_owner.get(key)
            owner = _split_repository(repos[0])[0]
            if installation is None:
                owners.append(
                    {
                        "owner": owner,
                        "installed": False,
                        "install_url": install_url(app_slug),
                        "repositories": [{"repository": repo, "state": REPOSITORY_NOT_INSTALLED} for repo in repos],
                    }
                )
                continue
            try:
                covered = {
                    name.lower()
                    for name in await list_installation_repositories(
                        access_token, installation["installation_id"], client=http
                    )
                }
                states = [
                    {"repository": repo, "state": REPOSITORY_COVERED if repo.lower() in covered else REPOSITORY_MISSING}
                    for repo in repos
                ]
            except RuntimeError:
                states = [{"repository": repo, "state": REPOSITORY_UNKNOWN} for repo in repos]
            owners.append(
                {
                    "owner": owner,
                    "installed": True,
                    # Adding a repository to an existing installation is done on its settings page.
                    "install_url": installation.get("settings_url")
                    or install_url(app_slug, target_id=installation.get("owner_id")),
                    "repositories": states,
                }
            )
        return {"owners": owners}

    if client is not None:
        return await _run(client)
    async with httpx.AsyncClient(timeout=30.0) as http:
        return await _run(http)


@adapter("github.app")
class GitHubAppAdapter(DelegatedToKdcubeAdapter):
    label = "GitHub"
    kind = "oauth2"
    authorize_url = f"{GITHUB_WEB}/login/oauth/authorize"
    token_url = f"{GITHUB_WEB}/login/oauth/access_token"

    def provider_scopes_for_claims(self, claims: list[str], claim_map: dict[str, Any]) -> list[str]:
        # A GitHub App's reach is its installation and permissions, not scopes.
        del claims, claim_map
        return []

    def token_request_headers(self) -> dict[str, str]:
        # Without Accept, GitHub answers form-encoded.
        return {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}

    def extract_token(self, raw: dict[str, Any]) -> dict[str, Any]:
        data = dict(raw or {})
        # GitHub answers a refused exchange or refresh with 200 and an error field.
        if data.get("error"):
            raise RuntimeError(f"GitHub OAuth error: {as_str(data.get('error'))}")
        if "refresh_token_expires_in" in data and "refresh_token_expires_at" not in data:
            try:
                data["refresh_token_expires_at"] = int(time.time()) + int(data["refresh_token_expires_in"])
            except Exception:
                pass
        return data

    def credential_refreshable(self, credential: dict[str, Any]) -> bool:
        if not super().credential_refreshable(credential):
            return False
        try:
            refresh_expires_at = int(credential.get("refresh_token_expires_at") or 0)
        except Exception:
            refresh_expires_at = 0
        return not refresh_expires_at or refresh_expires_at > int(time.time())

    async def fetch_profile(self, *, access_token: str, token: dict[str, Any] | None = None) -> dict[str, Any]:
        del token
        async with httpx.AsyncClient(timeout=30.0) as client:
            data = as_dict(await _get_json(client, f"{GITHUB_API}/user", access_token=access_token))
        login = as_str(data.get("login"))
        return {
            "external_subject": as_str(data.get("id")),
            "email": as_str(data.get("email")),
            "display_name": login or as_str(data.get("name")) or "GitHub account",
            "login": login,
            "workspace": "",
        }

    async def normalize_profile(self, credential: dict[str, Any]) -> dict[str, Any]:
        return {
            "external_subject": as_str(credential.get("external_subject") or credential.get("user_id")),
            "email": as_str(credential.get("email")),
            "display_name": as_str(credential.get("login")),
            "workspace": "",
        }


def app_slug_for(provider: Any) -> str:
    """The App slug from the provider's ``adapter_config``."""

    return as_str(as_dict(getattr(provider, "adapter_config", None)).get("app_slug"))


__all__ = [
    "GitHubAppAdapter",
    "REPOSITORY_COVERED",
    "REPOSITORY_MISSING",
    "REPOSITORY_NOT_INSTALLED",
    "REPOSITORY_UNKNOWN",
    "app_slug_for",
    "install_url",
    "list_installation_repositories",
    "list_user_installations",
    "repository_coverage",
]

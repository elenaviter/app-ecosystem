"""The agent's GitHub access through its owner's GitHub key (W371).

An agent pushes, pulls and opens pull requests as its owner: Connection Hub
holds the owner's GitHub connection (the deployment's GitHub App) and, for
an agent that attends the project, issues the owner's short-lived user token
for one repository on the project card. Nothing here stores it:

- ``pb worker git-credential`` is git's credential helper for github.com.
  git asks it per operation; it answers ``x-access-token`` and the token and
  forgets it. The clone's repository-local config names it
  (``credential.https://github.com.helper``, with ``useHttpPath`` so git
  says which repository).
- ``pb worker gh -- <args>`` runs gh with ``GH_TOKEN`` for that one command.

The token is asked of Connection Hub's ``project_agent_github_token_issue``
with this session's own Card bearer, resolved from the native store in this
process and never printed. Connection Hub asks the board whether the agent
attends and the repository is on the card; this side also refuses a host
other than github.com or a repository not on the card it holds.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Mapping
from urllib.parse import urlsplit

CONNECTION_HUB_BUNDLE_ID = "connection-hub@1-0"
ISSUE_OPERATION = "project_agent_github_token_issue"
# The Card bearer rides its own header, never Authorization: the platform
# checks every application operation called with a delegated Authorization
# bearer against the Card's selected operations, and no Card can select this
# route; Connection Hub authenticates the Card for identity itself (the first
# real push, 2026-09-28).
CARD_BEARER_HEADER = "X-Connection-Hub-Card-Bearer"
GITHUB_HOST = "github.com"
GIT_USERNAME = "x-access-token"
_REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")

Post = Callable[[str, Mapping[str, Any], str], Awaitable[tuple[int, Any]]]


# Where gh is installed when the process PATH does not say: a relay service
# (launchd, systemd) runs with /usr/bin:/bin:/usr/sbin:/sbin, and Homebrew puts
# gh in /opt/homebrew/bin (W371 line 5, 2026-09-28 17:18Z).
GH_LOCATIONS = (
    "/opt/homebrew/bin/gh",
    "/usr/local/bin/gh",
    "~/.local/bin/gh",
    "/home/linuxbrew/.linuxbrew/bin/gh",
    "/usr/bin/gh",
    "/snap/bin/gh",
)


def find_gh(*, path: str | None = None, locations: tuple[str, ...] = GH_LOCATIONS) -> str:
    """The absolute path of gh: PATH first, then the usual install locations; empty when none."""

    found = shutil.which("gh", path=os.environ.get("PATH", "") if path is None else path)
    if found:
        return found
    for location in locations:
        candidate = os.path.expanduser(location)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return ""


class GitHubKeyRefused(Exception):
    """A named refusal: no token, and the reason to show.

    ``availability`` marks a failure to reach an answer (Connection Hub down,
    a 5xx, a timeout) apart from a refusal (not_attending, card_denies,
    github_not_linked...): only the first may fall back to the deploy key.
    """

    def __init__(self, code: str, message: str, *, availability: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.availability = availability


# The remote connect-project keeps for this machine's deploy key once origin
# goes over HTTPS: `pb worker push` falls back to it when the key is unavailable.
DEPLOY_KEY_REMOTE = "deploykey"
_AVAILABILITY_STATUSES = frozenset({408, 429})


def classify_failure(exc: BaseException) -> tuple[str, bool]:
    """(code, availability) of a failed issue: only availability may fall back."""

    if isinstance(exc, GitHubKeyRefused):
        return exc.code, exc.availability
    try:
        from connection_hub.caller.errors import CredentialError, UpstreamError
    except ImportError:  # pragma: no cover - the caller package ships with pb
        CredentialError = UpstreamError = ()  # type: ignore[assignment,misc]
    if UpstreamError and isinstance(exc, UpstreamError):
        return str(getattr(exc, "code", "") or "connection_hub_unreachable"), True
    if CredentialError and isinstance(exc, CredentialError):
        return str(getattr(exc, "code", "") or "card_credential_refused"), False
    if isinstance(exc, (TimeoutError, ConnectionError)) or type(exc).__module__.startswith("aiohttp"):
        return "connection_hub_unreachable", True
    return str(getattr(exc, "code", "") or type(exc).__name__), False


@dataclass(frozen=True)
class GitHubToken:
    token: str
    expires_at: int
    login: str
    commit_email: str
    repository: str

    def __repr__(self) -> str:  # never the token
        return f"GitHubToken(repository={self.repository!r}, login={self.login!r}, expires_at={self.expires_at})"


def repository_name(value: str) -> str:
    """``owner/name`` from owner/name, a github.com URL or an SSH remote; empty otherwise."""

    text = str(value or "").strip()
    if text.startswith("git@"):
        host, _, path = text[len("git@"):].partition(":")
        if host.lower() != GITHUB_HOST:
            return ""
        text = path
    elif "://" in text:
        parts = urlsplit(text)
        if (parts.hostname or "").lower() != GITHUB_HOST:
            return ""
        text = parts.path
    elif text.startswith(("/", ".", "~")):
        # A local path, never a GitHub repository.
        return ""
    text = text.strip("/")
    if text.lower().endswith(".git"):
        text = text[:-4]
    return text if _REPOSITORY.fullmatch(text) else ""


def card_repositories(rows: Iterable[Mapping[str, Any]]) -> set[str]:
    """The github.com repositories on the project card, lower-cased for comparison."""

    names = set()
    for row in rows or ():
        name = repository_name(str(row.get("url") or ""))
        if name:
            names.add(name.lower())
    return names


def issue_url(endpoint: str, tenant: str, platform_project: str, bundle_id: str = CONNECTION_HUB_BUNDLE_ID) -> str:
    """Connection Hub's issue route on the platform the board endpoint names."""

    parts = urlsplit(endpoint)
    return (
        f"{parts.scheme}://{parts.netloc}/api/integrations/bundles/"
        f"{tenant}/{platform_project}/{bundle_id}/public/{ISSUE_OPERATION}"
    )


async def issue_token(*, post: Post, url: str, bearer: str, project_ref: str, repository: str) -> GitHubToken:
    """Ask Connection Hub for the owner's token for one repository; refusals are named."""

    status, body = await post(url, {"data": {"project_ref": project_ref, "repository": repository}}, bearer)
    result = body.get(ISSUE_OPERATION, body) if isinstance(body, Mapping) else {}
    if not isinstance(result, Mapping):
        result = {}
    if status == 404:
        raise GitHubKeyRefused(
            "github_key_route_missing",
            "Connection Hub on this platform has no GitHub key route yet; it needs the W371 release.",
        )
    if status in (401, 403) and not result.get("error"):
        raise GitHubKeyRefused("github_key_card_refused", "Connection Hub refused this agent's Card.")
    if status >= 500 or status in _AVAILABILITY_STATUSES:
        raise GitHubKeyRefused(
            f"http_{status}", f"Connection Hub did not answer (HTTP {status}).", availability=True
        )
    if result.get("ok") is not True or not str(result.get("token") or ""):
        code = str(result.get("error") or f"http_{status}")
        # Connection Hub names its own availability failures (a store, the
        # project host or the provider unreachable) with an "unavailable" code.
        unavailable = code.endswith("_unavailable") or int(result.get("status") or 0) == 503
        raise GitHubKeyRefused(code, str(result.get("message") or code), availability=unavailable)
    return GitHubToken(
        token=str(result["token"]),
        expires_at=int(result.get("expires_at") or 0),
        login=str(result.get("login") or ""),
        commit_email=str(result.get("commit_email") or ""),
        repository=str(result.get("repository") or repository),
    )


# -- git's credential helper protocol ----------------------------------------


def parse_credential_request(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in str(text or "").splitlines():
        if not line.strip():
            break
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value
    return fields


def credential_repository(fields: Mapping[str, str], on_card: set[str]) -> str:
    """The repository git asks for, when this helper may answer it; else a refusal."""

    if fields.get("protocol") != "https" or (fields.get("host") or "").lower() != GITHUB_HOST:
        raise GitHubKeyRefused("github_key_host_refused", "The GitHub key answers only https://github.com.")
    repository = repository_name(fields.get("path") or "")
    if not repository:
        raise GitHubKeyRefused(
            "github_key_path_missing",
            "git did not name the repository; set credential.useHttpPath true for github.com in this clone.",
        )
    if repository.lower() not in on_card:
        raise GitHubKeyRefused(
            "repository_not_on_card", f"{repository} is not on the project card; the GitHub key serves only those."
        )
    return repository


def credential_answer(token: GitHubToken) -> str:
    lines = [f"username={GIT_USERNAME}", f"password={token.token}"]
    if token.expires_at:
        lines.append(f"password_expiry_utc={token.expires_at}")
    return "\n".join(lines) + "\n"


def helper_command(runtime_kind: str, runtime_session_id: str) -> str:
    """The helper line a clone's config names: this session's own pb, no secret."""

    return "!pb worker git-credential " + " ".join(
        shlex.quote(part)
        for part in ("--runtime-kind", runtime_kind, "--runtime-session-id", runtime_session_id)
    )


def clone_config(helper: str, *, name: str = "", email: str = "") -> list[list[str]]:
    """``git config`` argument lists that make a clone use the GitHub key.

    The empty helper first clears any helper inherited from global config
    (a keychain would otherwise answer with another account) for github.com.
    """

    steps = [
        ["config", "--replace-all", "credential.https://github.com.helper", ""],
        ["config", "--add", "credential.https://github.com.helper", helper],
        ["config", "credential.https://github.com.useHttpPath", "true"],
    ]
    if name:
        steps.append(["config", "user.name", name])
    if email:
        steps.append(["config", "user.email", email])
    return steps


def push_through_deploy_key(args: list[str], remotes: set[str]) -> list[str]:
    """The same `git push` arguments aimed at the deploy-key remote.

    The first positional argument that names a remote is replaced; without
    one (a bare `git push`, or only a refspec), the deploy-key remote is put
    first.
    """

    out = list(args)
    for index, arg in enumerate(out):
        if not arg.startswith("-") and arg in remotes:
            out[index] = DEPLOY_KEY_REMOTE
            return out
    positional = next((index for index, arg in enumerate(out) if not arg.startswith("-")), len(out))
    return [*out[:positional], DEPLOY_KEY_REMOTE, *out[positional:]]


def gh_repository(args: list[str], origin_url: str) -> str:
    """The repository a gh command acts on: its -R/--repo, else the clone's origin."""

    for index, arg in enumerate(args):
        if arg in ("-R", "--repo") and index + 1 < len(args):
            return repository_name(args[index + 1])
        if arg.startswith("--repo="):
            return repository_name(arg.split("=", 1)[1])
    return repository_name(origin_url)


__all__ = [
    "CARD_BEARER_HEADER",
    "DEPLOY_KEY_REMOTE",
    "classify_failure",
    "push_through_deploy_key",
    "GH_LOCATIONS",
    "find_gh",
    "CONNECTION_HUB_BUNDLE_ID",
    "GIT_USERNAME",
    "GitHubKeyRefused",
    "GitHubToken",
    "ISSUE_OPERATION",
    "card_repositories",
    "clone_config",
    "credential_answer",
    "credential_repository",
    "gh_repository",
    "helper_command",
    "issue_token",
    "issue_url",
    "parse_credential_request",
    "repository_name",
]

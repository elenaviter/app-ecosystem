"""The agent's own account of its project workspace (W337).

The board's project card shows, for each attending agent, whether each
repository the project lists is set up on its host. The relay knows which
list it received; only the agent knows what it cloned. ``pb worker
workspace-report`` inspects ``<workspace>/<alias>`` for every repository of
the local project record and names one state each:

- ``verified``: a git checkout whose origin is the listed URL, and the remote
  answered ``git ls-remote`` just now;
- ``cloned``: such a checkout, not checked against the remote (``--no-verify``);
- ``unreachable``: missing, not a checkout, another origin, or the remote did
  not answer, with a one-line reason.

When the project sets a commit email (W368), each clone also says whether it
commits as ``<this agent's alias> <that email>``: ``matches``, ``differs`` or
``unset``. ``--set-identity`` first writes both values repository-local in each
clone of its own; the clone's worktrees inherit them.

The report is stored in the worker's local record and rides the project
heartbeat once per change, like the info line (W330).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

REPORT_STATES = ("verified", "cloned", "unreachable")
MAX_REASON_CHARS = 200
MAX_REPOSITORIES = 32

GitRunner = Callable[[Sequence[str], Path, float], "subprocess.CompletedProcess[str]"]
# An SSH host alias to its real host name (review on #168: deploy-key hosts
# clone from aliases like ``github-applications:owner/name.git``).
HostResolver = Callable[[str], str]


def _run_git(args: Sequence[str], cwd: Path, timeout: float) -> "subprocess.CompletedProcess[str]":
    env = dict(os.environ)
    # Never wait on a prompt: an agent's report must end on its own. BatchMode
    # is added even to an ssh command the host already set.
    env["GIT_TERMINAL_PROMPT"] = "0"
    ssh = str(env.get("GIT_SSH_COMMAND") or "ssh").strip()
    if "BatchMode" not in ssh:
        ssh = f"{ssh} -o BatchMode=yes"
    env["GIT_SSH_COMMAND"] = ssh
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def ssh_hostname(alias: str, *, timeout: float = 5.0) -> str:
    """The host an SSH alias names, from ``ssh -G`` (the host's own ssh config), or the alias itself."""

    # An alias is a host name, never an option (review on #168).
    if not alias or alias.startswith("-"):
        return alias
    try:
        result = subprocess.run(
            ["ssh", "-G", "--", alias],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return alias
    if result.returncode != 0:
        return alias
    for line in result.stdout.splitlines():
        key, _, value = line.strip().partition(" ")
        if key.lower() == "hostname" and value.strip():
            return value.strip()
    return alias


_SCP_FORM = re.compile(r"^(?:[\w.-]+@)?([^:/\s@]+):(?!//)(.+)$")
# The credentials end at the LAST "@" before the path: a password may contain
# "@" itself, and splitting at the first one left its tail in the host
# (review on #168).
_URL_FORM = re.compile(r"^([a-z+]+)://(?:[^/]*@)?([^/:@]+)(?::\d+)?/(.+)$", re.IGNORECASE)


def _remote_parts(url: str, resolve_host: HostResolver | None) -> tuple[str, str] | None:
    """(host, owner/name) of a remote URL, credentials dropped and SSH aliases resolved; None for a local path."""

    text = str(url or "").strip()
    match = _URL_FORM.match(text)
    if match:
        scheme, host, path = match.group(1).lower(), match.group(2), match.group(3)
        if scheme == "file":
            return None
        if scheme.startswith("ssh") and resolve_host is not None:
            host = resolve_host(host)
    else:
        match = _SCP_FORM.match(text)
        if not match:
            return None
        host, path = match.group(1), match.group(2)
        if resolve_host is not None:
            host = resolve_host(host)
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return host.lower(), path.lower()


def comparable_url(url: str, *, resolve_host: HostResolver | None = None) -> str:
    """One spelling for a repository URL: host/owner/name, lowercase, without .git or credentials."""

    parts = _remote_parts(url, resolve_host)
    if parts is None:
        return str(url or "").strip().rstrip("/")
    return f"{parts[0]}/{parts[1]}"


def _shown(url: str, resolve_host: HostResolver | None) -> str:
    """What a reason may name about a URL: host/owner/name only, never a local path or a credential."""

    parts = _remote_parts(url, resolve_host)
    return f"{parts[0]}/{parts[1]}" if parts else "a local path"


def inspect_repository(
    workspace: Path,
    repository: Mapping[str, Any],
    *,
    verify: bool = True,
    timeout: float = 15.0,
    git: GitRunner = _run_git,
    resolve_host: HostResolver | None = ssh_hostname,
) -> dict[str, str]:
    """One repository's state. Reasons are fixed texts: no local path, no raw origin, no git output (review on #168)."""

    alias = str(repository.get("alias") or "").strip()
    url = str(repository.get("url") or "").strip()
    folder = workspace / alias

    def unreachable(reason: str) -> dict[str, str]:
        return {"alias": alias, "state": "unreachable", "reason": reason[:MAX_REASON_CHARS]}

    if not folder.is_dir():
        return unreachable("not cloned: the workspace has no folder for this alias")
    try:
        inside = git(["rev-parse", "--is-inside-work-tree"], folder, timeout)
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return unreachable("the alias folder is not a git checkout")
        # A folder inside another checkout answers "true" too: it must be the
        # top of its own checkout (review on #168).
        top = git(["rev-parse", "--show-toplevel"], folder, timeout)
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != folder.resolve():
            return unreachable("the alias folder is inside another checkout, not a clone of its own")
        origin = git(["remote", "get-url", "origin"], folder, timeout)
        if origin.returncode != 0:
            return unreachable("the checkout has no origin remote")
        actual = origin.stdout.strip()
        if comparable_url(actual, resolve_host=resolve_host) != comparable_url(url, resolve_host=resolve_host):
            return unreachable(
                f"origin is {_shown(actual, resolve_host)}, the project lists {_shown(url, resolve_host)}"
            )
        if not verify:
            return {"alias": alias, "state": "cloned", "reason": ""}
        remote = git(["ls-remote", "--exit-code", "origin", "HEAD"], folder, timeout)
    except subprocess.TimeoutExpired:
        return unreachable(f"git did not answer within {int(timeout)}s")
    except OSError:
        return unreachable("git could not run on this host")
    if remote.returncode != 0:
        return unreachable(
            f"the remote did not answer (git exit {remote.returncode}): check this host's key or access"
        )
    return {"alias": alias, "state": "verified", "reason": ""}


IDENTITY_STATES = ("matches", "differs", "unset")


def commit_identity(
    alias: str, email: str, workspace: str, repositories: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """What this agent commits as in the project, and the commands that set it (W368).

    The name is always the agent's alias, the email the project's. Empty when
    the project sets no email or the agent has no alias: then nothing is set.
    """

    name = str(alias or "").strip()
    address = str(email or "").strip()
    if not name or not address:
        return {}
    root = Path(workspace) if workspace else Path("<workspace>")
    commands = []
    for repository in list(repositories)[:MAX_REPOSITORIES]:
        folder = str(repository.get("alias") or "").strip()
        if not folder:
            continue
        clone = shlex.quote(str(root / folder))
        commands.append(f"git -C {clone} config user.name {shlex.quote(name)}")
        commands.append(f"git -C {clone} config user.email {shlex.quote(address)}")
    return {
        "name": name,
        "email": address,
        "commands": commands,
        "rule": (
            "Every clone of the project commits as this name and email, repository-local; "
            "worktrees inherit the clone's. `pb worker workspace-report --set-identity` sets it in each clone."
        ),
    }


def clone_identity(
    folder: Path,
    name: str,
    email: str,
    *,
    set_identity: bool = False,
    timeout: float = 15.0,
    git: GitRunner = _run_git,
) -> str:
    """Whether the clone commits as ``name <email>``: matches, differs or unset.

    Reads what a commit here would use (repository, then user and system
    config), so a personal global identity that nothing overrides reads as
    ``differs``. With ``set_identity`` both values are written to the clone's
    own config first.
    """

    try:
        if set_identity:
            for key, value in (("user.name", name), ("user.email", email)):
                if git(["config", "--local", key, value], folder, timeout).returncode != 0:
                    return "unset"
        values = []
        for key in ("user.name", "user.email"):
            found = git(["config", "--get", key], folder, timeout)
            values.append(found.stdout.strip() if found.returncode == 0 else "")
    except (subprocess.TimeoutExpired, OSError):
        return "unset"
    if not all(values):
        return "unset"
    return "matches" if values == [name, email] else "differs"


def build_workspace_report(
    workspace: Path,
    repositories: Sequence[Mapping[str, Any]],
    *,
    revision: int,
    reported_at: str,
    verify: bool = True,
    timeout: float = 15.0,
    git: GitRunner = _run_git,
    resolve_host: HostResolver | None = None,
    identity: tuple[str, str] | None = None,
    set_identity: bool = False,
) -> dict[str, Any]:
    if resolve_host is None:
        cache: dict[str, str] = {}

        def resolve_host(alias: str) -> str:
            if alias not in cache:
                cache[alias] = ssh_hostname(alias, timeout=min(5.0, timeout))
            return cache[alias]

    rows = []
    for repository in list(repositories)[:MAX_REPOSITORIES]:
        alias = str(repository.get("alias") or "").strip()
        if not alias:
            continue
        row = inspect_repository(workspace, repository, verify=verify, timeout=timeout, git=git, resolve_host=resolve_host)
        # W368: only a clone of its own (not unreachable) has an identity to read or set.
        if identity and all(identity) and row["state"] != "unreachable":
            row["identity"] = clone_identity(
                workspace / alias, identity[0], identity[1], set_identity=set_identity, timeout=timeout, git=git
            )
        rows.append(row)
    return {"revision": int(revision), "reported_at": reported_at, "repositories": rows}


def report_signature(report: Mapping[str, Any]) -> str:
    """What makes two reports the same: the list revision and each state, not the time."""

    body = {
        "revision": int(report.get("revision") or 0),
        "repositories": [
            {
                "alias": str(row.get("alias") or ""),
                "state": str(row.get("state") or ""),
                "reason": str(row.get("reason") or ""),
                # W368: only when checked, so a report without it keeps its signature.
                **({"identity": str(row["identity"])} if row.get("identity") else {}),
            }
            for row in report.get("repositories") or []
            if isinstance(row, Mapping)
        ],
    }
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "IDENTITY_STATES",
    "REPORT_STATES",
    "build_workspace_report",
    "clone_identity",
    "commit_identity",
    "comparable_url",
    "inspect_repository",
    "report_signature",
    "ssh_hostname",
]

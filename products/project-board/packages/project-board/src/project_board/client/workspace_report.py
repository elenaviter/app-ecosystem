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

The report is stored in the worker's local record and rides the project
heartbeat once per change, like the info line (W330).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

REPORT_STATES = ("verified", "cloned", "unreachable")
MAX_REASON_CHARS = 200
MAX_REPOSITORIES = 32

GitRunner = Callable[[Sequence[str], Path, float], "subprocess.CompletedProcess[str]"]


def _run_git(args: Sequence[str], cwd: Path, timeout: float) -> "subprocess.CompletedProcess[str]":
    env = dict(os.environ)
    # Never wait on a prompt: an agent's report must end on its own.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def comparable_url(url: str) -> str:
    """One spelling for a repository URL: host/owner/name, lowercase, without .git."""

    text = str(url or "").strip()
    scp = re.match(r"^[\w.-]+@([^:/]+):(.+)$", text)
    if scp:
        host, path = scp.group(1), scp.group(2)
    else:
        match = re.match(r"^[a-z+]+://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+)$", text, re.IGNORECASE)
        if not match:
            return text.lower().rstrip("/")
        host, path = match.group(1), match.group(2)
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return f"{host.lower()}/{path.lower()}"


def _one_line(text: str) -> str:
    line = next((part.strip() for part in str(text or "").splitlines() if part.strip()), "")
    return line[:MAX_REASON_CHARS]


def inspect_repository(
    workspace: Path,
    repository: Mapping[str, Any],
    *,
    verify: bool = True,
    timeout: float = 15.0,
    git: GitRunner = _run_git,
) -> dict[str, str]:
    alias = str(repository.get("alias") or "").strip()
    url = str(repository.get("url") or "").strip()
    folder = workspace / alias

    def unreachable(reason: str) -> dict[str, str]:
        return {"alias": alias, "state": "unreachable", "reason": _one_line(reason)}

    if not folder.is_dir():
        return unreachable(f"not cloned: {folder} does not exist")
    try:
        inside = git(["rev-parse", "--is-inside-work-tree"], folder, timeout)
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return unreachable(f"{folder} is not a git checkout")
        origin = git(["remote", "get-url", "origin"], folder, timeout)
        if origin.returncode != 0:
            return unreachable(f"{folder} has no origin remote")
        actual = origin.stdout.strip()
        if comparable_url(actual) != comparable_url(url):
            return unreachable(f"origin is {actual}, the project lists {url}")
        if not verify:
            return {"alias": alias, "state": "cloned", "reason": ""}
        remote = git(["ls-remote", "--exit-code", "origin", "HEAD"], folder, timeout)
    except subprocess.TimeoutExpired:
        return unreachable(f"git did not answer within {int(timeout)}s")
    except OSError as exc:
        return unreachable(f"git could not run: {exc}")
    if remote.returncode != 0:
        return unreachable(f"remote did not answer: {_one_line(remote.stderr) or f'exit {remote.returncode}'}")
    return {"alias": alias, "state": "verified", "reason": ""}


def build_workspace_report(
    workspace: Path,
    repositories: Sequence[Mapping[str, Any]],
    *,
    revision: int,
    reported_at: str,
    verify: bool = True,
    timeout: float = 15.0,
    git: GitRunner = _run_git,
) -> dict[str, Any]:
    rows = [
        inspect_repository(workspace, repository, verify=verify, timeout=timeout, git=git)
        for repository in list(repositories)[:MAX_REPOSITORIES]
        if str(repository.get("alias") or "").strip()
    ]
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
            }
            for row in report.get("repositories") or []
            if isinstance(row, Mapping)
        ],
    }
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "REPORT_STATES",
    "build_workspace_report",
    "comparable_url",
    "inspect_repository",
    "report_signature",
]

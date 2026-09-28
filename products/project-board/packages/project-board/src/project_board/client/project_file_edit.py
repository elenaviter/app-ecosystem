"""Apply a project-file edit made on the board, in the coordinator's clone (W370).

Operator and coordinator, 2026-09-27: a person edits a project file on the
card; the board never writes it. The project's coordinator relay applies the
edit in its own repository clone and pushes the way the repository's
``file_edits`` policy says:

- ``direct``: commit onto the branch and push it (a local repository);
- ``pull_request``: push a ``file-edit/<id>`` branch and open a pull request
  with gh; without gh, the branch is pushed and the compare link returned.

The clean clone is never written: the edit is made in a worktree
``<workspace>/wt/file-edit-<id>`` from ``origin/<branch>``, removed afterwards
(project-workspace, section 6). A file that changed since the editor opened it
is refused, never merged. The commit is the coordinator's project identity and
names the person who asked.

Outcomes: ``committed``, ``pr_opened``, ``branch_pushed``, ``unchanged`` and
``refused`` (with a reason code and a line).
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .project_connect import github_repository, local_path

FILE_EDIT_KIND = "project.file.edit"
FILE_EDIT_POLICIES = ("direct", "pull_request")
GH_SERVICE_FIX = (
    "The relay service cannot read the login keychain, so the branch is pushed and "
    "the coordinator opens the pull request (add-a-worker-host step 7)."
)
# Step one carries the content inline in a control (capped at 64 KB): the
# board and the relay both refuse more than 60 KB of JSON-escaped content.
FILE_EDIT_MAX_BYTES = 60 * 1024
_EDIT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# (argv, cwd, timeout) and, with the owner's GitHub key, extra_env=.
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def _run(
    argv: Sequence[str], cwd: Path | None, timeout: float, extra_env: Mapping[str, str] | None = None
) -> "subprocess.CompletedProcess[str]":
    env = dict(os.environ)
    env.update(extra_env or {})
    env["GIT_TERMINAL_PROMPT"] = "0"
    ssh = str(env.get("GIT_SSH_COMMAND") or "ssh").strip()
    if "BatchMode" not in ssh:
        ssh = f"{ssh} -o BatchMode=yes"
    if "-T" not in ssh.split():
        ssh = f"{ssh} -T"
    env["GIT_SSH_COMMAND"] = ssh
    return subprocess.run(
        list(argv),
        cwd=str(cwd) if cwd else None,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def github_key_env(token: str) -> dict[str, str]:
    """The owner's GitHub key for one git or gh process (W371), in its environment only.

    git reads GIT_CONFIG_* as command-line config that no other process sees
    and no file keeps; the header is sent to https://github.com only. gh reads
    GH_TOKEN.
    """

    basic = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
        "GH_TOKEN": token,
    }


def default_policy(url: str) -> str:
    """A local repository takes a direct commit; a remote one a pull request (coordinator, 2026-09-27)."""

    return "direct" if local_path(url) else "pull_request"


def commit_message(path: str, requested_by: str) -> str:
    return f"Project file {path}: edited by {requested_by} on the board"


def _first_line(result: "subprocess.CompletedProcess[str]") -> str:
    text = (result.stderr or result.stdout or "").strip().splitlines()
    return text[0][:200] if text else f"exit {result.returncode}"


def apply_file_edit(
    *,
    workspace: str,
    alias: str,
    path: str,
    url: str,
    branch: str,
    base_commit: str,
    content: str,
    author_name: str,
    author_email: str,
    requested_by: str,
    edit_id: str,
    policy: str = "",
    timeout: float = 30.0,
    run: Runner | None = None,
    github_token: Any = None,
) -> dict[str, Any]:
    """Apply one edit; never raises for a git or gh failure, which is a ``refused`` outcome.

    ``github_token`` is the coordinator owner's GitHub key for this repository
    (W371): the branch is pushed over HTTPS with it and gh opens the pull
    request with it, and the commit carries the owner's My Card email.
    """

    run = run or _run
    policy = policy if policy in FILE_EDIT_POLICIES else default_policy(url)
    repo = github_repository(url)
    key_env = github_key_env(str(github_token.token)) if (github_token is not None and repo) else None
    if key_env is not None and str(getattr(github_token, "commit_email", "") or ""):
        author_email = str(github_token.commit_email)

    def refused(code: str, reason: str) -> dict[str, Any]:
        return {"outcome": "refused", "reason_code": code, "reason": reason, "policy": policy}

    def call(argv: Sequence[str], cwd: Path | None) -> "subprocess.CompletedProcess[str]":
        return run(argv, cwd, timeout, extra_env=key_env) if key_env is not None else run(argv, cwd, timeout)

    remote = f"https://github.com/{repo}.git" if key_env is not None else "origin"

    if not _EDIT_ID.match(edit_id):
        return refused("edit_invalid", "The edit id is not a plain name.")
    if not (author_name and author_email):
        return refused("commit_identity_unset", "The project sets no commit identity for the coordinator.")
    if len(json.dumps(content, ensure_ascii=True)) > FILE_EDIT_MAX_BYTES:
        return refused("file_too_large", f"An edit carries at most {FILE_EDIT_MAX_BYTES} bytes.")
    clone = Path(workspace) / alias
    if not workspace or not (clone / ".git").exists():
        return refused("repository_not_cloned", f"The coordinator has no clone of {alias}.")

    def git(*args: str, cwd: Path = clone) -> "subprocess.CompletedProcess[str]":
        return call(["git", *args], cwd)

    try:
        fetched = git("fetch", "--prune", "origin")
        if fetched.returncode != 0:
            return refused("fetch_failed", f"git fetch failed: {_first_line(fetched)}")
        if not branch:
            head = git("symbolic-ref", "--short", "refs/remotes/origin/HEAD").stdout.strip()
            branch = head[len("origin/"):] if head.startswith("origin/") else head
        target = f"origin/{branch}"
        if not branch or git("rev-parse", "--verify", "--quiet", f"{target}^{{commit}}").returncode != 0:
            return refused("branch_unknown", f"The clone has no {target}.")
        if git("cat-file", "-e", f"{base_commit}^{{commit}}").returncode != 0:
            return refused("changed_since_opened", "changed since you opened it; reopen")
        # The file as the editor saw it must be the file on the branch now.
        if git("diff", "--quiet", base_commit, target, "--", path).returncode != 0:
            return refused("changed_since_opened", "changed since you opened it; reopen")

        tree = Path(workspace) / "wt" / f"file-edit-{edit_id}"
        added = git("worktree", "add", "--detach", str(tree), target)
        if added.returncode != 0:
            return refused("worktree_failed", f"git worktree add failed: {_first_line(added)}")
        try:
            destination = (tree / path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            # A listed path stays inside its repository, links included.
            if tree.resolve() not in destination.resolve().parents:
                return refused("file_not_listed", "The path leaves its repository.")
            destination.write_text(content, encoding="utf-8")
            git("add", "--", path, cwd=tree)
            if git("diff", "--cached", "--quiet", cwd=tree).returncode == 0:
                return {"outcome": "unchanged", "reason": "nothing changed: the content equals the file", "policy": policy}
            message = commit_message(path, requested_by)
            committed = git(
                "-c", f"user.name={author_name}", "-c", f"user.email={author_email}",
                "commit", "-q", "-m", message, "-m", f"Requested-by: {requested_by}",
                cwd=tree,
            )
            if committed.returncode != 0:
                return refused("commit_failed", f"git commit failed: {_first_line(committed)}")
            commit = git("rev-parse", "HEAD", cwd=tree).stdout.strip()
            if policy == "direct":
                pushed = git("push", remote, f"HEAD:refs/heads/{branch}", cwd=tree)
                if pushed.returncode != 0:
                    return refused("push_refused", f"git push refused: {_first_line(pushed)}")
                return {"outcome": "committed", "commit": commit, "branch": branch, "policy": policy}
            edit_branch = f"file-edit/{edit_id}"
            pushed = git("push", remote, f"HEAD:refs/heads/{edit_branch}", cwd=tree)
            if pushed.returncode != 0:
                return refused("push_refused", f"git push refused: {_first_line(pushed)}")
            result: dict[str, Any] = {
                "outcome": "branch_pushed",
                "commit": commit,
                "branch": edit_branch,
                "base": branch,
                "policy": policy,
                **({"compare_url": f"https://github.com/{repo}/compare/{branch}...{edit_branch}"} if repo else {}),
            }
            if not repo:
                return result
            # A relay service (launchd, systemd) cannot open the login keychain
            # where an interactive `gh auth login` keeps its token: ask first,
            # so the result says why no pull request was opened (first use,
            # 2026-09-28).
            try:
                # With the owner's key gh needs no sign-in of its own (W371).
                signed_in = None if key_env is not None else run(["gh", "auth", "status", "--hostname", "github.com"], tree, timeout)
            except (OSError, subprocess.TimeoutExpired):
                return {**result, "reason_code": "gh_unavailable", "reason": "gh is not available on the coordinator's machine"}
            if signed_in is not None and signed_in.returncode != 0:
                return {
                    **result,
                    "reason_code": "gh_unavailable",
                    "reason": f"gh is not signed in for the relay service: {_first_line(signed_in)}. {GH_SERVICE_FIX}",
                }
            try:
                opened = call(
                    [
                        "gh", "pr", "create", "--repo", repo, "--base", branch, "--head", edit_branch,
                        "--title", message, "--body", f"Edited on the board by {requested_by}. Review and merge.",
                    ],
                    tree,
                )
            except (OSError, subprocess.TimeoutExpired):
                return {**result, "reason_code": "gh_unavailable", "reason": "gh is not available on the coordinator's machine"}
            if opened.returncode != 0:
                return {**result, "reason_code": "gh_unavailable", "reason": f"gh pr create failed: {_first_line(opened)}"}
            link = next((line.strip() for line in reversed(opened.stdout.splitlines()) if line.strip().startswith("https://")), "")
            return {**result, "outcome": "pr_opened", "pr_url": link}
        finally:
            git("worktree", "remove", "--force", str(tree))
            git("worktree", "prune")
    except subprocess.TimeoutExpired:
        return refused("git_timeout", f"git did not answer within {int(timeout)}s.")
    except OSError as exc:
        return refused("git_unavailable", f"git could not run: {exc.strerror or exc}")


__all__ = [
    "FILE_EDIT_KIND",
    "FILE_EDIT_MAX_BYTES",
    "FILE_EDIT_POLICIES",
    "apply_file_edit",
    "commit_message",
    "default_policy",
    "github_key_env",
]

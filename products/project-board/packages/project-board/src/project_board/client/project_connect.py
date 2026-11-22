"""Connect this machine to a project's repositories (W304 finding 19).

``pb worker connect-project`` is what an agent runs for Part 2 of connecting a
machine: once the operator has added it to a project, it sets up every
repository on the project card in ``<workspace>/<alias>``, one state each:

- ``reachable``: this machine reaches it, and it is cloned, or fetched and
  fast-forwarded, as project-workspace.md step 2 says;
- ``needs_key``: a GitHub repository the project owner's GitHub key (the
  account linked on the owner's Card) does not reach, with the key's answer.
  The operator links or grants that account; nothing else is tried (W942:
  "only github defined in operator card should be used", "this is prohibited
  to use on-host github"). No deploy key, SSH route or host gh login is used;
- ``unreachable``: not reachable and not on GitHub (a local path that is not
  on this machine reads "local to another machine"), or a key or SSH block
  that differs from what this command would make, with the reason;
- ``left_unchanged``: reachable, but the folder holds another repository,
  uncommitted work or a history that does not fast-forward. Nothing is forced.

Each row also says whether this agent can open pull requests there
(``pull_requests``, W304 finding 23): ``ready`` through ``pb worker gh`` when
the owner's key serves the repository, ``missing`` with the reason,
``not_applicable`` for a local or non-GitHub repository.

A GitHub clone is fetched and pushed over HTTPS with pb's credential helper
only; an existing SSH origin moves to HTTPS once the key serves it, and no
``deploykey`` remote is added (W942).

Every call takes list arguments, no shell, a timeout, and never waits on a
prompt. Clone and fetch move a whole repository, so they get their own limit
(``TRANSFER_TIMEOUT_SECONDS``), far above the short one for status checks: on
2026-09-28 a first clone of a large repository was cut off at 15 s and left
half checked out, with an empty index and a leftover index.lock. A git that
runs out of time is killed with every process it started; a clone cut off or
failed removes its partial folder, and an update leaves no lock of its own,
so the next run starts clean.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .github_key import clone_config
from .workspace_report import comparable_url

CONNECT_STATES = ("reachable", "needs_key", "unreachable", "left_unchanged")
# The workspace report's reason for a needs_key repository: the panel shows it
# under "not reachable yet" (agreed with the board panel, 2026-09-27; W942: the
# owner's key, never a deploy key).
NEEDS_KEY_REASON = "needs the project owner's GitHub key"
# A repository whose URL is a path that is not on this machine (a local-only
# project, whose repository lives on the operator's computer).
LOCAL_ELSEWHERE_REASON = "local to another machine"
PULL_REQUEST_FALLBACK = (
    "Ask the operator to link GitHub on the project owner's Card with access to the repository; "
    "until the owner's key serves it, nothing is pushed or opened there from this machine."
)

Runner = Callable[[Sequence[str], "Path | None", float, "Mapping[str, str] | None"], "subprocess.CompletedProcess[str]"]


# A first clone of a large repository takes minutes; status checks stay short.
TRANSFER_TIMEOUT_SECONDS = 1800.0


def _run(
    argv: Sequence[str], cwd: Path | None, timeout: float, env: Mapping[str, str] | None = None
) -> "subprocess.CompletedProcess[str]":
    """One command with a limit; past it, the command and everything it started are killed.

    git clone runs helpers (remote-https, index-pack) that outlive a killed
    parent and keep writing, so the whole process group goes.
    """

    process = subprocess.Popen(
        list(argv),
        cwd=str(cwd) if cwd else None,
        env=dict(env) if env is not None else None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
        process.communicate()
        raise
    return subprocess.CompletedProcess(list(argv), process.returncode, stdout, stderr)


_SCP_FORM = re.compile(r"^(?:[\w.-]+@)?([^:/\s@]+):(?!//)(.+)$")
_URL_FORM = re.compile(r"^([a-z+]+)://(?:[^/]*@)?([^/:@]+)(?::\d+)?/(.+)$", re.IGNORECASE)


def local_path(url: str) -> str:
    """The path of a local repository URL (a path or file://), or empty for a remote one."""

    text = str(url or "").strip()
    match = _URL_FORM.match(text)
    if match:
        return "/" + match.group(3).lstrip("/") if match.group(1).lower() == "file" else ""
    if text.startswith("file://"):
        return text[len("file://"):]
    return "" if _SCP_FORM.match(text) else text


def github_repository(url: str) -> str:
    """``owner/name`` of a github.com URL, as the card spells it; empty for any other remote."""

    text = str(url or "").strip()
    match = _URL_FORM.match(text) or _SCP_FORM.match(text)
    if not match:
        return ""
    host, path = (match.group(2), match.group(3)) if match.re is _URL_FORM else (match.group(1), match.group(2))
    if host.lower() != "github.com":
        return ""
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return path if path.count("/") == 1 else ""


@dataclass(frozen=True)
class Machine:
    """What the keys are named by and where they live: the host id, and this Linux user's SSH folder."""

    host_id: str
    ssh_dir: Path

    @property
    def ssh_config(self) -> Path:
        return self.ssh_dir / "config"


class Connector:
    """One connect run: the machine, the workspace, and the runner every command goes through."""

    def __init__(
        self,
        machine: Machine,
        workspace: Path,
        *,
        timeout: float = 15.0,
        transfer_timeout: float = TRANSFER_TIMEOUT_SECONDS,
        run: Runner | None = None,
        github_key: Callable[[str], Any] | None = None,
        helper: str = "",
        alias_name: str = "",
        project_ref: str = "",
    ) -> None:
        self.machine = machine
        self.workspace = workspace
        self.timeout = timeout
        self.transfer_timeout = max(float(transfer_timeout), float(timeout))
        self.run = run or _run
        # W371: the owner's GitHub key. github_key(repo) returns a token or
        # raises GitHubKeyRefused; helper is this session's credential-helper
        # line. Asked once per repository per run; the token is never kept.
        self.github_key = github_key
        self.helper = helper
        self.alias_name = alias_name
        self._key_state: dict[str, str] = {}
        self.commit_email = ""
        # W762: the Card's GitHub account, stamped with the project in each clone.
        self.account = ""
        self.project_ref = project_ref

    # -- the GitHub key (W371) -------------------------------------------

    def key_state(self, repo: str) -> str:
        """"ready" when the owner's GitHub key serves this repository, else why not."""

        if not repo:
            return ""
        if repo not in self._key_state:
            if self.github_key is None or not self.helper:
                self._key_state[repo] = "no GitHub key on this pb"
            else:
                try:
                    token = self.github_key(repo)
                except Exception as exc:  # noqa: BLE001 - GitHubKeyRefused and transport failures alike
                    self._key_state[repo] = str(getattr(exc, "message", "") or exc)
                else:
                    self._key_state[repo] = "ready"
                    self.commit_email = self.commit_email or str(getattr(token, "commit_email", "") or "")
                    self.account = self.account or str(getattr(token, "login", "") or "")
        return self._key_state[repo]

    def _key_config(self, folder: Path) -> None:
        for step in clone_config(
            self.helper,
            name=self.alias_name,
            email=self.commit_email,
            account=self.account,
            project_ref=self.project_ref,
        ):
            self._git(step, folder)

    def _key_flags(self) -> list[str]:
        return [
            "-c", "credential.https://github.com.helper=",
            "-c", f"credential.https://github.com.helper={self.helper}",
            "-c", "credential.https://github.com.useHttpPath=true",
        ]

    # -- ssh -------------------------------------------------------------

    def _ssh_env(self, through_alias: bool) -> dict[str, str]:
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        ssh = str(env.get("GIT_SSH_COMMAND") or "ssh").strip()
        # A github-<alias> URL is resolved by this SSH folder's config.
        if through_alias and "-F" not in ssh.split():
            ssh = f"{ssh} -F {self.machine.ssh_config}"
        if "BatchMode" not in ssh:
            ssh = f"{ssh} -o BatchMode=yes"
        # Never a terminal: a config that requests one prints "Pseudo-terminal
        # will not be allocated" for every repository (W304 finding 21).
        if "-T" not in ssh.split():
            ssh = f"{ssh} -T"
        env["GIT_SSH_COMMAND"] = ssh
        return env

    def _resolve(self, alias: str) -> tuple[str, list[str]]:
        """What ``github-<alias>`` resolves to in this SSH config: (hostname, identity files)."""

        name = f"github-{alias}"
        if not self.machine.ssh_config.is_file():
            return name, []
        try:
            result = self.run(["ssh", "-F", str(self.machine.ssh_config), "-G", "--", name], None, 5.0, None)
        except (OSError, subprocess.TimeoutExpired):
            return name, []
        host, files = name, []
        for line in result.stdout.splitlines() if result.returncode == 0 else []:
            key, _, value = line.strip().partition(" ")
            if key.lower() == "hostname" and host == name:
                host = value.strip()
            elif key.lower() == "identityfile":
                files.append(value.strip())
        return host, files

    def _canon(self, path: str) -> str:
        """One spelling of a path: ~/, $HOME/ and ${HOME}/ expanded, its folder resolved (ssh -G prints them unexpanded)."""

        home = str(Path.home())
        for prefix in ("~/", "$HOME/", "${HOME}/"):
            if path.startswith(prefix):
                path = f"{home}/{path[len(prefix):]}"
                break
        candidate = Path(path)
        try:
            return str(candidate.parent.resolve() / candidate.name)
        except OSError:
            return path

    # -- git -------------------------------------------------------------

    def _git(
        self, args: Sequence[str], cwd: Path, *, through_alias: bool = False, transfer: bool = False
    ) -> "subprocess.CompletedProcess[str]":
        limit = self.transfer_timeout if transfer else self.timeout
        return self.run(["git", *args], cwd, limit, self._ssh_env(through_alias))

    def _answers(self, url: str, *, through_alias: bool) -> bool:
        try:
            return self._git(["ls-remote", "--exit-code", url, "HEAD"], self.workspace, through_alias=through_alias).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    # -- pull requests -----------------------------------------------------

    def pull_requests(self, url: str) -> dict[str, str]:
        """Whether this agent can open a pull request on this repository: only through `pb worker gh`."""

        repo = github_repository(url)
        if not repo:
            return {"state": "not_applicable", "reason": ""}
        key = self.key_state(repo)
        if key == "ready":
            # The owner's key opens pull requests through `pb worker gh` (W371).
            return {"state": "ready", "reason": "", "via": "pb worker gh"}
        # W942: never the host's own gh login.
        return {"state": "missing", "reason": f"the project owner's GitHub key does not serve {repo}: {key}"}

    # -- one repository ----------------------------------------------------

    def connect(self, repository: Mapping[str, Any]) -> dict[str, Any]:
        alias = str(repository.get("alias") or "").strip()
        url = str(repository.get("url") or "").strip()
        branch = str(repository.get("branch") or "").strip()
        folder = self.workspace / alias
        repo = github_repository(url)

        def row(state: str, reason: str = "", **extra: Any) -> dict[str, Any]:
            return {"alias": alias, "url": url, "state": state, "reason": reason, **extra}

        path = local_path(url)
        if path and not Path(path).expanduser().exists():
            return row(
                "unreachable",
                f"{LOCAL_ELSEWHERE_REASON}: {path} is not on this machine.",
                report_reason=LOCAL_ELSEWHERE_REASON,
            )

        def resolve(host: str) -> str:
            return self._resolve(host[len("github-"):])[0] if host == f"github-{alias}" else host

        exists = (folder / ".git").exists()
        if folder.exists() and not exists:
            return row("left_unchanged", f"{folder} exists and is not a git checkout.")
        key = self.key_state(repo)
        if key:
            row = _with(row, github_key=key)
        if repo and key != "ready":
            # W942: a GitHub repository goes only through the owner's key. The operator sets that up;
            # no deploy key, SSH route or host login is tried.
            return row(
                "needs_key",
                f"the project owner's GitHub key does not serve {repo}: {key}. The operator links GitHub on the "
                "project owner's Card with access to this repository, then run `pb worker connect-project` again.",
                report_reason=NEEDS_KEY_REASON,
            )
        if key == "ready" and not exists:
            # W371: a new clone goes over HTTPS with the owner's GitHub key.
            https_url = f"https://github.com/{repo}.git"
            result = self._clone_or_update(
                folder,
                https_url,
                branch,
                exists=False,
                through_alias=False,
                row=row,
                flags=self._key_flags(),
                after_clone=self._key_config,
            )
            return {**result, "route": "github_key"} if result.get("state") == "reachable" else result
        origin = ""
        if exists:
            found = self._git(["remote", "get-url", "origin"], folder)
            origin = found.stdout.strip() if found.returncode == 0 else ""
            if comparable_url(origin, resolve_host=resolve) != comparable_url(url, resolve_host=resolve):
                return row("left_unchanged", f"{folder} has origin {origin or 'none'}, the project lists {url}.")
            source = origin
            if key == "ready":
                # W371 review: the helper answers https://github.com only, so an
                # SSH origin kept pushing with the deploy key. With the owner's key
                # ready, origin (fetch and push) goes over HTTPS; SSH stays only
                # where the key does not answer.
                return self._update_over_key(folder, origin, repo, branch, row)
        else:
            source = url
        # Only a repository that is not on GitHub gets here (a local path or another host).
        if not self._answers(source, through_alias=False):
            return row("unreachable", "The remote did not answer and is not on GitHub: ask the operator for access.")
        return self._clone_or_update(folder, source, branch, exists=exists, through_alias=False, row=row)

    def _update_over_key(
        self, folder: Path, origin: str, repo: str, branch: str, row: Callable[..., dict[str, Any]]
    ) -> dict[str, Any]:
        """An existing clone, moved to the owner's key: helper, HTTPS origin, then fetched as usual."""

        https_url = f"https://github.com/{repo}.git"
        self._key_config(folder)
        switched = origin != https_url
        if switched:
            # W942: no SSH route is kept aside; pushes go with the owner's key or not at all.
            self._git(["remote", "set-url", "origin", https_url], folder)
        # A push URL of its own would keep pushes on the old route.
        self._git(["config", "--unset-all", "remote.origin.pushurl"], folder)
        result = self._clone_or_update(folder, https_url, branch, exists=True, through_alias=False, row=row)
        if result.get("state") == "reachable":
            result = {**result, "route": "github_key"}
        return {**result, "origin_switched": "ssh_to_https"} if switched else result

    def _clone_or_update(
        self,
        folder: Path,
        source: str,
        branch: str,
        *,
        exists: bool,
        through_alias: bool,
        row: Callable[..., dict[str, Any]],
        flags: Sequence[str] = (),
        after_clone: Callable[[Path], None] | None = None,
    ) -> dict[str, Any]:
        lock = folder / ".git" / "index.lock"
        # A lock that was there before this run belongs to someone else; one
        # this run's killed git leaves behind is removed.
        lock_was_there = lock.exists()
        cloning = False
        try:
            if exists:
                dirty = self._git(["status", "--porcelain"], folder)
                if dirty.returncode != 0 or dirty.stdout.strip():
                    current = self._git(["branch", "--show-current"], folder).stdout.strip()
                    return row("left_unchanged", f"uncommitted changes on {current or 'a detached head'}; not updated.")
                fetched = self._git(["fetch", "--prune", "origin"], folder, through_alias=through_alias, transfer=True)
                if fetched.returncode != 0:
                    return row("unreachable", f"git fetch failed (exit {fetched.returncode}).")
                action = "updated"
            else:
                cloning = True
                cloned = self._git(
                    [*flags, "clone", "--quiet", source, str(folder)],
                    self.workspace,
                    through_alias=through_alias,
                    transfer=True,
                )
                if cloned.returncode != 0:
                    removed = self._remove_partial_clone(folder)
                    return row("unreachable", f"git clone failed (exit {cloned.returncode}).{removed}")
                cloning = False
                if after_clone is not None:
                    after_clone(folder)
                action = "cloned"
            if not branch:
                self._git(["remote", "set-head", "origin", "--auto"], folder, through_alias=through_alias)
                head = self._git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], folder).stdout.strip()
                branch = head[len("origin/"):] if head.startswith("origin/") else head
            if not branch:
                return row("left_unchanged", "the remote names no default branch.", action=action)
            for step in (["checkout", "--quiet", branch], ["merge", "--ff-only", "--quiet", f"origin/{branch}"]):
                if self._git(step, folder).returncode != 0:
                    return row(
                        "left_unchanged",
                        f"git {step[0]} of {branch} failed; never forced. Tell the coordinator.",
                        action=action,
                        branch=branch,
                    )
        except (OSError, subprocess.TimeoutExpired) as exc:
            limit = getattr(exc, "timeout", None) or (self.transfer_timeout if cloning else self.timeout)
            if cloning:
                removed = self._remove_partial_clone(folder)
                return row("unreachable", f"git clone did not finish within {int(limit)}s.{removed}")
            if not lock_was_there and lock.exists():
                lock.unlink(missing_ok=True)
            return row("unreachable", f"git did not answer within {int(limit)}s; nothing was left locked. Run again.")
        return row("reachable", "", action=action, branch=branch)

    def _remove_partial_clone(self, folder: Path) -> str:
        """A clone that did not finish leaves nothing behind, so the next run clones afresh."""

        try:
            inside = folder.resolve().is_relative_to(self.workspace.resolve()) and folder.resolve() != self.workspace.resolve()
        except OSError:
            inside = False
        if not inside or not folder.exists():
            return ""
        shutil.rmtree(folder, ignore_errors=True)
        return " The partial folder was removed; run again." if not folder.exists() else f" Remove {folder} by hand before running again."


def _with(row: Callable[..., dict[str, Any]], **fixed: Any) -> Callable[..., dict[str, Any]]:
    def build(state: str, reason: str = "", **extra: Any) -> dict[str, Any]:
        return row(state, reason, **fixed, **extra)

    return build


def connect_repositories(connector: Connector, repositories: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for repository in repositories:
        if not (str(repository.get("alias") or "").strip() and str(repository.get("url") or "").strip()):
            continue
        row = connector.connect(repository)
        row["pull_requests"] = connector.pull_requests(str(repository.get("url") or ""))
        rows.append(row)
    return rows


def next_step(rows: Sequence[Mapping[str, Any]]) -> str:
    """What the agent says and does next, from the states."""

    states = {str(row.get("state") or "") for row in rows}
    parts = []
    if "needs_key" in states:
        parts.append(
            "Tell the operator each repository the project owner's GitHub key does not serve, with its reason: "
            "they link GitHub on the project owner's Card with access to it. When they say it is done, "
            "run `pb worker connect-project` again."
        )
    if states & {"unreachable", "left_unchanged"}:
        parts.append("Tell the operator each repository that is unreachable or left unchanged, with its reason.")
    if not parts:
        parts.append("Every repository is reachable; the board shows it after the relay's next heartbeat.")
    if any((row.get("pull_requests") or {}).get("state") == "missing" for row in rows):
        parts.append(PULL_REQUEST_FALLBACK)
    return " ".join(parts)


__all__ = [
    "CONNECT_STATES",
    "Connector",
    "Machine",
    "LOCAL_ELSEWHERE_REASON",
    "NEEDS_KEY_REASON",
    "PULL_REQUEST_FALLBACK",
    "connect_repositories",
    "github_repository",
    "local_path",
    "next_step",
]

"""Connect this machine to a project's repositories (W304 finding 19).

``pb worker connect-project`` is what an agent runs for Part 2 of connecting a
machine: once the operator has added it to a project, it sets up every
repository on the project card in ``<workspace>/<alias>``, one state each:

- ``reachable``: this machine reaches it, and it is cloned, or fetched and
  fast-forwarded, as project-workspace.md step 2 says;
- ``needs_key``: a GitHub repository this machine does not reach yet. The
  machine's deploy key for it is made (or reused) with its ``github-<alias>``
  SSH block, exactly as add-a-worker-host step 7 makes them, and a grant is
  printed for the person to add on GitHub;
- ``unreachable``: not reachable and not on GitHub (a local path that is not
  on this machine reads "local to another machine"), or a key or SSH block
  that differs from what this command would make, with the reason;
- ``left_unchanged``: reachable, but the folder holds another repository,
  uncommitted work or a history that does not fast-forward. Nothing is forced.

Each row also says whether this agent can open pull requests there
(``pull_requests``, W304 finding 23): ``ready`` when gh is signed in with
write access, ``missing`` with the reason, ``not_applicable`` for a local or
non-GitHub repository. It never blocks the clone: deploy keys push branches,
and without gh the coordinator opens the pull requests.

A repository this machine already reaches, by any route, is not given a key.
A key and block another agent of this Linux user made are reused; one that
differs is named with its path and never overwritten. Revoking keys stays in
add-a-worker-host step 7, which sees every project of the host.

Every call takes list arguments, no shell, a timeout, and never waits on a
prompt.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .github_key import clone_config
from .workspace_report import comparable_url

CONNECT_STATES = ("reachable", "needs_key", "unreachable", "left_unchanged")
# The workspace report's reason for a needs_key repository: the panel shows it
# under "not reachable yet" (agreed with the board panel, 2026-09-27).
NEEDS_KEY_REASON = "needs this machine's deploy key"
# A repository whose URL is a path that is not on this machine (a local-only
# project, whose repository lives on the operator's computer).
LOCAL_ELSEWHERE_REASON = "local to another machine"
PULL_REQUEST_FALLBACK = (
    "Ask the operator to sign in gh as add-a-worker-host step 7 says; until then, push your branch "
    "and ask the coordinator to open the pull request, with the branch, base and title."
)
WRITE_PERMISSIONS = frozenset({"WRITE", "MAINTAIN", "ADMIN"})
GRANT_STEPS = (
    "Open the page, choose Add deploy key, enter the title, paste the key, "
    "tick Allow write access, and choose Add key."
)

Runner = Callable[[Sequence[str], "Path | None", float, "Mapping[str, str] | None"], "subprocess.CompletedProcess[str]"]


def _run(
    argv: Sequence[str], cwd: Path | None, timeout: float, env: Mapping[str, str] | None = None
) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        list(argv),
        cwd=str(cwd) if cwd else None,
        env=dict(env) if env is not None else None,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


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

    def key(self, alias: str) -> Path:
        return self.ssh_dir / f"deploy_{alias}"

    def grant_title(self) -> str:
        return f"{self.host_id} agents"


class Connector:
    """One connect run: the machine, the workspace, and the runner every command goes through."""

    def __init__(
        self,
        machine: Machine,
        workspace: Path,
        *,
        timeout: float = 15.0,
        run: Runner | None = None,
        github_key: Callable[[str], Any] | None = None,
        helper: str = "",
        alias_name: str = "",
    ) -> None:
        self.machine = machine
        self.workspace = workspace
        self.timeout = timeout
        self.run = run or _run
        # gh auth status, asked once per run: "" when signed in, else why not.
        self._gh_signed_in: str | None = None
        # W371: the owner's GitHub key. github_key(repo) returns a token or
        # raises GitHubKeyRefused; helper is this session's credential-helper
        # line. Asked once per repository per run; the token is never kept.
        self.github_key = github_key
        self.helper = helper
        self.alias_name = alias_name
        self._key_state: dict[str, str] = {}
        self.commit_email = ""

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
        return self._key_state[repo]

    def _key_config(self, folder: Path) -> None:
        for step in clone_config(self.helper, name=self.alias_name, email=self.commit_email):
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

    def _git(self, args: Sequence[str], cwd: Path, *, through_alias: bool = False) -> "subprocess.CompletedProcess[str]":
        return self.run(["git", *args], cwd, self.timeout, self._ssh_env(through_alias))

    def _answers(self, url: str, *, through_alias: bool) -> bool:
        try:
            return self._git(["ls-remote", "--exit-code", url, "HEAD"], self.workspace, through_alias=through_alias).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    # -- the deploy key ----------------------------------------------------

    def ensure_key(self, alias: str, repo: str) -> tuple[str, str]:
        """Make or reuse this machine's deploy key and github-<alias> block: (public key, refusal)."""

        key = self.machine.key(alias)
        public = key.with_name(key.name + ".pub")
        comment = f"{self.machine.host_id} deploy key: {alias} {repo}"
        if public.is_file():
            # The repository a key was made for is the last word of its comment, as step 7 reads it.
            words = public.read_text(encoding="utf-8").split()
            recorded = words[-1] if len(words) > 2 and "/" in words[-1] else ""
            if recorded and recorded.lower() != repo.lower():
                return "", (
                    f"{public} was made for {recorded}, and the project lists {repo} under {alias}: "
                    "add-a-worker-host step 7 retires it. Left unchanged."
                )
        self.machine.ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not key.is_file():
            made = self.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(key)], None, self.timeout, None
            )
            if made.returncode != 0 or not key.is_file():
                return "", f"ssh-keygen could not make {key} (exit {made.returncode})."
        if not public.is_file():
            derived = self.run(["ssh-keygen", "-y", "-f", str(key)], None, self.timeout, None)
            if derived.returncode != 0:
                return "", f"ssh-keygen could not read {key} (exit {derived.returncode})."
            public.write_text(" ".join(derived.stdout.split()[:2]) + f" {comment}\n", encoding="utf-8")
        host, files = self._resolve(alias)
        config = self.machine.ssh_config
        if host == f"github-{alias}":
            existed = config.is_file()
            with config.open("a", encoding="utf-8") as handle:
                handle.write(
                    f"\n# problem-board deploy key\nHost github-{alias}\n  HostName github.com\n"
                    f"  User git\n  IdentityFile {key}\n  IdentitiesOnly yes\n"
                )
            if not existed:
                config.chmod(0o600)
        elif host != "github.com" or self._canon(str(key)) not in {self._canon(file) for file in files}:
            return "", f"github-{alias} in {config} resolves to {host} without {key}. Left unchanged."
        return " ".join(public.read_text(encoding="utf-8").split()), ""

    def grant(self, alias: str, repo: str, public_key: str) -> dict[str, str]:
        return {
            "alias": alias,
            "repository": repo,
            "page": f"https://github.com/{repo}/settings/keys",
            "title": self.machine.grant_title(),
            "allow_write_access": "yes",
            "key": public_key,
        }

    # -- pull requests -----------------------------------------------------

    def pull_requests(self, url: str) -> dict[str, str]:
        """Whether gh can open a pull request on this repository for this Linux user."""

        repo = github_repository(url)
        if not repo:
            return {"state": "not_applicable", "reason": ""}
        if self.key_state(repo) == "ready":
            # The owner's key opens pull requests through `pb worker gh` (W371).
            return {"state": "ready", "reason": "", "via": "pb worker gh"}
        if self._gh_signed_in is None:
            try:
                status = self.run(["gh", "auth", "status"], None, self.timeout, None)
                self._gh_signed_in = "" if status.returncode == 0 else "gh is not signed in on this machine"
            except FileNotFoundError:
                self._gh_signed_in = "gh is not installed on this machine"
            except (OSError, subprocess.TimeoutExpired):
                self._gh_signed_in = "gh did not answer"
        if self._gh_signed_in:
            return {"state": "missing", "reason": self._gh_signed_in}
        try:
            viewed = self.run(
                ["gh", "repo", "view", repo, "--json", "viewerPermission", "-q", ".viewerPermission"],
                None,
                self.timeout,
                None,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {"state": "missing", "reason": "gh did not answer"}
        permission = viewed.stdout.strip().upper() if viewed.returncode == 0 else ""
        if permission in WRITE_PERMISSIONS:
            return {"state": "ready", "reason": ""}
        return {
            "state": "missing",
            "reason": f"the GitHub account gh is signed in with has {permission or 'no'} access to {repo}, not write",
        }

    # -- one repository ----------------------------------------------------

    def connect(self, repository: Mapping[str, Any]) -> dict[str, Any]:
        alias = str(repository.get("alias") or "").strip()
        url = str(repository.get("url") or "").strip()
        branch = str(repository.get("branch") or "").strip()
        folder = self.workspace / alias
        repo = github_repository(url)
        alias_url = f"github-{alias}:{repo}.git" if repo else ""

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
        else:
            # Project-workspace step 2: a new clone goes through github-<alias> when it resolves to github.com.
            source = alias_url if repo and self._resolve(alias)[0] == "github.com" else url
        through_alias = bool(alias_url) and source == alias_url
        if not self._answers(source, through_alias=through_alias):
            if not repo:
                return row("unreachable", "The remote did not answer and is not on GitHub: ask the operator for access.")
            public_key, refusal = self.ensure_key(alias, repo)
            if refusal:
                return row("unreachable", refusal)
            if through_alias or not self._answers(alias_url, through_alias=True):
                return row(
                    "needs_key", NEEDS_KEY_REASON, report_reason=NEEDS_KEY_REASON, grant=self.grant(alias, repo, public_key)
                )
            source, through_alias = alias_url, True
            if exists:
                # The same repository, through the key that reaches it.
                self._git(["remote", "set-url", "origin", alias_url], folder)
        if key == "ready" and exists:
            # An existing clone keeps its origin and gains the key for pushes and gh.
            self._key_config(folder)
        return self._clone_or_update(folder, source, branch, exists=exists, through_alias=through_alias, row=row)

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
        try:
            if exists:
                dirty = self._git(["status", "--porcelain"], folder)
                if dirty.returncode != 0 or dirty.stdout.strip():
                    current = self._git(["branch", "--show-current"], folder).stdout.strip()
                    return row("left_unchanged", f"uncommitted changes on {current or 'a detached head'}; not updated.")
                fetched = self._git(["fetch", "--prune", "origin"], folder, through_alias=through_alias)
                if fetched.returncode != 0:
                    return row("unreachable", f"git fetch failed (exit {fetched.returncode}).")
                action = "updated"
            else:
                cloned = self._git([*flags, "clone", "--quiet", source, str(folder)], self.workspace, through_alias=through_alias)
                if cloned.returncode != 0:
                    return row("unreachable", f"git clone failed (exit {cloned.returncode}).")
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
        except (OSError, subprocess.TimeoutExpired):
            return row("unreachable", f"git did not answer within {int(self.timeout)}s.")
        return row("reachable", "", action=action, branch=branch)


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
            "Show the person each grant: " + GRANT_STEPS + " When they say the keys are added, "
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
    "GRANT_STEPS",
    "Machine",
    "LOCAL_ELSEWHERE_REASON",
    "NEEDS_KEY_REASON",
    "PULL_REQUEST_FALLBACK",
    "connect_repositories",
    "github_repository",
    "local_path",
    "next_step",
]

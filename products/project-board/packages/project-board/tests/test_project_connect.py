"""Part 2 of connecting a machine: ``pb worker connect-project`` (W304 finding 19).

Once the operator adds an agent to a project, the agent sets up every
repository on the project card in one command. What this machine reaches is
cloned or fast-forwarded. A GitHub repository goes only through the project
owner's GitHub key over HTTPS; one the key does not serve is named with the
key's answer, and no deploy key, SSH route or host gh login is ever used (W942).
The workspace report follows, with the commit identity set.

github.com is played by local bare repositories behind a fake runner; ssh-keygen
and ``ssh -G`` are the real ones, on a temporary SSH folder.
"""

from __future__ import annotations

import asyncio
import functools
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from project_board.client import cli, project_connect, relay
from project_board.client.project_connect import (
    NEEDS_KEY_REASON,
    Connector,
    Machine,
    connect_repositories,
    github_repository,
    next_step,
)
from test_commit_identity import ALIAS, EMAIL, IdentityBoard, _config, _host
from test_workspace_report import _cli, _git, _heartbeats, _remote

pytestmark = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("git", "ssh", "ssh-keygen")), reason="needs git, ssh and ssh-keygen"
)

HOST_ID = "host-two"
_REAL_RUN = project_connect._run  # noqa: SLF001
_GITHUB_URL = re.compile(r"^(git@github\.com:|https://github\.com/|github-[\w.@-]+:)([\w.-]+/[\w.-]+?)(?:\.git)?$")


class GitHub:
    """github.com for the runner: its repositories, which answer without a key, and which keys are added."""

    def __init__(self, repositories: dict[str, Path]) -> None:
        self.repositories = repositories
        self.open: set[str] = set()
        self.granted: set[str] = set()
        # gh: None is not installed, else whether it is signed in, and the account's permission per repository.
        self.gh_signed_in: bool | None = None
        self.permissions: dict[str, str] = {}

    def answers(self, url: str) -> bool:
        match = _GITHUB_URL.match(url)
        if not match or match.group(2) not in self.repositories:
            return False
        through_alias = match.group(1).startswith("github-")
        return match.group(2) in (self.granted if through_alias else self.open)

    def __call__(self, argv, cwd, timeout, env):
        argv = list(argv)
        if argv[0] == "gh":
            if self.gh_signed_in is None:
                raise FileNotFoundError("gh")
            if argv[1:3] == ["auth", "status"]:
                return subprocess.CompletedProcess(argv, 0 if self.gh_signed_in else 1, "", "")
            permission = self.permissions.get(argv[3], "")
            return subprocess.CompletedProcess(argv, 0 if permission else 1, f"{permission}\n", "")
        if argv[0] != "git":
            return _REAL_RUN(argv, cwd, timeout, env)
        args = argv[1:]
        url = next((arg for arg in args if _GITHUB_URL.match(arg)), "")
        if not url and args[0] in {"fetch", "ls-remote", "remote"} and args[:2] != ["remote", "get-url"] and args[:2] != ["remote", "set-url"]:
            origin = _REAL_RUN(["git", "remote", "get-url", "origin"], cwd, timeout, env).stdout.strip()
            url = origin if _GITHUB_URL.match(origin) else ""
        if not url or args[:2] == ["remote", "set-url"]:
            return _REAL_RUN(argv, cwd, timeout, env)
        code = 0 if self.answers(url) else 128
        if code == 0 and args[0] == "clone":
            repo = _GITHUB_URL.match(url).group(2)
            done = _REAL_RUN(["git", "clone", "--quiet", str(self.repositories[repo]), args[-1]], cwd, timeout, env)
            _REAL_RUN(["git", "remote", "set-url", "origin", url], Path(args[-1]), timeout, env)
            return done
        # ls-remote, fetch and set-head only ask whether GitHub answers.
        return subprocess.CompletedProcess(argv, code, "", "" if code == 0 else "Permission denied (publickey).")


def _machine(tmp_path: Path) -> Machine:
    return Machine(host_id=HOST_ID, ssh_dir=tmp_path / "ssh")


def test_a_github_url_names_its_repository_and_any_other_remote_names_none():
    assert github_repository("git@github.com:KDCube/applications.git") == "KDCube/applications"
    assert github_repository("https://github.com/kdcube/kdcube") == "kdcube/kdcube"
    assert github_repository("ssh://git@github.com/example-org/app-ecosystem.git") == "example-org/app-ecosystem"
    assert github_repository("git@gitlab.com:kdcube/applications.git") == ""
    assert github_repository("/srv/git/applications.git") == ""


def test_what_this_machine_reaches_is_cloned_or_fast_forwarded_and_never_forced(tmp_path):
    remote = _remote(tmp_path / "remotes", "applications")
    other = _remote(tmp_path / "remotes", "other")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    connector = Connector(_machine(tmp_path), workspace)
    listed = [
        {"alias": "applications", "url": str(remote)},
        {"alias": "mixed-up", "url": str(remote)},
        {"alias": "gone", "url": str(tmp_path / "remotes" / "gone.git")},
    ]
    _git("clone", "-q", str(other), str(workspace / "mixed-up"), cwd=tmp_path)

    rows = {row["alias"]: row for row in connect_repositories(connector, listed)}
    assert rows["applications"]["state"] == "reachable" and rows["applications"]["action"] == "cloned"
    assert rows["applications"]["branch"] == "main"
    assert rows["mixed-up"]["state"] == "left_unchanged" and "the project lists" in rows["mixed-up"]["reason"]
    # A local path that is not here: no key can help, and none is made.
    assert rows["gone"]["state"] == "unreachable" and rows["gone"]["reason"].startswith("local to another machine")
    assert not (tmp_path / "ssh").exists()
    # Nor for a remote that is not on GitHub.
    refused = lambda argv, cwd, timeout, env: subprocess.CompletedProcess(argv, 128, "", "denied")  # noqa: E731
    elsewhere = Connector(_machine(tmp_path), workspace, run=refused).connect(
        {"alias": "elsewhere", "url": "git@gitlab.com:kdcube/elsewhere.git"}
    )
    assert elsewhere["state"] == "unreachable" and "not on GitHub" in elsewhere["reason"]
    assert not (tmp_path / "ssh").exists()

    # A new commit upstream is fast-forwarded on the next run.
    seed = tmp_path / "remotes" / "applications-seed"
    (seed / "NEW.md").write_text("new", encoding="utf-8")
    _git("add", "NEW.md", cwd=seed)
    _git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "new", cwd=seed)
    _git("push", "-q", str(remote), "main", cwd=seed)
    again = connector.connect(listed[0])
    assert again["state"] == "reachable" and again["action"] == "updated"
    assert (workspace / "applications" / "NEW.md").is_file()

    # Uncommitted work stops the update, on the branch it is on.
    (workspace / "applications" / "draft.txt").write_text("mine", encoding="utf-8")
    held = connector.connect(listed[0])
    assert held["state"] == "left_unchanged" and "uncommitted changes on main" in held["reason"]
    assert "needs_key" not in next_step([held]) and "left unchanged" in next_step([held])


def test_a_github_repository_without_the_owners_key_is_named_and_never_set_up_another_way(tmp_path):
    """W942 (operator, 2026-10-23): "only github defined in operator card should be used"."""
    github = KeyedGitHub({"kdcube/applications": _remote(tmp_path / "remotes", "applications")})
    github.open.add("kdcube/applications")  # this machine could reach it over SSH: that is never used
    github.granted.add("kdcube/applications")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    listed = {"alias": "applications", "url": "git@github.com:kdcube/applications.git", "role": "work"}
    issued: list[str] = []

    for connector in (
        Connector(_machine(tmp_path), workspace, run=github),
        Connector(_machine(tmp_path), workspace, run=github, github_key=_key(github, issued), helper=HELPER),
    ):
        row = connector.connect(listed)
        assert row["state"] == "needs_key" and row["report_reason"] == NEEDS_KEY_REASON
        assert "kdcube/applications" in row["reason"] and "project owner's Card" in row["reason"]
        assert "grant" not in row
    assert "Your owner has not connected GitHub on this project." in row["reason"]
    assert not (workspace / "applications").exists() and not (tmp_path / "ssh").exists()
    step = next_step([row])
    assert "project owner's Card" in step and "connect-project` again" in step and "deploy key" not in step

    # The operator links it: the next run clones over HTTPS with the key.
    github.keyed.add("kdcube/applications")
    done = Connector(_machine(tmp_path), workspace, run=github, github_key=_key(github, issued), helper=HELPER).connect(listed)
    assert done["state"] == "reachable" and done["action"] == "cloned" and done["route"] == "github_key"
    assert _GIT_ORIGIN(workspace / "applications") == "https://github.com/kdcube/applications.git"


def _GIT_ORIGIN(folder: Path) -> str:  # noqa: N802 - reads like a constant lookup in the asserts
    return subprocess.run(
        ["git", "remote", "get-url", "origin"], cwd=str(folder), capture_output=True, text=True, check=True
    ).stdout.strip()


def test_an_existing_ssh_clone_without_the_owners_key_is_not_fetched(tmp_path):
    """W942: no plain fetch over SSH either; the clone is left as it is and named."""
    github = KeyedGitHub({"kdcube/applications": _remote(tmp_path / "remotes", "applications")})
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    clone = workspace / "applications"
    _git("clone", "-q", str(github.repositories["kdcube/applications"]), str(clone), cwd=tmp_path)
    _git("remote", "set-url", "origin", "git@github.com:kdcube/applications.git", cwd=clone)
    seen: list[list[str]] = []

    def recording(argv, cwd, timeout, env):
        seen.append(list(argv))
        return github(argv, cwd, timeout, env)

    row = Connector(_machine(tmp_path), workspace, run=recording).connect(
        {"alias": "applications", "url": "git@github.com:kdcube/applications.git"}
    )
    assert row["state"] == "needs_key"
    assert not [argv for argv in seen if "fetch" in argv or "clone" in argv or "ls-remote" in argv]
    assert _GIT_ORIGIN(clone) == "git@github.com:kdcube/applications.git"


def test_an_existing_deploy_key_and_ssh_block_are_never_used_or_touched(tmp_path):
    github = KeyedGitHub({"kdcube/applications": _remote(tmp_path / "remotes", "applications")})
    github.granted.add("kdcube/applications")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    machine = _machine(tmp_path)
    machine.ssh_dir.mkdir()
    machine.ssh_config.write_text("Host github-applications\n  HostName github.com\n", encoding="utf-8")
    row = Connector(machine, workspace, run=github).connect(
        {"alias": "applications", "url": "git@github.com:kdcube/applications.git"}
    )
    assert row["state"] == "needs_key" and not (workspace / "applications").exists()
    assert machine.ssh_config.read_text(encoding="utf-8") == "Host github-applications\n  HostName github.com\n"
    assert sorted(path.name for path in machine.ssh_dir.iterdir()) == ["config"]


class GitHubBoard(IdentityBoard):
    """The project card lists a local journal remote and a GitHub work repository."""

    async def action(self, *, object_ref: str, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        answer = await super().action(object_ref=object_ref, action=action, payload=payload)
        if action == "worker.heartbeat" and (payload or {}).get("project_ref"):
            answer["object"]["assignment_project"]["repositories"] = [
                {"alias": "applications", "url": str(self.remote), "role": "journal", "path": "docs/journal"},
                {"alias": "kdcube", "url": "git@github.com:kdcube/kdcube.git", "role": "work"},
            ]
        return answer


def test_the_command_connects_sets_the_identity_and_reports_needs_key_to_the_board(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    identity, field, config, workspace = _host(tmp_path, monkeypatch)
    remote = _remote(tmp_path / "remotes", "applications")
    github = KeyedGitHub({"kdcube/kdcube": _remote(tmp_path / "remotes", "kdcube")})
    github.granted.add("kdcube/kdcube")  # a deploy key this machine has: never used (W942)
    monkeypatch.setattr(project_connect, "_run", github)

    class OwnerKey:
        """The owner's key as connect-project asks it: this session's helper, and a token per repository."""

        def __init__(self, args):
            self.token = _key(github, [])

        def helper(self):
            return HELPER

    monkeypatch.setattr(cli, "_GitHubKeySession", OwnerKey)
    ssh_dir = tmp_path / "ssh"
    report = cli.build_workspace_report
    monkeypatch.setattr(
        cli,
        "build_workspace_report",
        functools.partial(report, git=lambda args, cwd, timeout: github(["git", *args], cwd, timeout, None)),
    )
    board = GitHubBoard(identity.worker_name, remote)
    adapter = relay.ProblemBoardHostRelayAdapter(config=config, field=field, client=board)
    asyncio.run(adapter.poll_attendances_once())

    result = _cli(identity, "connect-project", "--ssh-dir", str(ssh_dir))
    assert [(row["alias"], row["state"]) for row in result["connected"]] == [
        ("applications", "reachable"),
        ("kdcube", "needs_key"),
    ]
    assert result["grants"] == [] and not ssh_dir.exists()
    assert "project owner's Card" in result["next"]
    assert result["commit_identity"]["name"] == ALIAS
    assert [(row["alias"], row["state"], row["reason"]) for row in result["workspace_report"]["repositories"]] == [
        ("applications", "verified", ""),
        ("kdcube", "unreachable", NEEDS_KEY_REASON),
    ]

    asyncio.run(adapter.poll_attendances_once())
    carried = _heartbeats(board)[-1]["workspace_report"]
    assert [row["reason"] for row in carried["repositories"]] == ["", NEEDS_KEY_REASON]

    # The operator links GitHub on the owner's Card; the same command, run again, clones it over HTTPS.
    github.keyed.add("kdcube/kdcube")
    again = _cli(identity, "connect-project", "--ssh-dir", str(ssh_dir))
    assert [row["state"] for row in again["connected"]] == ["reachable", "reachable"] and again["grants"] == []
    assert _GIT_ORIGIN(workspace / "kdcube") == "https://github.com/kdcube/kdcube.git"
    assert _config(workspace / "kdcube", "user.name") == ALIAS
    assert again["next"].startswith("Every repository is reachable")


def test_a_local_repository_is_cloned_when_it_is_here_and_named_when_it_is_on_another_machine(tmp_path):
    """Coordinator, 2026-09-27: a local-only project's repository is a path on the operator's computer."""

    source = tmp_path / "operator" / "journal"
    source.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=source)
    _git("config", "receive.denyCurrentBranch", "updateInstead", cwd=source)
    (source / "README.md").write_text("journal", encoding="utf-8")
    _git("add", "README.md", cwd=source)
    _git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "seed", cwd=source)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    connector = Connector(_machine(tmp_path), workspace)

    for url in (str(source), f"file://{source}"):
        alias = "journal" if url == str(source) else "journal-file"
        row = connector.connect({"alias": alias, "url": url, "role": "journal", "branch": "main"})
        assert row["state"] == "reachable" and row["action"] == "cloned" and "grant" not in row
    (source / "NEW.md").write_text("new", encoding="utf-8")
    _git("add", "NEW.md", cwd=source)
    _git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "new", cwd=source)
    again = connector.connect({"alias": "journal", "url": str(source), "role": "journal", "branch": "main"})
    assert again["state"] == "reachable" and again["action"] == "updated"
    assert (workspace / "journal" / "NEW.md").is_file()

    for url in ("/Users/someone-else/journal", "file:///Users/someone-else/journal"):
        away = connector.connect({"alias": "away", "url": url, "role": "journal"})
        assert away["state"] == "unreachable" and away["reason"].startswith("local to another machine: ")
        assert away["report_reason"] == "local to another machine" and "grant" not in away
    assert not (tmp_path / "ssh").exists()


def test_every_git_call_is_batch_without_a_terminal_and_none_uses_an_ssh_alias(tmp_path):
    """W304 finding 21: a config that requests a terminal must not print "Pseudo-terminal will not be allocated"."""

    github = KeyedGitHub({"kdcube/applications": _remote(tmp_path / "remotes", "applications")})
    github.keyed.add("kdcube/applications")
    seen: list[tuple[list[str], str]] = []

    def recording(argv, cwd, timeout, env):
        if argv[0] == "git":
            seen.append((list(argv), str((env or {}).get("GIT_SSH_COMMAND") or "")))
        return github(argv, cwd, timeout, env)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    connector = Connector(_machine(tmp_path), workspace, run=recording, github_key=_key(github, []), helper=HELPER)
    listed = {"alias": "applications", "url": "git@github.com:kdcube/applications.git"}
    assert connector.connect(listed)["action"] == "cloned"
    assert connector.connect(listed)["action"] == "updated"
    assert seen
    for argv, ssh in seen:
        assert "-T" in ssh.split() and "BatchMode=yes" in ssh
    assert not [argv for argv, _ssh in seen if any(arg.startswith(("github-", "git@github.com:")) for arg in argv)]


def test_pull_requests_are_ready_only_through_the_owners_key_never_the_hosts_gh(tmp_path):
    """W304 finding 23; W942: pull requests go through `pb worker gh` with the owner's key, never the host's gh."""

    github = KeyedGitHub({
        "kdcube/applications": _remote(tmp_path / "remotes", "applications"),
        "kdcube/kdcube": _remote(tmp_path / "remotes", "kdcube"),
    })
    github.keyed.add("kdcube/applications")
    github.gh_signed_in = True
    github.permissions = {"kdcube/applications": "WRITE", "kdcube/kdcube": "WRITE"}
    local = _remote(tmp_path / "remotes", "journal")
    ran: list[list[str]] = []

    def recording(argv, cwd, timeout, env):
        ran.append(list(argv))
        return github(argv, cwd, timeout, env)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    connector = Connector(_machine(tmp_path), workspace, run=recording, github_key=_key(github, []), helper=HELPER)
    assert connector.pull_requests("git@github.com:kdcube/applications.git") == {
        "state": "ready", "reason": "", "via": "pb worker gh"}
    missing = connector.pull_requests("https://github.com/kdcube/kdcube")
    assert missing["state"] == "missing" and "kdcube/kdcube" in missing["reason"]
    assert connector.pull_requests(str(local)) == {"state": "not_applicable", "reason": ""}
    assert not [argv for argv in ran if argv[0] == "gh"], "the host's gh is never asked"
    assert "project owner's Card" in next_step([{"state": "reachable", "pull_requests": {"state": "missing"}}])
    assert "Card" not in next_step([{"state": "reachable", "pull_requests": {"state": "ready"}}])


# The board panel's words (kdcube/applications services/connect_machine.py), agreed word for word.
PART_ONE_DONE = (
    "When your agent says it is enrolled and attends no project, this machine is connected. "
    "Continue with Part 2 to connect it to a project."
)
CONNECT_PROJECT_SENTENCE = "Use the problem-board-worker skill. Set up this project's repositories on this machine."
PART_TWO_TITLES = [
    "Pick the project",
    "Add the agent",
    "Say to your agent",
    "Add this machine's keys on GitHub",
    "Tell your agent the keys are added",
]
PART_TWO_NOTES = [
    "For each repository this machine cannot reach yet, your agent shows a page, a title and a key. "
    "Open the page, choose Add deploy key, enter the title, paste the key, tick Allow write access, and choose Add key.",
    "Your agent runs the setup again: it clones what it can now reach, and each repository shows as reachable here.",
]
WORKER_INTRO = (
    "At the machine, a terminal tab is enough: the agent runs while the tab stays open. "
    "Over ssh, use tmux, so the agent keeps running when the connection drops."
)
WORKER_TAB = (
    'mkdir -p "$HOME/.kdcube/pb/workspaces/$ALIAS" "$HOME/.claude-accounts/$ACCOUNT" && '
    'cd "$HOME/.kdcube/pb/workspaces/$ALIAS" && env CLAUDE_CONFIG_DIR="$HOME/.claude-accounts/$ACCOUNT" '
    'claude --add-dir "$HOME/.kdcube" --dangerously-skip-permissions --disallowedTools AskUserQuestion'
)


def test_readme_and_first_run_carry_the_panels_words_for_part_2_and_both_start_lines():
    from project_board.client.procedures import source_package_path

    package = source_package_path().parents[2].parent
    readme = " ".join((package / "README.md").read_text(encoding="utf-8").split())
    first_run = " ".join((source_package_path() / "references" / "first-run.md").read_text(encoding="utf-8").split())
    for text in (readme, first_run):
        for piece in (PART_ONE_DONE, CONNECT_PROJECT_SENTENCE, *PART_TWO_TITLES, *PART_TWO_NOTES, WORKER_INTRO, WORKER_TAB,
                      "At the machine: a terminal tab", "Over ssh: in tmux"):
            assert piece in text, piece
    assert "pb worker connect-project" in first_run and "local to another machine" in first_run
    # W304 finding 22: the second install comes before the bootstrap is deleted.
    assert "before the temporary bootstrap is deleted" in first_run and "claude_code_settings.changes" in first_run
    step7 = " ".join((source_package_path().parent / "add-a-worker-host.md").read_text(encoding="utf-8").split())
    assert "pb worker connect-project" in step7 and "Revoking stays with the script below" in step7
    assert "GRANT" not in step7, "W942: step 7 only revokes"


# --- W371, W942: the owner's GitHub key, and nothing else ---

from project_board.client.github_key import GitHubKeyRefused, GitHubToken, helper_command  # noqa: E402

HELPER = helper_command("claude-code", "session-1")
OWNER_EMAIL = "owner@example.test"


class KeyedGitHub(GitHub):
    """github.com over HTTPS: a repository the owner's key serves answers when git carries the pb helper."""

    def __init__(self, repositories: dict[str, Path]) -> None:
        super().__init__(repositories)
        self.keyed: set[str] = set()
        self.clone_flags: list[list[str]] = []

    def answers(self, url: str) -> bool:
        match = _GITHUB_URL.match(url)
        if match and match.group(1) == "https://github.com/" and match.group(2) in self.keyed:
            return True
        return super().answers(url)

    def __call__(self, argv, cwd, timeout, env):
        argv = list(argv)
        if argv[0] == "git" and argv[1:3] == ["remote", "add"]:
            return _REAL_RUN(argv, cwd, timeout, env)
        if argv[0] == "git" and argv[1:2] == ["-c"]:
            rest, flags = argv[1:], []
            while rest[:1] == ["-c"]:
                flags.append(rest[1])
                rest = rest[2:]
            self.clone_flags.append(flags)
            carries_helper = f"credential.https://github.com.helper={HELPER}" in flags
            url = rest[-2]
            repo = _GITHUB_URL.match(url).group(2)
            if rest[0] != "clone" or not (carries_helper and repo in self.keyed):
                return subprocess.CompletedProcess(argv, 128, "", "Authentication failed")
            done = _REAL_RUN(["git", "clone", "--quiet", str(self.repositories[repo]), rest[-1]], cwd, timeout, env)
            _REAL_RUN(["git", "remote", "set-url", "origin", url], Path(rest[-1]), timeout, env)
            return done
        return super().__call__(argv, cwd, timeout, env)


def _key(github: KeyedGitHub, issued: list):
    def issue(repo: str) -> GitHubToken:
        issued.append(repo)
        if repo not in github.keyed:
            raise GitHubKeyRefused("github_not_linked", "Your owner has not connected GitHub on this project.")
        return GitHubToken(token="ghu_secret", expires_at=0, login="owner", commit_email=OWNER_EMAIL, repository=repo)

    return issue


def _config_values(folder: Path, key: str) -> list[str]:
    found = subprocess.run(
        ["git", "config", "--local", "--get-all", key], cwd=str(folder), capture_output=True, text=True, check=False
    )
    return found.stdout.splitlines()


def test_a_repository_the_owners_key_serves_is_cloned_over_https_with_no_deploy_key(tmp_path):
    github = KeyedGitHub({"example-org/app-ecosystem": _remote(tmp_path / "remotes", "app-ecosystem")})
    github.keyed.add("example-org/app-ecosystem")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    issued: list[str] = []
    connector = Connector(
        _machine(tmp_path), workspace, run=github, github_key=_key(github, issued), helper=HELPER, alias_name=ALIAS
    )
    listed = {"alias": "app-ecosystem", "url": "git@github.com:example-org/app-ecosystem.git"}

    row = connector.connect(listed)

    assert row["state"] == "reachable" and row["action"] == "cloned", row
    assert row["route"] == "github_key" and row["github_key"] == "ready"
    clone = workspace / "app-ecosystem"
    assert _GIT_ORIGIN(clone) == "https://github.com/example-org/app-ecosystem.git"
    # The clone names this session's helper after clearing any inherited one, and says which repository.
    assert _config_values(clone, "credential.https://github.com.helper") == ["", HELPER]
    assert _config_values(clone, "credential.https://github.com.useHttpPath") == ["true"]
    assert _config_values(clone, "user.email") == [OWNER_EMAIL]
    assert _config_values(clone, "user.name") == [ALIAS]
    assert connector.commit_email == OWNER_EMAIL
    assert not (tmp_path / "ssh").exists(), "no deploy key is made"
    assert "ghu_secret" not in (clone / ".git" / "config").read_text(encoding="utf-8"), "the token is never stored"
    assert connector.pull_requests(listed["url"]) == {"state": "ready", "reason": "", "via": "pb worker gh"}
    assert issued == ["example-org/app-ecosystem"], "asked once per repository per run"


def test_a_refused_key_is_named_and_nothing_else_is_tried(tmp_path):
    github = KeyedGitHub({"example-org/applications": _remote(tmp_path / "remotes", "applications")})
    github.open.add("example-org/applications")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    connector = Connector(
        _machine(tmp_path), workspace, run=github, github_key=_key(github, []), helper=HELPER, alias_name=ALIAS
    )

    row = connector.connect({"alias": "applications", "url": "git@github.com:example-org/applications.git"})

    assert row["state"] == "needs_key" and row["report_reason"] == NEEDS_KEY_REASON
    assert row["github_key"] == "Your owner has not connected GitHub on this project."
    assert "grant" not in row and not (tmp_path / "ssh").exists()
    assert not (workspace / "applications").exists()
    assert connector.commit_email == ""


def test_an_existing_clone_moves_to_https_with_the_key_and_is_left_alone_without_it(tmp_path):
    """W371 review, W942: with the owner's key ready, origin (fetch and push) goes over HTTPS; without it the
    clone is named and left as it is, never fetched over SSH."""

    github = KeyedGitHub({"example-org/app-ecosystem": _remote(tmp_path / "remotes", "app-ecosystem")})
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    listed = {"alias": "app-ecosystem", "url": "git@github.com:example-org/app-ecosystem.git"}
    clone = workspace / "app-ecosystem"
    _git("clone", "-q", str(github.repositories["example-org/app-ecosystem"]), str(clone), cwd=tmp_path)
    _git("remote", "set-url", "origin", "git@github.com:example-org/app-ecosystem.git", cwd=clone)
    subprocess.run(["git", "config", "remote.origin.pushurl", "git@github.com:example-org/app-ecosystem.git"],
                   cwd=str(clone), check=True)

    # The key does not answer: nothing moves, nothing is fetched.
    unkeyed = Connector(
        _machine(tmp_path), workspace, run=github, github_key=_key(github, []), helper=HELPER, alias_name=ALIAS
    ).connect(listed)
    assert unkeyed["state"] == "needs_key" and "origin_switched" not in unkeyed
    assert _GIT_ORIGIN(clone) == "git@github.com:example-org/app-ecosystem.git"

    github.keyed.add("example-org/app-ecosystem")
    row = Connector(
        _machine(tmp_path), workspace, run=github, github_key=_key(github, []), helper=HELPER, alias_name=ALIAS
    ).connect(listed)

    assert row["state"] == "reachable" and row["action"] == "updated" and row["github_key"] == "ready"
    assert row["route"] == "github_key" and row["origin_switched"] == "ssh_to_https"
    assert _GIT_ORIGIN(clone) == "https://github.com/example-org/app-ecosystem.git"
    assert _config_values(clone, "remote.origin.pushurl") == [], "pushes follow origin"
    assert _config_values(clone, "credential.https://github.com.helper") == ["", HELPER]

    again = Connector(
        _machine(tmp_path), workspace, run=github, github_key=_key(github, []), helper=HELPER, alias_name=ALIAS
    ).connect(listed)
    assert again["state"] == "reachable" and "origin_switched" not in again, "the second run has nothing to move"


def test_no_deploykey_remote_is_added_when_origin_moves_to_https(tmp_path):
    """W942: the SSH route is not kept aside; pushes go with the owner's key or not at all."""
    github = KeyedGitHub({"example-org/app-ecosystem": _remote(tmp_path / "remotes", "app-ecosystem")})
    github.keyed.add("example-org/app-ecosystem")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    clone = workspace / "app-ecosystem"
    _git("clone", "-q", str(github.repositories["example-org/app-ecosystem"]), str(clone), cwd=tmp_path)
    _git("remote", "set-url", "origin", "git@github.com:example-org/app-ecosystem.git", cwd=clone)

    Connector(
        _machine(tmp_path), workspace, run=github, github_key=_key(github, []), helper=HELPER, alias_name=ALIAS
    ).connect({"alias": "app-ecosystem", "url": "git@github.com:example-org/app-ecosystem.git"})

    assert _GIT_ORIGIN(clone) == "https://github.com/example-org/app-ecosystem.git"
    remotes = subprocess.run(["git", "remote"], cwd=str(clone), capture_output=True, text=True, check=True)
    assert remotes.stdout.split() == ["origin"]


def _slow(step: str, *, lock: bool = True):
    """The real runner, except that one git step stalls after doing its work, as a big clone does."""

    def run(argv, cwd, timeout, env):
        argv = list(argv)
        if step in argv:
            folder = argv[-1] if step == "clone" else str(cwd)
            leave = f" && touch '{folder}/.git/index.lock'" if lock else ""
            script = " ".join(f"'{part}'" for part in argv) + leave + " && sleep 30"
            return _REAL_RUN(["sh", "-c", script], cwd, timeout, env)
        return _REAL_RUN(argv, cwd, timeout, env)

    return run


def test_a_first_clone_cut_off_by_its_limit_leaves_nothing_and_the_next_run_clones(tmp_path):
    """2026-09-28 ~21:20Z: a first clone of applications hit the 15 s limit and was
    left half checked out, an empty index and a leftover index.lock."""

    remote = _remote(tmp_path / "remotes", "applications")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    listed = {"alias": "applications", "url": str(remote)}

    cut = Connector(_machine(tmp_path), workspace, timeout=1.0, transfer_timeout=2.0, run=_slow("clone")).connect(listed)
    assert cut["state"] == "unreachable"
    assert cut["reason"] == "git clone did not finish within 2s. The partial folder was removed; run again."
    assert not (workspace / "applications").exists()

    again = Connector(_machine(tmp_path), workspace).connect(listed)
    assert again["state"] == "reachable" and again["action"] == "cloned"
    assert not (workspace / "applications" / ".git" / "index.lock").exists()


def test_an_update_cut_off_leaves_no_lock_of_its_own_and_keeps_anothers(tmp_path):
    remote = _remote(tmp_path / "remotes", "applications")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    listed = {"alias": "applications", "url": str(remote)}
    assert Connector(_machine(tmp_path), workspace).connect(listed)["action"] == "cloned"
    lock = workspace / "applications" / ".git" / "index.lock"

    cut = Connector(_machine(tmp_path), workspace, timeout=2.0, run=_slow("checkout")).connect(listed)
    assert cut["state"] == "unreachable" and "nothing was left locked" in cut["reason"]
    assert not lock.exists()
    assert (workspace / "applications" / ".git").is_dir(), "an existing clone is never removed"

    # A lock that was there before this run belongs to someone else: kept.
    lock.write_text("", encoding="utf-8")
    Connector(_machine(tmp_path), workspace, timeout=2.0, run=_slow("checkout", lock=False)).connect(listed)
    assert lock.exists()


def test_clone_and_fetch_get_the_transfer_limit_and_status_checks_the_short_one(tmp_path):
    remote = _remote(tmp_path / "remotes", "applications")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    limits: dict[str, float] = {}

    def recording(argv, cwd, timeout, env):
        if argv[0] == "git":
            verb = next(part for part in argv[1:] if not part.startswith("-") and "=" not in part)
            limits.setdefault(verb, timeout)
        return _REAL_RUN(argv, cwd, timeout, env)

    listed = {"alias": "applications", "url": str(remote)}
    Connector(_machine(tmp_path), workspace, timeout=15.0, run=recording).connect(listed)
    Connector(_machine(tmp_path), workspace, timeout=15.0, run=recording).connect(listed)
    assert limits["clone"] == project_connect.TRANSFER_TIMEOUT_SECONDS == 1800.0
    assert limits["fetch"] == 1800.0
    assert limits["status"] == limits["checkout"] == 15.0

"""The project-workspace procedure sets each repository up at its alias and its declared branch (W304 finding 39).

codex-ui's review: a plain clone lands on the remote's default branch and a
later fast-forward updates whatever is checked out, so a declared branch was
never applied, and two aliases with the same URL had no defined destination.
These tests run the procedure's own commands against real local repositories.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from project_board.client.procedures import source_package_path
from project_board.client.store import SharedFieldStore

pytestmark = pytest.mark.skipif(not (shutil.which("git") and shutil.which("bash")), reason="needs git and bash")


def _reference() -> str:
    return (source_package_path() / "references" / "project-workspace.md").read_text(encoding="utf-8")


def _setup_commands() -> str:
    section = _reference().split("## 2.", 1)[1]
    return re.search(r"```bash\n(.*?)```", section, re.S).group(1)


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    _git("init", "--quiet", "--initial-branch=main", cwd=source)
    _git("-c", "user.email=t@example.test", "-c", "user.name=t", "commit", "--quiet", "--allow-empty", "-m", "main", cwd=source)
    _git("checkout", "--quiet", "-b", "feature", cwd=source)
    _git("-c", "user.email=t@example.test", "-c", "user.name=t", "commit", "--quiet", "--allow-empty", "-m", "feature", cwd=source)
    _git("checkout", "--quiet", "main", cwd=source)
    bare = tmp_path / "applications.git"
    _git("clone", "--quiet", "--bare", str(source), str(bare))
    return bare


def _shell(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        pytest.skip(f"needs {name}")
    return executable


def _set_up(
    workspace: Path,
    alias: str,
    url: Path,
    branch: str = "",
    *,
    shell: str = "bash",
) -> None:
    subprocess.run(
        [_shell(shell), "-euc", _setup_commands()],
        check=True,
        env={"WORKSPACE": str(workspace), "ALIAS": alias, "URL": str(url), "BRANCH": branch, "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "SSH_CONFIG": "/dev/null"},
    )


def test_two_aliases_of_one_url_are_two_folders_each_at_its_branch(tmp_path, remote):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    _set_up(workspace, "applications", remote)
    _set_up(workspace, "journals", remote, branch="feature")

    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=workspace / "applications") == "main"
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=workspace / "journals") == "feature"
    assert _git("log", "-1", "--format=%s", cwd=workspace / "journals") == "feature"


def test_an_existing_clone_is_fetched_and_fast_forwarded_on_its_declared_branch(tmp_path, remote):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _set_up(workspace, "journals", remote, branch="feature")
    # The project moves on: a new commit on the declared branch.
    pusher = tmp_path / "pusher"
    _git("clone", "--quiet", "--branch", "feature", str(remote), str(pusher))
    _git("-c", "user.email=t@example.test", "-c", "user.name=t", "commit", "--quiet", "--allow-empty", "-m", "newer", cwd=pusher)
    _git("push", "--quiet", "origin", "feature", cwd=pusher)

    _set_up(workspace, "journals", remote, branch="feature")

    assert _git("log", "-1", "--format=%s", cwd=workspace / "journals") == "newer"


def test_a_record_not_yet_on_this_host_is_awaited_never_read_as_an_empty_list():
    words = " ".join(_reference().split())
    assert "Read `project_on_this_host` first." in words
    assert "`repositories` is empty only because nothing is here yet. Wait a minute and read again." in words
    assert "An empty list then means the card names no repositories yet" in words
    assert "It is a place inside the clone, never a separate clone." in words


def test_an_alias_whose_folder_holds_another_remote_is_refused_and_left_alone(tmp_path, remote):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _set_up(workspace, "applications", remote)
    other = tmp_path / "other.git"
    _git("clone", "--quiet", "--bare", str(remote), str(other))

    result = subprocess.run(
        ["bash", "-euc", _setup_commands()],
        env={"WORKSPACE": str(workspace), "ALIAS": "applications", "URL": str(other), "BRANCH": "", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "SSH_CONFIG": "/dev/null"},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 3
    assert f"the project declares {other}" in result.stderr
    assert _git("remote", "get-url", "origin", cwd=workspace / "applications") == str(remote)


def test_a_fresh_worker_reads_its_workspace_from_the_context(tmp_path, monkeypatch):
    from project_board.client import cli, host_config
    from project_board.contract.worker_identity import WorkerSessionIdentity

    host = host_config.initialize_host_config(
        target_id="target",
        endpoint="https://runtime.example/mcp",
        tenant="tenant",
        platform_project="project",
        host_id="spark1",
        allowed_roots=[str(tmp_path)],
        source_repositories={},
        config_path=tmp_path / "relay.json",
        state_root=tmp_path / "state",
    )
    workspace = tmp_path / "workspaces" / "space001"
    workspace.mkdir(parents=True)
    identity = WorkerSessionIdentity.create("claude-code", "a7b7935d-a064-43ec-937e-2b94f1660b68")
    host_config.enroll_worker_channel(
        host.path, identity=identity, profile="problem-board-claude-ops", authorized=True, working_directory=str(workspace)
    )
    monkeypatch.setenv("PROBLEM_BOARD_CONFIG", str(host.path))
    args = cli.build_parser().parse_args(
        ["worker", "context", "--runtime-kind", "claude-code", "--runtime-session-id", identity.runtime_session_id,
         "--project-ref", "work:project:quickstart-works-mttfmgqu"]
    )
    SharedFieldStore(host_config.HostRelayConfig.load(host.path).field_root).register_worker(
        worker_name=identity.worker_name, runtime_kind="claude-code", capabilities=[], authority_label="authority:test"
    )

    context = cli._worker_command(args)  # noqa: SLF001 - the command under test

    assert context["workspace"] == str(workspace)
    assert context["project_on_this_host"] is False


def test_without_a_declared_branch_a_resume_returns_to_the_remote_default(tmp_path, remote):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _set_up(workspace, "applications", remote)
    # The folder was left on another branch.
    _git("checkout", "--quiet", "feature", cwd=workspace / "applications")

    _set_up(workspace, "applications", remote)

    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=workspace / "applications") == "main"


def test_a_folder_with_uncommitted_work_is_left_on_its_branch(tmp_path, remote):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _set_up(workspace, "applications", remote)
    folder = workspace / "applications"
    _git("checkout", "--quiet", "feature", cwd=folder)
    (folder / "notes.txt").write_text("in progress\n")
    _git("add", "notes.txt", cwd=folder)

    result = subprocess.run(
        ["bash", "-euc", _setup_commands()],
        env={"WORKSPACE": str(workspace), "ALIAS": "applications", "URL": str(remote), "BRANCH": "", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "SSH_CONFIG": "/dev/null"},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 4
    assert "has uncommitted changes on feature" in result.stderr
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=folder) == "feature"
    assert (folder / "notes.txt").read_text() == "in progress\n"


def test_a_new_file_not_yet_added_also_keeps_the_folder_on_its_branch(tmp_path, remote):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _set_up(workspace, "applications", remote)
    folder = workspace / "applications"
    _git("checkout", "--quiet", "feature", cwd=folder)
    (folder / "new_source.py").write_text("print('draft')\n")

    result = subprocess.run(
        ["bash", "-euc", _setup_commands()],
        env={"WORKSPACE": str(workspace), "ALIAS": "applications", "URL": str(remote), "BRANCH": "", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "SSH_CONFIG": "/dev/null"},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 4
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=folder) == "feature"
    assert (folder / "new_source.py").exists()


def _alias_host(tmp_path: Path, remote: Path) -> dict[str, str]:
    """A deploy-key host as add-a-worker-host step 7 sets it up, reaching a local repository.

    The SSH configuration names github-applications for github.com, and git
    rewrites both URL forms onto the local bare repository, so the procedure
    runs offline while the origins keep the forms a real host has.
    """

    ssh_config = tmp_path / "ssh_config"
    ssh_config.write_text("Host github-applications\n  HostName github.com\n  User git\n")
    gitconfig = tmp_path / "gitconfig"
    base = str(remote.parent) + "/"
    gitconfig.write_text(
        f'[url "{base}"]\n    insteadOf = github-applications:kdcube/\n    insteadOf = git@github.com:kdcube/\n'
    )
    return {
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "SSH_CONFIG": str(ssh_config),
        "GIT_CONFIG_GLOBAL": str(gitconfig),
        "HOME": str(tmp_path),
    }


def _run(
    env: dict[str, str],
    workspace: Path,
    url: str,
    *,
    shell: str = "bash",
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_shell(shell), "-euc", _setup_commands()],
        env={**env, "WORKSPACE": str(workspace), "ALIAS": "applications", "URL": url, "BRANCH": ""},
        capture_output=True,
        text=True,
    )


def test_a_clone_through_the_host_s_ssh_alias_matches_the_declared_url(tmp_path, remote):
    env = _alias_host(tmp_path, remote)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    folder = workspace / "applications"
    subprocess.run(["git", "clone", "--quiet", "github-applications:kdcube/applications.git", str(folder)], env=env, check=True)

    result = _run(env, workspace, "git@github.com:kdcube/applications.git")

    assert result.returncode == 0, result.stderr
    assert subprocess.run(["git", "-C", str(folder), "config", "--get", "remote.origin.url"], env=env,
                          capture_output=True, text=True).stdout.strip() == "github-applications:kdcube/applications.git"


@pytest.mark.parametrize("shell", ("bash", "zsh"))
def test_a_new_clone_uses_the_host_s_ssh_alias(tmp_path, remote, shell):
    env = _alias_host(tmp_path, remote)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = _run(
        env,
        workspace,
        "git@github.com:kdcube/applications.git",
        shell=shell,
    )

    assert result.returncode == 0, result.stderr
    origin = subprocess.run(["git", "-C", str(workspace / "applications"), "config", "--get", "remote.origin.url"],
                            env=env, capture_output=True, text=True).stdout.strip()
    assert origin == "github-applications:kdcube/applications.git"


def test_another_repository_behind_the_alias_is_still_refused(tmp_path, remote):
    env = _alias_host(tmp_path, remote)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "clone", "--quiet", "github-applications:kdcube/applications.git", str(workspace / "applications")],
                   env=env, check=True)

    result = _run(env, workspace, "git@github.com:kdcube/app-ecosystem.git")

    assert result.returncode == 3
    assert "the project declares git@github.com:kdcube/app-ecosystem.git" in result.stderr

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


def _set_up(workspace: Path, alias: str, url: Path, branch: str = "") -> None:
    subprocess.run(
        ["bash", "-euc", _setup_commands()],
        check=True,
        env={"WORKSPACE": str(workspace), "ALIAS": alias, "URL": str(url), "BRANCH": branch, "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"},
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

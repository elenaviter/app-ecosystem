"""W794: the coordinator machine's own per-project copy of the listed files.

Operator, 2026-10-13: "we need the machine with coordinator and its credential"; the file shown is the
"last one" ("A": the default branch's newest commit, fetched just before the read).
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from project_board.client import project_file_source as source_module
from project_board.client.project_file_source import ProjectFileSource, listed_path
from project_board.contract.errors import DomainError

TOKEN = "ghu_W794_secret_token_value"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@example.test", "-c", "user.name=t", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout


@pytest.fixture()
def upstream(tmp_path: Path) -> Path:
    repo = tmp_path / "remote" / "owner" / "proj.git"
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    (work / "docs").mkdir()
    (work / "docs" / "env.md").write_text("# Env v1\n", encoding="utf-8")
    (work / "secret.md").write_text("not listed\n", encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-qm", "one")
    repo.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(repo))
    _git(repo, "config", "uploadpack.allowFilter", "true")
    _git(repo, "config", "uploadpack.allowAnySHA1InWant", "true")
    _git(work, "remote", "add", "origin", str(repo))
    return work


def _source(tmp_path: Path) -> ProjectFileSource:
    return ProjectFileSource(tmp_path / "runtime" / "project-files", remote_base=f"file://{tmp_path / 'remote'}/")


async def _token(repository: str) -> str:
    assert repository == "owner/proj"
    return TOKEN


def _read(source: ProjectFileSource, path: str = "docs/env.md"):
    return asyncio.run(source.read(project_id="p1", alias="apps", repository="owner/proj", path=path, token=_token))


def test_the_file_is_the_default_branchs_newest_commit(tmp_path, upstream):
    source = _source(tmp_path)
    content, commit = _read(source)
    assert content == "# Env v1\n" and len(commit) == 40
    (upstream / "docs" / "env.md").write_text("# Env v2\n", encoding="utf-8")
    _git(upstream, "commit", "-qam", "two")
    _git(upstream, "push", "-q", "origin", "main")
    content, newer = _read(source)
    assert content == "# Env v2\n" and newer != commit
    assert newer == _git(upstream, "rev-parse", "HEAD").strip()


def test_a_renamed_default_branch_is_followed(tmp_path, upstream):
    source = _source(tmp_path)
    _read(source)
    remote = tmp_path / "remote" / "owner" / "proj.git"
    (upstream / "docs" / "env.md").write_text("# On trunk\n", encoding="utf-8")
    _git(upstream, "commit", "-qam", "trunk")
    _git(upstream, "push", "-q", "origin", "main:trunk")
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/trunk")
    _git(remote, "branch", "-D", "main")
    assert _read(source)[0] == "# On trunk\n"


def test_only_what_is_read_is_fetched_and_no_working_tree_exists(tmp_path, upstream):
    source = _source(tmp_path)
    _read(source)
    clone = tmp_path / "runtime" / "project-files" / "p1" / "apps"
    assert sorted(path.name for path in clone.iterdir()) == [".git"]
    # The unlisted file's blob was never fetched.
    blob = _git(upstream, "rev-parse", "HEAD:secret.md").strip()
    listed = _git(clone, "rev-list", "--objects", "--missing=print", "refs/remotes/origin/main")
    assert f"?{blob}" in listed.split()


def test_the_token_is_never_in_argv_config_or_files(tmp_path, upstream, monkeypatch):
    seen: list[tuple[str, ...]] = []
    real = asyncio.create_subprocess_exec

    async def recording(*args, **kwargs):
        seen.append(tuple(str(arg) for arg in args))
        return await real(*args, **kwargs)

    monkeypatch.setattr(source_module.asyncio, "create_subprocess_exec", recording)
    source = _source(tmp_path)
    _read(source)
    assert seen and all(TOKEN not in " ".join(argv) for argv in seen)
    for path in (tmp_path / "runtime").rglob("*"):
        if path.is_file():
            assert TOKEN.encode() not in path.read_bytes(), path


def test_listed_paths_only(tmp_path, upstream):
    for bad in ("../x.md", "/etc/passwd", "docs/../secret.md", ".git/config", "a\\b", ""):
        with pytest.raises(DomainError) as caught:
            listed_path(bad)
        assert caught.value.code == "file_not_listed", bad
    with pytest.raises(DomainError) as caught:
        _read(_source(tmp_path), "docs/none.md")
    assert caught.value.code == "file_missing"


def test_two_concurrent_views_both_get_the_newest_commit(tmp_path, upstream):
    source = _source(tmp_path)

    async def both():
        return await asyncio.gather(*(
            source.read(project_id="p1", alias="apps", repository="owner/proj", path="docs/env.md", token=_token)
            for _ in range(2)
        ))

    first, second = asyncio.run(both())
    assert first == second and first[0] == "# Env v1\n"


def test_an_unreachable_remote_is_a_named_failure(tmp_path):
    source = ProjectFileSource(tmp_path / "rt", remote_base=f"file://{tmp_path / 'nowhere'}/")
    with pytest.raises(DomainError) as caught:
        asyncio.run(source.read(project_id="p1", alias="apps", repository="owner/proj", path="a.md", token=_token))
    assert caught.value.code == "fetch_failed"
    assert TOKEN not in str(caught.value)


# W848 (operator, 2026-10-14: "B: the client reads local repositories directly << of course this! this is special
# type of project. where we do not have any github"): a repository on this machine is read in place.


def _local(source: ProjectFileSource, repository: Path, path: str = "docs/env.md"):
    return asyncio.run(source.read_local(repository_path=str(repository), path=path))


def test_a_local_repository_serves_its_default_branchs_newest_commit(tmp_path, upstream):
    source = _source(tmp_path)
    content, commit = _local(source, upstream)
    assert content == "# Env v1\n" and commit == _git(upstream, "rev-parse", "HEAD").strip()
    (upstream / "docs" / "env.md").write_text("# Env v2\n", encoding="utf-8")
    _git(upstream, "commit", "-qam", "two")
    content, newer = _local(source, upstream)
    assert content == "# Env v2\n" and newer != commit
    assert not (tmp_path / "runtime" / "project-files").exists()  # no clone, no askpass, nothing written


def test_uncommitted_and_staged_edits_are_not_served(tmp_path, upstream):
    source = _source(tmp_path)
    (upstream / "docs" / "env.md").write_text("# Staged\n", encoding="utf-8")
    _git(upstream, "add", "docs/env.md")
    (upstream / "docs" / "env.md").write_text("# Working tree\n", encoding="utf-8")
    (upstream / "docs" / "new.md").write_text("untracked\n", encoding="utf-8")
    assert _local(source, upstream)[0] == "# Env v1\n"
    with pytest.raises(DomainError) as raised:
        _local(source, upstream, "docs/new.md")
    assert raised.value.code == "file_missing"


def test_a_local_bare_repository_is_read(tmp_path, upstream):
    assert _local(_source(tmp_path), tmp_path / "remote" / "owner" / "proj.git")[0] == "# Env v1\n"


def test_a_local_repository_that_cannot_be_read_is_a_named_refusal(tmp_path, upstream):
    source = _source(tmp_path)

    def code(repository: str, path: str = "docs/env.md") -> str:
        with pytest.raises(DomainError) as raised:
            asyncio.run(source.read_local(repository_path=repository, path=path))
        return raised.value.code

    assert code(str(tmp_path / "missing")) == "repository_path_missing"
    assert code("relative/path") == "repository_path_missing"
    (tmp_path / "plain").mkdir()
    assert code(str(tmp_path / "plain")) == "repository_not_git"
    assert code(str(upstream / "docs")) == "repository_not_git"  # a folder inside a repository is not one
    assert code(str(upstream), "../secret.md") == "file_not_listed"
    assert code(str(upstream), ".git/config") == "file_not_listed"
    _git(upstream, "checkout", "-q", "--detach")
    assert code(str(upstream)) == "repository_read_failed"

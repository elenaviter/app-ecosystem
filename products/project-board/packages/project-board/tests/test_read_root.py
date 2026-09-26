"""A host reads a project's setup from a read root the relay keeps current (W262).

The root checkout of an alias is where agents and the journal workspace write;
on a host it is often an operator's working checkout that cannot fast-forward
over their edits, so a setup read there goes stale. A map entry may name a
``read_root``: a dedicated, detached, never-edited worktree. ``pb worker
context`` reads the setup through it and names the commit; the relay's
housekeeping advances it when it is clean; a lag is named, never fatal.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from project_board.client import host_config
from project_board.client import local_state_maintenance as maintenance
from project_board.client import read_roots
from project_board.client import relay
from project_board.client.journals import JournalWorkspace, RepositoryMap
from project_board.client.project_setup import PROJECT_SETUP_FILE, PROJECT_SETUP_SCHEMA
from project_board.client.store import SharedFieldStore

PROJECT = "work:project:project-one"
HOME = "projects/project-one"
T0 = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)

GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(directory: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(directory), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **GIT_ENV},
    ).stdout.strip()


def setup_file(runtime: str) -> str:
    return json.dumps(
        {
            "schema": PROJECT_SETUP_SCHEMA,
            "instructions_ref": f"repo:journals/{HOME}/project-instructions.md",
            "runtimes": [
                {
                    "name": runtime,
                    "host": runtime,
                    "kind": "kdcube",
                    "actions": {
                        "refresh": {
                            "who": ["coordinator"],
                            "releases": [{"repository": "journals", "ref": "origin/main"}],
                        }
                    },
                }
            ],
        }
    )


def commit_setup(checkout: Path, runtime: str, facts: str) -> str:
    home = checkout / HOME
    home.mkdir(parents=True, exist_ok=True)
    (home / PROJECT_SETUP_FILE).write_text(setup_file(runtime), encoding="utf-8")
    (home / "project-facts.md").write_text(facts, encoding="utf-8")
    (home / "project-instructions.md").write_text(f"# {runtime}\n", encoding="utf-8")
    git(checkout, "add", "-A")
    git(checkout, "commit", "--quiet", "-m", runtime)
    git(checkout, "push", "--quiet", "origin", "HEAD:main")
    return git(checkout, "rev-parse", "HEAD")


@pytest.fixture()
def repos(tmp_path: Path) -> dict[str, Path]:
    """A bare remote, a stale root checkout, a read root, and an upstream clone."""

    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(remote)], check=True, env={**os.environ, **GIT_ENV})
    upstream = tmp_path / "upstream"
    subprocess.run(["git", "clone", "--quiet", str(remote), str(upstream)], check=True, capture_output=True, env={**os.environ, **GIT_ENV})
    git(upstream, "checkout", "--quiet", "-b", "main")
    commit_setup(upstream, "stale-runtime", "stale facts\n")
    root = tmp_path / "root"
    subprocess.run(["git", "clone", "--quiet", str(remote), str(root)], check=True, capture_output=True, env={**os.environ, **GIT_ENV})
    read_root = tmp_path / "read-root"
    git(root, "worktree", "add", "--quiet", "--detach", str(read_root), "origin/main")
    return {
        "remote": remote,
        "upstream": upstream,
        "root": root,
        "read_root": read_root,
        "tmp": tmp_path,
    }


def workspace(repos: dict[str, Path], *, with_read_root: bool = True) -> JournalWorkspace:
    entry: object = str(repos["root"])
    if with_read_root:
        entry = {"root": str(repos["root"]), "read_root": str(repos["read_root"])}
    result = JournalWorkspace(
        repos["tmp"] / "workspace", RepositoryMap.from_mapping({"journals": entry})
    )
    result.reconcile(
        {"project_ref": PROJECT, "journal_home_ref": f"repo:journals/{HOME}", "revision": 1}
    )
    return result


def advance_upstream(repos: dict[str, Path]) -> str:
    return commit_setup(repos["upstream"], "fresh-runtime", "fresh facts\n")


# -- context -----------------------------------------------------------------


def test_context_reads_the_setup_from_the_read_root_and_writes_stay_on_root(repos):
    fresh = advance_upstream(repos)
    # The relay advanced the read root; the operator's checkout stayed behind.
    git(repos["read_root"], "fetch", "--quiet", "origin", "main")
    git(repos["read_root"], "checkout", "--quiet", "--detach", "origin/main")
    git(repos["root"], "fetch", "--quiet", "origin", "main")

    context = workspace(repos).context(PROJECT)

    assert [runtime["name"] for runtime in context["runtimes"]] == ["fresh-runtime"]
    read_home = repos["read_root"].resolve() / HOME
    assert context["local_project_facts"] == str(read_home / "project-facts.md")
    assert Path(context["local_project_facts"]).read_text() == "fresh facts\n"
    assert context["local_project_instructions"] == str(read_home / "project-instructions.md")
    assert context["journal_home_commit"] == fresh
    assert context["journal_home_read_root"] == str(repos["read_root"].resolve())
    root_home = repos["root"].resolve() / HOME
    assert context["local_journal_home"] == str(root_home)
    assert context["local_journal_directory"] == str(root_home / "journal")
    assert context["project_setup_issues"] == []


def test_without_a_read_root_the_context_reads_root_and_names_its_commit(repos):
    context = workspace(repos, with_read_root=False).context(PROJECT)

    assert [runtime["name"] for runtime in context["runtimes"]] == ["stale-runtime"]
    assert context["journal_home_commit"] == git(repos["root"], "rev-parse", "HEAD")
    assert context["journal_home_read_root"] == ""
    assert context["local_project_facts"] == str(repos["root"].resolve() / HOME / "project-facts.md")
    assert context["project_setup_issues"] == []


def test_a_read_root_behind_its_ref_is_named_with_both_commits(repos):
    behind = git(repos["read_root"], "rev-parse", "HEAD")
    fresh = advance_upstream(repos)
    git(repos["read_root"], "fetch", "--quiet", "origin", "main")  # a local ref only

    context = workspace(repos).context(PROJECT)

    assert context["journal_home_commit"] == behind
    [issue] = context["project_setup_issues"]
    assert "journals" in issue and str(repos["read_root"].resolve()) in issue
    assert behind in issue and fresh in issue and "behind origin/main" in issue


def test_a_missing_read_root_falls_back_to_root_and_is_named(repos):
    git(repos["root"], "worktree", "remove", "--force", str(repos["read_root"]))

    context = workspace(repos).context(PROJECT)

    assert [runtime["name"] for runtime in context["runtimes"]] == ["stale-runtime"]
    assert context["journal_home_read_root"] == ""
    assert context["journal_home_commit"] == git(repos["root"], "rev-parse", "HEAD")
    [issue] = context["project_setup_issues"]
    assert "journals" in issue and "missing" in issue


# -- housekeeping ----------------------------------------------------------------


def repository_map(repos: dict[str, Path]) -> RepositoryMap:
    return RepositoryMap.from_mapping(
        {"journals": {"root": str(repos["root"]), "read_root": str(repos["read_root"])}}
    )


def test_housekeeping_advances_a_clean_read_root_to_its_ref(repos):
    fresh = advance_upstream(repos)
    state = repos["tmp"] / "host" / read_roots.READ_ROOTS_STATE

    summary = read_roots.advance_read_roots(repository_map(repos), state, now=T0)

    assert summary["advanced"] == 1 and summary["failed"] == 0
    assert git(repos["read_root"], "rev-parse", "HEAD") == fresh
    # The operator's checkout is never touched.
    assert git(repos["root"], "rev-parse", "HEAD") != fresh
    recorded = json.loads(state.read_text())["aliases"]["journals"]
    assert recorded["outcome"] == "advanced" and recorded["commit"] == fresh


def test_housekeeping_leaves_a_dirty_read_root_alone(repos, caplog):
    before = git(repos["read_root"], "rev-parse", "HEAD")
    advance_upstream(repos)
    (repos["read_root"] / "stray.txt").write_text("someone edited it\n")
    state = repos["tmp"] / "host" / read_roots.READ_ROOTS_STATE

    with caplog.at_level(logging.WARNING, logger=read_roots.__name__):
        first = read_roots.advance_read_roots(repository_map(repos), state, now=T0)
        second = read_roots.advance_read_roots(
            repository_map(repos), state, now=T0 + timedelta(minutes=10)
        )

    assert first["skipped_dirty"] == 1 and second["skipped_dirty"] == 1
    assert first["advanced"] == 0
    assert git(repos["read_root"], "rev-parse", "HEAD") == before
    warnings = [record for record in caplog.records if "read root not advanced" in record.getMessage()]
    assert len(warnings) == 1
    assert "alias=journals" in warnings[0].getMessage()


def test_a_second_pass_within_the_interval_runs_no_git(repos, monkeypatch):
    state = repos["tmp"] / "host" / read_roots.READ_ROOTS_STATE
    assert read_roots.advance_read_roots(repository_map(repos), state, now=T0)["unchanged"] == 1

    calls: list[object] = []
    monkeypatch.setattr(read_roots.subprocess, "run", lambda *a, **k: calls.append(a))
    summary = read_roots.advance_read_roots(
        repository_map(repos), state, now=T0 + timedelta(seconds=299)
    )

    assert summary["not_due"] == 1 and calls == []


def test_a_failed_fetch_never_raises_and_is_counted(repos, caplog):
    git(repos["read_root"], "remote", "set-url", "origin", str(repos["tmp"] / "gone.git"))
    state = repos["tmp"] / "host" / read_roots.READ_ROOTS_STATE

    with caplog.at_level(logging.WARNING, logger=read_roots.__name__):
        summary = read_roots.advance_read_roots(repository_map(repos), state, now=T0)

    assert summary["failed"] == 1
    assert json.loads(state.read_text())["aliases"]["journals"]["reason"].startswith("fetch origin main failed")
    assert any("alias=journals" in record.getMessage() for record in caplog.records)


def test_the_maintenance_pass_carries_the_read_root_counts(repos, tmp_path):
    fresh = advance_upstream(repos)
    field = SharedFieldStore(tmp_path / "field")
    state = tmp_path / "host" / read_roots.READ_ROOTS_STATE

    summary = maintenance.run_local_state_maintenance(
        field, now=T0, repositories=repository_map(repos), read_roots_state=state
    )

    assert summary["read_roots"]["advanced"] == 1
    assert git(repos["read_root"], "rev-parse", "HEAD") == fresh
    assert maintenance.run_local_state_maintenance(field, now=T0)["read_roots"] is None


# -- host config -------------------------------------------------------------------


def test_the_host_config_carries_a_read_root_to_the_relay_housekeeping(repos, tmp_path):
    host = host_config.initialize_host_config(
        target_id="target",
        endpoint="https://runtime.example/mcp",
        tenant="tenant",
        platform_project="project",
        host_id="host-one",
        allowed_roots=[str(tmp_path)],
        source_repositories={"journals": str(repos["root"])},
        config_path=tmp_path / "relay.json",
        state_root=tmp_path / "state",
    )
    value = json.loads(host.path.read_text())
    value["journal_workspace"]["source_repositories"]["journals"] = {
        "root": str(repos["root"]),
        "read_root": str(repos["read_root"]),
    }
    host.path.write_text(json.dumps(value))

    loaded = host_config.HostRelayConfig.load(host.path)
    assert loaded.source_repositories == (("journals", str(repos["root"].resolve())),)
    assert loaded.source_read_roots == (
        ("journals", str(repos["read_root"].resolve()), "origin/main"),
    )
    # A later revision of the map keeps the read root with its alias.
    updated = host_config.update_host_config(
        host.path, source_repositories={"other": str(repos["upstream"])}
    )
    assert updated.source_read_roots == loaded.source_read_roots

    supervisor = relay.ProblemBoardRelaySupervisor(config_path=host.path, connector=None)
    job = supervisor._read_root_maintenance()
    assert job["read_roots_state"] == host.path.parent / read_roots.READ_ROOTS_STATE
    assert job["repositories"].read_roots == {"journals": repos["read_root"].resolve()}


def test_a_read_root_outside_the_approved_roots_is_refused(repos, tmp_path):
    host = host_config.initialize_host_config(
        target_id="target",
        endpoint="https://runtime.example/mcp",
        tenant="tenant",
        platform_project="project",
        host_id="host-one",
        allowed_roots=[str(repos["root"])],
        source_repositories={"journals": str(repos["root"])},
        config_path=tmp_path / "relay.json",
        state_root=tmp_path / "state",
    )
    value = json.loads(host.path.read_text())
    value["journal_workspace"]["source_repositories"]["journals"] = {
        "root": str(repos["root"]),
        "read_root": str(repos["read_root"]),
    }
    with pytest.raises(Exception) as caught:
        host_config.HostRelayConfig.from_mapping(value)
    assert getattr(caught.value, "code", "") == "work_repository_root_not_allowed"


def test_a_read_ref_that_could_become_a_git_option_is_refused(tmp_path):
    import pytest
    from project_board.client.journals import RepositoryMap
    from project_board.contract.errors import DomainError

    root = tmp_path / "root"
    read_root = tmp_path / "read"
    root.mkdir()
    read_root.mkdir()
    for read_ref in ("-x/main", "origin/-x", "--upload-pack=touch/main", "origin/..", "main"):
        with pytest.raises(DomainError):
            RepositoryMap.from_mapping({"journals": {"root": str(root), "read_root": str(read_root), "read_ref": read_ref}})
    RepositoryMap.from_mapping({"journals": {"root": str(root), "read_root": str(read_root), "read_ref": "origin/release-1.2"}})

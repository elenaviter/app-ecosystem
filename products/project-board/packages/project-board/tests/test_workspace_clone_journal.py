"""Each worker reads its project's journal and pages from its own workspace clone (W343).

On the maintainer host, 2026-09-26, `pb worker context` read the journal home, and with
it the setup, facts, environment and instructions pages, through the host's
repository map: one host-wide checkout that was 186 commits behind its
remote. Every agent there got no runtimes, no instructions ref and "no
project-setup.json", while each had a current clone in its own workspace.

Each worker now reads and writes project state only through
``<workspace>/<alias>``. Two workers on one host whose clones hold different
commits each see their own pages and their own journal, a host map that names
a stale checkout is never read, and a clone that is missing or behind is named
with the step that fixes it.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from project_board.client import cli, host_config, relay
from project_board.client.project_setup import PROJECT_SETUP_FILE, PROJECT_SETUP_SCHEMA
from project_board.client.store import SharedFieldStore
from project_board.contract.worker_identity import WorkerSessionIdentity

PROJECT = "work:project:project-one"
HOME = "projects/project-one"
ENTRY = f"{HOME}/journal/2026.09.26.160000-decision.md"
BINDING = {
    "journal_binding": {
        "project_ref": PROJECT,
        "journal_home_ref": f"repo:journals/{HOME}",
        "project_artifact_ref": "",
        "revision": 1,
    }
}
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


def publish(checkout: Path, runtime: str) -> str:
    """Commit one version of the project's pages and journal entry, and push it."""

    home = checkout / HOME
    (home / "journal").mkdir(parents=True, exist_ok=True)
    (home / PROJECT_SETUP_FILE).write_text(
        json.dumps(
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
        ),
        encoding="utf-8",
    )
    (home / "project-instructions.md").write_text(f"# {runtime}\n", encoding="utf-8")
    (home / "project-facts.md").write_text(f"{runtime} facts\n", encoding="utf-8")
    (checkout / ENTRY).write_text(
        "---\n"
        "entry_ref: work:journal:20260926T160000Z:journal_0123456789abcdef:decision\n"
        f"project_ref: {PROJECT}\n"
        f"title: The {runtime} decision\n"
        "---\n\n"
        f"Decided on {runtime}.\n",
        encoding="utf-8",
    )
    git(checkout, "add", "-A")
    git(checkout, "commit", "--quiet", "-m", runtime)
    git(checkout, "push", "--quiet", "origin", "HEAD:main")
    return git(checkout, "rev-parse", "HEAD")


def clone(remote: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--quiet", str(remote), str(destination)],
        check=True,
        capture_output=True,
        env={**os.environ, **GIT_ENV},
    )


@pytest.fixture()
def host(tmp_path: Path) -> dict:
    """One host, a stale host checkout in its map, and three workers.

    ``behind`` cloned the journal repository at the first commit and fetched
    the second without merging it; ``current`` cloned at the second; ``fresh``
    has cloned nothing yet. The host map names a checkout holding a third,
    older content that no worker may ever read.
    """

    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--quiet", "--bare", "-b", "main", str(remote)],
        check=True,
        env={**os.environ, **GIT_ENV},
    )
    upstream = tmp_path / "upstream"
    clone(remote, upstream)
    git(upstream, "checkout", "--quiet", "-b", "main")
    publish(upstream, "host-checkout-runtime")
    stale = tmp_path / "host-checkout"
    clone(remote, stale)
    first = publish(upstream, "first-runtime")

    agents = tmp_path / "agents"
    behind = agents / "behind"
    clone(remote, behind / "journals")
    second = publish(upstream, "second-runtime")
    git(behind / "journals", "fetch", "--quiet", "origin")
    current = agents / "current"
    clone(remote, current / "journals")
    fresh = agents / "fresh"
    fresh.mkdir(parents=True)

    config = host_config.initialize_host_config(
        target_id="target",
        endpoint="https://runtime.example/mcp",
        tenant="tenant",
        platform_project="project",
        host_id="host-one",
        allowed_roots=[str(tmp_path)],
        source_repositories={"journals": str(stale)},
        config_path=tmp_path / "relay.json",
        state_root=tmp_path / "state",
    )
    field = SharedFieldStore(host_config.HostRelayConfig.load(config.path).field_root)
    workers = {}
    for number, (name, workspace) in enumerate(
        (("behind", behind), ("current", current), ("fresh", fresh)), start=1
    ):
        identity = WorkerSessionIdentity.create(
            "claude-code", f"{number:08d}-0000-4000-8000-000000000000"
        )
        channel = host_config.enroll_worker_channel(
            config.path,
            identity=identity,
            profile=f"problem-board-claude-{name}",
            authorized=True,
            working_directory=str(workspace),
        )
        field.register_worker(
            worker_name=identity.worker_name,
            runtime_kind="claude-code",
            capabilities=[],
            authority_label="authority:test",
        )
        workers[name] = {"identity": identity, "channel": channel, "workspace": workspace}
    loaded = host_config.HostRelayConfig.load(config.path)
    for worker in workers.values():
        # The relay channel of each worker binds the project journal, as on
        # the heartbeat that carries the binding.
        adapter = relay.ProblemBoardHostRelayAdapter(
            config=relay.RelayConfig.from_host_channel(
                loaded, worker["channel"], project_id="project-one"
            ),
            field=SharedFieldStore(loaded.field_root),
            client=object(),
        )
        adapter._reconcile_journal_binding(BINDING)  # noqa: SLF001 - the relay step under test
        worker["relay"] = adapter
    return {
        "config": loaded,
        "workers": workers,
        "stale": stale,
        "first": first,
        "second": second,
    }


def context(host: dict, name: str) -> dict:
    identity = host["workers"][name]["identity"]
    args = cli.build_parser().parse_args(
        [
            "worker", "context",
            "--config", str(host["config"].path),
            "--runtime-kind", identity.runtime_kind,
            "--runtime-session-id", identity.runtime_session_id,
            "--project-ref", PROJECT,
        ]
    )
    return cli._worker_command(args)  # noqa: SLF001 - the command under test


def runtimes(result: dict) -> list[str]:
    return [runtime["name"] for runtime in result.get("runtimes") or []]


def test_two_workers_on_different_commits_each_read_their_own_pages(host):
    behind = context(host, "behind")
    current = context(host, "current")

    assert behind["journal_state"] == "available"
    assert runtimes(behind) == ["first-runtime"]
    assert behind["journal_home_commit"] == host["first"]
    assert Path(behind["local_project_facts"]).read_text() == "first-runtime facts\n"
    assert behind["project_instructions_ref"] == f"repo:journals/{HOME}/project-instructions.md"
    assert Path(behind["local_journal_home"]).is_relative_to(host["workers"]["behind"]["workspace"])

    assert current["journal_state"] == "available"
    assert runtimes(current) == ["second-runtime"]
    assert current["journal_home_commit"] == host["second"]
    assert Path(current["local_project_facts"]).read_text() == "second-runtime facts\n"
    assert Path(current["local_journal_home"]).is_relative_to(host["workers"]["current"]["workspace"])


def test_the_host_map_naming_a_stale_checkout_is_never_read(host):
    for name in ("behind", "current"):
        result = context(host, name)

        assert "host-checkout-runtime" not in runtimes(result)
        text = json.dumps(result)
        assert str(host["stale"]) not in text
        assert result["journal_home_read_root"] == ""


def test_a_clone_behind_its_remote_is_named_with_the_fetch_step(host):
    behind = context(host, "behind")
    current = context(host, "current")

    clone_state = behind["journal_clone"]
    assert clone_state["state"] == "behind"
    assert clone_state["alias"] == "journals"
    assert clone_state["path"] == str(host["workers"]["behind"]["workspace"].resolve() / "journals")
    assert clone_state["commit"] == host["first"]
    assert clone_state["compared_commit"] == host["second"]
    assert clone_state["behind"] == 1
    assert "project-workspace.md step 2" in clone_state["action"]
    assert "merge --ff-only" in clone_state["action"]
    assert clone_state["action"] in behind["project_setup_issues"]

    assert current["journal_clone"]["state"] == "current"
    assert current["journal_clone"]["action"] == ""
    assert not any("journals at" in issue for issue in current["project_setup_issues"])


def test_a_missing_clone_is_named_and_no_other_checkout_stands_in(host):
    fresh = context(host, "fresh")

    assert fresh["journal_state"] == "unavailable"
    assert fresh["journal_error_code"] == "journal_repository_root_missing"
    expected = str(host["workers"]["fresh"]["workspace"].resolve() / "journals")
    assert fresh["journal_error_details"]["path"] == expected
    assert fresh["journal_error_details"]["alias"] == "journals"
    assert "project-workspace.md step 2" in fresh["journal_error"]
    assert "runtimes" not in fresh
    assert str(host["stale"]) not in json.dumps(fresh)


def test_each_relay_serves_the_journal_of_its_own_clone(host):
    behind = host["workers"]["behind"]["relay"].journal_workspace
    current = host["workers"]["current"]["relay"].journal_workspace

    old = behind.read_for_project(project_ref=PROJECT, repository_journal_ref=f"repo:journals/{ENTRY}")
    new = current.read_for_project(project_ref=PROJECT, repository_journal_ref=f"repo:journals/{ENTRY}")

    assert "Decided on first-runtime." in old["content"]
    assert "Decided on second-runtime." in new["content"]
    assert [row["title"] for row in behind.search("decision", project_ref=PROJECT)] == ["The first-runtime decision"]
    assert [row["title"] for row in current.search("decision", project_ref=PROJECT)] == ["The second-runtime decision"]
    assert behind.index.path != current.index.path

    stamp = behind.clone_stamp(PROJECT)
    assert stamp["journal_home_commit"] == host["first"]
    assert stamp["journal_clone"]["state"] == "behind"
    assert current.clone_stamp(PROJECT)["journal_clone"]["state"] == "current"

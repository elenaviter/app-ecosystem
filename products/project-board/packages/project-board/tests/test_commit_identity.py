"""The project's commit identity reaches every agent and its clones (W368).

Operator, 2026-09-27: agents commit as ``<their alias> <the project's email>``,
set for the project instead of remembered by each agent. The board carries the
email in the project row; the relay keeps it with the repository list on the
host; ``pb worker context`` hands the agent the exact commands; and
``pb worker workspace-report`` says whether each clone commits with it, setting
it first with ``--set-identity``.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from project_board.client import cli, host_config, relay
from project_board.client.store import SharedFieldStore
from project_board.client.workspace_report import clone_identity, commit_identity, report_signature
from project_board.contract.errors import DomainError
from test_attendance_materializes_project import _fresh_host
from test_workspace_report import ReportBoard, _cli, _git, _heartbeats, _remote

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

PROJECT_ID = "demo-project-0a1b2c3d"
EMAIL = "agents@example.com"
ALIAS = "claude-one@host"


class IdentityBoard(ReportBoard):
    """A board whose project row carries a commit email, under the preset revision."""

    def __init__(self, recipient: str, remote: Path, *, email: str | None = EMAIL, revision: int = 3) -> None:
        super().__init__(recipient, remote)
        self.email = email
        self.revision = revision

    async def action(self, *, object_ref: str, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        answer = await super().action(object_ref=object_ref, action=action, payload=payload)
        if action == "worker.heartbeat" and (payload or {}).get("project_ref"):
            project = answer["object"]["assignment_project"]
            project["repositories_revision"] = self.revision
            if self.email is None:
                project.pop("commit_identity_email", None)
            else:
                project["commit_identity_email"] = self.email
        return answer


def _host(tmp_path, monkeypatch):
    host, identity, field, config = _fresh_host(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    host_config.enroll_worker_channel(
        host.path, identity=identity, profile="problem-board-codex-one", authorized=True,
        working_directory=str(workspace), worker_alias=ALIAS,
    )
    monkeypatch.setenv("PROBLEM_BOARD_CONFIG", str(host.path))
    return identity, field, config, workspace


def _config(folder: Path, key: str) -> str:
    found = subprocess.run(["git", "config", "--local", "--get", key], cwd=str(folder), capture_output=True, text=True)
    return found.stdout.strip()


def test_the_relay_keeps_the_email_and_a_host_that_predates_it_takes_it_at_the_same_revision(tmp_path):
    field = SharedFieldStore(tmp_path / "field")
    field.create_project(project_id=PROJECT_ID, title="Demo", goal="Demo", owner="control-plane")
    listed = [{"alias": "applications", "url": "git@github.com:kdcube/applications.git", "role": "journal"}]

    # A board older than the field: no email, and the record says so.
    assert field.sync_project_repositories(PROJECT_ID, listed, revision=3) is True
    assert field.read_project_repositories(PROJECT_ID)["commit_identity_email"] == ""
    # A client that learns the field later, at the same revision, writes it once.
    assert field.sync_project_repositories(PROJECT_ID, listed, revision=3, commit_identity_email=EMAIL) is True
    assert field.read_project_repositories(PROJECT_ID)["commit_identity_email"] == EMAIL
    assert field.sync_project_repositories(PROJECT_ID, listed, revision=3, commit_identity_email=EMAIL) is False
    # A board without the field never clears what the host holds at that revision.
    assert field.sync_project_repositories(PROJECT_ID, listed, revision=3) is False
    # An older revision never replaces a newer one.
    assert field.sync_project_repositories(PROJECT_ID, listed, revision=2, commit_identity_email="") is False
    assert field.sync_project_repositories(PROJECT_ID, listed, revision=4, commit_identity_email="") is True
    assert field.read_project_repositories(PROJECT_ID)["commit_identity_email"] == ""


def test_context_names_the_identity_and_the_commands_for_every_clone():
    listed = [{"alias": "applications"}, {"alias": "app-ecosystem"}, {"alias": ""}]
    identity = commit_identity(ALIAS, EMAIL, "/work/agent one", listed)
    assert (identity["name"], identity["email"]) == (ALIAS, EMAIL)
    assert identity["commands"] == [
        "git -C '/work/agent one/applications' config user.name claude-one@host",
        "git -C '/work/agent one/applications' config user.email agents@example.com",
        "git -C '/work/agent one/app-ecosystem' config user.name claude-one@host",
        "git -C '/work/agent one/app-ecosystem' config user.email agents@example.com",
    ]
    # The project sets none, or the agent has no alias: nothing to set.
    assert commit_identity(ALIAS, "", "/work", listed) == {}
    assert commit_identity("", EMAIL, "/work", listed) == {}


def test_a_clone_matches_differs_or_is_unset_and_set_identity_writes_only_its_own_config(tmp_path, monkeypatch):
    remote = _remote(tmp_path / "remotes", "applications")
    clone = tmp_path / "applications"
    _git("clone", "-q", str(remote), str(clone), cwd=tmp_path)
    # No identity anywhere: global and system config are out of reach.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    assert clone_identity(clone, ALIAS, EMAIL) == "unset"
    _git("config", "user.name", "Someone", cwd=clone)
    _git("config", "user.email", "someone@example.com", cwd=clone)
    assert clone_identity(clone, ALIAS, EMAIL) == "differs"
    assert clone_identity(clone, ALIAS, EMAIL, set_identity=True) == "matches"
    assert (_config(clone, "user.name"), _config(clone, "user.email")) == (ALIAS, EMAIL)
    assert not (tmp_path / "no-global").exists()
    # A worktree inherits the clone's identity.
    _git("worktree", "add", "-q", "-b", "work/w1", str(tmp_path / "wt"), cwd=clone)
    assert clone_identity(tmp_path / "wt", ALIAS, EMAIL) == "matches"


def test_workspace_report_flags_each_clone_and_the_board_hears_it(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    identity, field, config, workspace = _host(tmp_path, monkeypatch)
    remote = _remote(tmp_path / "remotes", "applications")
    board = IdentityBoard(identity.worker_name, remote)
    adapter = relay.ProblemBoardHostRelayAdapter(config=config, field=field, client=board)
    asyncio.run(adapter.poll_attendances_once())
    assert field.read_project_repositories(PROJECT_ID)["commit_identity_email"] == EMAIL

    _git("clone", "-q", str(remote), str(workspace / "applications"), cwd=tmp_path)
    context = _cli(identity, "context", "--project-ref", "work:project:" + PROJECT_ID)
    assert context["commit_identity"]["name"] == ALIAS and context["commit_identity"]["email"] == EMAIL
    assert f"git -C {workspace / 'applications'} config user.email agents@example.com" in context["commit_identity"]["commands"]

    before = _cli(identity, "workspace-report", "--no-verify")
    assert before["commit_identity"] == {"name": ALIAS, "email": EMAIL}
    # kdcube is not cloned: unreachable, and no identity to read.
    assert [(row["alias"], row["state"], row.get("identity")) for row in before["repositories"]] == [
        ("applications", "cloned", "unset"),
        ("kdcube", "unreachable", None),
    ]
    after = _cli(identity, "workspace-report", "--no-verify", "--set-identity")
    assert after["repositories"][0]["identity"] == "matches"
    assert _config(workspace / "applications", "user.email") == EMAIL
    assert report_signature(before) != report_signature(after)

    asyncio.run(adapter.poll_attendances_once())
    carried = _heartbeats(board)[-1]["workspace_report"]
    assert [row.get("identity") for row in carried["repositories"]] == ["matches", None]


def test_without_a_project_email_nothing_is_read_set_or_flagged(tmp_path, monkeypatch):
    identity, field, config, workspace = _host(tmp_path, monkeypatch)
    remote = _remote(tmp_path / "remotes", "applications")
    board = IdentityBoard(identity.worker_name, remote, email=None)
    adapter = relay.ProblemBoardHostRelayAdapter(config=config, field=field, client=board)
    asyncio.run(adapter.poll_attendances_once())
    _git("clone", "-q", str(remote), str(workspace / "applications"), cwd=tmp_path)

    context = _cli(identity, "context", "--project-ref", "work:project:" + PROJECT_ID)
    assert context["commit_identity"] == {}
    report = _cli(identity, "workspace-report", "--no-verify")
    assert "commit_identity" not in report
    assert all("identity" not in row for row in report["repositories"])
    with pytest.raises(DomainError) as refused:
        _cli(identity, "workspace-report", "--no-verify", "--set-identity")
    assert refused.value.code == "field_commit_identity_unset"


def test_the_procedure_sets_it_in_every_existing_clone_now():
    procedure = " ".join(
        (
            Path(__file__).resolve().parents[1]
            / "src/project_board/procedures/problem-board-worker/references/project-workspace.md"
        ).read_text(encoding="utf-8").split()
    )
    assert "Set it in every clone you already have, now, not only after a fresh clone" in procedure
    assert "pb worker workspace-report --set-identity" in procedure
    assert "Never commit with an email you made up" in procedure
    assert "reads `matches`, `differs` or `unset` for its commit identity" in procedure


# --- W371 review, line 4: the owner's My Card email once their GitHub key answers ---

OWNER_EMAIL = "123+owner@users.noreply.github.com"


def test_context_and_the_workspace_report_give_the_owners_email_once_the_key_answered(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    identity, field, config, workspace = _host(tmp_path, monkeypatch)
    remote = _remote(tmp_path / "remotes", "applications")
    board = IdentityBoard(identity.worker_name, remote)
    asyncio.run(relay.ProblemBoardHostRelayAdapter(config=config, field=field, client=board).poll_attendances_once())
    _git("clone", "-q", str(remote), str(workspace / "applications"), cwd=tmp_path)

    # Before the key has answered: the project's email, named as the deploy-key fallback.
    before = _cli(identity, "context", "--project-ref", "work:project:" + PROJECT_ID)["commit_identity"]
    assert before["email"] == EMAIL and before["source"] == "project"
    assert "deploy key" in before["source_note"]

    # The key answered (connect-project, the credential helper or gh recorded it).
    field.record_github_identity(identity.worker_name, PROJECT_ID, login="owner", commit_email=OWNER_EMAIL)
    after = _cli(identity, "context", "--project-ref", "work:project:" + PROJECT_ID)["commit_identity"]
    assert after["email"] == OWNER_EMAIL and after["source"] == "owner_github_key"
    assert f"git -C {workspace / 'applications'} config user.email {OWNER_EMAIL}" in after["commands"]

    report = _cli(identity, "workspace-report", "--project-ref", "work:project:" + PROJECT_ID, "--set-identity")
    assert report["commit_identity"] == {"name": ALIAS, "email": OWNER_EMAIL}
    assert _config(workspace / "applications", "user.email") == OWNER_EMAIL


def test_issuing_the_key_records_the_owners_login_and_email_never_the_token(tmp_path, monkeypatch):
    from project_board.client.github_key import GitHubToken

    identity, field, config, workspace = _host(tmp_path, monkeypatch)
    remote = _remote(tmp_path / "remotes", "applications")
    board = IdentityBoard(identity.worker_name, remote)
    asyncio.run(relay.ProblemBoardHostRelayAdapter(config=config, field=field, client=board).poll_attendances_once())
    field.sync_project_repositories(
        PROJECT_ID,
        [{"alias": "applications", "url": "git@github.com:example-org/applications.git", "role": "work"}],
        revision=9,
        commit_identity_email=EMAIL,
    )

    async def issue(self, repository):
        return GitHubToken(token="ghu_secret", expires_at=0, login="owner", commit_email=OWNER_EMAIL, repository=repository)

    monkeypatch.setattr(cli._GitHubKeySession, "_issue", issue)  # noqa: SLF001
    args = type("Args", (), {"runtime_kind": identity.runtime_kind, "runtime_session_id": identity.runtime_session_id,
                             "config": None, "project_ref": "work:project:" + PROJECT_ID})()
    cli._GitHubKeySession(args).token("example-org/applications")  # noqa: SLF001

    recorded = field.read_github_identity(identity.worker_name, PROJECT_ID)
    assert (recorded["login"], recorded["commit_email"]) == ("owner", OWNER_EMAIL)
    assert "ghu_secret" not in str(recorded)

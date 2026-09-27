"""A project file edited on the card is applied in the coordinator's clone (W370).

The board never writes the file. The coordinator's relay makes the edit in a
worktree from origin/<branch>, refuses it when the file changed since the
editor opened it, commits as the coordinator's project identity naming the
person, and pushes by the repository's policy: a direct commit for a local
repository, a pull request (or a pushed branch without gh) for a remote one.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from project_board.client import project_file_edit
from project_board.client.project_file_edit import apply_file_edit, commit_message, default_policy
from test_workspace_report import _git

IDENTITY = {"author_name": "claude-coord@host", "author_email": "agents@example.com"}


def _out(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True).stdout.strip()


def _local_repository(root: Path) -> Path:
    """A non-bare repository that accepts pushes to its checked-out branch, like a local-only project's."""

    repo = root / "operator" / "ledger"
    repo.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "receive.denyCurrentBranch", "updateInstead", cwd=repo)
    (repo / "facts.md").write_text("# Facts\n", encoding="utf-8")
    _git("add", "facts.md", cwd=repo)
    _git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "seed", cwd=repo)
    return repo


def _clone(source: Path, workspace: Path, alias: str) -> Path:
    workspace.mkdir(parents=True, exist_ok=True)
    _git("clone", "-q", str(source), str(workspace / alias), cwd=workspace)
    return workspace / alias


def test_policies_default_by_where_the_repository_is():
    assert default_policy("/srv/operator/ledger") == "direct"
    assert default_policy("file:///srv/operator/ledger") == "direct"
    assert default_policy("git@github.com:kdcube/applications.git") == "pull_request"
    assert commit_message("facts.md", "Ana") == "Project file facts.md: edited by Ana on the board"


def test_a_local_repository_gets_a_direct_commit_as_the_coordinator_naming_the_person(tmp_path):
    source = _local_repository(tmp_path)
    workspace = tmp_path / "workspace"
    clone = _clone(source, workspace, "ledger")
    base = _out("rev-parse", "HEAD", cwd=clone)

    result = apply_file_edit(
        workspace=str(workspace), alias="ledger", path="facts.md", url=str(source), branch="main",
        base_commit=base, content="# Facts\n\nThe deadline is the 10th.\n", requested_by="Ana",
        edit_id="e1", **IDENTITY,
    )
    assert result["outcome"] == "committed" and result["policy"] == "direct"
    # updateInstead: the operator's own checkout now holds the edit.
    assert (source / "facts.md").read_text(encoding="utf-8").endswith("The deadline is the 10th.\n")
    assert _out("log", "-1", "--format=%an <%ae>|%s|%b", cwd=source) == (
        "claude-coord@host <agents@example.com>|Project file facts.md: edited by Ana on the board|Requested-by: Ana"
    )
    assert _out("rev-parse", "HEAD", cwd=source) == result["commit"]
    # The clean clone was never written, and the worktree is gone.
    assert _out("status", "--porcelain", cwd=clone) == ""
    assert not (workspace / "wt" / "file-edit-e1").exists()
    assert "file-edit-e1" not in _out("worktree", "list", cwd=clone)

    # The same content again changes nothing.
    again = apply_file_edit(
        workspace=str(workspace), alias="ledger", path="facts.md", url=str(source), branch="main",
        base_commit=result["commit"], content="# Facts\n\nThe deadline is the 10th.\n", requested_by="Ana",
        edit_id="e2", **IDENTITY,
    )
    assert again["outcome"] == "unchanged"


def test_a_file_changed_since_it_was_opened_is_refused_never_merged(tmp_path):
    source = _local_repository(tmp_path)
    workspace = tmp_path / "workspace"
    clone = _clone(source, workspace, "ledger")
    opened_at = _out("rev-parse", "HEAD", cwd=clone)
    (source / "facts.md").write_text("# Facts\n\nSomeone else's ruling.\n", encoding="utf-8")
    _git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "other", cwd=source)

    result = apply_file_edit(
        workspace=str(workspace), alias="ledger", path="facts.md", url=str(source), branch="main",
        base_commit=opened_at, content="# Facts\n\nMine.\n", requested_by="Ana", edit_id="e3", **IDENTITY,
    )
    assert result == {
        "outcome": "refused", "reason_code": "changed_since_opened",
        "reason": "changed since you opened it; reopen", "policy": "direct",
    }
    assert "Someone else's ruling." in (source / "facts.md").read_text(encoding="utf-8")
    unknown = apply_file_edit(
        workspace=str(workspace), alias="ledger", path="facts.md", url=str(source), branch="main",
        base_commit="0" * 40, content="x\n", requested_by="Ana", edit_id="e4", **IDENTITY,
    )
    assert unknown["reason_code"] == "changed_since_opened"


def test_a_remote_repository_gets_a_pull_request_or_a_pushed_branch_without_gh(tmp_path):
    bare = tmp_path / "remote.git"
    seed = _local_repository(tmp_path)
    _git("clone", "-q", "--bare", str(seed), str(bare), cwd=tmp_path)
    workspace = tmp_path / "workspace"
    clone = _clone(bare, workspace, "applications")
    base = _out("rev-parse", "HEAD", cwd=clone)
    url = "git@github.com:kdcube/applications.git"
    seen: list[list[str]] = []

    def with_gh(argv, cwd, timeout):
        if argv[0] == "gh":
            seen.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "Creating pull request\nhttps://github.com/kdcube/applications/pull/999\n", "")
        return project_file_edit._run(argv, cwd, timeout)  # noqa: SLF001

    opened = apply_file_edit(
        workspace=str(workspace), alias="applications", path="facts.md", url=url, branch="main",
        base_commit=base, content="# Facts\n\nNew.\n", requested_by="Ana", edit_id="e5", run=with_gh, **IDENTITY,
    )
    assert opened["outcome"] == "pr_opened" and opened["policy"] == "pull_request"
    assert opened["pr_url"] == "https://github.com/kdcube/applications/pull/999"
    assert opened["branch"] == "file-edit/e5" and opened["base"] == "main"
    assert _out("rev-parse", "refs/heads/file-edit/e5", cwd=bare) == opened["commit"]
    # main itself is untouched: the coordinator merges the pull request.
    assert _out("rev-parse", "refs/heads/main", cwd=bare) == base
    assert seen[0][:9] == ["gh", "pr", "create", "--repo", "kdcube/applications", "--base", "main", "--head", "file-edit/e5"]

    def without_gh(argv, cwd, timeout):
        if argv[0] == "gh":
            raise FileNotFoundError("gh")
        return project_file_edit._run(argv, cwd, timeout)  # noqa: SLF001

    pushed = apply_file_edit(
        workspace=str(workspace), alias="applications", path="facts.md", url=url, branch="main",
        base_commit=base, content="# Facts\n\nOther.\n", requested_by="Ana", edit_id="e6", run=without_gh, **IDENTITY,
    )
    assert pushed["outcome"] == "branch_pushed"
    assert pushed["compare_url"] == "https://github.com/kdcube/applications/compare/main...file-edit/e6"
    assert "gh is not available" in pushed["reason"] and pushed["reason_code"] == "gh_unavailable"


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"author_email": ""}, "commit_identity_unset"),
        ({"edit_id": "../x"}, "edit_invalid"),
        ({"alias": "missing"}, "repository_not_cloned"),
        ({"content": "x" * (project_file_edit.FILE_EDIT_MAX_BYTES + 1)}, "file_too_large"),
    ],
)
def test_what_is_refused_before_git_runs(tmp_path, changes, code):
    source = _local_repository(tmp_path)
    workspace = tmp_path / "workspace"
    clone = _clone(source, workspace, "ledger")
    request = {
        "workspace": str(workspace), "alias": "ledger", "path": "facts.md", "url": str(source), "branch": "main",
        "base_commit": _out("rev-parse", "HEAD", cwd=clone), "content": "x\n", "requested_by": "Ana",
        "edit_id": "e7", **IDENTITY, **changes,
    }
    assert apply_file_edit(**request)["reason_code"] == code


def test_one_more_control_kind_is_added_without_replacing_the_list(tmp_path):
    """The first opt-in text used --allow-control-kind, which replaces the whole list."""

    from test_attendance_materializes_project import _fresh_host
    from project_board.client import cli, host_config

    host, *_ = _fresh_host(tmp_path)
    before = set(host_config.HostRelayConfig.load(host.path).allowed_control_kinds)
    assert {"mail", "ping"} <= before and "project.file.edit" not in before
    host_config.update_host_config(host.path, add_control_kinds=["project.file.edit"])
    assert set(host_config.HostRelayConfig.load(host.path).allowed_control_kinds) == before | {"project.file.edit"}
    host_config.update_host_config(host.path, remove_control_kinds=["project.file.edit"])
    assert set(host_config.HostRelayConfig.load(host.path).allowed_control_kinds) == before
    parsed = cli.build_parser().parse_args(["host", "configure", "--add-control-kind", "project.file.edit"])
    assert parsed.add_control_kind == ["project.file.edit"] and parsed.allow_control_kind is None


def test_the_coordinators_relay_applies_an_edit_and_reports_the_result_to_the_board(tmp_path, monkeypatch):
    import asyncio
    import dataclasses
    import hashlib

    from project_board.client import relay
    from project_board.client.io import content_hash
    from test_commit_identity import _host

    identity, field, config, workspace = _host(tmp_path, monkeypatch)
    source = _local_repository(tmp_path / "remote-side")
    field.create_project(project_id="demo-project-0a1b2c3d", title="Demo", goal="Demo", owner="control-plane")
    field.sync_project_repositories(
        "demo-project-0a1b2c3d", [{"alias": "ledger", "url": str(source), "role": "work"}], revision=1,
        commit_identity_email="agents@example.com",
    )
    field.sync_project_files(
        "demo-project-0a1b2c3d", files=[{"purpose": "facts", "alias": "ledger", "path": "facts.md"}], revision=1
    )
    clone = _clone(source, workspace, "ledger")

    class Client:
        def __init__(self):
            self.calls = []

        async def action(self, *, object_ref, action, payload=None):
            self.calls.append({"object_ref": object_ref, "action": action, "payload": dict(payload or {})})
            return {"ok": True, "object": {}}

    client = Client()
    scoped = relay.ProblemBoardHostRelayAdapter(
        config=dataclasses.replace(config, project_id="demo-project-0a1b2c3d", workspace=str(workspace), worker_alias="claude-coord@host"),
        field=field,
        client=client,
    )
    content = "# Facts\n\nEdited on the card.\n"

    def control(**changes):
        payload = {
            "edit_ref": "work:file_edit:20260928T000000Z:edit_0123456789abcdef0123456789abcdef:facts",
            "alias": "ledger", "path": "facts.md", "branch": "main", "file_edits": "direct",
            "base_commit": _out("rev-parse", "HEAD", cwd=clone), "content": content,
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "requested_by": {"label": "Ana", "subject": "user-1"},
        }
        payload.update(changes)
        return {"kind": "project.file.edit", "project_ref": "work:project:demo-project-0a1b2c3d",
                "payload": payload, "payload_hash": content_hash(payload)}

    edit_ref, summary = asyncio.run(scoped._serve_file_edit(control()))  # noqa: SLF001
    [call] = client.calls
    assert call["object_ref"] == edit_ref and call["action"] == "project.file.edit.result"
    result = call["payload"]
    assert result["outcome"] == "committed" and len(result["commit"]) == 40 and result["reason_code"] == ""
    assert set(result) == {"edit_ref", "outcome", "commit", "pr_url", "reason", "reason_code"}
    assert (source / "facts.md").read_text(encoding="utf-8") == content
    assert _out("log", "-1", "--format=%an <%ae>", cwd=source) == "claude-coord@host <agents@example.com>"
    assert summary == "File edit committed"

    def outcome(**changes):
        client.calls.clear()
        asyncio.run(scoped._serve_file_edit(control(**changes)))  # noqa: SLF001
        return client.calls[0]["payload"]["outcome"], client.calls[0]["payload"]["reason_code"]

    assert outcome(path="README.md") == ("refused", "file_not_listed")
    assert outcome(content_sha256="0" * 64) == ("refused", "edit_invalid")
    assert outcome(base_commit=_out("rev-parse", "HEAD", cwd=source)) == ("unchanged", "")

    # The kind is a write: a host accepts it only after the explicit opt-in.
    request = {**control(), "sender": "control-plane"}
    assert scoped._receiver_refusal(request) == "receiver_policy_control_kind_denied"  # noqa: SLF001
    opted = relay.ProblemBoardHostRelayAdapter(
        config=dataclasses.replace(scoped.config, allowed_control_kinds=(*scoped.config.allowed_control_kinds, "project.file.edit")),
        field=field, client=client,
    )
    assert opted._receiver_refusal(request) != "receiver_policy_control_kind_denied"  # noqa: SLF001


def test_the_procedures_say_where_card_edits_go_and_how_a_host_opts_in():
    from project_board.client.procedures import source_package_path

    root = source_package_path()
    workspace = " ".join((root / "references" / "project-workspace.md").read_text(encoding="utf-8").split())
    coordinator = " ".join((root / "references" / "coordinator.md").read_text(encoding="utf-8").split())
    assert "A person may also edit a project file on the card. The board never writes it" in workspace
    assert "refuses it when the file changed since the person opened it" in workspace
    assert "**Edits made on the card come to you.**" in coordinator
    assert "`pb host configure --add-control-kind project.file.edit`" in coordinator
    assert "Never `--allow-control-kind` for this: it replaces the whole list" in coordinator

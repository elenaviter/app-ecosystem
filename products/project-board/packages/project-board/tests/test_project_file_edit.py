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
    # gh is asked whether it is signed in first, then opens the pull request.
    assert seen[0][:3] == ["gh", "auth", "status"]
    assert seen[1][:9] == ["gh", "pr", "create", "--repo", "kdcube/applications", "--base", "main", "--head", "file-edit/e5"]

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


def test_a_file_edit_ref_is_a_contract_kind_that_maps_to_the_result_operation():
    from project_board.contract.reference_records import reference_for_record
    from project_board.contract.refs import parse_ref
    from project_board.contract.worker_operation_contract import PROBLEM_BOARD_OPERATIONS_BY_KIND

    edit_ref = reference_for_record(
        "file_edit",
        {"edit_id": "edit_0123456789abcdef0123456789abcdef", "requested_at": "2026-09-28T00:10:00Z",
         "path": "docs/facts.md", "alias": "ledger"},
    )
    assert edit_ref.startswith("work:file_edit:")
    assert parse_ref(edit_ref).kind == "file_edit"
    assert PROBLEM_BOARD_OPERATIONS_BY_KIND["work.file_edit"] == ("project.file.edit.result",)


def test_the_edit_opt_in_is_named_where_a_coordinator_machine_is_set_up_or_handed_over():
    """Operator, 2026-09-28: the opt-in runs on any machine where a project coordinator runs."""

    from pathlib import Path

    from project_board.client.procedures import source_package_path

    root = source_package_path()
    command = "`pb host configure --add-control-kind project.file.edit`"

    def text(path) -> str:
        return " ".join(Path(path).read_text(encoding="utf-8").split())

    host = text(root.parent / "add-a-worker-host.md")
    assert "**If a project coordinator will run on this machine** (optional)" in host and command in host
    assert "one run covers every project coordinated here" in host
    coordinator = text(root / "references" / "coordinator.md")
    assert "4. Check that your machine accepts project-file edits made on the card" in coordinator
    first_run = text(root / "references" / "first-run.md")
    assert "If this agent will coordinate the project, its machine needs one opt-in" in first_run
    readme = text(Path(__file__).resolve().parents[1] / "README.md")
    assert "The machine where a project's coordinator runs applies the file edits people make on the board" in readme
    assert command in readme


def test_gh_signed_out_under_the_relay_service_is_named_with_its_fix(tmp_path):
    """First use, 2026-09-28: the launchd relay could not read the login keychain, so no pull request."""

    bare = tmp_path / "remote.git"
    seed = _local_repository(tmp_path)
    _git("clone", "-q", "--bare", str(seed), str(bare), cwd=tmp_path)
    workspace = tmp_path / "workspace"
    clone = _clone(bare, workspace, "applications")

    def keychain_locked(argv, cwd, timeout):
        if argv[:3] == ["gh", "auth", "status"]:
            return subprocess.CompletedProcess(argv, 1, "", "You are not logged into any GitHub hosts. To log in, run: gh auth login\n")
        if argv[0] == "gh":
            raise AssertionError("no pull request is attempted when gh is signed out")
        return project_file_edit._run(argv, cwd, timeout)  # noqa: SLF001

    result = apply_file_edit(
        workspace=str(workspace), alias="applications", path="facts.md", url="git@github.com:kdcube/applications.git",
        branch="main", base_commit=_out("rev-parse", "HEAD", cwd=clone), content="# Facts\n\nX.\n",
        requested_by="Ana", edit_id="e9", run=keychain_locked, **IDENTITY,
    )
    assert result["outcome"] == "branch_pushed" and result["reason_code"] == "gh_unavailable"
    assert result["reason"].startswith("gh is not signed in for the relay service: You are not logged into any GitHub hosts.")
    assert "the coordinator opens the pull request" in result["reason"]
    assert result["compare_url"].endswith("/compare/main...file-edit/e9")


def test_an_applied_edit_the_board_refuses_reaches_the_coordinator_once_on_receive(tmp_path, monkeypatch):
    """First use, 2026-09-28: the Card withheld the result, so the board never mailed the coordinator."""

    import asyncio
    import dataclasses
    import hashlib

    from project_board.client import relay
    from project_board.client.io import content_hash
    from project_board.contract.errors import DomainError
    from test_commit_identity import _host
    from test_workspace_report import _cli

    identity, field, config, workspace = _host(tmp_path, monkeypatch)
    source = _local_repository(tmp_path / "remote-side")
    project_id = "demo-project-0a1b2c3d"
    field.create_project(project_id=project_id, title="Demo", goal="Demo", owner="control-plane")
    field.sync_project_repositories(
        project_id, [{"alias": "ledger", "url": str(source), "role": "work"}], revision=1,
        commit_identity_email="agents@example.com",
    )
    field.sync_project_files(project_id, files=[{"purpose": "facts", "alias": "ledger", "path": "facts.md"}], revision=1)
    clone = _clone(source, workspace, "ledger")

    class RefusingBoard:
        async def action(self, *, object_ref, action, payload=None):
            raise DomainError("work_worker_operation_withheld_by_control_card", "withheld")

    scoped = relay.ProblemBoardHostRelayAdapter(
        config=dataclasses.replace(config, project_id=project_id, workspace=str(workspace), worker_alias="claude-coord@host"),
        field=field, client=RefusingBoard(),
    )
    content = "# Facts\n\nEdited on the card.\n"
    payload = {
        "edit_ref": "work:file_edit:20260928T095000Z:edit_0123456789abcdef0123456789abcdef:facts",
        "alias": "ledger", "path": "facts.md", "branch": "main", "file_edits": "direct",
        "base_commit": _out("rev-parse", "HEAD", cwd=clone), "content": content,
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "requested_by": {"label": "Ana", "subject": "user-1"},
    }
    control = {"kind": "project.file.edit", "project_ref": f"work:project:{project_id}", "payload": payload, "payload_hash": content_hash(payload)}
    _, summary = asyncio.run(scoped._serve_file_edit(control))  # noqa: SLF001
    assert summary == "File edit committed; the result was not accepted: work_worker_operation_withheld_by_control_card"

    [notice] = field.take_file_edit_notices(identity.worker_name, project_id)
    assert notice["outcome"] == "committed" and notice["requested_by"] == "Ana" and notice["path"] == "ledger:facts.md"
    assert notice["not_accepted"] == "work_worker_operation_withheld_by_control_card" and len(notice["commit"]) == 40
    # Taken once: the next take is empty.
    assert field.take_file_edit_notices(identity.worker_name, project_id) == []

    # Through receive: a second refused edit shows as a signal once, then not again.
    from project_board.client import cli, host_config

    second = {**payload, "edit_ref": payload["edit_ref"] + "-2", "content": "# Facts\n\nAgain.\n",
              "content_sha256": hashlib.sha256(b"# Facts\n\nAgain.\n").hexdigest(),
              "base_commit": _out("rev-parse", "HEAD", cwd=source)}
    asyncio.run(scoped._serve_file_edit({**control, "payload": second, "payload_hash": content_hash(second)}))  # noqa: SLF001
    host = host_config.HostRelayConfig.load(monkeypatch_env_config())
    received = {"signals": [], "projects": [{"project_ref": f"work:project:{project_id}"}]}
    first = cli._with_project_files_signals(dict(received), host, field, identity)  # noqa: SLF001
    edited = [signal for signal in first.get("signals") or [] if signal["kind"] == "project.file.edited"]
    assert len(edited) == 1 and edited[0]["requested_by"] == "Ana" and edited[0]["outcome"] == "committed"
    assert edited[0]["not_accepted"] == "work_worker_operation_withheld_by_control_card"
    assert "the board did not accept its result" in edited[0]["next"]
    later = cli._with_project_files_signals(dict(received), host, field, identity)  # noqa: SLF001
    assert not [signal for signal in later.get("signals") or [] if signal["kind"] == "project.file.edited"]


def monkeypatch_env_config() -> str:
    import os

    return os.environ["PROBLEM_BOARD_CONFIG"]


def test_the_procedures_name_the_service_sign_in_and_the_fallback_signal():
    from pathlib import Path

    from project_board.client.procedures import source_package_path

    root = source_package_path()
    host = " ".join((root.parent / "add-a-worker-host.md").read_text(encoding="utf-8").split())
    coordinator = " ".join((root / "references" / "coordinator.md").read_text(encoding="utf-8").split())
    # W371: the relay opens the edit's pull request under the coordinator
    # owner's key; gh is installed, found by absolute path, never signed in
    # with a token file for the service.
    assert "**Card edits on the coordinator's machine.**" in host
    assert "--insecure-storage" not in host and "No token is stored in a file for the service" in host
    assert "so `gh` must be installed on that machine" in host
    assert "`SIGNAL project.file.edited` once" in coordinator
    assert "would refuse mail, requests and pings. When the board does not accept" in coordinator


def test_with_the_owners_github_key_the_edit_pushes_over_https_and_gh_needs_no_sign_in(tmp_path):
    """W371: the coordinator's card edits push and open the pull request with its owner's GitHub key."""

    import base64
    from types import SimpleNamespace

    bare = tmp_path / "remote.git"
    seed = _local_repository(tmp_path)
    _git("clone", "-q", "--bare", str(seed), str(bare), cwd=tmp_path)
    workspace = tmp_path / "workspace"
    clone = _clone(bare, workspace, "app-ecosystem")
    https = "https://github.com/example-org/app-ecosystem.git"
    calls: list[tuple[list[str], dict]] = []

    def keyed(argv, cwd, timeout, extra_env=None):
        calls.append((list(argv), dict(extra_env or {})))
        if argv[0] == "gh":
            return subprocess.CompletedProcess(argv, 0, "https://github.com/example-org/app-ecosystem/pull/7\n", "")
        # github.com over HTTPS is played by the bare repository.
        return project_file_edit._run([str(bare) if part == https else part for part in argv], cwd, timeout)  # noqa: SLF001

    token = SimpleNamespace(token="ghu_owner", commit_email="owner@example.test")
    result = apply_file_edit(
        workspace=str(workspace), alias="app-ecosystem", path="facts.md", url="git@github.com:example-org/app-ecosystem.git",
        branch="main", base_commit=_out("rev-parse", "HEAD", cwd=clone), content="# Facts\n\nKeyed.\n",
        requested_by="Ana", edit_id="k1", run=keyed, github_token=token, **IDENTITY,
    )

    assert result["outcome"] == "pr_opened" and result["pr_url"].endswith("/pull/7")
    push = next(argv for argv, _ in calls if argv[:2] == ["git", "push"])
    assert push[2] == https, "the push goes over HTTPS with the key"
    assert all("ghu_owner" not in " ".join(argv) for argv, _ in calls), "the token is never on a command line"
    env = next(env for argv, env in calls if argv[:2] == ["git", "push"])
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert env["GIT_CONFIG_VALUE_0"] == "AUTHORIZATION: basic " + base64.b64encode(b"x-access-token:ghu_owner").decode()
    gh_calls = [(argv, env) for argv, env in calls if argv[0] == "gh"]
    assert [argv[:3] for argv, _ in gh_calls] == [["gh", "pr", "create"]], "no gh sign-in check"
    assert gh_calls[0][1]["GH_TOKEN"] == "ghu_owner"
    author = _out("log", "-1", "--format=%an <%ae>", result["commit"], cwd=bare)
    assert author == "claude-coord@host <owner@example.test>", "the commit carries the owner's My Card email"
    assert "ghu_owner" not in (clone / ".git" / "config").read_text(encoding="utf-8")


def test_the_relay_asks_its_cards_github_key_for_a_github_repository_and_goes_on_without_it(tmp_path, monkeypatch):
    """W371: the relay's Card asks Connection Hub for the owner's key; a refusal leaves the old path."""

    import asyncio
    import dataclasses
    import hashlib

    from project_board.client import relay
    from project_board.client.github_key import GitHubKeyRefused
    from project_board.client.io import content_hash
    from test_commit_identity import _host

    identity, field, config, workspace = _host(tmp_path, monkeypatch)
    field.create_project(project_id="demo-project-0a1b2c3d", title="Demo", goal="Demo", owner="control-plane")
    field.sync_project_repositories(
        "demo-project-0a1b2c3d",
        [{"alias": "app-ecosystem", "url": "git@github.com:example-org/app-ecosystem.git", "role": "work"}],
        revision=1,
        commit_identity_email="agents@example.com",
    )
    field.sync_project_files(
        "demo-project-0a1b2c3d", files=[{"purpose": "facts", "alias": "app-ecosystem", "path": "facts.md"}], revision=1
    )
    applied: list[dict] = []
    monkeypatch.setattr(relay, "apply_file_edit", lambda **kwargs: applied.append(kwargs) or {"outcome": "pr_opened"})

    class Client:
        def __init__(self, refuse: bool):
            self.refuse = refuse
            self.asked = []

        async def github_key(self, project_ref, repository):
            self.asked.append((project_ref, repository))
            if self.refuse:
                raise GitHubKeyRefused("github_not_linked", "Your owner has not connected GitHub on this project.")
            return "the-owners-token"

        async def action(self, *, object_ref, action, payload=None):
            return {"ok": True, "object": {}}

    content = "# Facts\n"
    payload = {
        "edit_ref": "work:file_edit:20260928T000000Z:edit_0123456789abcdef0123456789abcdef:facts",
        "alias": "app-ecosystem", "path": "facts.md", "branch": "main", "file_edits": "pull_request",
        "base_commit": "0" * 40, "content": content,
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "requested_by": {"label": "Ana", "subject": "user-1"},
    }
    control = {"kind": "project.file.edit", "project_ref": "work:project:demo-project-0a1b2c3d",
               "payload": payload, "payload_hash": content_hash(payload)}

    for refuse, expected in ((False, "the-owners-token"), (True, None)):
        client = Client(refuse)
        scoped = relay.ProblemBoardHostRelayAdapter(
            config=dataclasses.replace(config, project_id="demo-project-0a1b2c3d", workspace=str(workspace), worker_alias="claude-coord@host"),
            field=field,
            client=client,
        )
        asyncio.run(scoped._serve_file_edit(control))  # noqa: SLF001
        assert client.asked == [("work:project:demo-project-0a1b2c3d", "example-org/app-ecosystem")]
        assert applied[-1]["github_token"] == expected


def test_the_relay_runs_gh_by_absolute_path_and_names_where_it_looked_when_it_is_missing(tmp_path):
    """W371 line 5: the edit pushed under the owner's key but gh was not on the service PATH."""

    from types import SimpleNamespace

    bare = tmp_path / "remote.git"
    seed = _local_repository(tmp_path)
    _git("clone", "-q", "--bare", str(seed), str(bare), cwd=tmp_path)
    workspace = tmp_path / "workspace"
    clone = _clone(bare, workspace, "app-ecosystem")
    https = "https://github.com/example-org/app-ecosystem.git"
    ran: list[list[str]] = []

    def keyed(argv, cwd, timeout, extra_env=None):
        ran.append(list(argv))
        if argv[0] != "git":
            return subprocess.CompletedProcess(argv, 0, "https://github.com/example-org/app-ecosystem/pull/8\n", "")
        return project_file_edit._run([str(bare) if part == https else part for part in argv], cwd, timeout)  # noqa: SLF001

    token = SimpleNamespace(token="ghu_owner", commit_email="owner@example.test")
    common = dict(
        workspace=str(workspace), alias="app-ecosystem", path="facts.md",
        url="git@github.com:example-org/app-ecosystem.git", branch="main", requested_by="Ana",
        run=keyed, github_token=token, **IDENTITY,
    )

    opened = apply_file_edit(
        **common, base_commit=_out("rev-parse", "HEAD", cwd=clone), content="# Facts\n\nOne.\n",
        edit_id="g1", gh_path="/opt/homebrew/bin/gh",
    )
    assert opened["outcome"] == "pr_opened"
    assert ["/opt/homebrew/bin/gh", "pr", "create"] == next(argv for argv in ran if argv[0] != "git")[:3]

    missing = apply_file_edit(
        **common, base_commit=_out("rev-parse", "HEAD", cwd=clone), content="# Facts\n\nTwo.\n",
        edit_id="g2", gh_path="",
    )
    assert missing["outcome"] == "branch_pushed" and missing["reason_code"] == "gh_unavailable"
    assert "/opt/homebrew/bin/gh" in missing["reason"] and "where the relay looks" in missing["reason"]

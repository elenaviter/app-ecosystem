"""The project card shows each agent's onboarding state (W337).

The relay reports which project record this host holds (the repository
list's revision and when it arrived) on every project heartbeat. The agent
reports its workspace with `pb worker workspace-report`: for each listed
repository, `<workspace>/<alias>` is a checkout of that URL whose remote
answers (verified), or it is unreachable, with a reason. The report rides the
heartbeat once per change, like the info line (W330), so a board without the
field costs at most one forced heartbeat.
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
from project_board.client.workspace_report import build_workspace_report, comparable_url, inspect_repository
from project_board.contract.errors import DomainError
from test_attendance_materializes_project import PROJECT_REF, Board, _fresh_host

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _remote(root: Path, name: str) -> Path:
    """A bare repository with one commit, standing in for the project's remote."""

    seed = root / f"{name}-seed"
    seed.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=seed)
    (seed / "README.md").write_text(name, encoding="utf-8")
    _git("add", "README.md", cwd=seed)
    _git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "seed", cwd=seed)
    bare = root / f"{name}.git"
    _git("clone", "-q", "--bare", str(seed), str(bare), cwd=root)
    return bare


def test_urls_compare_across_ssh_and_https_spellings():
    assert comparable_url("git@github.com:KDCube/applications.git") == comparable_url("https://github.com/kdcube/applications")
    assert comparable_url("ssh://git@github.com/kdcube/kdcube.git") == "github.com/kdcube/kdcube"
    assert comparable_url("git@github.com:kdcube/kdcube.git") != comparable_url("git@github.com:kdcube/applications.git")


def test_each_repository_is_verified_or_unreachable_with_its_reason(tmp_path):
    remote = _remote(tmp_path, "applications")
    other = _remote(tmp_path, "other")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git("clone", "-q", str(remote), str(workspace / "applications"), cwd=tmp_path)
    _git("clone", "-q", str(other), str(workspace / "mixed-up"), cwd=tmp_path)
    (workspace / "plain").mkdir()

    listed = [
        {"alias": "applications", "url": str(remote)},
        {"alias": "missing", "url": str(remote)},
        {"alias": "plain", "url": str(remote)},
        {"alias": "mixed-up", "url": str(remote)},
    ]
    report = build_workspace_report(workspace, listed, revision=3, reported_at="2026-09-26T03:00:00Z")
    by_alias = {row["alias"]: row for row in report["repositories"]}
    assert by_alias["applications"] == {"alias": "applications", "state": "verified", "reason": ""}
    assert by_alias["missing"]["state"] == "unreachable" and "not cloned" in by_alias["missing"]["reason"]
    assert by_alias["plain"]["state"] == "unreachable" and "not a git checkout" in by_alias["plain"]["reason"]
    assert by_alias["mixed-up"]["state"] == "unreachable" and "origin is" in by_alias["mixed-up"]["reason"]
    assert report["revision"] == 3

    # A remote that stops answering is unreachable, with git's own first line.
    shutil.rmtree(remote)
    gone = inspect_repository(workspace, listed[0])
    assert gone["state"] == "unreachable" and gone["reason"].startswith("the remote did not answer")
    assert "\n" not in gone["reason"] and len(gone["reason"]) <= 200
    # Without asking the remote, a matching checkout is only cloned.
    assert inspect_repository(workspace, listed[0], verify=False)["state"] == "cloned"


def test_a_deploy_key_ssh_alias_matches_the_listed_url(tmp_path):
    """Review on #168: add-a-worker-host step 7 clones from an SSH alias per deploy key."""

    aliases = {"github-applications": "github.com"}
    resolve = lambda host: aliases.get(host, host)  # noqa: E731 - ssh -G, injected
    listed = "git@github.com:kdcube/applications.git"
    assert comparable_url("github-applications:kdcube/applications.git", resolve_host=resolve) == comparable_url(listed, resolve_host=resolve)
    assert comparable_url("git@github-applications:kdcube/applications.git", resolve_host=resolve) == "github.com/kdcube/applications"
    assert comparable_url("ssh://git@github-applications/kdcube/applications.git", resolve_host=resolve) == "github.com/kdcube/applications"
    # Without the alias resolved it would never match: that was the defect.
    assert comparable_url("github-applications:kdcube/applications.git") != comparable_url(listed)

    class Git:
        def __call__(self, args, cwd, timeout):
            if args[:2] == ["rev-parse", "--show-toplevel"]:
                return subprocess.CompletedProcess(args, 0, f"{cwd}\n", "")
            out = {"rev-parse": "true\n", "remote": "github-applications:kdcube/applications.git\n"}.get(args[0], "")
            return subprocess.CompletedProcess(args, 0, out, "")

    workspace = tmp_path / "workspace"
    (workspace / "applications").mkdir(parents=True)
    row = inspect_repository(workspace, {"alias": "applications", "url": listed}, git=Git(), resolve_host=resolve)
    assert row == {"alias": "applications", "state": "verified", "reason": ""}


def test_a_report_never_carries_a_token_a_local_path_or_git_output(tmp_path):
    """Review on #168: reasons are fixed texts from host/owner/name only."""

    secret = "ghp_SECRETTOKEN123"
    workspace = tmp_path / "private-workspace"
    (workspace / "applications").mkdir(parents=True)
    (workspace / "other").mkdir()

    class Git:
        def __init__(self, origin, ls_remote_code=0):
            self.origin, self.code = origin, ls_remote_code

        def __call__(self, args, cwd, timeout):
            if args[:2] == ["rev-parse", "--show-toplevel"]:
                return subprocess.CompletedProcess(args, 0, f"{cwd}\n", "")
            if args[0] == "rev-parse":
                return subprocess.CompletedProcess(args, 0, "true\n", "")
            if args[0] == "remote":
                return subprocess.CompletedProcess(args, 0, self.origin + "\n", "")
            return subprocess.CompletedProcess(args, self.code, "", f"fatal: https://x:{secret}@github.com denied at {cwd}")

    listed = {"alias": "applications", "url": "https://github.com/kdcube/applications.git"}
    rows = [
        inspect_repository(workspace, listed, git=Git(f"https://x-access-token:{secret}@github.com/kdcube/other.git"), resolve_host=None),
        inspect_repository(workspace, listed, git=Git(f"https://x-access-token:{secret}@github.com/kdcube/applications.git", 128), resolve_host=None),
        inspect_repository(workspace, {"alias": "other", "url": "/srv/private/applications.git"}, git=Git(str(tmp_path / "somewhere.git")), resolve_host=None),
        inspect_repository(workspace, {"alias": "missing", "url": listed["url"]}, git=Git(""), resolve_host=None),
    ]
    assert [row["state"] for row in rows] == ["unreachable"] * 4
    assert rows[0]["reason"] == "origin is github.com/kdcube/other, the project lists github.com/kdcube/applications"
    text = repr(rows)
    for leaked in (secret, "x-access-token", str(workspace), str(tmp_path), "/srv/private", "fatal:"):
        assert leaked not in text, leaked


def test_git_keeps_batch_mode_and_closed_stdin_under_a_host_ssh_command(monkeypatch, tmp_path):
    from project_board.client import workspace_report

    seen = {}

    def fake_run(args, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i /keys/deploy")
    monkeypatch.setattr(workspace_report.subprocess, "run", fake_run)
    workspace_report._run_git(["status"], tmp_path, 1.0)
    assert seen["env"]["GIT_SSH_COMMAND"] == "ssh -i /keys/deploy -o BatchMode=yes"
    assert seen["stdin"] is subprocess.DEVNULL and seen["env"]["GIT_TERMINAL_PROMPT"] == "0"


class ReportBoard(Board):
    """Lists local remotes on the card and stores the report like the service."""

    def __init__(self, recipient: str, remote: Path, *, acknowledges: bool = True) -> None:
        super().__init__(recipient)
        self.remote = remote
        self.acknowledges = acknowledges
        self.stored_signature = ""

    async def action(self, *, object_ref: str, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        answer = await super().action(object_ref=object_ref, action=action, payload=payload)
        if action == "worker.heartbeat" and (payload or {}).get("project_ref"):
            answer["object"]["assignment_project"]["repositories"] = [
                {"alias": "applications", "url": str(self.remote), "role": "journal", "path": "docs/journal"},
                {"alias": "kdcube", "url": str(self.remote.parent / "kdcube.git"), "role": "work"},
            ]
            report = (payload or {}).get("workspace_report")
            if isinstance(report, dict) and self.acknowledges:
                self.stored_signature = str(report.get("signature") or "")
            if self.acknowledges:
                answer["object"]["workspace_report_signature"] = self.stored_signature
        return answer


def _heartbeats(board: Board) -> list[dict[str, Any]]:
    return [call["payload"] for call in board.calls if call["action"] == "worker.heartbeat" and call["payload"].get("project_ref")]


def _host_with_workspace(tmp_path, monkeypatch):
    host, identity, field, config = _fresh_host(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    host_config.enroll_worker_channel(
        host.path, identity=identity, profile="problem-board-codex-one", authorized=True,
        working_directory=str(workspace),
    )
    monkeypatch.setenv("PROBLEM_BOARD_CONFIG", str(host.path))
    return host, identity, field, config, workspace


def _cli(identity, *arguments: str) -> dict[str, Any]:
    flags = ["--runtime-kind", identity.runtime_kind, "--runtime-session-id", identity.runtime_session_id]
    return cli._worker_command(cli.build_parser().parse_args(["worker", *arguments[:1], *flags, *arguments[1:]]))  # noqa: SLF001


def test_the_relay_reports_the_record_and_carries_the_agents_report_once(tmp_path, monkeypatch):
    host, identity, field, config, workspace = _host_with_workspace(tmp_path, monkeypatch)
    remote = _remote(tmp_path / "remotes", "applications")
    board = ReportBoard(identity.worker_name, remote)
    adapter = relay.ProblemBoardHostRelayAdapter(config=config, field=field, client=board)

    # No record yet: the first project heartbeat names none; its answer writes it.
    asyncio.run(adapter.poll_attendances_once())
    assert "project_record" not in _heartbeats(board)[0]
    assert field.read_project_repositories("quickstart-works-mttfmgqu")["revision"] == 3
    assert all("workspace_report" not in payload for payload in _heartbeats(board))

    _git("clone", "-q", str(remote), str(workspace / "applications"), cwd=tmp_path)
    reported = _cli(identity, "workspace-report")
    assert reported["project_ref"] == PROJECT_REF and reported["on_board"] is False
    assert [(row["alias"], row["state"]) for row in reported["repositories"]] == [
        ("applications", "verified"),
        ("kdcube", "unreachable"),
    ]

    asyncio.run(adapter.poll_attendances_once())
    # The report forces one heartbeat, which also names the record it holds.
    record = _heartbeats(board)[-1]["project_record"]
    assert record["revision"] == 3 and record["received_at"]
    carried = _heartbeats(board)[-1]["workspace_report"]
    assert carried["revision"] == 3 and carried["signature"] == board.stored_signature
    assert [row["state"] for row in carried["repositories"]] == ["verified", "unreachable"]
    assert _cli(identity, "workspace-report", "--no-verify")["repositories"][0]["state"] == "cloned"


def test_a_board_without_the_field_gets_at_most_one_forced_heartbeat_per_report(tmp_path, monkeypatch):
    def heartbeats_over_ten_cycles(*, report: bool) -> int:
        root = tmp_path / str(report)
        host, identity, field, config, workspace = _host_with_workspace(root, monkeypatch)
        board = ReportBoard(identity.worker_name, _remote(root / "remotes", "applications"), acknowledges=False)
        adapter = relay.ProblemBoardHostRelayAdapter(config=config, field=field, client=board)
        asyncio.run(adapter.poll_attendances_once())
        asyncio.run(adapter.poll_attendances_once())
        if report:
            _cli(identity, "workspace-report", "--no-verify")
        before = len(_heartbeats(board))
        for _ in range(10):
            asyncio.run(adapter.poll_attendances_once())
        return len(_heartbeats(board)) - before

    assert heartbeats_over_ten_cycles(report=True) <= heartbeats_over_ten_cycles(report=False) + 1


def test_a_report_before_the_record_arrived_is_refused_by_name(tmp_path, monkeypatch):
    host, identity, field, config, workspace = _host_with_workspace(tmp_path, monkeypatch)
    with pytest.raises(DomainError) as raised:
        _cli(identity, "workspace-report", "--project-ref", PROJECT_REF)
    assert raised.value.code in {"field_project_record_missing", "field_project_not_found"}
    assert isinstance(field, SharedFieldStore)


def test_review_on_168_a_password_with_an_at_sign_never_leaks_and_nested_folders_and_option_aliases_are_refused(tmp_path):
    """A password may contain "@"; an alias may not be an ssh option; a folder must be its own checkout."""

    from project_board.client.workspace_report import ssh_hostname

    leaked = comparable_url("https://user:p@ss@github.com/kdcube/applications.git")
    assert leaked == "github.com/kdcube/applications"
    assert "ss@" not in leaked and "p@" not in leaked
    assert ssh_hostname("-oProxyCommand=touch /tmp/x") == "-oProxyCommand=touch /tmp/x"

    # A real nested folder: a folder inside a parent checkout is not a clone.
    parent = tmp_path / "workspace"
    parent.mkdir()
    subprocess.run(["git", "init", "-q", str(parent)], check=True)
    (parent / "applications").mkdir()
    row = inspect_repository(
        parent, {"alias": "applications", "url": "https://github.com/kdcube/applications.git"}, verify=False, resolve_host=None
    )
    assert row["state"] == "unreachable"
    assert row["reason"] == "the alias folder is inside another checkout, not a clone of its own"
    assert str(tmp_path) not in row["reason"]

"""A project's files live in its repositories; the card lists where (W370).

Operator ruling, 2026-09-27: project files (instructions, facts, environment,
and any others the person lists) live in the project's repositories, never in
the board's database. The card lists each as a repository alias and a path,
three with a purpose. The board sends ``files`` and ``files_revision`` in the
project record and ``files_edit_allowed`` (this worker's Card check of
``project.files.edit``) on the heartbeat. ``pb worker context`` turns the list
into this agent's local paths with a state; the purpose files decide the
instructions, facts and environment refs; connect-project names what is
missing; the relay serves the card's view of a listed file from its own
per-project copy at the default branch's newest commit (W794).
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import os
from pathlib import Path
from typing import Any

import pytest

from project_board.client import relay
from project_board.client.io import content_hash
from project_board.client.store import SharedFieldStore
from project_board.contract.errors import DomainError
from test_commit_identity import IdentityBoard, _host
from test_workspace_report import _cli, _git, _remote
from procedure_reference import reference_text

PROJECT_ID = "demo-project-0a1b2c3d"
PROJECT_REF = "work:project:" + PROJECT_ID
FILES = [
    {"purpose": "instructions", "alias": "applications", "path": "docs/project-instructions.md", "description": "How we work"},
    {"purpose": "facts", "alias": "applications", "path": "docs/project-facts.md", "description": ""},
    {"purpose": "environment", "alias": "kdcube", "path": "docs/environment.md", "description": ""},
    {"purpose": "", "alias": "applications", "path": "docs/release-notes.md", "description": "What shipped"},
    {"purpose": "", "alias": "applications", "path": "docs/not-here.md", "description": ""},
]


class FilesBoard(IdentityBoard):
    """A W370 board: the files list in the project record, the edit permission on the heartbeat."""

    def __init__(self, recipient, remote, *, files=None, revision=2, edit_allowed=True, facts=False) -> None:
        super().__init__(recipient, remote)
        self.files = files
        self.files_revision = revision
        self.edit_allowed = edit_allowed
        self.facts = facts

    async def action(self, *, object_ref: str, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        answer = await super().action(object_ref=object_ref, action=action, payload=payload)
        if action == "worker.heartbeat" and (payload or {}).get("project_ref"):
            project = answer["object"]["assignment_project"]
            if self.facts:
                # Released 2247 facts, still sent read-only for one release.
                project.update({"facts": [{"label": "Old", "value": "pair"}], "facts_revision": 1})
            if self.files is not None:
                project.update({"files": self.files, "files_revision": self.files_revision})
                answer["object"]["files_edit_allowed"] = self.edit_allowed
        return answer


def _seeded_remote(root: Path, name: str, files: dict[str, str]) -> Path:
    remote = _remote(root, name)
    seed = root / f"{name}-seed"
    for relative, text in files.items():
        target = seed / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        _git("add", relative, cwd=seed)
    _git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "files", cwd=seed)
    _git("push", "-q", str(remote), "main", cwd=seed)
    return remote


def _setup(tmp_path, monkeypatch, **board: Any):
    identity, field, config, workspace = _host(tmp_path, monkeypatch)
    remote = _seeded_remote(
        tmp_path / "remotes",
        "applications",
        {
            "docs/project-instructions.md": "# Instructions\n",
            "docs/project-facts.md": "# Facts\n",
            "docs/release-notes.md": "# Notes\n",
        },
    )
    files_board = FilesBoard(identity.worker_name, remote, **board)
    adapter = relay.ProblemBoardHostRelayAdapter(config=config, field=field, client=files_board)
    asyncio.run(adapter.poll_attendances_once())
    asyncio.run(adapter.poll_attendances_once())
    _git("clone", "-q", str(remote), str(workspace / "applications"), cwd=tmp_path)
    return identity, field, config, workspace, adapter, files_board


def test_context_lists_each_file_in_this_agents_clone_and_the_purposes_decide_the_refs(tmp_path, monkeypatch):
    identity, field, config, workspace, adapter, board = _setup(tmp_path, monkeypatch, files=FILES, facts=True)
    context = _cli(identity, "context", "--project-ref", PROJECT_REF)

    assert context["project_instructions_ref"] == "repo:applications/docs/project-instructions.md"
    assert context["local_project_instructions"] == str(workspace / "applications" / "docs/project-instructions.md")
    assert context["project_instructions_state"] == "present"
    assert context["project_facts_ref"] == "repo:applications/docs/project-facts.md"
    assert context["project_facts_state"] == "present"
    # kdcube is not cloned here.
    assert context["project_environment_ref"] == "repo:kdcube/docs/environment.md"
    assert context["project_environment_state"] == "not_cloned"

    further = {row["path"]: row for row in context["project_files"]}
    assert set(further) == {"docs/release-notes.md", "docs/not-here.md"}
    assert further["docs/release-notes.md"]["state"] == "present"
    assert further["docs/release-notes.md"]["description"] == "What shipped"
    assert further["docs/not-here.md"]["state"] == "missing"
    assert context["project_files_revision"] == 2
    assert context["project_files_editable"] is True
    # The label/value facts retire once the list is known.
    assert context["project_card"] == "known" and "project_facts" not in context
    assert context["project_goal"] == "Onboard agents."


def test_an_empty_slot_is_empty_and_never_falls_back_to_the_journal(tmp_path, monkeypatch):
    only_further = [{"purpose": "", "alias": "applications", "path": "docs/release-notes.md", "description": ""}]
    identity, *_ = _setup(tmp_path, monkeypatch, files=only_further, edit_allowed=False)
    context = _cli(identity, "context", "--project-ref", PROJECT_REF)
    for key in ("project_instructions_ref", "project_facts_ref", "project_environment_ref"):
        assert context[key] == ""
    assert context["project_files_editable"] is False


def test_a_board_that_predates_the_list_keeps_todays_behaviour(tmp_path, monkeypatch):
    # A 2247 board: facts sent, no files list.
    identity, *_ = _setup(tmp_path, monkeypatch, files=None, facts=True)
    context = _cli(identity, "context", "--project-ref", PROJECT_REF)
    assert "project_files" not in context and "project_files_editable" not in context
    assert context["project_facts"] == [{"label": "Old", "value": "pair"}]


def test_the_list_is_kept_by_revision_and_the_permission_follows_every_answer(tmp_path):
    field = SharedFieldStore(tmp_path / "field")
    field.create_project(project_id=PROJECT_ID, title="Demo", goal="Demo", owner="control-plane")
    assert field.sync_project_files(PROJECT_ID, files=FILES, revision=2, edit_allowed=False) is True
    # An older list never replaces a newer one; the permission changes at any revision.
    assert field.sync_project_files(PROJECT_ID, files=[], revision=1, edit_allowed=False) is False
    assert len(field.read_project_files(PROJECT_ID)["files"]) == 5
    assert field.sync_project_files(PROJECT_ID, files=FILES, revision=2, edit_allowed=True) is True
    assert field.read_project_files(PROJECT_ID)["edit_allowed"] is True
    # A path leaving its repository is dropped; a second use of a purpose loses the purpose.
    risky = [
        {"purpose": "facts", "alias": "applications", "path": "../outside.md"},
        {"purpose": "facts", "alias": "applications", "path": "/etc/passwd"},
        {"purpose": "facts", "alias": "applications", "path": "./docs/a.md"},
        {"purpose": "facts", "alias": "applications", "path": "docs/b.md"},
    ]
    assert field.sync_project_files(PROJECT_ID, files=risky, revision=3) is True
    kept = field.read_project_files(PROJECT_ID)["files"]
    assert [(row["purpose"], row["path"]) for row in kept] == [("facts", "docs/a.md"), ("", "docs/b.md")]


def test_connect_project_names_every_listed_file_it_does_not_find(tmp_path, monkeypatch):
    identity, field, config, workspace, adapter, board = _setup(tmp_path, monkeypatch, files=FILES)
    result = _cli(identity, "connect-project", "--ssh-dir", str(tmp_path / "ssh"))
    missing = {(row["alias"], row["path"], row["state"]) for row in result["project_files_missing"]}
    assert ("applications", "docs/not-here.md", "missing") in missing
    assert ("kdcube", "docs/environment.md", "not_cloned") in missing
    assert "applications:docs/not-here.md (missing)" in result["next"]


class ViewClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def action(self, *, object_ref: str, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append({"object_ref": object_ref, "action": action, "payload": dict(payload or {})})
        return {"ok": True, "object": {}}


def _view_control(alias: str, path: str, *, repository_ref: str | None = None) -> dict[str, Any]:
    payload = {
        "view_ref": "work:journal_view:20260927T214000Z:view_0123456789abcdef0123456789abcdef:file",
        "mode": "file",
        "alias": alias,
        "path": path,
        "repository_ref": repository_ref if repository_ref is not None else f"repo:{alias}/{path}",
    }
    return {"kind": "project.file.read", "project_ref": PROJECT_REF, "payload": payload, "payload_hash": content_hash(payload)}


class KeyedViewClient(ViewClient):
    """A board client whose Connection Hub path issues the coordinator Card's GitHub key (W794)."""

    def __init__(self, *, refuse: bool = False) -> None:
        super().__init__()
        self.refuse = refuse
        self.issued: list[tuple[str, str]] = []

    async def github_key(self, project_ref: str, repository: str):
        self.issued.append((project_ref, repository))
        if self.refuse:
            raise DomainError("work_github_use_denied", "The Card does not hold project.github.use.")
        return type("Token", (), {"token": "ghu_w794_test_token"})()


def _machine_copy(tmp_path, workspace, monkeypatch, scoped):
    """W794: the relay reads its own per-project copy; the test's GitHub is the clone's local remote."""

    from project_board.client.project_file_source import ProjectFileSource

    import subprocess

    def git(cwd, *args):
        return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True).stdout

    remote = Path(git(workspace / "applications", "remote", "get-url", "origin").strip())
    git(remote, "config", "uploadpack.allowFilter", "true")
    git(remote, "config", "uploadpack.allowAnySHA1InWant", "true")
    monkeypatch.setattr(relay, "github_repository", lambda url: "owner/applications" if url else "")
    base = tmp_path / "github"
    (base / "owner").mkdir(parents=True)
    os.symlink(remote, base / "owner" / "applications.git")
    scoped._file_source = ProjectFileSource(tmp_path / "machine" / "project-files", remote_base=f"file://{base}/")  # noqa: SLF001
    return remote


def test_the_relay_serves_a_listed_file_from_its_own_copy_at_the_default_branch_head(tmp_path, monkeypatch):
    # W794 (operator, 2026-10-13: "we need the machine with coordinator and its credential"; "last one" = "A"):
    # the coordinator machine's own copy, fetched before the read, never the agent's clone or working tree.
    identity, field, config, workspace, adapter, board = _setup(tmp_path, monkeypatch, files=FILES)
    client = KeyedViewClient()
    scoped = relay.ProblemBoardHostRelayAdapter(
        config=dataclasses.replace(config, project_id=PROJECT_ID, workspace=str(workspace)), field=field, client=client
    )
    remote = _machine_copy(tmp_path, workspace, monkeypatch, scoped)
    # The agent's clone is dirty and behind: it must not matter.
    (workspace / "applications" / "docs" / "project-facts.md").write_text("# Local edit\n", encoding="utf-8")
    view_ref = asyncio.run(scoped._serve_file_view(_view_control("applications", "docs/project-facts.md")))  # noqa: SLF001
    [call] = client.calls
    assert call["object_ref"] == view_ref and call["action"] == "journal.view.publish"
    published = call["payload"]
    assert published["content"] == "# Facts\n"
    assert published["content_hash"] == hashlib.sha256(b"# Facts\n").hexdigest()
    assert published["repository_journal_ref"] == "repo:applications/docs/project-facts.md"
    import subprocess

    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(remote), check=True, capture_output=True, text=True).stdout.strip()
    assert published["source_commit"] == head and len(head) == 40
    assert client.issued == [(PROJECT_REF, "owner/applications")]
    assert "ghu_w794_test_token" not in repr(published)

    def refused(control) -> str:
        with pytest.raises(DomainError) as raised:
            asyncio.run(scoped._serve_file_view(control))  # noqa: SLF001
        return raised.value.code

    assert refused(_view_control("applications", "README.md")) == "file_not_listed"
    assert refused(_view_control("applications", "docs/not-here.md")) == "file_missing"
    assert refused(_view_control("applications", "docs/project-facts.md", repository_ref="repo:applications/x.md")) == "file_not_listed"


def test_a_coordinator_card_without_the_github_key_is_a_named_refusal(tmp_path, monkeypatch):
    identity, field, config, workspace, adapter, board = _setup(tmp_path, monkeypatch, files=FILES)
    scoped = relay.ProblemBoardHostRelayAdapter(
        config=dataclasses.replace(config, project_id=PROJECT_ID, workspace=str(workspace)), field=field,
        client=KeyedViewClient(refuse=True),
    )
    _machine_copy(tmp_path, workspace, monkeypatch, scoped)
    with pytest.raises(DomainError) as raised:
        asyncio.run(scoped._serve_file_view(_view_control("applications", "docs/project-facts.md")))  # noqa: SLF001
    assert raised.value.code == "github_key_refused" and "work_github_use_denied" in str(raised.value)


def test_a_repository_at_a_local_path_is_read_in_place_without_a_github_key(tmp_path, monkeypatch):
    # W848 (operator, 2026-10-14: "B: the client reads local repositories directly << of course this! this is
    # special type of project. where we do not have any github"): the project's repository URL is a path here.
    identity, field, config, workspace, adapter, board = _setup(tmp_path, monkeypatch, files=FILES)
    client = ViewClient()  # no github_key path at all
    local = workspace / "applications"
    other = tmp_path / "other-project"
    scoped = relay.ProblemBoardHostRelayAdapter(
        config=dataclasses.replace(
            config, project_id=PROJECT_ID, workspace=str(workspace),
            # This host's own map: the only local repositories a file view may read.
            source_repositories=(("applications", str(local)), ("other", str(other))),
        ),
        field=field, client=client,
    )
    scoped._file_source = relay.ProjectFileSource(tmp_path / "machine" / "project-files")  # noqa: SLF001
    import subprocess

    def git(cwd, *args):
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
            cwd=str(cwd), check=True, capture_output=True, text=True,
        ).stdout

    head = git(local, "rev-parse", "HEAD").strip()
    monkeypatch.setattr(
        field, "read_project_repositories",
        lambda project_id: {"repositories": [{"alias": "applications", "url": str(local)}]},
    )
    # An uncommitted edit in the folder is not what the view shows.
    (local / "docs" / "project-facts.md").write_text("# Uncommitted\n", encoding="utf-8")
    view_ref = asyncio.run(scoped._serve_file_view(_view_control("applications", "docs/project-facts.md")))  # noqa: SLF001
    [call] = client.calls
    assert call["object_ref"] == view_ref and call["payload"]["content"] == "# Facts\n"
    assert call["payload"]["source_commit"] == head
    assert not (tmp_path / "machine" / "project-files").exists()  # no clone is made

    def refused(url: str) -> str:
        monkeypatch.setattr(
            field, "read_project_repositories",
            lambda project_id: {"repositories": [{"alias": "applications", "url": url}]},
        )
        with pytest.raises(DomainError) as raised:
            asyncio.run(scoped._serve_file_view(_view_control("applications", "docs/project-facts.md")))  # noqa: SLF001
        return raised.value.code

    # W848 review (Mint): a path the board names but this host's map does not bind to the alias is refused,
    # without the host path in the message: another repository on this machine, another alias's root, a missing one.
    _git("clone", "-q", str(local), str(other), cwd=tmp_path)
    assert refused(str(other)) == "repository_not_mapped"
    assert refused(str(tmp_path / "gone")) == "repository_not_mapped"
    monkeypatch.setattr(
        field, "read_project_repositories",
        lambda project_id: {"repositories": [{"alias": "applications", "url": str(other)}]},
    )
    with pytest.raises(DomainError) as raised:
        asyncio.run(scoped._serve_file_view(_view_control("applications", "docs/project-facts.md")))  # noqa: SLF001
    assert raised.value.code == "repository_not_mapped" and str(tmp_path) not in str(raised.value)
    monkeypatch.setattr(
        field, "read_project_repositories",
        lambda project_id: {"repositories": [{"alias": "applications", "url": f"file://{local}"}]},
    )
    asyncio.run(scoped._serve_file_view(_view_control("applications", "docs/project-facts.md")))  # noqa: SLF001
    assert client.calls[-1]["payload"]["content"] == "# Facts\n"
    # A mapped root that is gone or is not a repository is named as such.
    other_git = other / ".git"
    import shutil

    shutil.rmtree(other_git)
    scoped.config = dataclasses.replace(scoped.config, source_repositories=(("applications", str(other)),))
    assert refused(str(other)) == "repository_not_git"
    shutil.rmtree(other)
    assert refused(str(other)) == "repository_path_missing"
    # A remote that is neither GitHub nor a path keeps W794's refusal.
    assert refused("https://gitlab.example.com/owner/x.git") == "repository_not_on_github"


def test_a_host_that_allows_journal_views_allows_file_views(tmp_path, monkeypatch):
    identity, field, config, workspace, adapter, board = _setup(tmp_path, monkeypatch, files=FILES)
    control = {**_view_control("applications", "docs/project-facts.md"), "sender": "control-plane"}
    older = dataclasses.replace(config, allowed_control_kinds=("journal.read", "mail", "ping"))
    assert relay.ProblemBoardHostRelayAdapter(config=older, field=field, client=board)._receiver_refusal(control) != "receiver_policy_control_kind_denied"  # noqa: SLF001
    closed = dataclasses.replace(config, allowed_control_kinds=("mail", "ping"))
    assert relay.ProblemBoardHostRelayAdapter(config=closed, field=field, client=board)._receiver_refusal(control) == "receiver_policy_control_kind_denied"  # noqa: SLF001
    assert "project.file.read" in relay.DEFAULT_CONTROL_KINDS


def _files_signal(identity) -> dict[str, Any] | None:
    received = _cli(identity, "receive")
    return next((s for s in received.get("signals") or [] if s.get("kind") == "project.files.changed"), None)


def test_receive_names_the_files_that_changed_until_the_agent_reads_them_again(tmp_path, monkeypatch):
    """Operator, 2026-09-27: when a file changes or joins the list, agents are told on their next check."""

    identity, field, config, workspace, adapter, board = _setup(tmp_path, monkeypatch, files=FILES)
    # Never read yet: every listed file is new to this agent.
    first = _files_signal(identity)
    assert first and {c["change"] for c in first["changes"]} == {"added"}
    assert "reread the files named here" in first["next"]

    _cli(identity, "context", "--project-ref", PROJECT_REF)
    assert _files_signal(identity) is None

    # A content change that reached this agent's clone is named, and repeats until context is read.
    facts = workspace / "applications" / "docs" / "project-facts.md"
    facts.write_text("# Facts\n\nA new ruling.\n", encoding="utf-8")
    changed = _files_signal(identity)
    assert changed["changes"] == [{"ref": "repo:applications/docs/project-facts.md", "change": "changed"}]
    assert _files_signal(identity) is not None
    _cli(identity, "context", "--project-ref", PROJECT_REF)
    assert _files_signal(identity) is None

    # A file added to the list is named too.
    SharedFieldStore(config.field_root).sync_project_files(
        PROJECT_ID,
        files=[*FILES, {"purpose": "", "alias": "applications", "path": "docs/monthly-routine.md", "description": "One monthly pass"}],
        revision=3,
    )
    added = _files_signal(identity)
    assert {"ref": "repo:applications/docs/monthly-routine.md", "change": "added"} in added["changes"]


def test_the_procedures_explain_project_files_in_the_operators_words():
    """W370 acceptance (operator, 2026-09-27): not done until the procedures say it as plainly as this."""

    from project_board.client.procedures import source_package_path

    root = source_package_path()

    def text(relative: str) -> str:
        return " ".join(reference_text(root / relative).split())

    skill = text("SKILL.md")
    workspace = text("references/project-workspace.md")
    coordinator = text("references/coordinator.md")
    create = " ".join((root.parent / "create-a-project.md").read_text(encoding="utf-8").split())
    shared = "Project files are the project's shared, current knowledge."
    for piece in (
        shared,
        "Every agent of the project is told where they are, reads them in its own clone, and follows them",
        "**Read them first, before any work.**",
        "The descriptions are a table of contents, not a reading list.",
        "when a file changes, or one is added to the list, agents are told on their next check and reread it.",
        "One edit changes what every agent on every machine does.",
        "rulings live in project files, not in any agent's private memory",
        "project files are the current truth (what applies now), and every project has them. A journal is history (what happened and why), and it stays optional.",
        "what the project is, its rules and conventions, how work is done there",
        "the decisions and rulings in force now",
        "machines, runtimes, how to test and deploy",
        "propose the change to the coordinator",
    ):
        assert piece in workspace, piece
    assert "project files are the project's shared, current knowledge" in skill
    assert "read a further file when its description fits the task at hand" in skill
    assert "`project.files.changed`" in skill
    assert "writes it into Facts" in coordinator and "rulings live in project files" in coordinator
    for piece in ("`instructions.md`", "`facts.md`", "`environment.md`", "A journal is optional.", "**Project files**"):
        assert piece in create, piece


class HookedClient(ViewClient):
    """A Data Bus client that keeps the relay's keyed push hooks (W794)."""

    def __init__(self) -> None:
        super().__init__()
        self.hooks: dict[str, Any] = {}

    def set_event_hook(self, key, hook) -> None:
        if hook is None:
            self.hooks.pop(key, None)
        else:
            self.hooks[key] = hook


def _available(project_ref: str = PROJECT_REF) -> dict[str, Any]:
    return {"type": "problem_board.worker.event.v1", "data": {"kind": "control.available", "refs": {"project_ref": project_ref}}}


def test_a_queued_control_is_pulled_at_once_not_at_the_end_of_the_turn(tmp_path, monkeypatch):
    # W794 (operator, 2026-10-13: "why requesting the doc should care if coord busy or no at all"): the board's
    # control.available push pulls and serves controls now, while the attendance turn may still be running.
    identity, field, config, workspace, adapter, board = _setup(tmp_path, monkeypatch, files=FILES)
    client = HookedClient()
    scoped_config = dataclasses.replace(config, project_id=PROJECT_ID, workspace=str(workspace))
    first = relay.ProblemBoardHostRelayAdapter(config=scoped_config, field=field, client=client)
    second = relay.ProblemBoardHostRelayAdapter(config=scoped_config, field=field, client=client)
    # Adapters are rebuilt every discovery turn: one hook per project, the newest.
    assert list(client.hooks) == [f"problem-board.controls:{PROJECT_ID}"]
    assert client.hooks[f"problem-board.controls:{PROJECT_ID}"] == second._on_push_event  # noqa: SLF001
    pulls: list[str] = []
    release = asyncio.Event()

    async def pull(*, project_ref=None):
        pulls.append(project_ref or "project")
        await release.wait()
        return {}

    monkeypatch.setattr(second, "_pull_controls", pull)

    async def scenario():
        turn_lock = second._pull_lock()  # noqa: SLF001
        assert turn_lock is first._pull_lock()  # noqa: SLF001 - one lock per project on the client
        hook = client.hooks[f"problem-board.controls:{PROJECT_ID}"]
        hook({"data": {"kind": "mail.available", "refs": {"project_ref": PROJECT_REF}}})
        hook(_available("work:project:another-project"))
        assert second._immediate_pull is None  # noqa: SLF001
        hook(_available())
        hook(_available())  # one pull in flight at a time
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert pulls == ["project"]
        release.set()
        await second._immediate_pull  # noqa: SLF001

    asyncio.run(scenario())
    assert pulls == ["project"]


def test_the_attendance_channel_registers_no_push_hook(tmp_path, monkeypatch):
    identity, field, config, workspace, adapter, board = _setup(tmp_path, monkeypatch, files=FILES)
    client = HookedClient()
    relay.ProblemBoardHostRelayAdapter(config=dataclasses.replace(config, project_id="attendance"), field=field, client=client)
    assert client.hooks == {}

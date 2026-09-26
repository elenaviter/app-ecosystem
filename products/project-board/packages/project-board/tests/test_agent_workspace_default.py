"""An agent's workspace is its own folder under the host root, never where its session started (W262).

On 2026-09-26 a Claude Code agent started in the shared kdcube checkout, had no
recorded folder, and was handed that checkout by `pb worker context`. The
operator: "it had to be a dedicated workspace". With no folder inside an
approved root, the agent's workspace is `<first allowed root>/<alias>`.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from project_board.client import cli, host_config
from project_board.contract.worker_identity import WorkerSessionIdentity


def _host(tmp_path: Path, roots: list[str]):
    return host_config.initialize_host_config(
        target_id="target",
        endpoint="https://runtime.example/mcp",
        tenant="tenant",
        platform_project="project",
        host_id="host-one",
        allowed_roots=roots,
        source_repositories={},
        config_path=tmp_path / "relay.json",
        state_root=tmp_path / "state",
    )


IDENTITY = WorkerSessionIdentity.create("claude-code", "22222222-2222-4222-8222-222222222222")


def test_the_default_is_the_first_root_and_the_alias_else_the_name():
    assert host_config.default_working_directory(["/w/root"], alias="lehrwerk", worker_name="claude-code-x") == "/w/root/lehrwerk"
    assert host_config.default_working_directory(["/w/root"], alias="", worker_name="claude-code-x") == "/w/root/claude-code-x"
    assert host_config.default_working_directory([], alias="lehrwerk", worker_name="x") == ""


def test_a_session_started_in_a_shared_checkout_gets_its_own_folder_under_the_root(tmp_path):
    root = tmp_path / "workspaces"
    root.mkdir()
    shared = tmp_path / "shared-checkout"
    shared.mkdir()
    host = _host(tmp_path, [str(root)])

    channel = host_config.enroll_worker_channel(
        host.path, identity=IDENTITY, profile="problem-board-claude-one", authorized=True,
        worker_alias="lehrwerk", working_directory=str(shared),
    )

    assert channel.working_directory == str(root.resolve() / "lehrwerk")
    assert channel.working_directory != str(shared)


def test_a_folder_inside_an_approved_root_is_kept(tmp_path):
    root = tmp_path / "workspaces"
    own = root / "codex-main"
    own.mkdir(parents=True)
    host = _host(tmp_path, [str(root)])

    channel = host_config.enroll_worker_channel(
        host.path, identity=IDENTITY, profile="problem-board-claude-one", authorized=True,
        working_directory=str(own),
    )

    assert channel.working_directory == str(own.resolve())


def test_context_names_the_host_root_folder_for_an_agent_with_none_recorded(tmp_path):
    root = tmp_path / "workspaces"
    root.mkdir()
    config = host_config.HostRelayConfig.load(_host(tmp_path, [str(root)]).path)
    channel = SimpleNamespace(working_directory="", worker_alias="lehrwerk", worker_name="claude-code-x")

    field = SimpleNamespace(_project_path=lambda _project_id: tmp_path / "no-project-record", worker_board_record=lambda _name: {})

    context = cli._worker_project_context(config, field, "work:project:one", channel=channel)  # noqa: SLF001

    assert context["workspace"] == str(root.resolve() / "lehrwerk")
    assert context["workspace_source"] == "host_root"
    assert "workspace_note" not in context


def test_context_with_no_approved_root_says_so_and_names_no_folder(tmp_path):
    config = host_config.HostRelayConfig.load(_host(tmp_path, [str(tmp_path)]).path)
    config = SimpleNamespace(
        journal_workspace_root=config.journal_workspace_root,
        allowed_roots=(),
        effective_agent_workspace_root="",
        repository_mapping=config.repository_mapping,
    )
    field = SimpleNamespace(_project_path=lambda _project_id: tmp_path / "no-project-record", worker_board_record=lambda _name: {})
    channel = SimpleNamespace(working_directory="", worker_alias="lehrwerk", worker_name="claude-code-x")

    context = cli._worker_project_context(config, field, "work:project:one", channel=channel)  # noqa: SLF001

    assert context["workspace"] == ""
    assert "Never choose a folder yourself" in context["workspace_note"]


def test_the_host_setting_names_the_agent_root_and_it_wins_over_the_first_work_root(tmp_path):
    first = tmp_path / "a-work-root"
    agents = tmp_path / "agents"
    first.mkdir()
    agents.mkdir()
    host = _host(tmp_path, [str(first), str(agents)])
    assert host_config.HostRelayConfig.load(host.path).effective_agent_workspace_root == str(first.resolve())

    updated = host_config.update_host_config(host.path, agent_workspace_root=str(agents))
    assert updated.agent_workspace_root == str(agents.resolve())
    assert host_config.HostRelayConfig.load(host.path).effective_agent_workspace_root == str(agents.resolve())

    channel = host_config.enroll_worker_channel(
        host.path, identity=IDENTITY, profile="problem-board-claude-one", authorized=True,
        worker_alias="lehrwerk", working_directory=str(first / "somewhere"),
    )
    # A folder under another work root is not this agent's workspace either.
    assert channel.working_directory == str(agents.resolve() / "lehrwerk")


def test_the_configure_command_takes_the_agent_workspace_root():
    parsed = cli.build_parser().parse_args(["host", "configure", "--agent-workspace-root", "/w/agents"])
    assert parsed.agent_workspace_root == "/w/agents"


def test_context_says_plainly_when_the_recorded_folder_is_outside_the_agent_root(tmp_path):
    root = tmp_path / "workspaces"
    root.mkdir()
    config = host_config.HostRelayConfig.load(_host(tmp_path, [str(root)]).path)
    field = SimpleNamespace(_project_path=lambda _project_id: tmp_path / "no-project-record", worker_board_record=lambda _name: {})
    channel = SimpleNamespace(
        working_directory=str(tmp_path / "shared-kdcube-checkout"), worker_alias="lehrwerk", worker_name="claude-code-x",
    )

    context = cli._worker_project_context(config, field, "work:project:one", channel=channel)  # noqa: SLF001

    assert context["workspace"] == str(root.resolve() / "lehrwerk")
    assert context["workspace_source"] == "host_root"
    assert "is outside the host's agent workspace root" in context["workspace_note"]
    assert "it is not your workspace" in context["workspace_note"]


def test_an_unsafe_alias_never_escapes_the_root(tmp_path):
    root = tmp_path / "workspaces"
    root.mkdir()
    for alias in ("../../etc", "/tmp/evil", "..", "a/b", ""):
        path = host_config.default_working_directory([str(root)], alias=alias, worker_name="claude-code-x")
        assert path == str(root / "claude-code-x"), alias
        assert host_config.is_inside(path, root)
    assert host_config.default_working_directory([str(root)], alias="..", worker_name="..") == ""


def test_the_agent_root_must_lie_inside_an_approved_root_and_can_be_cleared(tmp_path):
    import pytest
    from project_board.contract.errors import DomainError  # noqa: F401  (import path check)

    root = tmp_path / "workspaces"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    host = _host(tmp_path, [str(root)])
    with pytest.raises(Exception) as refused:
        host_config.update_host_config(host.path, agent_workspace_root=str(outside))
    assert getattr(refused.value, "code", "") == "work_agent_workspace_root_outside_allowed_roots"

    inside = root / "agents"
    host_config.update_host_config(host.path, agent_workspace_root=str(inside))
    assert host_config.HostRelayConfig.load(host.path).agent_workspace_root == str(inside.resolve())
    host_config.update_host_config(host.path, agent_workspace_root="")
    assert host_config.HostRelayConfig.load(host.path).agent_workspace_root == ""


def test_listen_report_and_enrollment_refuse_a_recorded_folder_outside_the_root(tmp_path):
    root = tmp_path / "workspaces"
    root.mkdir()
    shared = tmp_path / "shared-kdcube-checkout"
    shared.mkdir()
    host = _host(tmp_path, [str(root), str(tmp_path)])
    # Recorded before the host had an agent root: under an approved root, outside the agent root.
    channel = host_config.enroll_worker_channel(
        host.path, identity=IDENTITY, profile="problem-board-claude-one", authorized=True,
        worker_alias="lehrwerk", working_directory=str(shared),
    )
    host_config.update_host_config(host.path, agent_workspace_root=str(root))
    config = host_config.HostRelayConfig.load(host.path)

    workspace, source, note = host_config.agent_workspace(
        config, recorded=str(shared), alias="lehrwerk", worker_name=channel.worker_name,
    )
    assert workspace == str(root.resolve() / "lehrwerk") and source == "host_root"
    assert "it is not your workspace" in note

    again = host_config.enroll_worker_channel(
        host.path, identity=IDENTITY, profile="problem-board-claude-one", authorized=True,
        worker_alias="lehrwerk", working_directory=str(shared),
    )
    assert again.working_directory == str(root.resolve() / "lehrwerk"), "enrollment follows the same answer"


def test_an_alias_with_an_at_sign_is_its_own_folder_name(tmp_path):
    root = tmp_path / "workspaces"
    root.mkdir()
    assert host_config.default_working_directory(
        [str(root)], alias="claude-lehrwerk@elena", worker_name="claude-code-x"
    ) == str(root / "claude-lehrwerk@elena")


def test_a_recorded_folder_that_no_longer_exists_gives_way_to_the_alias_folder(tmp_path):
    # Rehearsal, 2026-09-26: listen kept a folder the agent had created and deleted.
    root = tmp_path / "workspaces"
    root.mkdir()
    config = host_config.HostRelayConfig.load(_host(tmp_path, [str(root)]).path)
    gone = root / "old-name"
    workspace, source, note = host_config.agent_workspace(
        config, recorded=str(gone), alias="claude-lehrwerk@elena", worker_name="claude-code-x",
    )
    assert workspace == str(root / "claude-lehrwerk@elena") and source == "host_root"
    assert "no longer exists" in note
    # The agent's own folder, not created yet, is kept.
    own = str(root / "claude-lehrwerk@elena")
    assert host_config.agent_workspace(config, recorded=own, alias="claude-lehrwerk@elena", worker_name="x")[:2] == (own, "recorded")

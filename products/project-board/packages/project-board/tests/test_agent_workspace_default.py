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
    config = SimpleNamespace(**{**{k: getattr(config, k) for k in ("journal_workspace_root",)},
                                "allowed_roots": (), "repository_mapping": config.repository_mapping})
    field = SimpleNamespace(_project_path=lambda _project_id: tmp_path / "no-project-record", worker_board_record=lambda _name: {})
    channel = SimpleNamespace(working_directory="", worker_alias="lehrwerk", worker_name="claude-code-x")

    context = cli._worker_project_context(config, field, "work:project:one", channel=channel)  # noqa: SLF001

    assert context["workspace"] == ""
    assert "Never choose a folder yourself" in context["workspace_note"]

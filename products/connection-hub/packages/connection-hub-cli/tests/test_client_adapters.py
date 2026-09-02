from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
import yaml

from connection_hub_cli.clients.adapters import (
    ClaudeCodeAdapter,
    ClaudeDesktopAdapter,
    HermesAdapter,
    OpenClawAdapter,
)
from connection_hub_cli.clients.command import CommandResult
from connection_hub_cli.clients.service import ClientService
from connection_hub_cli.errors import ClientConfigurationError
from connection_hub_cli.models import CallerProfile, HelperLaunch, ManagedInstallation
from connection_hub_cli.state import InstallationStore, ProfileStore


class _Credentials:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def put(self, credential_ref: str, bearer: str) -> None:
        self.values[credential_ref] = bearer

    def get(self, credential_ref: str) -> str | None:
        return self.values.get(credential_ref)

    def remove(self, credential_ref: str) -> bool:
        return self.values.pop(credential_ref, None) is not None

    def backend_name(self) -> str:
        return "test-keyring"

    def verify_ready(self) -> None:
        return None


class _ClientRunner:
    def __init__(self, client: str, path: Path) -> None:
        self.client = client
        self.path = path
        self.commands: list[list[str]] = []

    def available(self, executable: str) -> bool:
        return True

    def run(self, argv) -> CommandResult:
        argv = list(argv)
        self.commands.append(argv)
        if self.client == "claude-code":
            data = (
                json.loads(self.path.read_text())
                if self.path.exists()
                else {"unrelated": {"keep": True}}
            )
            if argv[2] == "add":
                marker = argv.index("--")
                name = argv[marker - 1]
                data.setdefault("mcpServers", {})[name] = {
                    "type": "stdio",
                    "command": argv[marker + 1],
                    "args": argv[marker + 2 :],
                    "env": {},
                }
            else:
                data.get("mcpServers", {}).pop(argv[-1], None)
            self.path.write_text(json.dumps(data))
        elif self.client == "hermes":
            data = (
                yaml.safe_load(self.path.read_text())
                if self.path.exists()
                else {"unrelated": {"keep": True}}
            )
            if argv[2] == "add":
                name = argv[3]
                command_index = argv.index("--command")
                args_index = argv.index("--args")
                data.setdefault("mcp_servers", {})[name] = {
                    "command": argv[command_index + 1],
                    "args": argv[args_index + 1 :],
                    "enabled": True,
                }
            else:
                data.get("mcp_servers", {}).pop(argv[-1], None)
            self.path.write_text(yaml.safe_dump(data))
        elif self.client == "openclaw":
            data = (
                json.loads(self.path.read_text())
                if self.path.exists()
                else {"unrelated": {"keep": True}}
            )
            if argv[2] == "set":
                data.setdefault("mcp", {}).setdefault("servers", {})[argv[3]] = (
                    json.loads(argv[4])
                )
            else:
                data.get("mcp", {}).get("servers", {}).pop(argv[-1], None)
            self.path.write_text(json.dumps(data))
        return CommandResult(returncode=0)


def _installation(client: str) -> ManagedInstallation:
    return ManagedInstallation.create(
        client=client,
        profile="agent",
        server_name="connection-hub-agent",
        launch=HelperLaunch(command="/opt/tools/connection-hub"),
        installation_id="a" * 32,
        now="2026-09-02T15:00:00+00:00",
    )


@pytest.mark.parametrize("client", ["claude-code", "hermes", "openclaw"])
def test_command_backed_adapters_round_trip_without_touching_unrelated_entries(
    tmp_path,
    client: str,
) -> None:
    suffix = "yaml" if client == "hermes" else "json"
    path = tmp_path / f"config.{suffix}"
    runner = _ClientRunner(client, path)
    if client == "claude-code":
        adapter = ClaudeCodeAdapter(runner=runner, config_path=path)
    elif client == "hermes":
        adapter = HermesAdapter(runner=runner, config_path=path)
    else:
        adapter = OpenClawAdapter(runner=runner, config_path=path)
    installation = _installation(client)

    assert adapter.install(installation) is True
    assert installation.owns_entry(adapter.inspect(installation.server_name))
    assert adapter.install(installation) is False
    assert adapter.remove(installation) is True
    assert adapter.inspect(installation.server_name) is None

    raw = (
        yaml.safe_load(path.read_text())
        if client == "hermes"
        else json.loads(path.read_text())
    )
    assert raw["unrelated"] == {"keep": True}
    assert all("delegated" not in " ".join(command) for command in runner.commands)


def test_command_backed_adapters_use_each_clients_native_stdio_registration() -> None:
    claude = ClaudeCodeAdapter()
    hermes = HermesAdapter()
    openclaw = OpenClawAdapter()

    claude_installation = _installation("claude-code")
    assert claude.install_command(claude_installation) == [
        "claude",
        "mcp",
        "add",
        "--transport",
        "stdio",
        "--scope",
        "user",
        "connection-hub-agent",
        "--",
        "/opt/tools/connection-hub",
        *claude_installation.args,
    ]
    assert claude.remove_command(claude_installation) == [
        "claude",
        "mcp",
        "remove",
        "--scope",
        "user",
        "connection-hub-agent",
    ]

    hermes_installation = _installation("hermes")
    assert hermes.install_command(hermes_installation) == [
        "hermes",
        "mcp",
        "add",
        "connection-hub-agent",
        "--command",
        "/opt/tools/connection-hub",
        "--args",
        *hermes_installation.args,
    ]
    assert hermes.remove_command(hermes_installation) == [
        "hermes",
        "mcp",
        "remove",
        "connection-hub-agent",
    ]

    openclaw_installation = _installation("openclaw")
    assert openclaw.install_command(openclaw_installation) == [
        "openclaw",
        "mcp",
        "set",
        "connection-hub-agent",
        json.dumps(openclaw_installation.to_entry(), separators=(",", ":")),
    ]
    assert openclaw.remove_command(openclaw_installation) == [
        "openclaw",
        "mcp",
        "unset",
        "connection-hub-agent",
    ]


def test_claude_desktop_round_trip_is_atomic_and_preserves_other_configuration(
    tmp_path,
) -> None:
    path = tmp_path / "Claude" / "claude_desktop_config.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "preferences": {"theme": "system"},
                "mcpServers": {"existing": {"command": "/bin/existing", "args": []}},
            }
        )
    )
    path.chmod(0o600)
    adapter = ClaudeDesktopAdapter(config_path=path)
    installation = _installation("claude-desktop")

    assert adapter.install(installation) is True
    installed_inode = path.stat().st_ino
    assert adapter.install(installation) is False
    assert path.stat().st_ino == installed_inode
    assert installation.owns_entry(adapter.inspect(installation.server_name))
    assert adapter.remove(installation) is True
    removed_inode = path.stat().st_ino
    assert adapter.remove(installation) is False
    assert path.stat().st_ino == removed_inode

    final = json.loads(path.read_text())
    assert final["preferences"] == {"theme": "system"}
    assert final["mcpServers"]["existing"]["command"] == "/bin/existing"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(path.parent.glob("*.tmp"))


def test_claude_desktop_install_does_not_create_an_absent_application_directory(
    tmp_path,
) -> None:
    path = tmp_path / "missing-claude" / "claude_desktop_config.json"
    adapter = ClaudeDesktopAdapter(config_path=path)

    with pytest.raises(ClientConfigurationError) as raised:
        adapter.install(_installation("claude-desktop"))

    assert raised.value.code == "client_not_installed"
    assert not path.parent.exists()


def test_adapter_refuses_to_remove_an_entry_changed_by_the_user(tmp_path) -> None:
    path = tmp_path / "claude_desktop_config.json"
    adapter = ClaudeDesktopAdapter(config_path=path)
    installation = _installation("claude-desktop")
    adapter.install(installation)
    data = json.loads(path.read_text())
    data["mcpServers"][installation.server_name]["args"].append("user-change")
    path.write_text(json.dumps(data))

    with pytest.raises(ClientConfigurationError) as raised:
        adapter.remove(installation)
    assert raised.value.code == "client_entry_changed"
    assert installation.server_name in json.loads(path.read_text())["mcpServers"]


def test_client_service_tracks_and_removes_only_its_managed_entry(tmp_path) -> None:
    profiles = ProfileStore(tmp_path / "profiles.json")
    installations = InstallationStore(tmp_path / "installations.json")
    credentials = _Credentials()
    profile = CallerProfile.create(name="agent", endpoint="https://hub.example/mcp")
    profiles.add(profile)
    credentials.put(profile.credential_ref, "test-bearer")
    adapter = ClaudeDesktopAdapter(config_path=tmp_path / "desktop.json")
    service = ClientService(
        profiles=profiles,
        installations=installations,
        credentials=credentials,
        adapters={"claude-desktop": adapter},
        launch=HelperLaunch(command="/opt/tools/connection-hub"),
    )

    installed = service.install(client="claude-desktop", profile_name="agent")
    assert installed.changed is True
    assert installations.get("claude-desktop", "connection-hub-agent") is not None

    repeated = service.install(client="claude-desktop", profile_name="agent")
    assert repeated.changed is False

    removed = service.remove(
        client="claude-desktop", server_name="connection-hub-agent"
    )
    assert removed.changed is True
    assert installations.list() == []

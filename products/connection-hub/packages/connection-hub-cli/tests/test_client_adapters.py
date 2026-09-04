from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from connection_hub_cli.clients.adapters import (
    ClaudeCodeAdapter,
    ClaudeDesktopAdapter,
    CodexAdapter,
    HermesAdapter,
    OpenClawAdapter,
    claude_desktop_config_path,
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


@pytest.mark.parametrize(
    "platform_name,environment,expected",
    [
        (
            "Darwin",
            {},
            "Library/Application Support/Claude/claude_desktop_config.json",
        ),
        (
            "Windows",
            {},
            "AppData/Roaming/Claude/claude_desktop_config.json",
        ),
        ("Linux", {}, ".config/Claude/claude_desktop_config.json"),
    ],
)
def test_claude_desktop_config_path_uses_the_platform_user_location(
    platform_name: str,
    environment: dict[str, str],
    expected: str,
    tmp_path,
) -> None:
    assert (
        claude_desktop_config_path(
            platform_name=platform_name,
            home=tmp_path,
            environment=environment,
        )
        == tmp_path / expected
    )


@pytest.mark.parametrize(
    "platform_name,variable",
    [("Windows", "APPDATA"), ("Linux", "XDG_CONFIG_HOME")],
)
def test_claude_desktop_config_path_honors_absolute_platform_override(
    platform_name: str,
    variable: str,
    tmp_path,
) -> None:
    root = tmp_path / "custom-config"

    assert (
        claude_desktop_config_path(
            platform_name=platform_name,
            home=tmp_path / "home",
            environment={variable: str(root)},
        )
        == root / "Claude" / "claude_desktop_config.json"
    )


def test_claude_desktop_config_path_rejects_an_unsupported_platform(tmp_path) -> None:
    with pytest.raises(ClientConfigurationError) as raised:
        claude_desktop_config_path(
            platform_name="Plan9",
            home=tmp_path,
            environment={},
        )

    assert raised.value.code == "unsupported_client_platform"


@pytest.mark.parametrize(
    "platform_name,variable",
    [("Windows", "APPDATA"), ("Linux", "XDG_CONFIG_HOME")],
)
def test_claude_desktop_config_path_ignores_relative_platform_override(
    platform_name: str,
    variable: str,
    tmp_path,
) -> None:
    path = claude_desktop_config_path(
        platform_name=platform_name,
        home=tmp_path,
        environment={variable: "relative/config"},
    )

    assert path.is_relative_to(tmp_path)


class _ClientRunner:
    def __init__(self, client: str, path: Path) -> None:
        self.client = client
        self.path = path
        self.commands: list[list[str]] = []
        self.codex_entries: dict[str, dict] = {}
        self.support_oauth = True
        self.support_removal = True
        self.fail_remove = False

    def available(self, executable: str) -> bool:
        return True

    def run(self, argv) -> CommandResult:
        argv = list(argv)
        self.commands.append(argv)
        if "--help" in argv:
            subcommand = argv[2]
            if subcommand == "login":
                return CommandResult(
                    returncode=0,
                    stdout=(
                        "Authenticate login --oauth-client-registration"
                        if self.support_oauth
                        else "login unavailable"
                    ),
                )
            if subcommand == "logout":
                labels = {
                    "claude-code": "Clear stored OAuth credentials",
                    "codex": "deauthenticate",
                    "hermes": "logout",
                    "openclaw": "logout",
                }
                return CommandResult(
                    returncode=0 if self.support_oauth else 2,
                    stdout=(labels[self.client] if self.support_oauth else ""),
                )
            if subcommand in {"remove", "unset"}:
                labels = {
                    "claude-code": "remove --scope",
                    "codex": "remove",
                    "hermes": "remove",
                    "openclaw": "unset",
                }
                return CommandResult(
                    returncode=0 if self.support_removal else 2,
                    stdout=(labels[self.client] if self.support_removal else ""),
                )
            help_text = {
                "claude-code": "--transport stdio http",
                "codex": "-- <COMMAND> stdio --url --oauth-client-registration",
                "hermes": "--command --args --url --auth",
                "openclaw": "set",
            }[self.client]
            if not self.support_oauth:
                help_text = {
                    "claude-code": "--transport stdio",
                    "codex": "-- <COMMAND> stdio",
                    "hermes": "--command --args",
                    "openclaw": "set",
                }[self.client]
            return CommandResult(returncode=0, stdout=help_text)

        if self.client == "claude-code":
            data = (
                json.loads(self.path.read_text())
                if self.path.exists()
                else {"unrelated": {"keep": True}}
            )
            if argv[2] == "add":
                transport = argv[argv.index("--transport") + 1]
                if transport == "stdio":
                    marker = argv.index("--")
                    name = argv[marker - 1]
                    entry = {
                        "type": "stdio",
                        "command": argv[marker + 1],
                        "args": argv[marker + 2 :],
                        "env": {},
                    }
                else:
                    name = argv[-2]
                    entry = {"type": "http", "url": argv[-1]}
                data.setdefault("mcpServers", {})[name] = entry
            elif argv[2] == "remove":
                if self.fail_remove:
                    return CommandResult(returncode=2, stderr="remove failed")
                data.get("mcpServers", {}).pop(argv[-1], None)
            self.path.write_text(json.dumps(data))
        elif self.client == "codex":
            if argv[2] == "get":
                entry = self.codex_entries.get(argv[3])
                if entry is None:
                    return CommandResult(
                        returncode=1,
                        stderr=f"No MCP server named '{argv[3]}' found.",
                    )
                return CommandResult(returncode=0, stdout=json.dumps(entry))
            if argv[2] == "add":
                name = argv[3]
                if "--url" in argv:
                    transport = {
                        "type": "streamable_http",
                        "url": argv[argv.index("--url") + 1],
                        "bearer_token_env_var": None,
                        "http_headers": None,
                        "env_http_headers": None,
                        "http_headers_helper": None,
                    }
                else:
                    marker = argv.index("--")
                    transport = {
                        "type": "stdio",
                        "command": argv[marker + 1],
                        "args": argv[marker + 2 :],
                    }
                self.codex_entries[name] = {"name": name, "transport": transport}
            elif argv[2] == "remove":
                if self.fail_remove:
                    return CommandResult(returncode=2, stderr="remove failed")
                self.codex_entries.pop(argv[3], None)
        elif self.client == "hermes":
            data = (
                yaml.safe_load(self.path.read_text())
                if self.path.exists()
                else {"unrelated": {"keep": True}}
            )
            if argv[2] == "add":
                if "--url" in argv:
                    name = argv[-1]
                    entry = {
                        "url": argv[argv.index("--url") + 1],
                        "auth": argv[argv.index("--auth") + 1],
                        "timeout": 60,
                        "connect_timeout": 20,
                        "enabled": True,
                    }
                else:
                    name = argv[3]
                    command_index = argv.index("--command")
                    args_index = argv.index("--args")
                    entry = {
                        "command": argv[command_index + 1],
                        "args": argv[args_index + 1 :],
                        "enabled": True,
                    }
                data.setdefault("mcp_servers", {})[name] = entry
            elif argv[2] == "remove":
                if self.fail_remove:
                    return CommandResult(returncode=2, stderr="remove failed")
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
            elif argv[2] == "unset":
                if self.fail_remove:
                    return CommandResult(returncode=2, stderr="remove failed")
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


@pytest.mark.parametrize("client", ["claude-code", "codex", "hermes", "openclaw"])
def test_command_backed_adapters_round_trip_without_touching_unrelated_entries(
    tmp_path,
    client: str,
) -> None:
    suffix = "yaml" if client == "hermes" else "json"
    path = tmp_path / f"config.{suffix}"
    runner = _ClientRunner(client, path)
    if client == "claude-code":
        adapter = ClaudeCodeAdapter(runner=runner, config_path=path)
    elif client == "codex":
        adapter = CodexAdapter(runner=runner)
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

    if client != "codex":
        raw = (
            yaml.safe_load(path.read_text())
            if client == "hermes"
            else json.loads(path.read_text())
        )
        assert raw["unrelated"] == {"keep": True}
    assert all("delegated" not in " ".join(command) for command in runner.commands)


def test_command_backed_adapters_use_each_clients_native_stdio_registration() -> None:
    claude = ClaudeCodeAdapter()
    codex = CodexAdapter()
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

    codex_installation = _installation("codex")
    assert codex.install_command(codex_installation) == [
        "codex",
        "mcp",
        "add",
        "connection-hub-agent",
        "--",
        "/opt/tools/connection-hub",
        *codex_installation.args,
    ]
    assert codex.remove_command(codex_installation) == [
        "codex",
        "mcp",
        "remove",
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


@pytest.mark.parametrize("client", ["claude-code", "codex", "hermes", "openclaw"])
def test_native_oauth_adapters_round_trip_without_credentials_in_config(
    tmp_path,
    client: str,
) -> None:
    path = tmp_path / ("config.yaml" if client == "hermes" else "config.json")
    runner = _ClientRunner(client, path)
    if client == "claude-code":
        adapter = ClaudeCodeAdapter(runner=runner, config_path=path)
    elif client == "codex":
        adapter = CodexAdapter(runner=runner)
    elif client == "hermes":
        adapter = HermesAdapter(runner=runner, config_path=path)
    else:
        adapter = OpenClawAdapter(runner=runner, config_path=path)
    installation = ManagedInstallation.create_oauth(
        client=client,
        endpoint="https://hub.example/mcp",
        server_name="connection-hub-agent",
        installation_id="b" * 32,
    )

    assert adapter.install(installation) is True
    entry = adapter.inspect(installation.server_name)
    assert installation.owns_entry(entry)
    assert "secret" not in json.dumps(entry).lower()
    assert "authorization" not in json.dumps(entry).lower()
    assert adapter.authorization_command(installation)
    assert adapter.remove(installation) is True


def test_explicit_oauth_rejection_does_not_write_client_configuration(tmp_path) -> None:
    path = tmp_path / "config.json"
    runner = _ClientRunner("claude-code", path)
    runner.support_oauth = False
    adapter = ClaudeCodeAdapter(runner=runner, config_path=path)
    installation = ManagedInstallation.create_oauth(
        client="claude-code",
        endpoint="https://hub.example/mcp",
        server_name="connection-hub-agent",
    )

    with pytest.raises(ClientConfigurationError) as raised:
        adapter.install(installation)

    assert raised.value.code == "client_mode_unavailable"
    assert not path.exists()


def test_bridge_rejection_without_managed_removal_does_not_write_configuration(
    tmp_path,
) -> None:
    path = tmp_path / "config.json"
    runner = _ClientRunner("claude-code", path)
    runner.support_removal = False
    adapter = ClaudeCodeAdapter(runner=runner, config_path=path)

    with pytest.raises(ClientConfigurationError) as raised:
        adapter.install(_installation("claude-code"))

    assert raised.value.code == "client_mode_unavailable"
    assert not path.exists()


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


def test_claude_desktop_write_uses_path_chmod_when_fchmod_is_unavailable(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "Claude" / "claude_desktop_config.json"
    path.parent.mkdir()
    monkeypatch.setattr(
        "connection_hub_cli.filesystem.os",
        SimpleNamespace(chmod=os.chmod),
    )
    adapter = ClaudeDesktopAdapter(config_path=path)

    assert adapter.install(_installation("claude-desktop")) is True
    assert json.loads(path.read_text())["mcpServers"]


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


def test_client_service_auto_prefers_native_oauth_for_a_remote_endpoint(
    tmp_path,
) -> None:
    profiles = ProfileStore(tmp_path / "profiles.json")
    installations = InstallationStore(tmp_path / "installations.json")
    path = tmp_path / "claude.json"
    runner = _ClientRunner("claude-code", path)
    adapter = ClaudeCodeAdapter(runner=runner, config_path=path)
    service = ClientService(
        profiles=profiles,
        installations=installations,
        credentials=_Credentials(),
        adapters={"claude-code": adapter},
        launch=HelperLaunch(command="/opt/tools/connection-hub"),
    )

    result = service.install(
        client="claude-code",
        endpoint="https://hub.example/mcp",
        mode="auto",
    )

    assert result.installation.mode == "oauth"
    assert result.installation.profile is None
    assert result.selection_reason == "native_oauth_available"
    assert result.authorization_command == (
        "claude",
        "mcp",
        "login",
        "connection-hub-claude-code",
    )


def test_client_service_auto_falls_back_only_when_a_local_profile_is_supplied(
    tmp_path,
) -> None:
    profiles = ProfileStore(tmp_path / "profiles.json")
    installations = InstallationStore(tmp_path / "installations.json")
    credentials = _Credentials()
    profile = CallerProfile.create(name="agent", endpoint="https://hub.example/mcp")
    profiles.add(profile)
    credentials.put(profile.credential_ref, "test-bearer")
    path = tmp_path / "claude.json"
    runner = _ClientRunner("claude-code", path)
    runner.support_oauth = False
    adapter = ClaudeCodeAdapter(runner=runner, config_path=path)
    service = ClientService(
        profiles=profiles,
        installations=installations,
        credentials=credentials,
        adapters={"claude-code": adapter},
        launch=HelperLaunch(command="/opt/tools/connection-hub"),
    )

    result = service.install(
        client="claude-code",
        profile_name="agent",
        endpoint=profile.endpoint,
        mode="auto",
    )

    assert result.installation.mode == "bridge"
    assert result.installation.profile == "agent"
    assert result.selection_reason == "native_oauth_unavailable_bridge_selected"


def test_client_service_rolls_back_the_client_entry_when_state_write_fails(
    tmp_path,
    monkeypatch,
) -> None:
    profiles = ProfileStore(tmp_path / "profiles.json")
    installations = InstallationStore(tmp_path / "installations.json")
    credentials = _Credentials()
    profile = CallerProfile.create(name="agent", endpoint="https://hub.example/mcp")
    profiles.add(profile)
    credentials.put(profile.credential_ref, "test-bearer")
    path = tmp_path / "claude.json"
    runner = _ClientRunner("claude-code", path)
    adapter = ClaudeCodeAdapter(runner=runner, config_path=path)
    service = ClientService(
        profiles=profiles,
        installations=installations,
        credentials=credentials,
        adapters={"claude-code": adapter},
        launch=HelperLaunch(command="/opt/tools/connection-hub"),
    )

    def fail_state_write(_installation) -> None:
        raise RuntimeError("state failed")

    monkeypatch.setattr(installations, "add", fail_state_write)

    with pytest.raises(RuntimeError, match="state failed"):
        service.install(client="claude-code", profile_name="agent", mode="bridge")

    assert adapter.inspect("connection-hub-agent") is None


def test_client_service_reports_failed_entry_rollback_after_state_write_failure(
    tmp_path,
    monkeypatch,
) -> None:
    profiles = ProfileStore(tmp_path / "profiles.json")
    installations = InstallationStore(tmp_path / "installations.json")
    credentials = _Credentials()
    profile = CallerProfile.create(name="agent", endpoint="https://hub.example/mcp")
    profiles.add(profile)
    credentials.put(profile.credential_ref, "test-bearer")
    path = tmp_path / "claude.json"
    runner = _ClientRunner("claude-code", path)
    runner.fail_remove = True
    adapter = ClaudeCodeAdapter(runner=runner, config_path=path)
    service = ClientService(
        profiles=profiles,
        installations=installations,
        credentials=credentials,
        adapters={"claude-code": adapter},
        launch=HelperLaunch(command="/opt/tools/connection-hub"),
    )

    def fail_state_write(_installation) -> None:
        raise RuntimeError("state failed")

    monkeypatch.setattr(installations, "add", fail_state_write)

    with pytest.raises(ClientConfigurationError) as raised:
        service.install(client="claude-code", profile_name="agent", mode="bridge")

    assert raised.value.code == "client_install_cleanup_failed"
    assert adapter.inspect("connection-hub-agent") is not None

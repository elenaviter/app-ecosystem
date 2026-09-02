from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Protocol

from connection_hub_cli.clients.command import CommandRunner, SubprocessCommandRunner
from connection_hub_cli.clients.config_files import (
    mutate_json_object,
    nested_value,
    read_json_object,
    read_yaml_object,
)
from connection_hub_cli.errors import ClientConfigurationError
from connection_hub_cli.models import ManagedInstallation


class ClientAdapter(Protocol):
    client: str

    def available(self) -> bool: ...

    def inspect(self, server_name: str) -> Any: ...

    def install(self, installation: ManagedInstallation) -> bool: ...

    def remove(self, installation: ManagedInstallation) -> bool: ...


class _CommandBackedAdapter:
    client: str
    executable: str

    def __init__(self, *, runner: CommandRunner | None = None) -> None:
        self.runner = runner or SubprocessCommandRunner()

    def available(self) -> bool:
        return self.runner.available(self.executable)

    def _require_available(self) -> None:
        if not self.available():
            raise ClientConfigurationError(
                "client_not_installed",
                f"The {self.client} executable is not installed or is not on PATH.",
            )

    def install(self, installation: ManagedInstallation) -> bool:
        self._require_available()
        existing = self.inspect(installation.server_name)
        if existing is not None:
            if installation.owns_entry(existing):
                return False
            raise ClientConfigurationError(
                "client_entry_conflict",
                f"{self.client} already has an MCP entry named '{installation.server_name}' that Connection Hub does not own.",
            )
        result = self.runner.run(self.install_command(installation))
        if result.returncode != 0:
            raise ClientConfigurationError(
                "client_install_failed",
                f"{self.client} rejected the Connection Hub MCP entry.",
            )
        if not installation.owns_entry(self.inspect(installation.server_name)):
            raise ClientConfigurationError(
                "client_install_not_verified",
                f"{self.client} did not retain the expected Connection Hub MCP entry.",
            )
        return True

    def remove(self, installation: ManagedInstallation) -> bool:
        self._require_available()
        existing = self.inspect(installation.server_name)
        if existing is None:
            return False
        if not installation.owns_entry(existing):
            raise ClientConfigurationError(
                "client_entry_changed",
                f"The {self.client} MCP entry '{installation.server_name}' changed after Connection Hub installed it; it was not removed.",
            )
        result = self.runner.run(self.remove_command(installation))
        if result.returncode != 0:
            raise ClientConfigurationError(
                "client_remove_failed",
                f"{self.client} could not remove the Connection Hub MCP entry.",
            )
        if self.inspect(installation.server_name) is not None:
            raise ClientConfigurationError(
                "client_remove_not_verified",
                f"{self.client} still contains the Connection Hub MCP entry after removal.",
            )
        return True

    def install_command(self, installation: ManagedInstallation) -> list[str]:
        raise NotImplementedError

    def remove_command(self, installation: ManagedInstallation) -> list[str]:
        raise NotImplementedError


class ClaudeCodeAdapter(_CommandBackedAdapter):
    client = "claude-code"
    executable = "claude"

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        config_path: Path | None = None,
    ) -> None:
        super().__init__(runner=runner)
        self.config_path = config_path or Path.home() / ".claude.json"

    def inspect(self, server_name: str) -> Any:
        return nested_value(
            read_json_object(self.config_path), ("mcpServers", server_name)
        )

    def install_command(self, installation: ManagedInstallation) -> list[str]:
        return [
            self.executable,
            "mcp",
            "add",
            "--transport",
            "stdio",
            "--scope",
            "user",
            installation.server_name,
            "--",
            installation.command,
            *installation.args,
        ]

    def remove_command(self, installation: ManagedInstallation) -> list[str]:
        return [
            self.executable,
            "mcp",
            "remove",
            "--scope",
            "user",
            installation.server_name,
        ]


class HermesAdapter(_CommandBackedAdapter):
    client = "hermes"
    executable = "hermes"

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        config_path: Path | None = None,
    ) -> None:
        super().__init__(runner=runner)
        hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
        self.config_path = config_path or hermes_home / "config.yaml"

    def inspect(self, server_name: str) -> Any:
        return nested_value(
            read_yaml_object(self.config_path), ("mcp_servers", server_name)
        )

    def install_command(self, installation: ManagedInstallation) -> list[str]:
        return [
            self.executable,
            "mcp",
            "add",
            installation.server_name,
            "--command",
            installation.command,
            "--args",
            *installation.args,
        ]

    def remove_command(self, installation: ManagedInstallation) -> list[str]:
        return [self.executable, "mcp", "remove", installation.server_name]


class OpenClawAdapter(_CommandBackedAdapter):
    client = "openclaw"
    executable = "openclaw"

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        config_path: Path | None = None,
    ) -> None:
        super().__init__(runner=runner)
        openclaw_home = Path(os.environ.get("OPENCLAW_HOME", Path.home() / ".openclaw"))
        self.config_path = config_path or openclaw_home / "openclaw.json"

    def inspect(self, server_name: str) -> Any:
        config = read_json_object(self.config_path, json5_allowed=True)
        return nested_value(config, ("mcp", "servers", server_name))

    def install_command(self, installation: ManagedInstallation) -> list[str]:
        return [
            self.executable,
            "mcp",
            "set",
            installation.server_name,
            json.dumps(installation.to_entry(), separators=(",", ":")),
        ]

    def remove_command(self, installation: ManagedInstallation) -> list[str]:
        return [self.executable, "mcp", "unset", installation.server_name]


class ClaudeDesktopAdapter:
    client = "claude-desktop"

    def __init__(self, *, config_path: Path | None = None) -> None:
        self.config_path = config_path or (
            Path.home()
            / "Library"
            / "Application Support"
            / "Claude"
            / "claude_desktop_config.json"
        )

    def available(self) -> bool:
        return self.config_path.exists() or self.config_path.parent.exists()

    def _require_available(self) -> None:
        if not self.available():
            raise ClientConfigurationError(
                "client_not_installed",
                "The Claude Desktop configuration directory does not exist.",
            )

    def inspect(self, server_name: str) -> Any:
        return nested_value(
            read_json_object(self.config_path), ("mcpServers", server_name)
        )

    def install(self, installation: ManagedInstallation) -> bool:
        self._require_available()

        def add_entry(config: dict[str, Any]) -> bool:
            servers = config.setdefault("mcpServers", {})
            if not isinstance(servers, dict):
                raise ClientConfigurationError(
                    "invalid_client_config",
                    "Claude Desktop mcpServers must be an object.",
                )
            existing = servers.get(installation.server_name)
            if existing is not None:
                if installation.owns_entry(existing):
                    return False
                raise ClientConfigurationError(
                    "client_entry_conflict",
                    f"Claude Desktop already has an MCP entry named '{installation.server_name}' that Connection Hub does not own.",
                )
            servers[installation.server_name] = installation.to_entry()
            return True

        return mutate_json_object(self.config_path, add_entry)

    def remove(self, installation: ManagedInstallation) -> bool:
        self._require_available()

        def remove_entry(config: dict[str, Any]) -> bool:
            servers = config.get("mcpServers")
            if not isinstance(servers, dict) or installation.server_name not in servers:
                return False
            if not installation.owns_entry(servers[installation.server_name]):
                raise ClientConfigurationError(
                    "client_entry_changed",
                    f"The Claude Desktop MCP entry '{installation.server_name}' changed after Connection Hub installed it; it was not removed.",
                )
            del servers[installation.server_name]
            return True

        return mutate_json_object(self.config_path, remove_entry)


def build_client_adapters(
    *,
    runner: CommandRunner | None = None,
) -> dict[str, ClientAdapter]:
    return {
        "claude-code": ClaudeCodeAdapter(runner=runner),
        "claude-desktop": ClaudeDesktopAdapter(),
        "hermes": HermesAdapter(runner=runner),
        "openclaw": OpenClawAdapter(runner=runner),
    }

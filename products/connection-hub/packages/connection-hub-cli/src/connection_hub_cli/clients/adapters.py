from __future__ import annotations

import json
import os
import platform
from collections.abc import Mapping
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

    def ensure_mode(self, mode: str) -> None: ...

    def inspect(self, server_name: str) -> Any: ...

    def install(self, installation: ManagedInstallation) -> bool: ...

    def rollback_install(self, installation: ManagedInstallation) -> None: ...

    def remove(self, installation: ManagedInstallation) -> bool: ...

    def authorization_command(
        self, installation: ManagedInstallation
    ) -> list[str] | None: ...


def claude_desktop_config_path(
    *,
    platform_name: str | None = None,
    home: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Resolve Claude Desktop's local MCP configuration on desktop platforms."""

    selected_platform = platform_name or platform.system()
    selected_home = Path.home() if home is None else Path(home)
    selected_environment = os.environ if environment is None else environment
    if selected_platform == "Darwin":
        root = selected_home / "Library" / "Application Support"
    elif selected_platform == "Windows":
        configured = str(selected_environment.get("APPDATA") or "").strip()
        candidate = Path(configured).expanduser() if configured else None
        root = (
            candidate
            if candidate is not None and candidate.is_absolute()
            else selected_home / "AppData" / "Roaming"
        )
    elif selected_platform == "Linux":
        configured = str(selected_environment.get("XDG_CONFIG_HOME") or "").strip()
        candidate = Path(configured).expanduser() if configured else None
        root = (
            candidate
            if candidate is not None and candidate.is_absolute()
            else selected_home / ".config"
        )
    else:
        raise ClientConfigurationError(
            "unsupported_client_platform",
            "Claude Desktop local MCP configuration is supported on macOS, Windows, and Linux.",
        )
    return root / "Claude" / "claude_desktop_config.json"


class _CommandBackedAdapter:
    client: str
    executable: str

    def __init__(self, *, runner: CommandRunner | None = None) -> None:
        self.runner = runner or SubprocessCommandRunner()
        self._verified_modes: set[str] = set()

    def available(self) -> bool:
        return self.runner.available(self.executable)

    def _require_available(self) -> None:
        if not self.available():
            raise ClientConfigurationError(
                "client_not_installed",
                f"The {self.client} executable is not installed or is not on PATH.",
            )

    def ensure_mode(self, mode: str) -> None:
        self._require_available()
        if mode in self._verified_modes:
            return
        checks = self.capability_checks(mode)
        if not checks:
            raise ClientConfigurationError(
                "client_mode_unavailable",
                f"The installed {self.client} client does not support Connection Hub {mode} installation.",
            )
        for command, markers in checks:
            result = self.runner.run(command)
            output = f"{result.stdout}\n{result.stderr}".lower()
            if result.returncode != 0 or any(
                marker.lower() not in output for marker in markers
            ):
                recovery = (
                    "Upgrade the client or use --mode bridge with a local caller profile."
                    if mode == "oauth"
                    else "Upgrade the client before installing the local stdio bridge."
                )
                raise ClientConfigurationError(
                    "client_mode_unavailable",
                    f"The installed {self.client} client does not expose the required {mode} MCP commands. {recovery}",
                )
        self._verified_modes.add(mode)

    def capability_checks(
        self, mode: str
    ) -> tuple[tuple[list[str], tuple[str, ...]], ...]:
        raise NotImplementedError

    def install(self, installation: ManagedInstallation) -> bool:
        self.ensure_mode(installation.mode)
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
        try:
            retained = installation.owns_entry(self.inspect(installation.server_name))
        except ClientConfigurationError:
            self._rollback_install(installation)
            raise
        if not retained:
            self._rollback_install(installation)
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
        logout = self.logout_command(installation)
        if logout is not None:
            result = self.runner.run(logout)
            if result.returncode != 0:
                raise ClientConfigurationError(
                    "client_oauth_logout_failed",
                    f"{self.client} could not remove OAuth custody for '{installation.server_name}'; its MCP entry was retained.",
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

    def rollback_install(self, installation: ManagedInstallation) -> None:
        """Remove a just-written entry before the interactive OAuth step exists."""

        existing = self.inspect(installation.server_name)
        if existing is None:
            return
        if not installation.owns_entry(existing):
            raise ClientConfigurationError(
                "client_install_cleanup_failed",
                f"The {self.client} MCP entry '{installation.server_name}' changed before installation rollback; remove it with the client before retrying.",
            )
        result = self.runner.run(self.remove_command(installation))
        if result.returncode != 0 or self.inspect(installation.server_name) is not None:
            raise ClientConfigurationError(
                "client_install_cleanup_failed",
                f"{self.client} retained MCP entry '{installation.server_name}' after local installation state failed; remove it with the client before retrying.",
            )

    def _rollback_install(self, installation: ManagedInstallation) -> None:
        result = self.runner.run(self.remove_command(installation))
        if result.returncode != 0:
            raise ClientConfigurationError(
                "client_install_cleanup_failed",
                f"{self.client} retained an MCP entry after installation verification failed. Remove '{installation.server_name}' with the client before retrying.",
            )

    def install_command(self, installation: ManagedInstallation) -> list[str]:
        raise NotImplementedError

    def remove_command(self, installation: ManagedInstallation) -> list[str]:
        raise NotImplementedError

    def logout_command(self, installation: ManagedInstallation) -> list[str] | None:
        return None

    def authorization_command(
        self, installation: ManagedInstallation
    ) -> list[str] | None:
        return None


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

    def capability_checks(
        self, mode: str
    ) -> tuple[tuple[list[str], tuple[str, ...]], ...]:
        add_help = [self.executable, "mcp", "add", "--help"]
        remove_help = [self.executable, "mcp", "remove", "--help"]
        if mode == "bridge":
            return (
                (add_help, ("--transport", "stdio")),
                (remove_help, ("remove", "--scope")),
            )
        if mode == "oauth":
            return (
                (add_help, ("--transport", "http")),
                ([self.executable, "mcp", "login", "--help"], ("authenticate",)),
                ([self.executable, "mcp", "logout", "--help"], ("oauth",)),
                (remove_help, ("remove", "--scope")),
            )
        return ()

    def install_command(self, installation: ManagedInstallation) -> list[str]:
        if installation.mode == "oauth":
            return [
                self.executable,
                "mcp",
                "add",
                "--transport",
                "http",
                "--scope",
                "user",
                installation.server_name,
                str(installation.endpoint),
            ]
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
            str(installation.command),
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

    def logout_command(self, installation: ManagedInstallation) -> list[str] | None:
        if installation.mode == "oauth":
            return [self.executable, "mcp", "logout", installation.server_name]
        return None

    def authorization_command(
        self, installation: ManagedInstallation
    ) -> list[str] | None:
        if installation.mode == "oauth":
            return [self.executable, "mcp", "login", installation.server_name]
        return None


class CodexAdapter(_CommandBackedAdapter):
    client = "codex"
    executable = "codex"

    def inspect(self, server_name: str) -> Any:
        result = self.runner.run([self.executable, "mcp", "get", server_name, "--json"])
        if result.returncode != 0:
            output = f"{result.stdout}\n{result.stderr}".lower()
            if "no mcp server named" in output:
                return None
            raise ClientConfigurationError(
                "client_inspect_failed",
                "Codex could not inspect its MCP configuration.",
            )
        try:
            value = json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError):
            raise ClientConfigurationError(
                "client_inspect_failed",
                "Codex returned an invalid MCP configuration record.",
            ) from None
        if not isinstance(value, dict):
            raise ClientConfigurationError(
                "client_inspect_failed",
                "Codex returned an invalid MCP configuration record.",
            )
        return value

    def capability_checks(
        self, mode: str
    ) -> tuple[tuple[list[str], tuple[str, ...]], ...]:
        add_help = [self.executable, "mcp", "add", "--help"]
        remove_help = [self.executable, "mcp", "remove", "--help"]
        if mode == "bridge":
            return (
                (add_help, ("-- <command>", "stdio")),
                (remove_help, ("remove",)),
            )
        if mode == "oauth":
            return (
                (add_help, ("--url", "--oauth-client-registration")),
                (
                    [self.executable, "mcp", "login", "--help"],
                    ("--oauth-client-registration",),
                ),
                (
                    [self.executable, "mcp", "logout", "--help"],
                    ("deauthenticate",),
                ),
                (remove_help, ("remove",)),
            )
        return ()

    def install_command(self, installation: ManagedInstallation) -> list[str]:
        if installation.mode == "oauth":
            return [
                self.executable,
                "mcp",
                "add",
                installation.server_name,
                "--url",
                str(installation.endpoint),
            ]
        return [
            self.executable,
            "mcp",
            "add",
            installation.server_name,
            "--",
            str(installation.command),
            *installation.args,
        ]

    def remove_command(self, installation: ManagedInstallation) -> list[str]:
        return [self.executable, "mcp", "remove", installation.server_name]

    def logout_command(self, installation: ManagedInstallation) -> list[str] | None:
        if installation.mode == "oauth":
            return [self.executable, "mcp", "logout", installation.server_name]
        return None

    def authorization_command(
        self, installation: ManagedInstallation
    ) -> list[str] | None:
        if installation.mode == "oauth":
            return [
                self.executable,
                "mcp",
                "login",
                installation.server_name,
                "--oauth-client-registration",
                "auto",
            ]
        return None


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

    def capability_checks(
        self, mode: str
    ) -> tuple[tuple[list[str], tuple[str, ...]], ...]:
        add_help = [self.executable, "mcp", "add", "--help"]
        remove_help = [self.executable, "mcp", "remove", "--help"]
        if mode == "bridge":
            return (
                (add_help, ("--command", "--args")),
                (remove_help, ("remove",)),
            )
        if mode == "oauth":
            return (
                (add_help, ("--url", "--auth")),
                ([self.executable, "mcp", "login", "--help"], ("login",)),
                ([self.executable, "mcp", "logout", "--help"], ("logout",)),
                (remove_help, ("remove",)),
            )
        return ()

    def install_command(self, installation: ManagedInstallation) -> list[str]:
        if installation.mode == "oauth":
            return [
                self.executable,
                "mcp",
                "add",
                "--url",
                str(installation.endpoint),
                "--auth",
                "oauth",
                installation.server_name,
            ]
        return [
            self.executable,
            "mcp",
            "add",
            installation.server_name,
            "--command",
            str(installation.command),
            "--args",
            *installation.args,
        ]

    def remove_command(self, installation: ManagedInstallation) -> list[str]:
        return [self.executable, "mcp", "remove", installation.server_name]

    def logout_command(self, installation: ManagedInstallation) -> list[str] | None:
        if installation.mode == "oauth":
            return [self.executable, "mcp", "logout", installation.server_name]
        return None

    def authorization_command(
        self, installation: ManagedInstallation
    ) -> list[str] | None:
        if installation.mode == "oauth":
            return [self.executable, "mcp", "login", installation.server_name]
        return None


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

    def capability_checks(
        self, mode: str
    ) -> tuple[tuple[list[str], tuple[str, ...]], ...]:
        set_help = [self.executable, "mcp", "set", "--help"]
        unset_help = [self.executable, "mcp", "unset", "--help"]
        if mode == "bridge":
            return ((set_help, ("set",)), (unset_help, ("unset",)))
        if mode == "oauth":
            return (
                (set_help, ("set",)),
                ([self.executable, "mcp", "login", "--help"], ("login",)),
                ([self.executable, "mcp", "logout", "--help"], ("logout",)),
                (unset_help, ("unset",)),
            )
        return ()

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

    def logout_command(self, installation: ManagedInstallation) -> list[str] | None:
        if installation.mode == "oauth":
            return [self.executable, "mcp", "logout", installation.server_name]
        return None

    def authorization_command(
        self, installation: ManagedInstallation
    ) -> list[str] | None:
        if installation.mode == "oauth":
            return [self.executable, "mcp", "login", installation.server_name]
        return None


class ClaudeDesktopAdapter:
    client = "claude-desktop"

    def __init__(self, *, config_path: Path | None = None) -> None:
        self.config_path = config_path or claude_desktop_config_path()

    def available(self) -> bool:
        return self.config_path.exists() or self.config_path.parent.exists()

    def _require_available(self) -> None:
        if not self.available():
            raise ClientConfigurationError(
                "client_not_installed",
                "The Claude Desktop configuration directory does not exist.",
            )

    def ensure_mode(self, mode: str) -> None:
        self._require_available()
        if mode == "bridge":
            return
        if mode == "oauth":
            raise ClientConfigurationError(
                "client_oauth_requires_ui",
                "Claude Desktop remote OAuth connectors are installed from its Settings interface; local mcpServers configuration supports the stdio bridge.",
            )
        raise ClientConfigurationError(
            "client_mode_unavailable",
            f"Claude Desktop does not support Connection Hub {mode} installation.",
        )

    def inspect(self, server_name: str) -> Any:
        return nested_value(
            read_json_object(self.config_path), ("mcpServers", server_name)
        )

    def install(self, installation: ManagedInstallation) -> bool:
        self.ensure_mode(installation.mode)

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

    def rollback_install(self, installation: ManagedInstallation) -> None:
        self.remove(installation)

    def authorization_command(
        self, installation: ManagedInstallation
    ) -> list[str] | None:
        return None


def build_client_adapters(
    *,
    runner: CommandRunner | None = None,
) -> dict[str, ClientAdapter]:
    return {
        "claude-code": ClaudeCodeAdapter(runner=runner),
        "claude-desktop": ClaudeDesktopAdapter(),
        "codex": CodexAdapter(runner=runner),
        "hermes": HermesAdapter(runner=runner),
        "openclaw": OpenClawAdapter(runner=runner),
    }

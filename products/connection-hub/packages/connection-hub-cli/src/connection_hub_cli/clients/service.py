from __future__ import annotations

from dataclasses import dataclass

from connection_hub_cli.clients.adapters import ClientAdapter
from connection_hub_cli.credentials import CredentialStore
from connection_hub_cli.errors import ClientConfigurationError, CredentialError
from connection_hub_cli.models import HelperLaunch, ManagedInstallation, validate_name
from connection_hub_cli.state import InstallationStore, ProfileStore


@dataclass(frozen=True, slots=True)
class ClientInstallationResult:
    installation: ManagedInstallation
    changed: bool


class ClientService:
    def __init__(
        self,
        *,
        profiles: ProfileStore,
        installations: InstallationStore,
        credentials: CredentialStore,
        adapters: dict[str, ClientAdapter],
        launch: HelperLaunch,
    ) -> None:
        self.profiles = profiles
        self.installations = installations
        self.credentials = credentials
        self.adapters = adapters
        self.launch = launch

    def _adapter(self, client: str) -> ClientAdapter:
        adapter = self.adapters.get(client)
        if adapter is None:
            raise ClientConfigurationError(
                "unsupported_client", f"Unsupported MCP client: {client}."
            )
        return adapter

    def helper_entry(self, *, profile_name: str) -> dict[str, object]:
        profile = self.profiles.require(profile_name)
        if self.credentials.get(profile.credential_ref) is None:
            raise CredentialError(
                "credential_missing",
                f"Caller profile '{profile_name}' has no credential in macOS Keychain.",
            )
        return {
            "command": self.launch.command,
            "args": [
                *self.launch.prefix_args,
                "mcp",
                "serve",
                "--profile",
                profile.name,
            ],
        }

    def install(
        self,
        *,
        client: str,
        profile_name: str,
        server_name: str | None = None,
    ) -> ClientInstallationResult:
        profile = self.profiles.require(profile_name)
        if self.credentials.get(profile.credential_ref) is None:
            raise CredentialError(
                "credential_missing",
                f"Caller profile '{profile_name}' has no credential in macOS Keychain.",
            )
        selected_name = validate_name(
            server_name or f"connection-hub-{profile.name}",
            field="MCP server name",
        )
        adapter = self._adapter(client)
        current = self.installations.get(client, selected_name)
        if current is not None:
            if current.profile != profile.name:
                raise ClientConfigurationError(
                    "installation_profile_conflict",
                    f"Connection Hub already manages '{selected_name}' for a different caller profile.",
                )
            existing_entry = adapter.inspect(selected_name)
            if not current.owns_entry(existing_entry):
                raise ClientConfigurationError(
                    "client_entry_changed",
                    f"The {client} MCP entry '{selected_name}' no longer matches its managed installation.",
                )
            return ClientInstallationResult(current, changed=False)

        installation = ManagedInstallation.create(
            client=client,
            profile=profile.name,
            server_name=selected_name,
            launch=self.launch,
        )
        changed = adapter.install(installation)
        try:
            self.installations.add(installation)
        except Exception:
            if changed:
                try:
                    adapter.remove(installation)
                except ClientConfigurationError:
                    pass
            raise
        return ClientInstallationResult(installation, changed=changed)

    def remove(self, *, client: str, server_name: str) -> ClientInstallationResult:
        installation = self.installations.get(client, server_name)
        if installation is None:
            raise ClientConfigurationError(
                "installation_not_found",
                f"Connection Hub does not manage '{server_name}' for {client}.",
            )
        changed = self._adapter(client).remove(installation)
        self.installations.remove(client, server_name)
        return ClientInstallationResult(installation, changed=changed)

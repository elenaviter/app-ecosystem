from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from connection_hub_cli.clients.adapters import ClientAdapter
from connection_hub_cli.credentials import CredentialStore
from connection_hub_cli.errors import ClientConfigurationError, CredentialError
from connection_hub_cli.models import (
    HelperLaunch,
    ManagedInstallation,
    validate_endpoint,
    validate_name,
)
from connection_hub_cli.state import InstallationStore, ProfileStore

if TYPE_CHECKING:
    from connection_hub_cli.authorization.profile_session import (
        OAuthProfileSessionService,
    )


@dataclass(frozen=True, slots=True)
class ClientInstallationResult:
    installation: ManagedInstallation
    changed: bool
    requested_mode: str
    selection_reason: str
    authorization_command: tuple[str, ...] | None = None


class ClientService:
    def __init__(
        self,
        *,
        profiles: ProfileStore,
        installations: InstallationStore,
        credentials: CredentialStore,
        adapters: dict[str, ClientAdapter],
        launch: HelperLaunch,
        oauth_sessions: OAuthProfileSessionService | None = None,
    ) -> None:
        self.profiles = profiles
        self.installations = installations
        self.credentials = credentials
        self.adapters = adapters
        self.launch = launch
        self.oauth_sessions = oauth_sessions

    def _adapter(self, client: str) -> ClientAdapter:
        adapter = self.adapters.get(client)
        if adapter is None:
            raise ClientConfigurationError(
                "unsupported_client", f"Unsupported MCP client: {client}."
            )
        return adapter

    def helper_entry(self, *, profile_name: str) -> dict[str, object]:
        profile = self.profiles.require(profile_name)
        if not self._credential_present(profile):
            raise CredentialError(
                "credential_missing",
                f"Caller profile '{profile_name}' has no credential in the operating-system credential store.",
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
        profile_name: str | None = None,
        endpoint: str | None = None,
        mode: str = "auto",
        server_name: str | None = None,
    ) -> ClientInstallationResult:
        requested_mode = str(mode or "").strip().lower()
        if requested_mode not in {"auto", "oauth", "bridge"}:
            raise ClientConfigurationError(
                "invalid_installation_mode",
                "The client installation mode must be auto, oauth, or bridge.",
            )
        profile = self.profiles.require(profile_name) if profile_name else None
        target = validate_endpoint(endpoint) if endpoint else None
        if profile is not None and target is not None and profile.endpoint != target:
            raise ClientConfigurationError(
                "installation_target_mismatch",
                "The caller profile and remote endpoint select different MCP targets.",
            )
        if profile is None and target is None:
            raise ClientConfigurationError(
                "installation_target_required",
                "Client installation requires --endpoint for native OAuth or --profile for the local bridge.",
            )

        adapter = self._adapter(client)
        selected_mode, selection_reason = self._select_mode(
            adapter=adapter,
            requested_mode=requested_mode,
            profile_available=profile is not None,
            endpoint_available=target is not None,
        )
        if selected_mode == "bridge":
            if profile is None:
                raise ClientConfigurationError(
                    "bridge_profile_required",
                    "Bridge installation requires a local caller profile.",
                )
            if not self._credential_present(profile):
                raise CredentialError(
                    "credential_missing",
                    f"Caller profile '{profile.name}' has no credential in the operating-system credential store.",
                )
        elif target is None:
            raise ClientConfigurationError(
                "oauth_endpoint_required",
                "Native OAuth installation requires the governed MCP endpoint in --endpoint.",
            )

        selected_name = validate_name(
            server_name
            or (
                f"connection-hub-{profile.name}"
                if profile is not None
                else f"connection-hub-{client}"
            ),
            field="MCP server name",
        )
        current = self.installations.get(client, selected_name)
        if current is not None:
            selected_profile = profile.name if selected_mode == "bridge" else None
            selected_endpoint = target if selected_mode == "oauth" else None
            if (
                current.mode != selected_mode
                or current.profile != selected_profile
                or current.endpoint != selected_endpoint
            ):
                raise ClientConfigurationError(
                    "installation_target_conflict",
                    f"Connection Hub already manages '{selected_name}' with a different mode or target.",
                )
            existing_entry = adapter.inspect(selected_name)
            if not current.owns_entry(existing_entry):
                raise ClientConfigurationError(
                    "client_entry_changed",
                    f"The {client} MCP entry '{selected_name}' no longer matches its managed installation.",
                )
            authorization = adapter.authorization_command(current)
            return ClientInstallationResult(
                current,
                changed=False,
                requested_mode=requested_mode,
                selection_reason=selection_reason,
                authorization_command=(
                    tuple(authorization) if authorization is not None else None
                ),
            )

        if selected_mode == "bridge":
            assert profile is not None
            installation = ManagedInstallation.create_bridge(
                client=client,
                profile=profile.name,
                server_name=selected_name,
                launch=self.launch,
            )
        else:
            assert target is not None
            installation = ManagedInstallation.create_oauth(
                client=client,
                endpoint=target,
                server_name=selected_name,
            )
        changed = adapter.install(installation)
        try:
            self.installations.add(installation)
        except Exception:
            if changed:
                try:
                    adapter.rollback_install(installation)
                except ClientConfigurationError:
                    raise ClientConfigurationError(
                        "client_install_cleanup_failed",
                        f"The {client} MCP entry '{selected_name}' may remain after local installation state failed; remove it with the client before retrying.",
                    ) from None
            raise
        authorization = adapter.authorization_command(installation)
        return ClientInstallationResult(
            installation,
            changed=changed,
            requested_mode=requested_mode,
            selection_reason=selection_reason,
            authorization_command=(
                tuple(authorization) if authorization is not None else None
            ),
        )

    @staticmethod
    def _select_mode(
        *,
        adapter: ClientAdapter,
        requested_mode: str,
        profile_available: bool,
        endpoint_available: bool,
    ) -> tuple[str, str]:
        if requested_mode == "bridge":
            adapter.ensure_mode("bridge")
            return "bridge", "operator_selected_local_custody"
        if requested_mode == "oauth":
            adapter.ensure_mode("oauth")
            return "oauth", "operator_selected_native_oauth"
        if not endpoint_available:
            adapter.ensure_mode("bridge")
            return "bridge", "local_profile_selected"
        try:
            adapter.ensure_mode("oauth")
        except ClientConfigurationError as exc:
            if not profile_available or exc.code not in {
                "client_mode_unavailable",
                "client_oauth_requires_ui",
            }:
                raise
            adapter.ensure_mode("bridge")
            return "bridge", "native_oauth_unavailable_bridge_selected"
        return "oauth", "native_oauth_available"

    def _credential_present(self, profile) -> bool:
        if getattr(profile, "auth_type", "static_bearer") != "oauth":
            return self.credentials.get(profile.credential_ref) is not None
        if self.oauth_sessions is None:
            return False
        return self.oauth_sessions.credential_present(profile)

    def remove(self, *, client: str, server_name: str) -> ClientInstallationResult:
        installation = self.installations.get(client, server_name)
        if installation is None:
            raise ClientConfigurationError(
                "installation_not_found",
                f"Connection Hub does not manage '{server_name}' for {client}.",
            )
        changed = self._adapter(client).remove(installation)
        self.installations.remove(client, server_name)
        return ClientInstallationResult(
            installation,
            changed=changed,
            requested_mode=installation.mode,
            selection_reason="managed_installation_removed",
        )

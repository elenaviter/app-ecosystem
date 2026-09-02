from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from connection_hub_cli.clients.adapters import ClientAdapter
from connection_hub_cli.credentials import CredentialStore
from connection_hub_cli.errors import ConnectionHubCliError
from connection_hub_cli.profiles import ProfileService
from connection_hub_cli.state import InstallationStore, ProfileStore


@dataclass(frozen=True, slots=True)
class Diagnostic:
    code: str
    severity: str
    summary: str
    recovery: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "summary": self.summary,
            "recovery": self.recovery,
        }


def _mode(path: Path) -> int | None:
    if not path.exists():
        return None
    return stat.S_IMODE(path.stat().st_mode)


async def collect_diagnostics(
    *,
    profiles: ProfileStore,
    installations: InstallationStore,
    credentials: CredentialStore,
    profile_service: ProfileService,
    adapters: dict[str, ClientAdapter],
    probe: bool,
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    try:
        credentials.verify_ready()
    except ConnectionHubCliError as exc:
        diagnostics.append(
            Diagnostic(
                code=exc.code,
                severity="error",
                summary=(
                    "The configured credential store is not writable: "
                    f"{credentials.backend_name()}."
                ),
                recovery=(
                    "Run from the logged-in macOS user session with an unlocked login "
                    "Keychain. If it still fails, repair that Keychain and run "
                    "connection-hub doctor again."
                ),
            )
        )
    else:
        diagnostics.append(
            Diagnostic(
                code="credential_store_ready",
                severity="ok",
                summary=(
                    "Delegated caller credentials passed a temporary write, read, and "
                    f"delete check in {credentials.backend_name()}."
                ),
            )
        )

    root_mode = _mode(profiles.path.parent)
    if root_mode is None:
        diagnostics.append(
            Diagnostic(
                code="state_directory_not_created",
                severity="ok",
                summary=(
                    "The local state directory has not been created because no state "
                    "has been stored."
                ),
            )
        )
    elif root_mode & 0o077:
        diagnostics.append(
            Diagnostic(
                code="state_directory_permissions",
                severity="error",
                summary="The Connection Hub local state directory is accessible by other local users.",
                recovery=f"Run: chmod 700 {profiles.path.parent}",
            )
        )
    else:
        diagnostics.append(
            Diagnostic(
                code="state_directory_private",
                severity="ok",
                summary="The Connection Hub local state directory is private to this operating-system user.",
            )
        )

    for path in (profiles.path, installations.path):
        mode = _mode(path)
        if mode is not None and mode & 0o077:
            diagnostics.append(
                Diagnostic(
                    code="state_file_permissions",
                    severity="error",
                    summary=f"Local state file permissions are too broad: {path}.",
                    recovery=f"Run: chmod 600 {path}",
                )
            )

    known_profiles = {profile.name: profile for profile in profiles.list()}
    for profile in known_profiles.values():
        try:
            present = profile_service.credential_present(profile)
        except ConnectionHubCliError as exc:
            diagnostics.append(
                Diagnostic(
                    code=exc.code,
                    severity="error",
                    summary=f"Caller profile '{profile.name}' could not read its credential store record.",
                )
            )
            continue
        if not present:
            diagnostics.append(
                Diagnostic(
                    code="credential_missing",
                    severity="error",
                    summary=f"Caller profile '{profile.name}' has no Keychain credential.",
                    recovery=f"Run: connection-hub profile credential replace {profile.name}",
                )
            )
            continue
        diagnostics.append(
            Diagnostic(
                code="profile_credential_ready",
                severity="ok",
                summary=f"Caller profile '{profile.name}' has a Keychain credential.",
            )
        )
        if probe:
            try:
                result = await profile_service.probe_profile(profile.name)
            except ConnectionHubCliError as exc:
                diagnostics.append(
                    Diagnostic(
                        code=exc.code,
                        severity="error",
                        summary=f"Caller profile '{profile.name}' could not connect to its governed MCP endpoint.",
                    )
                )
            else:
                diagnostics.append(
                    Diagnostic(
                        code="profile_probe_ready",
                        severity="ok",
                        summary=f"Caller profile '{profile.name}' connected and listed {result.tool_count} tool(s).",
                    )
                )

    for installation in installations.list():
        adapter = adapters[installation.client]
        if installation.profile not in known_profiles:
            diagnostics.append(
                Diagnostic(
                    code="installation_profile_missing",
                    severity="error",
                    summary=f"{installation.client} entry '{installation.server_name}' references a missing caller profile.",
                    recovery=(
                        f"Run: connection-hub client remove {installation.client} "
                        f"{installation.server_name}"
                    ),
                )
            )
            continue
        helper_path = Path(installation.command)
        helper_ready = helper_path.is_file() and os.access(helper_path, os.X_OK)
        if not helper_ready:
            diagnostics.append(
                Diagnostic(
                    code="helper_executable_missing",
                    severity="error",
                    summary=(
                        f"The helper executable for {installation.client} entry "
                        f"'{installation.server_name}' is unavailable."
                    ),
                    recovery=(
                        "Reinstall connection-hub-cli, remove this managed client "
                        "entry, and install it again."
                    ),
                )
            )
        if not adapter.available():
            diagnostics.append(
                Diagnostic(
                    code="client_not_installed",
                    severity="warning",
                    summary=f"The configured {installation.client} client is not currently available.",
                )
            )
            continue
        try:
            entry = adapter.inspect(installation.server_name)
        except ConnectionHubCliError as exc:
            diagnostics.append(
                Diagnostic(
                    code=exc.code,
                    severity="error",
                    summary=f"The {installation.client} configuration could not be inspected.",
                )
            )
            continue
        if not installation.owns_entry(entry):
            diagnostics.append(
                Diagnostic(
                    code="client_entry_changed",
                    severity="error",
                    summary=f"The {installation.client} entry '{installation.server_name}' no longer matches its managed definition.",
                )
            )
        elif helper_ready:
            diagnostics.append(
                Diagnostic(
                    code="client_entry_ready",
                    severity="ok",
                    summary=f"The {installation.client} entry '{installation.server_name}' is installed.",
                )
            )

    if not known_profiles:
        diagnostics.append(
            Diagnostic(
                code="no_profiles",
                severity="warning",
                summary="No delegated caller profiles are configured.",
                recovery="Run: connection-hub profile add <name> --endpoint <url>",
            )
        )
    return diagnostics

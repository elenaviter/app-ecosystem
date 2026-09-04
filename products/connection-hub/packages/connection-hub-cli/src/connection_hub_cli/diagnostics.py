from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from connection_hub_cli.authorization.session import (
    OAuthSessionRepository,
    OAuthSessionStore,
)
from connection_hub_cli.clients.adapters import ClientAdapter
from connection_hub_cli.credentials import CredentialStore
from connection_hub_cli.errors import ConnectionHubCliError
from connection_hub_cli.profiles import ProfileService
from connection_hub_cli.state import InstallationStore, ProfileStore

if TYPE_CHECKING:
    from connection_hub_cli.authorization.profile_session import (
        OAuthProfileSessionService,
    )


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
    oauth_sessions: OAuthSessionStore | None = None,
    oauth_repository: OAuthSessionRepository | None = None,
    oauth_profile_sessions: OAuthProfileSessionService | None = None,
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    platform_name = str(getattr(credentials, "platform_name", "unknown"))
    store_name = str(
        getattr(credentials, "store_name", "operating-system credential store")
    )
    recovery_hint = getattr(credentials, "recovery_hint", None)
    recovery = (
        recovery_hint()
        if callable(recovery_hint)
        else "Repair or unlock the operating-system credential store, then run connection-hub doctor again."
    )
    try:
        credentials.verify_ready()
    except ConnectionHubCliError as exc:
        diagnostics.append(
            Diagnostic(
                code=exc.code,
                severity="error",
                summary=(
                    "The configured credential store is not writable: "
                    f"{credentials.backend_name()} on {platform_name}."
                ),
                recovery=recovery,
            )
        )
    else:
        diagnostics.append(
            Diagnostic(
                code="credential_store_ready",
                severity="ok",
                summary=(
                    "Delegated caller credentials passed a temporary write, read, and "
                    f"delete check in {store_name} on {platform_name} "
                    f"({credentials.backend_name()})."
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
    elif platform_name == "Windows":
        diagnostics.append(
            Diagnostic(
                code="state_permissions_managed_by_windows",
                severity="ok",
                summary=(
                    "Local non-secret state uses the current Windows user profile "
                    "and its ACLs; delegated credentials remain in Windows "
                    "Credential Manager."
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

    if platform_name != "Windows":
        state_paths = [profiles.path, installations.path]
        if oauth_sessions is not None:
            state_paths.append(oauth_sessions.path)
        for path in state_paths:
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

    if oauth_sessions is not None and oauth_repository is not None:
        for session in oauth_sessions.list():
            try:
                present = oauth_repository.credential_present(session.session_id)
            except ConnectionHubCliError as exc:
                diagnostics.append(
                    Diagnostic(
                        code=exc.code,
                        severity="error",
                        summary=(
                            "The delegated management session for "
                            f"'{session.resource}' could not read its credential."
                        ),
                        recovery=(
                            "Revoke the matching caller card in Connection Hub, "
                            "then repair the local credential store."
                        ),
                    )
                )
                continue
            if not present:
                diagnostics.append(
                    Diagnostic(
                        code="oauth_session_credential_missing",
                        severity="error",
                        summary=(
                            "The delegated management session for "
                            f"'{session.resource}' has no native credential-store record."
                        ),
                        recovery=(
                            "Revoke the matching caller card in Connection Hub "
                            "before authorizing this CLI again."
                        ),
                    )
                )
                continue
            diagnostics.append(
                Diagnostic(
                    code="oauth_session_credential_ready",
                    severity="ok",
                    summary=(
                        "The delegated management session for "
                        f"'{session.resource}' has a native credential-store record."
                    ),
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
            if profile.auth_type == "oauth":
                profile_recovery = (
                    "Revoke the recorded access_id in Connection Hub, then remove or "
                    "authorize this OAuth profile again."
                )
            else:
                profile_recovery = (
                    f"Run: connection-hub profile credential replace {profile.name}"
                )
            diagnostics.append(
                Diagnostic(
                    code="credential_missing",
                    severity="error",
                    summary=(
                        f"Caller profile '{profile.name}' ({profile.auth_type}) has no "
                        "native credential-store record."
                    ),
                    recovery=profile_recovery,
                )
            )
            continue
        profile_detail = ""
        if profile.auth_type == "oauth" and oauth_profile_sessions is not None:
            try:
                status = oauth_profile_sessions.credential_status(profile)
            except ConnectionHubCliError as exc:
                diagnostics.append(
                    Diagnostic(
                        code=exc.code,
                        severity="error",
                        summary=(
                            f"Caller profile '{profile.name}' has an invalid OAuth "
                            "credential record."
                        ),
                        recovery=(
                            "Revoke the recorded access_id in Connection Hub before "
                            "removing local profile state."
                        ),
                    )
                )
                continue
            profile_detail = (
                f" Expiry: {status['expiry']}; refresh ready: "
                f"{str(status['refresh_ready']).lower()}."
            )
        diagnostics.append(
            Diagnostic(
                code="profile_credential_ready",
                severity="ok",
                summary=(
                    f"Caller profile '{profile.name}' ({profile.auth_type}) has a "
                    "native credential-store record for "
                    f"{profile.endpoint}; access_id: {profile.access_id or 'not recorded'}."
                    f"{profile_detail}"
                ),
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
        if installation.mode == "bridge" and installation.profile not in known_profiles:
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
        helper_ready = True
        if installation.mode == "bridge":
            helper_path = Path(str(installation.command))
            helper_ready = helper_path.is_file() and os.access(helper_path, os.X_OK)
        if installation.mode == "bridge" and not helper_ready:
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
            adapter.ensure_mode(installation.mode)
        except ConnectionHubCliError as exc:
            diagnostics.append(
                Diagnostic(
                    code=exc.code,
                    severity="error",
                    summary=(
                        f"The configured {installation.client} client no longer "
                        f"supports the managed {installation.mode} mode."
                    ),
                    recovery=(
                        f"Remove '{installation.server_name}', upgrade the client, "
                        "and install it again."
                    ),
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
                    summary=(
                        f"The {installation.client} entry "
                        f"'{installation.server_name}' is installed in "
                        f"{installation.mode} mode."
                    ),
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

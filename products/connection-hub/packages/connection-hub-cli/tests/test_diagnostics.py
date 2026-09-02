from __future__ import annotations

import json

import pytest

from connection_hub_cli.diagnostics import collect_diagnostics
from connection_hub_cli.errors import CredentialError, UpstreamError
from connection_hub_cli.models import (
    CallerProfile,
    HelperLaunch,
    ManagedInstallation,
    ProbeResult,
)
from connection_hub_cli.profiles import ProfileService
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


class _Adapter:
    client = "claude-desktop"

    def __init__(self, entry=None) -> None:
        self.entry = entry

    def available(self) -> bool:
        return True

    def inspect(self, _server_name: str):
        return self.entry

    def install(self, _installation) -> bool:
        raise AssertionError("diagnostics must not modify client configuration")

    def remove(self, _installation) -> bool:
        raise AssertionError("diagnostics must not modify client configuration")


def _services(tmp_path, probe):
    profiles = ProfileStore(tmp_path / "state" / "profiles.json")
    installations = InstallationStore(tmp_path / "state" / "installations.json")
    credentials = _Credentials()
    service = ProfileService(
        profiles=profiles,
        installations=installations,
        credentials=credentials,
        probe=probe,
    )
    return profiles, installations, credentials, service


@pytest.mark.asyncio
async def test_diagnostics_report_missing_credential_permissions_and_changed_client_entry(
    tmp_path,
) -> None:
    async def probe(**_kwargs) -> ProbeResult:
        return ProbeResult(tool_count=1)

    profiles, installations, credentials, service = _services(tmp_path, probe)
    profile = CallerProfile.create(name="agent", endpoint="https://hub.example/mcp")
    profiles.add(profile)
    installation = ManagedInstallation.create(
        client="claude-desktop",
        profile=profile.name,
        server_name="connection-hub-agent",
        launch=HelperLaunch(command="/opt/tools/connection-hub"),
        installation_id="a" * 32,
    )
    installations.add(installation)
    profiles.path.parent.chmod(0o755)
    profiles.path.chmod(0o644)

    diagnostics = await collect_diagnostics(
        profiles=profiles,
        installations=installations,
        credentials=credentials,
        profile_service=service,
        adapters={"claude-desktop": _Adapter(entry={"command": "/bin/foreign"})},
        probe=True,
    )

    codes = {item.code for item in diagnostics}
    assert "state_directory_permissions" in codes
    assert "state_file_permissions" in codes
    assert "credential_missing" in codes
    assert "client_entry_changed" in codes


@pytest.mark.asyncio
async def test_probe_diagnostics_do_not_render_a_bearer_or_upstream_exception(
    tmp_path,
) -> None:
    bearer = "synthetic-bearer-never-render"

    async def probe(**_kwargs) -> ProbeResult:
        raise UpstreamError(
            "mcp_connection_failed",
            f"upstream included {bearer}",
        )

    profiles, installations, credentials, service = _services(tmp_path, probe)
    profile = CallerProfile.create(name="agent", endpoint="https://hub.example/mcp")
    profiles.add(profile)
    credentials.put(profile.credential_ref, bearer)

    diagnostics = await collect_diagnostics(
        profiles=profiles,
        installations=installations,
        credentials=credentials,
        profile_service=service,
        adapters={},
        probe=True,
    )

    rendered = json.dumps([item.to_dict() for item in diagnostics])
    assert bearer not in rendered
    assert "profile_probe_ready" not in {item.code for item in diagnostics}
    assert "mcp_connection_failed" in {item.code for item in diagnostics}


@pytest.mark.asyncio
async def test_diagnostics_fail_when_keyring_backend_exists_but_cannot_write(
    tmp_path,
) -> None:
    async def probe(**_kwargs) -> ProbeResult:
        return ProbeResult(tool_count=1)

    profiles, installations, credentials, service = _services(tmp_path, probe)

    def fail_verification() -> None:
        raise CredentialError(
            "credential_store_write_failed",
            "The delegated caller credential could not be stored in macOS Keychain.",
        )

    credentials.verify_ready = fail_verification
    diagnostics = await collect_diagnostics(
        profiles=profiles,
        installations=installations,
        credentials=credentials,
        profile_service=service,
        adapters={},
        probe=False,
    )

    by_code = {item.code: item for item in diagnostics}
    assert by_code["credential_store_write_failed"].severity == "error"
    assert (
        "logged-in macOS user session"
        in by_code["credential_store_write_failed"].recovery
    )
    assert "credential_store_ready" not in by_code
    assert "state_directory_not_created" in by_code


@pytest.mark.asyncio
async def test_diagnostics_reject_a_managed_entry_with_a_missing_helper(
    tmp_path,
) -> None:
    async def probe(**_kwargs) -> ProbeResult:
        return ProbeResult(tool_count=1)

    profiles, installations, credentials, service = _services(tmp_path, probe)
    profile = CallerProfile.create(name="agent", endpoint="https://hub.example/mcp")
    profiles.add(profile)
    credentials.put(profile.credential_ref, "synthetic-bearer")
    installation = ManagedInstallation.create(
        client="claude-desktop",
        profile=profile.name,
        server_name="connection-hub-agent",
        launch=HelperLaunch(command=str(tmp_path / "removed-connection-hub")),
        installation_id="a" * 32,
    )
    installations.add(installation)

    diagnostics = await collect_diagnostics(
        profiles=profiles,
        installations=installations,
        credentials=credentials,
        profile_service=service,
        adapters={"claude-desktop": _Adapter(entry=installation.to_entry())},
        probe=False,
    )

    codes = {item.code for item in diagnostics}
    assert "helper_executable_missing" in codes
    assert "client_entry_ready" not in codes

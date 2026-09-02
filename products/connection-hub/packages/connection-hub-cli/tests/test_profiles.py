from __future__ import annotations

import pytest

from connection_hub_cli.errors import ProfileError, UpstreamError
from connection_hub_cli.models import HelperLaunch, ManagedInstallation, ProbeResult
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


def _service(tmp_path, probe):
    profiles = ProfileStore(tmp_path / "profiles.json")
    installations = InstallationStore(tmp_path / "installations.json")
    credentials = _Credentials()
    return (
        ProfileService(
            profiles=profiles,
            installations=installations,
            credentials=credentials,
            probe=probe,
        ),
        profiles,
        installations,
        credentials,
    )


@pytest.mark.asyncio
async def test_add_validates_before_storing_and_keeps_bearer_out_of_state(
    tmp_path,
) -> None:
    calls: list[tuple[str, str]] = []

    async def probe(*, endpoint: str, bearer: str) -> ProbeResult:
        calls.append((endpoint, bearer))
        return ProbeResult(tool_count=2, server_name="fixture", server_version="1")

    service, profiles, _, credentials = _service(tmp_path, probe)
    profile, result = await service.add(
        name="agent",
        endpoint="https://hub.example/mcp",
        bearer="delegated-test-bearer",
        access_id="access_1",
    )

    assert calls == [("https://hub.example/mcp", "delegated-test-bearer")]
    assert result.tool_count == 2
    assert credentials.values[profile.credential_ref] == "delegated-test-bearer"
    assert "delegated-test-bearer" not in profiles.path.read_text()


@pytest.mark.asyncio
async def test_failed_add_probe_leaves_no_profile_or_keychain_record(tmp_path) -> None:
    async def probe(**_kwargs) -> ProbeResult:
        raise UpstreamError("mcp_connection_failed", "Connection failed.")

    service, profiles, _, credentials = _service(tmp_path, probe)
    with pytest.raises(UpstreamError):
        await service.add(
            name="agent",
            endpoint="https://hub.example/mcp",
            bearer="candidate-never-stored",
        )

    assert profiles.list() == []
    assert credentials.values == {}


@pytest.mark.asyncio
async def test_failed_replacement_preserves_working_credential(tmp_path) -> None:
    fail = False

    async def probe(*, endpoint: str, bearer: str) -> ProbeResult:
        if fail:
            raise UpstreamError("mcp_connection_failed", "Connection failed.")
        return ProbeResult(tool_count=1)

    service, profiles, _, credentials = _service(tmp_path, probe)
    profile, _ = await service.add(
        name="agent",
        endpoint="https://hub.example/mcp",
        bearer="working-bearer",
    )
    fail = True

    with pytest.raises(UpstreamError):
        await service.replace_credential(name="agent", bearer="invalid-candidate")

    assert credentials.get(profile.credential_ref) == "working-bearer"
    assert profiles.require("agent").updated_at == profile.updated_at


@pytest.mark.asyncio
async def test_replacement_rolls_back_keychain_when_metadata_write_fails(
    tmp_path,
    monkeypatch,
) -> None:
    async def probe(**_kwargs) -> ProbeResult:
        return ProbeResult(tool_count=1)

    service, profiles, _, credentials = _service(tmp_path, probe)
    profile, _ = await service.add(
        name="agent",
        endpoint="https://hub.example/mcp",
        bearer="working-bearer",
    )

    def fail_update(_profile) -> None:
        raise RuntimeError("state write failed")

    monkeypatch.setattr(profiles, "update", fail_update)
    with pytest.raises(RuntimeError):
        await service.replace_credential(name="agent", bearer="candidate-bearer")

    assert credentials.get(profile.credential_ref) == "working-bearer"


@pytest.mark.asyncio
async def test_removal_rolls_back_keychain_when_metadata_write_fails(
    tmp_path,
    monkeypatch,
) -> None:
    async def probe(**_kwargs) -> ProbeResult:
        return ProbeResult(tool_count=1)

    service, profiles, _, credentials = _service(tmp_path, probe)
    profile, _ = await service.add(
        name="agent",
        endpoint="https://hub.example/mcp",
        bearer="working-bearer",
    )

    def fail_remove(_name):
        raise RuntimeError("state write failed")

    monkeypatch.setattr(profiles, "remove", fail_remove)
    with pytest.raises(RuntimeError):
        service.remove("agent")

    assert credentials.get(profile.credential_ref) == "working-bearer"


@pytest.mark.asyncio
async def test_profile_removal_refuses_live_client_entries_without_force(
    tmp_path,
) -> None:
    async def probe(**_kwargs) -> ProbeResult:
        return ProbeResult(tool_count=1)

    service, _, installations, _ = _service(tmp_path, probe)
    await service.add(
        name="agent",
        endpoint="https://hub.example/mcp",
        bearer="working-bearer",
    )
    installations.add(
        ManagedInstallation.create(
            client="claude-code",
            profile="agent",
            server_name="connection-hub-agent",
            launch=HelperLaunch(command="/bin/connection-hub"),
            installation_id="a" * 32,
        )
    )

    with pytest.raises(ProfileError) as raised:
        service.remove("agent")
    assert raised.value.code == "profile_in_use"

    removed = service.remove("agent", force=True)
    assert removed.dangling_installations == 1

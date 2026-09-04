from __future__ import annotations

import json
import os
import stat
from types import SimpleNamespace

import pytest
from connection_hub_cli.credentials import (
    MacOSKeychainCredentialStore,
    normalize_bearer,
)
from connection_hub_cli.errors import CredentialError, ProfileError, StateError
from connection_hub_cli.models import CallerProfile, ManagedInstallation
from connection_hub_cli.state import ProfileStore
from keyring.errors import PasswordDeleteError


class _Keyring:
    priority = 5

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.fail_write = False
        self.fail_read = False

    def set_password(self, service: str, username: str, password: str) -> None:
        if self.fail_write:
            raise RuntimeError(f"do not expose {password}")
        self.values[(service, username)] = password

    def get_password(self, service: str, username: str) -> str | None:
        if self.fail_read:
            raise RuntimeError("synthetic read failure")
        return self.values.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        try:
            del self.values[(service, username)]
        except KeyError as exc:
            raise PasswordDeleteError("not found") from exc


def _credential_store(backend: _Keyring | None = None) -> MacOSKeychainCredentialStore:
    return MacOSKeychainCredentialStore(
        backend=backend or _Keyring(),
        platform_name="Darwin",
        enforce_native_backend=False,
    )


def test_profile_accepts_https_and_loopback_http() -> None:
    assert CallerProfile.create(
        name="agent-a", endpoint="https://hub.example/mcp"
    ).endpoint == ("https://hub.example/mcp")
    assert CallerProfile.create(
        name="agent-b", endpoint="http://127.0.0.1:8010/mcp"
    ).endpoint == ("http://127.0.0.1:8010/mcp")
    assert CallerProfile.create(
        name="agent-c", endpoint="http://[::1]:8010/mcp"
    ).endpoint == ("http://[::1]:8010/mcp")


@pytest.mark.parametrize(
    "endpoint,code",
    [
        ("http://service.example/mcp", "insecure_endpoint"),
        ("https://user:password@service.example/mcp", "endpoint_contains_credentials"),
        ("https://service.example/mcp?token=value", "endpoint_contains_query"),
        ("file:///tmp/mcp", "invalid_endpoint"),
    ],
)
def test_profile_rejects_unsafe_endpoint_shapes(endpoint: str, code: str) -> None:
    with pytest.raises(ProfileError) as raised:
        CallerProfile.create(name="agent", endpoint=endpoint)
    assert raised.value.code == code


def test_profile_store_is_atomic_private_and_contains_no_credential(tmp_path) -> None:
    path = tmp_path / "state" / "profiles.json"
    profile = CallerProfile.create(
        name="agent",
        endpoint="https://hub.example/mcp",
        access_id="access_123",
        now="2026-09-02T15:00:00+00:00",
    )
    store = ProfileStore(path)
    store.add(profile)

    assert store.require("agent") == profile
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    raw = path.read_text()
    assert "delegated-test-bearer" not in raw
    assert json.loads(raw)["schema"] == ProfileStore.SCHEMA
    assert not list(path.parent.glob("*.tmp"))


def test_state_write_uses_path_chmod_when_fchmod_is_unavailable(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "connection_hub_cli.filesystem.os",
        SimpleNamespace(chmod=os.chmod),
    )
    store = ProfileStore(tmp_path / "profiles.json")
    profile = CallerProfile.create(name="agent", endpoint="https://hub.example/mcp")

    store.add(profile)

    assert store.require("agent") == profile


def test_legacy_client_installation_migrates_to_explicit_bridge_mode() -> None:
    legacy = {
        "installation_id": "a" * 32,
        "client": "claude-code",
        "profile": "agent",
        "server_name": "connection-hub-agent",
        "command": "/opt/tools/connection-hub",
        "args": ["mcp", "serve", "--profile", "agent"],
        "created_at": "2026-09-02T15:00:00+00:00",
    }

    installation = ManagedInstallation.from_dict(legacy)

    assert installation.mode == "bridge"
    assert installation.endpoint is None
    assert installation.to_dict()["record_version"] == 2


def test_native_oauth_client_installation_round_trips_without_a_credential() -> None:
    installation = ManagedInstallation.create_oauth(
        client="codex",
        endpoint="https://hub.example/mcp",
        server_name="connection-hub-codex",
        installation_id="b" * 32,
    )

    loaded = ManagedInstallation.from_dict(installation.to_dict())

    assert loaded == installation
    assert loaded.profile is None
    assert "credential" not in json.dumps(loaded.to_dict()).lower()


def test_reading_empty_state_does_not_create_local_state(tmp_path) -> None:
    path = tmp_path / "state" / "profiles.json"

    assert ProfileStore(path).list() == []
    assert not path.parent.exists()


def test_profile_store_rejects_a_symlink_state_file(tmp_path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}")
    link = tmp_path / "profiles.json"
    link.symlink_to(target)

    with pytest.raises(StateError) as raised:
        ProfileStore(link).list()
    assert raised.value.code == "state_symlink_rejected"


def test_keychain_store_normalizes_and_removes_a_bearer() -> None:
    backend = _Keyring()
    store = _credential_store(backend)

    store.put("a" * 32, "  Bearer delegated-test-bearer  ")
    assert store.get("a" * 32) == "delegated-test-bearer"
    assert store.remove("a" * 32) is True
    assert store.remove("a" * 32) is False


def test_keychain_readiness_check_leaves_no_temporary_record() -> None:
    backend = _Keyring()
    store = _credential_store(backend)

    store.verify_ready()

    assert backend.values == {}


def test_keychain_readiness_cleans_up_when_the_temporary_read_fails() -> None:
    backend = _Keyring()
    backend.fail_read = True
    store = _credential_store(backend)

    with pytest.raises(CredentialError) as raised:
        store.verify_ready()

    assert raised.value.code == "credential_store_read_failed"
    assert backend.values == {}


def test_keychain_failure_text_never_contains_the_candidate() -> None:
    backend = _Keyring()
    backend.fail_write = True
    store = _credential_store(backend)
    candidate = "delegated-test-bearer-never-render"

    with pytest.raises(CredentialError) as raised:
        store.put("a" * 32, candidate)
    assert candidate not in str(raised.value)
    assert candidate not in raised.value.message


def test_keychain_fails_closed_outside_verified_platform() -> None:
    with pytest.raises(CredentialError) as raised:
        MacOSKeychainCredentialStore(
            backend=_Keyring(),
            platform_name="Linux",
            enforce_native_backend=False,
        )
    assert raised.value.code == "unsupported_credential_store"


@pytest.mark.parametrize("value", ["", "token with spaces", "token\nnext", "Bearer "])
def test_invalid_bearers_are_rejected(value: str) -> None:
    with pytest.raises(CredentialError):
        normalize_bearer(value)

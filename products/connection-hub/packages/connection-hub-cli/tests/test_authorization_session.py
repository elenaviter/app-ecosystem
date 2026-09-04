from __future__ import annotations

import asyncio
import json
import stat

import pytest
from connection_hub_cli.authorization.models import OAuthTokenSet
from connection_hub_cli.authorization.session import (
    OAUTH_PROFILE_KEYRING_SERVICE,
    OAUTH_SESSION_KEYRING_SERVICE,
    MacOSOAuthSessionCredentialStore,
    NativeOAuthProfileCredentialStore,
    NativeOAuthSessionCredentialStore,
    OAuthSessionRecord,
    OAuthSessionRepository,
    OAuthSessionStore,
    session_id_for_target,
)
from connection_hub_cli.credentials import KEYRING_SERVICE, NativeCredentialStore
from connection_hub_cli.errors import AuthorizationError
from connection_hub_cli.paths import StatePaths
from keyring.errors import PasswordDeleteError


class _Keyring:
    priority = 5

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.fail_write = False

    def set_password(self, service: str, username: str, password: str) -> None:
        if self.fail_write:
            raise RuntimeError(f"failed to store {password}")
        self.values[(service, username)] = password

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        try:
            del self.values[(service, username)]
        except KeyError as exc:
            raise PasswordDeleteError("not found") from exc


def _credential_store(
    backend: _Keyring | None = None,
) -> MacOSOAuthSessionCredentialStore:
    return MacOSOAuthSessionCredentialStore(
        backend=backend or _Keyring(),
        platform_name="Darwin",
        enforce_native_backend=False,
    )


def _token(
    access: str = "access-secret-marker",
    refresh: str = "refresh-secret-marker",
    *,
    access_id: str | None = "access_cli",
    expires_at: int = 1_800_000_000,
) -> OAuthTokenSet:
    return OAuthTokenSet(
        access_token=access,
        refresh_token=refresh,
        expires_at=expires_at,
        scope="management.read management.reload",
        access_id=access_id,
    )


def _record(token: OAuthTokenSet | None = None) -> OAuthSessionRecord:
    return OAuthSessionRecord.create(
        target_key=("endpoint:https://runtime.example.test:demo-tenant:demo-project"),
        resource_metadata_url=(
            "https://runtime.example.test/.well-known/oauth-protected-resource"
            "?resource=https%3A%2F%2Fruntime.example.test%2Fmanagement"
        ),
        resource="urn:kdcube:management:deployment:demo-tenant:demo-project",
        issuer="https://runtime.example.test/oauth",
        token_endpoint="https://runtime.example.test/oauth/token",
        revocation_endpoint="https://runtime.example.test/oauth/revoke",
        client_id="native-client",
        scope="management.read",
        token=token or _token(),
        now="2026-09-03T10:00:00+00:00",
    )


def test_session_record_is_stable_and_contains_only_public_metadata() -> None:
    token = _token()
    record = _record(token)
    payload = record.to_dict()

    assert record.session_id == session_id_for_target(record.target_key)
    assert payload["access_id"] == "access_cli"
    assert payload["scope"] == token.scope
    assert "access_token" not in payload
    assert "refresh_token" not in payload
    assert "access-secret-marker" not in repr(payload)
    assert OAuthSessionRecord.from_dict(payload) == record


def test_session_record_requires_a_card_bound_access_id() -> None:
    with pytest.raises(AuthorizationError) as raised:
        _record(_token(access_id=None))

    assert raised.value.code == "oauth_access_id_missing"


def test_session_store_is_atomic_private_and_has_no_tokens(tmp_path) -> None:
    path = StatePaths(tmp_path / "state").oauth_sessions
    record = _record()
    store = OAuthSessionStore(path)

    store.add(record)

    assert store.require(record.session_id) == record
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    raw = path.read_text()
    assert "access-secret-marker" not in raw
    assert "refresh-secret-marker" not in raw
    assert json.loads(raw)["schema"] == OAuthSessionStore.SCHEMA


def test_oauth_tokens_round_trip_as_one_keychain_item_without_repr_leak() -> None:
    backend = _Keyring()
    credentials = _credential_store(backend)
    record = _record()
    token = _token()

    credentials.put(record.credential_ref, token)
    loaded = credentials.get(record.credential_ref)

    assert loaded == token
    assert loaded is not None
    assert token.access_token not in repr(loaded)
    assert token.refresh_token not in repr(loaded)
    assert len(backend.values) == 1


def test_logical_credentials_use_separate_native_service_names() -> None:
    backend = _Keyring()
    static = NativeCredentialStore(
        backend=backend,
        platform_name="Darwin",
        enforce_native_backend=False,
    )
    profile = NativeOAuthProfileCredentialStore(
        backend=backend,
        platform_name="Darwin",
        enforce_native_backend=False,
    )
    management = NativeOAuthSessionCredentialStore(
        backend=backend,
        platform_name="Darwin",
        enforce_native_backend=False,
    )

    static.put("1" * 32, "static-bearer")
    profile.put("2" * 32, _token())
    management.put("3" * 32, _token())

    assert {service for service, _account in backend.values} == {
        KEYRING_SERVICE,
        OAUTH_PROFILE_KEYRING_SERVICE,
        OAUTH_SESSION_KEYRING_SERVICE,
    }


def test_windows_store_round_trips_the_maximum_accepted_oauth_record() -> None:
    backend = _Keyring()
    credentials = NativeOAuthSessionCredentialStore(
        backend=backend,
        platform_name="Windows",
        enforce_native_backend=False,
    )
    token = OAuthTokenSet(
        access_token='"' * 65536,
        refresh_token="\\" * 65536,
        expires_at=1_800_000_000,
        scope="s" * 8192,
        access_id="access_cli",
    )

    credentials.put("4" * 32, token)

    assert credentials.get("4" * 32) == token
    assert len(backend.values) > 1
    assert credentials.remove("4" * 32) is True
    assert backend.values == {}


def test_keychain_failure_does_not_render_the_token() -> None:
    backend = _Keyring()
    backend.fail_write = True
    credentials = _credential_store(backend)
    token = _token()

    with pytest.raises(AuthorizationError) as raised:
        credentials.put("a" * 32, token)
    assert token.access_token not in str(raised.value)
    assert token.refresh_token not in str(raised.value)
    assert raised.value.__cause__ is None


def test_repository_creates_replaces_and_removes_one_session(tmp_path) -> None:
    backend = _Keyring()
    credentials = _credential_store(backend)
    sessions = OAuthSessionStore(tmp_path / "oauth-sessions.json")
    repository = OAuthSessionRepository(
        sessions=sessions,
        credentials=credentials,
    )
    original = _record()
    repository.create(original, _token())

    stored_record, stored_token = repository.load(original.session_id)
    assert stored_record == original
    assert stored_token.access_token == "access-secret-marker"

    replacement = _token(
        "replacement-access",
        "replacement-refresh",
        access_id="access_cli",
    )
    updated = repository.replace_token(original.session_id, replacement)
    assert updated.updated_at != ""
    assert repository.load(original.session_id)[1] == replacement

    removed = repository.remove(original.session_id)
    assert removed.session_id == original.session_id
    assert sessions.get(original.session_id) is None
    assert backend.values == {}


def test_repository_credential_store_probe_round_trips_and_cleans_up(tmp_path) -> None:
    backend = _Keyring()
    repository = OAuthSessionRepository(
        sessions=OAuthSessionStore(tmp_path / "oauth-sessions.json"),
        credentials=_credential_store(backend),
    )

    repository.verify_credential_store()

    assert backend.values == {}


def test_repository_cleans_keychain_if_metadata_create_fails(tmp_path) -> None:
    class _FailingAddStore(OAuthSessionStore):
        def add(self, record) -> None:
            raise AuthorizationError(
                "oauth_session_metadata_write_failed",
                "The session metadata could not be stored.",
            )

    backend = _Keyring()
    credentials = _credential_store(backend)
    sessions = _FailingAddStore(tmp_path / "oauth-sessions.json")
    repository = OAuthSessionRepository(
        sessions=sessions,
        credentials=credentials,
    )
    record = _record()

    with pytest.raises(AuthorizationError) as raised:
        repository.create(record, _token())
    assert raised.value.code == "oauth_session_metadata_write_failed"
    assert backend.values == {}


def test_repository_rejects_a_token_for_another_card_before_keychain_write(
    tmp_path,
) -> None:
    backend = _Keyring()
    repository = OAuthSessionRepository(
        sessions=OAuthSessionStore(tmp_path / "oauth-sessions.json"),
        credentials=_credential_store(backend),
    )
    record = _record(_token(access_id="access-1"))

    with pytest.raises(AuthorizationError) as raised:
        repository.create(record, _token(access_id="access-2"))

    assert raised.value.code == "oauth_session_access_id_mismatch"
    assert backend.values == {}


def test_repository_restores_previous_token_if_metadata_update_fails(tmp_path) -> None:
    class _FailingUpdateStore(OAuthSessionStore):
        fail_update = False

        def update(self, record) -> None:
            if self.fail_update:
                raise AuthorizationError(
                    "oauth_session_metadata_write_failed",
                    "The session metadata could not be updated.",
                )
            super().update(record)

    backend = _Keyring()
    sessions = _FailingUpdateStore(tmp_path / "oauth-sessions.json")
    repository = OAuthSessionRepository(
        sessions=sessions,
        credentials=_credential_store(backend),
    )
    record = _record()
    original = _token()
    replacement = _token("replacement-access", "replacement-refresh")
    repository.create(record, original)
    sessions.fail_update = True

    with pytest.raises(AuthorizationError) as raised:
        repository.replace_token(record.session_id, replacement)

    assert raised.value.code == "oauth_session_metadata_write_failed"
    assert repository.load(record.session_id)[1] == original


@pytest.mark.asyncio
async def test_concurrent_refresh_rotates_one_time_under_the_session_lock(
    tmp_path,
) -> None:
    repository = OAuthSessionRepository(
        sessions=OAuthSessionStore(tmp_path / "oauth-sessions.json"),
        credentials=_credential_store(),
    )
    expiring = _token(expires_at=120)
    record = _record(expiring)
    repository.create(record, expiring)
    refresh_calls = 0

    async def refresh(_record, _token_value):
        nonlocal refresh_calls
        refresh_calls += 1
        await asyncio.sleep(0.02)
        return _token(
            "replacement-access",
            "replacement-refresh",
            expires_at=1_000,
        )

    first, second = await asyncio.gather(
        repository.refresh_if_expiring(
            record.session_id,
            refresher=refresh,
            now=100,
        ),
        repository.refresh_if_expiring(
            record.session_id,
            refresher=refresh,
            now=100,
        ),
    )

    assert refresh_calls == 1
    assert first[1].access_token == "replacement-access"
    assert second[1].access_token == "replacement-access"


@pytest.mark.asyncio
async def test_refresh_preserves_access_id_when_extension_is_omitted(tmp_path) -> None:
    repository = OAuthSessionRepository(
        sessions=OAuthSessionStore(tmp_path / "oauth-sessions.json"),
        credentials=_credential_store(),
    )
    expiring = _token(expires_at=120, access_id="access-1")
    record = _record(expiring)
    repository.create(record, expiring)

    async def refresh(_record, _token_value):
        return _token(
            "replacement-access",
            "replacement-refresh",
            expires_at=1_000,
            access_id=None,
        )

    updated, replacement = await repository.refresh_if_expiring(
        record.session_id,
        refresher=refresh,
        now=100,
    )

    assert updated.access_id == "access-1"
    assert replacement.access_id == "access-1"
    assert repository.load(record.session_id)[1].access_id == "access-1"


@pytest.mark.asyncio
async def test_refresh_rejects_a_credential_for_another_card(tmp_path) -> None:
    repository = OAuthSessionRepository(
        sessions=OAuthSessionStore(tmp_path / "oauth-sessions.json"),
        credentials=_credential_store(),
    )
    original = _token(expires_at=120, access_id="access-1")
    record = _record(original)
    repository.create(record, original)

    async def refresh(_record, _token_value):
        return _token(
            "foreign-access",
            "foreign-refresh",
            expires_at=1_000,
            access_id="access-2",
        )

    with pytest.raises(AuthorizationError) as raised:
        await repository.refresh_if_expiring(
            record.session_id,
            refresher=refresh,
            now=100,
        )

    assert raised.value.code == "oauth_session_access_id_mismatch"
    assert repository.load(record.session_id)[1] == original


@pytest.mark.asyncio
async def test_authorization_slot_rejects_an_existing_target_before_oauth(
    tmp_path,
) -> None:
    backend = _Keyring()
    repository = OAuthSessionRepository(
        sessions=OAuthSessionStore(tmp_path / "oauth-sessions.json"),
        credentials=_credential_store(backend),
    )
    record = _record()
    repository.create(record, _token())

    with pytest.raises(AuthorizationError) as raised:
        async with repository.authorization_slot(record.target_key):
            raise AssertionError("an existing session must block authorization")
    assert raised.value.code == "oauth_session_exists"


def test_corrupt_keychain_value_fails_without_exposing_it() -> None:
    backend = _Keyring()
    credentials = _credential_store(backend)
    marker = "corrupt-secret-marker"
    backend.values[
        (
            "tech.kdcube.connection-hub.oauth-session",
            "a" * 32,
        )
    ] = marker

    with pytest.raises(AuthorizationError) as raised:
        credentials.get("a" * 32)
    assert raised.value.code == "oauth_session_credential_invalid"
    assert marker not in str(raised.value)
    assert raised.value.__cause__ is None

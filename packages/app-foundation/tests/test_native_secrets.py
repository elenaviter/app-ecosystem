from __future__ import annotations

import pytest
from app_foundation.secrets import (
    MAX_NATIVE_SECRET_VALUE_BYTES,
    NativeSecretError,
    NativeSecretValueStore,
)
from keyring.errors import PasswordDeleteError


class _MemoryBackend:
    priority = 5

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.set_calls = 0
        self.fail_set_call: int | None = None
        self.fail_reads = False
        self.fail_delete_accounts: set[str] = set()

    def set_password(self, service: str, username: str, password: str) -> None:
        self.set_calls += 1
        if self.fail_set_call == self.set_calls:
            raise RuntimeError(f"candidate must not leak: {password}")
        self.values[(service, username)] = password

    def get_password(self, service: str, username: str) -> str | None:
        if self.fail_reads:
            raise RuntimeError("synthetic read failure")
        return self.values.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        if username in self.fail_delete_accounts:
            raise RuntimeError("synthetic delete failure")
        try:
            del self.values[(service, username)]
        except KeyError as exc:
            raise PasswordDeleteError("not found") from exc


def _backend(platform_name: str) -> _MemoryBackend:
    modules = {
        "Darwin": ("keyring.backends.macOS", "Keyring"),
        "Windows": ("keyring.backends.Windows", "WinVaultKeyring"),
        "Linux": ("keyring.backends.SecretService", "Keyring"),
    }
    module, name = modules[platform_name]
    backend_type = type(name, (_MemoryBackend,), {"__module__": module})
    return backend_type()


def _store(
    platform_name: str,
    backend: _MemoryBackend | None = None,
    *,
    enforce_native_backend: bool = True,
) -> NativeSecretValueStore:
    return NativeSecretValueStore(
        service="example.native.secret",
        backend=backend or _backend(platform_name),
        platform_name=platform_name,
        enforce_native_backend=enforce_native_backend,
    )


@pytest.mark.parametrize(
    "platform_name,expected",
    [
        ("Darwin", "macOS Keychain"),
        ("Windows", "Windows Credential Manager"),
        ("Linux", "Linux Secret Service"),
    ],
)
def test_selects_only_the_reviewed_native_backend(
    platform_name: str,
    expected: str,
) -> None:
    store = _store(platform_name)

    assert store.platform_name == platform_name
    assert store.store_name == expected


@pytest.mark.parametrize("platform_name", ["Darwin", "Windows", "Linux"])
def test_rejects_a_wrong_backend_for_each_platform(platform_name: str) -> None:
    with pytest.raises(NativeSecretError) as raised:
        _store(platform_name, _MemoryBackend())

    assert raised.value.code == "insecure_native_secret_backend"


@pytest.mark.parametrize(
    "module,name",
    [
        ("keyring.backends.null", "Keyring"),
        ("keyring.backends.fail", "Keyring"),
        ("keyring.backends.chainer", "ChainerBackend"),
        ("keyrings.alt.file", "PlaintextKeyring"),
        ("keyrings.alt.file", "EncryptedKeyring"),
    ],
)
def test_rejects_known_non_native_backends(module: str, name: str) -> None:
    backend_type = type(name, (_MemoryBackend,), {"__module__": module})

    with pytest.raises(NativeSecretError) as raised:
        _store("Linux", backend_type())

    assert raised.value.code == "insecure_native_secret_backend"


def test_rejects_unsupported_and_unavailable_backends() -> None:
    with pytest.raises(NativeSecretError) as unsupported:
        _store("Plan9", _MemoryBackend())
    assert unsupported.value.code == "unsupported_native_secret_platform"

    backend = _backend("Linux")
    backend.priority = 0
    with pytest.raises(NativeSecretError) as unavailable:
        _store("Linux", backend)
    assert unavailable.value.code == "unavailable_native_secret_backend"
    assert "D-Bus" in unavailable.value.message


def test_backend_selection_failure_is_secret_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "backend-selection-canary"

    def fail_selection() -> _MemoryBackend:
        raise RuntimeError(canary)

    monkeypatch.setattr(
        "app_foundation.secrets.native.keyring.get_keyring",
        fail_selection,
    )

    with pytest.raises(NativeSecretError) as raised:
        NativeSecretValueStore(
            service="example.native.secret",
            platform_name="Linux",
        )

    assert raised.value.code == "unavailable_native_secret_backend"
    assert canary not in str(raised.value)
    assert raised.value.__cause__ is None


@pytest.mark.parametrize("platform_name", ["Darwin", "Linux"])
def test_direct_native_values_round_trip(platform_name: str) -> None:
    backend = _backend(platform_name)
    store = _store(platform_name, backend)

    store.replace("account", "secret-value")

    assert store.get("account") == "secret-value"
    assert store.remove("account") is True
    assert store.remove("account") is False
    assert backend.values == {}


@pytest.mark.parametrize(
    "size",
    [1279, 1280, 1281, 16 * 1024, 64 * 1024, MAX_NATIVE_SECRET_VALUE_BYTES],
)
def test_windows_values_across_the_native_item_boundary_round_trip(size: int) -> None:
    backend = _backend("Windows")
    store = _store("Windows", backend)
    value = "x" * size

    store.replace("account", value)

    assert store.get("account") == value
    assert store.remove("account") is True
    assert backend.values == {}


def test_windows_reads_and_replaces_a_legacy_direct_value() -> None:
    backend = _backend("Windows")
    backend.values[("example.native.secret", "account")] = "legacy-value"
    store = _store("Windows", backend)

    assert store.get("account") == "legacy-value"
    store.replace("account", "replacement-value")

    assert store.get("account") == "replacement-value"
    assert all("legacy-value" not in value for value in backend.values.values())


def test_interrupted_windows_candidate_preserves_the_old_generation() -> None:
    backend = _backend("Windows")
    store = _store("Windows", backend)
    store.replace("account", "working-value")
    old_records = dict(backend.values)
    backend.fail_set_call = backend.set_calls + 2
    canary = "candidate-secret-never-rendered-" * 100

    with pytest.raises(NativeSecretError) as raised:
        store.replace("account", canary)

    assert raised.value.code == "native_secret_write_failed"
    assert canary not in str(raised.value)
    assert store.get("account") == "working-value"
    assert backend.values == old_records


def test_failed_windows_manifest_switch_preserves_the_old_generation() -> None:
    backend = _backend("Windows")
    store = _store("Windows", backend)
    store.replace("account", "working-value")
    old_records = dict(backend.values)
    backend.fail_set_call = backend.set_calls + 2

    with pytest.raises(NativeSecretError) as raised:
        store.replace("account", "replacement-value")

    assert raised.value.code == "native_secret_write_failed"
    assert store.get("account") == "working-value"
    assert backend.values == old_records


def test_windows_rejects_values_above_the_supported_bound() -> None:
    store = _store("Windows")

    with pytest.raises(NativeSecretError) as raised:
        store.replace("account", "x" * (MAX_NATIVE_SECRET_VALUE_BYTES + 1))

    assert raised.value.code == "native_secret_too_large"


def test_windows_long_logical_account_uses_bounded_chunk_keys() -> None:
    backend = _backend("Windows")
    store = _store("Windows", backend)
    account = "a" * 512

    store.replace(account, "secret" * 400)

    chunk_accounts = [
        item_account
        for service, item_account in backend.values
        if service == "example.native.secret" and item_account != account
    ]
    assert chunk_accounts
    assert max(map(len, chunk_accounts)) < 160
    assert store.get(account) == "secret" * 400


def test_windows_replacement_removes_the_previous_generation() -> None:
    backend = _backend("Windows")
    store = _store("Windows", backend)
    store.replace("account", "first" * 500)
    first_accounts = set(backend.values)

    store.replace("account", "second" * 500)

    assert store.get("account") == "second" * 500
    assert not (first_accounts - {("example.native.secret", "account")}).intersection(
        backend.values
    )


def test_windows_missing_or_corrupt_chunks_fail_closed() -> None:
    backend = _backend("Windows")
    store = _store("Windows", backend)
    store.replace("account", "bounded-secret" * 200)
    missing = next(
        key
        for key in backend.values
        if key[0] == "example.native.secret" and key[1] != "account"
    )
    del backend.values[missing]

    with pytest.raises(NativeSecretError) as raised:
        store.get("account")

    assert raised.value.code == "native_secret_corrupt"
    assert "bounded-secret" not in str(raised.value)


def test_windows_integrity_mismatch_fails_closed() -> None:
    backend = _backend("Windows")
    store = _store("Windows", backend)
    store.replace("account", "bounded-secret" * 200)
    chunk_key = next(
        key
        for key in backend.values
        if key[0] == "example.native.secret" and key[1] != "account"
    )
    backend.values[chunk_key] = "Y29ycnVwdGVk"

    with pytest.raises(NativeSecretError) as raised:
        store.get("account")

    assert raised.value.code == "native_secret_corrupt"
    assert "bounded-secret" not in str(raised.value)


def test_windows_partial_removal_is_reported_without_claiming_success() -> None:
    backend = _backend("Windows")
    store = _store("Windows", backend)
    store.replace("account", "secret" * 400)
    chunk_account = next(
        account
        for service, account in backend.values
        if service == "example.native.secret" and account != "account"
    )
    backend.fail_delete_accounts.add(chunk_account)

    with pytest.raises(NativeSecretError) as raised:
        store.remove("account")

    assert raised.value.code == "native_secret_cleanup_failed"
    assert ("example.native.secret", "account") in backend.values
    assert ("example.native.secret", chunk_account) in backend.values

    backend.fail_delete_accounts.clear()
    assert store.remove("account") is True
    assert backend.values == {}


def test_readiness_probe_cleans_up_and_never_exposes_backend_exception_text() -> None:
    backend = _backend("Darwin")
    store = _store("Darwin", backend)
    store.verify_ready()
    assert backend.values == {}

    backend.fail_set_call = backend.set_calls + 1
    with pytest.raises(NativeSecretError) as raised:
        store.verify_ready()
    assert raised.value.__cause__ is None
    assert "candidate must not leak" not in str(raised.value)
    assert backend.values == {}

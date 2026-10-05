"""W558: a locked or missing Secret Service store is refused at once, never prompted for.

Host mint, 2026-10-05: keyring's Secret Service backend unlocks a locked
collection before each call; over SSH that waits for a prompt nobody sees. The
store now reads the lock state first and refuses by name.
"""

from __future__ import annotations

import sys
import types

import pytest
from app_foundation.secrets import NativeSecretError, NativeSecretValueStore, secret_service_lock_state


class _ItemNotFound(Exception):
    pass


def _secret_service(monkeypatch, state: str):
    """A stand-in secretstorage whose default collection is in ``state``."""

    closed = []

    class Connection:
        def close(self):
            closed.append(True)

    class Collection:
        def __init__(self, connection, path=None):
            if state == "missing":
                raise _ItemNotFound()

        def is_locked(self):
            return state == "locked"

    module = types.ModuleType("secretstorage")
    module.dbus_init = lambda: Connection()
    collection = types.ModuleType("secretstorage.collection")
    collection.Collection = Collection
    exceptions = types.ModuleType("secretstorage.exceptions")
    exceptions.ItemNotFoundException = _ItemNotFound
    monkeypatch.setitem(sys.modules, "secretstorage", module)
    monkeypatch.setitem(sys.modules, "secretstorage.collection", collection)
    monkeypatch.setitem(sys.modules, "secretstorage.exceptions", exceptions)
    return closed


class _PromptingBackend:
    """keyring's Secret Service backend: every call would unlock (prompt) first."""

    priority = 5

    def __init__(self):
        self.calls = 0
        self.values = {}

    def get_preferred_collection(self):
        self.calls += 1

    def get_password(self, service, username):
        self.calls += 1
        return self.values.get((service, username))

    def set_password(self, service, username, password):
        self.calls += 1
        self.values[(service, username)] = password

    def delete_password(self, service, username):
        self.calls += 1
        self.values.pop((service, username), None)


def _store(backend):
    return NativeSecretValueStore(
        service="example.native.secret", backend=backend, platform_name="Linux", enforce_native_backend=False
    )


@pytest.mark.parametrize("state", ["unlocked", "locked", "missing"])
def test_the_lock_state_is_read_without_prompting_and_the_connection_closed(monkeypatch, state):
    closed = _secret_service(monkeypatch, state)
    backend = _PromptingBackend()

    assert secret_service_lock_state(backend) == state
    assert backend.calls == 0 and closed == [True]


@pytest.mark.parametrize(
    "state,code", [("locked", "native_secret_store_locked"), ("missing", "native_secret_store_missing")]
)
def test_a_locked_or_missing_store_is_refused_before_the_backend_is_asked(monkeypatch, state, code):
    _secret_service(monkeypatch, state)
    backend = _PromptingBackend()
    store = _store(backend)

    for operation in (lambda: store.get("acct"), lambda: store.replace("acct", "v"), lambda: store.remove("acct")):
        with pytest.raises(NativeSecretError) as refused:
            operation()
        assert refused.value.code == code
    assert backend.calls == 0


def test_an_unlocked_store_works_as_before(monkeypatch):
    _secret_service(monkeypatch, "unlocked")
    store = _store(_PromptingBackend())

    store.replace("acct", "value")
    assert store.get("acct") == "value"
    assert store.remove("acct") is True


def test_another_backend_keeps_the_former_path():
    class Plain:
        pass

    assert secret_service_lock_state(Plain()) == "unknown"

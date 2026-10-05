"""W558: a custody call that never returns does not hold the custody thread forever.

Host mint, 2026-10-05: the relay started while the Secret Service store was
locked; its first keychain read waited for an unlock prompt nobody could see and
held the one custody thread, so every later read of the process (the newly
approved agent's included) queued behind it until the relay was restarted.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from connection_hub.caller.authorization import profile_session
from connection_hub.caller.errors import AuthorizationError


@pytest.fixture
def short_deadline(monkeypatch):
    """Each test gets its own custody thread; the process's real one is put back after."""

    monkeypatch.setattr(profile_session, "CUSTODY_DEADLINE_SECONDS", 0.3)
    original = profile_session._CUSTODY_EXECUTOR
    profile_session._CUSTODY_EXECUTOR = ThreadPoolExecutor(max_workers=1)
    yield
    profile_session._CUSTODY_EXECUTOR.shutdown(wait=False)
    profile_session._CUSTODY_EXECUTOR = original


def test_a_stuck_call_is_abandoned_and_the_next_one_runs_on_a_fresh_thread(short_deadline):
    release = threading.Event()
    stuck_executor = profile_session._CUSTODY_EXECUTOR

    async def scenario():
        with pytest.raises(AuthorizationError) as stuck:
            await profile_session.OAuthProfileSessionService._in_custody(release.wait)
        assert stuck.value.code == "oauth_credential_custody_timeout"
        assert profile_session._CUSTODY_EXECUTOR is not stuck_executor
        # The newly approved agent's read: answered at once, not queued behind the stuck one.
        return await profile_session.OAuthProfileSessionService._in_custody(lambda: "token")

    try:
        assert asyncio.run(scenario()) == "token"
    finally:
        release.set()


def test_a_cancelled_caller_waits_no_longer_than_the_deadline(short_deadline):
    release = threading.Event()

    async def scenario():
        task = asyncio.create_task(profile_session.OAuthProfileSessionService._in_custody(release.wait))
        await asyncio.sleep(0.05)
        task.cancel()
        started = asyncio.get_running_loop().time()
        with pytest.raises(asyncio.CancelledError):
            await task
        return asyncio.get_running_loop().time() - started

    try:
        assert asyncio.run(scenario()) < 1.0
    finally:
        release.set()


def test_a_call_within_the_deadline_is_unchanged(short_deadline):
    executor = profile_session._CUSTODY_EXECUTOR

    async def scenario():
        return await profile_session.OAuthProfileSessionService._in_custody(lambda a, b: a + b, 2, 3)

    assert asyncio.run(scenario()) == 5
    assert profile_session._CUSTODY_EXECUTOR is executor


def test_a_locked_store_refuses_at_once_and_the_same_process_reads_after_the_unlock(short_deadline, monkeypatch):
    """The relay case: locked when it starts, unlocked later, no restart."""

    import sys
    import types

    from connection_hub.caller.credentials import NativeCredentialStore
    from connection_hub.caller.errors import CredentialError

    state = {"locked": True}

    class _ItemNotFound(Exception):
        pass

    class Collection:
        def __init__(self, connection, path=None):
            pass

        def is_locked(self):
            return state["locked"]

    secretstorage = types.ModuleType("secretstorage")
    secretstorage.dbus_init = lambda: types.SimpleNamespace(close=lambda: None)
    collection = types.ModuleType("secretstorage.collection")
    collection.Collection = Collection
    exceptions = types.ModuleType("secretstorage.exceptions")
    exceptions.ItemNotFoundException = _ItemNotFound
    monkeypatch.setitem(sys.modules, "secretstorage", secretstorage)
    monkeypatch.setitem(sys.modules, "secretstorage.collection", collection)
    monkeypatch.setitem(sys.modules, "secretstorage.exceptions", exceptions)

    class SecretServiceBackend:
        priority = 5
        prompts = 0

        def get_preferred_collection(self):  # keyring's path: it would prompt
            SecretServiceBackend.prompts += 1

        def get_password(self, service, username):
            if state["locked"]:
                threading.Event().wait()  # an unlock prompt nobody answers
            return "bearer-token-value"

    store = NativeCredentialStore(backend=SecretServiceBackend(), platform_name="Linux", enforce_native_backend=False)

    async def read():
        return await profile_session.OAuthProfileSessionService._in_custody(store.get, "ref")

    with pytest.raises(CredentialError) as locked:
        asyncio.run(read())
    assert locked.value.code == "credential_store_locked"
    state["locked"] = False  # the operator unlocks
    assert asyncio.run(read()) == "bearer-token-value"
    assert SecretServiceBackend.prompts == 0

"""One lock per native credential; the store-wide lock never spans a keychain call (W464).

The service is the real OAuthProfileSessionService with the CLI suite's fake
discovery, authorization and token store. The token store can hold a get on a
test-owned gate, which stands for a slow keychain call.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading

import pytest

from connection_hub.caller.authorization import profile_session
from test_oauth_profiles import ENDPOINT, _service


class _HeldStore:
    """Wrap a token store so a get of one credential waits on a gate."""

    def __init__(self, inner, held_ref: str, gate: threading.Event, entered: threading.Event):
        self._inner, self._held_ref, self._gate, self._entered = inner, held_ref, gate, entered

    def get(self, credential_ref):
        if credential_ref == self._held_ref:
            self._entered.set()
            assert self._gate.wait(5), "test gate was not released"
        return self._inner.get(credential_ref)

    def put(self, credential_ref, token):
        return self._inner.put(credential_ref, token)

    def remove(self, credential_ref):
        return self._inner.remove(credential_ref)


async def _authorized(tmp_path, names):
    service, profiles, credentials = _service(tmp_path)
    for name in names:
        await service.authorize(name=name, endpoint=ENDPOINT)
    return service, profiles, credentials


def _spans(caplog, kind):
    return [
        dict(re.findall(r"(\w+)=(\S+)", r.getMessage()))
        for r in caplog.records
        if r.name == "connection_hub.oauth.spans" and f"kind={kind} " in r.getMessage()
    ]


@pytest.mark.asyncio
async def test_the_store_wide_lock_is_free_while_a_keychain_call_runs(tmp_path):
    service, profiles, credentials = await _authorized(tmp_path, ["agent-a"])
    held_ref = profiles.require("agent-a").credential_ref
    gate, entered = threading.Event(), threading.Event()
    service._credentials = _HeldStore(credentials, held_ref, gate, entered)
    reading = asyncio.create_task(service.access_token("agent-a"))
    try:
        assert await asyncio.to_thread(entered.wait, 3), "the read reached the keychain"
        # While agent-a's keychain call runs, anyone can take the store-wide lock.
        async def take_store_lock():
            async with service._transaction(
                service._transaction_lock, profile_name="other", operation="probe"
            ):
                return True

        assert await asyncio.wait_for(take_store_lock(), timeout=1.0)
    finally:
        gate.set()
    assert await reading


@pytest.mark.asyncio
async def test_another_agents_credential_lock_does_not_wait_for_a_held_one(tmp_path, caplog):
    service, profiles, credentials = await _authorized(tmp_path, ["agent-a", "agent-b"])
    held_ref = profiles.require("agent-a").credential_ref
    gate, entered = threading.Event(), threading.Event()
    service._credentials = _HeldStore(credentials, held_ref, gate, entered)
    caplog.set_level(logging.DEBUG, logger="connection_hub.oauth.spans")
    reading_a = asyncio.create_task(service.access_token("agent-a"))
    assert await asyncio.to_thread(entered.wait, 3)
    reading_b = asyncio.create_task(service.access_token("agent-b"))
    await asyncio.sleep(0.4)
    gate.set()
    await asyncio.gather(reading_a, reading_b)
    b_tag = profile_session.lock_spans.profile_tag("agent-b")
    b_credential = [s for s in _spans(caplog, "credential") if s["profile"] == b_tag and s["outcome"] == "ok"]
    assert b_credential, "agent-b took its own credential lock"
    assert all(int(s["wait_ms"]) < 200 for s in b_credential), (
        "agent-b's lock never waited for agent-a's keychain call"
    )
    # agent-b's keychain call itself still queues on the one custody thread:
    # that queue is measured by the custody records, not hidden (P2-B, point 1).


@pytest.mark.asyncio
async def test_two_state_roots_holding_one_credential_take_turns(tmp_path, caplog):
    first_root = tmp_path / "first"
    first_root.mkdir()
    service_1, profiles_1, credentials = await _authorized(first_root, ["agent-a"])
    second_root = tmp_path / "second"
    second_root.mkdir()
    service_2, profiles_2, _ = _service(second_root)
    profiles_2.add(profiles_1.require("agent-a"))  # sibling recovery copies the profile as is
    service_2._credentials = credentials  # one native store, as on a real host
    held_ref = profiles_1.require("agent-a").credential_ref
    gate, entered = threading.Event(), threading.Event()
    service_1._credentials = _HeldStore(credentials, held_ref, gate, entered)
    caplog.set_level(logging.DEBUG, logger="connection_hub.oauth.spans")
    reading_1 = asyncio.create_task(service_1.access_token("agent-a"))
    assert await asyncio.to_thread(entered.wait, 3)
    reading_2 = asyncio.create_task(service_2.access_token("agent-a"))
    await asyncio.sleep(0.4)
    assert not reading_2.done(), "the second root waits for the same credential's lock"
    gate.set()
    await asyncio.gather(reading_1, reading_2)
    waits = sorted(int(s["wait_ms"]) for s in _spans(caplog, "credential") if s["outcome"] == "ok")
    assert waits[-1] >= 300, "one root's credential lock waited for the other's keychain call"


@pytest.mark.asyncio
async def test_a_cancel_while_waiting_for_the_credential_lock_touches_no_keychain(tmp_path):
    from filelock import AsyncFileLock

    service, profiles, credentials = await _authorized(tmp_path, ["agent-a"])
    ref = profiles.require("agent-a").credential_ref
    calls = []
    original_get = credentials.get
    credentials.get = lambda r: (calls.append(r), original_get(r))[1]
    lock_path = service._credential_lock_path(ref)
    async with AsyncFileLock(str(lock_path), timeout=5):
        reading = asyncio.create_task(service.access_token("agent-a"))
        await asyncio.sleep(0.2)
        reading.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reading
    assert calls == [], "no keychain call was made by the cancelled read"


@pytest.mark.asyncio
async def test_a_cancel_before_the_authorize_commit_revokes_the_grant(tmp_path):
    from test_oauth_profiles import _OAuth

    oauth = _OAuth()
    released = asyncio.Event()

    async def slow_probe(**_kwargs):
        await released.wait()

    service, profiles, _ = _service(tmp_path, oauth=oauth, probe=slow_probe)
    task = asyncio.create_task(service.authorize(name="agent-a", endpoint=ENDPOINT))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert oauth.events == ["server.revoke"], "the unrecorded grant is revoked"
    assert profiles.get("agent-a") is None


@pytest.mark.asyncio
async def test_a_cancel_during_the_authorize_commit_keeps_the_stored_grant(tmp_path):
    from test_oauth_profiles import _OAuth

    oauth = _OAuth()
    service, profiles, credentials = _service(tmp_path, oauth=oauth)
    gate, entered = threading.Event(), threading.Event()
    original_put = credentials.put

    def held_put(ref, token):
        # Only the commit runs on the custody thread; the credential-store
        # check before the grant writes a probe value on the loop.
        if threading.current_thread().name.startswith("connection-hub-custody"):
            entered.set()
            assert gate.wait(5)
        return original_put(ref, token)

    credentials.put = held_put
    task = asyncio.create_task(service.authorize(name="agent-a", endpoint=ENDPOINT))
    assert await asyncio.to_thread(entered.wait, 3)
    task.cancel()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert oauth.events == [], "a committed grant is not revoked"
    assert profiles.get("agent-a") is not None


@pytest.mark.asyncio
async def test_a_credential_that_keeps_moving_is_named_as_such_not_as_a_timeout(tmp_path, monkeypatch):
    from dataclasses import replace

    from connection_hub_cli.errors import AuthorizationError

    service, profiles, _ = await _authorized(tmp_path, ["agent-a"])
    real = profiles.require("agent-a")
    reads = []

    def moving(name):
        reads.append(name)
        return replace(real, credential_ref=f"{real.credential_ref}-{len(reads)}")

    monkeypatch.setattr(service, "_require_oauth_profile", moving)
    with pytest.raises(AuthorizationError) as raised:
        await service.access_token("agent-a")
    assert raised.value.code == "oauth_profile_credential_changed"
    assert len(reads) == 4, "two attempts, each reading the record twice"


@pytest.mark.asyncio
async def test_a_second_cancel_during_the_revoke_still_finishes_the_revoke(tmp_path):
    """Infra on PR 445: a repeated cancel must not leave the unrecorded grant live."""

    from test_oauth_profiles import _OAuth

    probe_entered = asyncio.Event()
    revoke_entered = asyncio.Event()
    release_revoke = asyncio.Event()
    finished: list[str] = []

    class HeldRevokeOAuth(_OAuth):
        async def revoke(self, **kwargs):
            revoke_entered.set()
            await release_revoke.wait()
            finished.append("server.revoke")

    async def paused_probe(**_kwargs):
        probe_entered.set()
        await asyncio.Event().wait()

    service, profiles, _ = _service(tmp_path, oauth=HeldRevokeOAuth(), probe=paused_probe)
    task = asyncio.create_task(service.authorize(name="agent-a", endpoint=ENDPOINT))
    await asyncio.wait_for(probe_entered.wait(), 3)
    task.cancel()
    await asyncio.wait_for(revoke_entered.wait(), 3)
    task.cancel()  # a second cancel while the revoke runs
    await asyncio.sleep(0.05)
    assert not task.done(), "the revoke is still owned by the cancelled authorize"
    release_revoke.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished == ["server.revoke"], "the grant was revoked despite the second cancel"
    assert profiles.get("agent-a") is None


# P2-C2: removal and disconnect serialize with the credential lock (Infra's case on 7b14d6c4).


@pytest.mark.asyncio
async def test_a_retire_waits_while_another_task_holds_the_credential_lock(tmp_path):
    service, profiles, credentials = await _authorized(tmp_path, ["agent-a"])
    profile = profiles.require("agent-a")
    holding, release = asyncio.Event(), asyncio.Event()

    async def hold():
        async with service._credential_lock(profile.name, profile.credential_ref, operation="test_hold"):
            holding.set()
            await release.wait()

    holder = asyncio.create_task(hold())
    await holding.wait()
    retiring = asyncio.ensure_future(asyncio.to_thread(service.retire_local, profile))
    await asyncio.sleep(0.4)
    assert not retiring.done(), "the retire waits for the held credential lock"
    assert profiles.get("agent-a") is not None and credentials.get(profile.credential_ref) is not None
    release.set()
    await holder
    await retiring
    assert profiles.get("agent-a") is None


@pytest.mark.asyncio
async def test_retire_local_refuses_to_block_a_running_event_loop(tmp_path):
    from connection_hub_cli.errors import AuthorizationError

    service, profiles, _ = await _authorized(tmp_path, ["agent-a"])
    with pytest.raises(AuthorizationError) as raised:
        service.retire_local(profiles.require("agent-a"))
    assert raised.value.code == "oauth_profile_retire_on_event_loop"
    assert profiles.get("agent-a") is not None


@pytest.mark.asyncio
async def test_disconnect_revokes_and_retires_under_one_credential_lock(tmp_path):
    from test_oauth_profiles import _OAuth

    oauth = _OAuth()
    service, profiles, credentials = _service(tmp_path, oauth=oauth)
    await service.authorize(name="agent-a", endpoint=ENDPOINT)
    profile = profiles.require("agent-a")
    holding, release = asyncio.Event(), asyncio.Event()

    async def hold():
        async with service._credential_lock(profile.name, profile.credential_ref, operation="test_hold"):
            holding.set()
            await release.wait()

    holder = asyncio.create_task(hold())
    await holding.wait()
    disconnecting = asyncio.create_task(service.revoke_and_retire(profile))
    await asyncio.sleep(0.4)
    assert not disconnecting.done() and oauth.events == [], "nothing is revoked while the lock is held"
    release.set()
    await holder
    await disconnecting
    assert oauth.events == ["server.revoke"]
    assert profiles.get("agent-a") is None and credentials.get(profile.credential_ref) is None

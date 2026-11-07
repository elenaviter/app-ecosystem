"""Forget a revoked Card's local record only when this machine holds no credential for it (W1012).

After a password store reset the Card's credential is gone, so the Card cannot
be revoked from the machine. The person revokes it in Connection Hub and gives
its access_id; the local record is then forgotten. The review of the design
(codex-infra, 7 Nov 2026) asked for the check and the removal to happen under
the credential lock a refresh or reconnect takes, so a credential that arrives
meanwhile, or a record that changes, is never removed.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace

import pytest

from connection_hub.caller.authorization import profile_session
from connection_hub_cli.errors import AuthorizationError, ProfileError
from connection_hub_cli.models import ProbeResult
from connection_hub_cli.profiles import ProfileService
from connection_hub_cli.state import InstallationStore
from test_oauth_credential_lock import _authorized, _HeldStore


async def _reset_store(tmp_path):
    """One authorized profile whose credential the store reset lost."""

    service, profiles, credentials = await _authorized(tmp_path, ["agent-a"])
    profile = profiles.require("agent-a")
    token = credentials.get(profile.credential_ref)
    credentials.remove(profile.credential_ref)
    return service, profiles, credentials, profile, token


async def _hold(service, profile, holding, release):
    async with service._credential_lock(profile.name, profile.credential_ref, operation="test_hold"):
        holding.set()
        await release.wait()


@pytest.mark.asyncio
async def test_it_forgets_the_record_with_the_exact_access_id_and_touches_no_credential(tmp_path):
    service, profiles, credentials, profile, _ = await _reset_store(tmp_path)
    removed = await service.retire_revoked_without_custody("agent-a", access_id=profile.access_id)
    assert removed == profile
    assert profiles.get("agent-a") is None
    assert credentials.get(profile.credential_ref) is None


@pytest.mark.asyncio
async def test_another_access_id_changes_nothing(tmp_path):
    service, profiles, _, profile, _ = await _reset_store(tmp_path)
    with pytest.raises(ProfileError) as refused:
        await service.retire_revoked_without_custody("agent-a", access_id="aut_someone_else")
    assert refused.value.code == "oauth_profile_access_id_confirmation_required"
    assert profiles.get("agent-a") == profile


@pytest.mark.asyncio
async def test_a_credential_still_held_is_refused_and_kept(tmp_path):
    service, profiles, credentials = await _authorized(tmp_path, ["agent-a"])
    profile = profiles.require("agent-a")
    with pytest.raises(ProfileError) as refused:
        await service.retire_revoked_without_custody("agent-a", access_id=profile.access_id)
    assert refused.value.code == "oauth_profile_credential_present"
    assert profiles.get("agent-a") == profile
    assert credentials.get(profile.credential_ref) is not None


@pytest.mark.asyncio
async def test_a_credential_arriving_after_the_preliminary_check_is_refused_and_kept(tmp_path):
    service, profiles, credentials, profile, token = await _reset_store(tmp_path)
    # The caller's own preliminary check sees no credential.
    assert not service.credential_present(profile)
    holding, release = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(_hold(service, profile, holding, release))
    await holding.wait()
    retiring = asyncio.create_task(
        service.retire_revoked_without_custody("agent-a", access_id=profile.access_id)
    )
    await asyncio.sleep(0.3)
    assert not retiring.done(), "the retire waits for the credential lock"
    # A refresh or reconnect commits a credential while it holds the lock.
    credentials.put(profile.credential_ref, token)
    release.set()
    await holder
    with pytest.raises(ProfileError) as refused:
        await retiring
    assert refused.value.code == "oauth_profile_credential_present"
    assert profiles.get("agent-a") == profile
    assert credentials.get(profile.credential_ref) == token


@pytest.mark.asyncio
async def test_an_access_id_that_changes_while_waiting_for_the_lock_is_refused(tmp_path):
    service, profiles, _, profile, _ = await _reset_store(tmp_path)
    holding, release = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(_hold(service, profile, holding, release))
    await holding.wait()
    retiring = asyncio.create_task(
        service.retire_revoked_without_custody("agent-a", access_id=profile.access_id)
    )
    await asyncio.sleep(0.3)
    replacement = replace(profile, access_id="aut_replacement")
    profiles.update(replacement)
    release.set()
    await holder
    with pytest.raises(ProfileError) as refused:
        await retiring
    assert refused.value.code == "oauth_profile_access_id_confirmation_required"
    assert profiles.get("agent-a") == replacement


@pytest.mark.asyncio
async def test_a_replacement_record_on_another_credential_is_never_removed(tmp_path):
    service, profiles, credentials, profile, token = await _reset_store(tmp_path)
    holding, release = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(_hold(service, profile, holding, release))
    await holding.wait()
    retiring = asyncio.create_task(
        service.retire_revoked_without_custody("agent-a", access_id=profile.access_id)
    )
    await asyncio.sleep(0.3)
    # A reconnect writes a new credential and points the record at it.
    replacement = replace(profile, credential_ref="b" * 32, access_id="aut_replacement")
    credentials.put("b" * 32, token)
    profiles.update(replacement)
    release.set()
    await holder
    with pytest.raises(ProfileError) as refused:
        await retiring
    assert refused.value.code == "oauth_profile_access_id_confirmation_required"
    assert profiles.get("agent-a") == replacement
    assert credentials.get("b" * 32) == token


def test_remove_matching_refuses_a_record_that_changed(tmp_path):
    from connection_hub_cli.state import ProfileStore
    from test_oauth_profiles import _profile

    store = ProfileStore(tmp_path / "profiles.json")
    profile = _profile()
    store.add(profile)
    changed = replace(profile, access_id="aut_changed")
    store.update(changed)
    with pytest.raises(ProfileError) as refused:
        store.remove_matching(profile.name, profile)
    assert refused.value.code == "profile_changed"
    assert store.get(profile.name) == changed
    assert store.remove_matching(profile.name, changed) == changed
    assert store.get(profile.name) is None


@pytest.mark.asyncio
async def test_a_credential_store_that_does_not_answer_refuses(tmp_path, monkeypatch):
    service, profiles, credentials, profile, _ = await _reset_store(tmp_path)
    monkeypatch.setattr(profile_session, "CUSTODY_DEADLINE_SECONDS", 0.2)
    gate, entered = threading.Event(), threading.Event()
    service._credentials = _HeldStore(credentials, profile.credential_ref, gate, entered)
    try:
        with pytest.raises(AuthorizationError) as refused:
            await service.retire_revoked_without_custody("agent-a", access_id=profile.access_id)
        assert refused.value.code == "oauth_credential_custody_timeout"
        assert profiles.get("agent-a") == profile
    finally:
        gate.set()


async def _profile_service(tmp_path):
    service, profiles, credentials, profile, token = await _reset_store(tmp_path)

    async def probe(**_kwargs):
        return ProbeResult(tool_count=0)

    installations = InstallationStore(tmp_path / "installations.json")
    return (
        ProfileService(
            profiles=profiles,
            installations=installations,
            credentials=credentials,
            probe=probe,
            oauth_sessions=service,
        ),
        profiles,
        installations,
        profile,
    )


@pytest.mark.asyncio
async def test_the_profile_service_forgets_through_the_guarded_section(tmp_path):
    service, profiles, _, profile = await _profile_service(tmp_path)
    removed = await service.retire_revoked("agent-a", access_id=profile.access_id)
    assert removed.profile == profile and removed.dangling_installations == 0
    assert profiles.get("agent-a") is None


@pytest.mark.asyncio
async def test_the_profile_service_refuses_a_non_oauth_profile(tmp_path):
    service, profiles, _, profile = await _profile_service(tmp_path)
    from connection_hub_cli.models import CallerProfile

    bearer = CallerProfile.create(name="bearer-a", endpoint=profile.endpoint)
    profiles.add(bearer)
    with pytest.raises(ProfileError) as refused:
        await service.retire_revoked("bearer-a", access_id=profile.access_id)
    assert refused.value.code == "profile_not_oauth"
    assert profiles.get("bearer-a") == bearer


# Review of AE #947 (codex-infra), findings 1 and 4.


@pytest.mark.asyncio
async def test_an_older_valid_record_without_record_version_is_still_forgotten(tmp_path):
    import json

    service, profiles, _, profile, _ = await _reset_store(tmp_path)
    document = json.loads(profiles.path.read_text())
    document["profiles"]["agent-a"].pop("record_version", None)
    profiles.path.write_text(json.dumps(document))
    older = profiles.require("agent-a")
    assert older == profile
    removed = await service.retire_revoked_without_custody("agent-a", access_id=profile.access_id)
    assert removed == profile and profiles.get("agent-a") is None


@pytest.mark.asyncio
async def test_the_bounded_presence_read_answers_yes_and_no(tmp_path):
    service, profiles, credentials = await _authorized(tmp_path, ["agent-a"])
    profile = profiles.require("agent-a")
    assert await service.credential_present_bounded(profile) is True
    credentials.remove(profile.credential_ref)
    assert await service.credential_present_bounded(profile) is False


_STUCK_SCRIPT = r"""
import asyncio, sys, threading, time
sys.path.insert(0, sys.argv[1])
from connection_hub.caller.authorization import profile_session
from test_oauth_credential_lock import _authorized

profile_session.CUSTODY_DEADLINE_SECONDS = 0.5

class Stuck:
    def __init__(self, inner):
        self.inner = inner
    def get(self, ref):
        threading.Event().wait()  # a native store that never answers
    def put(self, ref, token):
        return self.inner.put(ref, token)
    def remove(self, ref):
        return self.inner.remove(ref)

async def main(tmp):
    import pathlib
    service, profiles, credentials = await _authorized(pathlib.Path(tmp), ["agent-a"])
    profile = profiles.require("agent-a")
    service._credentials = Stuck(credentials)
    for call in (
        lambda: service.credential_present_bounded(profile),
        lambda: service.retire_revoked_without_custody("agent-a", access_id=profile.access_id),
    ):
        try:
            await call()
        except Exception as exc:
            print(getattr(exc, "code", type(exc).__name__), flush=True)
    assert profiles.get("agent-a") == profile

asyncio.run(main(sys.argv[2]))
print("exited-cleanly", flush=True)
"""


def test_a_store_that_never_answers_refuses_and_the_process_still_exits(tmp_path):
    import os
    import subprocess
    import sys
    import time

    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-c", _STUCK_SCRIPT, os.path.dirname(__file__), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ},
    )
    elapsed = time.monotonic() - started
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.split() == [
        "oauth_credential_custody_timeout",
        "oauth_credential_custody_timeout",
        "exited-cleanly",
    ]
    assert elapsed < 30, elapsed

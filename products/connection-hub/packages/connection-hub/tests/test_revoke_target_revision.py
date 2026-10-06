"""Caller intent reaches the real durable Card revoke fence, not a reread."""

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from connection_hub.delegated_credentials.automation_access import AutomationAccessService
from connection_hub.delegated_credentials.cards.model import CARD_STATE_REVOKED
from connection_hub.delegated_credentials.cards.persistence import DurableCardPersistence
from connection_hub.delegated_credentials.cards.service import DelegatedCardService
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore, subject_hash_for
from connection_hub.delegated_credentials.issuer_gate import IssuerRegistry
from connection_hub.delegated_credentials.remote_issuer import RemoteIssuerAdapter
from test_card_service import _Cache
from test_credentialless_card_persistence import _Handles
from test_project_control_cards import _regular_control, _Redis


async def _fixture(tmp_path):
    authority = replace(_regular_control(revision=1), issuer_kind="opaque",
                        issuer_ref="authority:synthetic")
    store = BundleStorageDelegatedCardStore(tmp_path)
    cache = _Cache()
    lock = asyncio.Lock()
    waiting, release = asyncio.Event(), asyncio.Event()
    pause_next = [False]

    @asynccontextmanager
    async def mutation_lock(**kwargs):
        if pause_next[0]:
            pause_next[0] = False
            waiting.set()
            await release.wait()
        async with lock:
            yield

    cards = DelegatedCardService(store=store, cache=cache, mutation_lock=mutation_lock)
    subject_hash = subject_hash_for(authority.grantor_subject)
    await cards.commit(authority, subject_hash=subject_hash, expected_revision=0, now=int(time.time()))
    handles = _Handles()
    persistence = DurableCardPersistence(redis=object(), tenant="tenant", project="project",
        card_store=store, mutation_lock=mutation_lock, credential_handles=handles)
    # Only the serving projection is fake. Loads, persistence, immutable
    # revisions and the inside-lock comparison use production implementations.
    persistence._cards = cards
    service = AutomationAccessService(redis=_Redis(), tenant="tenant", project="project",
        config=None, grant_store=object(), card_persistence=persistence)
    service.notify_change = AsyncMock()
    requests = []

    async def prepare(body):
        return {"ok": True, "request": body["request"],
                "change_digest": body["request"]["change_digest"], "context_ref": "server-context"}

    async def decide(request):
        requests.append(request)
        return {"ok": True, "request": request, "decision": {
            "allowed": True, "reason": "", "policy_version": "policy-1",
            "valid_until": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()}}

    registry = IssuerRegistry()
    registry.register(RemoteIssuerAdapter(issuer_kind="opaque", adapter_id="synthetic-peer",
        transport=decide, prepare_transport=prepare))
    service.bind_issuer_registry(registry, actor_subject="authenticated-actor")
    return SimpleNamespace(authority=authority, store=store, cards=cards, cache=cache,
        service=service, handles=handles, subject_hash=subject_hash, requests=requests,
        waiting=waiting, release=release, pause_next=pause_next,
        user={"user_id": authority.grantor_subject})


async def _current(f):
    return (await f.store.read_current_authority(
        subject_hash=f.subject_hash, access_id=f.authority.access_id))[1]


@pytest.mark.asyncio
async def test_exact_caller_target_is_revoked_under_the_durable_fence(tmp_path):
    f = await _fixture(tmp_path)
    result = await f.service.revoke_access(f.user, access_id=f.authority.access_id,
        expected_access_id=f.authority.access_id, expected_card_revision=1,
        _issuer_request_id="synthetic-request")
    assert result["ok"] and result["removed"]
    current = await _current(f)
    assert current.state == CARD_STATE_REVOKED and current.card_revision == 2
    assert [r["card_revision"] for r in f.requests] == [1, 1]
    assert all(r["actor_subject"] == "authenticated-actor" for r in f.requests)
    assert f.handles.removed_ids == [f.authority.access_id]
    f.service.notify_change.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("expectations", [
    {}, {"expected_access_id": "control-regular"}, {"expected_card_revision": 1},
    {"expected_access_id": "control-regular", "expected_card_revision": True},
    {"expected_access_id": "control-regular", "expected_card_revision": 1.1},
    {"expected_access_id": "control-regular", "expected_card_revision": "1"},
    {"expected_access_id": "control-regular", "expected_card_revision": 0},
    {"expected_access_id": "other-target", "expected_card_revision": 1},
])
async def test_missing_invalid_or_changed_target_expectations_have_no_effects(tmp_path, expectations):
    f = await _fixture(tmp_path)
    result = await f.service.revoke_access(f.user, access_id=f.authority.access_id, **expectations)
    assert result["ok"] is False
    assert await _current(f) == f.authority
    assert f.requests == [] and f.handles.removed_ids == []
    f.service.notify_change.assert_not_called()


@pytest.mark.asyncio
async def test_replacement_before_load_does_not_adopt_the_new_revision(tmp_path):
    f = await _fixture(tmp_path)
    replacement = replace(f.authority, card_revision=2, label="replacement")
    await f.cards.commit(replacement, subject_hash=f.subject_hash, expected_revision=1, now=int(time.time()))
    result = await f.service.revoke_access(f.user, access_id=f.authority.access_id,
        expected_access_id=f.authority.access_id, expected_card_revision=1)
    assert result["error"] == "delegated_card_revision_conflict"
    assert result["status"] == 409 and result["retryable"] is False
    assert await _current(f) == replacement
    assert f.requests == [] and f.handles.removed_ids == []
    f.service.notify_change.assert_not_called()


@pytest.mark.asyncio
async def test_replacement_between_load_and_durable_lock_is_refused_before_effects(tmp_path):
    f = await _fixture(tmp_path)
    f.pause_next[0] = True
    revoke = asyncio.create_task(f.service.revoke_access(f.user, access_id=f.authority.access_id,
        expected_access_id=f.authority.access_id, expected_card_revision=1))
    try:
        await asyncio.wait_for(f.waiting.wait(), timeout=2)
        replacement = replace(f.authority, card_revision=2, label="replacement")
        await f.cards.commit(replacement, subject_hash=f.subject_hash, expected_revision=1, now=int(time.time()))
        # Instrument after the competing write: the refused revoke must do
        # nothing to projections, handles, authority, or notifications.
        effects = []
        for method in ("read", "reconcile_projection", "claim_transition", "commit_projection",
                       "commit_tombstone", "finalize_removal", "index_add", "index_remove"):
            original = getattr(f.cache, method)

            async def spy(*args, _method=method, _original=original, **kwargs):
                effects.append(_method)
                return await _original(*args, **kwargs)

            setattr(f.cache, method, spy)
        f.release.set()
        result = await asyncio.wait_for(revoke, timeout=2)
        assert result["error"] == "delegated_card_revision_conflict"
        assert result["reason"] == "card_revision_moved"
        assert result["status"] == 409 and result["retryable"] is False
        assert await _current(f) == replacement
        assert effects == [] and f.handles.removed_ids == []
        # Decide happened before the lock; revalidation was never reached.
        assert len(f.requests) == 1
        f.service.notify_change.assert_not_called()
    finally:
        f.release.set()
        if not revoke.done():
            revoke.cancel()
            await asyncio.gather(revoke, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["absent", "revoked"])
async def test_absent_or_already_revoked_expected_target_is_a_conflict(tmp_path, state):
    f = await _fixture(tmp_path)
    access_id = "absent-target" if state == "absent" else f.authority.access_id
    if state == "revoked":
        await f.cards.revoke(subject_hash=f.subject_hash, access_id=access_id, expected_revision=1)
    result = await f.service.revoke_access(f.user, access_id=access_id,
        expected_access_id=access_id, expected_card_revision=1)
    assert result["error"] == "delegated_card_revision_conflict"
    assert f.requests == [] and f.handles.removed_ids == []
    f.service.notify_change.assert_not_called()

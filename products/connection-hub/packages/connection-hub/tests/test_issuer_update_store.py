"""Disposable filesystem and injected-cache tests; not mounted Redis/flock qualification."""
import asyncio
import dataclasses
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from connection_hub.delegated_credentials.cards import update_store
from connection_hub.delegated_credentials.cards.cache import CardCacheEntry
from connection_hub.delegated_credentials.cards.persistence import DurableCardPersistence
from connection_hub.delegated_credentials.cards.service import DelegatedCardService, CardConflict
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore, CardStorageError
from connection_hub.delegated_credentials.issuer_update import IssuerUpdateQuery, issuer_managed_card_update
from test_lifecycle_store import _test_lock
from test_issuer_update import ACTOR, card, registry, wire


async def fixture(tmp_path):
    original = card()
    query = IssuerUpdateQuery.from_mapping(wire(original))
    store = BundleStorageDelegatedCardStore(tmp_path, lifecycle_lock_scope="same-host-flock")
    pointer = await store.write_revision(subject_hash=query.target.subject_hash, authority=original,
                                         updated_at=datetime.now(timezone.utc))
    await store.advance_current(subject_hash=query.target.subject_hash, pointer=pointer)
    live = {"entry": CardCacheEntry("card", authority=original, card_revision=1)}
    cache = MagicMock()

    async def claim(access_id, *, mutation_id, expected_revision, ttl_seconds):
        live["entry"] = CardCacheEntry("updating", card_revision=expected_revision, mutation_id=mutation_id)
        return True

    async def commit(authority, **kwargs):
        live["entry"] = CardCacheEntry("card", authority=authority, card_revision=authority.card_revision)
        return True

    async def remove(*args, **kwargs):
        live["entry"] = None
        return True

    cache.claim_transition = AsyncMock(side_effect=claim)
    cache.commit_projection = AsyncMock(side_effect=commit)
    cache.finalize_removal = AsyncMock(side_effect=remove)
    cache.read = AsyncMock(side_effect=lambda *args: live["entry"])
    handles = MagicMock()
    persistence = DurableCardPersistence(redis=MagicMock(), tenant="fixture", project="fixture", card_store=store,
        mutation_lock=_test_lock, credential_handles=handles)
    service = persistence.card_service
    service._cache = cache
    service._reconcile = AsyncMock()
    service._index = AsyncMock()
    handles.reset_mock()  # constructor chooses the injected store by truthiness
    return original, query, store, service, persistence, cache, handles


async def apply(persistence, reg, raw=None, host=lambda: True):
    return await issuer_managed_card_update(raw or wire(), actor_subject=ACTOR, registry=reg,
                                           persistence=persistence, host_is_current=host)


@pytest.mark.asyncio
async def test_commits_once_and_replays_original_even_after_a_later_legitimate_revision(tmp_path):
    original, query, store, service, persistence, cache, handles = await fixture(tmp_path)
    reg, calls = registry()
    result = await apply(persistence, reg)
    assert result["ok"] and result["state"] == "committed" and result["serving_state"] == "complete"
    assert result["authority"]["card_revision"] == 2 and len(calls) == 2
    authority = await persistence.read_issuer_update_authority(await update_store.read_receipt(store, query.transaction_id(ACTOR)))
    later = dataclasses.replace(authority, card_revision=3, label="later legitimate revision")
    pointer = await store.write_revision(subject_hash=query.target.subject_hash, authority=later, updated_at=datetime.now(timezone.utc))
    await store.advance_current(subject_hash=query.target.subject_hash, pointer=pointer)
    again = await apply(persistence, reg)
    assert again == result and len(calls) == 2 and cache.claim_transition.await_count == 1
    assert (await store.read_current_authority(subject_hash=query.target.subject_hash, access_id=query.target.access_id))[1] == later
    assert handles.mock_calls == []


@pytest.mark.asyncio
async def test_changed_body_same_actor_context_and_id_is_not_another_mutation(tmp_path):
    _, query, store, _, persistence, cache, _ = await fixture(tmp_path)
    reg, calls = registry()
    assert (await apply(persistence, reg))["ok"]
    changed = wire()
    changed["delta"]["grants"] = ["extra", "read", "write"]
    result = await apply(persistence, reg, changed)
    assert result["error"] == "issuer_update_replay_changed" and "authority" not in result
    assert cache.claim_transition.await_count == 1 and len(calls) == 2


@pytest.mark.asyncio
async def test_policy_denial_is_terminal_no_write_and_same_id_does_not_reopen(tmp_path):
    original, query, store, _, persistence, cache, handles = await fixture(tmp_path)
    denied, denied_calls = registry(allow=False)
    result = await apply(persistence, denied)
    assert result["state"] == "refused" and result["reason"] == "fixture_policy_denied"
    allowed, calls = registry()
    again = await apply(persistence, allowed)
    assert again["state"] == "refused" and calls == []
    assert (await store.read_current_authority(subject_hash=query.target.subject_hash, access_id=query.target.access_id))[1] == original
    cache.claim_transition.assert_not_called()
    assert handles.mock_calls == []


@pytest.mark.asyncio
async def test_prepared_pointer_and_history_do_not_publish_staged_update(tmp_path, monkeypatch):
    original, query, store, _, persistence, _, _ = await fixture(tmp_path)
    reg, calls = registry()
    write = update_store.write_json_atomic

    async def inspect(path, payload):
        if path == update_store.receipt_path(store, query.transaction_id(ACTOR)) and payload["state"] == "committed":
            assert (await store.read_current_authority(subject_hash=query.target.subject_hash, access_id=query.target.access_id))[1] == original
            assert len(await store.list_revision_names(subject_hash=query.target.subject_hash, access_id=query.target.access_id)) == 1
            with pytest.raises(CardStorageError, match="preparation_unresolved"):
                await store.advance_current(subject_hash=query.target.subject_hash,
                    pointer=(await store.read_current_authority(subject_hash=query.target.subject_hash, access_id=query.target.access_id))[0])
            raise RuntimeError("fixture interrupted before visibility")
        await write(path, payload)

    monkeypatch.setattr(update_store, "write_json_atomic", inspect)
    result = await apply(persistence, reg)
    assert result["state"] == "refused"
    assert (await store.read_current_authority(subject_hash=query.target.subject_hash, access_id=query.target.access_id))[1] == original
    assert len(await store.list_revision_names(subject_hash=query.target.subject_hash, access_id=query.target.access_id)) == 1


@pytest.mark.asyncio
async def test_error_after_authoritative_receipt_rename_is_still_committed(tmp_path, monkeypatch):
    _, query, store, _, persistence, _, _ = await fixture(tmp_path)
    reg, _ = registry()
    write = update_store.write_json_atomic
    injected = False

    async def fail_after(path, payload):
        nonlocal injected
        await write(path, payload)
        if not injected and path == update_store.receipt_path(store, query.transaction_id(ACTOR)) and payload["state"] == "committed":
            injected = True
            raise OSError("fixture after rename")

    monkeypatch.setattr(update_store, "write_json_atomic", fail_after)
    result = await apply(persistence, reg)
    assert result["ok"] and result["state"] == "committed"


@pytest.mark.asyncio
async def test_serving_failure_returns_committed_pending_and_identical_retry_finishes(tmp_path):
    _, _, _, _, persistence, cache, _ = await fixture(tmp_path)
    reg, calls = registry()
    side_effect = cache.commit_projection.side_effect
    cache.commit_projection.side_effect = RuntimeError("fixture serving outage")
    result = await apply(persistence, reg)
    assert not result["ok"] and result["state"] == "committed" and result["serving_state"] == "pending"
    assert result["status"] == 202 and "authority" not in result
    cache.commit_projection.side_effect = side_effect
    again = await apply(persistence, reg)
    assert again["ok"] and len(calls) == 2 and cache.claim_transition.await_count == 1


@pytest.mark.asyncio
async def test_original_expiry_is_checked_inside_actual_publication_thread(tmp_path, monkeypatch):
    original, query, store, _, persistence, _, _ = await fixture(tmp_path)
    reg, _ = registry(until_seconds=0.15)
    from connection_hub.delegated_credentials import durable_io
    write = durable_io._write_text_atomic

    def delayed(path, text):
        if path == update_store.receipt_path(store, query.transaction_id(ACTOR)) and '"state": "committed"' in text:
            import time
            time.sleep(0.18)
        return write(path, text)

    monkeypatch.setattr(durable_io, "_write_text_atomic", delayed)
    result = await apply(persistence, reg)
    assert result["state"] == "refused" and result["reason"] == "issuer_decision_expired"
    assert (await store.read_current_authority(subject_hash=query.target.subject_hash, access_id=query.target.access_id))[1] == original


@pytest.mark.asyncio
async def test_context_moves_during_final_policy_read_no_authority_is_published(tmp_path):
    original, query, store, _, persistence, _, _ = await fixture(tmp_path)
    reg, calls = registry()
    adapter = reg._adapters[card().issuer_kind]
    decide = adapter.decide
    current = True

    async def move(request):
        nonlocal current
        response = await decide(request)
        if len(calls) == 2:
            current = False
        return response

    adapter.decide = move
    result = await apply(persistence, reg, host=lambda: current)
    assert result["state"] == "refused" and "authority" not in result
    assert (await store.read_current_authority(subject_hash=query.target.subject_hash, access_id=query.target.access_id))[1] == original


@pytest.mark.asyncio
async def test_finalization_failure_does_not_erase_commit_and_can_be_retried(tmp_path):
    _, _, _, _, persistence, cache, _ = await fixture(tmp_path)
    reg, calls = registry(finalize=False)
    result = await apply(persistence, reg)
    assert result["state"] == "committed" and result["status"] == 202 and not result["issuer_outcome_confirmed"]
    reg._adapters[card().issuer_kind].finalize_context = AsyncMock(return_value=True)
    assert (await apply(persistence, reg))["ok"] and len(calls) == 2 and cache.claim_transition.await_count == 1


@pytest.mark.asyncio
async def test_cancelled_publication_drains_started_io_before_fences_release(tmp_path, monkeypatch):
    import threading
    from contextlib import asynccontextmanager
    from connection_hub.delegated_credentials import durable_io

    _, query, store, service, persistence, _, _ = await fixture(tmp_path)
    reg, calls = registry()
    held = 0
    started, release = threading.Event(), threading.Event()
    write = durable_io._write_text_atomic

    @asynccontextmanager
    async def tracking_lock(**kwargs):
        nonlocal held
        held += 1
        try:
            yield
        finally:
            held -= 1

    def paused(path, text):
        if path == update_store.receipt_path(store, query.transaction_id(ACTOR)) and '"state": "committed"' in text:
            started.set()
            assert release.wait(5), "disposable write was not released"
            assert held == 2
        return write(path, text)

    service._mutation_lock = tracking_lock
    monkeypatch.setattr(durable_io, "_write_text_atomic", paused)
    task = asyncio.create_task(apply(persistence, reg))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 5), 6)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done() and held == 2
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert held == 0
    assert (await update_store.read_receipt(store, query.transaction_id(ACTOR)))["state"] == "committed"
    assert (await apply(persistence, reg))["ok"] and len(calls) == 2


@pytest.mark.asyncio
async def test_expired_marker_refilled_with_before_does_not_strand_a_refused_intent(tmp_path):
    original, query, store, _, persistence, cache, _ = await fixture(tmp_path)
    reg, calls = registry()
    cache.read.side_effect = None
    cache.read.return_value = CardCacheEntry("card", authority=original, card_revision=1)
    cache.finalize_removal.side_effect = None
    cache.finalize_removal.return_value = False
    result = await apply(persistence, reg)
    assert result["state"] == "refused" and result["reason"] == "issuer_update_serving_fence_lost"
    assert result["serving_state"] == "complete" and result["requires_new_request_id"]
    assert not update_store.active_path(store, query.transaction_id(ACTOR)).exists()
    again = await apply(persistence, reg)
    assert again["state"] == "refused" and len(calls) == 2
    current = await store.read_current_authority(subject_hash=query.target.subject_hash, access_id=query.target.access_id)
    assert current[1] == original
    # The diagnostic intent is retired; a normal writer may now progress.
    await store.advance_current(subject_hash=query.target.subject_hash, pointer=current[0])


@pytest.mark.asyncio
async def test_identical_terminal_recovery_retries_active_retirement_without_overwriting_later_revision(tmp_path, monkeypatch):
    _, query, store, _, persistence, cache, _ = await fixture(tmp_path)
    reg, calls = registry()
    retire = update_store.retire

    async def interrupted_retirement(*args):
        pass  # models a crash after terminal receipt rename, before unlink

    monkeypatch.setattr(update_store, "retire", interrupted_retirement)
    original_result = await apply(persistence, reg)
    assert original_result["ok"] and update_store.active_path(store, query.transaction_id(ACTOR)).exists()
    monkeypatch.setattr(update_store, "retire", retire)
    current = await store.read_current_authority(subject_hash=query.target.subject_hash, access_id=query.target.access_id)
    later = dataclasses.replace(current[1], card_revision=3, label="later legitimate revision")
    pointer = await store.write_revision(subject_hash=query.target.subject_hash, authority=later,
        updated_at=datetime.now(timezone.utc))
    await store.advance_current(subject_hash=query.target.subject_hash, pointer=pointer)
    assert await apply(persistence, reg) == original_result
    assert not update_store.active_path(store, query.transaction_id(ACTOR)).exists()
    assert len(calls) == 2 and cache.claim_transition.await_count == 1 and cache.commit_projection.await_count == 1
    assert (await store.read_current_authority(subject_hash=query.target.subject_hash, access_id=query.target.access_id))[1] == later

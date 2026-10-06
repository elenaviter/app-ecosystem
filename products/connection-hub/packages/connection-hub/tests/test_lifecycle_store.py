"""Storage visibility tests only: not issuer, serving or mounted qualification."""

from __future__ import annotations

import dataclasses
import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from connection_hub.delegated_credentials.cards import lifecycle_store
from connection_hub.delegated_credentials.cards.lifecycle import LifecycleRefused, LifecycleRequest
from connection_hub.delegated_credentials.cards.model import CardAuthority, NamedServiceSelection
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore, CardStorageError, subject_hash_for
from connection_hub.delegated_credentials.issuer_gate import change_digest

MOMENT = datetime(2026, 10, 6, tzinfo=timezone.utc)
ACTOR = "authenticated-human-admin"


def _pair():
    return tuple(CardAuthority(
        access_id=f"card-{index}", grantor_subject=f"owner-{index}", source="control",
        client_id="", delegate_subject="",
        card_kind="control", card_revision=1, state="active", issuer_kind=f"opaque-{index}",
        issuer_ref="opaque-lineage", resource_grants={}, resource_operations={},
        named_service_operations=NamedServiceSelection.none(),
        properties={"opaque.identity-marker": {"paired-with": f"card-{1-index}"}},
    ) for index in range(2))


def _wire(authorities):
    targets = [{"owner_subject": card.grantor_subject, "access_id": card.access_id,
                "expected_card_revision": card.card_revision,
                "expected_authority_fingerprint": card.content_hash(),
                "issuer_kind": card.issuer_kind, "issuer_ref": card.issuer_ref}
               for card in authorities]
    targets.sort(key=lambda target: (target["owner_subject"], target["access_id"]))
    return {"context_ref": "synthetic-context", "request_id": "synthetic-request",
            "action": "revoke", "change_digest": change_digest({"action": "revoke", "targets": targets}),
            "targets": targets}


async def _seed(store, authorities):
    for card in authorities:
        scope = subject_hash_for(card.grantor_subject)
        pointer = await store.write_revision(subject_hash=scope, authority=card, updated_at=MOMENT)
        await store.advance_current(subject_hash=scope, pointer=pointer)


async def _states(store, request):
    result = []
    for target in request.targets:
        current = await store.read_current_authority(subject_hash=target.subject_hash, access_id=target.access_id)
        result.append(None if current is None else (current[1].state, current[1].card_revision))
    return result


async def _allow():
    pass


@asynccontextmanager
async def _test_lock(**kwargs):
    yield {}


@pytest.mark.asyncio
@pytest.mark.parametrize("reader", ["store", "service", "resolver", "list_active", "list_current", "rebuild", "history", "initial", "explicit_revision"])
async def test_each_production_reader_keeps_staged_authority_out_of_current_and_history(tmp_path, reader):
    from connection_hub.delegated_credentials.cards.persistence import DurableCardPersistence
    from connection_hub.delegated_credentials.cards.resolver import DelegatedCardResolver
    from connection_hub.delegated_credentials.cards.reconcile import CardProjectionReconciler
    from connection_hub.delegated_credentials.cards.service import DelegatedCardService

    store = BundleStorageDelegatedCardStore(tmp_path)
    cards = _pair()
    await _seed(store, cards)
    request = LifecycleRequest.from_mapping(_wire(cards))
    cache = MagicMock()
    cache.read_in_current_run = AsyncMock(return_value=(True, None))
    cache.restore_projection = AsyncMock(return_value=True)
    cache.index_members = AsyncMock(return_value=[])
    cache.index_add = AsyncMock()
    cache.reconcile_projection = AsyncMock(return_value=True)
    service = DelegatedCardService(store=store, cache=cache, mutation_lock=_test_lock)
    resolver = DelegatedCardResolver(cache=cache, store=store)
    persistence = DurableCardPersistence(redis=object(), tenant="fixture", project="fixture",
        card_store=store, mutation_lock=_test_lock, credential_handles=MagicMock())
    persistence._resolver = resolver
    reconciler = CardProjectionReconciler(cache=cache, store=store)

    async def check():
        receipt = await lifecycle_store.read_receipt(store, request.transaction_id(ACTOR))
        for target, expected in zip(request.targets, sorted(cards, key=lambda c: (c.grantor_subject, c.access_id))):
            if reader == "store":
                actual = (await store.read_current_authority(subject_hash=target.subject_hash, access_id=target.access_id))[1]
            elif reader == "service":
                actual = (await service._assert_expected(subject_hash=target.subject_hash, access_id=target.access_id,
                                                         expected_revision=1))[1]
            elif reader == "resolver":
                actual = await resolver.resolve(subject_hash=target.subject_hash, access_id=target.access_id,
                                                now=int(MOMENT.timestamp()))
            elif reader == "list_active":
                actual, = await resolver.list_active(subject_hash=target.subject_hash, now=int(MOMENT.timestamp()))
            elif reader == "list_current":
                actual, = await persistence.list_current(subject_hash=target.subject_hash)
            elif reader == "rebuild":
                await reconciler._repair(target.access_id, 0, expected, int(MOMENT.timestamp()))
                actual = cache.reconcile_projection.call_args.kwargs["authority"]
            elif reader == "history":
                names = await store.list_revision_names(subject_hash=target.subject_hash, access_id=target.access_id)
                assert len(names) == 1
                assert names[0] == next(e["before"]["revision_name"] for e in receipt["targets"] if e["access_id"] == target.access_id)
                continue
            elif reader == "explicit_revision":
                entry = next(e for e in receipt["targets"] if e["access_id"] == target.access_id)
                assert await store.read_revision(subject_hash=target.subject_hash, access_id=target.access_id,
                                                 revision_name=entry["after"]["revision_name"]) is None
                continue
            else:
                actual = await store.read_initial_authority(subject_hash=target.subject_hash, access_id=target.access_id)
            assert actual == expected
        raise LifecycleRefused("fixture_abort_after_reader_check")

    with pytest.raises(LifecycleRefused, match="fixture_abort_after_reader_check"):
        await lifecycle_store.atomic_revoke(store, request=request, actor_subject=ACTOR, now=MOMENT, before_publish=check)
    for target in request.targets:
        assert len(await store.list_revision_names(subject_hash=target.subject_hash, access_id=target.access_id)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["prepared", "1", "2"])
async def test_killed_preparer_blocks_single_card_writer_before_any_cache_or_revision_effect(tmp_path, stage):
    from connection_hub.delegated_credentials.cards.service import CardConflict, DelegatedCardService

    store = BundleStorageDelegatedCardStore(tmp_path)
    cards = _pair()
    await _seed(store, cards)
    body = _wire(cards)
    request = LifecycleRequest.from_mapping(body)
    killed = subprocess.run([sys.executable, "-c", _KILL_CHILD, str(tmp_path), json.dumps(body), stage],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True, timeout=10)
    assert killed.returncode == -signal.SIGKILL, killed.stderr
    cache = MagicMock()
    cache.reconcile_projection = AsyncMock()
    cache.claim_transition = AsyncMock()
    service = DelegatedCardService(store=store, cache=cache, mutation_lock=_test_lock)
    for card in cards:
        with pytest.raises(CardConflict, match="lifecycle_preparation_unresolved"):
            await service.commit(dataclasses.replace(card, card_revision=2),
                subject_hash=subject_hash_for(card.grantor_subject), expected_revision=1)
    cache.reconcile_projection.assert_not_called()
    cache.claim_transition.assert_not_called()
    assert await _states(store, request) == [("active", 1), ("active", 1)]


def _service_cache():
    cache = MagicMock()
    for name in ("reconcile_projection", "claim_lifecycle", "lifecycle_fenced", "finish_lifecycle"):
        setattr(cache, name, AsyncMock(return_value=True))
    cache.release_lifecycle = AsyncMock()
    cache.index_remove = AsyncMock()
    return cache


@pytest.mark.asyncio
async def test_pair_service_holds_receipt_then_both_ordered_card_fences_through_cleanup_and_replay(tmp_path):
    from connection_hub.delegated_credentials.cards.service import DelegatedCardService

    store = BundleStorageDelegatedCardStore(tmp_path, lifecycle_lock_scope="same-host-flock")
    cards = _pair()
    await _seed(store, cards)
    request = LifecycleRequest.from_mapping(_wire(cards))
    held, order, checks, cleanup_calls = [], [], [], []

    @asynccontextmanager
    async def locks(**kwargs):
        held.append(kwargs["lock_path"])
        order.append(kwargs)
        try:
            yield {}
        finally:
            held.pop()

    async def check(authorities):
        assert len(held) == 3
        assert authorities == tuple(sorted(cards, key=lambda c: (c.grantor_subject, c.access_id)))
        checks.append(1)
        return datetime.now(timezone.utc) + timedelta(seconds=30)

    async def cleanup(authorities):
        assert len(held) == 3
        cleanup_calls.append(authorities)

    cache = _service_cache()
    service = DelegatedCardService(store=store, cache=cache, mutation_lock=locks)
    receipt = await service.revoke_lifecycle(request, actor_subject=ACTOR, before_commit=check, after_commit=cleanup)
    assert receipt["state"] == "committed" and receipt["serving_state"] == "complete"
    assert checks == [1, 1] and len(cleanup_calls) == 1
    assert order[0]["operation"] == "delegated-card-lifecycle"
    assert [item["lock_path"] for item in order[1:]] == [store.card_path(subject_hash=t.subject_hash,
        access_id=t.access_id) / ".mutation.lock" for t in sorted(request.targets, key=lambda t: (t.subject_hash, t.access_id))]
    assert all(item["wait_seconds"] == 30 for item in order)
    assert held == []
    assert await service.revoke_lifecycle(request, actor_subject=ACTOR, before_commit=check, after_commit=cleanup) == receipt
    assert checks == [1, 1] and len(cleanup_calls) == 1


@pytest.mark.asyncio
async def test_pair_service_committed_serving_limbo_blocks_both_writers_and_recovers_under_both_fences(tmp_path):
    from connection_hub.delegated_credentials.cards.service import CardConflict, DelegatedCardService

    store = BundleStorageDelegatedCardStore(tmp_path, lifecycle_lock_scope="same-host-flock")
    cards = _pair()
    await _seed(store, cards)
    request = LifecycleRequest.from_mapping(_wire(cards))
    cache = _service_cache()
    service = DelegatedCardService(store=store, cache=cache, mutation_lock=_test_lock)
    check = AsyncMock(return_value=datetime.now(timezone.utc) + timedelta(seconds=30))
    failing_cleanup = AsyncMock(side_effect=OSError("synthetic handle storage unavailable"))
    receipt = await service.revoke_lifecycle(request, actor_subject=ACTOR, before_commit=check, after_commit=failing_cleanup)
    assert receipt["state"] == "committed" and receipt["serving_state"] == "pending"
    assert await _states(store, request) == [("revoked", 2), ("revoked", 2)]
    cache.finish_lifecycle.assert_not_called()
    for card in cards:
        with pytest.raises(CardConflict, match="lifecycle_preparation_unresolved"):
            await service.commit(dataclasses.replace(card, card_revision=3),
                subject_hash=subject_hash_for(card.grantor_subject), expected_revision=2)
    cleanup = AsyncMock()
    completed = await service.revoke_lifecycle(request, actor_subject=ACTOR, before_commit=check, after_commit=cleanup)
    assert completed["state"] == "committed" and completed["serving_state"] == "complete"
    assert check.await_count == 2  # no new decision or mutation on recovery/replay
    assert cleanup.await_count == 1
    await _seed(store, tuple(dataclasses.replace(card, card_revision=3) for card in cards))
    assert await service.revoke_lifecycle(request, actor_subject=ACTOR, before_commit=check, after_commit=cleanup) == completed
    assert cleanup.await_count == 1


@pytest.mark.asyncio
async def test_pair_service_expiry_after_staging_aborts_both_and_does_not_remove_handles(tmp_path):
    from connection_hub.delegated_credentials.cards.service import DelegatedCardService

    store = BundleStorageDelegatedCardStore(tmp_path, lifecycle_lock_scope="same-host-flock")
    cards = _pair()
    await _seed(store, cards)
    request = LifecycleRequest.from_mapping(_wire(cards))
    cache = _service_cache()
    check = AsyncMock(side_effect=[datetime.now(timezone.utc) + timedelta(seconds=30), LifecycleRefused("issuer_decision_expired")])
    cleanup = AsyncMock()
    service = DelegatedCardService(store=store, cache=cache, mutation_lock=_test_lock)
    with pytest.raises(LifecycleRefused, match="issuer_decision_expired"):
        await service.revoke_lifecycle(request, actor_subject=ACTOR, before_commit=check, after_commit=cleanup)
    assert await _states(store, request) == [("active", 1), ("active", 1)]
    receipt = await lifecycle_store.read_receipt(store, request.transaction_id(ACTOR))
    assert receipt["state"] == "refused" and receipt["serving_state"] == "complete"
    cleanup.assert_not_called()
    cache.release_lifecycle.assert_awaited_once()


@pytest.mark.asyncio
async def test_pair_service_requires_explicit_host_flock_capability_and_first_issuer_gate(tmp_path):
    from connection_hub.delegated_credentials.cards.service import DelegatedCardService

    store = BundleStorageDelegatedCardStore(tmp_path)
    cards = _pair()
    await _seed(store, cards)
    request = LifecycleRequest.from_mapping(_wire(cards))
    cache = _service_cache()
    check, cleanup = AsyncMock(), AsyncMock()
    service = DelegatedCardService(store=store, cache=cache, mutation_lock=_test_lock)
    with pytest.raises(LifecycleRefused, match="issuer_lifecycle_atomic_fences_unavailable"):
        await service.revoke_lifecycle(request, actor_subject=ACTOR, before_commit=check, after_commit=cleanup)
    check.assert_not_called()
    store.lifecycle_lock_scope = "same-host-flock"
    check.side_effect = LifecycleRefused("issuer_decision_expired")
    with pytest.raises(LifecycleRefused, match="issuer_decision_expired"):
        await service.revoke_lifecycle(request, actor_subject=ACTOR, before_commit=check, after_commit=cleanup)
    cache.claim_lifecycle.assert_not_called()
    cache.reconcile_projection.assert_not_called()
    assert await lifecycle_store.read_receipt(store, request.transaction_id(ACTOR)) is None


@pytest.mark.asyncio
async def test_cancellation_drains_started_intent_writer_before_releasing_either_fence(tmp_path, monkeypatch):
    from connection_hub.delegated_credentials import durable_io
    from connection_hub.delegated_credentials.cards import service as service_module

    store = BundleStorageDelegatedCardStore(tmp_path, lifecycle_lock_scope="same-host-flock")
    cards = _pair()
    await _seed(store, cards)
    request = LifecycleRequest.from_mapping(_wire(cards))
    held, started, finish = [], threading.Event(), threading.Event()
    original = durable_io._write_text_atomic

    def write(path, payload):
        if path.parent.name == "active":
            started.set()
            assert finish.wait(5), "fixture writer was not released"
        return original(path, payload)

    @asynccontextmanager
    async def locks(**kwargs):
        held.append(kwargs["lock_path"])
        try:
            yield {}
        finally:
            held.pop()

    monkeypatch.setattr(durable_io, "_write_text_atomic", write)
    monkeypatch.setattr(service_module, "CARD_LOCK_WAIT_SECONDS", 0.03)
    service = service_module.DelegatedCardService(store=store, cache=_service_cache(), mutation_lock=locks)
    gate = AsyncMock(return_value=datetime.now(timezone.utc) + timedelta(seconds=30))
    task = asyncio.create_task(service.revoke_lifecycle(request, actor_subject=ACTOR,
                                                       before_commit=gate, after_commit=AsyncMock()))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        await asyncio.sleep(0.08)  # past the shortened fixture progress deadline
        assert not task.done() and len(held) == 3
    finally:
        finish.set()
    with pytest.raises(service_module.CardConflict, match="card_lifecycle_timeout"):
        await task
    assert held == []
    assert await _states(store, request) == [("active", 1), ("active", 1)]
    receipt = await lifecycle_store.read_receipt(store, request.transaction_id(ACTOR))
    assert receipt["state"] == "prepared"
    recovered = await service.revoke_lifecycle(request, actor_subject=ACTOR,
                                              before_commit=gate, after_commit=AsyncMock())
    assert recovered["state"] == "refused" and recovered["serving_state"] == "complete"
    assert await _states(store, request) == [("active", 1), ("active", 1)]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancellation", ["outer_timeout", "external_cancel"])
async def test_outer_cancellation_propagates_after_started_write_drains_and_fences_release(tmp_path, monkeypatch, cancellation):
    from connection_hub.delegated_credentials import durable_io
    from connection_hub.delegated_credentials.cards.service import DelegatedCardService

    store = BundleStorageDelegatedCardStore(tmp_path, lifecycle_lock_scope="same-host-flock")
    cards = _pair()
    await _seed(store, cards)
    request = LifecycleRequest.from_mapping(_wire(cards))
    held, started, finish = [], threading.Event(), threading.Event()
    original = durable_io._write_text_atomic

    def write(path, text):
        if path.parent.name == "active":
            started.set()
            assert finish.wait(5), "fixture writer was not released"
        return original(path, text)

    @asynccontextmanager
    async def locks(**kwargs):
        held.append(kwargs["lock_path"])
        try:
            yield {}
        finally:
            held.pop()

    monkeypatch.setattr(durable_io, "_write_text_atomic", write)
    service = DelegatedCardService(store=store, cache=_service_cache(), mutation_lock=locks)
    timeout = asyncio.timeout(None)

    async def invoke():
        async with timeout:
            return await service.revoke_lifecycle(request, actor_subject=ACTOR,
                before_commit=AsyncMock(return_value=datetime.now(timezone.utc) + timedelta(seconds=30)),
                after_commit=AsyncMock())

    task = asyncio.create_task(invoke())
    try:
        assert await asyncio.to_thread(started.wait, 1)
        if cancellation == "outer_timeout":
            timeout.reschedule(asyncio.get_running_loop().time() + 0.02)
        else:
            task.cancel("fixture-original-cancellation")
        await asyncio.sleep(0.08)
        assert not task.done() and len(held) == 3
    finally:
        finish.set()
    if cancellation == "outer_timeout":
        with pytest.raises(TimeoutError):
            await task
        assert timeout.expired() and not task.cancelled() and task.cancelling() == 0
    else:
        with pytest.raises(asyncio.CancelledError, match="fixture-original-cancellation"):
            await task
        assert task.cancelled() and not timeout.expired()
    assert held == []
    assert await _states(store, request) == [("active", 1), ("active", 1)]
    assert (await lifecycle_store.read_receipt(store, request.transaction_id(ACTOR)))["state"] == "prepared"


@pytest.mark.asyncio
async def test_expiry_in_commit_write_thread_is_checked_after_temp_write_before_rename(tmp_path, monkeypatch):
    from connection_hub.delegated_credentials.cards.service import DelegatedCardService
    from pathlib import Path

    store = BundleStorageDelegatedCardStore(tmp_path, lifecycle_lock_scope="same-host-flock")
    cards = _pair()
    await _seed(store, cards)
    request = LifecycleRequest.from_mapping(_wire(cards))
    transaction_id = request.transaction_id(ACTOR)
    original = Path.write_text
    commit_writer_reached = threading.Event()

    def write(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path.parent.name == "lifecycle-transactions" and path.name.startswith(f".{transaction_id}.json.tmp."):
            commit_writer_reached.set()
            time.sleep(1.1)  # only this fixture-owned IO thread, not platform IO
        return result

    monkeypatch.setattr(Path, "write_text", write)
    cache = _service_cache()
    cleanup = AsyncMock()
    service = DelegatedCardService(store=store, cache=cache, mutation_lock=_test_lock)
    deadline = datetime.now(timezone.utc) + timedelta(seconds=1)
    with pytest.raises(LifecycleRefused, match="issuer_decision_expired"):
        await service.revoke_lifecycle(request, actor_subject=ACTOR,
            before_commit=AsyncMock(return_value=deadline), after_commit=cleanup)
    assert commit_writer_reached.is_set()
    assert await _states(store, request) == [("active", 1), ("active", 1)]
    receipt = await lifecycle_store.read_receipt(store, transaction_id)
    assert receipt["state"] == "refused" and receipt["serving_state"] == "complete"
    cleanup.assert_not_called()


@pytest.mark.asyncio
async def test_unsupported_object_backend_refuses_without_manufacturing_local_storage():
    store = BundleStorageDelegatedCardStore("s3://synthetic-bucket/synthetic-root")
    assert store.lifecycle_publish_backend == ""
    with pytest.raises(LifecycleRefused, match="issuer_lifecycle_atomic_backend_unavailable"):
        await lifecycle_store.atomic_revoke(store, request=LifecycleRequest.from_mapping(_wire(_pair())),
                                            actor_subject=ACTOR, now=MOMENT, before_publish=_allow)


@pytest.mark.asyncio
async def test_both_staged_pointers_still_read_before_one_visibility_point(tmp_path, monkeypatch):
    store = BundleStorageDelegatedCardStore(tmp_path)
    cards = _pair()
    await _seed(store, cards)
    request = LifecycleRequest.from_mapping(_wire(cards))
    observations = []
    original = lifecycle_store.write_json_atomic

    async def write(path, payload):
        await original(path, payload)
        if payload.get("schema") == lifecycle_store.LIFECYCLE_POINTER_SCHEMA:
            observations.append(await _states(BundleStorageDelegatedCardStore(tmp_path), request))
            with pytest.raises(CardStorageError, match="lifecycle_preparation_unresolved"):
                await store.advance_current(subject_hash=request.targets[0].subject_hash,
                                             pointer=await store.read_current(subject_hash=request.targets[0].subject_hash,
                                                                              access_id=request.targets[0].access_id))

    monkeypatch.setattr(lifecycle_store, "write_json_atomic", write)
    result = await lifecycle_store.atomic_revoke(store, request=request, actor_subject=ACTOR, now=MOMENT, before_publish=_allow)
    assert observations == [[("active", 1), ("active", 1)]] * 2
    assert result["state"] == "committed"
    assert await _states(store, request) == [("revoked", 2), ("revoked", 2)]


@pytest.mark.asyncio
async def test_failing_second_pointer_never_commits_first_authority(tmp_path, monkeypatch):
    store = BundleStorageDelegatedCardStore(tmp_path)
    cards = _pair()
    await _seed(store, cards)
    request = LifecycleRequest.from_mapping(_wire(cards))
    original = lifecycle_store.write_json_atomic
    staged = 0

    async def write(path, payload):
        nonlocal staged
        if payload.get("schema") == lifecycle_store.LIFECYCLE_POINTER_SCHEMA:
            staged += 1
            if staged == 2:
                raise OSError("synthetic second-pointer failure")
        await original(path, payload)

    monkeypatch.setattr(lifecycle_store, "write_json_atomic", write)
    with pytest.raises(OSError):
        await lifecycle_store.atomic_revoke(store, request=request, actor_subject=ACTOR, now=MOMENT, before_publish=_allow)
    assert await _states(store, request) == [("active", 1), ("active", 1)]
    receipt = await lifecycle_store.read_receipt(store, request.transaction_id(ACTOR))
    assert receipt["state"] == "refused"


@pytest.mark.asyncio
async def test_expired_final_gate_leaves_both_authorities_unchanged(tmp_path):
    store = BundleStorageDelegatedCardStore(tmp_path)
    cards = _pair()
    await _seed(store, cards)
    request = LifecycleRequest.from_mapping(_wire(cards))

    async def refuse():
        raise LifecycleRefused("issuer_decision_expired")

    with pytest.raises(LifecycleRefused, match="issuer_decision_expired"):
        await lifecycle_store.atomic_revoke(store, request=request, actor_subject=ACTOR, now=MOMENT, before_publish=refuse)
    assert await _states(store, request) == [("active", 1), ("active", 1)]


@pytest.mark.asyncio
async def test_io_error_after_commit_rename_is_not_reported_as_no_write(tmp_path, monkeypatch):
    store = BundleStorageDelegatedCardStore(tmp_path)
    cards = _pair()
    await _seed(store, cards)
    request = LifecycleRequest.from_mapping(_wire(cards))
    original = lifecycle_store.write_json_atomic

    async def write(path, payload):
        await original(path, payload)
        if payload.get("schema") == lifecycle_store.LIFECYCLE_RECEIPT_SCHEMA and payload.get("state") == "committed":
            raise OSError("synthetic failure after the commit rename")

    monkeypatch.setattr(lifecycle_store, "write_json_atomic", write)
    result = await lifecycle_store.atomic_revoke(store, request=request, actor_subject=ACTOR, now=MOMENT, before_publish=_allow)
    assert result["state"] == "committed"
    assert await _states(store, request) == [("revoked", 2), ("revoked", 2)]


@pytest.mark.asyncio
async def test_corrupted_shared_binding_fails_closed_for_both_pointers(tmp_path):
    store = BundleStorageDelegatedCardStore(tmp_path)
    cards = _pair()
    await _seed(store, cards)
    request = LifecycleRequest.from_mapping(_wire(cards))
    result = await lifecycle_store.atomic_revoke(store, request=request, actor_subject=ACTOR, now=MOMENT, before_publish=_allow)
    result["binding"]["actor_subject"] = "changed-actor"
    await lifecycle_store.write_json_atomic(lifecycle_store.receipt_path(store, request.transaction_id(ACTOR)), result)
    for target in request.targets:
        with pytest.raises(CardStorageError, match="lifecycle_receipt_invalid"):
            await store.read_current_authority(subject_hash=target.subject_hash, access_id=target.access_id)


@pytest.mark.asyncio
async def test_identical_replay_returns_receipt_without_reapplying_or_using_new_authority(tmp_path):
    store = BundleStorageDelegatedCardStore(tmp_path)
    cards = _pair()
    await _seed(store, cards)
    request = LifecycleRequest.from_mapping(_wire(cards))
    result = await lifecycle_store.atomic_revoke(store, request=request, actor_subject=ACTOR, now=MOMENT, before_publish=_allow)
    next_cards = tuple(dataclasses.replace(card, card_revision=3, label="later legitimate authority") for card in cards)
    await _seed(store, next_cards)

    async def must_not_run():
        raise AssertionError("a receipt replay is not another mutation")

    assert await lifecycle_store.atomic_revoke(store, request=request, actor_subject=ACTOR, now=MOMENT, before_publish=must_not_run) == result
    assert await _states(store, request) == [("active", 3), ("active", 3)]


@pytest.mark.asyncio
async def test_changed_replay_same_key_is_refused(tmp_path):
    store = BundleStorageDelegatedCardStore(tmp_path)
    cards = _pair()
    await _seed(store, cards)
    request = LifecycleRequest.from_mapping(_wire(cards))
    await lifecycle_store.atomic_revoke(store, request=request, actor_subject=ACTOR, now=MOMENT, before_publish=_allow)
    body = _wire(cards)
    body["targets"][1]["expected_authority_fingerprint"] = "f" * 64
    body["change_digest"] = change_digest({"action": "revoke", "targets": body["targets"]})
    changed = LifecycleRequest.from_mapping(body)
    assert changed.transaction_id(ACTOR) == request.transaction_id(ACTOR)
    with pytest.raises(LifecycleRefused, match="issuer_lifecycle_replay_changed"):
        await lifecycle_store.atomic_revoke(store, request=changed, actor_subject=ACTOR, now=MOMENT, before_publish=_allow)
    assert await _states(store, request) == [("revoked", 2), ("revoked", 2)]


@pytest.mark.asyncio
async def test_marker_rebinding_fingerprint_is_checked_before_intent_or_first_effect(tmp_path):
    store = BundleStorageDelegatedCardStore(tmp_path)
    cards = _pair()
    request = LifecycleRequest.from_mapping(_wire(cards))
    actual = (cards[0], dataclasses.replace(cards[1], properties={"opaque.identity-marker": {"paired-with": "different-card"}}))
    await _seed(store, actual)
    with pytest.raises(LifecycleRefused, match="issuer_lifecycle_fingerprint_moved"):
        await lifecycle_store.atomic_revoke(store, request=request, actor_subject=ACTOR, now=MOMENT, before_publish=_allow)
    assert await lifecycle_store.read_receipt(store, request.transaction_id(ACTOR)) is None
    assert await _states(store, request) == [("active", 1), ("active", 1)]


@pytest.mark.asyncio
async def test_missing_second_target_leaves_first_unchanged(tmp_path):
    store = BundleStorageDelegatedCardStore(tmp_path)
    cards = _pair()
    await _seed(store, cards[:1])
    request = LifecycleRequest.from_mapping(_wire(cards))
    with pytest.raises(LifecycleRefused, match="issuer_lifecycle_target_missing"):
        await lifecycle_store.atomic_revoke(store, request=request, actor_subject=ACTOR, now=MOMENT, before_publish=_allow)
    assert await lifecycle_store.read_receipt(store, request.transaction_id(ACTOR)) is None
    assert await _states(store, request) == [("active", 1), None]


@pytest.mark.asyncio
async def test_missing_receipt_never_falls_back_to_embedded_before_or_after(tmp_path, monkeypatch):
    store = BundleStorageDelegatedCardStore(tmp_path)
    cards = _pair()
    await _seed(store, cards)
    request = LifecycleRequest.from_mapping(_wire(cards))
    await lifecycle_store.atomic_revoke(store, request=request, actor_subject=ACTOR, now=MOMENT, before_publish=_allow)

    async def missing(*args, **kwargs):
        return None

    monkeypatch.setattr(lifecycle_store, "read_receipt", missing)
    with pytest.raises(CardStorageError, match="lifecycle_receipt_missing"):
        await _states(store, request)


@pytest.mark.parametrize("revision", [True, 1.0, "1", 0, -1, None])
def test_request_does_not_coerce_revision(revision):
    body = _wire(_pair())
    body["targets"][0]["expected_card_revision"] = revision
    with pytest.raises(LifecycleRefused, match="issuer_lifecycle_revision_invalid"):
        LifecycleRequest.from_mapping(body)


@pytest.mark.parametrize("field", ["actor_subject", "user_id", "viewer", "approval_hash"])
def test_request_cannot_supply_actor_or_self_authenticating_approval(field):
    body = _wire(_pair())
    body[field] = "forged"
    with pytest.raises(LifecycleRefused, match="issuer_lifecycle_request_invalid"):
        LifecycleRequest.from_mapping(body)


_KILL_CHILD = r'''
import asyncio, json, os, signal, sys
from datetime import datetime, timezone
from connection_hub.delegated_credentials.cards import lifecycle_store as module
from connection_hub.delegated_credentials.cards.lifecycle import LifecycleRequest
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore
store = BundleStorageDelegatedCardStore(sys.argv[1])
request = LifecycleRequest.from_mapping(json.loads(sys.argv[2]))
original = module.write_json_atomic
stage = sys.argv[3]
count = 0
async def write(path, payload):
    global count
    await original(path, payload)
    if payload.get('schema') == module.LIFECYCLE_POINTER_SCHEMA:
        count += 1
        if stage == str(count):
            os.kill(os.getpid(), signal.SIGKILL)
    if payload.get('schema') == module.LIFECYCLE_RECEIPT_SCHEMA:
        if stage == payload.get('state'):
            os.kill(os.getpid(), signal.SIGKILL)
module.write_json_atomic = write
async def allow(): pass
asyncio.run(module.atomic_revoke(store, request=request, actor_subject='authenticated-human-admin',
    now=datetime(2026,10,6,tzinfo=timezone.utc), before_publish=allow))
'''

_RECOVER_CHILD = r'''
import asyncio, json, sys
from connection_hub.delegated_credentials.cards import lifecycle_store as module
from connection_hub.delegated_credentials.cards.lifecycle import LifecycleRequest
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore
store = BundleStorageDelegatedCardStore(sys.argv[1])
request = LifecycleRequest.from_mapping(json.loads(sys.argv[2]))
async def run():
    async def states():
        values = []
        for t in request.targets:
            _, card = await store.read_current_authority(subject_hash=t.subject_hash, access_id=t.access_id)
            values.append([card.state, card.card_revision])
        return values
    before = await states()
    first = await module.abort_prepared(store, request=request, actor_subject='authenticated-human-admin')
    second = await module.abort_prepared(store, request=request, actor_subject='authenticated-human-admin')
    print(json.dumps({'before': before, 'after': await states(), 'state': first['state'], 'idempotent': first == second}))
asyncio.run(run())
'''


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["prepared", "1", "2", "committed"])
async def test_killed_disposable_writer_and_fresh_process_recovery(tmp_path, stage):
    # Only these fixture-owned subprocesses are killed. No running client,
    # production state, service, credentials or platform process is touched.
    store = BundleStorageDelegatedCardStore(tmp_path)
    cards = _pair()
    await _seed(store, cards)
    body = json.dumps(_wire(cards))
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    killed = subprocess.run([sys.executable, "-c", _KILL_CHILD, str(tmp_path), body, stage],
                            env=environment, capture_output=True, text=True, timeout=10)
    assert killed.returncode == -signal.SIGKILL, killed.stderr
    recovered = subprocess.run([sys.executable, "-c", _RECOVER_CHILD, str(tmp_path), body],
                               env=environment, capture_output=True, text=True, timeout=10, check=True)
    proof = json.loads(recovered.stdout)
    expected = [["revoked", 2], ["revoked", 2]] if stage == "committed" else [["active", 1], ["active", 1]]
    assert proof["before"] == proof["after"] == expected
    assert proof["state"] == ("committed" if stage == "committed" else "refused")
    assert proof["idempotent"] is True

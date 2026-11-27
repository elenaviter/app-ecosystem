"""The conversation slice store: guarded categories only, exclusive revisions, digest paths."""

from __future__ import annotations

import asyncio

import pytest

from connection_hub.delegated_credentials.slices.store import (
    BundleStorageConversationSliceStore, ConversationSlice, SliceConflict, SliceStoreError,
    conversation_key,
)

NAMED = "https://example.test/mcp/named-services"


def _slice(**changes):
    base = dict(grantor_subject="user-1", card_access_id="agent-user-1", conversation_id="conv/../1",
                card_generation=100, base_card_revision=4,
                selection={"named_service_operations": {NAMED: {"slack": ["object.list"]}}})
    return ConversationSlice(**{**base, **changes})


@pytest.mark.asyncio
async def test_a_slice_commits_revisions_in_order_and_reads_the_newest(tmp_path):
    store = BundleStorageConversationSliceStore(tmp_path)
    first = await store.write(_slice(), expected_revision=0)
    second = await store.write(_slice(selection={}), expected_revision=1)

    assert (first.slice_revision, second.slice_revision) == (1, 2)
    current = await store.read(grantor_subject="user-1", card_access_id="agent-user-1", conversation_id="conv/../1")
    assert current == second
    # The client-chosen id never reaches a path.
    assert not any(".." in part for path in tmp_path.rglob("*") for part in path.relative_to(tmp_path).parts)
    assert any(conversation_key("conv/../1") in str(path) for path in tmp_path.rglob("*"))


@pytest.mark.asyncio
async def test_two_writers_expecting_the_same_revision_commit_once(tmp_path):
    store = BundleStorageConversationSliceStore(tmp_path)
    await store.write(_slice(), expected_revision=0)

    results = await asyncio.gather(store.write(_slice(selection={}), expected_revision=1),
                                   store.write(_slice(selection={}), expected_revision=1), return_exceptions=True)

    assert sum(isinstance(result, ConversationSlice) for result in results) == 1
    assert sum(isinstance(result, SliceConflict) for result in results) == 1


@pytest.mark.asyncio
async def test_a_stale_writer_is_refused(tmp_path):
    store = BundleStorageConversationSliceStore(tmp_path)
    await store.write(_slice(), expected_revision=0)
    with pytest.raises(SliceConflict) as caught:
        await store.write(_slice(), expected_revision=0)
    assert caught.value.current_revision == 1


def test_a_slice_never_holds_what_kdcube_carries_out():
    for category in ("model", "instructions", "skills", "presentation", "cache_policy"):
        with pytest.raises(SliceStoreError, match="conversation_slice_category_not_guarded"):
            _slice(selection={category: ["x"]})


def _scope():
    return dict(grantor_subject="user-1", card_access_id="agent-user-1", conversation_id="conv/../1")


@pytest.mark.asyncio
async def test_two_writers_that_both_passed_the_revision_check_still_commit_once(tmp_path, monkeypatch):
    """With no lock (or one that let both in), the exclusive claim is the compare-and-set."""
    store = BundleStorageConversationSliceStore(tmp_path)
    await store.write(_slice(), expected_revision=0)
    real = store._revisions
    seen = {"n": 0}

    async def stale_listing(directory):
        seen["n"] += 1
        return [1]  # both writers see revision 1 as current, even after the first commits revision 2

    monkeypatch.setattr(store, "_revisions", stale_listing)
    first = await store.write(_slice(selection={}), expected_revision=1)
    with pytest.raises(SliceConflict):
        await store.write(_slice(selection={"resource_families": ["mcp"]}), expected_revision=1)
    monkeypatch.setattr(store, "_revisions", real)
    assert await store.read(**_scope()) == first


@pytest.mark.asyncio
async def test_a_crash_between_the_claim_and_the_revision_leaves_the_slice_readable(tmp_path, monkeypatch):
    """A claimed revision that was never written is invisible; once stale, the next write takes it over."""
    from connection_hub.delegated_credentials.slices import store as module

    store = BundleStorageConversationSliceStore(tmp_path)
    first = await store.write(_slice(), expected_revision=0)

    def crash(*args, **kwargs):
        raise OSError("disk went away")

    monkeypatch.setattr(module, "_write_durable", crash)
    with pytest.raises(OSError):
        await store.write(_slice(selection={}), expected_revision=1)
    monkeypatch.undo()
    assert await store.read(**_scope()) == first
    # A fresh claim refuses: its writer may still be writing.
    with pytest.raises(SliceConflict):
        await store.write(_slice(selection={}), expected_revision=1)
    monkeypatch.setattr(module, "STALE_CLAIM_SECONDS", -1.0)
    assert (await store.write(_slice(selection={}), expected_revision=1)).slice_revision == 2
    assert not [path for path in tmp_path.rglob("*.tmp")]


@pytest.mark.asyncio
async def test_a_stage_refuses_while_a_revision_is_claimed_but_not_written(tmp_path, monkeypatch):
    from connection_hub.delegated_credentials.slices import store as module

    store = BundleStorageConversationSliceStore(tmp_path)
    await store.write(_slice(), expected_revision=0)

    def crash(*args, **kwargs):
        raise OSError("disk went away")

    monkeypatch.setattr(module, "_write_durable", crash)
    with pytest.raises(OSError):
        await store.write(_slice(selection={}), expected_revision=1)
    monkeypatch.undo()
    with pytest.raises(SliceConflict):
        await store.prepare(_slice(selection={}), expected_revision=1, change_id="txn-1:slice")
    assert not await store.prepared_changes(**_scope())


@pytest.mark.asyncio
async def test_every_write_runs_in_the_slice_section(tmp_path):
    from contextlib import asynccontextmanager

    from connection_hub.delegated_credentials.cards.store import subject_hash_for

    entered = []

    @asynccontextmanager
    async def section(subject_hash, access_id, conversation):
        entered.append((subject_hash, access_id, conversation))
        yield

    store = BundleStorageConversationSliceStore(tmp_path, section=section)
    await store.write(_slice(), expected_revision=0)
    await store.prepare(_slice(selection={}), expected_revision=1, change_id="txn-1:slice")
    await store.commit_prepared(_slice(selection={}), change_id="txn-1:slice")
    await store.prepare(_slice(), expected_revision=2, change_id="txn-2:slice")
    await store.release(_slice(), change_id="txn-2:slice")
    key = (subject_hash_for("user-1"), "agent-user-1", conversation_key("conv/../1"))
    assert entered == [key] * 5


@pytest.mark.asyncio
async def test_a_card_section_is_refused_inside_a_slice_section(tmp_path):
    """The slice section is the innermost one: taking a Card section inside it would invert the order."""
    from contextlib import asynccontextmanager

    from connection_hub.delegated_credentials.cards.locks import row_fenced_lock
    from connection_hub.delegated_credentials.cards.store import CardStorageError

    @asynccontextmanager
    async def base(**_kwargs):
        yield None

    lock = row_fenced_lock(base)
    async with lock(lock_path=tmp_path / "c.lock", resource_id="delegated-card:agent-1", operation="t",
                    wait_seconds=1):
        async with lock(lock_path=tmp_path / "s.lock", resource_id="conversation-slice:h:agent-1:k",
                        operation="t", wait_seconds=1):
            with pytest.raises(CardStorageError, match="card_lock_order_violation"):
                async with lock(lock_path=tmp_path / "c2.lock", resource_id="delegated-card:agent-2",
                                operation="t", wait_seconds=1):
                    pass


def test_the_slice_key_is_its_own_redis_lock_kind():
    from connection_hub.delegated_credentials.cards.redis_lock import card_lock_key

    assert card_lock_key(tenant="t", project="p", resource_id="conversation-slice:h:agent-1:k") == (
        "kdcube:cards:lock:t:p:slice:h:agent-1:k")

"""W40: the conversation slice store: guarded categories only, exclusive revisions, digest paths."""

from __future__ import annotations

import asyncio

import pytest

from connection_hub.delegated_credentials.slices.store import (
    BundleStorageConversationSliceStore, ConversationSlice, SLICE_STATE_DELETING, SliceConflict, SliceStoreError,
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
        with pytest.raises(SliceStoreError, match="conversation_slice_category_not_guarded"):
            _slice(pending={category: ["x"]})


@pytest.mark.asyncio
async def test_a_deleting_tombstone_is_a_revision_and_removal_clears_the_slice(tmp_path):
    store = BundleStorageConversationSliceStore(tmp_path)
    await store.write(_slice(), expected_revision=0)
    marked = await store.write(_slice(state=SLICE_STATE_DELETING), expected_revision=1)
    assert marked.state == SLICE_STATE_DELETING

    assert await store.remove(grantor_subject="user-1", card_access_id="agent-user-1", conversation_id="conv/../1") == 2
    assert await store.read(grantor_subject="user-1", card_access_id="agent-user-1", conversation_id="conv/../1") is None

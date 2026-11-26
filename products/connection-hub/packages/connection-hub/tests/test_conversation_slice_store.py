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
async def test_a_transaction_that_prepares_inside_a_picker_write_still_commits_and_leaves_no_marker(tmp_path):
    """The picker write and a consent's STAGE overlap; the write withdraws, the commit lands."""
    from connection_hub.delegated_credentials.slices.store import SlicePrepared

    store = BundleStorageConversationSliceStore(tmp_path)
    await store.write(_slice(), expected_revision=0)
    consent = _slice(selection={"named_service_operations": {NAMED: {"slack": ["object.list", "object.post"]}}})
    original = store._revisions
    calls = {"n": 0}

    async def revisions_with_a_stage_in_between(directory):
        calls["n"] += 1
        if calls["n"] == 1:  # the picker write passed its marker check; the STAGE runs now
            await store.prepare(consent, expected_revision=1, change_id="txn-1")
        return await original(directory)

    store._revisions = revisions_with_a_stage_in_between
    with pytest.raises(SlicePrepared):
        await store.write(_slice(selection={}), expected_revision=1)
    store._revisions = original

    committed = await store.commit_prepared(consent, change_id="txn-1")
    assert committed.slice_revision == 2 and committed.selection == consent.selection
    assert await store.prepared_changes(**_scope()) == []
    assert (await store.read(**_scope())).selection == consent.selection


@pytest.mark.asyncio
async def test_a_stage_that_finds_the_slice_moved_after_its_marker_withdraws_it(tmp_path):
    store = BundleStorageConversationSliceStore(tmp_path)
    await store.write(_slice(), expected_revision=0)
    original = store._revisions
    calls = {"n": 0}

    async def revisions_moved_after_the_marker(directory):
        calls["n"] += 1
        if calls["n"] == 2:  # the marker is published; a write that passed its check earlier lands now
            store._revisions = original
            await BundleStorageConversationSliceStore(tmp_path)._write_unchecked_for_test(_slice(selection={}), 1)
        return await original(directory)

    async def _write_unchecked(self, candidate, expected):
        from connection_hub.delegated_credentials.slices import store as module
        directory = self._directory(candidate.grantor_subject, candidate.card_access_id, candidate.conversation_id)
        written = ConversationSlice(**{**candidate.__dict__, "slice_revision": expected + 1})
        assert module._create_exclusive(directory / f"slice_revision_{expected + 1:08d}.json", written.to_document())

    BundleStorageConversationSliceStore._write_unchecked_for_test = _write_unchecked
    try:
        store._revisions = revisions_moved_after_the_marker
        with pytest.raises(SliceConflict):
            await store.prepare(_slice(), expected_revision=1, change_id="txn-2")
    finally:
        del BundleStorageConversationSliceStore._write_unchecked_for_test
    assert await store.prepared_changes(**_scope()) == []


@pytest.mark.asyncio
async def test_a_crash_while_writing_a_revision_leaves_the_slice_readable(tmp_path, monkeypatch):
    """The bytes are published whole or not at all; a partial revision never becomes the current one."""
    from connection_hub.delegated_credentials.slices import store as module

    store = BundleStorageConversationSliceStore(tmp_path)
    first = await store.write(_slice(), expected_revision=0)

    def crash(*args, **kwargs):
        raise OSError("disk went away")

    monkeypatch.setattr(module.os, "link", crash)
    with pytest.raises(OSError):
        await store.write(_slice(selection={}), expected_revision=1)
    monkeypatch.undo()
    assert await store.read(**_scope()) == first
    assert not [path for path in tmp_path.rglob("*.tmp")]
    assert (await store.write(_slice(selection={}), expected_revision=1)).slice_revision == 2

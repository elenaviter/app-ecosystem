"""W578: a disconnect's account fence and every writer that adds that account, on the real Card store.

The lock is an ``fcntl.flock`` per Card lock file (the production lock's
mechanism, without KDCube's process registry), so a held section is observed
exactly as another instance would observe it. Cross-process interleavings run
in the app's real-backend suite.
"""

from __future__ import annotations

import asyncio
import fcntl
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from connection_hub.delegated_credentials.cards import account_fence as fence
from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.service import (
    CardConflict, CardMutationLockTimeout, DelegatedCardService,
)
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore
from connection_hub.delegated_credentials.cards.transaction_store import CardTransactionRefused
from test_card_service import NOW, SUBJECT_HASH, _Cache, _authority
from test_card_transaction_store import Decisions

ACCOUNT = ("google", "acct-1")
DISCONNECT = "d" * 64
OTHER = "e" * 64
INTENT = "b" * 64


@asynccontextmanager
async def flock_section(*, lock_path, wait_seconds=None, **_):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + (wait_seconds if wait_seconds is not None else 30.0)
    with open(lock_path, "a+") as handle:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if loop.time() >= deadline:
                    raise CardMutationLockTimeout(str(lock_path)) from None
                await asyncio.sleep(0.02)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


async def _world(tmp_path):
    store = BundleStorageDelegatedCardStore(tmp_path)
    tx.bind_transaction_decisions(store, Decisions())
    service = DelegatedCardService(store=store, cache=_Cache(), mutation_lock=flock_section)
    card = _authority()
    await service.commit(card, subject_hash=SUBJECT_HASH, expected_revision=0, now=NOW)
    return store, service, card


def _binding(card):
    return replace(card, card_revision=card.card_revision + 1,
                   account_scope={ACCOUNT[0]: {ACCOUNT[1]: ("mail.read",)}})


async def _fence(store, transaction_id=DISCONNECT):
    """A disconnect's fence written as reserve_accounts writes it, but stopped before its scan."""
    await fence.write_json_atomic(fence._index_path(store, transaction_id),
                                  {"transaction_id": transaction_id, "accounts": [list(ACCOUNT)]})
    await fence.write_json_atomic(fence._dir(store, *ACCOUNT) / "fence.json", {"transaction_id": transaction_id})


@pytest.mark.asyncio
async def test_a_fresh_fence_with_no_receipt_yet_blocks_a_new_binding(tmp_path):
    """claude-main's interleaving: the writer lands between the disconnect's fence write and its prepare."""
    store, service, card = await _world(tmp_path)
    await _fence(store)
    with pytest.raises(CardConflict, match="card_account_reserved"):
        await service.commit(_binding(card), subject_hash=SUBJECT_HASH, expected_revision=card.card_revision,
                             now=NOW)
    assert not list((fence._dir(store, *ACCOUNT) / "pending").glob("*.json"))  # its mark was withdrawn
    current = await store.read_current_authority(subject_hash=SUBJECT_HASH, access_id=card.access_id)
    assert current[1] == card


@pytest.mark.asyncio
async def test_a_fence_of_an_aborted_disconnect_no_longer_blocks(tmp_path):
    store, service, card = await _world(tmp_path)
    await _fence(store)
    await tx.abort_unstaged(store, DISCONNECT, intent_digest=INTENT)
    await service.commit(_binding(card), subject_hash=SUBJECT_HASH, expected_revision=card.card_revision, now=NOW)


@pytest.mark.asyncio
async def test_a_write_that_adds_no_account_is_never_fenced(tmp_path):
    store, service, card = await _world(tmp_path)
    await _fence(store)
    await service.commit(replace(card, card_revision=card.card_revision + 1, label="renamed"),
                         subject_hash=SUBJECT_HASH, expected_revision=card.card_revision, now=NOW)


@pytest.mark.asyncio
async def test_the_disconnect_refuses_while_a_direct_writer_holds_its_section_and_its_mark(tmp_path):
    store, service, card = await _world(tmp_path)
    lock_path = service._lock_path(subject_hash=SUBJECT_HASH, access_id=card.access_id)
    async with flock_section(lock_path=lock_path):
        await fence.mark_binding(store, [ACCOUNT], mark_id="direct-live", kind="direct",
                                 subject_hash=SUBJECT_HASH, access_id=card.access_id)
        with pytest.raises(CardTransactionRefused, match="card_account_binding_in_progress"):
            await service.reserve_accounts([ACCOUNT], transaction_id=DISCONNECT)
    assert not (fence._dir(store, *ACCOUNT) / "fence.json").exists()  # refused: its fence is released


@pytest.mark.asyncio
async def test_a_direct_mark_whose_section_ended_is_removed_and_the_disconnect_proceeds(tmp_path):
    """A writer that died after its mark: the free section and a durable reread prove nothing is in flight."""
    store, service, card = await _world(tmp_path)
    await fence.mark_binding(store, [ACCOUNT], mark_id="direct-dead", kind="direct",
                             subject_hash=SUBJECT_HASH, access_id=card.access_id)
    await service.reserve_accounts([ACCOUNT], transaction_id=DISCONNECT)
    assert not (fence._dir(store, *ACCOUNT) / "pending" / "direct-dead.json").exists()
    assert (await fence.read_json_or_none(fence._dir(store, *ACCOUNT) / "fence.json")) == {
        "transaction_id": DISCONNECT}


@pytest.mark.asyncio
async def test_a_staged_binding_blocks_the_disconnect_until_its_own_decision(tmp_path):
    store, service, card = await _world(tmp_path)
    from datetime import datetime, timezone
    when = datetime.fromtimestamp(NOW, timezone.utc) if not isinstance(NOW, datetime) else NOW
    await service.stage_transaction(transaction_id=OTHER, intent_digest=INTENT, participant="project",
                                    subject_hash=SUBJECT_HASH, original=card, candidate=_binding(card), now=when)
    with pytest.raises(CardTransactionRefused, match="card_account_binding_in_progress"):
        await service.reserve_accounts([ACCOUNT], transaction_id=DISCONNECT)
    store._card_transaction_decisions.recorded[OTHER] = "committed"
    await service.decide_transaction(transaction_id=OTHER, intent_digest=INTENT, decision="committed",
                                     subject_hash=SUBJECT_HASH, access_id=card.access_id)
    # Decided: the binding is live (and will be listed); the mark is gone and the fence can be taken.
    await service.reserve_accounts([ACCOUNT], transaction_id=DISCONNECT)


@pytest.mark.asyncio
async def test_a_second_disconnect_of_the_same_account_is_refused_and_release_is_exact(tmp_path):
    store, service, card = await _world(tmp_path)
    await service.reserve_accounts([ACCOUNT], transaction_id=DISCONNECT)
    with pytest.raises(CardTransactionRefused, match="card_account_reserved"):
        await service.reserve_accounts([ACCOUNT], transaction_id=OTHER)
    await service.release_accounts(OTHER)  # another transaction's release clears nothing
    assert (fence._dir(store, *ACCOUNT) / "fence.json").exists()
    await service.release_accounts(DISCONNECT)
    assert not (fence._dir(store, *ACCOUNT) / "fence.json").exists()


@pytest.mark.asyncio
async def test_presumed_abort_releases_a_never_prepared_disconnects_fence(tmp_path):
    store, service, card = await _world(tmp_path)
    await service.reserve_accounts([ACCOUNT], transaction_id=DISCONNECT)
    await service.abort_unstaged_transaction(transaction_id=DISCONNECT, subject_hash=SUBJECT_HASH,
                                             access_id=card.access_id, intent_digest=INTENT)
    assert not (fence._dir(store, *ACCOUNT) / "fence.json").exists()
    await service.commit(_binding(card), subject_hash=SUBJECT_HASH, expected_revision=card.card_revision, now=NOW)

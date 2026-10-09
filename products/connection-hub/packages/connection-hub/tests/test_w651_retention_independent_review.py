"""Independent W651 retention regressions: retryability, scan progress and late sealing.

These assertions intentionally fail on AE 35972d4e. They use only synthetic
Cards and the actual filesystem collection store; no live credentials or runtime.
"""

from pathlib import Path

import pytest

from connection_hub.delegated_credentials.cards import card_read_collection as collections
from connection_hub.delegated_credentials.cards.card_read_collection_operation import collection_id_for
from test_card_transaction_store import _setup
from test_w502_card_read_collection import COLLECTION, _reads, _seal
from test_w502_card_read_collection_register import DEADLINE as REQUEST_DEADLINE
from test_w502_card_read_collection_register import PEER, PROJECT, _request, _verified, _world
from test_w651_collection_retention import DEADLINE


@pytest.mark.asyncio
async def test_a_failed_leaf_unlink_keeps_the_header_and_can_be_retried(tmp_path, monkeypatch):
    store, service, before, _ = await _setup(tmp_path)
    await _seal(store, before)
    leaf = next((store.root / "card-collections" / COLLECTION / "leaves").iterdir())
    original = Path.unlink

    def fail_one(self, *args, **kwargs):
        if self == leaf:
            raise PermissionError("synthetic one-leaf failure")
        return original(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_one)
        try:
            await service.sweep_expired_collections(now=DEADLINE)
        except OSError:
            pass  # a retryable failure is also acceptable, but the deadline must survive
    assert await collections.load_header(store, COLLECTION) is not None
    await service.sweep_expired_collections(now=DEADLINE)
    assert not (store.root / "card-collections" / COLLECTION).exists()


@pytest.mark.asyncio
async def test_bounded_sweeps_eventually_reach_expired_entries_after_a_live_prefix(tmp_path, monkeypatch):
    store, service, before, _ = await _setup(tmp_path)
    directory = store.root / "card-collections"
    identifiers = [f"{index:032x}" for index in range(97)]
    for index, collection_id in enumerate(identifiers):
        await collections.seal_collection(
            store, collection_id=collection_id, scope="work:project:synthetic",
            actor_subject="synthetic-owner", request_id=f"r-{index}",
            deadline=DEADLINE + 100 if index < 96 else DEADLINE,
            reads=_reads(before), catalog="")
    original = Path.iterdir

    def stable_order(self):
        if self == directory:
            return iter([directory / ident for ident in identifiers if (directory / ident).exists()])
        return original(self)

    monkeypatch.setattr(Path, "iterdir", stable_order)
    for _ in range(15):
        await service.sweep_expired_collections(now=DEADLINE, limit=8)
    assert await collections.load_header(store, identifiers[-1]) is None
    assert all((directory / ident).exists() for ident in identifiers[:-1])


@pytest.mark.asyncio
async def test_a_registration_started_before_expiry_cannot_reseal_after_expiry_and_delete(tmp_path, monkeypatch):
    operation, _, store, *_ = await _world(tmp_path)
    first = _request()
    assert _verified(await operation.answer(first), first)["kind"] == "collection"
    collection_id = collection_id_for(service_id=PEER, scope=PROJECT, request_id="zero-1",
                                      deadline=REQUEST_DEADLINE)
    original_catalog = operation._active_catalog

    async def expire_and_delete():
        catalog = await original_catalog()
        operation._clock = lambda: REQUEST_DEADLINE + 1
        await collections.delete_collection(store, collection_id)
        return catalog

    monkeypatch.setattr(operation, "_active_catalog", expire_and_delete)
    retry = _request()
    result = _verified(await operation.answer(retry), retry)
    assert result == {"kind": "refused", "code": "card_read_collection_expired", "status": 409}
    assert await collections.load_header(store, collection_id) is None

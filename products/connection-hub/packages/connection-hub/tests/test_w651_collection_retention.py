"""W651 retention: a sealed collection is deleted once its deadline passed and no in-flight transaction names it.

Horizon ZERO beyond the deadline (Main, 2026-10-09 00:05Z). Eligibility is read
under the collection's own lock, which every stage and finish of the
collection takes OUTERMOST (before any Card section), as does the sweep; an
in-flight user is a prepared receipt or an index entry (unstaged included,
group aggregates followed to their lead). Deletion is leaves first, header
last. A FINISH of an already decided receipt whose collection is gone skips
only the inert release of its fences; every other FINISH duty still runs.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from connection_hub.delegated_credentials.cards import card_read_collection as collections
from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.card_read_collection_operation import collection_id_for
from test_card_transaction_store import INTENT, SUBJECT_HASH, _setup
from test_w502_card_read_collection import COLLECTION, RS, _prepare, _seal
from test_w578_card_group_store import GROUP, _read, _service_finish, _service_members, _service_stage
from test_w578_card_read_set import CATALOG, _bind_catalog
from test_w651_card_group_collection import COLLECTION as GROUP_COLLECTION
from test_w651_card_group_collection import _other_reads
from test_w651_card_group_collection import _seal as _seal_group

DEADLINE = 2_000_000_000  # both fixtures seal with this deadline


def _recording_locks(service):
    """A real per-path lock (as the production flock), recording the order resources are entered."""
    locks: dict[str, asyncio.Lock] = {}
    entered: list[str] = []

    @asynccontextmanager
    async def mutation_lock(*, lock_path, resource_id, operation, wait_seconds):
        async with locks.setdefault(str(lock_path), asyncio.Lock()):
            entered.append(resource_id)
            yield

    service._mutation_lock = mutation_lock
    return entered


def _exists(store, collection_id):
    return (store.root / "card-collections" / collection_id).exists()


async def _sweep(service, now=DEADLINE):
    return await service.sweep_expired_collections(now=now)


@pytest.mark.asyncio
async def test_an_expired_unreferenced_collection_is_deleted_and_a_live_one_is_kept(tmp_path):
    store, service, before, _ = await _setup(tmp_path)
    await _seal(store, before)
    assert await _sweep(service, now=DEADLINE - 1) == {"deleted": 0, "leaves": 0, "protected": 0, "skipped": 0}
    assert await collections.load_header(store, COLLECTION) is not None
    assert await _sweep(service) == {"deleted": 1, "leaves": 2, "protected": 0, "skipped": 0}
    assert not _exists(store, COLLECTION) and not collections.collection_lock_path(store, COLLECTION).exists()
    assert await _sweep(service) == {"deleted": 0, "leaves": 0, "protected": 0, "skipped": 0}  # idempotent


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["committed", "aborted"])
async def test_a_prepared_read_collection_protects_it_and_a_repeated_finish_after_deletion_completes(
        tmp_path, decision):
    store, service, before, _ = await _setup(tmp_path)
    reservations = _bind_catalog(store, tmp_path)
    await _prepare(service, await _seal(store, before))
    assert (await _sweep(service))["protected"] == 1 and _exists(store, COLLECTION)
    store._card_transaction_decisions.recorded[RS] = decision
    decided = await service.decide_read_set_transaction(transaction_id=RS, intent_digest=INTENT, decision=decision)
    assert (await _sweep(service))["deleted"] == 1 and not _exists(store, COLLECTION)
    # Repeated FINISH (a lost acknowledgement): only the inert fence release is skipped.
    again = await service.decide_read_set_transaction(transaction_id=RS, intent_digest=INTENT, decision=decision)
    assert again == decided and await tx.list_in_doubt(store) == [] and await reservations.holders() == []
    await tx.assert_replaceable(store, subject_hash=SUBJECT_HASH, access_id=before.access_id)


@pytest.mark.asyncio
async def test_a_prepared_receipt_whose_collection_is_missing_still_refuses_and_stays_in_doubt(tmp_path):
    store, service, before, _ = await _setup(tmp_path)
    _bind_catalog(store, tmp_path)
    await _prepare(service, await _seal(store, before))
    await collections.delete_collection(store, COLLECTION)  # not the sweep: a damaged store
    store._card_transaction_decisions.recorded[RS] = "aborted"
    with pytest.raises(tx.CardTransactionRefused, match="card_read_collection_unknown"):
        await service.decide_read_set_transaction(transaction_id=RS, intent_digest=INTENT, decision="aborted")
    assert [entry["transaction_id"] for entry in await tx.list_in_doubt(store)] == [RS]


@pytest.mark.asyncio
async def test_an_unstaged_entry_protects_its_collection_until_its_abort(tmp_path, monkeypatch):
    store, service, before, _ = await _setup(tmp_path)
    _bind_catalog(store, tmp_path)
    header = await _seal(store, before)

    async def crash(*args, **kwargs):
        raise RuntimeError("crash after the index entry, before the receipt")

    with monkeypatch.context() as patch:
        patch.setattr(tx, "_reserve_catalog", crash)
        with pytest.raises(RuntimeError):
            await _prepare(service, header)
    assert await tx.list_in_doubt(store) == [{"transaction_id": RS, "state": "unstaged"}]
    assert (await _sweep(service))["protected"] == 1 and _exists(store, COLLECTION)
    await tx.abort_unstaged(store, RS, intent_digest=INTENT)
    assert (await _sweep(service))["deleted"] == 1


@pytest.mark.asyncio
async def test_an_entry_that_names_no_collection_and_has_no_receipt_blocks_all_deletion(tmp_path):
    """Written before entries named collections: its collection cannot be known, so nothing is deleted."""
    store, service, before, _ = await _setup(tmp_path)
    await _seal(store, before)
    path = tx.active_path(store, "a" * 64)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"transaction_id": "a" * 64}))
    assert await tx.protected_collections(store) is None
    assert (await _sweep(service))["deleted"] == 0 and _exists(store, COLLECTION)
    path.unlink()
    assert (await _sweep(service))["deleted"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["committed", "aborted"])
async def test_a_prepared_group_lead_protects_the_collection_and_a_repeated_group_finish_completes(
        tmp_path, decision):
    store, service, before, after = await _setup(tmp_path)
    _bind_catalog(store, tmp_path)
    ref = await _seal_group(store, _other_reads())
    await _service_stage(service, _service_members(before, after), catalog=CATALOG, collection=ref,
                         actor_subject="synthetic-owner")
    assert (await _sweep(service))["protected"] == 1 and _exists(store, GROUP_COLLECTION)
    decided = await _service_finish(store, service, decision)
    assert (await _sweep(service))["deleted"] == 1 and not _exists(store, GROUP_COLLECTION)
    assert await _service_finish(store, service, decision) == decided
    assert await tx.list_in_doubt(store) == []
    assert await _read(store, before) == (after if decision == "committed" else before)


@pytest.mark.asyncio
async def test_an_unstaged_group_protects_the_collection_through_its_own_entry_before_the_lead_exists(
        tmp_path, monkeypatch):
    store, service, before, after = await _setup(tmp_path)
    _bind_catalog(store, tmp_path)
    ref = await _seal_group(store, _other_reads())

    async def crash(**kwargs):
        raise RuntimeError("crash before the lead member is staged")

    with monkeypatch.context() as patch:
        patch.setattr(service, "stage_transaction", crash)
        with pytest.raises(RuntimeError):
            await _service_stage(service, _service_members(before, after), catalog=CATALOG, collection=ref)
    assert await tx.read_receipt(store, tx.member_transaction_id(GROUP, 0)) is None
    assert json.loads(tx.active_path(store, GROUP).read_text())["collection_id"] == GROUP_COLLECTION
    assert (await _sweep(service))["protected"] == 1 and _exists(store, GROUP_COLLECTION)
    await _service_finish(store, service, "aborted")
    assert (await _sweep(service))["deleted"] == 1


@pytest.mark.asyncio
async def test_a_crash_after_the_decided_member_receipt_before_materialization_keeps_the_collection(
        tmp_path, monkeypatch):
    store, service, before, after = await _setup(tmp_path)
    _bind_catalog(store, tmp_path)
    ref = await _seal_group(store, _other_reads())
    await _service_stage(service, _service_members(before, after), catalog=CATALOG, collection=ref)

    async def crash(*args, **kwargs):
        raise RuntimeError("crash after the decided receipt, before the pointer is retired")

    with monkeypatch.context() as patch:
        patch.setattr(tx, "_retire_pointer", crash)
        with pytest.raises(RuntimeError):
            await _service_finish(store, service, "committed")
    lead = await tx.read_receipt(store, tx.member_transaction_id(GROUP, 0))
    assert lead["state"] == "committed"
    assert (await _sweep(service))["protected"] == 1 and _exists(store, GROUP_COLLECTION)
    await _service_finish(store, service, "committed")  # recovery re-drives FINISH: materializes, then cleans up
    assert await _read(store, before) == after and await tx.list_in_doubt(store) == []
    assert (await _sweep(service))["deleted"] == 1


@pytest.mark.asyncio
async def test_a_crash_after_the_decided_read_collection_receipt_before_cleanup_keeps_the_collection(
        tmp_path, monkeypatch):
    store, service, before, _ = await _setup(tmp_path)
    reservations = _bind_catalog(store, tmp_path)
    await _prepare(service, await _seal(store, before))
    store._card_transaction_decisions.recorded[RS] = "committed"

    async def crash(*args, **kwargs):
        raise RuntimeError("crash after the decided receipt, before its cleanup")

    with monkeypatch.context() as patch:
        patch.setattr(tx, "_release_catalog", crash)
        with pytest.raises(RuntimeError):
            await service.decide_read_set_transaction(transaction_id=RS, intent_digest=INTENT, decision="committed")
    assert (await tx.read_receipt(store, RS))["state"] == "committed"
    assert (await _sweep(service))["protected"] == 1 and _exists(store, COLLECTION)
    await service.decide_read_set_transaction(transaction_id=RS, intent_digest=INTENT, decision="committed")
    assert await reservations.holders() == [] and await tx.list_in_doubt(store) == []
    assert (await _sweep(service))["deleted"] == 1


@pytest.mark.asyncio
async def test_a_partly_deleted_collection_refuses_every_prepare_and_the_next_sweep_finishes_it(tmp_path):
    store, service, before, _ = await _setup(tmp_path)
    _bind_catalog(store, tmp_path)
    header = await _seal(store, before)
    leaves = store.root / "card-collections" / COLLECTION / "leaves"
    next(iter(sorted(leaves.iterdir()))).unlink()  # a sweep that crashed after its first leaf
    with pytest.raises(tx.CardTransactionRefused, match="card_read_collection_incomplete"):
        await _prepare(service, header)
    assert await tx.read_receipt(store, RS) is None and await tx.list_in_doubt(store) == []
    await tx.assert_replaceable(store, subject_hash=SUBJECT_HASH, access_id=before.access_id)
    assert (await _sweep(service))["deleted"] == 1 and not _exists(store, COLLECTION)


@pytest.mark.asyncio
async def test_a_first_prepare_holding_the_lock_finishes_first_and_the_sweep_then_finds_it_protected(
        tmp_path, monkeypatch):
    store, service, before, _ = await _setup(tmp_path)
    _bind_catalog(store, tmp_path)
    _recording_locks(service)
    header = await _seal(store, before)
    release, original = asyncio.Event(), tx.prepare_read_collection

    async def slow_prepare(*args, **kwargs):
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(tx, "prepare_read_collection", slow_prepare)
    prepare = asyncio.create_task(_prepare(service, header))
    await asyncio.sleep(0.05)
    sweep = asyncio.create_task(_sweep(service))
    await asyncio.sleep(0.05)
    assert not sweep.done()  # waits on the collection lock the PREPARE holds
    release.set()
    receipt, report = await prepare, await sweep
    assert receipt["state"] == "prepared" and report["protected"] == 1 and report["deleted"] == 0
    assert await collections.resolve_collection(store, COLLECTION)


@pytest.mark.asyncio
async def test_a_sweep_holding_the_lock_finishes_first_and_the_prepare_then_finds_it_gone(tmp_path, monkeypatch):
    store, service, before, _ = await _setup(tmp_path)
    _bind_catalog(store, tmp_path)
    _recording_locks(service)
    header = await _seal(store, before)
    release, original = asyncio.Event(), collections.delete_collection

    async def slow_delete(*args, **kwargs):
        removed = await original(*args, **kwargs)
        await release.wait()
        return removed

    monkeypatch.setattr(collections, "delete_collection", slow_delete)
    sweep = asyncio.create_task(_sweep(service))
    await asyncio.sleep(0.05)
    prepare = asyncio.create_task(service.stage_read_collection_transaction(  # began before the deadline
        transaction_id=RS, intent_digest=INTENT, participant="project", collection_id=COLLECTION,
        root=header["root"], count=header["count"], catalog=header["catalog"], now=DEADLINE - 1))
    await asyncio.sleep(0.05)
    assert not prepare.done()  # waits on the collection lock the sweep holds
    release.set()
    assert (await sweep)["deleted"] == 1
    with pytest.raises(tx.CardTransactionRefused, match="card_read_collection_unknown"):
        await prepare
    assert await tx.read_receipt(store, RS) is None and await tx.list_in_doubt(store) == []
    await tx.assert_replaceable(store, subject_hash=SUBJECT_HASH, access_id=before.access_id)


@pytest.mark.asyncio
async def test_every_stage_and_finish_takes_the_collection_lock_before_any_card_section(tmp_path):
    store, service, before, after = await _setup(tmp_path)
    _bind_catalog(store, tmp_path)
    entered = _recording_locks(service)
    await _prepare(service, await _seal(store, before))
    assert entered[0] == f"card-collection:{COLLECTION}" and len(entered) > 1
    assert all(name.startswith("delegated-card:") for name in entered[1:])
    entered.clear()
    store._card_transaction_decisions.recorded[RS] = "aborted"
    await service.decide_read_set_transaction(transaction_id=RS, intent_digest=INTENT, decision="aborted")
    assert entered == [f"card-collection:{COLLECTION}"]
    entered.clear()
    ref = await _seal_group(store, _other_reads())
    await _service_stage(service, _service_members(before, after), catalog=CATALOG, collection=ref)
    assert entered[0] == f"card-collection:{GROUP_COLLECTION}" and f"card-collection:{GROUP_COLLECTION}" \
        not in entered[1:] and all(name.startswith("delegated-card:") for name in entered[1:])
    entered.clear()
    await _service_finish(store, service, "aborted")
    assert entered[0] == f"card-collection:{GROUP_COLLECTION}"
    assert all(name.startswith("delegated-card:") for name in entered[1:])


def test_the_collection_id_includes_the_deadline_so_a_deleted_id_is_never_sealed_again():
    first = collection_id_for(service_id="pb", scope="work:project:p", request_id="r", deadline=DEADLINE)
    assert first == collection_id_for(service_id="pb", scope="work:project:p", request_id="r", deadline=DEADLINE)
    assert first != collection_id_for(service_id="pb", scope="work:project:p", request_id="r", deadline=DEADLINE + 1)


@pytest.mark.asyncio
async def test_registration_seals_under_the_collection_lock_and_rechecks_the_deadline_under_it(tmp_path):
    """A sweep holding the lock past the deadline makes a waiting registration refuse, never reseal."""
    from test_w502_card_read_collection_register import DEADLINE as REQUEST_DEADLINE
    from test_w502_card_read_collection_register import PEER, PROJECT, _request, _verified, _world
    operation, _, store, *_ = await _world(tmp_path)
    locks: dict[str, asyncio.Lock] = {}
    entered: list[str] = []

    @asynccontextmanager
    async def collection_lock(collection_id):
        async with locks.setdefault(collection_id, asyncio.Lock()):
            entered.append(collection_id)
            yield

    operation._collection_lock = collection_lock
    collection_id = collection_id_for(service_id=PEER, scope=PROJECT, request_id="zero-1", deadline=REQUEST_DEADLINE)
    first = _request()
    assert _verified(await operation.answer(first), first)["kind"] == "collection"
    assert entered == [collection_id]
    held, release = asyncio.Event(), asyncio.Event()

    async def sweep_holding_the_lock():
        async with collection_lock(collection_id):
            held.set()
            await release.wait()
            operation._clock = lambda: REQUEST_DEADLINE  # the deadline passed while the sweep held the lock
            await collections.delete_collection(store, collection_id)

    sweep = asyncio.create_task(sweep_holding_the_lock())
    await held.wait()
    retry = _request()
    registration = asyncio.create_task(operation.answer(retry))
    await asyncio.sleep(0.05)
    assert not registration.done()  # waits on the lock the sweep holds
    release.set()
    await sweep
    assert _verified(await registration, retry) == {"kind": "refused", "code": "card_read_collection_expired",
                                                    "status": 409}
    assert await collections.load_header(store, collection_id) is None

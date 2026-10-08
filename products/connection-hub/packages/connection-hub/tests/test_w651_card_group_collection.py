"""W502 lane D (W651): a write group holds its read dependencies by reference to a Hub-sealed collection.

At the service: the lead member's receipt records only the reference, the
collection's leaves are fenced with the transaction AND collection and
released by the group's one decision; a collection that overlaps the group's
own targets, lacks a bound Control parent, is mixed with enumerated reads, or
is not the referenced one, refuses before anything is staged.
"""

from __future__ import annotations

import json

import pytest

from connection_hub.delegated_credentials.cards import card_read_collection as collections
from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.store import CardStorageError
from test_card_transaction_store import INTENT, SUBJECT_HASH, _setup
from test_w578_card_group_store import GROUP, _read, _service_finish, _service_members, _service_stage
from test_w578_card_read_set import CATALOG, _bind_catalog

COLLECTION = "d" * 32
OTHER = "f" * 64


def _other_reads(count: int = 2):
    """Dependencies of OTHER persons: absences here, so they need no seeded Cards."""
    return [{"subject_hash": OTHER, "access_id": f"aut_other_{index:04d}", "revision": 0} for index in range(count)]


async def _seal(store, reads, collection_id=COLLECTION):
    header = await collections.seal_collection(store, collection_id=collection_id, scope="work:project:synthetic",
                                               actor_subject="synthetic-owner", request_id="migrate-1",
                                               deadline=2_000_000_000, reads=reads, catalog=CATALOG)
    return {"schema": "connection-hub.card-read-collection-ref.v1",
            **{key: header[key] for key in ("collection_id", "scope", "count", "root", "catalog", "deadline")}}


def _lead_receipt(store):
    return json.loads(tx.receipt_path(store, tx.member_transaction_id(GROUP, 0)).read_text())


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["committed", "aborted"])
async def test_a_group_by_reference_fences_the_collection_and_its_decision_releases_it(tmp_path, decision):
    store, service, before, after = await _setup(tmp_path)
    reservations = _bind_catalog(store, tmp_path)
    ref = await _seal(store, _other_reads())
    members = _service_members(before, after)
    group = await _service_stage(service, members, catalog=CATALOG, collection=ref, actor_subject="synthetic-owner")
    assert group["staged"] is True
    lead = _lead_receipt(store)
    assert lead["collection"] == {key: ref[key] for key in ("collection_id", "root", "count")} and "reads" not in lead
    with pytest.raises(CardStorageError, match="card_transaction_unresolved"):
        await tx.assert_replaceable(store, subject_hash=OTHER, access_id="aut_other_0000")
    decided = await _service_finish(store, service, decision)
    assert decided["state"] == decision and await tx.list_in_doubt(store) == []
    assert await reservations.holders() == []
    await tx.assert_replaceable(store, subject_hash=OTHER, access_id="aut_other_0000")
    assert await _read(store, before) == (after if decision == "committed" else before)


@pytest.mark.asyncio
async def test_the_lead_receipt_has_one_size_whatever_the_collection_size(tmp_path):
    import re
    sizes = set()
    for count in (2, 60, 600):
        store, service, before, after = await _setup(tmp_path / str(count))
        _bind_catalog(store, tmp_path / str(count))
        ref = await _seal(store, _other_reads(count))
        await _service_stage(service, _service_members(before, after), catalog=CATALOG, collection=ref)
        raw = tx.receipt_path(store, tx.member_transaction_id(GROUP, 0)).read_bytes()
        sizes.add(len(re.sub(rb'"count": [0-9]+', b'"count": N', raw)))
    assert len(sizes) == 1, sizes


@pytest.mark.asyncio
async def test_a_collection_that_overlaps_the_groups_own_targets_refuses_and_stages_nothing(tmp_path):
    store, service, before, after = await _setup(tmp_path)
    _bind_catalog(store, tmp_path)
    ref = await _seal(store, sorted(_other_reads() + [
        {"subject_hash": SUBJECT_HASH, "access_id": before.access_id, "revision": before.card_revision}],
        key=lambda read: (read["subject_hash"], read["access_id"])))
    with pytest.raises(tx.CardTransactionRefused, match="card_group_collection_overlaps_target"):
        await _service_stage(service, _service_members(before, after), catalog=CATALOG, collection=ref)
    assert await tx.read_receipt(store, GROUP) is None
    assert await _read(store, before) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("change, code", [("root", "card_read_collection_moved"), ("count", "card_read_collection_moved"),
                                          ("catalog", "card_read_collection_moved"), ("scope", "card_read_collection_moved"),
                                          ("missing", "card_read_collection_unknown")])
async def test_a_reference_that_is_not_the_sealed_collection_refuses(tmp_path, change, code):
    store, service, before, after = await _setup(tmp_path)
    _bind_catalog(store, tmp_path)
    ref = await _seal(store, _other_reads())
    ref = {**ref, **{"root": {"root": "0" * 64}, "count": {"count": 3}, "catalog": {"catalog": "1" * 64},
                     "scope": {"scope": "work:project:other"}, "missing": {"collection_id": "e" * 32}}[change]}
    with pytest.raises(tx.CardTransactionRefused, match=code):
        await _service_stage(service, _service_members(before, after), catalog=CATALOG, collection=ref)
    assert await tx.read_receipt(store, GROUP) is None


@pytest.mark.asyncio
async def test_a_collection_mixed_with_enumerated_reads_refuses(tmp_path):
    store, service, before, after = await _setup(tmp_path)
    _bind_catalog(store, tmp_path)
    ref = await _seal(store, _other_reads())
    with pytest.raises(tx.CardTransactionRefused, match="card_group_dependencies_mixed"):
        await _service_stage(service, _service_members(before, after), catalog=CATALOG, collection=ref,
                             reads=[{"subject_hash": OTHER, "access_id": "aut_x", "revision": 0}])


@pytest.mark.asyncio
async def test_an_altered_leaf_refuses_the_whole_group(tmp_path):
    store, service, before, after = await _setup(tmp_path)
    _bind_catalog(store, tmp_path)
    ref = await _seal(store, _other_reads())
    leaf = collections.leaf_path(store, COLLECTION, subject_hash=OTHER, access_id="aut_other_0000")
    leaf.write_text(json.dumps({**json.loads(leaf.read_text()), "revision": 7}))
    with pytest.raises(tx.CardTransactionRefused, match="card_read_collection_root_mismatch"):
        await _service_stage(service, _service_members(before, after), catalog=CATALOG, collection=ref)
    assert await tx.read_receipt(store, GROUP) is None


@pytest.mark.asyncio
async def test_a_dependency_that_moved_after_registration_refuses_the_whole_group(tmp_path):
    from dataclasses import replace

    from test_card_transaction_store import NOW
    store, service, before, after = await _setup(tmp_path)
    _bind_catalog(store, tmp_path)
    ref = await _seal(store, [{"subject_hash": SUBJECT_HASH, "access_id": "aut_dep_absent", "revision": 0}])
    # The sealed absence became a Card between registration and PREPARE.
    await service.commit(replace(after, access_id="aut_dep_absent", card_revision=1), subject_hash=SUBJECT_HASH,
                         expected_revision=0, now=NOW)
    with pytest.raises(tx.CardTransactionRefused, match="card_dependency_moved"):
        await _service_stage(service, _service_members(before, after), catalog=CATALOG, collection=ref)
    assert await _read(store, before) == before

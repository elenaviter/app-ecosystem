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


# ── the group codec's collection form (card_group.py) ──

from connection_hub.delegated_credentials.cards import card_group as groups
from connection_hub.delegated_credentials.cards.card_read_set import read_collection_dependencies
from service_foundation.coordination.durable_decision_log import DecisionRefused


def _ref(count=2, **changes):
    return {"schema": "connection-hub.card-read-collection-ref.v1", "collection_id": COLLECTION,
            "scope": "work:project:synthetic", "count": count, "root": "a" * 64, "catalog": CATALOG,
            "deadline": 2_000_000_000, **changes}


def _group_members(before, after):
    return [groups.group_member(original=original, candidate=candidate, action=action)
            for _, original, candidate, action in _service_members(before, after)]


@pytest.mark.asyncio
async def test_the_group_projection_by_reference_is_bounded_and_verifies_exactly(tmp_path):
    import re

    from service_foundation.coordination.durable_wire import canonical_json_bytes
    store, service, before, after = await _setup(tmp_path)
    members = _group_members(before, after)
    sizes = set()
    for count in (2, 60, 1000):
        projection = groups.hub_group_participant_input(members=members, actor_subject="a", actor_kind="caller",
                                                        collection=_ref(count))
        assert projection["dependency_revisions"] == read_collection_dependencies(_ref(count))
        sizes.add(len(re.sub(rb':[0-9]+[,}]', b':N,', canonical_json_bytes(projection))))
        value = groups.group_candidate_value(members, collection=_ref(count))
        assert groups.verify_group_projection(projection, value)["collection"] == _ref(count)
    assert len(sizes) == 1
    assert groups.group_targets(value) == {(m["subject_hash"], m["access_id"]) for m in members}


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["bool_count", "extra_read_dep", "other_ref", "no_collection_in_value",
                                    "collection_with_enumerated_deps", "bad_ref"])
async def test_the_group_codec_refuses_every_mixed_or_tampered_form(tmp_path, tamper):
    store, service, before, after = await _setup(tmp_path)
    members = _group_members(before, after)
    projection = groups.hub_group_participant_input(members=members, actor_subject="a", actor_kind="caller",
                                                    collection=_ref())
    value = groups.group_candidate_value(members, collection=_ref())
    key = f"card-collection:{COLLECTION}:{'a' * 64}"
    if tamper == "bool_count":  # count 1 written as True: equal under ==, refused as not an exact int
        value = groups.group_candidate_value(members, collection=_ref(1))
        projection = groups.hub_group_participant_input(members=members, actor_subject="a", actor_kind="caller",
                                                        collection=_ref(1))
        projection["dependency_revisions"] = {**projection["dependency_revisions"], key: True}
    elif tamper == "extra_read_dep":
        projection = {**projection, "dependency_revisions": {**projection["dependency_revisions"],
                                                             f"card-absent:{OTHER}:aut_x": 1}}
    elif tamper == "other_ref":
        value = groups.group_candidate_value(members, collection=_ref(root="b" * 64))
    elif tamper == "no_collection_in_value":
        value = groups.group_candidate_value(members)
    elif tamper == "collection_with_enumerated_deps":
        projection = groups.hub_group_participant_input(members=members, actor_subject="a", actor_kind="caller",
            reads=[{"subject_hash": OTHER, "access_id": "aut_x", "revision": 0}])
    else:
        value = {**value, "collection": {**_ref(), "count": 0}}
    with pytest.raises(DecisionRefused):
        groups.verify_group_projection(projection, value)


@pytest.mark.asyncio
async def test_a_group_input_cannot_mix_a_collection_with_enumerated_reads(tmp_path):
    store, service, before, after = await _setup(tmp_path)
    with pytest.raises(DecisionRefused, match="card_group_dependencies_mixed"):
        groups.hub_group_participant_input(members=_group_members(before, after), actor_subject="a",
                                           actor_kind="caller", collection=_ref(),
                                           reads=[{"subject_hash": OTHER, "access_id": "aut_x", "revision": 0}])


@pytest.mark.asyncio
async def test_v1_enumerated_groups_are_byte_identical_to_before(tmp_path):
    store, service, before, after = await _setup(tmp_path)
    members = _group_members(before, after)
    value = groups.group_candidate_value(members)
    assert set(value) == {"schema", "cards", "effects"}
    reads = [{"subject_hash": OTHER, "access_id": "aut_x", "revision": 0}]
    projection = groups.hub_group_participant_input(members=members, actor_subject="a", actor_kind="caller",
                                                    reads=reads)
    assert groups.verify_group_projection(projection, value) == value


# ── Main 23:22Z: the W578 locking rule, pinned for the collection form ──

@pytest.mark.asyncio
async def test_a_write_to_a_leaf_after_the_lead_staged_is_refused_until_the_decision(tmp_path):
    from dataclasses import replace

    from connection_hub.delegated_credentials.cards.service import CardConflict
    from test_card_transaction_store import NOW
    store, service, before, after = await _setup(tmp_path)
    _bind_catalog(store, tmp_path)
    ref = await _seal(store, [{"subject_hash": SUBJECT_HASH, "access_id": "aut_dep_absent", "revision": 0}])
    await _service_stage(service, _service_members(before, after), catalog=CATALOG, collection=ref)
    created = replace(after, access_id="aut_dep_absent", card_revision=1)
    with pytest.raises(CardConflict, match="card_transaction_unresolved"):
        await service.commit(created, subject_hash=SUBJECT_HASH, expected_revision=0, now=NOW)
    await _service_finish(store, service, "aborted")
    await service.commit(created, subject_hash=SUBJECT_HASH, expected_revision=0, now=NOW)  # released by the decision


@pytest.mark.asyncio
async def test_a_member_that_moved_before_its_own_staging_refuses_the_group_and_abort_releases_the_leaves(
        tmp_path, monkeypatch):
    from dataclasses import replace

    from test_card_transaction_store import NOW
    store, service, before, after = await _setup(tmp_path)
    reservations = _bind_catalog(store, tmp_path)
    ref = await _seal(store, _other_reads())
    members = _service_members(before, after)
    real = service.stage_transaction
    calls = []

    async def stage_then_move(**kwargs):
        receipt = await real(**kwargs)
        calls.append(kwargs["transaction_id"])
        if len(calls) == 1:  # the lead staged; the next member's slot moves before its own staging
            later = members[1]
            moved = replace(later[2], card_revision=1) if later[1] is None else replace(later[1], card_revision=later[1].card_revision + 1)
            await service.commit(moved, subject_hash=later[0], expected_revision=0 if later[1] is None
                                 else later[1].card_revision, now=NOW)
        return receipt
    monkeypatch.setattr(service, "stage_transaction", stage_then_move)
    with pytest.raises(tx.CardTransactionRefused) as refused:
        await _service_stage(service, members, catalog=CATALOG, collection=ref)
    # The second member (a newly minted id) found its slot used before its own staging.
    assert str(refused.value) == "card_transaction_absent_slot_used"
    monkeypatch.setattr(service, "stage_transaction", real)
    with pytest.raises(CardStorageError, match="card_transaction_unresolved"):
        await tx.assert_replaceable(store, subject_hash=OTHER, access_id="aut_other_0000")  # still held
    await _service_finish(store, service, "aborted")
    await tx.assert_replaceable(store, subject_hash=OTHER, access_id="aut_other_0000")
    assert await reservations.holders() == []


@pytest.mark.asyncio
async def test_members_stage_in_canonical_order_and_only_the_lead_carries_the_collection(tmp_path, monkeypatch):
    store, service, before, after = await _setup(tmp_path)
    _bind_catalog(store, tmp_path)
    ref = await _seal(store, _other_reads(5))
    members = _service_members(before, after)
    real = service.stage_transaction
    seen = []

    async def record(**kwargs):
        seen.append((kwargs["transaction_id"], kwargs["candidate"].access_id,
                     kwargs.get("collection"), len(kwargs.get("collection_reads") or ())))
        return await real(**kwargs)
    monkeypatch.setattr(service, "stage_transaction", record)
    await _service_stage(service, members, catalog=CATALOG, collection=ref)
    assert [entry[0] for entry in seen] == [tx.member_transaction_id(GROUP, index) for index in range(len(members))]
    assert [entry[1] for entry in seen] == [member[2].access_id for member in members]  # canonical order
    assert seen[0][2] == {key: ref[key] for key in ("collection_id", "root", "count")} and seen[0][3] == 5
    assert all(entry[2] is None and entry[3] == 0 for entry in seen[1:])

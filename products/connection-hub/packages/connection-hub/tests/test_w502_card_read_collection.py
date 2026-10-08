"""W502 lane D: a read set held by reference to a Hub-sealed collection.

The collection is sealed once (leaves, then the header); PREPARE resolves it,
fences every leaf with this transaction AND collection, and writes a receipt
that holds only the reference (id, root, count, catalog), whatever the number
of reads. Fences bind through the collection's own leaf; the recorded
decision releases exactly the collection's fences; a missing, partial,
extra or altered collection refuses by name.
"""

from __future__ import annotations

import json

import pytest

from service_foundation.coordination.durable_wire import canonical_json_bytes

from connection_hub.delegated_credentials.cards import card_read_collection as collections
from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.card_read_set import read_set_candidate_value
from connection_hub.delegated_credentials.cards.store import CardStorageError
from service_foundation.coordination.durable_wire import sha256_hex
from test_card_transaction_store import INTENT, SUBJECT_HASH, _setup
from test_w578_card_read_set import CATALOG, _bind_catalog

RS = "e" * 64
COLLECTION = "c" * 32


def _reads(before, extra: int = 0):
    reads = [{"subject_hash": SUBJECT_HASH, "access_id": before.access_id, "revision": before.card_revision},
             {"subject_hash": SUBJECT_HASH, "access_id": "aut_absent", "revision": 0}]
    reads += [{"subject_hash": f"{index:064x}", "access_id": f"aut_absent_{index}", "revision": 0}
              for index in range(1, extra + 1)]
    return sorted(reads, key=lambda read: (read["subject_hash"], read["access_id"]))


async def _seal(store, before, *, extra: int = 0, collection_id: str = COLLECTION):
    return await collections.seal_collection(store, collection_id=collection_id, scope="work:project:synthetic",
                                             actor_subject="synthetic-owner", request_id="r-1", deadline=2_000_000_000,
                                             reads=_reads(before, extra), catalog=CATALOG)


async def _prepare(service, header):
    return await service.stage_read_collection_transaction(
        transaction_id=RS, intent_digest=INTENT, participant="project", collection_id=header["collection_id"],
        root=header["root"], count=header["count"], catalog=header["catalog"])


def test_the_root_is_the_w578_read_set_candidate_digest_of_the_same_reads():
    reads = [{"subject_hash": "a" * 64, "access_id": "x", "revision": 2},
             {"subject_hash": "b" * 64, "access_id": "y", "revision": 0}]
    assert collections.collection_root(list(reversed(reads)), CATALOG) == sha256_hex(
        canonical_json_bytes(read_set_candidate_value(reads, CATALOG)))


@pytest.mark.asyncio
async def test_a_prepared_collection_fences_every_leaf_and_its_receipt_holds_only_the_reference(tmp_path):
    store, service, before, after = await _setup(tmp_path)
    reservations = _bind_catalog(store, tmp_path)
    header = await _seal(store, before)
    receipt = await _prepare(service, header)
    assert set(receipt) == {"schema", "transaction_id", "intent_digest", "participant", "state", "reason",
                            "collection_id", "root", "count", "catalog"}
    assert receipt["state"] == "prepared" and receipt["count"] == 2 and receipt["root"] == header["root"]
    for access_id in (before.access_id, "aut_absent"):
        with pytest.raises(CardStorageError, match="card_transaction_unresolved"):
            await tx.assert_replaceable(store, subject_hash=SUBJECT_HASH, access_id=access_id)
    assert [holder["transaction_id"] for holder in await reservations.holders()] == [RS]
    assert [entry["transaction_id"] for entry in await tx.list_in_doubt(store)] == [RS]


@pytest.mark.asyncio
async def test_the_receipt_has_one_size_whatever_the_number_of_reads(tmp_path):
    sizes = set()
    for extra in (1, 31, 98):
        store, service, before, _ = await _setup(tmp_path / str(extra))
        _bind_catalog(store, tmp_path / str(extra))
        receipt = await _prepare(service, await _seal(store, before, extra=extra))
        raw = json.loads(tx.receipt_path(store, RS).read_text())
        sizes.add(len(canonical_json_bytes({key: value for key, value in raw.items() if key != "count"})))
        assert raw["count"] == extra + 2 and "reads" not in raw
    assert len(sizes) == 1, sizes


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["committed", "aborted"])
async def test_the_recorded_decision_releases_exactly_the_collection_s_fences(tmp_path, decision):
    store, service, before, after = await _setup(tmp_path)
    reservations = _bind_catalog(store, tmp_path)
    await _prepare(service, await _seal(store, before))
    store._card_transaction_decisions.recorded[RS] = decision
    decided = await service.decide_read_set_transaction(transaction_id=RS, intent_digest=INTENT, decision=decision)
    assert decided["state"] == decision and await tx.list_in_doubt(store) == []
    assert await reservations.holders() == []
    for access_id in (before.access_id, "aut_absent"):
        await tx.assert_replaceable(store, subject_hash=SUBJECT_HASH, access_id=access_id)
    again = await service.decide_read_set_transaction(transaction_id=RS, intent_digest=INTENT, decision=decision)
    assert again == decided


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["root", "count", "catalog"])
async def test_a_reference_that_is_not_the_sealed_collection_refuses_and_fences_nothing(tmp_path, change):
    store, service, before, after = await _setup(tmp_path)
    _bind_catalog(store, tmp_path)
    header = await _seal(store, before)
    moved = {**header, change: {"root": "0" * 64, "count": header["count"] + 1, "catalog": "1" * 64}[change]}
    with pytest.raises(tx.CardTransactionRefused, match="card_read_collection_moved"):
        await _prepare(service, moved)
    assert await tx.read_receipt(store, RS) is None
    await tx.assert_replaceable(store, subject_hash=SUBJECT_HASH, access_id=before.access_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing_header", "missing_leaf", "extra_leaf", "altered_leaf"])
async def test_a_partial_or_altered_collection_refuses_by_name(tmp_path, damage):
    store, service, before, after = await _setup(tmp_path)
    _bind_catalog(store, tmp_path)
    header = await _seal(store, before)
    leaves = collections.header_path(store, COLLECTION).parent / "leaves"
    first = sorted(leaves.iterdir())[0]
    if damage == "missing_header":
        collections.header_path(store, COLLECTION).unlink()
        expected = "card_read_collection_unknown"
    elif damage == "missing_leaf":
        first.unlink()
        expected = "card_read_collection_incomplete"
    elif damage == "extra_leaf":
        (leaves / ("f" * 64 + ".json")).write_text(first.read_text())
        expected = "card_read_collection_incomplete"
    else:
        raw = json.loads(first.read_text())
        first.write_text(json.dumps({**raw, "revision": raw["revision"] + 5}))
        expected = "card_read_collection_root_mismatch"
    with pytest.raises(tx.CardTransactionRefused, match=expected):
        await _prepare(service, header)
    assert await tx.read_receipt(store, RS) is None


@pytest.mark.asyncio
async def test_a_moved_card_refuses_the_collection_and_leaves_no_receipt(tmp_path):
    store, service, before, after = await _setup(tmp_path)
    _bind_catalog(store, tmp_path)
    header = await collections.seal_collection(
        store, collection_id=COLLECTION, scope="work:project:synthetic", actor_subject="synthetic-owner",
        request_id="r-1", deadline=2_000_000_000, catalog=CATALOG,
        reads=[{"subject_hash": SUBJECT_HASH, "access_id": before.access_id, "revision": before.card_revision + 1}])
    with pytest.raises(tx.CardTransactionRefused, match="card_dependency_moved"):
        await _prepare(service, header)
    assert await tx.read_receipt(store, RS) is None


@pytest.mark.asyncio
async def test_a_sealed_collection_is_immutable_and_its_replay_is_exact(tmp_path):
    store, service, before, after = await _setup(tmp_path)
    header = await _seal(store, before)
    assert await _seal(store, before) == header
    with pytest.raises(CardStorageError, match="card_read_collection_conflict"):
        await _seal(store, before, extra=1)


@pytest.mark.asyncio
async def test_a_replayed_prepare_is_the_same_receipt_and_another_reference_is_refused(tmp_path):
    store, service, before, after = await _setup(tmp_path)
    _bind_catalog(store, tmp_path)
    header = await _seal(store, before)
    first = await _prepare(service, header)
    assert await _prepare(service, header) == first
    other = await _seal(store, before, extra=1, collection_id="d" * 32)
    with pytest.raises(tx.CardTransactionRefused, match="card_transaction_replay_changed"):
        await _prepare(service, other)


@pytest.mark.asyncio
async def test_a_fence_naming_another_collection_is_a_binding_violation(tmp_path):
    store, service, before, after = await _setup(tmp_path)
    _bind_catalog(store, tmp_path)
    await _prepare(service, await _seal(store, before))
    fence = tx.read_fence_path(store, subject_hash=SUBJECT_HASH, access_id=before.access_id)
    fence.write_text(json.dumps({"transaction_id": RS, "collection_id": "d" * 32}))
    with pytest.raises(CardStorageError, match="card_transaction_read_fence_binding_invalid"):
        await tx.assert_replaceable(store, subject_hash=SUBJECT_HASH, access_id=before.access_id)


# ── the by-reference binding: a bounded projection, exact validation ──

from connection_hub.delegated_credentials.cards import card_read_set as read_set
from service_foundation.coordination.durable_decision_log import DecisionRefused


def _ref(count: int = 2, **changes):
    return {"schema": read_set.READ_COLLECTION_REF_SCHEMA, "collection_id": COLLECTION,
            "scope": "work:project:synthetic", "count": count, "root": "a" * 64, "catalog": CATALOG,
            "deadline": 2_000_000_000, **changes}


def test_the_projection_is_one_size_whatever_the_number_of_reads():
    # Only the count's own decimal digits differ: it is the one dependency value; the ref enters as a digest.
    sizes = {len(canonical_json_bytes(read_set.hub_read_collection_participant_input(
        ref=_ref(count), actor_subject="synthetic-owner", actor_kind="caller"))) - len(str(count))
        for count in (3, 66, 1000)}
    assert len(sizes) == 1, sizes
    projection = read_set.hub_read_collection_participant_input(ref=_ref(1000), actor_subject="a", actor_kind="caller")
    assert set(projection["dependency_revisions"]) == {f"card-collection:{COLLECTION}:{'a' * 64}",
                                                       f"catalog-active:{CATALOG}"}
    assert read_set.verify_read_collection_projection(projection, _ref(1000)) == _ref(1000)


@pytest.mark.parametrize("change", [{"count": 0}, {"count": True}, {"root": "A" * 64}, {"collection_id": "c" * 31},
                                    {"catalog": "x"}, {"deadline": 0}, {"schema": "other"}, {"extra": 1}])
def test_a_malformed_reference_is_refused_by_name(change):
    with pytest.raises(DecisionRefused, match="card_read_collection_ref_invalid"):
        read_set.validate_read_collection_ref(_ref(**change))


@pytest.mark.parametrize("field, value", [("dependency_revisions", {}), ("candidate_digest", "0" * 64),
                                          ("binding_ref", "collection:" + "d" * 32), ("action", "write")])
def test_a_projection_that_does_not_bind_the_reference_is_refused(field, value):
    projection = {**read_set.hub_read_collection_participant_input(ref=_ref(), actor_subject="a", actor_kind="caller"),
                  field: value}
    with pytest.raises(DecisionRefused, match="card_read_set_not_bound"):
        read_set.verify_read_collection_projection(projection, _ref())


def test_an_enumerated_read_set_parser_never_reads_a_collection_key():
    from connection_hub.delegated_credentials.cards.card_participant import reads_from_dependencies
    with pytest.raises(DecisionRefused, match="card_dependency_invalid"):
        reads_from_dependencies(read_set.read_collection_dependencies(_ref()))


# ── through the Hub participant, from a signed authority, by reference ──

async def _participant(tmp_path, *, deadline: int = 2_000_000_000, actor: str = "synthetic-owner"):
    from test_w578_card_group_participant import AUTHORITY, _Authority

    from connection_hub.delegated_credentials.cards.authority_intent_source import (
        AuthorityCardIntentSource, AuthorityDecisionReader,
    )
    from connection_hub.delegated_credentials.cards.card_participant import DecisionStorePort, HubCardParticipant

    store, service, before, after = await _setup(tmp_path)
    reservations = _bind_catalog(store, tmp_path)
    header = await collections.seal_collection(
        store, collection_id=COLLECTION, scope="work:project:synthetic", actor_subject="synthetic-owner",
        request_id="r-1", deadline=deadline, reads=_reads(before), catalog=CATALOG)
    ref = {"schema": read_set.READ_COLLECTION_REF_SCHEMA, **{key: header[key] for key in (
        "collection_id", "scope", "count", "root", "catalog", "deadline")}}
    authority = _Authority(read_set.hub_read_collection_participant_input(ref=ref, actor_subject=actor,
                                                                          actor_kind="caller"), ref)
    clock = lambda: 1_800_000_000  # noqa: E731
    decisions = AuthorityDecisionReader(fetch=authority.fetch, authority=AUTHORITY, clock=clock)
    tx.bind_transaction_decisions(store, DecisionStorePort(decisions))
    hub = HubCardParticipant(service=service, store=store,
                             intents=AuthorityCardIntentSource(store=store, fetch=authority.fetch,
                                                               authority=AUTHORITY, clock=clock),
                             decisions=decisions)
    return store, hub, authority, reservations, before


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["committed", "aborted"])
async def test_a_by_reference_read_set_prepares_and_finishes_through_the_participant(tmp_path, decision):
    from test_w578_card_group_participant import TX

    store, hub, authority, reservations, before = await _participant(tmp_path)
    receipt = await hub.prepare(TX)
    assert receipt.transaction_id == TX
    raw = json.loads(tx.receipt_path(store, TX).read_text())
    assert raw["schema"] == tx.READ_COLLECTION_RECEIPT_SCHEMA and "reads" not in raw
    # The Hub's own intent record holds the reference, not the reads.
    intent = json.loads((store.root / "card-transactions" / "intents" / f"{TX}.json").read_text())
    assert intent["reads"] == [] and intent["collection"]["collection_id"] == COLLECTION
    with pytest.raises(CardStorageError, match="card_transaction_unresolved"):
        await tx.assert_replaceable(store, subject_hash=SUBJECT_HASH, access_id=before.access_id)
    authority.decision = decision
    await hub.finish(TX, decision)
    assert (await tx.state(store, transaction_id=TX))["state"] == decision
    assert await reservations.holders() == [] and await tx.list_in_doubt(store) == []
    await tx.assert_replaceable(store, subject_hash=SUBJECT_HASH, access_id=before.access_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("case, code", [("expired", "card_read_collection_expired"),
                                        ("other_actor", "card_read_collection_moved")])
async def test_an_expired_or_foreign_collection_is_never_prepared(tmp_path, case, code):
    from test_w578_card_group_participant import TX

    store, hub, authority, reservations, before = await _participant(
        tmp_path, deadline=1 if case == "expired" else 2_000_000_000,
        actor="someone-else" if case == "other_actor" else "synthetic-owner")
    with pytest.raises(DecisionRefused, match=code):
        await hub.prepare(TX)
    assert await tx.read_receipt(store, TX) is None
    await tx.assert_replaceable(store, subject_hash=SUBJECT_HASH, access_id=before.access_id)

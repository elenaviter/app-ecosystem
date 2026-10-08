"""W502 lane D: ``card_read_collection_register`` seals exactly the census's reads, by reference.

The Hub derives the descriptors through the census identity path, so the sealed
collection equals what the initiator's census would enumerate for the same
persons; the signed answer carries only the bounded reference.
"""

from __future__ import annotations

import dataclasses
import os
from datetime import datetime, timezone

import pytest

from connection_hub.delegated_credentials.admission import AdmissionRequest, sign_admission_request
from connection_hub.delegated_credentials.cards import card_read_collection as collections
from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.card_read_collection_operation import (
    ANSWER_SCHEMA, OPERATION, REQUEST_SCHEMA, CardReadCollectionOperation, collection_id_for,
    collection_request_digest,
)
from connection_hub.delegated_credentials.cards.card_read_set import (
    READ_COLLECTION_REF_SCHEMA, hub_read_collection_participant_input, validate_read_collection_ref,
)
from connection_hub.delegated_credentials.cards.participant_operation import ParticipantCaller
from connection_hub.delegated_credentials.cards.store import subject_hash_for
from connection_hub.delegated_credentials.catalog.reservations import catalog_version_digest
from service_foundation.coordination.participant_answer import (
    AnswerContract, ParticipantAnswerRefused, verify_participant_answer,
)
from test_card_census_read import (
    ADMIN, NOW, OTHER, PEER, PROJECT, RECEIPT_SECRET, REQUEST_SECRET, _Nonces, _request as _census_request,
    _verified as _census_verified, _world as _census_world,
)
from test_card_transaction_store import INTENT, TX
from test_w580_bound_card_writers import redis_client  # noqa: F401 - fixture

DEADLINE = NOW + 600


async def _world(tmp_path, *, catalog=True, prefix="work:project:"):
    census, store, control, identity, catalog_store = await _census_world(tmp_path, catalog=catalog)
    caller = ParticipantCaller(service_id=PEER, request_secret=REQUEST_SECRET, receipt_secret=RECEIPT_SECRET,
                               receipt_signer_id="connection-hub@1-0", audience="problem-board@1-0",
                               hub_resource="connection-hub@1-0", bind=None, scope_field="project_ref",
                               census_scope_prefix=prefix)
    operation = CardReadCollectionOperation(callers={PEER: caller}, card_store=store, catalog_store=catalog_store,
                                            nonces=_Nonces(), clock=lambda: NOW)
    return operation, census, store, control, identity, catalog_store


def _request(persons=(ADMIN, OTHER), *, scope=PROJECT, request_id="register-1", deadline=DEADLINE,
             actor="user:admin-1", secret=REQUEST_SECRET, nonce=None, **changes):
    data = {"schema": REQUEST_SCHEMA, "request_echo": os.urandom(16).hex(), "scope": scope,
            "persons": sorted(persons), "actor_subject": actor, "request_id": request_id, "deadline": deadline}
    data.update(changes)
    proof = {"service_id": PEER, "timestamp": str(NOW), "nonce": nonce or os.urandom(12).hex()}
    proof["signature"] = sign_admission_request(
        secret=secret, **proof, delegated_token=f"{REQUEST_SCHEMA}:{data['request_echo']}",
        request=AdmissionRequest(resource="connection-hub@1-0", operation=OPERATION,
                                 invocation_id=data["request_echo"], request_digest=collection_request_digest(data),
                                 approval_context={"protocol": REQUEST_SCHEMA}))
    return {**data, "service_proof": proof}


def _verified(response, request):
    answer = dict(response["collection_answer"])
    proof = answer.pop("receipt_proof")
    unsigned_request = {name: request[name] for name in (
        "schema", "request_echo", "scope", "persons", "actor_subject", "request_id", "deadline")}
    assert answer["schema"] == ANSWER_SCHEMA and answer["audience"] == "problem-board@1-0"
    assert answer["request_digest"] == collection_request_digest(request)
    result = verify_participant_answer({**answer, "receipt_proof": proof}, schema=ANSWER_SCHEMA,
        secret=RECEIPT_SECRET, signer_id="connection-hub@1-0", audience="problem-board@1-0",
        direction="hub-to-authority", request=unsigned_request, now=NOW, contract=AnswerContract.COLLECTION)
    return result


async def _census_reads(census, persons):
    """What the initiator's census enumerates: PB's read_reservations() shape over the census answer."""
    request = _census_request(persons)
    result = _census_verified(await census.answer(request), request)
    reads = {}
    for person in result["persons"]:
        for entry in (person["my"], person["control"], *person["chain"]["cards"]):
            key = (entry["subject_hash"], entry["access_id"])
            reads[key] = {"subject_hash": key[0], "access_id": key[1], "revision": entry.get("revision", 0)}
    return [reads[key] for key in sorted(reads)]


@pytest.mark.asyncio
async def test_registration_seals_exactly_the_census_reads_and_answers_only_a_bounded_reference(tmp_path):
    operation, census, store, control, identity, catalog_store = await _world(tmp_path)
    request = _request()
    response = await operation.answer(request)
    assert response["ok"] is True
    result = _verified(response, request)
    assert result["kind"] == "collection" and set(result) == {"kind", "ref"}
    ref = validate_read_collection_ref(result["ref"])
    expected = await _census_reads(census, [ADMIN, OTHER])
    active = await catalog_store.read_active()
    catalog = catalog_version_digest(active.version, active.content_hash)
    assert ref == {"schema": READ_COLLECTION_REF_SCHEMA, "scope": PROJECT, "deadline": DEADLINE,
                   "collection_id": collection_id_for(service_id=PEER, scope=PROJECT, request_id="register-1"),
                   "count": len(expected), "root": collections.collection_root(expected, catalog), "catalog": catalog}
    header, reads = await collections.resolve_collection(store, ref["collection_id"])
    assert reads == expected and header["actor_subject"] == "user:admin-1" and header["request_id"] == "register-1"
    assert {(read["access_id"], read["revision"]) for read in reads} >= {
        (identity.control_id, control.card_revision), (identity.my_card_id, 0)}
    # The reference drives the bounded Hub participant input, with no reads in it.
    projection = hub_read_collection_participant_input(ref=ref, actor_subject="user:admin-1", actor_kind="caller")
    assert "reads" not in str(projection.get("dependency_revisions"))


@pytest.mark.asyncio
async def test_a_retry_reseals_the_same_collection_and_changed_reads_conflict(tmp_path):
    operation, *_ = await _world(tmp_path)
    first_request = _request()
    first = _verified(await operation.answer(first_request), first_request)
    retry = _request()  # a new echo and nonce, the same request id and persons
    assert _verified(await operation.answer(retry), retry) == first
    # A different person list under the same request id seals different reads: refused by name.
    other = _request([ADMIN])
    assert _verified(await operation.answer(other), other) == {
        "kind": "refused", "code": "card_read_collection_conflict", "status": 409}


@pytest.mark.asyncio
async def test_a_card_under_another_decision_refuses_the_whole_registration(tmp_path):
    operation, census, store, control, identity, catalog_store = await _world(tmp_path)
    await tx.stage(store, transaction_id=TX, intent_digest=INTENT, participant="project",
                   subject_hash=subject_hash_for(identity.project_subject), original=control,
                   candidate=dataclasses.replace(control, card_revision=control.card_revision + 1, label="staged"),
                   now=datetime.fromtimestamp(NOW, timezone.utc))
    request = _request()
    assert _verified(await operation.answer(request), request) == {
        "kind": "refused", "code": "card_read_collection_in_transaction", "status": 409}
    collection_id = collection_id_for(service_id=PEER, scope=PROJECT, request_id="register-1")
    assert await collections.load_header(store, collection_id) is None  # nothing sealed, not even partially


@pytest.mark.asyncio
@pytest.mark.parametrize("case,code,status", [
    ("scope", "card_census_scope_forbidden", 403),
    ("deadline", "card_read_collection_deadline_passed", 409),
    ("catalog_store", "card_catalog_reservation_unavailable", 503),
])
async def test_signed_refusals_name_their_cause(tmp_path, case, code, status):
    operation, *_ = await _world(tmp_path, catalog=case != "catalog_store", prefix="work:other:" if case == "scope" else "work:project:")
    request = _request(deadline=NOW if case == "deadline" else DEADLINE)
    assert _verified(await operation.answer(request), request) == {"kind": "refused", "code": code, "status": status}


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["empty_persons", "unsorted_persons", "bad_deadline", "extra_field", "bad_schema"])
async def test_malformed_requests_are_refused_unsigned(tmp_path, change):
    operation, *_ = await _world(tmp_path)
    request = _request()
    if change == "empty_persons":
        request["persons"] = []
    elif change == "unsorted_persons":
        request["persons"] = [OTHER, ADMIN]
    elif change == "bad_deadline":
        request["deadline"] = "soon"
    elif change == "extra_field":
        request["reads"] = []
    else:
        request["schema"] = "card-census-read.v1"
    response = await operation.answer(request)
    assert response == {"ok": False, "status": 400, "error": {"code": "card_read_collection_request_invalid"}}


@pytest.mark.asyncio
async def test_unauthenticated_and_replayed_requests_get_unsigned_refusals(tmp_path):
    operation, *_ = await _world(tmp_path)
    forged = _request(secret="x" * 40)
    assert (await operation.answer(forged))["error"]["code"] == "card_participant_unauthenticated"
    request = _request(nonce="n" * 24)
    assert (await operation.answer(request))["ok"] is True
    replay = _request(nonce="n" * 24, request_id="register-2")
    assert (await operation.answer(replay))["error"]["code"] == "card_participant_unauthenticated"
    # A census proof does not admit a registration: the operation and protocol are bound.
    census_shaped = _census_request()
    assert (await operation.answer(census_shaped))["error"]["code"] == "card_read_collection_request_invalid"


@pytest.mark.asyncio
async def test_the_answer_is_bound_to_the_collection_contract(tmp_path):
    operation, *_ = await _world(tmp_path)
    request = _request()
    answer = dict((await operation.answer(request))["collection_answer"])
    for field, value in (("request_id", "register-x"), ("deadline", DEADLINE + 1), ("actor_subject", "user:x")):
        tampered = {**answer, field: value}
        with pytest.raises(ParticipantAnswerRefused):
            verify_participant_answer(tampered, schema=ANSWER_SCHEMA, secret=RECEIPT_SECRET,
                signer_id="connection-hub@1-0", audience="problem-board@1-0", direction="hub-to-authority",
                request={name: request[name] for name in ("schema", "request_echo", "scope", "persons",
                                                           "actor_subject", "request_id", "deadline")},
                now=NOW, contract=AnswerContract.COLLECTION)


@pytest.mark.asyncio
async def test_the_reference_is_one_size_whatever_the_person_count(tmp_path):
    sizes = set()
    for count in (2, 30, 120):
        operation, *_ = await _world(tmp_path / str(count))
        persons = sorted({ADMIN, OTHER, *(f"user:extra-{index:04d}" for index in range(count - 2))})
        request = _request(persons)
        result = _verified(await operation.answer(request), request)
        from service_foundation.coordination.durable_wire import canonical_json_bytes
        import re
        sizes.add(len(re.sub(rb'"count":[0-9]+', b'"count":N', canonical_json_bytes(result["ref"]))))
    assert len(sizes) == 1


async def test_a_real_pair_seals_my_control_and_the_complete_chain_like_the_census(tmp_path, redis_client):
    from connection_hub.delegated_credentials.cards.census_read import CardCensusReadOperation
    from test_w502_my_card_fence_real_path import TARGET, PROJECT_REF, _create, _service

    h = await _service(tmp_path, redis_client)
    assert (await _create(h, "request-create"))["ok"] is True
    caller = ParticipantCaller(service_id=PEER, request_secret=REQUEST_SECRET, receipt_secret=RECEIPT_SECRET,
                               receipt_signer_id="connection-hub@1-0", audience="problem-board@1-0",
                               hub_resource="connection-hub@1-0", bind=None, scope_field="project_ref",
                               census_scope_prefix="work:project:")
    census = CardCensusReadOperation(callers={PEER: caller}, card_store=h.store, catalog_store=None,
                                     nonces=_Nonces(), clock=lambda: NOW)
    census_request = _census_request([TARGET], scope=PROJECT_REF, include_catalog=False)
    entry = _census_verified(await census.answer(census_request), census_request)["persons"][0]
    assert entry["chain"]["state"] == "complete" and entry["chain"]["cards"]
    expected = {}
    for card in (entry["my"], entry["control"], *entry["chain"]["cards"]):
        expected[(card["subject_hash"], card["access_id"])] = {
            "subject_hash": card["subject_hash"], "access_id": card["access_id"], "revision": card.get("revision", 0)}
    operation = CardReadCollectionOperation(callers={PEER: caller}, card_store=h.store, catalog_store=None,
                                            nonces=_Nonces(), clock=lambda: NOW)
    request = _request([TARGET], scope=PROJECT_REF)
    result = _verified(await operation.answer(request), request)
    # Without a catalog store the registration refuses rather than sealing a catalog-less collection.
    assert result == {"kind": "refused", "code": "card_catalog_reservation_unavailable", "status": 503}

    class _NoActiveCatalog:
        async def read_active(self):
            return None
    operation = CardReadCollectionOperation(callers={PEER: caller}, card_store=h.store, catalog_store=_NoActiveCatalog(),
                                            nonces=_Nonces(), clock=lambda: NOW)
    request = _request([TARGET], scope=PROJECT_REF)
    ref = _verified(await operation.answer(request), request)["ref"]
    _, reads = await collections.resolve_collection(h.store, ref["collection_id"])
    assert reads == [expected[key] for key in sorted(expected)] and ref["catalog"] == ""
    assert all(read["revision"] >= 1 for read in reads)  # My, Control and every chain Card are present

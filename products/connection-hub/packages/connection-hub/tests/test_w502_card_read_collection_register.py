"""W502 lane D: ``card_read_collection_register`` seals the census's read reservations as a Hub collection.

Admitted like the census read; the descriptors equal the census consumer's
read reservations over the same persons; the answer is signed under the closed
collection contract and carries only the bounded reference; a retry of the
same request reaches the same sealed collection; a staged Card, an expired
deadline or a foreign scope refuses and seals nothing.
"""

from __future__ import annotations

import dataclasses
import os
from datetime import datetime, timezone

import pytest

from service_foundation.coordination.participant_answer import AnswerContract, sign_participant_answer

from connection_hub.delegated_credentials.admission import AdmissionRequest, sign_admission_request
from connection_hub.delegated_credentials.cards import card_read_collection as collections
from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.card_read_collection_operation import (
    ANSWER_SCHEMA, OPERATION, REQUEST_SCHEMA, CardReadCollectionOperation, collection_id_for,
    collection_request_digest, descriptors_of,
)
from connection_hub.delegated_credentials.cards.card_read_set import (
    READ_COLLECTION_REF_SCHEMA, validate_read_collection_ref,
)
from connection_hub.delegated_credentials.cards.participant_operation import ParticipantCaller
from connection_hub.delegated_credentials.cards.store import subject_hash_for
from connection_hub.delegated_credentials.catalog.reservations import catalog_version_digest
from test_card_census_read import (
    ADMIN, NOW, OTHER, PEER, PROJECT, RECEIPT_SECRET, REQUEST_SECRET, _Nonces, _world as _census_world,
)
from test_card_transaction_store import INTENT, TX

DEADLINE = NOW + 300


async def _world(tmp_path):
    census, store, control, identity, catalog_store = await _census_world(tmp_path)
    caller = ParticipantCaller(service_id=PEER, request_secret=REQUEST_SECRET, receipt_secret=RECEIPT_SECRET,
                               receipt_signer_id="connection-hub@1-0", audience="problem-board@1-0",
                               hub_resource="connection-hub@1-0", bind=None, scope_field="project_ref",
                               census_scope_prefix="work:project:")
    operation = CardReadCollectionOperation(callers={PEER: caller}, card_store=store, catalog_store=catalog_store,
                                            nonces=_Nonces(), clock=lambda: NOW)
    return operation, census, store, control, identity, catalog_store


def _request(persons=(ADMIN, OTHER), *, scope=PROJECT, request_id="zero-1", deadline=DEADLINE):
    data = {"schema": REQUEST_SCHEMA, "request_echo": os.urandom(16).hex(), "scope": scope,
            "persons": sorted(persons), "actor_subject": "admin-1", "request_id": request_id, "deadline": deadline}
    proof = {"service_id": PEER, "timestamp": str(NOW), "nonce": os.urandom(12).hex()}
    proof["signature"] = sign_admission_request(
        secret=REQUEST_SECRET, **proof, delegated_token=f"{REQUEST_SCHEMA}:{data['request_echo']}",
        request=AdmissionRequest(resource="connection-hub@1-0", operation=OPERATION,
                                 invocation_id=data["request_echo"], request_digest=collection_request_digest(data),
                                 approval_context={"protocol": REQUEST_SCHEMA}))
    return {**data, "service_proof": proof}


def _verified(response, request):
    answer = dict(response["collection_answer"])
    proof = answer.pop("receipt_proof")
    assert answer["schema"] == ANSWER_SCHEMA and answer["request_digest"] == collection_request_digest(request)
    for name in ("request_echo", "scope", "persons", "actor_subject", "request_id", "deadline"):
        assert answer[name] == request[name]
    assert sign_participant_answer(answer, schema=ANSWER_SCHEMA, secret=RECEIPT_SECRET,
                                   signer_id=proof["service_id"], timestamp=proof["timestamp"],
                                   contract=AnswerContract.COLLECTION)["signature"] == proof["signature"]
    return answer["result"]


async def _census_reservations(census, persons):
    """Problem Board's read_reservations over the census of the same persons, written out independently
    (Applications services/card_business_hub_census.py: My, Control and every chain Card per person,
    revision or 0, distinct by (subject_hash, access_id), sorted)."""
    from test_card_census_read import _request as census_request, _verified as census_verified
    request = census_request(persons)
    result = census_verified(await census.answer(request), request)
    reads = {}
    for person in result["persons"]:
        for entry in (person["my"], person["control"], *person["chain"]["cards"]):
            key = (entry["subject_hash"], entry["access_id"])
            reads[key] = {"subject_hash": key[0], "access_id": key[1], "revision": entry.get("revision", 0)}
    return [reads[key] for key in sorted(reads)], result["catalog"]


@pytest.mark.asyncio
async def test_registration_seals_exactly_the_census_read_reservations_and_answers_a_bounded_reference(tmp_path):
    operation, census, store, control, identity, catalog_store = await _world(tmp_path)
    request = _request()
    result = _verified(await operation.answer(request), request)
    assert result["kind"] == "collection"
    ref = validate_read_collection_ref(result["ref"])
    assert ref["schema"] == READ_COLLECTION_REF_SCHEMA and ref["scope"] == PROJECT and ref["deadline"] == DEADLINE
    assert ref["collection_id"] == collection_id_for(service_id=PEER, scope=PROJECT, request_id="zero-1")
    expected, catalog = await _census_reservations(census, (ADMIN, OTHER))
    header, reads = await collections.resolve_collection(store, ref["collection_id"])
    assert reads == expected and ref["count"] == len(expected)
    assert ref["catalog"] == catalog_version_digest(catalog["version"], catalog["content_hash"])
    assert ref["root"] == collections.collection_root(expected, ref["catalog"]) == header["root"]


@pytest.mark.asyncio
async def test_a_retry_of_the_same_request_reaches_the_same_sealed_collection(tmp_path):
    operation, *_ = await _world(tmp_path)
    first, second = _request(), _request()
    assert _verified(await operation.answer(first), first)["ref"] == _verified(
        await operation.answer(second), second)["ref"]


@pytest.mark.asyncio
async def test_a_staged_card_refuses_the_registration_and_seals_nothing(tmp_path):
    operation, census, store, control, identity, catalog_store = await _world(tmp_path)
    await tx.stage(store, transaction_id=TX, intent_digest=INTENT, participant="project",
                   subject_hash=subject_hash_for(identity.project_subject), original=control,
                   candidate=dataclasses.replace(control, card_revision=control.card_revision + 1, label="staged"),
                   now=datetime.fromtimestamp(NOW, timezone.utc))
    request = _request([ADMIN])
    result = _verified(await operation.answer(request), request)
    assert result == {"kind": "refused", "code": "card_read_collection_in_transaction", "status": 409}
    assert await collections.load_header(store, collection_id_for(
        service_id=PEER, scope=PROJECT, request_id="zero-1")) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("case, code, status", [("expired", "card_read_collection_expired", 409),
                                                ("foreign_scope", "card_census_scope_forbidden", 403)])
async def test_an_expired_or_foreign_registration_is_refused_signed(tmp_path, case, code, status):
    operation, *_ = await _world(tmp_path)
    request = _request(deadline=NOW) if case == "expired" else _request(scope="other:project:x")
    assert _verified(await operation.answer(request), request) == {"kind": "refused", "code": code,
                                                                     "status": status}


@pytest.mark.asyncio
async def test_a_replayed_nonce_or_an_unsigned_request_is_refused_unsigned(tmp_path):
    operation, *_ = await _world(tmp_path)
    request = _request()
    await operation.answer(request)
    replay = await operation.answer(request)
    assert replay == {"ok": False, "status": 401, "error": {"code": "card_participant_unauthenticated"}}
    tampered = {**_request(), "request_id": "zero-2"}
    assert (await operation.answer(tampered))["error"]["code"] == "card_participant_unauthenticated"

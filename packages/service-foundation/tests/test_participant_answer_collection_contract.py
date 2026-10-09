"""The closed COLLECTION answer contract (W502 lane D card_read_collection_register): exact fields, round trip."""

from __future__ import annotations

import pytest

from service_foundation.coordination.participant_answer import (
    COLLECTION_ANSWER_FIELDS, COLLECTION_ECHO_FIELDS, COLLECTION_REQUEST_FIELDS, AnswerContract,
    ParticipantAnswerRefused, request_digest, sign_participant_answer, verify_participant_answer,
)

SECRET, SCHEMA, NOW = "k" * 40, "card-read-collection-answer.v1", 1_800_000_000
REQUEST = {"schema": "card-read-collection-register.v1", "request_echo": "a" * 32, "scope": "work:project:q",
           "persons": ["user:a", "user:b"], "exclude": [{"subject_hash": "a" * 64, "access_id": "c1"}],
           "actor_subject": "a", "request_id": "zero-1", "deadline": NOW + 300}


def _answer(**changes):
    unsigned = {"schema": SCHEMA, "direction": "hub-to-authority", "audience": "problem-board@1-0",
                "request_digest": request_digest(REQUEST, contract=AnswerContract.COLLECTION),
                **{name: REQUEST[name] for name in COLLECTION_ECHO_FIELDS},
                "result": {"kind": "collection", "ref": {}}}
    unsigned.update(changes)
    proof = sign_participant_answer(unsigned, schema=SCHEMA, secret=SECRET, signer_id="hub", timestamp=str(NOW),
                                    contract=AnswerContract.COLLECTION)
    return {**unsigned, "receipt_proof": proof}


def _verify(answer, request=REQUEST):
    return verify_participant_answer(answer, schema=SCHEMA, secret=SECRET, signer_id="hub",
                                     audience="problem-board@1-0", direction="hub-to-authority", request=request,
                                     now=NOW, contract=AnswerContract.COLLECTION)


def test_the_collection_contract_fields_are_exact():
    assert COLLECTION_REQUEST_FIELDS == set(REQUEST)
    assert COLLECTION_ANSWER_FIELDS == COLLECTION_ECHO_FIELDS | {
        "schema", "direction", "audience", "request_digest", "result"}


def test_a_signed_collection_answer_round_trips_and_binds_every_echoed_field():
    assert _verify(_answer()) == {"kind": "collection", "ref": {}}
    with pytest.raises(ParticipantAnswerRefused, match="answer_binding_mismatch"):
        _verify(_answer(), request={**REQUEST, "persons": ["user:a"]})


def test_the_collection_contract_is_not_another_contract():
    with pytest.raises(ParticipantAnswerRefused):
        request_digest(REQUEST)
    with pytest.raises(ParticipantAnswerRefused):
        request_digest(REQUEST, contract=AnswerContract.CENSUS)

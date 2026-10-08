"""Closed census contract, identical producer bytes, and strict request binding."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from service_foundation.coordination.participant_answer import (
    AnswerContract, CENSUS_ANSWER_FIELDS, CENSUS_REQUEST_FIELDS,
    ParticipantAnswerRefused, request_digest, sign_participant_answer, verify_participant_answer,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures/census_answer_vectors.json").read_text())
CONFIG = {key: value for key, value in FIXTURE["config"].items() if key != "request_schema"}
VECTORS = FIXTURE["vectors"]
CONTRACT = AnswerContract.CENSUS


def verify(answer, request, **overrides):
    return verify_participant_answer(answer, request=request, contract=CONTRACT,
                                     **{**CONFIG, **overrides})


def resign(answer):
    unsigned = copy.deepcopy(answer)
    unsigned.pop("receipt_proof", None)
    proof = sign_participant_answer(unsigned, schema=CONFIG["schema"], secret=CONFIG["secret"],
        signer_id=CONFIG["signer_id"], timestamp=str(CONFIG["now"]), contract=CONTRACT)
    return {**unsigned, "receipt_proof": proof}


@pytest.mark.parametrize("vector", VECTORS, ids=lambda value: value["name"])
def test_census_shared_vectors_preserve_exact_producer_bytes(vector):
    assert request_digest(vector["request"], contract=CONTRACT) == vector["request_digest"]
    assert resign(vector["answer"]) == vector["answer"]
    assert verify(vector["answer"], vector["request"]) == vector["answer"]["result"]
    assert set(vector["answer"]) == CENSUS_ANSWER_FIELDS | {"receipt_proof"}


@pytest.mark.parametrize("field,value", [
    ("schema", "other-answer.v1"), ("direction", "other-direction"), ("audience", "other.test@1"),
    ("request_echo", "f" * 32), ("request_digest", "f" * 64), ("scope", "other-scope"),
    ("persons", ["other-person"]), ("include_catalog", False), ("result", {"kind": "changed"}),
])
def test_every_unsigned_field_tamper_refuses(field, value):
    vector = VECTORS[0]
    with pytest.raises(ParticipantAnswerRefused):
        verify({**vector["answer"], field: value}, vector["request"])


@pytest.mark.parametrize("field,value", [
    ("direction", "other-direction"), ("audience", "other.test@1"),
    ("request_echo", "f" * 32), ("request_digest", "f" * 64), ("scope", "other-scope"),
    ("persons", ["other-person"]), ("persons", ["user:member-2", "user:admin-1"]),
    ("include_catalog", False), ("include_catalog", 1),
])
def test_valid_signature_never_replaces_exact_binding(field, value):
    vector = VECTORS[0]
    answer = resign({**vector["answer"], field: value})
    with pytest.raises(ParticipantAnswerRefused, match="answer_binding_mismatch"):
        verify(answer, vector["request"])


@pytest.mark.parametrize("field", sorted(CENSUS_REQUEST_FIELDS))
def test_every_request_field_is_mandatory(field):
    request = dict(VECTORS[0]["request"])
    request.pop(field)
    with pytest.raises(ParticipantAnswerRefused, match="request_invalid"):
        request_digest(request, contract=CONTRACT)


@pytest.mark.parametrize("field", sorted(CENSUS_ANSWER_FIELDS | {"receipt_proof"}))
def test_every_answer_field_is_mandatory(field):
    answer = copy.deepcopy(VECTORS[0]["answer"])
    answer.pop(field)
    with pytest.raises(ParticipantAnswerRefused, match="answer_malformed"):
        verify(answer, VECTORS[0]["request"])


@pytest.mark.parametrize("extra", [{"service_proof": {}}, {"contract": "census"},
    {"fields": list(CENSUS_REQUEST_FIELDS)}, {"action": "prepare"}])
def test_transport_or_caller_contract_fields_are_not_silently_stripped(extra):
    with pytest.raises(ParticipantAnswerRefused, match="request_invalid"):
        request_digest({**VECTORS[0]["request"], **extra}, contract=CONTRACT)


@pytest.mark.parametrize("contract", [None, "census", "participant", {}, [],
    CENSUS_REQUEST_FIELDS, True])
def test_untrusted_or_extensible_contracts_are_refused(contract):
    vector = VECTORS[0]
    unsigned = {key: value for key, value in vector["answer"].items() if key != "receipt_proof"}
    with pytest.raises(ParticipantAnswerRefused, match="answer_configuration_invalid"):
        request_digest(vector["request"], contract=contract)
    with pytest.raises(ParticipantAnswerRefused, match="answer_configuration_invalid"):
        sign_participant_answer(unsigned, schema=CONFIG["schema"], secret=CONFIG["secret"],
            signer_id=CONFIG["signer_id"], timestamp=str(CONFIG["now"]), contract=contract)
    with pytest.raises(ParticipantAnswerRefused, match="answer_configuration_invalid"):
        verify_participant_answer(vector["answer"], request=vector["request"], contract=contract, **CONFIG)


def test_default_contract_does_not_auto_select_census_from_request_data():
    vector = VECTORS[0]
    with pytest.raises(ParticipantAnswerRefused, match="request_invalid"):
        request_digest(vector["request"])
    with pytest.raises(ParticipantAnswerRefused, match="request_invalid"):
        verify_participant_answer(vector["answer"], request=vector["request"], **CONFIG)
    unsigned = {key: value for key, value in vector["answer"].items() if key != "receipt_proof"}
    with pytest.raises(ParticipantAnswerRefused, match="answer_malformed"):
        sign_participant_answer(unsigned, schema=CONFIG["schema"], secret=CONFIG["secret"],
            signer_id=CONFIG["signer_id"], timestamp=str(CONFIG["now"]))


def test_participant_request_cannot_use_the_census_contract():
    fixture = json.loads((Path(__file__).parent / "fixtures/participant_answer_vectors.json").read_text())
    with pytest.raises(ParticipantAnswerRefused, match="request_invalid"):
        request_digest(fixture["vectors"][0]["request"], contract=CONTRACT)


@pytest.mark.parametrize("vector", VECTORS, ids=lambda value: value["name"])
def test_fresh_attempt_echo_and_request_schema_bind_old_answers(vector):
    for request in ({**vector["request"], "request_echo": "f" * 32},
                    {**vector["request"], "schema": "other-request.v1"}):
        with pytest.raises(ParticipantAnswerRefused, match="answer_binding_mismatch"):
            verify(vector["answer"], request)


@pytest.mark.parametrize("field,value", [("service_id", "other.test@1"),
    ("timestamp", "1800000001"), ("signature", "x" * 43)])
def test_every_proof_field_tamper_refuses(field, value):
    vector = VECTORS[0]
    answer = copy.deepcopy(vector["answer"])
    answer["receipt_proof"][field] = value
    with pytest.raises(ParticipantAnswerRefused):
        verify(answer, vector["request"])


@pytest.mark.parametrize("delta", [-301, 301])
def test_stale_answers_refuse(delta):
    with pytest.raises(ParticipantAnswerRefused, match="answer_stale"):
        verify(VECTORS[0]["answer"], VECTORS[0]["request"], now=CONFIG["now"] + delta)


def test_wrong_key_and_schema_refuse():
    with pytest.raises(ParticipantAnswerRefused, match="answer_proof_invalid"):
        verify(VECTORS[0]["answer"], VECTORS[0]["request"], secret="x" * 40)
    with pytest.raises(ParticipantAnswerRefused, match="answer_malformed"):
        verify(VECTORS[0]["answer"], VECTORS[0]["request"], schema="other-answer.v1")


def test_authenticated_result_is_detached_and_refusal_remains_a_refusal():
    vector = VECTORS[2]
    answer = copy.deepcopy(vector["answer"])
    result = verify(answer, vector["request"])
    assert result["kind"] == "refused"
    result["code"] = "caller-change"
    assert answer["result"]["code"] == "card_census_scope_forbidden"


@pytest.mark.parametrize("value", [1.5, float("nan"), {1: "bad-key"}, "\ud800"])
def test_noncanonical_census_values_refuse_before_hash_or_signature(value):
    vector = VECTORS[0]
    with pytest.raises(ParticipantAnswerRefused, match="request_invalid"):
        request_digest({**vector["request"], "persons": [value]}, contract=CONTRACT)
    with pytest.raises(ParticipantAnswerRefused, match="answer_malformed"):
        verify({**vector["answer"], "result": {"opaque": value}}, vector["request"])

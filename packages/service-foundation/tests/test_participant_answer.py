"""Shared producer/consumer vectors and fail-closed envelope verification."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from service_foundation.coordination.participant_answer import (
    ANSWER_FIELDS, REQUEST_FIELDS, ParticipantAnswerRefused, request_digest,
    sign_participant_answer, verify_participant_answer,
)


FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "participant_answer_vectors.json").read_text())
CONFIG = FIXTURE["config"]
VECTORS = FIXTURE["vectors"]


def _verify(answer, request, **overrides):
    return verify_participant_answer(answer, request=request, **{**CONFIG, **overrides})


def _resign(answer):
    answer = copy.deepcopy(answer)
    answer.pop("receipt_proof", None)
    proof = sign_participant_answer(answer, schema=CONFIG["schema"], secret=CONFIG["secret"],
                                    signer_id=CONFIG["signer_id"], timestamp=str(CONFIG["now"]))
    return {**answer, "receipt_proof": proof}


@pytest.mark.parametrize("vector", VECTORS, ids=lambda v: v["name"])
def test_shared_producer_and_verifier_bytes(vector):
    assert request_digest(vector["request"]) == vector["request_digest"]
    assert _resign(vector["answer"]) == vector["answer"]
    assert _verify(vector["answer"], vector["request"]) == vector["answer"]["result"]
    assert set(vector["answer"]) == ANSWER_FIELDS | {"receipt_proof"}


@pytest.mark.parametrize("tamper", FIXTURE["signed_field_tampers"], ids=lambda v: v["field"])
def test_every_signed_field_tamper_is_refused(tamper):
    vector = VECTORS[0]
    answer = copy.deepcopy(vector["answer"])
    answer[tamper["field"]] = tamper["value"]
    with pytest.raises(ParticipantAnswerRefused):
        _verify(answer, vector["request"])


@pytest.mark.parametrize("vector", VECTORS, ids=lambda v: v["name"])
def test_new_echo_on_retry_refuses_previous_answer(vector):
    request = {**vector["request"], "request_echo": FIXTURE["new_request_echo"]}
    with pytest.raises(ParticipantAnswerRefused, match="answer_binding_mismatch"):
        _verify(vector["answer"], request)


@pytest.mark.parametrize("field,value", [("audience", "other.test"), ("direction", "other-direction")])
def test_validly_signed_wrong_audience_or_direction_is_not_trusted(field, value):
    vector = VECTORS[0]
    answer = _resign({**vector["answer"], field: value})
    with pytest.raises(ParticipantAnswerRefused, match="answer_binding_mismatch"):
        _verify(answer, vector["request"])


@pytest.mark.parametrize("field,value", [
    ("action", "other-action"), ("scope", "other-scope"), ("transaction_id", "other-tx"),
    ("decision", "committed"), ("limit", 2), ("cursor", "other-cursor"),
    ("request_digest", "f" * 64), ("request_echo", "f" * 32),
])
def test_valid_signature_does_not_replace_exact_request_binding(field, value):
    vector = VECTORS[0]
    answer = _resign({**vector["answer"], field: value})
    with pytest.raises(ParticipantAnswerRefused, match="answer_binding_mismatch"):
        _verify(answer, vector["request"])


def test_request_binding_has_json_type_equality_not_python_bool_int_equality():
    vector = VECTORS[2]
    answer = _resign({**vector["answer"], "limit": True})
    with pytest.raises(ParticipantAnswerRefused, match="answer_binding_mismatch"):
        _verify(answer, vector["request"])


@pytest.mark.parametrize("delta", [-301, 301])
def test_expired_or_future_response_is_refused(delta):
    vector = VECTORS[0]
    with pytest.raises(ParticipantAnswerRefused, match="answer_stale"):
        _verify(vector["answer"], vector["request"], now=CONFIG["now"] + delta)


@pytest.mark.parametrize("delta", [-300, 0, 300])
def test_skew_boundary_is_inclusive(delta):
    vector = VECTORS[0]
    assert _verify(vector["answer"], vector["request"], now=CONFIG["now"] + delta)


@pytest.mark.parametrize("secret", ["short", b"short", "x" * 31, b"x" * 31, None, 32, bytearray(32), "\ud800"])
def test_short_or_invalid_keys_refuse_sign_and_verify(secret):
    vector = VECTORS[0]
    unsigned = {k: v for k, v in vector["answer"].items() if k != "receipt_proof"}
    with pytest.raises(ParticipantAnswerRefused, match="answer_configuration_invalid"):
        sign_participant_answer(unsigned, schema=CONFIG["schema"], secret=secret,
                                signer_id=CONFIG["signer_id"], timestamp=str(CONFIG["now"]))
    with pytest.raises(ParticipantAnswerRefused, match="answer_configuration_invalid"):
        _verify(vector["answer"], vector["request"], secret=secret)


def test_wrong_key_or_signer_does_not_verify():
    vector = VECTORS[0]
    with pytest.raises(ParticipantAnswerRefused, match="answer_proof_invalid"):
        _verify(vector["answer"], vector["request"], secret="x" * 40)
    with pytest.raises(ParticipantAnswerRefused, match="answer_binding_mismatch"):
        _verify(vector["answer"], vector["request"], signer_id="other-signer")


@pytest.mark.parametrize("change", [
    {"extra": 1}, {"timestamp": 1800000000}, {"timestamp": "01800000000"},
    {"timestamp": "-1"}, {"timestamp": "1800000000\n"}, {"timestamp": "1.8e9"},
    {"signature": "x" * 44}, {"signature": "!" * 43}, {"signature": None},
])
def test_malformed_proof_is_refused(change):
    vector = VECTORS[0]
    answer = copy.deepcopy(vector["answer"])
    answer["receipt_proof"].update(change)
    with pytest.raises(ParticipantAnswerRefused, match="answer_malformed"):
        _verify(answer, vector["request"])


@pytest.mark.parametrize("field", ["service_id", "timestamp", "signature"])
def test_missing_proof_field_is_refused(field):
    vector = VECTORS[0]
    answer = copy.deepcopy(vector["answer"])
    del answer["receipt_proof"][field]
    with pytest.raises(ParticipantAnswerRefused, match="answer_malformed"):
        _verify(answer, vector["request"])


@pytest.mark.parametrize("field", sorted(ANSWER_FIELDS | {"receipt_proof"}))
def test_missing_answer_field_is_refused(field):
    vector = VECTORS[0]
    answer = copy.deepcopy(vector["answer"])
    del answer[field]
    with pytest.raises(ParticipantAnswerRefused, match="answer_malformed"):
        _verify(answer, vector["request"])


@pytest.mark.parametrize("field", sorted(REQUEST_FIELDS))
def test_missing_request_field_is_not_defaulted(field):
    request = dict(VECTORS[0]["request"])
    del request[field]
    with pytest.raises(ParticipantAnswerRefused, match="request_invalid"):
        request_digest(request)


def test_full_admission_request_must_be_explicitly_selected_before_hashing():
    request = {**VECTORS[0]["request"], "service_proof": {"untrusted": True}}
    with pytest.raises(ParticipantAnswerRefused, match="request_invalid"):
        request_digest(request)
    assert request_digest({k: request[k] for k in REQUEST_FIELDS}) == VECTORS[0]["request_digest"]


@pytest.mark.parametrize("echo", [None, True, "", "ab", "A" * 32, "a" * 129, "a" * 32 + "\n"])
def test_echo_is_bounded_fresh_hex_data(echo):
    with pytest.raises(ParticipantAnswerRefused, match="request_invalid"):
        request_digest({**VECTORS[0]["request"], "request_echo": echo})


@pytest.mark.parametrize("value", [1.5, float("nan"), {1: "bad"}, "\ud800"])
def test_noncanonical_payload_is_refused_without_unsigned_success(value):
    vector = VECTORS[0]
    answer = {**vector["answer"], "result": {"opaque": value}}
    with pytest.raises(ParticipantAnswerRefused, match="answer_malformed"):
        _verify(answer, vector["request"])


@pytest.mark.parametrize("answer", [None, [], {"ok": False, "status": 403}, {"ok": True}])
def test_unsigned_transport_result_is_not_a_definitive_participant_reply(answer):
    with pytest.raises(ParticipantAnswerRefused, match="answer_malformed"):
        _verify(answer, VECTORS[0]["request"])


def test_transport_ok_is_unsigned_and_never_trusted_and_results_are_detached():
    vector = VECTORS[3]
    transport = {"ok": True, "participant_answer": copy.deepcopy(vector["answer"])}
    result = _verify(transport["participant_answer"], vector["request"])
    assert result["kind"] == "refused"
    result["code"] = "changed-by-caller"
    assert transport["participant_answer"]["result"]["code"] == "example_refused"


@pytest.mark.parametrize("override", [
    {"schema": "bad\nframe"}, {"signer_id": "bad\nframe"}, {"audience": ""},
    {"direction": ""}, {"now": True}, {"now": float("nan")}, {"now": float("inf")},
    {"now": -1}, {"now": 10 ** 400}, {"max_skew": True}, {"max_skew": -1}, {"max_skew": 301},
])
def test_invalid_trusted_configuration_is_finite(override):
    with pytest.raises(ParticipantAnswerRefused, match="answer_configuration_invalid"):
        _verify(VECTORS[0]["answer"], VECTORS[0]["request"], **override)


def test_valid_bytes_key_and_floating_point_clock():
    vector = VECTORS[0]
    assert _verify(vector["answer"], vector["request"], secret=CONFIG["secret"].encode(),
                   now=float(CONFIG["now"])) == vector["answer"]["result"]

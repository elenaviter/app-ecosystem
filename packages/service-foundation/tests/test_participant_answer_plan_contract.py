"""The closed PLAN answer contract (W578 card_lifecycle_plan): exact fields, echo binding, round trip."""

from __future__ import annotations

import pytest

from service_foundation.coordination.participant_answer import (
    PLAN_ANSWER_FIELDS, PLAN_ECHO_FIELDS, PLAN_REQUEST_FIELDS, AnswerContract, ParticipantAnswerRefused,
    request_digest, sign_participant_answer, verify_participant_answer,
)

SECRET, SCHEMA, NOW = "k" * 40, "card-lifecycle-plan-answer.v1", 1_800_000_000
REQUEST = {"schema": "card-lifecycle-plan-request.v1", "request_echo": "a" * 32, "scope": "work:project:q",
           "actor_subject": "user:creator", "actor_kind": "caller", "request_id": "r1",
           "creations": [{"ref": "p", "kind": "application_control"}], "updates": []}


def _answer(**changes):
    unsigned = {"schema": SCHEMA, "direction": "hub-to-authority", "audience": "problem-board@1-0",
                "request_digest": request_digest(REQUEST, contract=AnswerContract.PLAN),
                **{name: REQUEST[name] for name in PLAN_ECHO_FIELDS}, "result": {"kind": "plan", "plan": {}}}
    unsigned.update(changes)
    proof = sign_participant_answer(unsigned, schema=SCHEMA, secret=SECRET, signer_id="hub", timestamp=str(NOW),
                                    contract=AnswerContract.PLAN)
    return {**unsigned, "receipt_proof": proof}


def _verify(answer, request=REQUEST):
    return verify_participant_answer(answer, schema=SCHEMA, secret=SECRET, signer_id="hub",
                                     audience="problem-board@1-0", direction="hub-to-authority", request=request,
                                     now=NOW, contract=AnswerContract.PLAN)


def test_the_plan_contract_fields_are_exact():
    assert PLAN_REQUEST_FIELDS == set(REQUEST)
    assert PLAN_ANSWER_FIELDS == PLAN_ECHO_FIELDS | {"schema", "direction", "audience", "request_digest", "result"}


def test_a_signed_plan_answer_round_trips_and_binds_every_echoed_field():
    assert _verify(_answer()) == {"kind": "plan", "plan": {}}
    with pytest.raises(ParticipantAnswerRefused, match="answer_binding_mismatch"):
        _verify(_answer(), request={**REQUEST, "creations": []})


def test_the_plan_contract_is_not_the_participant_or_census_contract():
    with pytest.raises(ParticipantAnswerRefused):
        request_digest(REQUEST)  # the default participant contract refuses these fields

"""Exact write-candidate custody on the real file store."""

from dataclasses import replace

import pytest

from service_foundation.coordination.durable_decision_log import DecisionRefused
from service_foundation.coordination.durable_wire import canonical_json_bytes
from service_foundation.coordination.participant_answer import (
    AnswerContract, request_digest, sign_participant_answer,
)

from connection_hub.delegated_credentials.admission import AdmissionRequest, sign_admission_request
from connection_hub.delegated_credentials.cards.candidate_custody import (
    CandidateCustody, SEAL_CLEANUP_GRACE_SECONDS, candidate_set_id,
)
from connection_hub.delegated_credentials.cards.card_group import (
    group_candidate_value, group_member, hub_group_participant_input,
)
from connection_hub.delegated_credentials.cards.candidate_registration import (
    ANSWER_SCHEMA, OPERATION, REQUEST_SCHEMA, CandidateRegistration, plan_attestation, registration_digest,
)
from connection_hub.delegated_credentials.cards.lifecycle_plan_operation import (
    ANSWER_SCHEMA as PLAN_ANSWER, DIRECTION, REQUEST_SCHEMA as PLAN_REQUEST,
)
from connection_hub.delegated_credentials.cards.participant_operation import ParticipantCaller
from connection_hub.delegated_credentials.cards.transaction_authority_v2 import card_authority_signature
from test_card_transaction_store import _setup, _visible


NOW = 1_800_000_000
PROJECT = "work:project:one"
PEER = "problem-board"
ACTOR = "user:author"
INCARNATION = "project-version-1"
REQUEST = {"id": "removal1", "approval_basis": "a" * 64}


def group_for(before, after):
    members = [group_member(original=before, candidate=after, action="update")]
    projection = hub_group_participant_input(members=members, actor_subject=ACTOR, actor_kind="caller")
    scope = {"tenant": "test", "app": "platform/connection-hub@1-0", "peer": PEER, "actor": ACTOR,
             "project": PROJECT, "incarnation": INCARNATION, "target_scope": projection["target_scope"]}
    return scope, projection, group_candidate_value(members)


@pytest.mark.asyncio
async def test_a_seal_is_hidden_single_copy_and_expiry_is_local(tmp_path):
    store, service, before, after = await _setup(tmp_path)
    now = [NOW]
    custody = CandidateCustody(store=store, service=service, clock=lambda: now[0])
    scope, projection, candidate = group_for(before, after)
    args = dict(scope=scope, request=REQUEST, deadline=NOW + 120, projection=projection, candidate=candidate)
    ref = await custody.seal(**args)
    assert set(ref) == {"schema", "set_id", "count", "root"}
    assert len(canonical_json_bytes(ref)) < 256
    assert await _visible(store, before) == before
    assert await custody.seal(**args) == ref
    assert await custody.discard_unadopted(ref["set_id"]) is False
    assert candidate_set_id(scope, REQUEST, NOW + 121) != ref["set_id"]

    changed_scope, changed_projection, changed_candidate = group_for(before, replace(after, label="different"))
    with pytest.raises(DecisionRefused, match="candidate_custody_conflict"):
        await custody.seal(**{**args, "scope": changed_scope, "projection": changed_projection,
                              "candidate": changed_candidate})

    now[0] = args["deadline"] + SEAL_CLEANUP_GRACE_SECONDS
    report = await custody.sweep_unadopted(limit=1)
    assert report == {"deleted": 1, "skipped": 0}
    assert await _visible(store, before) == before
    with pytest.raises(DecisionRefused, match="candidate_custody_expired"):
        await custody.seal(**args)


class Nonces:
    def __init__(self):
        self.seen = set()

    async def set(self, key, _value, *, ex, nx):
        assert ex > 0 and nx
        if key in self.seen:
            return False
        self.seen.add(key)
        return True


@pytest.mark.asyncio
async def test_registration_requires_peer_admission_and_the_signed_plan(tmp_path):
    store, service, before, after = await _setup(tmp_path)
    _, projection, candidate = group_for(before, after)
    caller = ParticipantCaller(PEER, "r" * 40, "s" * 40, "hub-signer", "pb-audience",
                               "hub-bundle", bind=lambda _scope: None, plan_scope_prefix="work:project:")
    plan_request = {
        "schema": PLAN_REQUEST, "request_echo": "a" * 32, "scope": PROJECT, "actor_subject": ACTOR,
        "actor_kind": "caller", "request_id": "plan-for-removal", "creations": [],
        "updates": [{"kind": "remove_person"}],
    }
    plan = {**plan_request, "schema": PLAN_ANSWER, "direction": DIRECTION, "audience": caller.audience,
            "request_digest": request_digest(plan_request, contract=AnswerContract.PLAN),
            "result": {"kind": "plan", "plan": {"candidate_value": candidate, "participant_input": projection}}}
    seal_request = {"schema": REQUEST_SCHEMA, "request_echo": "b" * 32, "scope": PROJECT,
                    "incarnation": INCARNATION, "request_id": REQUEST["id"], "deadline": NOW + 100}
    proof = sign_participant_answer(plan, schema=PLAN_ANSWER, secret=caller.receipt_secret,
                                    signer_id=caller.receipt_signer_id, timestamp=str(NOW),
                                    contract=AnswerContract.PLAN)
    plan_answer = {**plan, "receipt_proof": proof}
    secret = "hub-attestation-secret-for-fixture-tests"
    attestation = plan_attestation(plan_answer, caller=caller, secret=secret)
    operation = CandidateRegistration(
        callers={PEER: caller}, custody=CandidateCustody(store=store, service=service, clock=lambda: NOW),
        nonces=Nonces(), nonce_prefix="test-candidate-sets:", tenant="test", app="platform/connection-hub@1-0",
        attestation_secret=secret,
    )

    def signed(plan_payload, nonce):
        request = {**seal_request, "plan_answer": plan_payload, "plan_attestation": attestation}
        admitted = AdmissionRequest(resource=caller.hub_resource, operation=OPERATION,
            invocation_id=request["request_echo"], request_digest=registration_digest(request),
            approval_context={"protocol": REQUEST_SCHEMA})
        signature = sign_admission_request(secret=caller.request_secret, service_id=PEER, timestamp=str(NOW),
            nonce=nonce, delegated_token=f"{REQUEST_SCHEMA}:{request['request_echo']}", request=admitted)
        return {**request, "service_proof": {"service_id": PEER, "timestamp": str(NOW),
                                            "nonce": nonce, "signature": signature}}

    answer = await operation.answer(signed(plan_answer, "3" * 32))
    assert answer["ok"] is True
    signed_answer = dict(answer["candidate_set_answer"])
    receipt = signed_answer.pop("receipt_proof")
    assert receipt["signature"] == card_authority_signature(
        signed_answer, secret=caller.receipt_secret, service_id=caller.receipt_signer_id,
        timestamp=str(NOW), protocol=ANSWER_SCHEMA)
    assert await _visible(store, before) == before
    assert (await operation.answer(signed(plan_answer, "3" * 32)))["status"] == 401
    other_plan = {**plan, "result": {**plan["result"], "extra": "different plan"}}
    other_proof = sign_participant_answer(other_plan, schema=PLAN_ANSWER, secret=caller.receipt_secret,
        signer_id=caller.receipt_signer_id, timestamp=str(NOW), contract=AnswerContract.PLAN)
    invalid = {**other_plan, "receipt_proof": other_proof}
    refused = await operation.answer(signed(invalid, "4" * 32))
    assert refused["candidate_set_answer"]["result"]["code"] == "candidate_custody_plan_invalid"

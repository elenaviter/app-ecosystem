"""W578: card_lifecycle_plan, the signed non-writing plan entry for a peer (CodeApp 23:48).

Authenticated like card_census_read; scope by plan_scope_prefix; ONE
request-bound authorization envelope from the project host, validated step
by step before the planner runs; a signed answer under the shared PLAN
answer contract with every request field echoed. The planner here is a
stand-in for W594's; it records what it was given.
"""

from __future__ import annotations

import dataclasses
import os

import pytest

from service_foundation.coordination.participant_answer import AnswerContract, verify_participant_answer

from connection_hub.delegated_credentials.admission import AdmissionRequest, sign_admission_request
from connection_hub.delegated_credentials.cards.lifecycle_plan_operation import (
    ANSWER_SCHEMA, OPERATION, REQUEST_FIELDS, REQUEST_SCHEMA, CardLifecyclePlanOperation, plan_request_digest,
)
from connection_hub.delegated_credentials.cards.participant_operation import ParticipantCaller
from connection_hub.delegated_credentials.project_authorization import (
    PROJECT_CONTROL_CREATE, PROJECT_PERSON_CONTROL_CREATE, LifecyclePlanAuthorization, ProjectAuthorizationDecision,
)

NOW = 1_800_000_000
PEER, PROJECT, CREATOR = "problem-board", "work:project:new", "user:creator"
REQUEST_SECRET, RECEIPT_SECRET = "r" * 40, "s" * 40
GENESIS = [
    {"ref": "p", "kind": "application_control", "identity": {"holder_subject": CREATOR, "issuer_ref": PROJECT},
     "selection": {}, "parent": None},
    {"ref": "c", "kind": "project_person_control", "identity": {"target_subject": CREATOR}, "selection": {},
     "parent": {"ref": "p"}},
    {"ref": "my", "kind": "project_person_my_card", "identity": {"person_subject": CREATOR}, "selection": {},
     "parent": {"ref": "c"}},
]
PLAN = {"candidate_value": {"schema": "connection-hub.card-group.v1", "cards": [], "effects": []},
        "participant_input": {}, "reads": [], "catalog_digest": ""}


class _Nonces:
    def __init__(self):
        self.keys = set()

    async def set(self, key, value, *, ex, nx):
        if key in self.keys:
            return False
        self.keys.add(key)
        return True


class _Port:
    """The project host's whole-plan adapter: one exact decision per step, or a chosen denial."""

    def __init__(self, deny=None, tamper=None):
        self.deny, self.tamper, self.requests = deny, tamper, []

    async def authorize_lifecycle_plan(self, request):
        self.requests.append(request)
        decisions = []
        for step in request.steps:
            single = request.step_request(step)
            decision = (ProjectAuthorizationDecision.deny(single, reason=self.deny[1])
                        if self.deny and step.ref == self.deny[0]
                        else ProjectAuthorizationDecision.allow(single, delegable_grants=("memories:read",)))
            decisions.append((step.ref, decision))
        envelope = LifecyclePlanAuthorization(request=request, decisions=tuple(decisions))
        return self.tamper(envelope) if self.tamper else envelope


class _Planner:
    def __init__(self, result=None):
        self.calls, self.result = [], result or {"ok": True, "plan": PLAN}

    async def __call__(self, host, **kwargs):
        self.calls.append(kwargs)
        return self.result


def _operation(*, prefix="work:project:", port=None, planner=None):
    caller = ParticipantCaller(service_id=PEER, request_secret=REQUEST_SECRET, receipt_secret=RECEIPT_SECRET,
                               receipt_signer_id="connection-hub@1-0", audience="problem-board@1-0",
                               hub_resource="connection-hub@1-0", bind=None, scope_field="project_ref",
                               plan_scope_prefix=prefix)
    port, planner = port or _Port(), planner or _Planner()
    return CardLifecyclePlanOperation(callers={PEER: caller}, authorization=port, planner=planner, host=object(),
                                      nonces=_Nonces(), clock=lambda: NOW), port, planner


def _request(creations=GENESIS, updates=(), *, scope=PROJECT, secret=REQUEST_SECRET, nonce=None, actor=CREATOR):
    data = {"schema": REQUEST_SCHEMA, "request_echo": os.urandom(16).hex(), "scope": scope, "actor_subject": actor,
            "actor_kind": "caller", "request_id": "plan-request-1", "creations": list(creations),
            "updates": list(updates)}
    proof = {"service_id": PEER, "timestamp": str(NOW), "nonce": nonce or os.urandom(12).hex()}
    proof["signature"] = sign_admission_request(
        secret=secret, **proof, delegated_token=f"{REQUEST_SCHEMA}:{data['request_echo']}",
        request=AdmissionRequest(resource="connection-hub@1-0", operation=OPERATION,
                                 invocation_id=data["request_echo"], request_digest=plan_request_digest(data),
                                 approval_context={"protocol": REQUEST_SCHEMA}))
    return {**data, "service_proof": proof}


def _verified(response, request):
    answer = dict(response["plan_answer"])
    return verify_participant_answer(
        answer, schema=ANSWER_SCHEMA, secret=RECEIPT_SECRET, signer_id="connection-hub@1-0",
        audience="problem-board@1-0", direction="hub-to-authority",
        request={name: request[name] for name in REQUEST_FIELDS}, now=NOW, contract=AnswerContract.PLAN)


@pytest.mark.asyncio
async def test_a_genesis_plan_is_authorized_per_step_and_answered_signed():
    operation, port, planner = _operation()
    request = _request()
    response = await operation.answer(request)
    assert response["ok"] is True and _verified(response, request) == {"kind": "plan", "plan": PLAN}
    [asked] = port.requests
    assert [(step.ref, step.operation, step.target_subject) for step in asked.steps] == [
        ("p", PROJECT_CONTROL_CREATE, CREATOR), ("c", PROJECT_PERSON_CONTROL_CREATE, CREATOR),
        ("my", PROJECT_PERSON_CONTROL_CREATE, CREATOR)]
    assert asked.request_digest == plan_request_digest(request) == response["plan_answer"]["request_digest"]
    [call] = planner.calls
    assert call["authorization"].request == asked and call["project_ref"] == PROJECT
    assert call["creations"] == GENESIS and call["actor_subject"] == CREATOR


@pytest.mark.asyncio
async def test_a_denied_step_refuses_the_plan_by_its_own_reason_and_nothing_is_planned():
    operation, _port, planner = _operation(port=_Port(deny=("p", "project_creation_not_allowed")))
    request = _request()
    response = await operation.answer(request)
    assert _verified(response, request) == {"kind": "refused", "code": "project_creation_not_allowed",
                                            "status": 403}
    assert planner.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper,reason", [
    (lambda e: dataclasses.replace(e, decisions=e.decisions[:-1]), "lifecycle_plan_authorization_steps_mismatch"),
    (lambda e: dataclasses.replace(e, request=dataclasses.replace(e.request, request_digest="0" * 64)),
     "lifecycle_plan_authorization_request_mismatch"),
    (lambda e: dataclasses.replace(e, decisions=tuple(
        (ref, dataclasses.replace(d, target_subject="someone-else") if ref == "c" else d) for ref, d in e.decisions)),
     "project_authorization_target_mismatch"),
])
async def test_an_envelope_that_does_not_exactly_cover_this_request_refuses(tamper, reason):
    operation, _port, planner = _operation(port=_Port(tamper=tamper))
    request = _request()
    result = _verified(await operation.answer(request), request)
    assert result["kind"] == "refused" and result["code"] == reason and planner.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["", "work:other:"])
async def test_a_caller_not_entitled_to_the_scope_plans_nothing(prefix):
    operation, port, planner = _operation(prefix=prefix)
    request = _request()
    assert _verified(await operation.answer(request), request)["code"] == "card_plan_scope_forbidden"
    assert port.requests == [] and planner.calls == []


@pytest.mark.asyncio
async def test_the_planners_refusal_is_answered_signed():
    operation, _port, _planner = _operation(planner=_Planner({"ok": False, "error": "card_group_chain_invalid"}))
    request = _request()
    assert _verified(await operation.answer(request), request) == {
        "kind": "refused", "code": "card_group_chain_invalid", "status": 409}


@pytest.mark.asyncio
async def test_an_unknown_creation_kind_is_refused_before_authorization():
    operation, port, _planner = _operation()
    request = _request([{**GENESIS[0], "kind": "admin_role"}])
    assert _verified(await operation.answer(request), request)["code"] == "card_plan_request_invalid"
    assert port.requests == []


@pytest.mark.asyncio
async def test_an_update_is_a_step_with_its_own_target():
    operation, port, _planner = _operation()
    request = _request(GENESIS[1:], updates=[{"kind": "revoke", "target_subject": "invitation:abc",
                                               "access_id": "invitation-control-1"}])
    assert (await operation.answer(request))["ok"] is True
    assert [(s.ref, s.target_subject) for s in port.requests[0].steps][-1] == ("update:0", "invitation:abc")


@pytest.mark.asyncio
async def test_unauthenticated_replayed_or_malformed_requests_get_unsigned_refusals():
    operation, _port, planner = _operation()
    assert (await operation.answer(_request(secret="x" * 40)))["error"]["code"] == "card_participant_unauthenticated"
    request = _request(nonce="n" * 24)
    assert (await operation.answer(request))["ok"] is True
    assert (await operation.answer(request))["error"]["code"] == "card_participant_unauthenticated"
    too_many = _request(GENESIS * 3)
    assert (await operation.answer(too_many))["error"]["code"] == "card_plan_request_invalid"
    assert (await operation.answer({**_request(), "extra": 1}))["error"]["code"] == "card_plan_request_invalid"
    assert len(planner.calls) == 1

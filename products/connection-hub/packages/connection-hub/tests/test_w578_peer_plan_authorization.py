"""W578: the Hub asks the calling project host for a plan's authorization envelope.

A fake Problem Board endpoint verifies the Hub's proof exactly as agreed with
its author (the five-field body, the authority request signer and secret,
protocol ``card-lifecycle-plan-authorize.v1``) and answers per step with the
decision's wire mapping. The Hub turns the answer into a
``LifecyclePlanAuthorization`` or refuses by name.
"""

from __future__ import annotations

import dataclasses

import pytest

from service_foundation.coordination.durable_wire import canonical_json_bytes, sha256_hex

from connection_hub.delegated_credentials.admission import AdmissionRequest, ServiceProof, verify_admission_request
from connection_hub.delegated_credentials.cards.lifecycle_plan_authorization import (
    OPERATION, PROTOCOL, PeerLifecyclePlanAuthorization, plan_authorization_body,
)
from connection_hub.delegated_credentials.cards.participant_descriptor import build_participant_callers
from connection_hub.delegated_credentials.cards.transaction_authority_v2 import PROTOCOL as AUTHORITY_PROTOCOL
from connection_hub.delegated_credentials.project_authorization import (
    PROJECT_CONTROL_CREATE, PROJECT_PERSON_CONTROL_CREATE, PROJECT_PERSON_CONTROL_REVOKE,
    LifecyclePlanAuthorization, LifecyclePlanAuthorizationRequest, LifecyclePlanStep, ProjectAuthorizationDecision,
    ProjectAuthorizationError, ProjectControlLocator,
)
from test_card_participant_descriptor import PB_BUNDLE, SIGNER_SECRET, _connections, _resolve
from test_card_participant_operation import PEER

ACTOR, PROJECT, REQUEST, DIGEST = "platform-user-1", "work:project:quickstart", "plan-1", "d" * 64
NOW = 1_800_000_000
STEPS = (LifecyclePlanStep("p", PROJECT_CONTROL_CREATE, ACTOR),
         LifecyclePlanStep("c", PROJECT_PERSON_CONTROL_CREATE, ACTOR),
         LifecyclePlanStep("update:0", PROJECT_PERSON_CONTROL_REVOKE, "invited-person"))


def _request(**changes):
    values = dict(actor_subject=ACTOR, project_ref=PROJECT, request_id=REQUEST, request_digest=DIGEST, steps=STEPS)
    values.update(changes)
    return LifecyclePlanAuthorizationRequest(**values)


def _decisions(request, *, refs=None, digest=None):
    out = []
    for step in request.steps:
        decision = ProjectAuthorizationDecision.allow(request.step_request(step), delegable_grants=("memories:read",))
        if digest is not None:
            decision = dataclasses.replace(decision, request_digest=digest)
        out.append({"ref": step.ref, "decision": decision.to_dict()})
    if refs is not None:
        out = [item for item in out if item["ref"] in refs]
    return out


class _Board:
    """Problem Board's project_lifecycle_plan_authorize, verifying the agreed frame."""

    def __init__(self, answer=None, *, raises=None):
        self.answer, self.raises, self.calls = answer, raises, []

    async def __call__(self, *, bundle_id, operation, data):
        # As the operation route sees it: the signed body under "data", the identity hints given as None
        # (else the platform adds the session's), and the answer inside the route's envelope.
        signed = data["data"]
        assert data == {"data": signed, "user_id": None, "fingerprint": None}
        data = signed
        self.calls.append((bundle_id, operation, data))
        if self.raises:
            raise self.raises
        body = {name: value for name, value in data.items() if name != "service_proof"}
        assert set(body) == {"actor_subject", "project_ref", "request_id", "request_digest", "steps"}
        proof = ServiceProof(**data["service_proof"])
        verdict = verify_admission_request(
            secret=SIGNER_SECRET, proof=proof, delegated_token=f"{PROTOCOL}:{body['request_id']}",
            request=AdmissionRequest(resource=bundle_id, operation=OPERATION, invocation_id=body["request_id"],
                                     request_digest=sha256_hex(canonical_json_bytes(body)),
                                     approval_context={"protocol": PROTOCOL}),
            now=NOW)
        assert verdict.allowed, verdict
        return {"status": "ok", "bundle_id": bundle_id,
                operation: self.answer(body) if callable(self.answer) else self.answer}


def _port(board):
    return PeerLifecyclePlanAuthorization(call=board, bundle_id=PB_BUNDLE, signer_id="connection-hub",
                                          secret=SIGNER_SECRET, clock=lambda: NOW)


def test_the_decision_wire_mapping_round_trips_with_and_without_a_project_control():
    request = _request().step_request(STEPS[1])
    plain = ProjectAuthorizationDecision.allow(request, delegable_grants=("a",), evidence={"why": "admin"})
    located = ProjectAuthorizationDecision.allow(
        request, project_control=ProjectControlLocator(control_id="ctl-1", holder_subject=ACTOR))
    denied = ProjectAuthorizationDecision.deny(request, reason="project_last_admin")
    for decision in (plain, located, denied):
        assert ProjectAuthorizationDecision.from_mapping(decision.to_dict()) == decision
    assert "project_control" not in plain.to_dict() and plain.to_dict()["request_digest"] == DIGEST


@pytest.mark.parametrize("change", [
    {"unknown": 1}, {"allowed": "yes"}, {"platform_admin": 1}, {"request_digest": None},
    {"delegable_grants": "a"}, {"evidence": []}, {"project_control": {"control_id": "x"}},
])
def test_a_malformed_decision_mapping_is_refused(change):
    mapping = {**ProjectAuthorizationDecision.allow(_request().step_request(STEPS[0])).to_dict(), **change}
    with pytest.raises(ProjectAuthorizationError):
        ProjectAuthorizationDecision.from_mapping(mapping)


@pytest.mark.parametrize("missing", ["allowed", "actor_subject", "request_id"])
def test_a_decision_mapping_without_a_required_field_is_refused(missing):
    mapping = ProjectAuthorizationDecision.allow(_request().step_request(STEPS[0])).to_dict()
    del mapping[missing]
    with pytest.raises(ProjectAuthorizationError, match="project_authorization_decision_invalid"):
        ProjectAuthorizationDecision.from_mapping(mapping)


@pytest.mark.asyncio
async def test_the_board_verifies_the_agreed_frame_and_its_answer_becomes_the_envelope():
    request = _request()
    board = _Board(lambda body: {"ok": True, "decisions": _decisions(request)})
    envelope = await _port(board).authorize_lifecycle_plan(request)
    assert isinstance(envelope, LifecyclePlanAuthorization) and envelope.allowed
    envelope.validate_for(request)
    bundle_id, operation, data = board.calls[0]
    assert (bundle_id, operation) == (PB_BUNDLE, OPERATION)
    assert {k: v for k, v in data.items() if k != "service_proof"} == plan_authorization_body(request)
    assert [step["ref"] for step in data["steps"]] == ["p", "c", "update:0"]  # ordered as planned


@pytest.mark.asyncio
async def test_the_proof_never_verifies_as_a_transaction_authority_request():
    request = _request()
    board = _Board(lambda body: {"ok": True, "decisions": _decisions(request)})
    await _port(board).authorize_lifecycle_plan(request)
    _, _, data = board.calls[0]
    body = {name: value for name, value in data.items() if name != "service_proof"}
    verdict = verify_admission_request(
        secret=SIGNER_SECRET, proof=ServiceProof(**data["service_proof"]),
        delegated_token=f"{AUTHORITY_PROTOCOL}:{REQUEST}",
        request=AdmissionRequest(resource=PB_BUNDLE, operation=OPERATION, invocation_id=REQUEST,
                                 request_digest=sha256_hex(canonical_json_bytes(body)),
                                 approval_context={"protocol": AUTHORITY_PROTOCOL}),
        now=NOW)
    assert not verdict.allowed


@pytest.mark.asyncio
@pytest.mark.parametrize("answer,reason", [
    (lambda r: {"ok": True, "decisions": _decisions(r, refs={"p", "c"})}, "lifecycle_plan_authorization_steps_mismatch"),
    (lambda r: {"ok": True, "decisions": _decisions(r, digest="e" * 64)},
     "project_authorization_request_digest_mismatch"),
    (lambda r: {"ok": True, "decisions": _decisions(r, digest="")}, "project_authorization_request_digest_mismatch"),
    (lambda r: {"ok": False, "error": "project_actor_role_not_administrative"}, "card_plan_authorization_refused"),
    (lambda r: {"ok": True, "decisions": {"p": {}}}, "card_plan_authorization_invalid"),
    (lambda r: {"ok": True, "decisions": [{"ref": "p"}]}, "card_plan_authorization_invalid"),
    (lambda r: ["not", "a", "mapping"], "card_plan_authorization_invalid"),
])
async def test_an_answer_that_is_not_exactly_this_plans_envelope_is_refused(answer, reason):
    request = _request()
    with pytest.raises(ProjectAuthorizationError, match=reason):
        await _port(_Board(lambda body: answer(request))).authorize_lifecycle_plan(request)


@pytest.mark.asyncio
async def test_a_transport_failure_is_unavailable_by_name():
    with pytest.raises(ProjectAuthorizationError, match="card_plan_authorization_unavailable"):
        await _port(_Board(raises=RuntimeError("connection reset"))).authorize_lifecycle_plan(_request())


@pytest.mark.asyncio
async def test_each_descriptor_built_caller_asks_its_own_host_under_its_authority_signer():
    request = _request()
    board = _Board(lambda body: {"ok": True, "decisions": _decisions(request)})
    built = await build_participant_callers(_connections(plan_scope_prefix="work:project:"),
                                            resolve_secret=_resolve, call=board, card_store=None, card_service=None)
    port = built.callers[PEER].plan_authorization
    assert isinstance(port, PeerLifecyclePlanAuthorization)
    port._clock = lambda: NOW  # the fake board verifies at a fixed time
    assert (await port.authorize_lifecycle_plan(request)).allowed
    assert board.calls[0][0] == PB_BUNDLE and board.calls[0][2]["service_proof"]["service_id"] == "connection-hub"


def _platform_route(answer):
    """The operation route as the platform runs it: a hint not given is filled from the session; the
    operation receives the rest as its arguments; its answer comes back inside the route's envelope."""
    seen = {}

    async def route(*, bundle_id, operation, data):
        arguments = dict(data)
        arguments.setdefault("user_id", "session-user")
        arguments.setdefault("fingerprint", "session-fingerprint")
        seen.update(body=arguments.pop("data"), hints=arguments)
        return {"status": "ok", "bundle_id": bundle_id, operation: answer(seen["body"])}

    return route, seen


@pytest.mark.asyncio
async def test_the_platform_adds_no_session_identity_to_the_signed_plan_body():
    """Live 2026-10-09 23:01Z (managed edit): a flat body took the session's user_id; the plan body is the same."""
    request = _request()
    route, seen = _platform_route(lambda body: {"ok": True, "decisions": _decisions(request)})
    authorization = await _port(route).authorize_lifecycle_plan(request)
    assert seen["hints"] == {"user_id": None, "fingerprint": None}
    assert set(seen["body"]) == {"actor_subject", "project_ref", "request_id", "request_digest", "steps",
                                 "service_proof"}
    assert [ref for ref, _ in authorization.decisions] == [step.ref for step in request.steps]


@pytest.mark.asyncio
async def test_a_raw_answer_from_a_local_call_is_read_as_before():
    request = _request()

    async def local(*, bundle_id, operation, data):  # a local (non-route) call answers the operation's own body
        return {"ok": True, "decisions": _decisions(request)}

    authorization = await _port(local).authorize_lifecycle_plan(request)
    assert len(authorization.decisions) == len(request.steps)


@pytest.mark.asyncio
async def test_a_route_answer_outside_the_contract_is_invalid_never_refused_or_allowed():
    async def route(*, bundle_id, operation, data):
        return {"status": "error", "detail": "synthetic"}

    with pytest.raises(ProjectAuthorizationError, match="card_plan_authorization_invalid"):
        await _port(route).authorize_lifecycle_plan(_request())

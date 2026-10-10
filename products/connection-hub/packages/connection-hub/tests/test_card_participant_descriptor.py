"""W502: the participant caller descriptor, built into the operation the entrypoint serves.

The end-to-end case runs the Hub operation over callers built from a
descriptor, against a fake Problem Board entrypoint that authenticates the
Hub's request proof, reads the scope as its ``project_ref`` request field and
answers in PR579's ``{ok, authority}`` shape.
"""

from __future__ import annotations

import time

import pytest

from service_foundation.coordination.durable_wire import canonical_json_bytes, sha256_hex

from connection_hub.delegated_credentials.admission import AdmissionRequest, ServiceProof, verify_admission_request
from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.composition import card_transaction_coordinator
from connection_hub.delegated_credentials.cards.participant_descriptor import (
    build_participant_callers, participant_caller_descriptors,
)
from connection_hub.delegated_credentials.cards.participant_operation import CardTransactionParticipantOperation
from connection_hub.delegated_credentials.cards.transaction_authority_v2 import (
    PROTOCOL, TransactionAuthorityRefused, card_authority_signature,
)
from test_card_transaction_store import _Applier
from test_card_participant_operation import (
    AUTHORITY, AUTHORITY_SECRET, NOW, PEER, PROJECT, RECEIPT_SECRET, REQUEST_SECRET, TX, _request, _verified,
    _world,
)

SIGNER_SECRET = "h" * 40
PB_BUNDLE, PB_OPERATION = "problem-board@1-0", "project_card_transaction_authority"
SECRETS = {"refs.request": REQUEST_SECRET, "refs.receipt": RECEIPT_SECRET, "refs.authority": AUTHORITY_SECRET,
           "refs.signer": SIGNER_SECRET}
PB_CODES = {"authority_decision_pending": "work_card_transaction_authority_decision_pending",
            "authority_late_stage": "work_card_transaction_authority_late_stage",
            "authority_transaction_unknown": "work_card_transaction_decision_unknown"}


def _connections(**changes):
    caller = {
        "request_secret_ref": "refs.request", "receipt_secret_ref": "refs.receipt",
        "receipt_signer_id": "connection-hub@1-0", "audience": "problem-board@1-0",
        "hub_resource": "connection-hub@1-0", "scope_field": "project_ref", "census_scope_prefix": "work:project:",
        "authority": {
            "service_id": AUTHORITY.service_id, "audience": AUTHORITY.audience, "secret_ref": "refs.authority",
            "request_signer_id": "connection-hub", "request_secret_ref": "refs.signer",
            "binding": {"bundle_id": PB_BUNDLE, "operation": PB_OPERATION, "response_key": "authority",
                        "refusals": {code: reason for reason, code in PB_CODES.items()}},
        },
    }
    caller.update(changes)
    return {"card_transactions": {"enabled": True, "callers": {PEER: caller}}}


async def _resolve(reference):
    return SECRETS.get(reference, "")


def test_a_well_formed_descriptor_is_read_and_a_malformed_one_is_left_out():
    assert set(participant_caller_descriptors(_connections())) == {PEER}
    assert participant_caller_descriptors(_connections(audience="")) == {}
    pinned = _connections()
    pinned["card_transactions"]["callers"][PEER]["authority"]["binding"]["request_fields"] = {"project_ref": "x"}
    assert participant_caller_descriptors(pinned) == {}  # the scope is never fixed by the descriptor
    assert participant_caller_descriptors({"card_transactions": {"enabled": True}}) == {}


@pytest.mark.asyncio
async def test_a_caller_with_a_missing_or_short_secret_is_left_out():
    async def short(reference):
        return "short" if reference == "refs.receipt" else SECRETS[reference]

    built = await build_participant_callers(_connections(), resolve_secret=short, call=None, card_store=None,
                                            card_service=None)
    assert built.callers == {} and built.authorities == {}


class _PB:
    """Problem Board's entrypoint, as PR579 serves it: proof-authenticated, scope as project_ref."""

    def __init__(self, world):
        self.world, self.scopes = world, []

    async def __call__(self, *, bundle_id, operation, data):
        # As the operation route sees it: the signed body under "data", the identity hints given as None,
        # and the answer inside the route's envelope.
        assert data["user_id"] is None and data["fingerprint"] is None and set(data) == {"data", "user_id",
                                                                                          "fingerprint"}
        return {"status": "ok", operation: await self._answer(bundle_id=bundle_id, operation=operation,
                                                              data=data["data"])}

    async def _answer(self, *, bundle_id, operation, data):
        assert (bundle_id, operation) == (PB_BUNDLE, PB_OPERATION)
        unsigned = {name: value for name, value in data.items() if name != "service_proof"}
        verdict = verify_admission_request(
            secret=SIGNER_SECRET, proof=ServiceProof(**data["service_proof"]),
            delegated_token=f"{PROTOCOL}:{data['request_echo']}",
            request=AdmissionRequest(resource=PB_BUNDLE, operation=PB_OPERATION, invocation_id=data["request_echo"],
                                     request_digest=sha256_hex(canonical_json_bytes(unsigned)),
                                     approval_context={"protocol": PROTOCOL}))
        assert verdict.allowed, verdict
        self.scopes.append(data["project_ref"])
        try:
            response = await self.world.fetch_for(data["project_ref"])(
                data["transaction_id"], data["phase"], data["request_echo"])
        except TransactionAuthorityRefused as exc:
            return {"ok": False, "status": 409, "error": {"code": PB_CODES[exc.reason]}}
        # Signed at the real clock: the descriptor-built reader verifies against it.
        stamp = int(time.time())
        unsigned = {name: value for name, value in response.items() if name != "authority_proof"}
        if unsigned["decided_at"] is not None:
            unsigned["decided_at"] = stamp
        proof = {"service_id": AUTHORITY.service_id, "timestamp": str(stamp),
                 "signature": card_authority_signature(unsigned, secret=AUTHORITY_SECRET,
                                                       service_id=AUTHORITY.service_id, timestamp=str(stamp))}
        return {"ok": True, "authority": {**unsigned, "authority_proof": proof}}


@pytest.mark.asyncio
async def test_the_descriptor_built_operation_prepares_and_finishes_through_problem_boards_endpoint(tmp_path):
    world, before, after = await _world(tmp_path)
    pb = _PB(world)
    built = await build_participant_callers(_connections(), resolve_secret=_resolve, call=pb,
                                            card_store=world.store, card_service=world.service)
    assert set(built.callers) == {PEER}
    assert built.callers[PEER].census_scope_prefix == "work:project:"

    class _Persistence:
        card_store, card_service = world.store, world.service

    class _NoLocal:
        async def read(self, transaction_id):
            return None

    card_transaction_coordinator(persistence=_Persistence(), decisions=_NoLocal(), grant_store=None, policies=None,
                                 authorities=built.authorities)
    world.service.bind_effect_applier(_Applier())  # composition bound the real targets; this test has no stores
    operation = CardTransactionParticipantOperation(callers=built.callers, card_store=world.store,
                                                    nonces=world.nonces, enabled=True, clock=lambda: NOW)
    request = _request("prepare")
    assert _verified(await operation.answer(request), request)["kind"] == "receipt"
    assert set(pb.scopes) == {PROJECT}  # the verified request scope reached PB as project_ref
    world.decision = "committed"
    request = _request("finish", decision="committed")
    finished = _verified(await operation.answer(request), request)
    assert finished["kind"] == "receipt", finished
    assert (await tx.state(world.store, transaction_id=TX))["state"] == "committed"
    request = _request("read_pending", scope="work:project:other")
    assert _verified(await operation.answer(request), request)["code"] == "transaction_unknown"

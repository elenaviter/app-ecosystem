"""W585: the Hub builds the bound-issuance context only from a committed decision."""

from __future__ import annotations

import hashlib
import importlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from service_foundation.coordination.durable_decision_log import DecisionRecord, Intent

from connection_hub.delegated_credentials.cards.bound_issuance import (
    CUSTODY_NAMESPACE,
    BoundIssuanceRefused,
    build_issuance_context,
    integration_user_id,
    require_production_custody,
)
from connection_hub.delegated_credentials.cards.participant_effects import EffectBinding

TX = hashlib.sha256(b"tx").hexdigest()
EXPIRES = 1_900_000_000


def _intent(actor: str = "user:admin-1") -> Intent:
    return Intent(actor=actor, request_id="req-1", context="ctx", payload_digest=hashlib.sha256(b"p").hexdigest(),
                  participants=("connection-hub",), expires_at=datetime.now(timezone.utc) + timedelta(hours=1))


def _binding(intent_digest: str, *, kind: str = "grant_binding", revision: int = 4) -> EffectBinding:
    receipt = {"transaction_id": TX, "intent_digest": intent_digest, "access_id": "aut_card",
               "after": {"access_id": "aut_card", "card_revision": revision}}
    return EffectBinding(TX, kind, "slot-a", hashlib.sha256(b"e").hexdigest(),
                         hashlib.sha256(b"r").hexdigest(), json.dumps(receipt, sort_keys=True))


PAYLOAD = {"access_id": "aut_card", "slot": "slot-a", "expires_at": EXPIRES, "operations": [],
           "resource_grants": {}, "resource_operations": {}, "named_services": {}}


class _Decisions:
    def __init__(self, record):
        self.record = record

    async def read(self, transaction_id):
        return self.record if self.record and self.record.transaction_id == transaction_id else None


def _record(intent: Intent, state: str = "committed") -> DecisionRecord:
    return DecisionRecord(transaction_id=TX, intent=intent, state=state, prepared={}, finished={})


@pytest.mark.asyncio
async def test_the_context_takes_its_actor_from_the_committed_intent_only() -> None:
    intent = _intent()
    context = await build_issuance_context(binding=_binding(intent.digest), payload=PAYLOAD,
                                           decisions=_Decisions(_record(intent)), tenant="t", project="p")
    assert context.actor == "user:admin-1"
    assert (context.transaction_id, context.slot, context.access_id) == (TX, "slot-a", "aut_card")
    assert (context.target_incarnation, context.expires_at) == (4, EXPIRES)
    assert (context.tenant, context.project) == ("t", "p")


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["aborted", "prepared"])
async def test_an_undecided_or_aborted_transaction_never_issues(state) -> None:
    intent = _intent()
    with pytest.raises(BoundIssuanceRefused, match="issuance_decision_not_committed"):
        await build_issuance_context(binding=_binding(intent.digest), payload=PAYLOAD,
                                     decisions=_Decisions(_record(intent, state)), tenant="t", project="p")
    with pytest.raises(BoundIssuanceRefused, match="issuance_decision_not_committed"):
        await build_issuance_context(binding=_binding(intent.digest), payload=PAYLOAD,
                                     decisions=_Decisions(None), tenant="t", project="p")


@pytest.mark.asyncio
async def test_a_receipt_from_another_intent_is_refused() -> None:
    committed, other = _intent(), _intent(actor="user:someone-else")
    with pytest.raises(BoundIssuanceRefused, match="issuance_intent_mismatch"):
        await build_issuance_context(binding=_binding(other.digest), payload=PAYLOAD,
                                     decisions=_Decisions(_record(committed)), tenant="t", project="p")


@pytest.mark.asyncio
@pytest.mark.parametrize(("change", "reason"), [
    ({"slot": "slot-b"}, "issuance_binding_mismatch"),
    ({"access_id": "aut_other"}, "issuance_binding_mismatch"),
    ({"expires_at": 0}, "issuance_deadline_invalid"),
    ({"expires_at": "1900000000"}, "issuance_deadline_invalid"),
])
async def test_the_payload_must_match_the_effect_and_carry_a_deadline(change, reason) -> None:
    intent = _intent()
    with pytest.raises(BoundIssuanceRefused, match=reason):
        await build_issuance_context(binding=_binding(intent.digest), payload={**PAYLOAD, **change},
                                     decisions=_Decisions(_record(intent)), tenant="t", project="p")


@pytest.mark.asyncio
async def test_only_a_grant_binding_effect_issues() -> None:
    intent = _intent()
    with pytest.raises(BoundIssuanceRefused, match="issuance_effect_kind_invalid"):
        await build_issuance_context(binding=_binding(intent.digest, kind="credential_lifetime"), payload=PAYLOAD,
                                     decisions=_Decisions(_record(intent)), tenant="t", project="p")


@pytest.mark.asyncio
async def test_the_sdk_accepts_the_context_when_it_is_importable() -> None:
    try:
        sdk = importlib.import_module("kdcube_ai_app.auth.bundle.session_issuance")
    except ImportError:
        pytest.skip("the SDK with session_issuance is not on the path")
    intent = _intent()
    context = await build_issuance_context(binding=_binding(intent.digest), payload=PAYLOAD,
                                           decisions=_Decisions(_record(intent)), tenant="t", project="p")
    bound = sdk.IssuanceContext.from_context(context)
    assert bound.actor == "user:admin-1" and bound.target_incarnation == 4


def test_the_custody_namespace_is_pinned() -> None:
    # Ops 16:42: one constant in composition and purge; the SDK parser has no dots.
    assert CUSTODY_NAMESPACE == "connection-hub-issuance-custody"


class _Custody:
    def __init__(self, namespace=CUSTODY_NAMESPACE):
        self.namespace = namespace

    async def create(self, *, secret_ref, value, expires_at):
        return True

    async def get(self, *, secret_ref):
        return None


@pytest.mark.parametrize("backend", ["in-memory", "secrets-service", "secrets-file", ""])
def test_production_refuses_non_durable_custody(backend) -> None:
    with pytest.raises(BoundIssuanceRefused, match="issuance_custody_not_durable"):
        require_production_custody(_Custody(), backend=backend, production=True)


@pytest.mark.parametrize("backend", ["aws-sm", "host-vault"])
def test_production_accepts_a_durable_backend_in_the_one_namespace(backend) -> None:
    custody = _Custody()
    assert require_production_custody(custody, backend=backend, production=True) is custody
    with pytest.raises(BoundIssuanceRefused, match="issuance_custody_namespace_invalid"):
        require_production_custody(_Custody("connection-hub.issuance-custody"), backend=backend, production=True)
    with pytest.raises(BoundIssuanceRefused, match="issuance_custody_unavailable"):
        require_production_custody(object(), backend=backend, production=True)


def test_development_may_use_a_local_backend() -> None:
    custody = _Custody()
    assert require_production_custody(custody, backend="in-memory", production=False) is custody


def test_the_integration_subject_is_client_and_grantor() -> None:
    assert integration_user_id("client-1", "user:owner") == "integration:client-1:user:owner"
    for bad in (("", "user:owner"), ("client 1", "user:owner"), ("client:1", "user:owner"), ("c", " u")):
        with pytest.raises(BoundIssuanceRefused, match="issuance_user_invalid"):
            integration_user_id(*bad)

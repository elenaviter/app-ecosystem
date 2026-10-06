"""W585: the Hub builds the bound-issuance context only from a committed v2 decision."""

from __future__ import annotations

import hashlib
import importlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from service_foundation.coordination.durable_decision_log import DecisionRecord, IntentDraft

from connection_hub.delegated_credentials.cards import bound_issuance
from connection_hub.delegated_credentials.cards.bound_issuance import (
    CUSTODY_NAMESPACE,
    BoundIssuanceRefused,
    build_issuance_context,
    integration_user_id,
    require_production_custody,
)
from connection_hub.delegated_credentials.cards.card_participant import PARTICIPANT
from connection_hub.delegated_credentials.cards.participant_effects import EffectBinding, _canonical, _digest

TX = hashlib.sha256(b"tx").hexdigest()
EXPIRES = 1_900_000_000
PAYLOAD = {"access_id": "aut_card", "slot": "slot-a", "expires_at": EXPIRES, "operations": [],
           "resource_grants": {}, "resource_operations": {}, "named_services": {}}


def _hub_input(actor="user:admin-1", kind="caller"):
    return {"participant": PARTICIPANT, "binding_kind": "connection-hub.card", "binding_ref": "aut_card",
            "target_scope": "s" * 64, "target_incarnation": 3, "action": "update", "before_revision": 3,
            "candidate_revision": 4, "candidate_digest": "c" * 64, "dependency_revisions": {},
            "actor_subject": actor, "actor_kind": kind, "provisioning": {}}


def _intent(**hub):
    draft = IntentDraft(replay_scope="test", request_id="req-1",
                        expires_at=int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
                        participants=(PARTICIPANT,), payload={"participant_inputs": {PARTICIPANT: _hub_input(**hub)}})
    return draft.bind(TX, 1)


def _binding(intent_digest, *, kind="grant_binding", payload=PAYLOAD, receipt_extra=None, transaction_id=TX):
    receipt = {"transaction_id": transaction_id, "intent_digest": intent_digest, "access_id": "aut_card",
               "before": {"access_id": "aut_card", "card_revision": 3},
               "after": {"access_id": "aut_card", "card_revision": 4}, **(receipt_extra or {})}
    effect_digest = _digest(_canonical({"kind": kind, "key": "slot-a", "payload": payload}))
    return EffectBinding(transaction_id, kind, "slot-a", effect_digest, hashlib.sha256(b"r").hexdigest(),
                         json.dumps(receipt, sort_keys=True))


class _Decisions:
    def __init__(self, record):
        self.record = record

    async def read(self, transaction_id):
        return self.record


def _record(intent, state="committed"):
    return DecisionRecord(intent, state, {}, {})


async def _build(intent, *, binding=None, payload=PAYLOAD, state="committed", record=...):
    return await build_issuance_context(
        binding=binding or _binding(intent.digest), payload=payload,
        decisions=_Decisions(_record(intent, state) if record is ... else record), tenant="t", project="p")


@pytest.mark.asyncio
async def test_the_actor_is_the_committed_projections_only() -> None:
    intent = _intent()
    context = await _build(intent)
    assert context.actor == "user:admin-1"
    assert (context.transaction_id, context.slot, context.access_id) == (TX, "slot-a", "aut_card")
    assert (context.target_incarnation, context.expires_at) == (4, EXPIRES)


@pytest.mark.asyncio
async def test_an_actor_in_the_receipt_is_never_used() -> None:
    intent = _intent()
    binding = _binding(intent.digest, receipt_extra={"actor": "user:forged", "actor_subject": "user:forged"})
    assert (await _build(intent, binding=binding)).actor == "user:admin-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("hub", [{"actor": ""}, {"kind": "unknown"}])
async def test_a_projection_without_a_named_actor_refuses(hub) -> None:
    intent = _intent(**hub)
    with pytest.raises(BoundIssuanceRefused, match="issuance_actor_invalid"):
        await _build(intent)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["aborted", "prepared"])
async def test_an_undecided_or_aborted_transaction_never_issues(state) -> None:
    intent = _intent()
    with pytest.raises(BoundIssuanceRefused, match="issuance_decision_not_committed"):
        await _build(intent, state=state)
    with pytest.raises(BoundIssuanceRefused, match="issuance_decision_not_committed"):
        await _build(intent, record=None)


@pytest.mark.asyncio
async def test_a_receipt_from_another_intent_or_transaction_is_refused() -> None:
    committed, other = _intent(), _intent(actor="user:someone-else")
    with pytest.raises(BoundIssuanceRefused, match="issuance_intent_mismatch"):
        await _build(committed, binding=_binding(other.digest))
    other_tx = hashlib.sha256(b"other").hexdigest()
    with pytest.raises(BoundIssuanceRefused, match="issuance_intent_mismatch"):
        await _build(committed, binding=_binding(committed.digest, transaction_id=other_tx))


@pytest.mark.asyncio
@pytest.mark.parametrize(("change", "reason"), [
    ({"slot": "slot-b"}, "issuance_binding_mismatch"),
    ({"access_id": "aut_other"}, "issuance_binding_mismatch"),
    ({"expires_at": EXPIRES + 10**8}, "issuance_binding_mismatch"),
])
async def test_a_payload_that_is_not_the_decided_effect_is_refused(change, reason) -> None:
    # Ops F3: the deadline (or any field) cannot differ from what the effect digest decided.
    intent = _intent()
    with pytest.raises(BoundIssuanceRefused, match=reason):
        await _build(intent, payload={**PAYLOAD, **change})


@pytest.mark.asyncio
@pytest.mark.parametrize("expires_at", [0, "1900000000"])
async def test_a_decided_but_invalid_deadline_is_refused(expires_at) -> None:
    intent = _intent()
    payload = {**PAYLOAD, "expires_at": expires_at}
    with pytest.raises(BoundIssuanceRefused, match="issuance_deadline_invalid"):
        await _build(intent, binding=_binding(intent.digest, payload=payload), payload=payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("after", [
    {"access_id": "aut_card", "card_revision": True}, {"access_id": "aut_card", "card_revision": "4"},
    {"access_id": "aut_card", "card_revision": 4.0}, {"access_id": "aut_card", "card_revision": 0},
    {"access_id": "aut_card", "card_revision": 5}, {"access_id": "aut_other", "card_revision": 4},
])
async def test_the_incarnation_is_the_exact_committed_revision(after) -> None:
    # Ops F4: no coercion; exactly before + 1 for the same Card.
    intent = _intent()
    with pytest.raises(BoundIssuanceRefused, match="issuance_binding_mismatch"):
        await _build(intent, binding=_binding(intent.digest, receipt_extra={"after": after}))


@pytest.mark.asyncio
async def test_a_zero_incarnation_is_refused_even_when_one_past_its_base() -> None:
    intent = _intent()
    extra = {"before": {"access_id": "aut_card", "card_revision": -1}, "after": {"access_id": "aut_card", "card_revision": 0}}
    with pytest.raises(BoundIssuanceRefused, match="issuance_binding_mismatch"):
        await _build(intent, binding=_binding(intent.digest, receipt_extra=extra))


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["transaction_id", "effect_digest", "receipt_digest"])
async def test_binding_identities_must_be_64_hex(field) -> None:
    intent = _intent()
    good = _binding(intent.digest)
    values = {"transaction_id": good.transaction_id, "kind": good.kind, "key": good.key,
              "effect_digest": good.effect_digest, "receipt_digest": good.receipt_digest,
              "receipt_json": good.receipt_json}
    values[field] = "X" * 64
    with pytest.raises(BoundIssuanceRefused, match="issuance_binding_mismatch"):
        await _build(intent, binding=EffectBinding(**values))


@pytest.mark.asyncio
async def test_only_a_grant_binding_effect_issues() -> None:
    intent = _intent()
    with pytest.raises(BoundIssuanceRefused, match="issuance_effect_kind_invalid"):
        await _build(intent, binding=_binding(intent.digest, kind="credential_lifetime"))


@pytest.mark.asyncio
async def test_the_sdk_accepts_the_context_when_it_is_importable() -> None:
    try:
        sdk = importlib.import_module("kdcube_ai_app.auth.bundle.session_issuance")
    except ImportError:
        pytest.skip("the SDK with session_issuance is not on the path")
    bound = sdk.IssuanceContext.from_context(await _build(_intent()))
    assert bound.actor == "user:admin-1" and bound.target_incarnation == 4


def test_the_custody_namespace_is_pinned() -> None:
    assert CUSTODY_NAMESPACE == "connection-hub-issuance-custody"


class _Custody:
    def __init__(self, namespace=CUSTODY_NAMESPACE, declared_backend="host-vault", running_ok=True):
        self.namespace, self.declared_backend, self.running_ok, self.qualified = namespace, declared_backend, running_ok, 0

    async def create(self, *, secret_ref, value, expires_at):
        return True

    async def get(self, *, secret_ref):
        return None

    async def qualify(self):
        self.qualified += 1
        if not self.running_ok:
            raise RuntimeError("issuance_custody_not_durable: running backend is the ephemeral sidecar")


@pytest.mark.asyncio
async def test_production_requires_the_exact_sdk_wrapper_type(monkeypatch) -> None:
    monkeypatch.setattr(bound_issuance, "_issuance_custody_type", lambda: None)
    with pytest.raises(BoundIssuanceRefused, match="issuance_custody_unqualified"):
        await require_production_custody(_Custody(), production=True)

    class _Wrapper(_Custody):
        pass

    class _Subclass(_Wrapper):
        pass

    monkeypatch.setattr(bound_issuance, "_issuance_custody_type", lambda: _Wrapper)
    with pytest.raises(BoundIssuanceRefused, match="issuance_custody_unqualified"):
        await require_production_custody(_Custody(), production=True)  # a duck type
    with pytest.raises(BoundIssuanceRefused, match="issuance_custody_unqualified"):
        await require_production_custody(_Subclass(), production=True)  # exact type only
    good = _Wrapper()
    assert await require_production_custody(good, production=True) is good and good.qualified == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["secrets-service", "secrets-file", "in-memory", None, ""])
async def test_production_refuses_a_backend_not_declared_durable(monkeypatch, backend) -> None:
    class _Wrapper(_Custody):
        pass

    monkeypatch.setattr(bound_issuance, "_issuance_custody_type", lambda: _Wrapper)
    custody = _Wrapper(declared_backend=backend)
    with pytest.raises(BoundIssuanceRefused, match="issuance_custody_not_durable"):
        await require_production_custody(custody, production=True)
    assert custody.qualified == 0


@pytest.mark.asyncio
async def test_production_refuses_when_the_running_backend_does_not_qualify(monkeypatch) -> None:
    # Ops wrapper F1: a declared host-vault whose running sidecar is ephemeral.
    class _Wrapper(_Custody):
        pass

    monkeypatch.setattr(bound_issuance, "_issuance_custody_type", lambda: _Wrapper)
    with pytest.raises(BoundIssuanceRefused, match="issuance_custody_not_durable") as raised:
        await require_production_custody(_Wrapper(running_ok=False), production=True)
    assert "sidecar" not in str(raised.value)  # the wrapper's text never leaks


@pytest.mark.asyncio
@pytest.mark.parametrize("production", [True, False])
async def test_the_namespace_has_no_default(production) -> None:
    class _NoNamespace:  # a bare store: no public namespace at all
        async def create(self, *, secret_ref, value, expires_at):
            return True

        async def get(self, *, secret_ref):
            return None

    custody = _NoNamespace()
    with pytest.raises(BoundIssuanceRefused, match="issuance_custody_namespace_invalid"):
        await require_production_custody(custody, production=production)
    with pytest.raises(BoundIssuanceRefused, match="issuance_custody_namespace_invalid"):
        await require_production_custody(_Custody("some-other-bundle"), production=production)


@pytest.mark.asyncio
async def test_production_must_be_named_explicitly() -> None:
    with pytest.raises(BoundIssuanceRefused, match="issuance_custody_unavailable"):
        await require_production_custody(_Custody(), production="yes")
    with pytest.raises(BoundIssuanceRefused, match="issuance_custody_unavailable"):
        await require_production_custody(object(), production=False)


@pytest.mark.asyncio
async def test_development_may_use_a_local_custody_in_the_namespace() -> None:
    custody = _Custody(declared_backend="in-memory")
    assert await require_production_custody(custody, production=False) is custody and custody.qualified == 0


def test_the_integration_subject_is_client_and_grantor() -> None:
    assert integration_user_id("client-1", "user:owner") == "integration:client-1:user:owner"
    for bad in (("", "user:owner"), ("client 1", "user:owner"), ("client:1", "user:owner"), ("c", " u")):
        with pytest.raises(BoundIssuanceRefused, match="issuance_user_invalid"):
            integration_user_id(*bad)

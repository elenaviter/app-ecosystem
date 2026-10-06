"""Isolated helper checks, not qualification of production target adapters."""
from copy import deepcopy

import pytest

from connection_hub.delegated_credentials.cards.model import CARD_POINTER_SCHEMA
from connection_hub.delegated_credentials.cards.participant_effects import (
    ParticipantEffectApplier, ParticipantEffectRefused,
)

TX = "a" * 64


def receipt():
    before = {"schema": CARD_POINTER_SCHEMA, "access_id": "card_1", "card_revision": 1,
              "revision_name": "revision_1", "content_hash": "b" * 64,
              "updated_at": "2026-10-06T12:00:00Z", "expires_at": 100, "state": "active"}
    return {"transaction_id": TX, "state": "committed", "intent_digest": "c" * 64,
            "participant": "hub", "subject_hash": "d" * 64, "access_id": "card_1",
            "change_digest": "e" * 64, "before": before,
            "after": {**before, "card_revision": 2, "revision_name": "revision_2",
                      "content_hash": "f" * 64, "expires_at": 200},
            "effects": [{"kind": "credential_lifetime", "key": "access",
                         "payload": {"access_id": "card_1", "expires_at": 200}}]}


class SyntheticTarget:
    def __init__(self):
        self.calls = []
        self.applied = {}

    async def apply_once(self, binding, payload):
        self.calls.append((binding, deepcopy(payload)))
        identity = (binding.transaction_id, binding.kind, binding.key)
        previous = self.applied.setdefault(identity, (binding.receipt_digest, binding.effect_digest))
        if previous != (binding.receipt_digest, binding.effect_digest):
            raise ParticipantEffectRefused("synthetic_target_binding_moved")
        return binding.effect_digest


def harness(saved=None):
    saved = receipt() if saved is None else saved
    target = SyntheticTarget()
    async def read(transaction_id):
        assert transaction_id == TX
        return deepcopy(saved)
    return saved, target, ParticipantEffectApplier(read_receipt=read, targets={"credential_lifetime": target})


async def invoke(applier, payload=None):
    await applier("credential_lifetime", "access", payload or {"access_id": "card_1", "expires_at": 200},
                  transaction_id=TX)


@pytest.mark.asyncio
async def test_exact_replay_keeps_binding_and_absolute_deadline():
    _, target, applier = harness()
    await invoke(applier)
    await invoke(applier)
    assert len(target.applied) == 1
    assert target.calls[0] == target.calls[1]
    assert target.calls[0][1]["expires_at"] == 200  # no now() or TTL recomputation


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["prepared", "aborted", "unknown", None])
async def test_zero_effects_without_committed_decision(state):
    saved, target, applier = harness()
    saved["state"] = state
    with pytest.raises(ParticipantEffectRefused, match="card_effect_not_committed"):
        await invoke(applier)
    assert target.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change,reason", [
    ({"expires_at": 201}, "card_effect_digest_mismatch"),
    ({"access_id": "later_card"}, "card_effect_payload_binding_invalid"),
    ({"expires_at": True}, "card_effect_deadline_invalid"),
    ({"ttl_seconds": 100}, "card_effect_payload_binding_invalid"),
    ({"access_token": "synthetic-forbidden-value"}, "card_effect_secret_field"),
])
async def test_payload_change_refuses_before_target(change, reason):
    _, target, applier = harness()
    with pytest.raises(ParticipantEffectRefused, match=reason):
        await invoke(applier, {"access_id": "card_1", "expires_at": 200, **change})
    assert target.calls == []


@pytest.mark.asyncio
async def test_duplicate_effect_is_not_a_partial_application():
    saved, target, applier = harness()
    saved["effects"].append(deepcopy(saved["effects"][0]))
    with pytest.raises(ParticipantEffectRefused, match="card_effect_set_invalid"):
        await invoke(applier)
    assert target.calls == []


@pytest.mark.asyncio
async def test_later_receipt_vector_cannot_reuse_target_identity():
    saved, target, applier = harness()
    await invoke(applier)
    saved["after"]["content_hash"] = "1" * 64
    with pytest.raises(ParticipantEffectRefused, match="synthetic_target_binding_moved"):
        await invoke(applier)
    assert len(target.applied) == 1


@pytest.mark.asyncio
async def test_adapter_absence_fails_closed():
    async def read(tx):
        return receipt()
    with pytest.raises(ParticipantEffectRefused, match="card_effect_adapter_unavailable"):
        await invoke(ParticipantEffectApplier(read_receipt=read, targets={}))


@pytest.mark.asyncio
async def test_boolean_revision_is_not_an_integer_revision():
    saved, target, applier = harness()
    saved["before"]["card_revision"] = True
    with pytest.raises(ParticipantEffectRefused, match="card_effect_receipt_binding_invalid"):
        await invoke(applier)
    assert target.calls == []


@pytest.mark.asyncio
async def test_unhashable_policy_mode_is_a_named_refusal():
    saved, target, applier = harness()
    saved["effects"] = [{"kind": "invocation_policy", "key": "resource/operation",
                         "payload": {"access_id": "card_1", "mode": []}}]
    with pytest.raises(ParticipantEffectRefused, match="card_effect_payload_invalid"):
        await applier("invocation_policy", "resource/operation", saved["effects"][0]["payload"],
                      transaction_id=TX)
    assert target.calls == []

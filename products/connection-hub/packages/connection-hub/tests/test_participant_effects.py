"""Isolated helper checks, not qualification of production target adapters."""
from copy import deepcopy
import hashlib

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
            "participant": "hub", "subject_hash": hashlib.sha256(b"synthetic-owner").hexdigest(), "access_id": "card_1",
            "change_digest": "e" * 64, "before": before,
            "after": {**before, "card_revision": 2, "revision_name": "revision_2",
                      "content_hash": "f" * 64, "expires_at": 200},
            "effects": [{"kind": "credential_lifetime", "key": "access",
                         "payload": {"access_id": "card_1", "expires_at": 200, "base_card_revision": 1}}]}


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
    await applier("credential_lifetime", "access", payload or {"access_id": "card_1", "expires_at": 200,
                                                             "base_card_revision": 1},
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
        await invoke(applier, {"access_id": "card_1", "expires_at": 200, "base_card_revision": 1, **change})
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
                         "payload": {"owner_subject": "synthetic-owner", "mode": [], "expected_revision": 0,
                                     "authority": {"access_id": "card_1", "resource": "resource",
                                                   "operation": "operation", "surface": "outer"}}}]
    with pytest.raises(ParticipantEffectRefused, match="card_effect_payload_invalid"):
        await applier("invocation_policy", "resource/operation", saved["effects"][0]["payload"],
                      transaction_id=TX)
    assert target.calls == []


@pytest.mark.asyncio
async def test_non_policy_prepare_and_release_are_validated_noops():
    saved, target, applier = harness()
    effect = saved["effects"][0]
    saved["state"] = "prepared"
    await applier.prepare(effect["kind"], effect["key"], effect["payload"], transaction_id=TX)
    saved["state"] = "aborted"
    await applier.release(effect["kind"], effect["key"], effect["payload"], transaction_id=TX)
    assert target.calls == []


@pytest.mark.asyncio
async def test_policy_phase_identity_is_stable_across_decision():
    saved = receipt()
    saved["effects"] = [{"kind": "invocation_policy", "key": "resource/operation", "payload": {
        "owner_subject": "synthetic-owner", "mode": "always", "expected_revision": 0,
        "authority": {"access_id": "card_1", "resource": "resource", "operation": "operation", "surface": "outer"}}}]
    bindings = []
    class Target:
        async def prepare_once(self, binding, payload):
            bindings.append(binding)
            return binding.effect_digest
        async def release_once(self, binding, payload):
            bindings.append(binding)
            return binding.effect_digest
    async def read(tx):
        return deepcopy(saved)
    applier = ParticipantEffectApplier(read_receipt=read, targets={"invocation_policy": Target()})
    effect = saved["effects"][0]
    saved["state"] = "prepared"
    saved["reason"] = ""
    await applier.prepare(effect["kind"], effect["key"], effect["payload"], transaction_id=TX)
    saved["state"] = "aborted"
    saved["reason"] = "synthetic abort"
    await applier.release(effect["kind"], effect["key"], effect["payload"], transaction_id=TX)
    assert bindings[0] == bindings[1]
    assert "state" not in bindings[0].receipt()

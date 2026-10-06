"""Isolated helper checks, not qualification of production target adapters."""
from copy import deepcopy
import hashlib

import pytest

from connection_hub.delegated_credentials.cards.model import CARD_POINTER_SCHEMA
from connection_hub.delegated_credentials.cards.participant_effects import (
    ParticipantEffectApplier, ParticipantEffectRefused,
)
from connection_hub.invocation_policy.models import InvocationAuthority

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
            raise ParticipantEffectRefused("card_effect_binding_mismatch")
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
    with pytest.raises(ParticipantEffectRefused, match="card_effect_binding_mismatch"):
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
    saved["effects"][0]["key"] = InvocationAuthority.from_mapping(saved["effects"][0]["payload"]["authority"]).key
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


def effect_for(kind):
    payloads = {
        "grant_binding": {"access_id": "card_1", "operations": ["read"], "resource_grants": {},
                          "resource_operations": {}, "named_services": {}, "expires_at": 200, "slot": "new"},
        "credential_lifetime": {"access_id": "card_1", "expires_at": 200, "base_card_revision": 1},
        "invocation_policy": {"owner_subject": "synthetic-owner", "mode": "always", "expected_revision": 0,
                              "authority": {"access_id": "card_1", "resource": "resource", "operation": "read",
                                            "surface": "outer", "account": {"provider_id": "synthetic", "account_id": "account_1"}}},
        "grant_unbind": {"access_id": "card_1", "session_id": "synthetic-session", "token_sha256": "2" * 64},
    }
    payload = payloads[kind]
    key = {"grant_binding": "new", "credential_lifetime": "card", "grant_unbind": "old"}.get(kind)
    if key is None:
        key = InvocationAuthority.from_mapping(payload["authority"]).key
    return {"kind": kind, "key": key, "payload": payload}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["grant_binding", "credential_lifetime", "invocation_policy", "grant_unbind"])
async def test_every_kind_dispatches_only_its_exact_bound_payload(kind):
    saved = receipt()
    effect = effect_for(kind)
    saved["effects"] = [effect]
    target = SyntheticTarget()
    async def read(tx):
        return deepcopy(saved)
    applier = ParticipantEffectApplier(read_receipt=read, targets={kind: target})
    result = await applier.apply(kind, effect["key"], effect["payload"], transaction_id=TX)
    assert result == target.calls[0][0].effect_digest
    assert target.calls[0][1] == effect["payload"]


@pytest.mark.asyncio
async def test_named_lifetime_noop_reaches_public_callback_unchanged():
    saved = receipt()
    class Target:
        async def apply_once(self, binding, payload):
            return "no_active_credentials"
    async def read(tx):
        return saved
    applier = ParticipantEffectApplier(read_receipt=read, targets={"credential_lifetime": Target()})
    effect = saved["effects"][0]
    assert await applier(effect["kind"], effect["key"], effect["payload"], transaction_id=TX) == "no_active_credentials"


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [None, False, True, 1, "", "applied", "3" * 64])
async def test_unproven_target_success_is_refused(result):
    saved = receipt()
    class Target:
        async def apply_once(self, binding, payload):
            return result
    async def read(tx):
        return saved
    applier = ParticipantEffectApplier(read_receipt=read, targets={"credential_lifetime": Target()})
    with pytest.raises(ParticipantEffectRefused, match="card_effect_applied_receipt_invalid"):
        await invoke(applier)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["grant_binding", "invocation_policy", "grant_unbind"])
async def test_lifetime_noop_cannot_claim_another_kind_applied(kind):
    saved = receipt()
    effect = effect_for(kind)
    saved["effects"] = [effect]
    class Target:
        async def apply_once(self, binding, payload):
            return "no_active_credentials"
    async def read(tx):
        return saved
    applier = ParticipantEffectApplier(read_receipt=read, targets={kind: Target()})
    with pytest.raises(ParticipantEffectRefused, match="card_effect_applied_receipt_invalid"):
        await applier(kind, effect["key"], effect["payload"], transaction_id=TX)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["reader", "target"])
@pytest.mark.parametrize("exception", [ValueError, ParticipantEffectRefused])
async def test_external_exception_text_is_never_exposed(source, exception):
    import traceback
    sentinel = "synthetic-secret-not-for-output"
    async def read(tx):
        if source == "reader":
            raise exception(sentinel)
        return receipt()
    class Target:
        async def apply_once(self, binding, payload):
            raise exception(sentinel)
    applier = ParticipantEffectApplier(read_receipt=read, targets={"credential_lifetime": Target()})
    with pytest.raises(ParticipantEffectRefused) as caught:
        await invoke(applier)
    assert sentinel not in str(caught.value)
    assert sentinel not in "".join(traceback.format_exception(caught.value))


@pytest.mark.asyncio
@pytest.mark.parametrize("deadline", [0, 1, 200])
async def test_past_or_zero_absolute_deadlines_are_not_extended(deadline):
    saved, target, applier = harness()
    saved["effects"][0]["payload"]["expires_at"] = deadline
    await invoke(applier, saved["effects"][0]["payload"])
    assert target.calls[0][1]["expires_at"] == deadline


@pytest.mark.asyncio
@pytest.mark.parametrize("change,reason", [
    ({"owner_subject": "another-owner"}, "card_effect_owner_mismatch"),
    ({"expected_revision": True}, "card_effect_payload_invalid"),
    ({"authority": {"access_id": "other", "resource": "resource", "operation": "read", "surface": "outer"}}, "card_effect_policy_binding_invalid"),
])
async def test_policy_actor_target_and_revision_are_bound(change, reason):
    saved = receipt()
    saved["state"] = "prepared"
    effect = effect_for("invocation_policy")
    effect["payload"].update(change)
    saved["effects"] = [effect]
    target = SyntheticTarget()
    async def read(tx):
        return saved
    applier = ParticipantEffectApplier(read_receipt=read, targets={"invocation_policy": target})
    with pytest.raises(ParticipantEffectRefused, match=reason):
        await applier.prepare(effect["kind"], effect["key"], effect["payload"], transaction_id=TX)
    assert target.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("key,reason", [("x" * 192, "card_effect_key_invalid"), ("truncated-key", "card_effect_policy_binding_invalid")])
async def test_policy_key_is_exact_and_oversized_key_is_refused_at_prepare(key, reason):
    saved = receipt()
    saved["state"] = "prepared"
    effect = effect_for("invocation_policy")
    effect["key"] = key
    saved["effects"] = [effect]
    target = SyntheticTarget()
    async def read(tx):
        return saved
    applier = ParticipantEffectApplier(read_receipt=read, targets={"invocation_policy": target})
    with pytest.raises(ParticipantEffectRefused, match=reason):
        await applier.prepare(effect["kind"], key, effect["payload"], transaction_id=TX)
    assert target.calls == []

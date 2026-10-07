"""W603: the applier's ``credential_issue`` rules, on synthetic receipts (no target adapter qualified here)."""

from copy import deepcopy
import hashlib

import pytest

from connection_hub.delegated_credentials.cards.model import CARD_POINTER_SCHEMA
from connection_hub.delegated_credentials.cards.participant_effects import (
    CREDENTIAL_ISSUE_SUPERSEDED, ParticipantEffectApplier, ParticipantEffectRefused,
)
from connection_hub.delegated_credentials.oauth_issuance import credential_issue_effect, effect_digest

TX = "a" * 64
GROUP = "9" * 64


def _pointer(revision: int) -> dict:
    return {"schema": CARD_POINTER_SCHEMA, "access_id": "card_1", "card_revision": revision,
            "revision_name": f"revision_{revision}", "content_hash": str(revision) * 64,
            "updated_at": "2026-10-07T08:00:00Z", "expires_at": 500, "state": "active"}


def _receipt(*, created: bool, effects=None, state="committed") -> dict:
    revision = 1 if created else 3
    receipt = {"transaction_id": TX, "state": state, "intent_digest": "c" * 64, "participant": "hub",
               "subject_hash": hashlib.sha256(b"synthetic-owner").hexdigest(), "access_id": "card_1",
               "change_digest": "e" * 64, "before": None if created else _pointer(revision - 1),
               "after": _pointer(revision)}
    if created:
        receipt["group"] = {"transaction_id": GROUP, "index": 0, "lead": TX}
    receipt["effects"] = effects if effects is not None else [
        credential_issue_effect(access_id="card_1", slot=slot, expires_at=500, card_revision=revision)
        for slot in ("access", "refresh")]
    return receipt


class _Target:
    def __init__(self, answer=None):
        self.calls, self.answer = [], answer

    async def _record(self, phase, binding, payload):
        self.calls.append((phase, binding.key, deepcopy(payload), binding.effect_digest))
        return self.answer or binding.effect_digest

    async def apply_once(self, binding, payload):
        return await self._record("apply", binding, payload)

    async def prepare_once(self, binding, payload):
        return await self._record("prepare", binding, payload)

    async def release_once(self, binding, payload):
        return await self._record("release", binding, payload)


def _applier(receipt, target):
    async def read(transaction_id):
        assert transaction_id == TX
        return deepcopy(receipt)
    return ParticipantEffectApplier(read_receipt=read, targets={"credential_issue": target})


@pytest.mark.asyncio
@pytest.mark.parametrize("created", [True, False])
async def test_each_phase_reaches_the_target_with_the_plans_effect_digest(created):
    receipt = _receipt(created=created)
    effect = receipt["effects"][0]
    for phase, state in (("prepare", "prepared"), ("apply", "committed"), ("release", "aborted")):
        target = _Target()
        applier = _applier({**receipt, "state": state}, target)
        result = await getattr(applier, phase)(effect["kind"], effect["key"], effect["payload"], transaction_id=TX)
        assert result == effect_digest(effect)  # the digest the plan fixed at begin
        assert target.calls == [(phase, "access", effect["payload"], effect_digest(effect))]


@pytest.mark.asyncio
async def test_superseded_is_a_named_outcome_of_this_kind_only():
    receipt = _receipt(created=False)
    effect = receipt["effects"][1]
    applier = _applier(receipt, _Target(answer=CREDENTIAL_ISSUE_SUPERSEDED))
    assert await applier.apply("credential_issue", "refresh", effect["payload"],
                               transaction_id=TX) == CREDENTIAL_ISSUE_SUPERSEDED
    for other in ("no_active_credentials", "applied", "superseded "):
        with pytest.raises(ParticipantEffectRefused, match="card_effect_applied_receipt_invalid"):
            await _applier(receipt, _Target(answer=other)).apply("credential_issue", "refresh", effect["payload"],
                                                                 transaction_id=TX)


@pytest.mark.asyncio
@pytest.mark.parametrize("change,reason", [
    (lambda p: {**p, "slot": "refresh"}, "card_effect_payload_invalid"),
    (lambda p: {**p, "card_revision": 2}, "card_effect_base_revision_mismatch"),
    (lambda p: {**p, "card_revision": True}, "card_effect_base_revision_mismatch"),
    (lambda p: {**p, "expires_at": 0}, "card_effect_payload_invalid"),
    (lambda p: {**p, "access_id": "card_2"}, "card_effect_payload_binding_invalid"),
    (lambda p: {**p, "token_sha256": "0" * 64}, "card_effect_payload_binding_invalid"),
    (lambda p: {**p, "refresh_token": "raw"}, "card_effect_secret_field"),
])
async def test_the_payload_is_closed_and_bound_to_the_committed_revision(change, reason):
    effect = credential_issue_effect(access_id="card_1", slot="access", expires_at=500, card_revision=3)
    receipt = _receipt(created=False, effects=[{**effect, "payload": change(effect["payload"])}])
    with pytest.raises(ParticipantEffectRefused, match=reason):
        await _applier(receipt, _Target()).apply("credential_issue", "access", change(effect["payload"]),
                                                 transaction_id=TX)


@pytest.mark.asyncio
async def test_an_undeclared_slot_key_is_refused():
    effect = credential_issue_effect(access_id="card_1", slot="access", expires_at=500, card_revision=3)
    bad = {**effect, "key": "id_token", "payload": {**effect["payload"], "slot": "id_token"}}
    with pytest.raises(ParticipantEffectRefused, match="card_effect_payload_invalid"):
        await _applier(_receipt(created=False, effects=[bad]), _Target()).apply(
            "credential_issue", "id_token", bad["payload"], transaction_id=TX)


@pytest.mark.asyncio
async def test_a_created_card_carries_only_its_original_credentials():
    lifetime = {"kind": "credential_lifetime", "key": "access",
                "payload": {"access_id": "card_1", "expires_at": 500, "base_card_revision": 0}}
    issue = credential_issue_effect(access_id="card_1", slot="access", expires_at=500, card_revision=1)
    with pytest.raises(ParticipantEffectRefused, match="card_effect_set_invalid"):
        await _applier(_receipt(created=True, effects=[issue, lifetime]), _Target()).apply(
            "credential_issue", "access", issue["payload"], transaction_id=TX)


@pytest.mark.asyncio
async def test_an_absent_before_outside_a_group_member_is_refused():
    receipt = _receipt(created=True)
    del receipt["group"]
    effect = receipt["effects"][0]
    with pytest.raises(ParticipantEffectRefused, match="card_effect_receipt_binding_invalid"):
        await _applier(receipt, _Target()).apply("credential_issue", "access", effect["payload"], transaction_id=TX)
    receipt = _receipt(created=True)
    receipt["after"] = _pointer(2)  # a creation is always revision 1
    with pytest.raises(ParticipantEffectRefused, match="card_effect_receipt_binding_invalid"):
        await _applier(receipt, _Target()).apply("credential_issue", "access", effect["payload"], transaction_id=TX)

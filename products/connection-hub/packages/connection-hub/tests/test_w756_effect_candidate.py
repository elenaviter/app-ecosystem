"""FINISH reads the exact durable candidate, while public reads stay fenced."""
import copy

import pytest

from connection_hub.delegated_credentials.automation_access import record_from_card
from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.service import CardServingUnavailable
from connection_hub.delegated_credentials.cards.store import CardStorageError

from test_card_service import SUBJECT_HASH
from test_w756_local_whole_save import _world


@pytest.mark.asyncio
async def test_sealed_effect_binding_reads_committed_receipt_not_its_missing_mutable_state(tmp_path, monkeypatch):
    host, store, decisions, before, after, policies, effects = await _world(tmp_path)
    commit = policies.commit_policy_change
    attempts = 0

    async def cut(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise OSError("test second policy cut")
        return await commit(**kwargs)

    monkeypatch.setattr(policies, "commit_policy_change", cut)
    with pytest.raises(CardServingUnavailable, match="card_effects_pending"):
        await host._persist_record(record_from_card(after), expected_revision=before.card_revision,
                                   effects=effects, require_transaction=True)
    row = next(iter(decisions.rows.values()))
    receipt = await tx.read_receipt(store, row.transaction_id)
    # ParticipantEffectApplier deliberately removes mutable fields so the
    # effect binding remains identical between prepare, commit and replay.
    sealed = {field: value for field, value in receipt.items() if field not in {"state", "reason"}}
    assert "state" not in sealed and receipt["state"] == "committed"
    assert await tx._effect_candidate(store, sealed) == after
    with pytest.raises(CardStorageError, match="card_effects_pending"):
        await store.read_current_authority(subject_hash=SUBJECT_HASH, access_id=before.access_id)

    changed = copy.deepcopy(sealed)
    changed["intent_digest"] = "e" * 64
    with pytest.raises(CardStorageError, match="card_transaction_pointer_binding_invalid"):
        await tx._effect_candidate(store, changed)
    assert decisions.decisions == ["committed"]


@pytest.mark.asyncio
async def test_effect_candidate_never_trusts_a_submitted_committed_state_after_abort(tmp_path, monkeypatch):
    host, store, decisions, before, after, policies, effects = await _world(tmp_path)
    prepare = policies.prepare_policy_change
    attempts = 0

    async def cut(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise OSError("test partial prepare cut")
        return await prepare(**kwargs)

    monkeypatch.setattr(policies, "prepare_policy_change", cut)
    with pytest.raises(Exception):
        await host._persist_record(record_from_card(after), expected_revision=before.card_revision,
                                   effects=effects, require_transaction=True)
    row = next(iter(decisions.rows.values()))
    receipt = await tx.read_receipt(store, row.transaction_id)
    assert receipt["state"] == "aborted"
    submitted = {**receipt, "state": "committed"}
    with pytest.raises(CardStorageError, match="card_transaction_not_committed"):
        await tx._effect_candidate(store, submitted)
    assert decisions.decisions == ["aborted"]

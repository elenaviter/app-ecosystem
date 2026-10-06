"""W578 on the W581 kernel: one Card edit and a multi-participant edit run the SAME coordinator."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from service_foundation.coordination.durable_decision_log import (
    Coordinator, DecisionRecord, DecisionRefused, Intent, Receipt,
)

from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.card_participant import (
    PARTICIPANT, CardIntent, DecisionStorePort, HubCardParticipant, LocalCardIntentSource,
)
from connection_hub.delegated_credentials.cards.store import CardStorageError
from connection_hub.delegated_credentials.issuer_gate import change_digest
from test_card_service import SUBJECT_HASH
from test_card_transaction_store import EFFECTS, _Applier, _setup, _visible

TXID = "e" * 64


class _Store:
    """Test-only DecisionStore (Ops gate 3: never importable from production)."""

    def __init__(self):
        self.rows, self.decisions = {}, []

    async def begin(self, intent):
        row = self.rows.get(intent.request_id)
        if row is None:
            row = self.rows[intent.request_id] = DecisionRecord(TXID, intent, "preparing", {}, {})
        elif row.intent != intent:
            raise DecisionRefused("intent_conflict")
        return row

    async def read(self, transaction_id):
        return next((r for r in self.rows.values() if r.transaction_id == transaction_id), None)

    async def _put(self, row):
        self.rows[row.intent.request_id] = row
        return row

    async def record_prepared(self, receipt):
        row = await self.read(receipt.transaction_id)
        return await self._put(replace(row, state="prepared", prepared={**row.prepared, receipt.participant: receipt}))

    async def decide(self, transaction_id, decision, *, witness_digest=""):
        row = await self.read(transaction_id)
        if row.terminal:
            if row.state != decision:
                raise DecisionRefused("decision_conflict")
            return row
        self.decisions.append(decision)
        return await self._put(replace(row, state=decision))

    async def abort_expired(self, transaction_id):
        return await self.decide(transaction_id, "aborted")

    async def record_finished(self, receipt):
        row = await self.read(receipt.transaction_id)
        return await self._put(replace(row, finished={**row.finished, receipt.participant: receipt}))

    async def list_in_doubt(self, *, limit):
        return [r for r in self.rows.values() if set(r.finished) != set(r.intent.participants)][:limit]


class _Verifier:
    async def prepared(self, record, receipt):
        assert receipt.participant in record.intent.participants

    async def finished(self, record, receipt):
        assert receipt.participant in record.intent.participants


class _Other:
    """A second participant (PB's policy/fence, say) running the same protocol."""

    name = "problem-board.policy"

    def __init__(self, intent_digest):
        self.digest, self.calls = intent_digest, []

    async def prepare(self, transaction_id):
        self.calls.append("prepare")
        return Receipt(transaction_id, self.digest, self.name, "f" * 64)

    async def finish(self, transaction_id, decision):
        self.calls.append(("finish", decision))
        return Receipt(transaction_id, self.digest, self.name, "f" * 64)

    async def read_pending(self, transaction_id):
        return None

    async def list_prepared(self, *, limit):
        return []


async def _edit(tmp_path, *, participants=(PARTICIPANT,), effects=EFFECTS):
    store, service, before, after = await _setup(tmp_path)
    applier = _Applier()
    service.bind_effect_applier(applier)
    decisions = _Store()
    tx.bind_transaction_decisions(store, DecisionStorePort(decisions))
    intent = Intent(actor="person", request_id="r-1", context="connection-hub.card:update",
                    payload_digest=change_digest(after.to_dict()), participants=participants,
                    expires_at=datetime.now(timezone.utc) + timedelta(minutes=5))
    row = await decisions.begin(intent)
    intents = LocalCardIntentSource(store)
    await intents.record(CardIntent(transaction_id=row.transaction_id, intent_digest=intent.digest,
                                    subject_hash=SUBJECT_HASH, original=before, candidate=after,
                                    effects=tuple(effects)))
    hub = HubCardParticipant(service=service, store=store, intents=intents)
    others = {name: _Other(intent.digest) for name in participants if name != PARTICIPANT}
    coordinator = Coordinator(decisions, {PARTICIPANT: hub, **others}, _Verifier())
    return store, coordinator, decisions, intent, before, after, applier, others


@pytest.mark.asyncio
@pytest.mark.parametrize("participants", [(PARTICIPANT,), (PARTICIPANT, "problem-board.policy")])
async def test_one_and_two_participant_edits_run_the_same_coordinator(tmp_path, participants):
    store, coordinator, decisions, intent, before, after, applier, others = await _edit(
        tmp_path, participants=participants)
    record = await coordinator.prepare(intent)
    assert set(record.prepared) == set(participants)
    with pytest.raises(CardStorageError, match="card_transaction_undecided"):
        await _visible(store, before)  # prepared, undecided: never served
    assert applier.applied == []  # no effect before the decision
    await coordinator.decide(record.transaction_id, "committed")
    record = await coordinator.finish(record.transaction_id)
    assert set(record.finished) == set(participants) and decisions.decisions == ["committed"]
    assert await _visible(store, before) == after
    assert [kind for _, kind, _ in applier.applied] == ["grant_binding", "invocation_policy"]
    for other in others.values():
        assert other.calls == ["prepare", ("finish", "committed")]


@pytest.mark.asyncio
async def test_an_aborted_edit_leaves_the_card_and_applies_nothing(tmp_path):
    store, coordinator, decisions, intent, before, after, applier, _ = await _edit(tmp_path)
    record = await coordinator.prepare(intent)
    await coordinator.decide(record.transaction_id, "aborted")
    await coordinator.finish(record.transaction_id)
    assert await _visible(store, before) == before and applier.applied == []


@pytest.mark.asyncio
async def test_finish_materializes_only_the_recorded_decision(tmp_path):
    store, coordinator, decisions, intent, before, after, applier, _ = await _edit(tmp_path)
    record = await coordinator.prepare(intent)
    hub = coordinator.participants[PARTICIPANT]
    with pytest.raises(DecisionRefused, match="card_transaction_decision_not_recorded"):
        await hub.finish(record.transaction_id, "committed")  # nothing recorded yet
    assert (await tx.state(store, transaction_id=record.transaction_id))["state"] == "prepared"


@pytest.mark.asyncio
async def test_the_intent_is_immutable_and_read_only_by_transaction_id(tmp_path):
    store, coordinator, decisions, intent, before, after, applier, _ = await _edit(tmp_path)
    intents = LocalCardIntentSource(store)
    loaded = await intents.load(TXID)
    with pytest.raises(DecisionRefused, match="card_intent_conflict"):
        await intents.record(replace(loaded, candidate=replace(after, label="swapped")))
    with pytest.raises(DecisionRefused, match="card_intent_unknown"):
        await intents.load("d" * 64)


@pytest.mark.asyncio
async def test_recovery_lists_the_prepared_card_and_finishes_it(tmp_path):
    store, coordinator, decisions, intent, before, after, applier, _ = await _edit(tmp_path)
    record = await coordinator.prepare(intent)
    hub = coordinator.participants[PARTICIPANT]
    assert [r.transaction_id for r in await hub.list_prepared(limit=10)] == [record.transaction_id]
    assert (await hub.read_pending(record.transaction_id)).participant == PARTICIPANT
    await coordinator.decide(record.transaction_id, "committed")
    await coordinator.recover(limit=10)
    assert await _visible(store, before) == after and await hub.list_prepared(limit=10) == []

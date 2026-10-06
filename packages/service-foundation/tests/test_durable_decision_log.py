from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from service_foundation.coordination.durable_decision_log import (
    Coordinator, DecisionRecord, DecisionRefused, Intent, Receipt,
)


class MemoryStore:
    """A protocol spy; durable CAS is exercised separately against PostgreSQL."""

    def __init__(self):
        self.rows = {}
        self.decisions = []

    async def begin(self, intent):
        record = self.rows.get(intent.request_id)
        if record is not None:
            if record.intent != intent:
                raise DecisionRefused("intent_conflict")
            return record
        record = DecisionRecord(hashlib.sha256(intent.request_id.encode()).hexdigest(),
                                intent, "preparing", {}, {})
        self.rows[intent.request_id] = record
        return record

    async def read(self, transaction_id):
        return next((row for row in self.rows.values()
                     if row.transaction_id == transaction_id), None)

    async def record_prepared(self, receipt):
        row = await self.read(receipt.transaction_id)
        prepared = dict(row.prepared)
        prepared[receipt.participant] = receipt
        self.rows[row.intent.request_id] = replace(row, prepared=prepared)
        return self.rows[row.intent.request_id]

    async def decide(self, transaction_id, decision, *, witness_digest=""):
        row = await self.read(transaction_id)
        if row.terminal:
            if row.state != decision:
                raise DecisionRefused("decision_conflict")
            return row
        self.decisions.append(decision)
        self.rows[row.intent.request_id] = replace(
            row, state=decision, witness_digest=witness_digest if decision == "committed" else "")
        return self.rows[row.intent.request_id]

    async def abort_expired(self, transaction_id):
        row = await self.read(transaction_id)
        if row.intent.expires_at > datetime.now(timezone.utc):
            raise DecisionRefused("not_expired")
        return await self.decide(transaction_id, "aborted")

    async def record_finished(self, receipt):
        row = await self.read(receipt.transaction_id)
        finished = dict(row.finished)
        finished[receipt.participant] = receipt
        self.rows[row.intent.request_id] = replace(row, finished=finished)
        return self.rows[row.intent.request_id]

    async def list_in_doubt(self, *, limit):
        return [row for row in self.rows.values() if len(row.finished) < len(row.intent.participants)]


class Realm:
    def __init__(self, name, intent):
        self.name, self.intent = name, intent
        self.calls = []

    async def prepare(self, transaction_id):
        self.calls.append("prepare")
        return Receipt(transaction_id, self.intent.digest, self.name, "b" * 64)

    async def finish(self, transaction_id, decision):
        self.calls.append("finish:" + decision)
        return Receipt(transaction_id, self.intent.digest, self.name, "c" * 64)


class Verifier:
    async def prepared(self, record, receipt):
        if receipt.receipt_digest != "b" * 64:
            raise DecisionRefused("receipt_untrusted")

    async def finished(self, record, receipt):
        if receipt.receipt_digest != "c" * 64:
            raise DecisionRefused("receipt_untrusted")


def intent(names):
    return Intent("actor", "request", "context", "f" * 64, tuple(names),
                  datetime.now(timezone.utc) + timedelta(minutes=1))


@pytest.mark.asyncio
@pytest.mark.parametrize("names", [("card",), ("business", "card")])
async def test_one_and_two_realm_edits_use_the_same_durable_decision_path(names):
    frozen = intent(names)
    store = MemoryStore()
    realms = {name: Realm(name, frozen) for name in names}
    manager = Coordinator(store, realms, Verifier())

    prepared = await manager.prepare(frozen)
    assert list(prepared.prepared) == list(names)
    await manager.decide(prepared.transaction_id, "committed", witness_digest="e" * 64)
    result = await manager.finish(prepared.transaction_id)
    assert result.state == "committed"
    assert store.decisions == ["committed"]
    assert list(result.finished) == list(names)
    assert await manager.finish(prepared.transaction_id) == result
    assert all(realm.calls == ["prepare", "finish:committed"] for realm in realms.values())


@pytest.mark.asyncio
async def test_commit_cannot_skip_a_participant_and_recovery_cannot_abort_before_expiry():
    frozen = intent(("business", "card"))
    store = MemoryStore()
    manager = Coordinator(store, {}, Verifier())
    row = await store.begin(frozen)
    await store.record_prepared(Receipt(row.transaction_id, frozen.digest, "business", "b" * 64))

    with pytest.raises(DecisionRefused, match="participants_not_prepared"):
        await manager.decide(row.transaction_id, "committed")
    assert (await manager.recover())[0].state == "preparing"
    assert store.decisions == []


@pytest.mark.asyncio
async def test_late_commit_after_abort_reports_terminal_conflict_first():
    frozen = intent(("card",))
    store = MemoryStore()
    manager = Coordinator(store, {}, Verifier())
    row = await store.begin(frozen)
    await manager.decide(row.transaction_id, "aborted")
    with pytest.raises(DecisionRefused, match="decision_conflict"):
        await manager.decide(row.transaction_id, "committed", witness_digest="e" * 64)


@pytest.mark.asyncio
async def test_commit_requires_exact_nonempty_lowercase_witness_before_store_call():
    frozen = intent(("card",))
    store = MemoryStore()
    manager = Coordinator(store, {}, Verifier())
    row = await store.begin(frozen)
    await store.record_prepared(
        Receipt(row.transaction_id, frozen.digest, "card", "b" * 64))
    for witness in ("", "e" * 63, "E" * 64):
        with pytest.raises(DecisionRefused, match="commit_witness_missing"):
            await manager.decide(row.transaction_id, "committed", witness_digest=witness)
    assert store.decisions == []


@pytest.mark.asyncio
async def test_abort_requires_every_realm_ack_even_when_stage_receipt_was_lost():
    frozen = intent(("business", "card"))
    store = MemoryStore()
    realms = {name: Realm(name, frozen) for name in frozen.participants}
    manager = Coordinator(store, realms, Verifier())
    row = await store.begin(frozen)
    await store.record_prepared(Receipt(row.transaction_id, frozen.digest, "business", "b" * 64))
    await manager.decide(row.transaction_id, "aborted")

    finished = await manager.finish(row.transaction_id)
    assert set(finished.finished) == {"business", "card"}
    assert realms["card"].calls == ["finish:aborted"]


@pytest.mark.asyncio
async def test_forged_prepared_receipt_is_rejected_before_storage():
    frozen = intent(("card",))
    store = MemoryStore()
    realm = Realm("card", frozen)

    async def forged_prepare(transaction_id):
        return Receipt(transaction_id, frozen.digest, "card", "0" * 64)

    realm.prepare = forged_prepare
    with pytest.raises(DecisionRefused, match="receipt_untrusted"):
        await Coordinator(store, {"card": realm}, Verifier()).prepare(frozen)
    assert (await store.read(store.rows[frozen.request_id].transaction_id)).prepared == {}


@pytest.mark.asyncio
async def test_recovery_continues_past_an_unexpired_row_to_expired_work():
    valid = intent(("card",))
    expired = replace(valid, request_id="expired",
                      expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    store = MemoryStore()
    valid_row = await store.begin(valid)
    expired_row = await store.begin(expired)
    realms = {"card": Realm("card", expired)}
    manager = Coordinator(store, realms, Verifier())

    result = await manager.recover()
    assert len(result) == 2
    assert (await store.read(valid_row.transaction_id)).state == "preparing"
    assert (await store.read(expired_row.transaction_id)).state == "aborted"
    assert "card" in (await store.read(expired_row.transaction_id)).finished


def test_canonical_intent_preserves_participant_order_and_fixes_identity():
    first = intent(("business", "card"))
    second = replace(first, participants=("card", "business"))
    assert first.canonical_bytes != second.canonical_bytes
    assert first.digest != second.digest
    with pytest.raises(DecisionRefused, match="intent_invalid"):
        replace(first, participants=("card", "card"))

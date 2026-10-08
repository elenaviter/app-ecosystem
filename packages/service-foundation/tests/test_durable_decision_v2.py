"""Coordinator protocol regressions independent of the PostgreSQL fixture."""

from __future__ import annotations

import time
from dataclasses import replace

import pytest

from service_foundation.coordination.durable_decision_v2 import (
    Coordinator, DecisionRecord, DecisionRefused, Receipt, RecoveryIncomplete,
)
from service_foundation.coordination.durable_wire import (
    IntentDraft, participant_projection, projection_digest,
)


def draft(names=("card",), *, request="request", expiry=None):
    inputs = {}
    for name in names:
        inputs[name] = {
            "participant": name, "binding_kind": "card", "binding_ref": name,
            "target_scope": "project-a", "target_incarnation": "inc-a",
            "action": "update", "before_revision": 2, "candidate_revision": 3,
            "candidate_digest": "a" * 64, "dependency_revisions": {},
            "actor_subject": "user-a", "actor_kind": "human", "provisioning": None,
        }
    return IntentDraft("project-a:user-a", request,
                       int(time.time()) + 60 if expiry is None else expiry,
                       names, {"participant_inputs": inputs})


class MemoryStore:
    def __init__(self):
        self.rows = {}
        self.requests = {}
        self.decisions = []

    async def begin(self, frozen, *, transaction_id=None, epoch=None, connection=None):
        key = frozen.replay_scope, frozen.request_id
        if key in self.requests:
            row = self.rows[self.requests[key]]
            if frozen.bind(row.transaction_id, row.intent.epoch).canonical_bytes != row.intent.canonical_bytes:
                raise DecisionRefused("intent_conflict")
            return row
        transaction_id = transaction_id or "tx-" + frozen.request_id
        epoch = epoch or len(self.rows) + 1
        row = DecisionRecord(frozen.bind(transaction_id, epoch), "preparing", {}, {})
        self.rows[transaction_id] = row
        self.requests[key] = transaction_id
        return row

    async def read(self, transaction_id, *, connection=None):
        return self.rows.get(transaction_id)

    async def record_prepared(self, receipt, *, connection=None):
        row = self.rows[receipt.transaction_id]
        prepared = {**row.prepared, receipt.participant: receipt}
        state = "prepared" if len(prepared) == len(row.intent.participants) else "preparing"
        row = replace(row, prepared=prepared, state=state)
        self.rows[row.transaction_id] = row
        return row

    async def decide(self, transaction_id, decision, *, witness_digest="", connection=None):
        row = self.rows[transaction_id]
        self.decisions.append(decision)
        row = replace(row, state=decision,
                      witness_digest=witness_digest if decision == "committed" else "",
                      decided_at=int(time.time()))
        self.rows[transaction_id] = row
        return row

    async def abort_expired(self, transaction_id, *, connection=None):
        row = self.rows[transaction_id]
        if row.intent.expires_at > int(time.time()):
            raise DecisionRefused("not_expired")
        return await self.decide(transaction_id, "aborted")

    async def record_finished(self, receipt, *, connection=None):
        row = self.rows[receipt.transaction_id]
        row = replace(row, finished={**row.finished, receipt.participant: receipt})
        self.rows[row.transaction_id] = row
        return row

    async def list_in_doubt(self, *, limit):
        return [row for row in self.rows.values()
                if not row.terminal or len(row.finished) < len(row.intent.participants)]


class Realm:
    def __init__(self, store, name, *, forged=False):
        self.store, self.name, self.forged = store, name, forged
        self.calls = []

    async def _receipt(self, transaction_id, digest):
        row = await self.store.read(transaction_id)
        intent = row.intent
        return Receipt(transaction_id, intent.epoch, intent.digest, self.name,
                       projection_digest(intent, self.name),
                       participant_projection(intent, self.name)["candidate_digest"],
                       digest)

    async def prepare(self, transaction_id):
        self.calls.append("prepare")
        return await self._receipt(transaction_id,
                                   "0" * 64 if self.forged else "b" * 64)

    async def finish(self, transaction_id, decision):
        self.calls.append("finish:" + decision)
        return await self._receipt(transaction_id, "c" * 64)


class Verifier:
    async def prepared(self, row, receipt):
        if receipt.receipt_digest != "b" * 64:
            raise DecisionRefused("receipt_untrusted")

    async def finished(self, row, receipt):
        if receipt.receipt_digest != "c" * 64:
            raise DecisionRefused("receipt_untrusted")


@pytest.mark.asyncio
@pytest.mark.parametrize("names", [("card",), ("business", "card")])
async def test_same_entry_points_for_one_and_two_participants(names):
    store = MemoryStore()
    realms = {name: Realm(store, name) for name in names}
    manager = Coordinator(store, realms, Verifier())
    row = await manager.prepare(draft(names))
    assert row.state == "prepared"
    await manager.decide(row.transaction_id, "committed", witness_digest="e" * 64)
    done = await manager.finish(row.transaction_id)
    assert set(done.finished) == set(names)
    assert store.decisions == ["committed"]
    assert await manager.finish(row.transaction_id) == done


@pytest.mark.asyncio
async def test_aborted_orphan_gets_finish_without_central_prepare_receipt():
    store = MemoryStore()
    row = await store.begin(draft(("business", "card")))
    realms = {name: Realm(store, name) for name in row.intent.participants}
    manager = Coordinator(store, realms, Verifier())
    await store.record_prepared(await realms["business"].prepare(row.transaction_id))
    await manager.decide(row.transaction_id, "aborted")
    done = await manager.finish(row.transaction_id)
    assert set(done.finished) == {"business", "card"}
    assert realms["card"].calls == ["finish:aborted"]


@pytest.mark.asyncio
async def test_recovery_skips_valid_row_and_finishes_expired_row_same_pass():
    store = MemoryStore()
    valid = await store.begin(draft(request="valid"))
    expired = await store.begin(draft(request="expired", expiry=int(time.time()) - 1))
    realm = Realm(store, "card")
    done = await Coordinator(store, {"card": realm}, Verifier()).recover()
    assert len(done) == 2
    assert (await store.read(valid.transaction_id)).state == "preparing"
    assert (await store.read(expired.transaction_id)).state == "aborted"
    assert "card" in (await store.read(expired.transaction_id)).finished


@pytest.mark.asyncio
async def test_terminal_conflict_and_exact_commit_witness():
    store = MemoryStore()
    manager = Coordinator(store, {}, Verifier())
    row = await store.begin(draft())
    await manager.decide(row.transaction_id, "aborted")
    with pytest.raises(DecisionRefused, match="decision_conflict"):
        await manager.decide(row.transaction_id, "committed", witness_digest="e" * 64)
    assert store.decisions == ["aborted"]

    prepared = await store.begin(draft(request="prepared"))
    await store.record_prepared(await Realm(store, "card").prepare(prepared.transaction_id))
    for witness in ("", "e" * 63, "E" * 64):
        with pytest.raises(DecisionRefused, match="commit_witness_missing"):
            await manager.decide(prepared.transaction_id, "committed",
                                 witness_digest=witness)
    assert store.decisions == ["aborted"]
    await manager.decide(prepared.transaction_id, "committed", witness_digest="e" * 64)
    with pytest.raises(DecisionRefused, match="decision_conflict"):
        await manager.decide(prepared.transaction_id, "committed", witness_digest="f" * 64)


@pytest.mark.asyncio
async def test_forged_prepared_receipt_is_not_recorded():
    store = MemoryStore()
    realm = Realm(store, "card", forged=True)
    with pytest.raises(DecisionRefused, match="receipt_untrusted"):
        await Coordinator(store, {"card": realm}, Verifier()).prepare(draft())
    assert (await store.read("tx-request")).prepared == {}


@pytest.mark.asyncio
async def test_recovery_continues_after_a_stuck_participant():
    store = MemoryStore()
    first = await store.begin(draft(("stuck",), request="a"))
    second = await store.begin(draft(("good",), request="b"))
    await store.decide(first.transaction_id, "aborted")
    await store.decide(second.transaction_id, "aborted")
    stuck = Realm(store, "stuck")
    good = Realm(store, "good")

    async def unavailable(_transaction_id, _decision):
        raise RuntimeError("realm unavailable")

    stuck.finish = unavailable
    manager = Coordinator(store, {"stuck": stuck, "good": good}, Verifier())
    with pytest.raises(RecoveryIncomplete) as failure:
        await manager.recover()
    assert set(failure.value.failures) == {first.transaction_id}
    assert [row.transaction_id for row in failure.value.completed] == [second.transaction_id]
    assert "good" in (await store.read(second.transaction_id)).finished
    assert "stuck" not in (await store.read(first.transaction_id)).finished

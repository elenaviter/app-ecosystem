"""W578 on the W581 kernel: one Card edit and a multi-participant edit run the SAME coordinator."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from service_foundation.coordination.durable_decision_log import (
    Coordinator, DecisionRecord, DecisionRefused, IntentDraft, Receipt,
)
from service_foundation.coordination.durable_wire import participant_projection, projection_digest

from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.card_participant import (
    PARTICIPANT, CardIntent, DecisionStorePort, HubCardParticipant, HubLocalReceiptVerifier,
    LocalCardIntentSource, card_intent_payload_digest, hub_participant_input,
)
from connection_hub.delegated_credentials.cards.store import CardStorageError
from connection_hub.delegated_credentials.issuer_gate import change_digest
from test_card_service import SUBJECT_HASH
from test_card_transaction_store import EFFECTS, _Applier, _setup, _visible

TXID = "e" * 64
WITNESS = "c" * 64  # the initiating application's post-state witness (W581 F5)


class _Store:
    """Test-only DecisionStore (Ops gate 3: never importable from production)."""

    def __init__(self):
        self.rows, self.decisions = {}, []

    async def begin(self, draft, *, transaction_id=None, epoch=None, connection=None):
        intent = draft.bind(transaction_id or TXID, epoch or 1)
        row = self.rows.get(draft.request_id)
        if row is None:
            row = self.rows[draft.request_id] = DecisionRecord(intent, "preparing", {}, {})
        elif row.intent.digest != intent.digest:
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
        return await self._put(replace(row, state=decision, witness_digest=witness_digest))

    async def abort_expired(self, transaction_id, *, connection=None):
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

    def __init__(self, decisions):
        self.decisions, self.calls = decisions, []

    async def _receipt(self, transaction_id):
        intent = (await self.decisions.read(transaction_id)).intent
        selected = participant_projection(intent, self.name)
        return Receipt(transaction_id, intent.epoch, intent.digest, self.name,
                       projection_digest(intent, self.name), selected["candidate_digest"], "f" * 64)

    async def prepare(self, transaction_id):
        self.calls.append("prepare")
        return await self._receipt(transaction_id)

    async def finish(self, transaction_id, decision):
        self.calls.append(("finish", decision))
        return await self._receipt(transaction_id)

    async def read_pending(self, transaction_id):
        return None

    async def list_prepared(self, *, limit):
        return []


def _other_input(name):
    return {"participant": name, "binding_kind": "problem-board.project", "binding_ref": "work:project:one",
            "target_scope": "project", "target_incarnation": 1, "action": "update", "before_revision": 3,
            "candidate_revision": 4, "candidate_digest": "a" * 64, "dependency_revisions": {},
            "actor_subject": "person", "actor_kind": "caller", "provisioning": {}}


def _draft(before, after, *, effects=EFFECTS, participants=(PARTICIPANT,), request_id="r-1", hub_input=None):
    inputs = {name: _other_input(name) for name in participants if name != PARTICIPANT}
    inputs[PARTICIPANT] = hub_input or hub_participant_input(
        original=before, candidate=after, subject_hash=SUBJECT_HASH, action="update",
        actor_subject="person", actor_kind="caller", effects=effects)
    return IntentDraft(replay_scope=f"test:{request_id}", request_id=request_id,
                       expires_at=int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()),
                       participants=participants, payload={"participant_inputs": inputs})


async def _edit(tmp_path, *, participants=(PARTICIPANT,), effects=EFFECTS, hub_candidate=None, hub_effects=None,
                hub_input=None):
    store, service, before, after = await _setup(tmp_path)
    applier = _Applier()
    service.bind_effect_applier(applier)
    decisions = _Store()
    tx.bind_transaction_decisions(store, DecisionStorePort(decisions))
    intent = _draft(before, after, effects=effects, participants=participants,
                    hub_input=hub_input(before, after) if hub_input else None)
    row = await decisions.begin(intent)
    intents = LocalCardIntentSource(store)
    await intents.record(CardIntent(transaction_id=row.transaction_id, intent_digest=row.intent.digest,
                                    subject_hash=SUBJECT_HASH, original=before,
                                    candidate=hub_candidate or after,
                                    effects=tuple(effects if hub_effects is None else hub_effects),
                                    action="update", actor_subject="person", actor_kind="caller"))
    hub = HubCardParticipant(service=service, store=store, intents=intents, decisions=decisions)
    others = {name: _Other(decisions) for name in participants if name != PARTICIPANT}
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
    await coordinator.decide(record.transaction_id, "committed", witness_digest=WITNESS)
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
    await coordinator.decide(record.transaction_id, "committed", witness_digest=WITNESS)
    await coordinator.recover(limit=10)
    assert await _visible(store, before) == after and await hub.list_prepared(limit=10) == []


@pytest.mark.asyncio
async def test_an_abort_finishes_a_participant_whose_prepare_was_never_recorded(tmp_path):
    # W581 F1: an ABORT reaches every intent participant; the Hub answers with
    # an idempotent tombstone, and a late stage of that transaction refuses.
    store, coordinator, decisions, intent, before, after, applier, _ = await _edit(tmp_path)
    await decisions.begin(intent)
    await coordinator.decide(TXID, "aborted")
    hub = coordinator.participants[PARTICIPANT]
    first = await hub.finish(TXID, "aborted")
    again = await hub.finish(TXID, "aborted")
    assert first == again and first.participant == PARTICIPANT
    with pytest.raises(DecisionRefused, match="card_transaction_aborted"):
        await hub.prepare(TXID)
    assert await _visible(store, before) == before and await tx.list_in_doubt(store) == []


@pytest.mark.asyncio
async def test_a_stage_after_a_recorded_decision_is_refused(tmp_path):
    store, coordinator, decisions, intent, before, after, applier, _ = await _edit(tmp_path)
    await decisions.begin(intent)
    await decisions.decide(TXID, "aborted")
    with pytest.raises(DecisionRefused, match="card_transaction_late_stage"):
        await coordinator.participants[PARTICIPANT].prepare(TXID)
    assert await _visible(store, before) == before



# ── Ops 12:48: B1 intent bound to the coordinator, B2 tombstone under the section ──


@pytest.mark.asyncio
async def test_the_hub_never_applies_a_candidate_the_coordinator_intent_does_not_name(tmp_path):
    # B1: the Intent's payload_digest names `after`; the Hub record holds another candidate.
    _, _, _, _, before, after, _, _ = await _edit(tmp_path / "probe")
    swapped = replace(after, label="NOT what the coordinator intent names")
    store, coordinator, decisions, intent, before, after, applier, _ = await _edit(tmp_path, hub_candidate=swapped)
    with pytest.raises(DecisionRefused, match="card_intent_not_bound"):
        await coordinator.prepare(intent)
    assert await _visible(store, before) == before and applier.applied == []


@pytest.mark.asyncio
async def test_an_abort_that_meets_a_finished_stage_finishes_its_receipt(tmp_path):
    # B2: inside the section the stage won; the abort finishes that receipt (no tombstone).
    store, coordinator, decisions, intent, before, after, applier, _ = await _edit(tmp_path)
    hub = coordinator.participants[PARTICIPANT]
    await hub.prepare(TXID)  # staged, but its reply never reached record_prepared
    await decisions.decide(TXID, "aborted")
    receipt = await hub.finish(TXID, "aborted")
    assert (await tx.state(store, transaction_id=TXID))["state"] == "aborted"
    assert receipt.participant == PARTICIPANT
    assert await _visible(store, before) == before and await tx.list_in_doubt(store) == []
    with pytest.raises(DecisionRefused, match="card_transaction_aborted"):  # N2
        await hub.prepare(TXID)


@pytest.mark.asyncio
async def test_the_tombstone_releases_the_stage_serving_mark(tmp_path):
    # N1: a crash after the F8 mark and before the receipt leaves the mark; the tombstone releases it.
    from connection_hub.delegated_credentials.cards.service import transaction_mutation_id
    store, coordinator, decisions, intent, before, after, applier, _ = await _edit(tmp_path)
    released = []

    async def finalize_removal(access_id, *, mutation_id):
        released.append((access_id, mutation_id))

    coordinator.participants[PARTICIPANT]._service._cache.finalize_removal = finalize_removal
    await decisions.begin(intent)
    await decisions.decide(TXID, "aborted")
    await coordinator.participants[PARTICIPANT].finish(TXID, "aborted")
    assert released == [(before.access_id, transaction_mutation_id(TXID))]


@pytest.mark.asyncio
async def test_a_tombstone_is_never_written_over_a_prepared_receipt(tmp_path):
    # N3: abort_unstaged refuses when a receipt exists.
    store, coordinator, decisions, intent, before, after, applier, _ = await _edit(tmp_path)
    await coordinator.participants[PARTICIPANT].prepare(TXID)
    with pytest.raises(tx.CardTransactionRefused, match="card_transaction_prepared"):
        await tx.abort_unstaged(store, TXID)


@pytest.mark.asyncio
async def test_with_a_real_lock_an_abort_cannot_race_an_in_flight_stage(tmp_path):
    # B2 with a REAL per-Card lock: a lost-reply prepare is paused just before
    # its receipt write; the abort waits for the section, then finishes that
    # receipt instead of tombstoning around it. Nothing is left fenced or hidden.
    import asyncio
    from contextlib import asynccontextmanager

    store, coordinator, decisions, intent, before, after, applier, _ = await _edit(tmp_path)
    hub = coordinator.participants[PARTICIPANT]
    locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def real_lock(*, lock_path, **kwargs):
        lock = locks.setdefault(str(lock_path), asyncio.Lock())
        async with lock:
            yield

    hub._service._mutation_lock = real_lock
    paused, resume = asyncio.Event(), asyncio.Event()
    real_write = tx.write_json_atomic

    async def pausing_write(path, payload):
        if payload.get("schema") == tx.TRANSACTION_RECEIPT_SCHEMA and payload.get("state") == "prepared":
            paused.set()
            await resume.wait()
        return await real_write(path, payload)

    tx.write_json_atomic = pausing_write
    try:
        await decisions.begin(intent)
        staging = asyncio.create_task(hub.prepare(TXID))
        await paused.wait()
        await decisions.decide(TXID, "aborted")  # the presumed abort
        aborting = asyncio.create_task(hub.finish(TXID, "aborted"))
        await asyncio.sleep(0.05)
        assert not aborting.done()  # it waits for the stage's section
        resume.set()
        await staging
        await aborting
    finally:
        tx.write_json_atomic = real_write
    assert (await tx.state(store, transaction_id=TXID))["state"] == "aborted"
    assert await _visible(store, before) == before
    assert await tx.list_in_doubt(store) == []
    await hub._service.commit(replace(before, card_revision=before.card_revision + 1, label="next"),
                              subject_hash=SUBJECT_HASH, expected_revision=before.card_revision, now=1_780_000_000)



@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["payload", "dropped", "added"])
async def test_the_hub_never_applies_an_effect_the_coordinator_intent_does_not_name(tmp_path, change):
    # Ops T2: the effects are part of the bound payload, not only the candidate.
    hub_effects = {
        "payload": [EFFECTS[0], {**EFFECTS[1], "payload": {"mode": "never"}}],
        "dropped": EFFECTS[:1],
        "added": [*EFFECTS, {"kind": "grant_unbind", "key": "old", "payload": {"token_sha256": "a" * 64}}],
    }[change]
    store, coordinator, decisions, intent, before, after, applier, _ = await _edit(tmp_path, hub_effects=hub_effects)
    with pytest.raises(DecisionRefused, match="card_intent_not_bound"):
        await coordinator.prepare(intent)
    assert await tx.state(store, transaction_id=TXID) is None and applier.applied == []


@pytest.mark.asyncio
async def test_recovery_finishes_an_abort_whose_hub_intent_was_never_recorded(tmp_path):
    # Ops R1: a crash between begin and intents.record must not strand recovery.
    store, coordinator, decisions, intent, before, after, applier, _ = await _edit(tmp_path)
    other = _draft(before, after, request_id="r-orphan")
    decisions.rows.clear()
    orphan = "d" * 64
    await decisions.begin(other, transaction_id=orphan)  # no Hub intent for it
    row = await decisions.read(orphan)
    await decisions.decide(row.transaction_id, "aborted")
    await coordinator.recover(limit=10)
    assert (await decisions.read(row.transaction_id)).finished.keys() == {PARTICIPANT}
    assert await tx.list_in_doubt(store) == [] and await _visible(store, before) == before



@pytest.mark.asyncio
async def test_the_no_intent_fallback_never_tombstones_an_undecided_transaction(tmp_path):
    # Ops 13:12: the record.state == aborted check of the R1 fallback, pinned.
    store, coordinator, decisions, intent, before, after, applier, _ = await _edit(tmp_path)
    other = _draft(before, after, request_id="r-undecided")
    orphan = "d" * 64
    await decisions.begin(other, transaction_id=orphan)
    with pytest.raises(DecisionRefused, match="card_intent_unknown"):
        await coordinator.participants[PARTICIPANT].finish(orphan, "aborted")
    assert not (store.root / "card-transactions" / "aborted" / f"{orphan}.json").exists()


# ── Ops 16:55 (v2): every projection field the Hub acts on is compared ──


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "value"), [
    ("binding_kind", "problem-board.project"), ("binding_ref", "aut_other"), ("target_scope", "f" * 64),
    ("before_revision", 99), ("candidate_revision", 99), ("target_incarnation", 99),
    ("dependency_revisions", {"other": 1}), ("action", ""), ("actor_subject", ""), ("actor_kind", "unknown"),
    # CodeApp 17:25: exact values, not only non-empty ones.
    ("action", "revoke"), ("actor_subject", "someone-else"), ("actor_kind", "grantor"),
])
async def test_a_projection_that_differs_in_any_acted_on_field_never_stages(tmp_path, field, value):
    def tampered(before, after):
        good = hub_participant_input(original=before, candidate=after, subject_hash=SUBJECT_HASH, action="update",
                                     actor_subject="person", actor_kind="caller", effects=EFFECTS)
        return {**good, field: value}

    try:
        store, coordinator, decisions, intent, before, after, applier, _ = await _edit(tmp_path, hub_input=tampered)
    except Exception as exc:  # the kernel's own projection rules may refuse first
        assert "invalid" in str(exc) or "revision" in str(exc)
        return
    with pytest.raises(DecisionRefused):
        await coordinator.prepare(intent)
    assert await tx.state(store, transaction_id=TXID) is None and applier.applied == []


@pytest.mark.asyncio
async def test_the_receipt_is_the_exact_v2_receipt_bound_to_the_persisted_intent(tmp_path):
    store, coordinator, decisions, intent, before, after, applier, _ = await _edit(tmp_path)
    record = await coordinator.prepare(intent)
    receipt = record.prepared[PARTICIPANT]
    persisted = (await decisions.read(TXID)).intent
    assert (receipt.epoch, receipt.global_intent_digest) == (persisted.epoch, persisted.digest)
    assert receipt.projection_digest == projection_digest(persisted, PARTICIPANT)
    assert receipt.candidate_digest == card_intent_payload_digest(original=before, candidate=after, effects=EFFECTS)


@pytest.mark.asyncio
async def test_the_local_verifier_authenticates_only_the_hubs_own_durable_receipt(tmp_path):
    from dataclasses import replace as dc_replace
    store, coordinator, decisions, intent, before, after, applier, _ = await _edit(tmp_path)
    record = await coordinator.prepare(intent)
    verifier = HubLocalReceiptVerifier(store)
    receipt = record.prepared[PARTICIPANT]
    await verifier.prepared(record, receipt)
    with pytest.raises(DecisionRefused, match="receipt_unauthenticated"):
        await verifier.prepared(record, dc_replace(receipt, receipt_digest="0" * 64))
    with pytest.raises(DecisionRefused, match="receipt_participant_unknown"):
        await verifier.prepared(record, dc_replace(receipt, participant="problem-board.policy"))


# ── EMain 18:35: the REAL verifier authenticates the Hub's own abort tombstones ──


async def _real_verifier_edit(tmp_path):
    store, coordinator, decisions, intent, before, after, applier, others = await _edit(tmp_path)
    coordinator = Coordinator(decisions, dict(coordinator.participants), HubLocalReceiptVerifier(store))
    return store, coordinator, decisions, intent, before, after, applier


@pytest.mark.asyncio
async def test_r1_an_abort_with_no_recorded_intent_finishes_with_the_real_verifier(tmp_path):
    store, coordinator, decisions, intent, before, after, applier = await _real_verifier_edit(tmp_path)
    other = _draft(before, after, request_id="r-orphan-real")
    decisions.rows.clear()
    orphan = "d" * 64
    await decisions.begin(other, transaction_id=orphan)  # crashed before intents.record
    await decisions.decide(orphan, "aborted")
    await coordinator.recover(limit=10)
    assert (await decisions.read(orphan)).finished.keys() == {PARTICIPANT}
    assert await coordinator.recover(limit=10) == []  # nothing left in doubt
    assert await _visible(store, before) == before and applier.applied == []


@pytest.mark.asyncio
async def test_f1_an_abort_of_a_never_staged_intent_finishes_with_the_real_verifier(tmp_path):
    store, coordinator, decisions, intent, before, after, applier = await _real_verifier_edit(tmp_path)
    await decisions.begin(intent)  # the intent is recorded by _edit; prepare never runs
    await coordinator.decide(TXID, "aborted")
    record = await coordinator.finish(TXID)
    assert record.finished.keys() == {PARTICIPANT}
    assert await _visible(store, before) == before and applier.applied == []


@pytest.mark.asyncio
async def test_the_real_verifier_refuses_a_tombstone_for_another_transaction_or_a_commit(tmp_path):
    from dataclasses import replace as dc_replace
    from connection_hub.delegated_credentials.cards.card_participant import receipt_digest
    store, coordinator, decisions, intent, before, after, applier = await _real_verifier_edit(tmp_path)
    await decisions.begin(intent)
    await coordinator.decide(TXID, "aborted")
    record = await coordinator.finish(TXID)
    receipt = record.finished[PARTICIPANT]
    verifier = HubLocalReceiptVerifier(store)
    committed = dc_replace(record, state="committed")
    with pytest.raises(DecisionRefused, match="receipt_unauthenticated"):
        await verifier.finished(committed, receipt)  # a tombstone never proves a COMMIT
    with pytest.raises(DecisionRefused, match="receipt_unauthenticated"):
        await verifier.finished(record, dc_replace(receipt, receipt_digest=receipt_digest({"forged": 1})))


@pytest.mark.asyncio
async def test_the_real_verifier_refuses_a_tombstone_naming_another_transaction(tmp_path):
    import json as _json
    from connection_hub.delegated_credentials.cards.card_participant import receipt_digest
    store, coordinator, decisions, intent, before, after, applier = await _real_verifier_edit(tmp_path)
    await decisions.begin(intent)
    await coordinator.decide(TXID, "aborted")
    path = tx.tombstone_path(store, TXID)
    path.parent.mkdir(parents=True, exist_ok=True)
    forged = {"transaction_id": "f" * 64, "state": "aborted"}
    path.write_text(_json.dumps(forged))  # a file at this id's path that names another transaction
    record = await decisions.read(TXID)
    receipt = Receipt(TXID, record.intent.epoch, record.intent.digest, PARTICIPANT,
                      projection_digest(record.intent, PARTICIPANT),
                      participant_projection(record.intent, PARTICIPANT)["candidate_digest"], receipt_digest(forged))
    with pytest.raises(DecisionRefused, match="receipt_unauthenticated"):
        await HubLocalReceiptVerifier(store).finished(record, receipt)


@pytest.mark.asyncio
async def test_a_refused_prepare_then_abort_finishes_with_the_real_verifier(tmp_path):
    # EMain #599 (18:2x): _coordinated_write on a refused prepare decides ABORT and
    # finishes; the Hub never staged, so its finish is the F1 tombstone.
    store, coordinator, decisions, intent, before, after, applier = await _real_verifier_edit(tmp_path)
    hub = coordinator.participants[PARTICIPANT]
    moved = replace(before, card_revision=before.card_revision + 1, label="another admin saved first")
    await hub._service.commit(moved, subject_hash=SUBJECT_HASH, expected_revision=before.card_revision,
                              now=1_780_000_000)
    with pytest.raises(DecisionRefused):
        await coordinator.prepare(intent)
    await coordinator.decide(TXID, "aborted")
    record = await coordinator.finish(TXID)
    assert record.finished.keys() == {PARTICIPANT}
    assert await coordinator.recover(limit=10) == [] and applier.applied == []
    assert await _visible(store, before) == moved

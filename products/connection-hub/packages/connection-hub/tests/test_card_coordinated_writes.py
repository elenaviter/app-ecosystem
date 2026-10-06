"""W502 ONE protocol: an existing-Card write runs through the W581 coordinator, not a direct commit."""

from __future__ import annotations

from dataclasses import replace

import pytest

from service_foundation.coordination.durable_decision_log import Coordinator

from connection_hub.delegated_credentials.automation_access import (
    AutomationAccessService, record_from_card,
)
from connection_hub.delegated_credentials.caller_writer_gate import CallerWriteRefused
from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.card_participant import (
    PARTICIPANT, DecisionStorePort, HubCardParticipant, LocalCardIntentSource,
)
from connection_hub.delegated_credentials.cards.model import CardCredentialHandles
from connection_hub.delegated_credentials.cards.service import CardConflict
from test_card_participant import _Store, _Verifier
from test_card_service import SUBJECT_HASH
from test_card_transaction_store import _setup, _visible


class _Persistence:
    """The persistence port as AutomationAccessService uses it, over the real Card store."""

    def __init__(self, store):
        self.store, self.direct = store, []

    async def load(self, access_id, *, subject_hash):
        current = await self.store.read_current_authority(subject_hash=subject_hash, access_id=access_id)
        return None if current is None else (current[1], CardCredentialHandles(access_id=access_id))

    async def persist(self, authority, handles, *, subject_hash, expected_revision, **kwargs):
        self.direct.append(authority.card_revision)

    async def persist_guarded(self, authority, handles, *, subject_hash, expected_revision, before_commit):
        await before_commit()
        self.direct.append(authority.card_revision)


async def _host(tmp_path):
    store, service, before, after = await _setup(tmp_path)
    decisions = _Store()
    tx.bind_transaction_decisions(store, DecisionStorePort(decisions))
    intents = LocalCardIntentSource(store)
    hub = HubCardParticipant(service=service, store=store, intents=intents, decisions=decisions)
    host = object.__new__(AutomationAccessService)
    host._persistence = _Persistence(store)
    host._caller_writers = None
    host.bind_card_coordinator(Coordinator(decisions, {PARTICIPANT: hub}, _Verifier()),
                               intents=intents, decisions=decisions)
    return host, store, decisions, before, after


@pytest.mark.asyncio
async def test_an_existing_card_edit_is_one_coordinated_transaction(tmp_path):
    host, store, decisions, before, after = await _host(tmp_path)
    await host._persist_record(record_from_card(after), expected_revision=before.card_revision)
    assert await _visible(store, before) == after
    assert decisions.decisions == ["committed"] and host._persistence.direct == []
    row = next(iter(decisions.rows.values()))
    assert set(row.finished) == {PARTICIPANT} and len(row.witness_digest) == 64
    assert await tx.list_in_doubt(store) == []


@pytest.mark.asyncio
async def test_a_write_that_changes_credential_handles_is_not_routed_yet(tmp_path):
    host, store, decisions, before, after = await _host(tmp_path)
    changed = replace(record_from_card(after), access_token="new-bearer")
    await host._persist_record(changed, expected_revision=before.card_revision)
    assert host._persistence.direct == [after.card_revision] and decisions.decisions == []


@pytest.mark.asyncio
async def test_a_moved_card_aborts_and_leaves_nothing_fenced(tmp_path):
    host, store, decisions, before, after = await _host(tmp_path)
    stale = replace(after, card_revision=before.card_revision + 2)
    with pytest.raises(CardConflict):
        await host._persist_record(record_from_card(stale), expected_revision=before.card_revision + 1)
    assert decisions.decisions in ([], ["aborted"]) and await _visible(store, before) == before
    assert await tx.list_in_doubt(store) == []


@pytest.mark.asyncio
async def test_a_gate_that_refuses_before_the_decision_aborts_the_transaction(tmp_path):
    host, store, decisions, before, after = await _host(tmp_path)

    async def refusing_gate():
        raise CallerWriteRefused("pb_refused")

    with pytest.raises(CallerWriteRefused):
        await host._coordinated_write(record_from_card(after), after, expected_revision=before.card_revision,
                                      caller_write=None, gate=refusing_gate, witness="")
    assert decisions.decisions == ["aborted"] and await _visible(store, before) == before
    assert await tx.list_in_doubt(store) == []


@pytest.mark.asyncio
async def test_a_refused_commit_aborts_and_leaves_nothing_fenced(tmp_path):
    # W581 S2: the store refuses the COMMIT (e.g. the approval expired) after prepare.
    from service_foundation.coordination.durable_decision_log import DecisionRefused
    host, store, decisions, before, after = await _host(tmp_path)
    real_decide = decisions.decide

    async def refuse_commit(transaction_id, decision, *, witness_digest=""):
        if decision == "committed":
            raise DecisionRefused("commit_expired")
        return await real_decide(transaction_id, decision, witness_digest=witness_digest)

    decisions.decide = refuse_commit
    with pytest.raises(CardConflict, match="commit_expired"):
        await host._persist_record(record_from_card(after), expected_revision=before.card_revision)
    assert decisions.decisions == ["aborted"] and await _visible(store, before) == before
    assert await tx.list_in_doubt(store) == []


@pytest.mark.asyncio
async def test_a_governed_coordinated_commit_records_the_gates_change_digest(tmp_path):
    # Ops T3: the witness of a governed write is the binding policy's authorized digest.
    from connection_hub.delegated_credentials.caller_writer_gate import CallerWrite
    from connection_hub.delegated_credentials.cards.model import ControlCardBinding
    from test_delegated_access_renewal import _Policy, _registry
    host, store, decisions, before, after = await _host(tmp_path)
    binding = ControlCardBinding(control_id="c-1", issuer_ref="work:project:one", issuer_kind="project",
                                 control_revision=1)
    bound = replace(before, card_revision=before.card_revision + 1, control_card=binding)
    service = next(iter(host._card_coordinator[0].participants.values()))._service
    await service.commit(bound, subject_hash=SUBJECT_HASH, expected_revision=before.card_revision,
                         now=1_780_000_000)
    policy = _Policy(True)
    host._caller_writers = _registry(policy)
    seen = []
    real = host._enlisted_gate

    async def spy(authority, **kwargs):
        result = await real(authority, **kwargs)
        seen.append(result[1])
        return result

    host._enlisted_gate = spy
    edited = replace(bound, card_revision=bound.card_revision + 1, label="governed edit")
    await host._persist_record(record_from_card(edited), expected_revision=bound.card_revision,
                               caller_write=CallerWrite("extend", "person-1"))
    assert await _visible(store, before) == edited
    row = next(iter(decisions.rows.values()))
    assert seen[0] is not None and row.witness_digest == seen[0].change_digest
    assert ("decide", "extend") in policy.calls and ("finalize", "committed") in policy.calls


@pytest.mark.asyncio
async def test_effects_that_cannot_be_routed_are_refused_never_dropped(tmp_path):
    host, store, decisions, before, after = await _host(tmp_path)
    changed = replace(record_from_card(after), access_token="new-bearer")  # not routable yet
    effects = [{"kind": "credential_lifetime", "key": "card",
                "payload": {"access_id": before.access_id, "expires_at": 1_790_000_000, "base_card_revision": 1}}]
    with pytest.raises(CardConflict, match="card_effects_unroutable"):
        await host._persist_record(changed, expected_revision=before.card_revision, effects=effects)
    assert await _visible(store, before) == before


@pytest.mark.asyncio
async def test_a_coordinated_write_carries_its_effects_to_finish(tmp_path):
    # Ops 13:16: the credential's life is applied only after the COMMIT, through the applier.
    from test_card_transaction_store import _Applier
    host, store, decisions, before, after = await _host(tmp_path)
    applier = _Applier()
    hub = host._card_coordinator[0].participants[PARTICIPANT]
    hub._service.bind_effect_applier(applier)
    effects = [{"kind": "credential_lifetime", "key": "card",
                "payload": {"access_id": before.access_id, "expires_at": 1_790_000_000, "base_card_revision": 1}}]
    await host._persist_record(record_from_card(after), expected_revision=before.card_revision, effects=effects)
    assert await _visible(store, before) == after
    assert [kind for _, kind, _ in applier.applied] == ["credential_lifetime"]

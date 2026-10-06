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

"""W502 v2: a transaction Problem Board initiated reaches the Hub only through the verified authority."""

from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import replace

import pytest

from service_foundation.coordination.durable_decision_log import DecisionRecord, DecisionRefused, IntentDraft

from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.authority_intent_source import (
    AuthorityCardIntentSource,
    AuthorityDecisionReader,
    CardAuthorityBinding,
)
from connection_hub.delegated_credentials.cards.card_participant import (
    PARTICIPANT, DecisionStorePort, HubCardParticipant, candidate_value, hub_participant_input,
)
from connection_hub.delegated_credentials.cards.transaction_authority_v2 import TransactionAuthorityRefused
from test_card_service import SUBJECT_HASH
from test_card_transaction_store import EFFECTS, _Applier, _setup, _visible

TX = "a" * 64
SECRET = "k" * 40
AUTHORITY = CardAuthorityBinding(secret=SECRET, service_id="problem-board@1-0", audience="connection-hub@1-0")
NOW = 1_800_000_000


def _apps():
    path = os.environ.get("APPS_CARD_BUSINESS_AUTHORITY")
    if not path:
        pytest.skip("APPS_CARD_BUSINESS_AUTHORITY (Apps services/card_business_authority.py) is not set")
    spec = importlib.util.spec_from_file_location("apps_card_business_authority", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _ProblemBoard:
    """PB's side: its persisted record and its real signer, behind the fetch port."""

    def __init__(self, apps, before, after, *, effects=EFFECTS):
        hub_input = hub_participant_input(original=before, candidate=after, subject_hash=SUBJECT_HASH,
                                          action="update", actor_subject="user:pb-admin", actor_kind="caller",
                                          effects=effects)
        draft = IntentDraft(replay_scope="pb:work:project:one", request_id="pb-req-1", expires_at=NOW + 600,
                            participants=(PARTICIPANT,),
                            payload={"participant_inputs": {PARTICIPANT: hub_input},
                                     "participant_candidates": {PARTICIPANT: candidate_value(
                                         original=before, candidate=after, effects=effects)},
                                     "pb_state": {"project_ref": "work:project:one"}})
        self.apps, self.record, self.calls, self.fail = apps, DecisionRecord(draft.bind(TX, 3), "preparing", {}, {}), [], False

    def decide(self, state):
        self.record = replace(self.record, state=state, decided_at=NOW + 5)

    async def fetch(self, transaction_id, phase, request_echo):
        self.calls.append((phase, request_echo))
        if self.fail:
            raise ConnectionError("transport down")
        assert transaction_id == self.record.transaction_id
        try:
            return self.apps.build_authority_response(
                self.record, phase=phase, request_echo=request_echo, audience=AUTHORITY.audience,
                participant=PARTICIPANT, service_id=AUTHORITY.service_id, secret=SECRET, now=NOW)
        except DecisionRefused as exc:  # the endpoint's refusal, by name, over the transport
            raise TransactionAuthorityRefused(str(exc)) from None


async def _hub(tmp_path, *, effects=EFFECTS):
    store, service, before, after = await _setup(tmp_path)
    applier = _Applier()
    service.bind_effect_applier(applier)
    pb = _ProblemBoard(_apps(), before, after, effects=effects)
    clock = lambda: NOW  # noqa: E731
    decisions = AuthorityDecisionReader(fetch=pb.fetch, authority=AUTHORITY, clock=clock)
    intents = AuthorityCardIntentSource(store=store, fetch=pb.fetch, authority=AUTHORITY, clock=clock)
    tx.bind_transaction_decisions(store, DecisionStorePort(decisions))
    hub = HubCardParticipant(service=service, store=store, intents=intents, decisions=decisions)
    return hub, pb, store, before, after, applier


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["committed", "aborted"])
async def test_a_pb_initiated_change_stages_and_finishes_only_through_the_verified_authority(tmp_path, state):
    hub, pb, store, before, after, applier = await _hub(tmp_path)
    receipt = await hub.prepare(TX)
    assert receipt.epoch == 3 and receipt.global_intent_digest == pb.record.intent.digest
    with pytest.raises(Exception, match="card_transaction_undecided"):
        await _visible(store, before)  # staged, undecided: never served
    pb.decide(state)
    finished = await hub.finish(TX, state)
    assert finished.participant == PARTICIPANT
    assert await _visible(store, before) == (after if state == "committed" else before)
    assert bool(applier.applied) is (state == "committed")
    echoes = [echo for _, echo in pb.calls]
    assert len(echoes) == len(set(echoes))  # a fresh echo for every request


@pytest.mark.asyncio
async def test_the_verified_intent_is_recorded_once_and_reused_after_the_stage_window(tmp_path):
    hub, pb, store, before, after, applier = await _hub(tmp_path)
    await hub.prepare(TX)
    stage_calls = [phase for phase, _ in pb.calls].count("stage")
    pb.decide("committed")  # PB's stage window is closed from here on
    await hub.finish(TX, "committed")
    assert [phase for phase, _ in pb.calls].count("stage") == stage_calls  # finish read the recorded intent


@pytest.mark.asyncio
async def test_a_hub_card_that_moved_since_pb_froze_the_intent_is_refused(tmp_path):
    hub, pb, store, before, after, applier = await _hub(tmp_path)
    await hub._service.commit(replace(before, card_revision=before.card_revision + 1, label="moved"),
                              subject_hash=SUBJECT_HASH, expected_revision=before.card_revision, now=NOW)
    with pytest.raises(DecisionRefused, match="card_intent_base_moved"):
        await hub.prepare(TX)
    assert await tx.state(store, transaction_id=TX) is None and applier.applied == []


@pytest.mark.asyncio
async def test_an_unreachable_authority_stages_nothing(tmp_path):
    hub, pb, store, before, after, applier = await _hub(tmp_path)
    pb.fail = True
    with pytest.raises(DecisionRefused, match="authority_unavailable"):  # the participant's protocol refusal
        await hub.prepare(TX)
    assert await tx.state(store, transaction_id=TX) is None


@pytest.mark.asyncio
async def test_a_response_signed_with_another_key_is_never_trusted(tmp_path):
    hub, pb, store, before, after, applier = await _hub(tmp_path)
    pb_secret = "z" * 40
    real_fetch = pb.fetch

    async def forged(transaction_id, phase, request_echo):
        response = await real_fetch(transaction_id, phase, request_echo)
        response["authority_proof"] = {**response["authority_proof"], "signature": "A" * 43}
        return response

    hub._intents._fetch = forged
    with pytest.raises(DecisionRefused, match="authority_signature_invalid"):
        await hub.prepare(TX)
    assert await tx.state(store, transaction_id=TX) is None and pb_secret


@pytest.mark.asyncio
async def test_the_decision_reader_follows_pbs_record(tmp_path):
    hub, pb, store, before, after, applier = await _hub(tmp_path)
    reader = hub._decisions
    record = await reader.read(TX)
    assert (record.state, record.terminal, record.transaction_id) == ("preparing", False, TX)
    pb.decide("aborted")
    record = await reader.read(TX)
    assert (record.state, record.decided_at, record.intent.digest) == ("aborted", NOW + 5, pb.record.intent.digest)

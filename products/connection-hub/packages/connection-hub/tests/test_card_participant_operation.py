"""W502: ``card_transaction_participant``, the Hub participant driven by another application's coordinator.

A fake Problem Board authority signs real ``card-transaction-authority.v2``
responses for one GlobalIntent; the Hub side is the real participant, Card
store, authority reader and intent source. Requests carry the real admission
proof. Every authenticated answer must pass the shared verify_participant_answer.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from service_foundation.coordination.durable_decision_log import DecisionRecord, IntentDraft
from service_foundation.coordination.durable_wire import participant_projection

from connection_hub.delegated_credentials.admission import AdmissionRequest, sign_admission_request
from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.authority_intent_source import (
    AuthorityCardIntentSource, AuthorityDecisionReader, CardAuthorityBinding,
)
from connection_hub.delegated_credentials.cards.card_participant import (
    PARTICIPANT, HubCardParticipant, LocalCardIntentSource,
)
from connection_hub.delegated_credentials.cards.participant_operation import (
    ANSWER_SCHEMA, OPERATION, REQUEST_FIELDS, REQUEST_SCHEMA, CardTransactionParticipantOperation, ParticipantCaller,
    RoutedDecisionPort, ScopeBinding, request_digest,
)
from connection_hub.delegated_credentials.cards.transaction_authority_v2 import (
    PROTOCOL, TransactionAuthorityRefused, card_authority_signature,
)
from test_card_transaction_store import _Applier, _setup

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "w502_hub_participant_vectors.json").read_text())
TX = "c" * 64
NOW = 1_800_000_000
PEER = "problem-board"
PROJECT = "work:project:one"
AUTHORITY_SECRET = "a" * 40
REQUEST_SECRET = "r" * 40
RECEIPT_SECRET = "s" * 40
AUTHORITY = CardAuthorityBinding(secret=AUTHORITY_SECRET, service_id="problem-board@1-0",
                                 audience="connection-hub@1-0")


class _Nonces:
    def __init__(self):
        self.keys = set()

    async def set(self, key, value, *, ex, nx):
        if key in self.keys:
            return False
        self.keys.add(key)
        return True


class _NoLocal:
    async def read(self, transaction_id):
        return None


class _World:
    """PB-initiated transactions, looked up as PB does: by (project_ref, transaction_id)."""

    def __init__(self, store, service, records):
        self.store, self.service = store, service
        self.records = dict(records)   # {(project, txid): DecisionRecord}
        self.decided = {}              # {txid: "committed" | "aborted"}
        self.nonces = _Nonces()
        tx.bind_transaction_decisions(store, RoutedDecisionPort(local=_NoLocal(), card_store=store,
                                                                authorities={PEER: self.reader}))
        self.caller = ParticipantCaller(
            service_id=PEER, request_secret=REQUEST_SECRET, receipt_secret=RECEIPT_SECRET,
            receipt_signer_id="connection-hub@1-0", audience="problem-board@1-0",
            hub_resource="connection-hub@1-0", bind=self.bind, scope_field="project_ref")

    @property
    def record(self):
        return self.records[(PROJECT, TX)]

    @property
    def decision(self):
        return self.decided.get(TX, "undecided")

    @decision.setter
    def decision(self, value):
        self.decided[TX] = value

    def reader(self, scope):
        return AuthorityDecisionReader(fetch=self.fetch_for(scope), authority=AUTHORITY, clock=lambda: NOW)

    def bind(self, scope):
        fetch = self.fetch_for(scope)
        decisions = AuthorityDecisionReader(fetch=fetch, authority=AUTHORITY, clock=lambda: NOW)
        intents = AuthorityCardIntentSource(store=self.store, fetch=fetch, authority=AUTHORITY, clock=lambda: NOW,
                                            authority_id=PEER, scope_field="project_ref")
        return ScopeBinding(participant=HubCardParticipant(service=self.service, store=self.store, intents=intents,
                                                           decisions=decisions),
                            decisions=decisions)

    def operation(self, *, enabled=True):
        return CardTransactionParticipantOperation(callers={PEER: self.caller}, card_store=self.store,
                                                   nonces=self.nonces, enabled=enabled, clock=lambda: NOW)

    def fetch_for(self, scope):
        async def fetch(transaction_id, phase, request_echo):
            record = self.records.get((scope, transaction_id))
            if record is None:
                raise TransactionAuthorityRefused("authority_transaction_unknown")
            decision = self.decided.get(transaction_id, "undecided")
            if phase == "decision" and decision == "undecided":
                raise TransactionAuthorityRefused("authority_decision_pending")
            if phase == "stage" and decision != "undecided":  # PB's stage window closes at the decision
                raise TransactionAuthorityRefused("authority_late_stage")
            intent = record.intent
            unsigned = {
                "schema": PROTOCOL, "phase": phase, "request_echo": request_echo, "audience": AUTHORITY.audience,
                "participant": PARTICIPANT, "global_intent_bytes": intent.canonical_bytes.decode("utf-8"),
                "global_intent_digest": intent.digest, "projection": participant_projection(intent, PARTICIPANT),
                "candidate": intent.as_mapping()["payload"]["participant_candidates"][PARTICIPANT],
                "decision": decision if phase == "decision" else "undecided",
                "decided_at": NOW if phase == "decision" else None,
            }
            return {**unsigned, "authority_proof": {
                "service_id": AUTHORITY.service_id, "timestamp": str(NOW),
                "signature": card_authority_signature(unsigned, secret=AUTHORITY_SECRET,
                                                      service_id=AUTHORITY.service_id, timestamp=str(NOW))}}
        return fetch


def _record(project, txid, participant_input, candidate_value, *, request_id="pb-op"):
    draft = IntentDraft(replay_scope="pb:" + project, request_id=request_id, expires_at=NOW + 600,
                        participants=(PARTICIPANT,),
                        payload={"project_ref": project, "participant_inputs": {PARTICIPANT: participant_input},
                                 "participant_candidates": {PARTICIPANT: candidate_value}})
    return DecisionRecord(draft.bind(txid, 1), "preparing", {}, {})


async def _world(tmp_path):
    store, service, before, after = await _setup(tmp_path)
    service.bind_effect_applier(_Applier())
    accepted = VECTORS["accepted"]
    record = _record(PROJECT, TX, accepted["participant_input"], accepted["candidate_value"])
    return _World(store, service, {(PROJECT, TX): record}), before, after


def _request(action, *, transaction_id=TX, decision=None, limit=None, cursor=None, scope=PROJECT,
             secret=REQUEST_SECRET, service_id=PEER, echo=None, nonce=None):
    data = {"schema": REQUEST_SCHEMA, "action": action, "request_echo": echo or os.urandom(16).hex(),
            "scope": scope, "transaction_id": transaction_id, "decision": decision, "limit": limit,
            "cursor": cursor}
    proof = {"service_id": service_id, "timestamp": str(NOW), "nonce": nonce or os.urandom(12).hex()}
    proof["signature"] = sign_admission_request(
        secret=secret, **proof, delegated_token=f"{REQUEST_SCHEMA}:{data['request_echo']}",
        request=AdmissionRequest(resource="connection-hub@1-0", operation=OPERATION,
                                 invocation_id=data["request_echo"], request_digest=request_digest(data),
                                 approval_context={"protocol": REQUEST_SCHEMA}))
    return {**data, "service_proof": proof}


def _verified(response, request):
    """PB's verifier, as the shared helper does it: every request field echoed, the proof exact and fresh."""
    from service_foundation.coordination.participant_answer import verify_participant_answer

    unsigned_request = {name: request[name] for name in REQUEST_FIELDS}
    result = verify_participant_answer(response["participant_answer"], schema=ANSWER_SCHEMA,
                                       secret=RECEIPT_SECRET, signer_id="connection-hub@1-0",
                                       audience="problem-board@1-0", direction="hub-to-authority",
                                       request=unsigned_request, now=NOW)
    assert response["participant_answer"]["request_digest"] == request_digest(request)
    assert response["ok"] is (result["kind"] != "refused")
    return result


@pytest.mark.asyncio
async def test_prepare_read_pending_and_finish_through_the_operation(tmp_path):
    world, before, after = await _world(tmp_path)
    operation = world.operation()
    request = _request("prepare")
    prepared = _verified(await operation.answer(request), request)
    assert prepared["kind"] == "receipt"
    assert prepared["receipt"]["candidate_digest"] == VECTORS["accepted"]["candidate_digest"]
    assert set(prepared["receipt"]) == {"transaction_id", "epoch", "global_intent_digest", "participant",
                                        "projection_digest", "candidate_digest", "receipt_digest"}
    recorded = await LocalCardIntentSource(world.store).load(TX)
    assert (recorded.authority, recorded.scope) == (PEER, PROJECT)

    request = _request("read_pending")
    assert _verified(await operation.answer(request), request) == {"kind": "pending", "receipt": prepared["receipt"]}

    world.decision = "committed"
    # The Card store's reader routes this transaction's decision to its authority: it sees
    # COMMITTED (effects pending), not card_transaction_undecided.
    from connection_hub.delegated_credentials.cards.store import CardStorageError
    with pytest.raises(CardStorageError, match="card_effects_pending"):
        await world.store.read_current_authority(subject_hash=recorded.subject_hash,
                                                 access_id=recorded.original.access_id)
    request = _request("finish", decision="committed")
    finished = _verified(await operation.answer(request), request)
    assert finished["kind"] == "receipt" and finished["receipt"]["transaction_id"] == TX
    assert (await tx.state(world.store, transaction_id=TX))["state"] == "committed"


@pytest.mark.asyncio
@pytest.mark.parametrize("recorded, claimed", [("committed", "aborted"), ("aborted", "committed"),
                                               ("undecided", "committed")])
async def test_finish_never_applies_the_callers_claimed_decision(tmp_path, recorded, claimed):
    world, before, after = await _world(tmp_path)
    operation = world.operation()
    await operation.answer(_request("prepare"))
    world.decision = recorded
    request = _request("finish", decision=claimed)
    assert _verified(await operation.answer(request), request) == {
        "kind": "refused", "code": "card_decision_mismatch", "status": 409}
    assert (await tx.state(world.store, transaction_id=TX))["state"] == "prepared"


@pytest.mark.asyncio
async def test_another_scope_for_a_known_transaction_is_refused_before_staging(tmp_path):
    # The scope reaches the authority fetch: another project's lookup does not know TX.
    world, before, after = await _world(tmp_path)
    request = _request("prepare", scope="work:project:other")
    assert _verified(await world.operation().answer(request), request) == {
        "kind": "refused", "code": "transaction_unknown", "status": 404}
    assert await tx.state(world.store, transaction_id=TX) is None


@pytest.mark.asyncio
async def test_an_authority_answer_naming_another_project_is_refused(tmp_path):
    # Defence in depth: even if the authority returns TX under "other", its signed
    # intent names PROJECT, so the verified payload check refuses it.
    world, before, after = await _world(tmp_path)
    world.records[("work:project:other", TX)] = world.record
    request = _request("prepare", scope="work:project:other")
    assert _verified(await world.operation().answer(request), request)["code"] == "card_intent_not_bound"
    assert await tx.state(world.store, transaction_id=TX) is None


@pytest.mark.asyncio
async def test_one_caller_serves_two_projects(tmp_path):
    # EMain #605: per-scope binding, so one PB caller stages and finishes in each of its projects.
    from connection_hub.delegated_credentials.cards.card_participant import candidate_value, hub_participant_input

    world, before, after = await _world(tmp_path)
    scope = VECTORS["accepted"]["participant_input"]["target_scope"]
    second = replace(before, access_id="aut_second", label="second project's Card")
    await world.service.commit(second, subject_hash=scope, expected_revision=0, now=NOW)
    edited = replace(second, card_revision=second.card_revision + 1, label="edited in project two")
    other_tx, project_two = "e" * 64, "work:project:two"
    world.records[(project_two, other_tx)] = _record(
        project_two, other_tx,
        hub_participant_input(original=second, candidate=edited, subject_hash=scope, action="update",
                              actor_subject="user:project-admin", actor_kind="caller"),
        candidate_value(original=second, candidate=edited, effects=()), request_id="pb-two")
    operation = world.operation()
    for project, txid in ((PROJECT, TX), (project_two, other_tx)):
        request = _request("prepare", transaction_id=txid, scope=project)
        assert _verified(await operation.answer(request), request)["kind"] == "receipt", project
    request = _request("finish", transaction_id=other_tx, scope=PROJECT, decision="aborted")
    world.decided[other_tx] = "aborted"
    assert _verified(await operation.answer(request), request)["code"] == "transaction_unknown"
    for project, txid in ((PROJECT, TX), (project_two, other_tx)):
        world.decided[txid] = "aborted"
        request = _request("finish", transaction_id=txid, scope=project, decision="aborted")
        assert _verified(await operation.answer(request), request)["kind"] == "receipt", project
        assert (await tx.state(world.store, transaction_id=txid))["state"] == "aborted"


@pytest.mark.asyncio
async def test_an_unknown_transaction_is_a_signed_refusal(tmp_path):
    world, before, after = await _world(tmp_path)
    request = _request("read_pending", transaction_id="d" * 64)
    assert _verified(await world.operation().answer(request), request)["code"] == "transaction_unknown"


@pytest.mark.asyncio
async def test_a_refused_prepare_then_the_authoritys_abort_finishes_with_a_tombstone(tmp_path):
    # EMain R2: the base moved, prepare is refused (signed), PB aborts, finish(aborted) is receipted.
    world, before, after = await _world(tmp_path)
    await world.service.commit(replace(before, card_revision=before.card_revision + 1, label="moved"),
                               subject_hash=VECTORS["accepted"]["participant_input"]["target_scope"],
                               expected_revision=before.card_revision, now=NOW)
    operation = world.operation()
    request = _request("prepare")
    assert _verified(await operation.answer(request), request)["code"] == "card_intent_base_moved"
    world.decision = "aborted"
    request = _request("finish", decision="aborted")
    finished = _verified(await operation.answer(request), request)
    assert finished["kind"] == "receipt" and finished["receipt"]["transaction_id"] == TX


@pytest.mark.asyncio
async def test_disabled_refuses_new_prepare_but_finishes_prepared_work(tmp_path):
    world, before, after = await _world(tmp_path)
    request = _request("prepare")
    assert _verified(await world.operation(enabled=False).answer(request), request) == {
        "kind": "refused", "code": "card_transactions_unavailable", "status": 503}
    await world.operation().answer(_request("prepare"))
    world.decision = "aborted"
    request = _request("finish", decision="aborted")
    assert _verified(await world.operation(enabled=False).answer(request), request)["kind"] == "receipt"


@pytest.mark.asyncio
async def test_list_prepared_pages_this_callers_scope_and_recovers_after_a_restart(tmp_path):
    world, before, after = await _world(tmp_path)
    await world.operation().answer(_request("prepare"))
    # A restart: a fresh world (participant, reader, operation) over the same Card store.
    fresh = _World(world.store, world.service, world.records)
    operation = fresh.operation()
    request = _request("list_prepared", transaction_id=None, limit=1)
    page = _verified(await operation.answer(request), request)
    assert page["kind"] == "page" and [r["transaction_id"] for r in page["receipts"]] == [TX]
    assert page["next_cursor"] is None
    request = _request("list_prepared", transaction_id=None, limit=1, cursor=TX)
    assert _verified(await operation.answer(request), request) == {"kind": "page", "receipts": [], "next_cursor": None}
    request = _request("list_prepared", transaction_id=None, limit=5, scope="work:project:other")
    assert _verified(await operation.answer(request), request)["receipts"] == []  # another scope sees nothing
    request = _request("list_prepared", transaction_id=None, limit=5, cursor=TX, scope="work:project:other")
    assert _verified(await operation.answer(request), request)["code"] == "card_participant_cursor_invalid"
    request = _request("list_prepared", transaction_id=None, limit=5, cursor="e" * 64)
    assert _verified(await operation.answer(request), request)["code"] == "card_participant_cursor_invalid"
    fresh.decision = "committed"
    request = _request("finish", decision="committed")
    assert _verified(await operation.answer(request), request)["kind"] == "receipt"
    request = _request("list_prepared", transaction_id=None, limit=5)
    assert _verified(await operation.answer(request), request)["receipts"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["schema", "direction", "audience", "request_echo", "request_digest", "action",
                                   "scope", "transaction_id", "decision", "limit", "cursor", "result"])
async def test_every_signed_answer_field_is_tamper_evident(tmp_path, field):
    world, before, after = await _world(tmp_path)
    request = _request("prepare")
    from service_foundation.coordination.participant_answer import (
        ParticipantAnswerRefused, verify_participant_answer,
    )

    answer = dict((await world.operation().answer(request))["participant_answer"])
    answer[field] = "f" * 32 if isinstance(answer[field], str) else {"tampered": True}
    with pytest.raises(ParticipantAnswerRefused):
        verify_participant_answer(answer, schema=ANSWER_SCHEMA, secret=RECEIPT_SECRET,
                                  signer_id="connection-hub@1-0", audience="problem-board@1-0",
                                  direction="hub-to-authority",
                                  request={name: request[name] for name in REQUEST_FIELDS}, now=NOW)


@pytest.mark.asyncio
async def test_unauthenticated_requests_get_an_unsigned_refusal_and_change_nothing(tmp_path):
    world, before, after = await _world(tmp_path)
    operation = world.operation()
    unknown = await operation.answer(_request("prepare", service_id="someone-else"))
    assert unknown == {"ok": False, "status": 403, "error": {"code": "card_participant_caller_unknown"}}
    wrong_key = await operation.answer(_request("prepare", secret="x" * 40))
    assert wrong_key["error"]["code"] == "card_participant_unauthenticated"
    tampered = _request("prepare")
    tampered["scope"] = "work:project:other"  # changed after signing
    assert (await operation.answer(tampered))["error"]["code"] == "card_participant_unauthenticated"
    replayed = _request("prepare", nonce="ab" * 12)
    assert (await operation.answer(replayed))["ok"] is True
    again = _request("prepare", nonce="ab" * 12)
    assert (await operation.answer(again))["error"]["code"] == "card_participant_unauthenticated"
    assert all("participant_answer" not in answer for answer in (unknown, wrong_key))


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"extra": 1}, {"decision": "committed"}, {"limit": 5}, {"request_echo": "AB" * 16},
    {"request_echo": "ab"}, {"schema": "card-transaction-participant.v0"}, {"action": "commit"},
    {"transaction_id": None}, {"scope": ""},
])
async def test_malformed_requests_are_refused_unsigned(tmp_path, change):
    world, before, after = await _world(tmp_path)
    request = {**_request("prepare"), **change}
    assert await world.operation().answer(request) == {
        "ok": False, "status": 400, "error": {"code": "card_participant_request_invalid"}}


@pytest.mark.asyncio
async def test_a_missing_field_is_malformed_not_defaulted(tmp_path):
    world, before, after = await _world(tmp_path)
    request = _request("prepare")
    del request["cursor"]
    assert (await world.operation().answer(request))["error"]["code"] == "card_participant_request_invalid"


@pytest.mark.asyncio
async def test_a_transaction_staged_for_one_scope_is_not_readable_under_another(tmp_path):
    world, before, after = await _world(tmp_path)
    operation = world.operation()
    await operation.answer(_request("prepare"))
    # The authority's intent names PROJECT; a request naming another scope never reads it.
    request = _request("read_pending", scope="work:project:other")
    assert _verified(await operation.answer(request), request)["code"] == "transaction_unknown"
    world.records[("work:project:other", TX)] = world.record  # an authority answering under another scope
    request = _request("read_pending", scope="work:project:other")
    assert _verified(await operation.answer(request), request)["code"] == "card_intent_not_bound"


def test_short_caller_keys_are_refused_at_configuration():
    with pytest.raises(ValueError, match="secret_too_short"):
        ParticipantCaller(service_id=PEER, request_secret="short", receipt_secret=RECEIPT_SECRET,
                          receipt_signer_id="hub", audience="pb", hub_resource="hub", bind=None)


@pytest.mark.asyncio
async def test_the_authenticated_request_is_frozen_before_any_await(tmp_path):
    # CodeApp 19:35: a caller object mutated during the nonce await must not move the dispatch.
    world, before, after = await _world(tmp_path)
    request = _request("prepare")

    class _MutatingNonces(_Nonces):
        async def set(self, key, value, *, ex, nx):
            request["scope"] = "work:project:other"
            request["action"] = "read_pending"
            return await super().set(key, value, ex=ex, nx=nx)

    world.nonces = _MutatingNonces()
    frozen = {**request}
    answer = await world.operation().answer(request)
    result = _verified(answer, frozen)
    assert result["kind"] == "receipt" and answer["participant_answer"]["scope"] == PROJECT
    assert (await tx.state(world.store, transaction_id=TX))["state"] == "prepared"

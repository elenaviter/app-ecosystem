"""W750: a prepared policy effect is settled through its Card decision authority."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from service_foundation.coordination.durable_decision_log import Coordinator, IntentDraft, DecisionRefused

from connection_hub.delegated_credentials.cards.card_participant import (
    PARTICIPANT, CardGroupIntent, CardGroupMemberIntent, CardIntent, HubCardParticipant,
    LocalCardIntentSource, hub_participant_input,
)
from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.card_group import group_member, hub_group_participant_input
from connection_hub.delegated_credentials.cards.effect_targets import compose_card_effects
from connection_hub.delegated_credentials.cards.participant_operation import RoutedDecisionPort, ScopeBinding
from connection_hub.delegated_credentials.cards.policy_marker_settlement import CardPolicyMarkerSettler
from connection_hub.delegated_credentials.cards.transaction_store import read_receipt
from connection_hub.delegated_credentials.cards.service import CardServingUnavailable
from connection_hub.delegated_credentials.cards.store import CardStorageError
from connection_hub.invocation_policy import InvocationPolicyConflict, POLICY_ALWAYS, POLICY_ONCE
from connection_hub.invocation_policy.models import POLICY_CHANGE_COMMITTED, POLICY_CHANGE_PREPARED
from test_card_coordinated_writes import _host, _pg_host
from test_card_participant import _Store, _Verifier
from test_invocation_policy import _authority, _service
from test_card_service import SUBJECT_HASH
from connection_hub.invocation_policy.service import invocation_policy_conflict_response


async def _staged(tmp_path, *, decision="undecided", caller_staged=False, host_data=None):
    host, store, decisions, before, after = host_data or await _host(tmp_path)
    policies = _service(tmp_path / "policies")
    authority = replace(_authority(operation="restart"), access_id=before.access_id)
    effect = {"kind": "invocation_policy", "key": authority.key, "payload": {
        "owner_subject": before.grantor_subject, "authority": authority.to_dict(),
        "mode": POLICY_ONCE, "expected_revision": 0,
    }}
    local_coordinator = host._card_coordinator[0]
    coordinator = local_coordinator
    hub = coordinator.participants[PARTICIPANT]
    callers = {}
    if caller_staged:
        decisions = _Store()
        hub = HubCardParticipant(service=hub._service, store=store,
                                 intents=LocalCardIntentSource(store), decisions=decisions)
        coordinator = Coordinator(decisions, {PARTICIPANT: hub}, _Verifier())
        scope = "work:project:w750"
        tx.bind_transaction_decisions(store, RoutedDecisionPort(
            local=local_coordinator.store, card_store=store,
            authorities={"problem-board": lambda received: decisions if received == scope else None},
        ))
        callers = {"problem-board": SimpleNamespace(bind=lambda received: ScopeBinding(
            participant=hub, decisions=decisions) if received == scope else None)}
    compose_card_effects(card_service=hub._service, card_store=store, grant_store=object(), policies=policies)
    draft = IntentDraft(
        replay_scope="w750:card", request_id="w750-policy", expires_at=1_900_000_000,
        participants=(PARTICIPANT,), payload={"participant_inputs": {PARTICIPANT: hub_participant_input(
            original=before, candidate=after, subject_hash=SUBJECT_HASH, action="update",
            actor_subject=before.grantor_subject, actor_kind="grantor", effects=(effect,),
        )}},
    )
    row = await decisions.begin(draft)
    await LocalCardIntentSource(store).record(CardIntent(
        transaction_id=row.transaction_id, intent_digest=row.intent.digest,
        subject_hash=SUBJECT_HASH, original=before, candidate=after, effects=(effect,),
        action="update", actor_subject=before.grantor_subject, actor_kind="grantor",
        authority="problem-board" if caller_staged else "", scope=scope if caller_staged else "",
    ))
    await coordinator.prepare_existing(row.transaction_id)
    marker = await policies.get_policy_change(owner_subject=before.grantor_subject, authority=authority)
    assert marker.state == POLICY_CHANGE_PREPARED
    if decision != "undecided":
        await coordinator.decide(row.transaction_id, decision,
                                 witness_digest="a" * 64 if decision == "committed" else "")
    policies.bind_transaction_settler(CardPolicyMarkerSettler(
        card_store=store, coordinator=local_coordinator, callers=callers, max_wait_seconds=1,
    ))
    return policies, authority, before, store, coordinator, decisions, row


@pytest.mark.asyncio
async def test_committed_card_is_finished_before_new_direct_policy_write(tmp_path):
    policies, authority, before, store, coordinator, decisions, row = await _staged(tmp_path, decision="committed")
    # A crash between the durable COMMIT and FINISH leaves the marker in PREPARED.
    assert not (await decisions.read(row.transaction_id)).finished
    saved = await policies.set_policy(owner_subject=before.grantor_subject, authority=authority,
                                      mode=POLICY_ALWAYS)
    assert saved.mode == POLICY_ALWAYS and saved.revision == 2
    assert (await decisions.read(row.transaction_id)).finished
    marker = await policies.get_policy_change(owner_subject=before.grantor_subject, authority=authority)
    assert marker.state == POLICY_CHANGE_COMMITTED and marker.policy_revision == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("next_change", ("set", "prepare"))
@pytest.mark.parametrize("caller_staged", (False, True))
async def test_finished_card_marker_allows_the_next_policy_change(
    tmp_path, next_change, caller_staged,
):
    policies, authority, before, store, coordinator, decisions, row = await _staged(
        tmp_path, decision="committed", caller_staged=caller_staged,
    )
    await coordinator.finish(row.transaction_id)
    receipt = await read_receipt(store, row.transaction_id)
    assert receipt["intent_finished"] is True
    assert not await tx.pending_effects(store, receipt)
    finished = (await decisions.read(row.transaction_id)).finished
    assert finished

    if next_change == "set":
        saved = await policies.set_policy(
            owner_subject=before.grantor_subject, authority=authority,
            mode=POLICY_ALWAYS, expected_revision=1,
        )
        assert (saved.mode, saved.revision) == (POLICY_ALWAYS, 2)
    else:
        prepared = await policies.prepare_policy_change(
            owner_subject=before.grantor_subject, authority=authority,
            mode=POLICY_ALWAYS, change_id="next-direct", expected_revision=1,
        )
        assert prepared.change_id == "next-direct"
        assert prepared.state == POLICY_CHANGE_PREPARED
    assert (await decisions.read(row.transaction_id)).finished == finished
    assert (await store.read_current_authority(
        subject_hash=SUBJECT_HASH, access_id=before.access_id,
    ))[1].card_revision == before.card_revision + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("next_change", ("set", "prepare"))
@pytest.mark.parametrize("finish_complete", (False, True))
async def test_committed_marker_without_recovery_binding_stays_closed(
    tmp_path, monkeypatch, next_change, finish_complete,
):
    policies, authority, before, store, coordinator, decisions, row = await _staged(
        tmp_path, decision="committed",
    )
    if finish_complete:
        await coordinator.finish(row.transaction_id)
    else:
        original_write = tx.write_json_atomic

        async def fail_effect_ack(path, payload):
            if path == tx.effects_path(store, row.transaction_id):
                raise OSError("injected_after_policy_commit")
            return await original_write(path, payload)

        monkeypatch.setattr(tx, "write_json_atomic", fail_effect_ack)
        with pytest.raises(CardServingUnavailable, match="card_effects_pending"):
            await coordinator.finish(row.transaction_id)
    marker = await policies.get_policy_change(owner_subject=before.grantor_subject, authority=authority)
    assert marker.state == POLICY_CHANGE_COMMITTED
    receipt = await read_receipt(store, row.transaction_id)
    assert bool(receipt.get("intent_finished")) is finish_complete
    # The marker alone cannot distinguish completed FINISH from a crash before
    # effect acknowledgement. Turning recovery off does not prove completion.
    unbound = _service(tmp_path / "policies")
    with pytest.raises(InvocationPolicyConflict, match="invocation_policy_change_in_progress"):
        if next_change == "set":
            await unbound.set_policy(owner_subject=before.grantor_subject, authority=authority,
                                     mode=POLICY_ALWAYS)
        else:
            await unbound.prepare_policy_change(
                owner_subject=before.grantor_subject, authority=authority,
                mode=POLICY_ALWAYS, change_id="next-direct",
            )
    assert (await policies.get(owner_subject=before.grantor_subject, authority=authority)).revision == 1
    if not finish_complete:
        assert len(await tx.pending_effects(store, receipt)) == 1
        assert not (await decisions.read(row.transaction_id)).finished
        with pytest.raises(CardStorageError, match="card_effects_pending"):
            await store.read_current_authority(subject_hash=SUBJECT_HASH, access_id=before.access_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("next_change", ("set", "prepare"))
@pytest.mark.parametrize("caller_staged", (False, True))
async def test_crash_after_policy_commit_before_effect_ack_finishes_card_first(
    tmp_path, monkeypatch, next_change, caller_staged,
):
    policies, authority, before, store, coordinator, decisions, row = await _staged(
        tmp_path, decision="committed", caller_staged=caller_staged,
    )
    original_write = tx.write_json_atomic
    failed = False

    async def fail_effect_ack(path, payload):
        nonlocal failed
        if path == tx.effects_path(store, row.transaction_id) and not failed:
            failed = True
            raise OSError("injected_after_policy_commit")
        return await original_write(path, payload)

    monkeypatch.setattr(tx, "write_json_atomic", fail_effect_ack)
    with pytest.raises(CardServingUnavailable, match="card_effects_pending"):
        await coordinator.finish(row.transaction_id)
    assert failed
    marker = await policies.get_policy_change(owner_subject=before.grantor_subject, authority=authority)
    assert marker.state == POLICY_CHANGE_COMMITTED
    receipt = await read_receipt(store, row.transaction_id)
    assert receipt["state"] == "committed"
    assert len(await tx.pending_effects(store, receipt)) == 1
    assert not (await decisions.read(row.transaction_id)).finished
    with pytest.raises(CardStorageError, match="card_effects_pending"):
        await store.read_current_authority(subject_hash=SUBJECT_HASH, access_id=before.access_id)

    if next_change == "set":
        saved = await policies.set_policy(owner_subject=before.grantor_subject, authority=authority,
                                          mode=POLICY_ALWAYS)
        assert saved.revision == 2 and saved.mode == POLICY_ALWAYS
    else:
        prepared = await policies.prepare_policy_change(
            owner_subject=before.grantor_subject, authority=authority,
            mode=POLICY_ALWAYS, change_id="next-direct", expected_revision=1,
        )
        assert prepared.change_id == "next-direct" and prepared.state == POLICY_CHANGE_PREPARED
    # The new operation itself must FINISH the old Card; an explicit replay
    # after the fact must not be needed to make its authority readable.
    if not caller_staged:
        assert (await decisions.read(row.transaction_id)).finished
    assert (await store.read_current_authority(
        subject_hash=SUBJECT_HASH, access_id=before.access_id))[1].card_revision == before.card_revision + 1
    assert await tx.list_in_doubt(store) == []
    await coordinator.finish(row.transaction_id)
    current = await policies.get(owner_subject=before.grantor_subject, authority=authority)
    receipt = await read_receipt(store, row.transaction_id)
    assert not await tx.pending_effects(store, receipt)
    assert current.revision == (2 if next_change == "set" else 1)


@pytest.mark.asyncio
async def test_recorded_commit_finishes_even_after_decision_wait_window(tmp_path):
    policies, authority, before, store, coordinator, decisions, row = await _staged(
        tmp_path, decision="committed",
    )
    finish = coordinator.finish

    async def slow_finish(transaction_id):
        await asyncio.sleep(0.05)
        return await finish(transaction_id)

    policies.bind_transaction_settler(CardPolicyMarkerSettler(
        card_store=store, coordinator=SimpleNamespace(store=coordinator.store, finish=slow_finish),
        callers={}, max_wait_seconds=0.01,
    ))
    saved = await policies.set_policy(owner_subject=before.grantor_subject, authority=authority,
                                      mode=POLICY_ALWAYS)
    assert saved.revision == 2 and (await decisions.read(row.transaction_id)).finished


@pytest.mark.asyncio
async def test_expired_undecided_card_aborts_and_releases_before_new_prepare(tmp_path):
    policies, authority, before, store, coordinator, decisions, row = await _staged(tmp_path)
    prepared = await policies.prepare_policy_change(
        owner_subject=before.grantor_subject, authority=authority, mode=POLICY_ALWAYS,
        change_id="next-direct", expected_revision=0,
    )
    assert prepared.change_id == "next-direct"
    assert (await decisions.read(row.transaction_id)).state == "aborted"
    assert (await decisions.read(row.transaction_id)).finished
    assert await policies.get(owner_subject=before.grantor_subject, authority=authority) is None


@pytest.mark.asyncio
async def test_live_undecided_card_keeps_marker_and_returns_retryable_conflict(tmp_path):
    policies, authority, before, store, coordinator, decisions, row = await _staged(tmp_path)

    async def not_expired(transaction_id):
        raise DecisionRefused("not_expired")

    decisions.abort_expired = not_expired
    with pytest.raises(InvocationPolicyConflict, match="invocation_policy_earlier_change_undecided"):
        await policies.set_policy(owner_subject=before.grantor_subject, authority=authority,
                                  mode=POLICY_ALWAYS)
    marker = await policies.get_policy_change(owner_subject=before.grantor_subject, authority=authority)
    assert marker.change_id.startswith(row.transaction_id + ":")
    assert not (await decisions.read(row.transaction_id)).terminal


@pytest.mark.asyncio
async def test_unreachable_receipt_lookup_stays_bounded_and_names_only_the_member(tmp_path, monkeypatch):
    policies, authority, before, store, coordinator, _decisions, row = await _staged(tmp_path)
    marker = await policies.get_policy_change(owner_subject=before.grantor_subject, authority=authority)
    never_answers = asyncio.Event()

    async def unreadable(_store, _member_id):
        await never_answers.wait()

    monkeypatch.setattr(
        "connection_hub.delegated_credentials.cards.policy_marker_settlement.read_receipt",
        unreadable,
    )
    settler = CardPolicyMarkerSettler(
        card_store=store, coordinator=coordinator, callers={}, max_wait_seconds=0.02,
    )
    started = asyncio.get_running_loop().time()
    with pytest.raises(InvocationPolicyConflict, match="invocation_policy_earlier_change_undecided") as raised:
        await settler(marker, before.grantor_subject)
    assert asyncio.get_running_loop().time() - started < 0.2
    assert invocation_policy_conflict_response(raised.value)["transaction_ref"] == {
        "member_transaction_id": row.transaction_id,
    }
    assert (await policies.get_policy_change(owner_subject=before.grantor_subject,
                                             authority=authority)).state == POLICY_CHANGE_PREPARED


@pytest.mark.asyncio
async def test_stale_revision_does_not_finish_an_earlier_committed_change(tmp_path):
    policies, authority, before, store, coordinator, decisions, row = await _staged(
        tmp_path, decision="committed",
    )
    with pytest.raises(InvocationPolicyConflict, match="invocation_policy_revision_moved"):
        await policies.set_policy(owner_subject=before.grantor_subject, authority=authority,
                                  mode=POLICY_ALWAYS, expected_revision=1)
    assert not (await decisions.read(row.transaction_id)).finished
    assert (await policies.get_policy_change(owner_subject=before.grantor_subject,
                                             authority=authority)).state == POLICY_CHANGE_PREPARED


@pytest.mark.asyncio
async def test_missing_caller_authority_keeps_the_exact_prepared_marker(tmp_path):
    policies, authority, before, store, coordinator, decisions, row = await _staged(
        tmp_path, decision="committed", caller_staged=True,
    )
    policies.bind_transaction_settler(CardPolicyMarkerSettler(
        card_store=store, coordinator=coordinator, callers={}, max_wait_seconds=0.05,
    ))
    with pytest.raises(InvocationPolicyConflict, match="invocation_policy_earlier_change_undecided"):
        await policies.prepare_policy_change(owner_subject=before.grantor_subject, authority=authority,
                                             mode=POLICY_ALWAYS, change_id="next-direct")
    assert (await policies.get_policy_change(owner_subject=before.grantor_subject,
                                             authority=authority)).state == POLICY_CHANGE_PREPARED


@pytest.mark.asyncio
async def test_unreachable_caller_decision_read_is_bounded(tmp_path):
    policies, authority, before, store, coordinator, decisions, row = await _staged(
        tmp_path, decision="committed", caller_staged=True,
    )
    unavailable = asyncio.Event()

    class _Unreachable:
        async def read(self, transaction_id):
            await unavailable.wait()

    policies.bind_transaction_settler(CardPolicyMarkerSettler(
        card_store=store, coordinator=coordinator,
        callers={"problem-board": SimpleNamespace(bind=lambda scope: ScopeBinding(
            participant=coordinator.participants[PARTICIPANT], decisions=_Unreachable()))},
        max_wait_seconds=0.01,
    ))
    with pytest.raises(InvocationPolicyConflict, match="invocation_policy_earlier_change_undecided"):
        await policies.set_policy(owner_subject=before.grantor_subject, authority=authority,
                                  mode=POLICY_ALWAYS)
    assert not unavailable.is_set()
    assert (await policies.get_policy_change(owner_subject=before.grantor_subject,
                                             authority=authority)).state == POLICY_CHANGE_PREPARED


@pytest.mark.asyncio
async def test_group_lead_member_marker_recovers_the_group_decision(tmp_path):
    host, store, decisions, before, after = await _host(tmp_path)
    policies = _service(tmp_path / "policies")
    authority = replace(_authority(operation="restart"), access_id=before.access_id)
    effect = {"kind": "invocation_policy", "key": authority.key, "payload": {
        "owner_subject": before.grantor_subject, "authority": authority.to_dict(),
        "mode": POLICY_ONCE, "expected_revision": 0,
    }}
    created = replace(after, access_id="aut_zz_new", card_revision=1, label="created in group")
    members = (CardGroupMemberIntent(SUBJECT_HASH, before, after, "update"),
               CardGroupMemberIntent(SUBJECT_HASH, None, created, "create"))
    projection = hub_group_participant_input(
        members=[group_member(original=item.original, candidate=item.candidate, action=item.action)
                 for item in members], actor_subject=before.grantor_subject, actor_kind="grantor",
        effects=(effect,),
    )
    draft = IntentDraft(replay_scope="w750:group", request_id="w750-group", expires_at=4_000_000_000,
                        participants=(PARTICIPANT,), payload={"participant_inputs": {PARTICIPANT: projection}})
    row = await decisions.begin(draft)
    old_hub = host._card_coordinator[0].participants[PARTICIPANT]
    compose_card_effects(card_service=old_hub._service, card_store=store, grant_store=object(), policies=policies)
    source = LocalCardIntentSource(store, service=old_hub._service, decisions=decisions)
    await source.record(CardGroupIntent(row.transaction_id, row.intent.digest, members,
                                        effects=(effect,), actor_subject=before.grantor_subject,
                                        actor_kind="grantor"))
    hub = HubCardParticipant(service=old_hub._service, store=store, intents=source, decisions=decisions)
    coordinator = Coordinator(decisions, {PARTICIPANT: hub}, _Verifier())
    await coordinator.prepare_existing(row.transaction_id)
    marker = await policies.get_policy_change(owner_subject=before.grantor_subject, authority=authority)
    assert marker.change_id == f"{tx.member_transaction_id(row.transaction_id, 0)}:{authority.key}"
    assert not (await decisions.read(row.transaction_id)).terminal
    async def not_expired(_transaction_id):
        raise DecisionRefused("not_expired")

    policies.bind_transaction_settler(CardPolicyMarkerSettler(
        card_store=store,
        coordinator=SimpleNamespace(
            store=SimpleNamespace(read=decisions.read, abort_expired=not_expired),
            finish=coordinator.finish,
        ),
        callers={}, max_wait_seconds=0.01,
    ))
    with pytest.raises(InvocationPolicyConflict, match="invocation_policy_earlier_change_undecided") as raised:
        await policies.set_policy(owner_subject=before.grantor_subject, authority=authority,
                                  mode=POLICY_ALWAYS)
    refusal = invocation_policy_conflict_response(raised.value)
    assert refusal["retryable"] is True
    assert refusal["transaction_ref"] == {
        "member_transaction_id": tx.member_transaction_id(row.transaction_id, 0),
        "decision_transaction_id": row.transaction_id,
    }
    assert (await decisions.read(row.transaction_id)).state == "prepared"
    await coordinator.decide(row.transaction_id, "committed", witness_digest="a" * 64)
    policies.bind_transaction_settler(CardPolicyMarkerSettler(
        card_store=store, coordinator=coordinator, callers={}, max_wait_seconds=1,
    ))
    saved = await policies.set_policy(owner_subject=before.grantor_subject, authority=authority,
                                      mode=POLICY_ALWAYS)
    assert saved.revision == 2 and (await decisions.read(row.transaction_id)).finished


@pytest.mark.asyncio
async def test_postgres_commit_is_finished_before_new_policy_write(tmp_path):
    host, store, decisions, before, after, drop = await _pg_host(tmp_path)
    try:
        policies, authority, before, store, coordinator, decisions, row = await _staged(
            tmp_path, decision="committed", host_data=(host, store, decisions, before, after),
        )
        saved = await policies.set_policy(owner_subject=before.grantor_subject, authority=authority,
                                          mode=POLICY_ALWAYS)
        assert saved.revision == 2
        assert (await decisions.read(row.transaction_id)).finished
        assert await decisions.list_in_doubt(limit=10) == []
    finally:
        await drop()


@pytest.mark.asyncio
async def test_caller_staged_commit_finishes_using_its_authority(tmp_path):
    policies, authority, before, store, coordinator, decisions, row = await _staged(
        tmp_path, decision="committed", caller_staged=True,
    )
    saved = await policies.set_policy(owner_subject=before.grantor_subject, authority=authority,
                                      mode=POLICY_ALWAYS)
    assert saved.revision == 2
    assert (await read_receipt(store, row.transaction_id))["state"] == "committed"


@pytest.mark.asyncio
async def test_caller_staged_undecided_never_gets_a_hub_abort(tmp_path):
    policies, authority, before, store, coordinator, decisions, row = await _staged(
        tmp_path, caller_staged=True,
    )
    with pytest.raises(InvocationPolicyConflict, match="invocation_policy_earlier_change_undecided"):
        await policies.set_policy(owner_subject=before.grantor_subject, authority=authority,
                                  mode=POLICY_ALWAYS)
    assert decisions.decisions == []
    assert (await decisions.read(row.transaction_id)).state == "prepared"

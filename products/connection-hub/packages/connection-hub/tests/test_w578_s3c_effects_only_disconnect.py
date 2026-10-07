"""W578 S3c: a disconnect of an account no Card binds is the SAME durable transaction, effects only.

CodeApp, 7 October 2026 02:45 UTC: "My preferred zero-Card path is the SAME
generic durable COMMIT/ABORT coordinator with an account-delete effect-only
participant input." On the real Card store, Hub participant, coordinator,
effect applier and account fence (the harness of test_w578_disconnect_group):

- the input is the versioned ``connection-hub.card-effects`` binding; an empty
  one, another effect kind, a Card-bound payload, another owner or a
  non-canonical order is refused, and so is any projection field that differs;
- the account is fenced and the Cards re-listed after the fence: a binding
  that appears aborts; STAGE holds the exact incarnation; COMMIT deletes only
  it; FINISH releases the fence only after the deletion (or the ABORT's
  release);
- every cut CodeApp named: before STAGE, after STAGE, after COMMIT before the
  delete, during the delete, after the delete before FINISH, and an old
  replay after a reconnection and a newer disconnect's fence.
"""

from __future__ import annotations

import pytest

from service_foundation.coordination.durable_decision_log import DecisionRefused

from connection_hub.delegated_credentials.cards import account_fence as fence
from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.card_effects import (
    EFFECTS_BINDING_KIND, effects_candidate_value, hub_effects_participant_input, validate_effects_candidate,
    verify_effects_projection,
)
from connection_hub.delegated_credentials.cards.store import subject_hash_for
from connection_hub.delegated_to_kdcube.models import ConnectedAccount
from connection_hub.delegated_to_kdcube.store import AccountDisconnectPending
from test_card_service import SUBJECT_HASH
from test_w578_disconnect_group import PROVIDER, _transaction_id, _world

FREE = "acct-free"  # connected, bound by no Card


async def _free_world(tmp_path):
    host, store, decisions, accounts, card, _, grantor = await _world(tmp_path)
    connected = await accounts.upsert_account(ConnectedAccount(account_id=FREE, provider_id=PROVIDER))
    return host, store, decisions, accounts, card, connected, grantor


async def _disconnect(host, grantor):
    return await host.disconnect_account_in_transaction(grantor_subject=grantor, provider_id=PROVIDER,
                                                        account_id=FREE)


def _fenced(store) -> bool:
    return (fence._dir(store, PROVIDER, FREE) / "fence.json").exists()


async def _held(accounts) -> bool:
    return await accounts._prop(accounts.account_delete_hold_key(FREE)) is not None


def _effect(grantor, incarnation="inc-1", **payload):
    return {"kind": "account_delete", "key": f"{PROVIDER}:{FREE}",
            "payload": {"access_id": "", "grantor_subject": grantor, "provider_id": PROVIDER, "account_id": FREE,
                        "incarnation": incarnation, **payload}}


# --- the representation -------------------------------------------------------------------------------------------

def test_the_effects_only_input_is_versioned_bounded_and_bound_to_one_owner():
    grantor = "user-1"
    owner = subject_hash_for(grantor)
    projection = hub_effects_participant_input(subject_hash=owner, effects=[_effect(grantor)],
                                               actor_subject=grantor, actor_kind="grantor")
    assert projection["binding_kind"] == EFFECTS_BINDING_KIND and projection["action"] == "effect"
    assert projection["dependency_revisions"] == {} and projection["binding_ref"].startswith("effects:")
    assert (projection["before_revision"], projection["candidate_revision"], projection["target_incarnation"]) == (1, 1, 1)
    value = effects_candidate_value(owner, [_effect(grantor)])
    assert verify_effects_projection(projection, value) == value
    refused = {
        "card_effects_empty": effects_candidate_value(owner, []),
        "card_effects_effect_invalid": effects_candidate_value(owner, [{**_effect(grantor), "kind": "grant_unbind"}]),
        "card_effects_owner_mismatch": effects_candidate_value(subject_hash_for("someone-else"), [_effect(grantor)]),
        "card_effects_too_large": effects_candidate_value(owner, [
            {**_effect(grantor), "key": f"{PROVIDER}:a{i}",
             "payload": {**_effect(grantor)["payload"], "account_id": f"a{i}"}} for i in range(5)]),
    }
    for reason, candidate in refused.items():
        with pytest.raises(DecisionRefused, match=reason):
            validate_effects_candidate(candidate)
    with pytest.raises(DecisionRefused, match="card_effects_effect_invalid"):  # a Card-bound delete is the group's
        validate_effects_candidate(effects_candidate_value(owner, [_effect(grantor, access_id="aut_x")]))
    unordered = {"schema": value["schema"], "subject_hash": owner, "effects": [
        {**_effect(grantor), "key": f"{PROVIDER}:b", "payload": {**_effect(grantor)["payload"], "account_id": "b"}},
        {**_effect(grantor), "key": f"{PROVIDER}:a", "payload": {**_effect(grantor)["payload"], "account_id": "a"}}]}
    with pytest.raises(DecisionRefused, match="card_effects_not_canonical"):
        validate_effects_candidate(unordered)
    for field, wrong in (("action", "update"), ("before_revision", 2), ("dependency_revisions", {"card:x": 1}),
                         ("target_scope", "0" * 64), ("candidate_digest", "0" * 64), ("before_revision", True)):
        with pytest.raises(DecisionRefused, match="card_effects_not_bound"):
            verify_effects_projection({**projection, field: wrong}, value)


def test_a_peer_authority_can_never_send_an_effects_only_candidate():
    """The authority's single-Card candidate check refuses this shape: account deletion is Hub-initiated only."""
    from connection_hub.delegated_credentials.cards.transaction_authority_v2 import CANDIDATE_FIELDS

    value = effects_candidate_value(subject_hash_for("user-1"), [_effect("user-1")])
    assert set(value) != CANDIDATE_FIELDS


# --- the transaction -------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_unbound_account_is_deleted_under_one_effects_only_decision(tmp_path):
    host, store, decisions, accounts, card, connected, grantor = await _free_world(tmp_path)
    result = await _disconnect(host, grantor)
    assert result == {"ok": True, "removed": True, "bindings_cleared": 0, "bindings_cleared_grants": []}
    assert decisions.decisions == ["committed"]
    row = next(iter(decisions.rows.values()))
    assert row.intent.as_mapping()["payload"]["participant_inputs"]["connection-hub.card"]["binding_kind"] \
        == EFFECTS_BINDING_KIND
    assert await accounts.get_account(FREE) is None and FREE not in await accounts._index()
    assert not _fenced(store) and not await _held(accounts)
    assert await tx.list_in_doubt(store) == []
    receipt = await tx.read_receipt(store, _transaction_id(decisions))
    assert tx.is_effects_receipt(receipt) and receipt["state"] == "committed"
    # The Card that binds another account is untouched.
    assert (await store.read_current_authority(subject_hash=SUBJECT_HASH, access_id=card.access_id))[1] == card


@pytest.mark.asyncio
async def test_a_binding_that_appears_after_the_fence_aborts_and_keeps_the_account(tmp_path, monkeypatch):
    host, store, decisions, accounts, card, connected, grantor = await _free_world(tmp_path)
    real = host._account_binding_members
    calls = []

    async def members(*args):
        calls.append(1)
        found = await real(*args)
        if len(calls) == 2:  # the re-list after the fence: a Card that gained the binding
            current = (await store.read_current_authority(subject_hash=SUBJECT_HASH, access_id=card.access_id))[1]
            return [(current, current)]
        return found

    monkeypatch.setattr(host, "_account_binding_members", members)
    result = await _disconnect(host, grantor)
    assert result["error"] == "account_binding_not_pruned" and result["reason"] == "card_account_binding_changed"
    assert decisions.decisions == ["aborted"] and await accounts.get_account(FREE) is not None
    assert not _fenced(store) and not await _held(accounts)


@pytest.mark.asyncio
async def test_while_fenced_a_new_binding_refuses_and_while_held_a_reconnect_refuses(tmp_path, monkeypatch):
    """Cut: after STAGE, before any decision. Recovery's presumed ABORT then releases both."""
    host, store, decisions, accounts, card, connected, grantor = await _free_world(tmp_path)
    coordinator = host._card_coordinator[0]
    real_decide = coordinator.decide

    async def dies_before_deciding(*args, **kwargs):
        raise RuntimeError("process died after STAGE")

    monkeypatch.setattr(coordinator, "decide", dies_before_deciding)
    result = await _disconnect(host, grantor)
    assert result["removed"] is False and result["retryable"] is True
    assert decisions.decisions == [] and _fenced(store) and await _held(accounts)
    with pytest.raises(fence.AccountFenceRefused, match="card_account_reserved"):
        await fence.mark_binding(store, [(PROVIDER, FREE)], mark_id="direct-x", kind="direct",
                                 subject_hash=SUBJECT_HASH, access_id=card.access_id)
    with pytest.raises(AccountDisconnectPending):
        await accounts.upsert_account(ConnectedAccount(account_id=FREE, provider_id=PROVIDER))
    monkeypatch.setattr(coordinator, "decide", real_decide)
    transaction_id = _transaction_id(decisions)
    await coordinator.decide(transaction_id, "aborted")  # recovery presumes the abort
    await coordinator.finish(transaction_id)
    assert (await accounts.get_account(FREE)).incarnation == connected.incarnation
    assert not _fenced(store) and not await _held(accounts) and await tx.list_in_doubt(store) == []


@pytest.mark.asyncio
async def test_a_crash_after_the_fence_before_stage_is_aborted_by_tombstone_and_released(tmp_path, monkeypatch):
    """Cut: before STAGE (fence written, nothing prepared, the initiator's ABORT also lost)."""
    host, store, decisions, accounts, card, connected, grantor = await _free_world(tmp_path)
    coordinator = host._card_coordinator[0]
    real_prepare, real_decide = coordinator.prepare_existing, coordinator.decide

    async def dies(*args, **kwargs):
        raise RuntimeError("process died")

    monkeypatch.setattr(coordinator, "prepare_existing", dies)
    monkeypatch.setattr(coordinator, "decide", dies)
    result = await _disconnect(host, grantor)
    assert result["error"] == "card_transaction_pending" and _fenced(store) and not await _held(accounts)
    monkeypatch.setattr(coordinator, "prepare_existing", real_prepare)
    monkeypatch.setattr(coordinator, "decide", real_decide)
    transaction_id = _transaction_id(decisions)
    await coordinator.decide(transaction_id, "aborted")
    await coordinator.finish(transaction_id)
    assert await tx.read_receipt(store, transaction_id) is None  # never prepared: a tombstone
    assert (store.root / "card-transactions").exists() and not _fenced(store)
    assert await accounts.get_account(FREE) is not None
    with pytest.raises(DecisionRefused, match="card_transaction_aborted"):  # a late stage refuses
        await coordinator.participants["connection-hub.card"].prepare(transaction_id)


@pytest.mark.asyncio
async def test_a_crash_after_commit_before_the_delete_keeps_the_fence_until_recovery_deletes(tmp_path, monkeypatch):
    host, store, decisions, accounts, card, connected, grantor = await _free_world(tmp_path)
    real = accounts.disconnect_incarnation
    failures = []

    async def failing_once(*args, **kwargs):
        if not failures:
            failures.append(1)
            raise RuntimeError("account store unreachable")
        return await real(*args, **kwargs)

    monkeypatch.setattr(accounts, "disconnect_incarnation", failing_once)
    result = await _disconnect(host, grantor)
    assert result["error"] == "card_transaction_pending" and decisions.decisions == ["committed"]
    assert await accounts.get_account(FREE) is not None and _fenced(store) and await _held(accounts)
    entries = await tx.list_in_doubt(store)
    assert [entry.get("effects_only") for entry in entries] == [True] and entries[0]["needs_finish"] is True
    await host._card_coordinator[0].finish(_transaction_id(decisions))  # recovery
    assert await accounts.get_account(FREE) is None and not _fenced(store) and not await _held(accounts)


@pytest.mark.asyncio
async def test_a_crash_during_the_delete_finishes_the_index_and_credential_cleanup_on_recovery(tmp_path, monkeypatch):
    host, store, decisions, accounts, card, connected, grantor = await _free_world(tmp_path)
    await accounts.set_credential(connected.credential_id, {"access_token_ref": "kept-until-deleted"})
    real = accounts.delete_credential
    failures = []

    async def failing_once(credential_id):
        if not failures:
            failures.append(1)
            raise RuntimeError("died inside the deletion")
        return await real(credential_id)

    monkeypatch.setattr(accounts, "delete_credential", failing_once)
    result = await _disconnect(host, grantor)
    assert result["error"] == "card_transaction_pending"
    assert await accounts.get_account(FREE) is None  # the record went; the credential did not
    assert await accounts.get_credential(connected.credential_id)
    await host._card_coordinator[0].finish(_transaction_id(decisions))
    assert not await accounts.get_credential(connected.credential_id)
    assert FREE not in await accounts._index() and not _fenced(store)


@pytest.mark.asyncio
async def test_a_crash_after_the_delete_before_finish_replays_the_pinned_outcome_past_a_reconnect(tmp_path, monkeypatch):
    host, store, decisions, accounts, card, connected, grantor = await _free_world(tmp_path)
    service = host._persistence.card_service
    real = service.release_accounts
    failures = []

    async def failing_once(transaction_id):
        if not failures:
            failures.append(1)
            raise RuntimeError("process died before the release")
        return await real(transaction_id)

    monkeypatch.setattr(service, "release_accounts", failing_once)
    result = await _disconnect(host, grantor)
    assert result["error"] == "card_transaction_pending" and await accounts.get_account(FREE) is None
    assert _fenced(store)
    later = await accounts.upsert_account(ConnectedAccount(account_id=FREE, provider_id=PROVIDER))
    await host._card_coordinator[0].finish(_transaction_id(decisions))  # replays the pinned outcome
    assert (await accounts.get_account(FREE)).incarnation == later.incarnation  # never deleted
    assert not _fenced(store)


@pytest.mark.asyncio
async def test_an_old_replay_never_deletes_a_new_connection_or_touches_a_newer_fence(tmp_path):
    host, store, decisions, accounts, card, connected, grantor = await _free_world(tmp_path)
    assert (await _disconnect(host, grantor))["removed"] is True
    old = _transaction_id(decisions)
    later = await accounts.upsert_account(ConnectedAccount(account_id=FREE, provider_id=PROVIDER))
    newer = "f" * 64
    await host._persistence.card_service.reserve_accounts([(PROVIDER, FREE)], transaction_id=newer)
    await host._card_coordinator[0].finish(old)  # an old replay
    assert (await accounts.get_account(FREE)).incarnation == later.incarnation
    assert await fence.fence_holder(store, PROVIDER, FREE) == newer

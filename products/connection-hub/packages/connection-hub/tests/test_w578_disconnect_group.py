"""W578: an account disconnect is ONE Card group transaction; the account goes only after its COMMIT.

On the real Card store, the real Hub participant and coordinator, the real
effect applier with the ``account_delete`` target over a locked account store,
and the S1 account fence. The decision store is the package's in-memory one.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from service_foundation.coordination.durable_decision_log import Coordinator

from connection_hub.delegated_credentials.automation_access import AutomationAccessService
from connection_hub.delegated_credentials.cards import account_fence as fence
from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.card_participant import (
    PARTICIPANT, DecisionStorePort, HubCardParticipant, LocalCardIntentSource,
)
from connection_hub.delegated_credentials.cards.effect_targets import compose_card_effects
from connection_hub.delegated_credentials.cards.model import CardCredentialHandles, ControlCardBinding
from test_card_participant import _Store, _Verifier
from test_card_service import SUBJECT_HASH
from test_card_transaction_store import _setup
from test_w578_account_incarnation import GRANTOR as _UNUSED, _AccountLocks  # noqa: F401
from test_account_store import _MemoryUserConfiguration
from connection_hub.delegated_to_kdcube.models import ConnectedAccount
from connection_hub.delegated_to_kdcube.store import DelegatedToKdcubeStore

PROVIDER, ACCOUNT = "google", "acct-1"


class _Persistence:
    def __init__(self, store, service):
        self.store, self.card_service = store, service

    async def load(self, access_id, *, subject_hash):
        current = await self.store.read_current_authority(subject_hash=subject_hash, access_id=access_id)
        return None if current is None else (current[1], CardCredentialHandles(access_id=access_id))

    async def list_active(self, *, subject_hash, now=None):
        cards = []
        for access_id in await self.store.list_card_ids(subject_hash=subject_hash):
            current = await self.store.read_current_authority(subject_hash=subject_hash, access_id=access_id)
            if current is not None:
                cards.append(current[1])
        return cards


class _Grants:
    async def set_card_credentials_expiry(self, *a, **k):
        return "applied"


async def _world(tmp_path, *, bound=False):
    store, service, card, _ = await _setup(tmp_path)
    grantor = card.grantor_subject
    decisions = _Store()
    tx.bind_transaction_decisions(store, DecisionStorePort(decisions))
    accounts = DelegatedToKdcubeStore(user_id=grantor, backend=_MemoryUserConfiguration(),
                                      account_lock=_AccountLocks())
    compose_card_effects(card_service=service, card_store=store, grant_store=_Grants(), policies=None,
                         accounts_for=lambda subject: accounts if subject == grantor else None)
    intents = LocalCardIntentSource(store)
    hub = HubCardParticipant(service=service, store=store, intents=intents, decisions=decisions)
    host = object.__new__(AutomationAccessService)
    host._persistence = _Persistence(store, service)
    host._caller_writers = None
    host.bind_card_coordinator(Coordinator(decisions, {PARTICIPANT: hub}, _Verifier()),
                               intents=intents, decisions=decisions)
    host.bind_account_stores(lambda subject: accounts if subject == grantor else None)
    binding = replace(card, card_revision=card.card_revision + 1,
                      account_scope={PROVIDER: {ACCOUNT: ("mail.read",)}},
                      control_card=(ControlCardBinding(control_id="c-1", issuer_ref="work:project:one",
                                                       issuer_kind="project", control_revision=1)
                                    if bound else None))
    await service.commit(binding, subject_hash=SUBJECT_HASH, expected_revision=card.card_revision)
    connected = await accounts.upsert_account(ConnectedAccount(account_id=ACCOUNT, provider_id=PROVIDER,
                                                               claims=("mail.read",)))
    return host, store, decisions, accounts, binding, connected, grantor


async def _current(store, card):
    return (await store.read_current_authority(subject_hash=SUBJECT_HASH, access_id=card.access_id))[1]


@pytest.mark.asyncio
async def test_the_disconnect_prunes_every_binding_and_deletes_the_account_under_one_decision(tmp_path):
    host, store, decisions, accounts, card, connected, grantor = await _world(tmp_path)
    result = await host.disconnect_account_in_transaction(grantor_subject=grantor, provider_id=PROVIDER,
                                                          account_id=ACCOUNT)
    assert result["ok"] is True and result["removed"] is True and result["bindings_cleared_grants"] == [card.access_id]
    assert decisions.decisions == ["committed"]
    assert PROVIDER not in (await _current(store, card)).account_scope
    assert await accounts.get_account(ACCOUNT) is None
    # The fence and the incarnation hold are gone once the group finished.
    assert not (fence._dir(store, PROVIDER, ACCOUNT) / "fence.json").exists()
    assert await accounts._prop(accounts.account_delete_hold_key(ACCOUNT)) is None
    reconnected = await accounts.upsert_account(ConnectedAccount(account_id=ACCOUNT, provider_id=PROVIDER))
    assert reconnected.incarnation != connected.incarnation


@pytest.mark.asyncio
async def test_a_control_bound_binding_refuses_the_whole_disconnect_and_changes_nothing(tmp_path):
    host, store, decisions, accounts, card, connected, grantor = await _world(tmp_path, bound=True)
    result = await host.disconnect_account_in_transaction(grantor_subject=grantor, provider_id=PROVIDER,
                                                          account_id=ACCOUNT)
    assert result["error"] == "card_transactions_direct_write_refused" and result["removed"] is False
    assert decisions.decisions == [] and await accounts.get_account(ACCOUNT) is not None
    assert (await _current(store, card)).account_scope == card.account_scope


@pytest.mark.asyncio
async def test_a_binding_added_while_the_fence_is_taken_aborts_retryably(tmp_path, monkeypatch):
    host, store, decisions, accounts, card, connected, grantor = await _world(tmp_path)
    real = host._account_binding_members
    calls = []

    async def members(*args):
        calls.append(1)
        found = await real(*args)
        if len(calls) == 2:  # the re-list after the fence sees a changed set
            return found + found
        return found

    monkeypatch.setattr(host, "_account_binding_members", members)
    result = await host.disconnect_account_in_transaction(grantor_subject=grantor, provider_id=PROVIDER,
                                                          account_id=ACCOUNT)
    assert result == {"ok": False, "error": "account_binding_not_pruned", "reason": "card_account_binding_changed",
                      "removed": False, "retryable": True, "status": 409}
    assert decisions.decisions == ["aborted"] and await accounts.get_account(ACCOUNT) is not None
    assert not (fence._dir(store, PROVIDER, ACCOUNT) / "fence.json").exists()  # the ABORT released it


@pytest.mark.asyncio
async def test_a_reconnect_after_the_plan_moves_the_incarnation_and_the_stage_aborts(tmp_path, monkeypatch):
    host, store, decisions, accounts, card, connected, grantor = await _world(tmp_path)
    real = host._persistence.card_service.reserve_accounts

    async def reserve_then_reconnect(accounts_, *, transaction_id):
        await real(accounts_, transaction_id=transaction_id)
        await accounts.upsert_account(ConnectedAccount(account_id=ACCOUNT, provider_id=PROVIDER))

    monkeypatch.setattr(host._persistence.card_service, "reserve_accounts", reserve_then_reconnect)
    result = await host.disconnect_account_in_transaction(grantor_subject=grantor, provider_id=PROVIDER,
                                                          account_id=ACCOUNT)
    assert result["removed"] is False and result["retryable"] is True and decisions.decisions == ["aborted"]
    assert await accounts.get_account(ACCOUNT) is not None  # the reconnection survives
    assert (await _current(store, card)).account_scope == card.account_scope


@pytest.mark.asyncio
async def test_without_card_transactions_the_caller_keeps_its_ordered_path(tmp_path):
    host, store, decisions, accounts, card, connected, grantor = await _world(tmp_path)
    del host._card_coordinator
    assert await host.disconnect_account_in_transaction(grantor_subject=grantor, provider_id=PROVIDER,
                                                        account_id=ACCOUNT) is None
    assert decisions.decisions == [] and await accounts.get_account(ACCOUNT) is not None


@pytest.mark.asyncio
async def test_no_binding_card_returns_none_for_the_ordered_path(tmp_path):
    host, store, decisions, accounts, card, connected, grantor = await _world(tmp_path)
    other = await accounts.upsert_account(ConnectedAccount(account_id="acct-2", provider_id=PROVIDER))
    assert other.incarnation
    assert await host.disconnect_account_in_transaction(grantor_subject=grantor, provider_id=PROVIDER,
                                                        account_id="acct-2") is None
    assert decisions.decisions == []


def _transaction_id(decisions):
    return next(iter(decisions.rows.values())).transaction_id


@pytest.mark.asyncio
async def test_a_crash_after_commit_before_the_deletion_answers_pending_and_recovery_finishes(tmp_path, monkeypatch):
    host, store, decisions, accounts, card, connected, grantor = await _world(tmp_path)
    real = accounts.disconnect_incarnation
    failures = []

    async def failing_once(*args, **kwargs):
        if not failures:
            failures.append(1)
            raise RuntimeError("account store unreachable")
        return await real(*args, **kwargs)

    monkeypatch.setattr(accounts, "disconnect_incarnation", failing_once)
    result = await host.disconnect_account_in_transaction(grantor_subject=grantor, provider_id=PROVIDER,
                                                          account_id=ACCOUNT)
    assert result == {"ok": False, "error": "card_transaction_pending", "removed": False, "retryable": True,
                      "status": 503}
    assert decisions.decisions == ["committed"] and await accounts.get_account(ACCOUNT) is not None
    # COMMIT is recorded and the deletion is not: the fence still refuses every new binding.
    with pytest.raises(fence.AccountFenceRefused, match="card_account_reserved"):
        await fence.mark_binding(store, [(PROVIDER, ACCOUNT)], mark_id="direct-x", kind="direct",
                                 subject_hash=SUBJECT_HASH, access_id="another-card")
    coordinator = host._card_coordinator[0]
    await coordinator.finish(_transaction_id(decisions))  # recovery
    assert await accounts.get_account(ACCOUNT) is None
    assert not (fence._dir(store, PROVIDER, ACCOUNT) / "fence.json").exists()


@pytest.mark.asyncio
async def test_a_crash_after_the_deletion_before_the_fence_release_finishes_on_replay(tmp_path, monkeypatch):
    host, store, decisions, accounts, card, connected, grantor = await _world(tmp_path)
    service = host._persistence.card_service
    real = service.release_accounts
    failures = []

    async def failing_once(transaction_id):
        if not failures:
            failures.append(1)
            raise RuntimeError("process died before the release")
        return await real(transaction_id)

    monkeypatch.setattr(service, "release_accounts", failing_once)
    result = await host.disconnect_account_in_transaction(grantor_subject=grantor, provider_id=PROVIDER,
                                                          account_id=ACCOUNT)
    assert result["removed"] is False and result["error"] == "card_transaction_pending"
    assert await accounts.get_account(ACCOUNT) is None  # the deletion was applied and pinned
    assert (fence._dir(store, PROVIDER, ACCOUNT) / "fence.json").exists()  # still fenced
    later = await accounts.upsert_account(ConnectedAccount(account_id=ACCOUNT, provider_id=PROVIDER))
    coordinator = host._card_coordinator[0]
    await coordinator.finish(_transaction_id(decisions))  # recovery replays: pinned outcome, then release
    assert not (fence._dir(store, PROVIDER, ACCOUNT) / "fence.json").exists()
    assert (await accounts.get_account(ACCOUNT)).incarnation == later.incarnation  # the reconnection survives

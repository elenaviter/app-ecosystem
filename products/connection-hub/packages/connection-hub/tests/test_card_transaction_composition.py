"""W502: the Hub composition binds Card writes to the ONE protocol only when enabled, and fails closed."""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.automation_access import AutomationAccessService, record_from_card
from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.card_participant import PARTICIPANT
from connection_hub.delegated_credentials.cards.composition import (
    CardTransactionsUnavailable,
    bind_card_transactions,
    card_transactions_enabled,
    decision_namespace,
)
from test_card_coordinated_writes import _Persistence
from test_card_transaction_store import _setup, _visible


@pytest.mark.parametrize(("connections", "enabled"), [
    (None, False), ({}, False), ({"card_transactions": {}}, False),
    ({"card_transactions": {"enabled": "true"}}, False), ({"card_transactions": {"enabled": 1}}, False),
    ({"card_transactions": {"enabled": True}}, True),
])
def test_activation_is_off_unless_explicitly_true(connections, enabled) -> None:
    assert card_transactions_enabled(connections) is enabled


def test_the_namespace_is_per_tenant_and_project() -> None:
    assert decision_namespace(tenant="t", project="p") == f"{PARTICIPANT}:t:p"


@pytest.mark.parametrize("persistence", [None, SimpleNamespace(card_store=None, card_service=object())])
def test_enabled_without_its_parts_refuses_never_writes_directly(persistence) -> None:
    with pytest.raises(CardTransactionsUnavailable, match="card_transactions_unavailable"):
        bind_card_transactions(object(), persistence=persistence, decisions=object(), grant_store=None, policies=None)
    with pytest.raises(CardTransactionsUnavailable, match="card_transactions_unavailable"):
        bind_card_transactions(object(), persistence=SimpleNamespace(card_store=object(), card_service=object()),
                               decisions=None, grant_store=None, policies=None)


@pytest.mark.asyncio
async def test_a_composed_service_commits_an_edit_through_the_real_postgres_decision_store(tmp_path):
    dsn = os.environ.get("CONNECTION_HUB_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("CONNECTION_HUB_TEST_POSTGRES_DSN is not set")
    import asyncpg
    from service_foundation.coordination.durable_decision_log import PostgresDecisionStore

    from connection_hub.delegated_credentials.cards import composition

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    tenant, project = f"t{uuid.uuid4().hex[:8]}", "w502"
    try:
        decisions = await composition.postgres_decision_store(pool, tenant=tenant, project=project)
        assert isinstance(decisions, PostgresDecisionStore)
        store, card_service, before, after = await _setup(tmp_path)
        host = object.__new__(AutomationAccessService)
        host._persistence = _Persistence(store)
        host._caller_writers = None
        persistence = SimpleNamespace(card_store=store, card_service=card_service)
        coordinator = bind_card_transactions(host, persistence=persistence, decisions=decisions,
                                             grant_store=None, policies=None)
        assert set(coordinator.participants) == {PARTICIPANT}
        await host._persist_record(record_from_card(after), expected_revision=before.card_revision)
        assert await _visible(store, before) == after and host._persistence.direct == []
        assert await decisions.list_in_doubt(limit=10) == [] and await tx.list_in_doubt(store) == []
    finally:
        async with pool.acquire() as connection:
            await connection.execute(
                f"DELETE FROM {composition.DECISION_SCHEMA}.service_foundation_decisions WHERE namespace=$1",
                decision_namespace(tenant=tenant, project=project))
        await pool.close()


@pytest.mark.asyncio
async def test_concurrent_first_creation_of_the_decision_store_succeeds(tmp_path):
    # EMain #599 N1: concurrent CREATE ... IF NOT EXISTS can fail; creation is serialized.
    dsn = os.environ.get("CONNECTION_HUB_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("CONNECTION_HUB_TEST_POSTGRES_DSN is not set")
    import asyncio

    import asyncpg

    from connection_hub.delegated_credentials.cards import composition

    pools = [await asyncpg.create_pool(dsn, min_size=1, max_size=2) for _ in range(4)]
    try:
        async with pools[0].acquire() as connection:
            await connection.execute(f"DROP SCHEMA IF EXISTS {composition.DECISION_SCHEMA} CASCADE")
        stores = await asyncio.gather(*(composition.postgres_decision_store(pool, tenant="t", project=f"p{i}")
                                        for i, pool in enumerate(pools)))
        assert len(stores) == 4
    finally:
        for pool in pools:
            await pool.close()


async def _recovery_setup(tmp_path):
    dsn = os.environ.get("CONNECTION_HUB_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("CONNECTION_HUB_TEST_POSTGRES_DSN is not set")
    import asyncpg

    from connection_hub.delegated_credentials.cards import composition

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    tenant, project = f"r{uuid.uuid4().hex[:8]}", "w502"
    decisions = await composition.postgres_decision_store(pool, tenant=tenant, project=project)
    store, card_service, before, after = await _setup(tmp_path)
    persistence = SimpleNamespace(card_store=store, card_service=card_service)

    async def drop():
        async with pool.acquire() as connection:
            await connection.execute(
                f"DELETE FROM {composition.DECISION_SCHEMA}.service_foundation_decisions WHERE namespace=$1",
                decision_namespace(tenant=tenant, project=project))
        await pool.close()

    return composition, decisions, persistence, store, before, after, drop


def _hub_draft(before, after, *, request_id, expires_in):
    import time

    from service_foundation.coordination.durable_decision_log import IntentDraft

    from connection_hub.delegated_credentials.cards.card_participant import hub_participant_input
    from test_card_service import SUBJECT_HASH
    return IntentDraft(replay_scope="recovery-test", request_id=request_id,
                       expires_at=int(time.time()) + expires_in, participants=(PARTICIPANT,),
                       payload={"participant_inputs": {PARTICIPANT: hub_participant_input(
                           original=before, candidate=after, subject_hash=SUBJECT_HASH, action="update",
                           actor_subject="person", actor_kind="caller")}})


async def _staged(composition, decisions, persistence, store, before, after, *, request_id, expires_in=300):
    from connection_hub.delegated_credentials.cards.card_participant import CardIntent
    from test_card_service import SUBJECT_HASH
    coordinator, intents = composition.card_transaction_coordinator(
        persistence=persistence, decisions=decisions, grant_store=None, policies=None)
    row = await decisions.begin(_hub_draft(before, after, request_id=request_id, expires_in=expires_in))
    await intents.record(CardIntent(transaction_id=row.transaction_id, intent_digest=row.intent.digest,
                                    subject_hash=SUBJECT_HASH, original=before, candidate=after,
                                    action="update", actor_subject="person", actor_kind="caller"))
    await coordinator.prepare_existing(row.transaction_id)
    return coordinator, row.transaction_id


@pytest.mark.asyncio
async def test_recovery_finishes_a_commit_a_crash_left_unfinished(tmp_path):
    composition, decisions, persistence, store, before, after, drop = await _recovery_setup(tmp_path)
    try:
        coordinator, txid = await _staged(composition, decisions, persistence, store, before, after,
                                          request_id="crash-after-decide")
        await coordinator.decide(txid, "committed", witness_digest="c" * 64)
        # The crash: a fresh coordinator over the same database and Card store.
        fresh, _ = composition.card_transaction_coordinator(persistence=persistence, decisions=decisions,
                                                            grant_store=None, policies=None)
        report = await composition.recover_card_transactions(fresh)
        assert report == {"ok": True, "finished": 1, "pending": 0, "failed": 0}
        assert await _visible(store, before) == after
        assert await composition.recover_card_transactions(fresh) == {"ok": True, "finished": 0, "pending": 0,
                                                                       "failed": 0}
    finally:
        await drop()


@pytest.mark.asyncio
async def test_recovery_presumes_abort_for_an_expired_undecided_transaction(tmp_path):
    import asyncio

    composition, decisions, persistence, store, before, after, drop = await _recovery_setup(tmp_path)
    try:
        coordinator, txid = await _staged(composition, decisions, persistence, store, before, after,
                                          request_id="crash-after-prepare", expires_in=2)
        assert (await composition.recover_card_transactions(coordinator))["pending"] == 1  # not expired: kept
        await asyncio.sleep(3)
        report = await composition.recover_card_transactions(coordinator)
        assert report == {"ok": True, "finished": 1, "pending": 0, "failed": 0}
        assert (await decisions.read(txid)).state == "aborted"
        assert await _visible(store, before) == before and await tx.list_in_doubt(store) == []
    finally:
        await drop()


@pytest.mark.asyncio
async def test_a_failing_participant_is_reported_and_retried_not_hidden(tmp_path):
    composition, decisions, persistence, store, before, after, drop = await _recovery_setup(tmp_path)
    try:
        coordinator, txid = await _staged(composition, decisions, persistence, store, before, after,
                                          request_id="participant-fails")
        await coordinator.decide(txid, "committed", witness_digest="c" * 64)
        hub = coordinator.participants[PARTICIPANT]
        real_finish = hub.finish

        async def failing(transaction_id, decision):
            raise RuntimeError("participant unavailable")

        hub.finish = failing
        assert await composition.recover_card_transactions(coordinator) == {"ok": False, "finished": 0,
                                                                             "pending": 0, "failed": 1}
        hub.finish = real_finish
        assert (await composition.recover_card_transactions(coordinator))["finished"] == 1
    finally:
        await drop()

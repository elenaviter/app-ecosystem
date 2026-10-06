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

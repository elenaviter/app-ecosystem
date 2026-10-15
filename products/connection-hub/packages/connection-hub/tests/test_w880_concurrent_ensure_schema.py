"""W880: concurrent Connection Hub schema bootstraps never deadlock.

Window 15f, 2026-10-15 22:09:10Z: two Hub ``on_bundle_load`` runs started
together and both ran ``durable_authority.prepare``. Postgres reported a
deadlock inside the OAuth authority ``ensure_schema``: one transaction waited
for ShareLock on ``connection_hub_oauth_profile_devices`` while holding what the
other needed on ``connection_hub_oauth_clients``. KDCube's critical-section
README ("Database Bootstrap Critical Section") prescribes one transaction under
``pg_advisory_xact_lock`` for schema bootstrap; these tests start several
bootstraps together on one disposable PostgreSQL.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from connection_hub.delegated_credentials import admission_replay as admission_module
from connection_hub.delegated_credentials.admission_replay import PostgresAdmissionReplayClaimStore
from connection_hub.delegated_credentials.authority_cutover import PostgresAuthorityCutoverStore
from connection_hub.delegated_credentials.cards.handle_authority import PostgresCardHandleMetadataStore
from connection_hub.delegated_credentials.oauth.authority_store import PostgresOAuthAuthorityStore
from connection_hub.hub.authenticator_store import AuthenticatorStore

LOADERS = 6
ROUNDS = 8
# What one Hub load bootstraps, in order: the stores durable_authority.prepare runs (card handles via their
# metadata store), then the request-authenticator store on_bundle_load runs on every load (review of #828).
PREPARE_ORDER = (
    PostgresOAuthAuthorityStore,
    PostgresCardHandleMetadataStore,
    PostgresAdmissionReplayClaimStore,
    PostgresAuthorityCutoverStore,
    AuthenticatorStore,
)


@pytest.fixture
async def pool():
    dsn = os.environ.get("CONNECTION_HUB_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("CONNECTION_HUB_TEST_POSTGRES_DSN is not set")
    import asyncpg

    created = await asyncpg.create_pool(dsn, min_size=1, max_size=LOADERS * 2)
    try:
        yield created
    finally:
        await created.close()


async def _together(pool, store_type, tenant: str) -> list[BaseException]:
    stores = [store_type(pg_pool=pool, tenant=tenant, project="w880") for _ in range(LOADERS)]
    results = await asyncio.gather(*(store.ensure_schema() for store in stores), return_exceptions=True)
    return [result for result in results if isinstance(result, BaseException)]


@pytest.mark.asyncio
@pytest.mark.parametrize("store_type", PREPARE_ORDER, ids=lambda store_type: store_type.__name__)
async def test_concurrent_bootstraps_of_one_store_never_deadlock(pool, store_type) -> None:
    failures = []
    for _ in range(ROUNDS):
        failures += await _together(pool, store_type, f"w880-{uuid.uuid4().hex[:12]}")
    assert failures == [], [type(failure).__name__ + ": " + str(failure)[:120] for failure in failures]


@pytest.mark.asyncio
@pytest.mark.parametrize("store_type", PREPARE_ORDER, ids=lambda store_type: store_type.__name__)
async def test_concurrent_restarts_on_an_existing_schema_never_deadlock(pool, store_type) -> None:
    """The 15f case: the schema exists (a restart), and every load re-runs the idempotent DDL at once."""

    failures = []
    for _ in range(ROUNDS):
        tenant = f"w880-{uuid.uuid4().hex[:12]}"
        await store_type(pg_pool=pool, tenant=tenant, project="w880").ensure_schema()
        failures += await _together(pool, store_type, tenant)
    assert failures == [], [type(failure).__name__ + ": " + str(failure)[:120] for failure in failures]


@pytest.mark.asyncio
async def test_concurrent_prepare_sequences_never_deadlock(pool) -> None:
    """Several loaders each run the whole prepare order at once, as two Hub loads did on 15f."""

    async def prepare(tenant: str) -> None:
        for store_type in PREPARE_ORDER:
            await store_type(pg_pool=pool, tenant=tenant, project="w880").ensure_schema()

    failures = []
    for _ in range(ROUNDS):
        tenant = f"w880-{uuid.uuid4().hex[:12]}"
        results = await asyncio.gather(*(prepare(tenant) for _ in range(LOADERS)), return_exceptions=True)
        failures += [result for result in results if isinstance(result, BaseException)]
    assert failures == [], [type(failure).__name__ + ": " + str(failure)[:120] for failure in failures]


@pytest.mark.asyncio
async def test_a_failing_bootstrap_still_fails_the_load(pool, monkeypatch) -> None:
    """The lock serializes setup; it never turns a failed DDL into a silent success."""

    monkeypatch.setattr(admission_module, "admission_replay_schema_sql", lambda schema: "SELECT * FROM w880_no_such_table")
    store = PostgresAdmissionReplayClaimStore(pg_pool=pool, tenant=f"w880-{uuid.uuid4().hex[:12]}", project="w880")
    with pytest.raises(Exception, match="w880_no_such_table"):
        await store.ensure_schema()

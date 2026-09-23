from __future__ import annotations

import os
import uuid

import pytest

from connection_hub.delegated_credentials.authority_cutover import (
    FAMILY_OAUTH_ACCESS,
    FAMILY_OAUTH_CLIENTS,
    FAMILY_OAUTH_REFRESH,
    AuthorityCutoverConflict,
    AuthorityCutoverReceipt,
    AuthorityCutoverRequired,
    PostgresAuthorityCutoverStore,
    authority_cutover_schema_sql,
)

_SOURCE = "1" * 64
_TARGET = "2" * 64
_PREVIEW = "3" * 64


def _receipt(*, target_generation: str = _TARGET) -> AuthorityCutoverReceipt:
    counts = {
        FAMILY_OAUTH_CLIENTS: 49,
        FAMILY_OAUTH_REFRESH: 56,
        FAMILY_OAUTH_ACCESS: 1,
    }
    return AuthorityCutoverReceipt(
        migration_id="w253-test-v1",
        source_generation=_SOURCE,
        target_generation=target_generation,
        source_counts=counts,
        target_counts=counts,
        prerequisites={"w251_card_identity": "reviewed-post-state"},
        preview_sha256=_PREVIEW,
    )


def test_cutover_receipt_requires_exact_reconciliation() -> None:
    receipt = _receipt()
    assert receipt.validated().source_counts[FAMILY_OAUTH_REFRESH] == 56

    with pytest.raises(ValueError, match="reconcile exactly"):
        AuthorityCutoverReceipt(
            migration_id=receipt.migration_id,
            source_generation=receipt.source_generation,
            target_generation=receipt.target_generation,
            source_counts={FAMILY_OAUTH_CLIENTS: 1},
            target_counts={FAMILY_OAUTH_CLIENTS: 0},
            preview_sha256=receipt.preview_sha256,
        ).validated()


def test_cutover_schema_has_immutable_activation_identity() -> None:
    sql = authority_cutover_schema_sql("kdcube_demo")

    assert "CREATE SCHEMA IF NOT EXISTS kdcube_demo" in sql
    assert "migration_id                 TEXT PRIMARY KEY" in sql
    assert "activated_revision           BIGSERIAL UNIQUE" in sql
    assert "prerequisites                JSONB NOT NULL" in sql
    assert "preview_sha256               CHAR(64) NOT NULL" in sql


@pytest.mark.asyncio
async def test_cutover_activation_is_idempotent_and_conflict_checked_in_postgres() -> None:
    dsn = os.environ.get("CONNECTION_HUB_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("CONNECTION_HUB_TEST_POSTGRES_DSN is not set")

    import asyncpg

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    store = PostgresAuthorityCutoverStore(
        pg_pool=pool,
        tenant=f"w253-cutover-{uuid.uuid4().hex}",
        project="authority-receipt",
    )
    try:
        await store.ensure_schema()

        first = await store.activate(_receipt())
        repeated = await store.activate(_receipt())

        assert first.activated_revision == repeated.activated_revision == 1
        assert first.prerequisites == {
            "w251_card_identity": "reviewed-post-state"
        }
        required = await store.require_activated(
            "w253-test-v1",
            required_families=(
                FAMILY_OAUTH_CLIENTS,
                FAMILY_OAUTH_REFRESH,
                FAMILY_OAUTH_ACCESS,
            ),
        )
        assert required.target_counts == required.source_counts

        with pytest.raises(AuthorityCutoverRequired, match="families_missing"):
            await store.require_activated(
                "w253-test-v1",
                required_families=("bundle_sessions",),
            )
        with pytest.raises(AuthorityCutoverConflict):
            await store.activate(_receipt(target_generation="4" * 64))
    finally:
        async with pool.acquire() as connection:
            await connection.execute(
                f"DROP SCHEMA IF EXISTS {store.schema} CASCADE"
            )
        await pool.close()

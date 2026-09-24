from __future__ import annotations

import asyncio
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


def _receipt(
    *,
    generation_id: str = "authority-test-v1",
    target_generation: str = _TARGET,
) -> AuthorityCutoverReceipt:
    counts = {
        FAMILY_OAUTH_CLIENTS: 49,
        FAMILY_OAUTH_REFRESH: 56,
        FAMILY_OAUTH_ACCESS: 1,
    }
    return AuthorityCutoverReceipt(
        generation_id=generation_id,
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
            generation_id=receipt.generation_id,
            source_generation=receipt.source_generation,
            target_generation=receipt.target_generation,
            source_counts={FAMILY_OAUTH_CLIENTS: 1},
            target_counts={FAMILY_OAUTH_CLIENTS: 0},
            preview_sha256=receipt.preview_sha256,
        ).validated()


def test_cutover_schema_has_immutable_activation_identity() -> None:
    sql = authority_cutover_schema_sql("kdcube_demo")

    assert "CREATE SCHEMA IF NOT EXISTS kdcube_demo" in sql
    assert "generation_id                 TEXT PRIMARY KEY" in sql
    assert "activated_revision           BIGSERIAL UNIQUE" in sql
    assert "prerequisites                JSONB NOT NULL" in sql
    assert "preview_sha256               CHAR(64) NOT NULL" in sql
    assert "LOCK TABLE kdcube_demo.connection_hub_authority_cutovers" in sql
    assert "RENAME COLUMN migration_id TO generation_id" in sql


@pytest.mark.asyncio
async def test_cutover_activation_is_idempotent_and_conflict_checked_in_postgres() -> None:
    dsn = os.environ.get("CONNECTION_HUB_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("CONNECTION_HUB_TEST_POSTGRES_DSN is not set")

    import asyncpg

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    store = PostgresAuthorityCutoverStore(
        pg_pool=pool,
        tenant=f"authority-cutover-{uuid.uuid4().hex}",
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
            "authority-test-v1",
            required_families=(
                FAMILY_OAUTH_CLIENTS,
                FAMILY_OAUTH_REFRESH,
                FAMILY_OAUTH_ACCESS,
            ),
        )
        assert required.target_counts == required.source_counts

        with pytest.raises(AuthorityCutoverRequired, match="families_missing"):
            await store.require_activated(
                "authority-test-v1",
                required_families=("bundle_sessions",),
            )
        with pytest.raises(AuthorityCutoverConflict):
            await store.activate(_receipt(target_generation="4" * 64))

        second = await store.activate(
            _receipt(generation_id="authority-test-v2")
        )
        assert second.generation_id == "authority-test-v2"
        assert await store.read("authority-test-v1") is None
        assert await store.read("authority-test-v2") == second
    finally:
        async with pool.acquire() as connection:
            await connection.execute(
                f"DROP SCHEMA IF EXISTS {store.schema} CASCADE"
            )
        await pool.close()


@pytest.mark.asyncio
async def test_cutover_schema_renames_legacy_migration_id_in_postgres() -> None:
    dsn = os.environ.get("CONNECTION_HUB_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("CONNECTION_HUB_TEST_POSTGRES_DSN is not set")

    import asyncpg

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    store = PostgresAuthorityCutoverStore(
        pg_pool=pool,
        tenant=f"acl-{uuid.uuid4().hex}",
        project="receipt",
    )
    try:
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(f"CREATE SCHEMA {store.schema}")
            await connection.execute(
                f"""
                CREATE TABLE {store.schema}.connection_hub_authority_cutovers (
                    migration_id TEXT PRIMARY KEY,
                    activated_revision BIGSERIAL UNIQUE,
                    source_generation TEXT NOT NULL,
                    target_generation TEXT NOT NULL,
                    source_counts JSONB NOT NULL,
                    target_counts JSONB NOT NULL,
                    preview_sha256 CHAR(64) NOT NULL,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    prerequisites JSONB NOT NULL DEFAULT '{{}}'::jsonb
                )
                """
            )

        await asyncio.gather(store.ensure_schema(), store.ensure_schema())
        await store.ensure_schema()

        async with pool.acquire() as connection:
            columns = await connection.fetch(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = $1
                  AND table_name = 'connection_hub_authority_cutovers'
                ORDER BY ordinal_position
                """,
                store.schema,
            )
        column_names = [str(row["column_name"]) for row in columns]
        assert "generation_id" in column_names
        assert "migration_id" not in column_names

        activated = await store.activate(_receipt())
        assert activated.generation_id == "authority-test-v1"
        assert (await store.read("authority-test-v1")) == activated
    finally:
        async with pool.acquire() as connection:
            await connection.execute(
                f"DROP SCHEMA IF EXISTS {store.schema} CASCADE"
            )
        await pool.close()

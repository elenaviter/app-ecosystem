from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

import pytest

from connection_hub.delegated_credentials.admission_replay import (
    MIGRATED_DIGEST_ONLY_SERVICE_ID,
    TABLE_ADMISSION_REPLAY_CLAIMS,
    PostgresAdmissionReplayClaimStore,
    admission_nonce_digest,
    admission_replay_schema_sql,
)


class _Connection:
    def __init__(
        self,
        values: list[Any] | None = None,
        rows: list[Any] | None = None,
    ) -> None:
        self.values = list(values or [])
        self.rows = list(rows or [])
        self.calls: list[tuple[str, str, tuple[Any, ...]]] = []
        self.transaction_depth = 0

    @asynccontextmanager
    async def transaction(self):
        self.transaction_depth += 1
        try:
            yield
        finally:
            self.transaction_depth -= 1

    async def execute(self, sql: str, *args: Any) -> str:
        assert self.transaction_depth == 1
        self.calls.append(("execute", sql, args))
        return "CREATE TABLE"

    async def fetchval(self, sql: str, *args: Any) -> Any:
        assert self.transaction_depth == 1
        self.calls.append(("fetchval", sql, args))
        return self.values.pop(0) if self.values else None

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        assert self.transaction_depth == 1
        self.calls.append(("fetchrow", sql, args))
        return self.rows.pop(0) if self.rows else None


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


def _store(connection: _Connection) -> PostgresAdmissionReplayClaimStore:
    return PostgresAdmissionReplayClaimStore(
        pg_pool=_Pool(connection),
        tenant="tenant-a",
        project="project-a",
    )


def test_admission_replay_schema_contains_only_digest_identity() -> None:
    sql = admission_replay_schema_sql("kdcube_tenant_a_project_a")

    assert "nonce_sha256" in sql
    assert "PRIMARY KEY (service_id, nonce_sha256)" in sql
    assert "UNIQUE INDEX" in sql
    assert "nonce TEXT" not in sql
    assert "token" not in sql.lower()


def test_admission_nonce_digest_preserves_the_exact_signed_nonce() -> None:
    assert admission_nonce_digest(
        service_id="crm-api", nonce="nonce-value"
    ) != admission_nonce_digest(service_id="crm-api", nonce="nonce-value ")


@pytest.mark.asyncio
async def test_claim_hashes_nonce_before_sql_and_reports_conflict() -> None:
    nonce = "nonce-value-that-must-not-enter-sql"
    digest = admission_nonce_digest(service_id="crm-api", nonce=nonce)
    connection = _Connection(values=[digest, None])
    store = _store(connection)

    assert await store.claim(
        service_id="crm-api",
        nonce=nonce,
        ttl_seconds=600,
        now=100,
    )
    assert not await store.claim(
        service_id="crm-api",
        nonce=nonce,
        ttl_seconds=600,
        now=101,
    )

    for _kind, sql, args in connection.calls:
        assert nonce not in sql
        assert nonce not in args
    assert connection.calls[0][2] == (
        "crm-api",
        digest,
        "tenant-a",
        "project-a",
        100,
        600,
    )
    assert "ON CONFLICT (nonce_sha256)" in connection.calls[0][1]


@pytest.mark.asyncio
async def test_digest_only_import_is_insert_only_and_preserves_absolute_expiry() -> None:
    digest = "a" * 64
    expires_at_ms = 2_000_000_000_000
    connection = _Connection(
        rows=[
            {
                "service_id": MIGRATED_DIGEST_ONLY_SERVICE_ID,
                "tenant": "tenant-a",
                "project": "project-a",
                "expires_at_ms": expires_at_ms,
            }
        ]
    )

    await _store(connection).import_digest(
        nonce_sha256=digest,
        expires_at_ms=expires_at_ms,
    )

    assert [kind for kind, _sql, _args in connection.calls] == [
        "execute",
        "fetchrow",
    ]
    assert "ON CONFLICT (nonce_sha256) DO NOTHING" in connection.calls[0][1]
    assert "UPDATE" not in connection.calls[0][1]
    assert connection.calls[0][2] == (
        MIGRATED_DIGEST_ONLY_SERVICE_ID,
        digest,
        "tenant-a",
        "project-a",
        expires_at_ms,
    )


@pytest.mark.asyncio
async def test_purge_expired_is_bounded() -> None:
    connection = _Connection(values=[7])

    assert await _store(connection).purge_expired(now=1_000, limit=25) == 7
    _kind, sql, args = connection.calls[0]
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "LIMIT $2" in sql
    assert args == (1_000, 25)


@pytest.mark.asyncio
async def test_admission_replay_contract_against_real_postgres() -> None:
    dsn = os.environ.get("CONNECTION_HUB_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("CONNECTION_HUB_TEST_POSTGRES_DSN is not set")

    import asyncpg

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    store = PostgresAdmissionReplayClaimStore(
        pg_pool=pool,
        tenant=f"authority-test-{uuid.uuid4().hex}",
        project="admission-replay",
    )
    try:
        await store.ensure_schema()
        assert await store.claim(
            service_id="crm-api",
            nonce="nonce-1234567890abcd",
            ttl_seconds=600,
            now=1_000,
        )
        assert not await store.claim(
            service_id="crm-api",
            nonce="nonce-1234567890abcd",
            ttl_seconds=600,
            now=1_001,
        )
        assert await store.claim(
            service_id="billing-api",
            nonce="nonce-1234567890abcd",
            ttl_seconds=600,
            now=1_001,
        )
        assert await store.purge_expired(now=1_601, limit=1) == 1

        async with pool.acquire() as connection:
            remaining = await connection.fetchval(
                f"SELECT count(*) FROM {store.schema}.{TABLE_ADMISSION_REPLAY_CLAIMS}"
            )
        assert remaining == 1

        concurrent = await asyncio.gather(
            store.claim(
                service_id="crm-api",
                nonce="nonce-concurrent-1234",
                ttl_seconds=600,
                now=2_000,
            ),
            store.claim(
                service_id="crm-api",
                nonce="nonce-concurrent-1234",
                ttl_seconds=600,
                now=2_000,
            ),
        )
        assert sorted(concurrent) == [False, True]
    finally:
        async with pool.acquire() as connection:
            await connection.execute(f"DROP SCHEMA IF EXISTS {store.schema} CASCADE")
        await pool.close()

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest

from connection_hub.delegated_credentials.authority_config import (
    CONNECTION_HUB_AUTHORITY_FAMILIES,
    DurableAuthorityConfig,
)
from connection_hub.delegated_credentials.authority_cutover import (
    AuthorityCutoverReceipt,
    AuthorityCutoverRequired,
)
from connection_hub.delegated_credentials.oauth.runtime_store import (
    OAuthGrantStoreProvider,
)
from connection_hub.delegated_credentials.oauth.store import (
    GrantStore,
    GrantStoreUnavailable,
)


class _Redis:
    def __init__(self) -> None:
        self.get_calls = 0

    async def get(self, _key: str) -> None:
        self.get_calls += 1
        return None


class _Authority:
    def __init__(self, record: dict[str, Any]) -> None:
        self.record = record
        self.tokens: list[str] = []

    async def get_access_grant_record(self, token: str) -> dict[str, Any]:
        self.tokens.append(token)
        return dict(self.record)


class _Cutovers:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    async def require_activated(
        self,
        generation_id: str,
        *,
        required_families: tuple[str, ...],
    ) -> object:
        self.calls.append((generation_id, required_families))
        if self.failure is not None:
            raise self.failure
        return object()


def _config(backend: str, generation_id: str = "") -> DurableAuthorityConfig:
    return DurableAuthorityConfig.from_mapping(
        {"backend": backend, "generation_id": generation_id}
    )


@pytest.mark.asyncio
async def test_redis_migration_source_is_an_explicit_selected_store() -> None:
    redis = _Redis()
    provider = OAuthGrantStoreProvider.from_config(
        config=_config("redis-migration-source"),
        redis=redis,
        pg_pool=None,
        tenant="demo-tenant",
        project="demo-project",
    )

    store = await provider.resolve()

    assert store.redis is redis
    assert provider.authority_store is None
    assert provider.cutover_store is None


def test_postgresql_selection_requires_a_pool_without_redis_fallback() -> None:
    with pytest.raises(
        GrantStoreUnavailable,
        match="selected_authority.postgresql_pool_unavailable",
    ):
        OAuthGrantStoreProvider.from_config(
            config=_config("postgresql", "authority-v1"),
            redis=_Redis(),
            pg_pool=None,
            tenant="demo-tenant",
            project="demo-project",
        )


@pytest.mark.asyncio
async def test_selected_postgresql_store_checks_receipt_and_reads_authority() -> None:
    redis = _Redis()
    authority = _Authority({"credential": {"issuer_authority_id": "delegated_client"}})
    cutovers = _Cutovers()
    provider = OAuthGrantStoreProvider(
        config=_config("postgresql", "authority-v9"),
        grant_store=GrantStore(
            redis,
            "demo-tenant",
            "demo-project",
            authority_store=authority,
        ),
        authority_store=authority,  # type: ignore[arg-type]
        cutover_store=cutovers,  # type: ignore[arg-type]
    )

    store = await provider.resolve()
    record = await store.get_access_grant_record("postgres-issued-bearer")

    assert record == {
        "credential": {"issuer_authority_id": "delegated_client"}
    }
    assert cutovers.calls == [
        ("authority-v9", CONNECTION_HUB_AUTHORITY_FAMILIES)
    ]
    assert authority.tokens == ["postgres-issued-bearer"]
    assert redis.get_calls == 0


@pytest.mark.asyncio
async def test_unactivated_postgresql_generation_has_a_named_failure() -> None:
    config = _config("postgresql", "authority-v9")
    provider = OAuthGrantStoreProvider(
        config=config,
        grant_store=GrantStore(
            _Redis(),
            "demo-tenant",
            "demo-project",
            authority_store=_Authority({}),
        ),
        authority_store=_Authority({}),  # type: ignore[arg-type]
        cutover_store=_Cutovers(
            AuthorityCutoverRequired(
                "authority_cutover_receipt_missing",
                generation_id=config.generation_id,
            )
        ),  # type: ignore[arg-type]
    )

    with pytest.raises(GrantStoreUnavailable) as raised:
        await provider.resolve()

    assert (
        raised.value.operation
        == "selected_authority.authority_cutover_receipt_missing"
    )


@pytest.mark.asyncio
async def test_selected_store_reads_postgresql_issued_bearer_against_postgres() -> None:
    dsn = os.environ.get("CONNECTION_HUB_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("CONNECTION_HUB_TEST_POSTGRES_DSN is not set")

    import asyncpg

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    generation_id = "authority-runtime-store-v1"
    provider = OAuthGrantStoreProvider.from_config(
        config=_config("postgresql", generation_id),
        redis=_Redis(),
        pg_pool=pool,
        tenant=f"runtime-store-{uuid.uuid4().hex}",
        project="selected-authority",
    )
    assert provider.cutover_store is not None
    try:
        await provider.ensure_schema()
        counts = {
            family: 0 for family in CONNECTION_HUB_AUTHORITY_FAMILIES
        }
        await provider.cutover_store.activate(
            AuthorityCutoverReceipt(
                generation_id=generation_id,
                source_generation="1" * 64,
                target_generation="2" * 64,
                source_counts=counts,
                target_counts=counts,
                preview_sha256="3" * 64,
            )
        )

        store = await provider.resolve()
        await store.bind_access_grant(
            "postgres-issued-bearer",
            ["worker.heartbeat"],
            600,
            credential={
                "schema": "kdcube.credential.v1",
                "issuer_authority_id": "delegated_client",
            },
        )

        record = await store.get_access_grant_record(
            "postgres-issued-bearer"
        )
        assert record is not None
        assert record["credential"]["issuer_authority_id"] == (
            "delegated_client"
        )
    finally:
        async with pool.acquire() as connection:
            await connection.execute(
                f"DROP SCHEMA IF EXISTS {provider.cutover_store.schema} CASCADE"
            )
        await pool.close()

from __future__ import annotations

import hashlib
import os
import uuid
from collections import deque
from typing import Any

import pytest

from connection_hub.delegated_credentials.oauth.authority_schema import (
    TABLE_ACCESS_BINDINGS,
    TABLE_CLIENTS,
    TABLE_FAMILIES,
    TABLE_REFRESH_GENERATIONS,
    oauth_authority_schema_sql,
)
from connection_hub.delegated_credentials.devices.authority_schema import (
    TABLE_PROFILE_DEVICES,
)
from connection_hub.delegated_credentials.oauth.authority_store import (
    PostgresOAuthAuthorityStore,
    RefreshTokenReuseDetected,
)
from connection_hub.delegated_credentials.oauth.store import GrantStore


class _Transaction:
    def __init__(self, connection: "_Connection") -> None:
        self.connection = connection

    async def __aenter__(self) -> None:
        self.connection.transaction_depth += 1
        self.connection.transaction_enters += 1

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        self.connection.transaction_depth -= 1
        self.connection.transaction_exits += 1


class _Connection:
    def __init__(
        self,
        *,
        rows: list[dict[str, Any] | None] | None = None,
        row_sets: list[list[dict[str, Any]]] | None = None,
    ) -> None:
        self.rows = deque(rows or [])
        self.row_sets = deque(row_sets or [])
        self.calls: list[tuple[str, str, tuple[Any, ...], int]] = []
        self.transaction_depth = 0
        self.transaction_enters = 0
        self.transaction_exits = 0

    def transaction(self) -> _Transaction:
        return _Transaction(self)

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append(("execute", sql, args, self.transaction_depth))
        return "UPDATE 1" if sql.lstrip().startswith("UPDATE") else "INSERT 0 1"

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.calls.append(("fetchrow", sql, args, self.transaction_depth))
        return self.rows.popleft() if self.rows else None

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append(("fetch", sql, args, self.transaction_depth))
        return self.row_sets.popleft() if self.row_sets else []


class _Acquire:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _Connection:
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def acquire(self) -> _Acquire:
        return _Acquire(self.connection)


def _store(connection: _Connection) -> PostgresOAuthAuthorityStore:
    return PostgresOAuthAuthorityStore(
        pg_pool=_Pool(connection),
        tenant="demo-tenant",
        project="demo-project",
    )


def _all_arguments(connection: _Connection) -> list[Any]:
    return [argument for _kind, _sql, args, _depth in connection.calls for argument in args]


def test_schema_has_hash_columns_and_no_raw_bearer_columns() -> None:
    sql = oauth_authority_schema_sql("kdcube_demo_tenant_demo_project")

    assert "token_sha256" in sql
    assert "raw_token" not in sql
    assert "refresh_token " not in sql
    assert "access_token " not in sql
    assert "connection_hub_oauth_refresh_generations" in sql
    assert "connection_hub_oauth_access_bindings" in sql
    assert "revision" in sql
    assert "activated_revision" in sql


@pytest.mark.asyncio
async def test_ensure_schema_installs_device_authority_in_one_transaction() -> None:
    connection = _Connection()
    store = _store(connection)

    await store.ensure_schema()

    assert connection.transaction_enters == 1
    assert connection.transaction_exits == 1
    assert len(connection.calls) == 2
    assert all(depth == 1 for _kind, _sql, _args, depth in connection.calls)
    assert "connection_hub_oauth_credential_families" in connection.calls[0][1]
    assert TABLE_PROFILE_DEVICES in connection.calls[1][1]


def test_grant_store_exposes_the_configured_device_authority() -> None:
    class Authority:
        profile_devices = object()

    authority = Authority()
    store = GrantStore(
        object(),
        tenant="demo-tenant",
        project="demo-project",
        authority_store=authority,
    )

    assert store.profile_devices is authority.profile_devices


@pytest.mark.asyncio
async def test_create_refresh_hashes_the_bearer_and_uses_one_transaction(
    monkeypatch,
) -> None:
    raw_token = "refresh-secret-that-must-not-reach-sql"
    connection = _Connection()
    store = _store(connection)
    monkeypatch.setattr(
        "connection_hub.delegated_credentials.oauth.authority_store.secrets.token_urlsafe",
        lambda _size: raw_token,
    )

    issued = await store.create_refresh_token(
        {
            "registry_access_id": "aut_card",
            "card_kind": "automation",
            "client_id": "dcr-client",
            "sub": "user-1",
            "identity_scope": "grantor",
        },
        ttl_seconds=600,
    )

    assert issued == raw_token
    assert connection.transaction_enters == 1
    assert connection.transaction_exits == 1
    assert all(depth == 1 for _kind, _sql, _args, depth in connection.calls)
    arguments = _all_arguments(connection)
    assert raw_token not in arguments
    assert hashlib.sha256(raw_token.encode()).hexdigest() in arguments


@pytest.mark.asyncio
async def test_rotate_locks_and_advances_one_generation_without_sql_bearers(
    monkeypatch,
) -> None:
    old_token = "old-refresh-bearer"
    new_token = "new-refresh-bearer"
    connection = _Connection(
        rows=[
            {
                "generation_id": "ogen_old",
                "family_id": "ofam_1",
                "generation_state": "active",
                "generation_live": True,
                "family_state": "active",
                "family_live": True,
            }
        ]
    )
    store = _store(connection)
    monkeypatch.setattr(
        "connection_hub.delegated_credentials.oauth.authority_store.secrets.token_urlsafe",
        lambda _size: new_token,
    )

    rotated = await store.rotate_refresh_token(
        old_token,
        {"client_id": "dcr-client", "sub": "user-1"},
        ttl_seconds=600,
        expected_generation="ogen_old",
    )

    assert rotated == new_token
    assert connection.transaction_enters == 1
    assert connection.transaction_exits == 1
    assert "FOR UPDATE OF generation, family" in connection.calls[0][1]
    assert all(depth == 1 for _kind, _sql, _args, depth in connection.calls)
    arguments = _all_arguments(connection)
    assert old_token not in arguments
    assert new_token not in arguments
    assert hashlib.sha256(old_token.encode()).hexdigest() in arguments
    assert hashlib.sha256(new_token.encode()).hexdigest() in arguments


@pytest.mark.asyncio
async def test_rotate_refuses_a_stale_generation_without_mutation() -> None:
    connection = _Connection(
        rows=[
            {
                "generation_id": "ogen_current",
                "family_id": "ofam_1",
                "generation_state": "active",
                "generation_live": True,
                "family_state": "active",
                "family_live": True,
            }
        ]
    )
    store = _store(connection)

    rotated = await store.rotate_refresh_token(
        "refresh-bearer",
        {"client_id": "dcr-client", "sub": "user-1"},
        ttl_seconds=600,
        expected_generation="ogen_stale",
    )

    assert rotated is None
    assert [kind for kind, _sql, _args, _depth in connection.calls] == ["fetchrow"]
    assert connection.transaction_enters == 1
    assert connection.transaction_exits == 1


@pytest.mark.asyncio
async def test_rotation_race_revokes_the_reused_refresh_family() -> None:
    connection = _Connection(
        rows=[
            {
                "generation_id": "ogen_consumed",
                "family_id": "ofam_1",
                "generation_state": "consumed",
                "generation_live": True,
                "family_state": "active",
                "family_live": True,
            }
        ]
    )

    with pytest.raises(RefreshTokenReuseDetected):
        await _store(connection).rotate_refresh_token(
            "refresh-bearer",
            {"client_id": "dcr-client", "sub": "user-1"},
            ttl_seconds=600,
            expected_generation="ogen_consumed",
        )

    assert connection.transaction_enters == 1
    assert connection.transaction_exits == 1
    assert len(connection.calls) == 3
    assert "SET state = 'revoked'" in connection.calls[1][1]
    assert "WHERE family_id = $1 AND state = 'active'" in connection.calls[2][1]


@pytest.mark.asyncio
async def test_consumed_refresh_reuse_revokes_the_family_without_raw_bearer() -> None:
    raw_token = "consumed-refresh-bearer"
    connection = _Connection(
        rows=[
            {
                "generation_id": "ogen_consumed",
                "family_id": "ofam_1",
                "record": {"sub": "user-1"},
                "generation_state": "consumed",
                "generation_live": True,
                "family_state": "active",
                "family_live": True,
            }
        ]
    )

    with pytest.raises(RefreshTokenReuseDetected):
        await _store(connection).get_refresh_token_state(raw_token)

    assert connection.transaction_enters == 1
    assert connection.transaction_exits == 1
    assert len(connection.calls) == 3
    assert "FOR UPDATE OF generation, family" in connection.calls[0][1]
    assert "SET state = 'revoked'" in connection.calls[1][1]
    assert "WHERE family_id = $1 AND state = 'active'" in connection.calls[2][1]
    assert raw_token not in _all_arguments(connection)
    assert hashlib.sha256(raw_token.encode()).hexdigest() in _all_arguments(
        connection
    )


@pytest.mark.asyncio
async def test_access_binding_uses_only_the_bearer_hash() -> None:
    raw_token = "access-secret-that-must-not-reach-sql"
    connection = _Connection()
    store = _store(connection)

    await store.bind_access_grant(
        raw_token,
        {"registry_access_id": "aut_card", "operations": ["search"]},
        ttl_seconds=300,
    )

    arguments = _all_arguments(connection)
    assert raw_token not in arguments
    assert hashlib.sha256(raw_token.encode()).hexdigest() in arguments
    assert f"WHERE connection_hub_oauth_access_bindings.state <> 'revoked'" in (
        connection.calls[0][1]
    )


@pytest.mark.asyncio
async def test_client_lookup_extends_only_in_second_half_of_ttl() -> None:
    connection = _Connection(
        rows=[
            {
                "client_id": "client-1",
                "redirect_uris": ["http://127.0.0.1/callback"],
                "grant_types": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_method": "none",
                "application_type": "native",
                "metadata": {},
            }
        ]
    )

    record = await _store(connection).get_client_record(
        "client-1",
        ttl_seconds=600,
    )

    assert record is not None
    assert record["client_id"] == "client-1"
    assert record["grant_types"] == ["authorization_code", "refresh_token"]
    assert len(connection.calls) == 1
    kind, sql, arguments, depth = connection.calls[0]
    assert kind == "fetchrow"
    assert arguments == ("client-1", 600)
    assert depth == 0
    assert "candidate.expires_at < now()" in sql
    assert "FROM candidate" in sql
    assert "last_used_at" not in sql
    assert "updated_at" not in sql


@pytest.mark.asyncio
async def test_card_lifecycle_uses_stable_id_in_one_transaction() -> None:
    connection = _Connection(row_sets=[[{"family_id": "ofam_1"}]])
    store = _store(connection)

    extended = await store.extend_card_credentials("aut_card", 900)

    assert extended is True
    assert connection.transaction_enters == 1
    assert connection.transaction_exits == 1
    assert [kind for kind, _sql, _args, _depth in connection.calls] == [
        "fetch",
        "execute",
        "execute",
        "execute",
    ]
    assert all(depth == 1 for _kind, _sql, _args, depth in connection.calls)
    assert all("refresh-bearer" not in args for _kind, _sql, args, _depth in connection.calls)
    assert connection.calls[0][2] == ("aut_card",)


@pytest.mark.asyncio
async def test_card_revocation_reaches_refresh_and_access_rows_transactionally() -> None:
    connection = _Connection(row_sets=[[{"family_id": "ofam_1"}]])
    store = _store(connection)

    revoked = await store.revoke_card_credentials("aut_card")

    assert revoked is True
    assert connection.transaction_enters == 1
    assert connection.transaction_exits == 1
    sql = "\n".join(call[1] for call in connection.calls)
    assert "connection_hub_oauth_refresh_generations" in sql
    assert "connection_hub_oauth_credential_families" in sql
    assert "connection_hub_oauth_access_bindings" in sql
    assert all(depth == 1 for _kind, _sql, _args, depth in connection.calls)


@pytest.mark.asyncio
async def test_grant_store_routes_durable_authority_away_from_redis() -> None:
    class NoDurableRedis:
        def __getattr__(self, name: str):
            raise AssertionError(f"durable OAuth operation reached Redis: {name}")

    class Authority:
        def __init__(self) -> None:
            self.calls: list[tuple[str, Any]] = []

        async def create_refresh_token(self, record, *, ttl_seconds):
            self.calls.append(("refresh", (dict(record), ttl_seconds)))
            return "refresh-token"

        async def register_client(self, record, *, ttl_seconds):
            self.calls.append(("client", (dict(record), ttl_seconds)))
            return dict(record)

        async def bind_access_grant(
            self,
            access_token,
            record,
            *,
            ttl_seconds,
        ):
            self.calls.append(
                (
                    "access",
                    (access_token, dict(record), ttl_seconds),
                )
            )

    authority = Authority()
    store = GrantStore(
        NoDurableRedis(),
        tenant="demo-tenant",
        project="demo-project",
        authority_store=authority,
    )

    assert await store.create_refresh_token(
        client_id="client-1",
        sub="user-1",
        scopes=["scope:read"],
    ) == "refresh-token"
    client = await store.register_client(redirect_uris=["http://127.0.0.1/callback"])
    await store.bind_access_grant("access-token", ["search"], 300)

    assert client["client_id"].startswith("dcr-")
    assert [name for name, _payload in authority.calls] == [
        "refresh",
        "client",
        "access",
    ]


@pytest.mark.asyncio
async def test_client_extension_executes_against_real_postgres() -> None:
    dsn = os.environ.get("CONNECTION_HUB_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("CONNECTION_HUB_TEST_POSTGRES_DSN is not set")

    import asyncpg

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    store = PostgresOAuthAuthorityStore(
        pg_pool=pool,
        tenant=f"authority-test-{uuid.uuid4().hex}",
        project="oauth-authority",
    )
    try:
        await store.ensure_schema()
        record = {
            "client_id": "client-1",
            "redirect_uris": ["http://127.0.0.1/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_method": "none",
            "application_type": "native",
            "metadata": {},
        }
        await store.register_client(record, ttl_seconds=600)
        async with pool.acquire() as connection:
            await connection.execute(
                f"""
                UPDATE {store.schema}.{TABLE_CLIENTS}
                SET expires_at = now() + interval '1 second'
                WHERE client_id = $1
                """,
                "client-1",
            )

        found = await store.get_client_record("client-1", ttl_seconds=600)

        assert found == record
        async with pool.acquire() as connection:
            revision = await connection.fetchval(
                f"""
                SELECT revision
                FROM {store.schema}.{TABLE_CLIENTS}
                WHERE client_id = $1
                """,
                "client-1",
            )
        assert revision == 2
    finally:
        async with pool.acquire() as connection:
            await connection.execute(f"DROP SCHEMA IF EXISTS {store.schema} CASCADE")
        await pool.close()


@pytest.mark.asyncio
async def test_card_credential_lifecycle_executes_against_real_postgres() -> None:
    dsn = os.environ.get("CONNECTION_HUB_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("CONNECTION_HUB_TEST_POSTGRES_DSN is not set")

    import asyncpg

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    store = PostgresOAuthAuthorityStore(
        pg_pool=pool,
        tenant=f"authority-test-{uuid.uuid4().hex}",
        project="card-credential-lifecycle",
    )
    try:
        await store.ensure_schema()
        await store.create_refresh_token(
            {
                "registry_access_id": "aut_card",
                "card_kind": "automation",
                "client_id": "client-1",
                "sub": "user-1",
            },
            ttl_seconds=600,
        )
        await store.bind_access_grant(
            "access-bearer",
            {"registry_access_id": "aut_card", "operations": ["search"]},
            ttl_seconds=600,
        )

        assert await store.extend_card_credentials("aut_card", 900) is True
        assert await store.revoke_card_credentials("aut_card") is True

        async with pool.acquire() as connection:
            family_state = await connection.fetchval(
                f"""
                SELECT state
                FROM {store.schema}.{TABLE_FAMILIES}
                WHERE registry_access_id = $1
                """,
                "aut_card",
            )
            refresh_state = await connection.fetchval(
                f"""
                SELECT generation.state
                FROM {store.schema}.{TABLE_REFRESH_GENERATIONS} AS generation
                JOIN {store.schema}.{TABLE_FAMILIES} AS family
                  ON family.family_id = generation.family_id
                WHERE family.registry_access_id = $1
                """,
                "aut_card",
            )
            access_state = await connection.fetchval(
                f"""
                SELECT state
                FROM {store.schema}.{TABLE_ACCESS_BINDINGS}
                WHERE registry_access_id = $1
                """,
                "aut_card",
            )
        assert (family_state, refresh_state, access_state) == (
            "revoked",
            "revoked",
            "revoked",
        )
    finally:
        async with pool.acquire() as connection:
            await connection.execute(f"DROP SCHEMA IF EXISTS {store.schema} CASCADE")
        await pool.close()

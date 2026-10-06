from __future__ import annotations

import hashlib
import json
import os
import time
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
from connection_hub.delegated_credentials.migration.model import (
    AuthorityMigrationRecord,
)
from connection_hub.delegated_credentials.oauth.migration import (
    PostgresOAuthMigrationTarget,
)


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


def _assert_family_locked_first(connection: _Connection) -> None:
    """W408 review: the family row is locked before any generation row."""

    kind, sql, _args, _depth = connection.calls[0]
    assert kind == "execute"
    assert "connection_hub_oauth_credential_families" in sql
    assert "FOR UPDATE" in sql and "FOR UPDATE OF" not in sql
    assert "WHERE token_sha256 = $1" in sql


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
    # W414: the device grant migration is never part of schema setup; the
    # release runs it only after every process checks Card continuity.
    assert not any("UPDATE" in sql for _kind, sql, _args, _depth in connection.calls)


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
async def test_refresh_migration_is_insert_only_and_never_sends_bearer_to_sql() -> None:
    raw_token = "existing-refresh-bearer"
    digest = hashlib.sha256(raw_token.encode()).hexdigest()
    expires_at_ms = 2_000_000_000_000
    record = {
        "registry_access_id": "aut_card",
        "card_kind": "automation",
        "client_id": "dcr-client",
        "sub": "user-1",
        "identity_scope": "grantor",
    }
    connection = _Connection(
        rows=[
            {
                "generation_id": f"ogen_migration_{digest}",
                "family_id": f"ofam_migration_{digest}",
                "record": record,
                "generation_state": "active",
                "generation_expires_at_ms": expires_at_ms,
                "tenant": "demo-tenant",
                "project": "demo-project",
                "registry_access_id": "aut_card",
                "card_kind": "automation",
                "client_id": "dcr-client",
                "subject": "user-1",
                "identity_scope": "grantor",
                "current_generation_id": f"ogen_migration_{digest}",
                "family_state": "active",
                "family_expires_at_ms": expires_at_ms,
            }
        ]
    )
    target = PostgresOAuthMigrationTarget(_store(connection))

    await target.import_record(
        AuthorityMigrationRecord(
            record_type="oauth_refresh",
            identity=digest,
            families=("oauth_refresh_families",),
            payload={
                "bearer_sha256": digest,
                "migration_state": "active",
                "record": record,
            },
            secrets={"bearer": raw_token},
            expires_at_ms=expires_at_ms,
        )
    )

    arguments = _all_arguments(connection)
    assert raw_token not in arguments
    assert digest in arguments
    assert all(
        not sql.lstrip().startswith("UPDATE")
        and "DO UPDATE" not in sql
        for _kind, sql, _args, _depth in connection.calls
    )
    assert sum("ON CONFLICT" in sql for _kind, sql, _args, _depth in connection.calls) == 2


@pytest.mark.asyncio
async def test_client_migration_normalizes_legacy_missing_grant_types() -> None:
    redirect_uris = ["http://127.0.0.1/callback"]
    default_grant_types = ["authorization_code", "refresh_token"]
    connection = _Connection(
        rows=[
            {
                "tenant": "demo-tenant",
                "project": "demo-project",
                "redirect_uris": redirect_uris,
                "grant_types": default_grant_types,
                "token_endpoint_auth_method": "none",
                "application_type": "native",
                "metadata": {"kind": "legacy"},
                "retired_at": None,
                "expires_at_ms": None,
            }
        ]
    )
    target = PostgresOAuthMigrationTarget(_store(connection))

    created = await target.import_record(
        AuthorityMigrationRecord(
            record_type="oauth_client",
            identity="legacy-client",
            families=("oauth_clients",),
            payload={
                "migration_state": "active",
                "record": {
                    "client_id": "legacy-client",
                    "redirect_uris": redirect_uris,
                    "token_endpoint_auth_method": "none",
                    "application_type": "native",
                    "metadata": {"kind": "legacy"},
                },
            },
        )
    )

    assert created is True
    assert default_grant_types in [
        json.loads(argument)
        for argument in _all_arguments(connection)
        if isinstance(argument, str) and argument.startswith("[")
    ]


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
    _assert_family_locked_first(connection)
    assert "FOR UPDATE OF generation, family" in connection.calls[1][1]
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
    assert [kind for kind, _sql, _args, _depth in connection.calls] == ["execute", "fetchrow"]
    _assert_family_locked_first(connection)
    assert connection.transaction_enters == 1
    assert connection.transaction_exits == 1


@pytest.mark.asyncio
async def test_rotation_rollback_restores_only_the_current_replacement() -> None:
    replacement_expires_at = object()
    connection = _Connection(
        rows=[
            {
                "prior_generation_id": "ogen_old",
                "prior_state": "consumed",
                "replacement_generation_id": "ogen_new",
                "replacement_state": "active",
                "replacement_expires_at": replacement_expires_at,
                "replacement_live": True,
                "family_id": "ofam_1",
                "current_generation_id": "ogen_new",
                "family_state": "active",
                "family_live": True,
            }
        ]
    )
    store = _store(connection)

    restored = await store.rollback_refresh_token_rotation(
        "old-refresh-bearer",
        "new-refresh-bearer",
        expected_generation="ogen_old",
        expected_replacement_generation="ogen_new",
    )

    assert restored is True
    assert connection.transaction_enters == 1
    assert connection.transaction_exits == 1
    assert [kind for kind, _sql, _args, _depth in connection.calls] == [
        "execute",
        "fetchrow",
        "execute",
        "execute",
        "execute",
    ]
    _assert_family_locked_first(connection)
    assert all(depth == 1 for _kind, _sql, _args, depth in connection.calls)
    sql = "\n".join(call[1] for call in connection.calls)
    assert "FOR UPDATE OF prior, successor, family" in sql
    assert "SET state = 'revoked'" in sql
    assert "SET state = 'active'" in sql
    arguments = _all_arguments(connection)
    assert "old-refresh-bearer" not in arguments
    assert "new-refresh-bearer" not in arguments
    assert hashlib.sha256(b"old-refresh-bearer").hexdigest() in arguments
    assert hashlib.sha256(b"new-refresh-bearer").hexdigest() in arguments


@pytest.mark.asyncio
async def test_rotation_rollback_does_not_revive_a_revoked_family() -> None:
    connection = _Connection(
        rows=[
            {
                "prior_generation_id": "ogen_old",
                "prior_state": "consumed",
                "replacement_generation_id": "ogen_new",
                "replacement_state": "revoked",
                "replacement_expires_at": object(),
                "replacement_live": True,
                "family_id": "ofam_1",
                "current_generation_id": "ogen_new",
                "family_state": "revoked",
                "family_live": True,
            }
        ]
    )

    restored = await _store(connection).rollback_refresh_token_rotation(
        "old-refresh-bearer",
        "new-refresh-bearer",
        expected_generation="ogen_old",
        expected_replacement_generation="ogen_new",
    )

    assert restored is False
    assert [kind for kind, _sql, _args, _depth in connection.calls] == ["execute", "fetchrow"]
    _assert_family_locked_first(connection)


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
    assert len(connection.calls) == 4
    _assert_family_locked_first(connection)
    assert "SET state = 'revoked'" in connection.calls[2][1]
    assert "WHERE family_id = $1 AND state = 'active'" in connection.calls[3][1]


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
    assert len(connection.calls) == 4
    _assert_family_locked_first(connection)
    assert "FOR UPDATE OF generation, family" in connection.calls[1][1]
    assert "SET state = 'revoked'" in connection.calls[2][1]
    assert "WHERE family_id = $1 AND state = 'active'" in connection.calls[3][1]
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
        "execute",
        "fetch",
        "execute",
        "execute",
        "execute",
    ]
    assert all(depth == 1 for _kind, _sql, _args, depth in connection.calls)
    assert all("refresh-bearer" not in args for _kind, _sql, args, _depth in connection.calls)
    # W408 review: the Card's families are locked in id order before generations.
    assert "ORDER BY family_id" in connection.calls[0][1]
    assert "FOR UPDATE" in connection.calls[0][1]
    assert connection.calls[0][2] == ("aut_card",)
    assert connection.calls[1][2] == ("aut_card",)


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
async def test_failed_issuance_can_restore_then_retry_against_real_postgres() -> None:
    dsn = os.environ.get("CONNECTION_HUB_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("CONNECTION_HUB_TEST_POSTGRES_DSN is not set")

    import asyncpg

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    authority = PostgresOAuthAuthorityStore(
        pg_pool=pool,
        tenant=f"authority-test-{uuid.uuid4().hex}",
        project="refresh-issuance-rollback",
    )
    store = GrantStore(
        object(),
        tenant=authority.tenant,
        project=authority.project,
        authority_store=authority,
    )
    try:
        await authority.ensure_schema()
        old_token = await store.create_refresh_token(
            client_id="client-1",
            sub="user-1",
            scopes=["records:read"],
            registry_access_id="aut_card",
            card_kind="automation",
        )
        old_state = await store.get_refresh_token_state(old_token)
        assert old_state is not None
        withheld_replacement = await store.rotate_refresh_token(
            old_token,
            state=old_state,
        )
        assert withheld_replacement

        assert await store.rollback_refresh_token_rotation(
            old_token,
            withheld_replacement,
            state=old_state,
        ) is True
        assert await store.get_refresh_token_state(old_token) is not None
        assert await store.get_refresh_token_state(withheld_replacement) is None

        delivered_replacement = await store.rotate_refresh_token(old_token)
        assert delivered_replacement
        with pytest.raises(RefreshTokenReuseDetected):
            await store.get_refresh_token_state(old_token)
        assert await store.get_refresh_token_state(delivered_replacement) is None
    finally:
        async with pool.acquire() as connection:
            await connection.execute(
                f"DROP SCHEMA IF EXISTS {authority.schema} CASCADE"
            )
        await pool.close()


@pytest.mark.asyncio
async def test_legacy_client_migration_is_idempotent_against_real_postgres() -> None:
    dsn = os.environ.get("CONNECTION_HUB_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("CONNECTION_HUB_TEST_POSTGRES_DSN is not set")

    import asyncpg

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    store = PostgresOAuthAuthorityStore(
        pg_pool=pool,
        tenant=f"authority-test-{uuid.uuid4().hex}",
        project="legacy-client-migration",
    )
    try:
        await store.ensure_schema()
        target = PostgresOAuthMigrationTarget(store)
        record = AuthorityMigrationRecord(
            record_type="oauth_client",
            identity="legacy-client",
            families=("oauth_clients",),
            payload={
                "migration_state": "active",
                "record": {
                    "client_id": "legacy-client",
                    "redirect_uris": ["http://127.0.0.1/callback"],
                    "token_endpoint_auth_method": "none",
                    "application_type": "native",
                    "metadata": {"kind": "legacy"},
                },
            },
        )

        assert await target.import_record(record) is True
        assert await target.import_record(record) is False
        assert await store.get_client_record("legacy-client", ttl_seconds=600) == {
            "client_id": "legacy-client",
            "redirect_uris": ["http://127.0.0.1/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_method": "none",
            "application_type": "native",
            "metadata": {"kind": "legacy"},
        }
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


@pytest.mark.asyncio
async def test_card_credentials_absolute_expiry_is_replay_safe_against_real_postgres() -> None:
    # W582 (Ops gate 3): one absolute deadline; a replay bumps no revision; a
    # past deadline really expires; a Card with nothing live is a named no-op.
    dsn = os.environ.get("CONNECTION_HUB_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("CONNECTION_HUB_TEST_POSTGRES_DSN is not set")

    import asyncpg

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    store = PostgresOAuthAuthorityStore(
        pg_pool=pool,
        tenant=f"authority-test-{uuid.uuid4().hex}",
        project="card-credential-expiry",
    )

    async def revisions():
        async with pool.acquire() as connection:
            return (
                await connection.fetchval(f"SELECT revision FROM {store.schema}.{TABLE_FAMILIES} WHERE registry_access_id = $1", "aut_card"),
                await connection.fetchval(f"SELECT revision FROM {store.schema}.{TABLE_ACCESS_BINDINGS} WHERE registry_access_id = $1", "aut_card"),
                await connection.fetchval(f"SELECT extract(epoch FROM expires_at)::bigint FROM {store.schema}.{TABLE_ACCESS_BINDINGS} WHERE registry_access_id = $1", "aut_card"),
            )

    try:
        await store.ensure_schema()
        await store.create_refresh_token(
            {"registry_access_id": "aut_card", "card_kind": "automation", "client_id": "client-1", "sub": "user-1"},
            ttl_seconds=600,
        )
        await store.bind_access_grant("access-bearer", {"registry_access_id": "aut_card", "operations": ["search"]},
                                      ttl_seconds=600)
        deadline = int(time.time()) + 7200
        assert await store.card_credentials_live("aut_card") is True
        assert await store.set_card_credentials_expiry("aut_card", deadline) == "applied"
        first = await revisions()
        assert first[2] == deadline
        assert await store.set_card_credentials_expiry("aut_card", deadline) == "applied"
        assert await revisions() == first  # the replay wrote nothing
        past = int(time.time()) - 60
        assert await store.set_card_credentials_expiry("aut_card", past) == "applied"
        assert (await revisions())[2] == past
        assert await store.get_access_grant_record("access-bearer") is None  # really expired
        # Ops 13:19: an ended credential is never revived by a later deadline.
        assert await store.card_credentials_live("aut_card") is False
        ended = await revisions()
        assert await store.set_card_credentials_expiry("aut_card", deadline) == "no_active_credentials"
        assert await revisions() == ended
        assert await store.set_card_credentials_expiry("aut_none", deadline) == "no_active_credentials"
    finally:
        async with pool.acquire() as connection:
            await connection.execute(f"DROP SCHEMA IF EXISTS {store.schema} CASCADE")
        await pool.close()


@pytest.mark.asyncio
async def test_a_binding_is_revoked_by_its_pinned_digest_against_real_postgres() -> None:
    dsn = os.environ.get("CONNECTION_HUB_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("CONNECTION_HUB_TEST_POSTGRES_DSN is not set")

    import asyncpg

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    store = PostgresOAuthAuthorityStore(pg_pool=pool, tenant=f"authority-test-{uuid.uuid4().hex}",
                                        project="grant-revoke-digest")
    try:
        await store.ensure_schema()
        await store.bind_access_grant("old-bearer", {"registry_access_id": "aut_card", "operations": ["s"]},
                                      ttl_seconds=600)
        await store.bind_access_grant("new-bearer", {"registry_access_id": "aut_card", "operations": ["s"]},
                                      ttl_seconds=600)
        old = hashlib.sha256(b"old-bearer").hexdigest()
        assert await store.revoke_access_grant_by_digest(old) == "revoked"
        assert await store.revoke_access_grant_by_digest(old) == "revoked"  # replay-stable (Ops N-O)
        assert await store.revoke_access_grant_by_digest("0" * 64) == "absent"
        assert await store.get_access_grant_record("old-bearer") is None
        assert await store.get_access_grant_record("new-bearer") is not None  # the replacement is untouched
    finally:
        async with pool.acquire() as connection:
            await connection.execute(f"DROP SCHEMA IF EXISTS {store.schema} CASCADE")
        await pool.close()


async def _capped_store():
    dsn = os.environ.get("CONNECTION_HUB_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("CONNECTION_HUB_TEST_POSTGRES_DSN is not set")
    import asyncpg

    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=4)
    store = PostgresOAuthAuthorityStore(pg_pool=pool, tenant=f"cap-test-{uuid.uuid4().hex}", project="w585")
    await store.ensure_schema()
    await store.ensure_schema()  # W585: the additive DDL is idempotent
    return pool, store


async def _family(pool, store, access_id="aut_card"):
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            f"""SELECT extract(epoch FROM expires_at)::bigint AS expires_at,
                       extract(epoch FROM cap_expires_at)::bigint AS cap, card_revision
                FROM {store.schema}.{TABLE_FAMILIES} WHERE registry_access_id = $1""", access_id)
        generation = await connection.fetchrow(
            f"""SELECT extract(epoch FROM generation.expires_at)::bigint AS expires_at, generation.state
                FROM {store.schema}.{TABLE_REFRESH_GENERATIONS} AS generation
                JOIN {store.schema}.{TABLE_FAMILIES} AS family
                  ON family.current_generation_id = generation.generation_id
                WHERE family.registry_access_id = $1""", access_id)
    return dict(row), dict(generation)


async def _drop(pool, store):
    async with pool.acquire() as connection:
        await connection.execute(f"DROP SCHEMA IF EXISTS {store.schema} CASCADE")
    await pool.close()


RECORD = {"registry_access_id": "aut_card", "card_kind": "automation", "client_id": "client-1", "sub": "user-1"}


@pytest.mark.asyncio
async def test_w585_rotation_never_outlives_the_committed_card_cap_against_real_postgres() -> None:
    pool, store = await _capped_store()
    try:
        now = int(time.time())
        token = await store.create_refresh_token(RECORD, ttl_seconds=3600)
        # No cap at all: rotation behaves exactly as before (now + ttl).
        token = await store.rotate_refresh_token(token, RECORD, ttl_seconds=3600)
        family, generation = await _family(pool, store)
        assert family["cap"] is None and abs(family["expires_at"] - (now + 3600)) <= 5
        # The committed Card lifetime (revision 2) sets the cap; rotation stays under it.
        cap = now + 600
        assert await store.set_card_credentials_expiry("aut_card", cap, card_revision=2) == "applied"
        token = await store.rotate_refresh_token(token, RECORD, ttl_seconds=3600)
        family, generation = await _family(pool, store)
        assert (family["cap"], family["card_revision"]) == (cap, 2)
        assert family["expires_at"] == cap and generation["expires_at"] == cap
        # Ops: a caller cap HIGHER than the stored cap still takes the stored cap.
        token = await store.rotate_refresh_token(token, RECORD, ttl_seconds=3600, expires_at_cap=cap + 1000)
        family, generation = await _family(pool, store)
        assert family["expires_at"] == cap and generation["expires_at"] == cap
        # A lower caller cap wins.
        token = await store.rotate_refresh_token(token, RECORD, ttl_seconds=3600, expires_at_cap=cap - 100)
        family, generation = await _family(pool, store)
        assert family["expires_at"] == cap - 100 and generation["expires_at"] == cap - 100
        assert token
    finally:
        await _drop(pool, store)


@pytest.mark.asyncio
async def test_w585_a_passed_cap_or_a_moved_incarnation_refuses_without_consuming_the_token() -> None:
    pool, store = await _capped_store()
    try:
        now = int(time.time())
        token = await store.create_refresh_token(RECORD, ttl_seconds=3600)
        assert await store.set_card_credentials_expiry("aut_card", now + 600, card_revision=3) == "applied"
        # The caller read Card revision 2; the family is capped by revision 3.
        assert await store.rotate_refresh_token(token, RECORD, ttl_seconds=3600, card_incarnation=2) is None
        _, generation = await _family(pool, store)
        assert generation["state"] == "active"  # not consumed: a re-read caller can retry
        assert await store.rotate_refresh_token(token, RECORD, ttl_seconds=3600, expires_at_cap=now - 1) is None
        _, generation = await _family(pool, store)
        assert generation["state"] == "active"
        rotated = await store.rotate_refresh_token(token, RECORD, ttl_seconds=3600, card_incarnation=3)
        assert rotated
    finally:
        await _drop(pool, store)


@pytest.mark.asyncio
async def test_w585_an_older_card_revision_never_moves_the_cap_back() -> None:
    pool, store = await _capped_store()
    try:
        now = int(time.time())
        await store.create_refresh_token(RECORD, ttl_seconds=3600)
        assert await store.set_card_credentials_expiry("aut_card", now + 300, card_revision=4) == "applied"
        # A replayed effect from revision 3 does not touch the family.
        await store.set_card_credentials_expiry("aut_card", now + 3000, card_revision=3)
        family, _ = await _family(pool, store)
        assert (family["cap"], family["card_revision"], family["expires_at"]) == (now + 300, 4, now + 300)
        # The same revision replays without a write.
        assert await store.set_card_credentials_expiry("aut_card", now + 300, card_revision=4) == "applied"
    finally:
        await _drop(pool, store)


@pytest.mark.asyncio
async def test_w585_issuance_carries_the_cap_from_the_start() -> None:
    pool, store = await _capped_store()
    try:
        now = int(time.time())
        await store.create_refresh_token(RECORD, ttl_seconds=3600, cap_expires_at=now + 120, card_revision=5)
        family, generation = await _family(pool, store)
        assert (family["cap"], family["card_revision"]) == (now + 120, 5)
        assert family["expires_at"] == now + 120 and generation["expires_at"] == now + 120
    finally:
        await _drop(pool, store)


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["rotate_first", "effect_first", "concurrent"])
async def test_w585_rotation_racing_the_lifetime_effect_never_passes_the_committed_cap(order) -> None:
    # Ops gate (a): rotation versus the credential_lifetime FINISH, in both orders and concurrently.
    import asyncio
    pool, store = await _capped_store()
    try:
        now = int(time.time())
        token = await store.create_refresh_token(RECORD, ttl_seconds=3600)
        cap = now + 400

        async def rotate():
            return await store.rotate_refresh_token(token, RECORD, ttl_seconds=3600)

        async def effect():
            return await store.set_card_credentials_expiry("aut_card", cap, card_revision=2)

        if order == "rotate_first":
            await rotate()
            await effect()
        elif order == "effect_first":
            await effect()
            await rotate()
        else:
            await asyncio.gather(rotate(), effect())
        family, generation = await _family(pool, store)
        assert family["cap"] == cap and family["card_revision"] == 2
        assert family["expires_at"] <= cap and generation["expires_at"] <= cap  # never revived or extended
    finally:
        await _drop(pool, store)


@pytest.mark.asyncio
async def test_w585_a_stale_lifetime_effect_never_moves_the_access_binding() -> None:
    # Ops W1 (14:25): the binding is revision-guarded like the family. A replayed revision-3
    # effect after the revision-5 cap moves neither the family nor the access binding.
    pool, store = await _capped_store()
    try:
        now = int(time.time())
        await store.create_refresh_token(RECORD, ttl_seconds=3600)
        await store.bind_access_grant("access-bearer", {"registry_access_id": "aut_card", "operations": ["search"]},
                                      ttl_seconds=3600)
        assert await store.set_card_credentials_expiry("aut_card", now + 600, card_revision=5) == "applied"

        async def binding():
            async with pool.acquire() as connection:
                row = await connection.fetchrow(
                    f"""SELECT extract(epoch FROM expires_at)::bigint AS expires_at, card_revision
                        FROM {store.schema}.{TABLE_ACCESS_BINDINGS} WHERE registry_access_id = 'aut_card'""")
            return dict(row)

        assert await binding() == {"expires_at": now + 600, "card_revision": 5}
        for stale in (now + 3000, now + 60):  # later and earlier deadlines from an older revision
            # Every live row is at a newer revision: the superseded effect is the named no-op.
            assert await store.set_card_credentials_expiry("aut_card", stale, card_revision=3) == "no_active_credentials"
            assert await binding() == {"expires_at": now + 600, "card_revision": 5}
            family, _ = await _family(pool, store)
            assert (family["cap"], family["card_revision"]) == (now + 600, 5)
        # A second family issued later without a revision (0) lets the stale effect through the
        # live-row count; the binding's own guard still leaves it at revision 5.
        second = {**RECORD, "client_id": "client-2"}
        await store.create_refresh_token(second, ttl_seconds=3600)
        await store.set_card_credentials_expiry("aut_card", now + 3000, card_revision=3)
        assert await binding() == {"expires_at": now + 600, "card_revision": 5}
    finally:
        await _drop(pool, store)


@pytest.mark.asyncio
async def test_w585_issuance_through_the_public_facade_carries_the_cap_against_real_postgres() -> None:
    # Infra and Ops, 16:13: the SDK issues through GrantStore, never the authority directly.
    pool, store = await _capped_store()
    try:
        now = int(time.time())
        facade = GrantStore(object(), tenant=store.tenant, project=store.project, refresh_ttl=3600,
                            authority_store=store)
        token = await facade.create_refresh_token(
            client_id="client-1", sub="user-1", scopes=[], registry_access_id="aut_card",
            card_kind="automation", cap_expires_at=now + 120, card_revision=5)
        assert token
        family, generation = await _family(pool, store)
        assert (family["cap"], family["card_revision"]) == (now + 120, 5)
        assert family["expires_at"] == now + 120 and generation["expires_at"] == now + 120
        with pytest.raises(ValueError, match="refresh_cap_passed"):
            await facade.create_refresh_token(client_id="client-1", sub="user-1", scopes=[],
                                              registry_access_id="aut_late", cap_expires_at=now - 1)
        async with pool.acquire() as connection:
            assert await connection.fetchval(
                f"SELECT count(*) FROM {store.schema}.{TABLE_FAMILIES} WHERE registry_access_id = 'aut_late'") == 0
    finally:
        await _drop(pool, store)


@pytest.mark.asyncio
async def test_w585_the_redis_fallback_bounds_issuance_by_the_cap() -> None:
    class Redis:
        def __init__(self) -> None:
            self.setex_calls: list[tuple[str, int]] = []

        async def setex(self, key, ttl, value):
            self.setex_calls.append((key, int(ttl)))

    redis = Redis()
    facade = GrantStore(redis, tenant="t", project="p", refresh_ttl=3600)
    now = int(time.time())
    await facade.create_refresh_token(client_id="c", sub="s", scopes=[], cap_expires_at=now + 90)
    await facade.create_refresh_token(client_id="c", sub="s", scopes=[])
    assert 85 <= redis.setex_calls[0][1] <= 90
    assert redis.setex_calls[1][1] == 3600
    with pytest.raises(ValueError, match="refresh_cap_passed"):
        await facade.create_refresh_token(client_id="c", sub="s", scopes=[], cap_expires_at=now - 1)
    assert len(redis.setex_calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("cap_source", ["stored", "caller"])
async def test_w585_a_rotation_that_waits_on_the_family_lock_past_its_cap_is_refused_unconsumed(cap_source) -> None:
    """Ops' gate (16:14): session A holds the family lock across the cap deadline;
    session B's rotation, which began before the deadline, must refuse and leave
    the presented generation unconsumed. now() (the transaction start) passed it."""

    import asyncio

    pool, store = await _capped_store()
    try:
        token = await store.create_refresh_token(RECORD, ttl_seconds=3600)
        async with pool.acquire() as connection:
            database_now = int(await connection.fetchval("SELECT extract(epoch FROM now())::bigint"))
        cap = database_now + 3
        # "stored": the committed Card wrote the cap (family expiry and cap both).
        # "caller": only the rotation's own cap bounds it, so the cap check alone refuses.
        if cap_source == "stored":
            assert await store.set_card_credentials_expiry("aut_card", cap, card_revision=1) == "applied"
        caller_cap = {"expires_at_cap": cap} if cap_source == "caller" else {}
        holder = await pool.acquire()
        lock = holder.transaction()
        await lock.start()
        await holder.execute(f"SELECT 1 FROM {store.schema}.{TABLE_FAMILIES} WHERE registry_access_id = 'aut_card' FOR UPDATE")
        rotation = asyncio.create_task(store.rotate_refresh_token(token, RECORD, ttl_seconds=3600, **caller_cap))
        await asyncio.sleep(0.5)
        assert not rotation.done(), "the rotation must be waiting on the family lock"
        while int(time.time()) <= cap + 1:
            await asyncio.sleep(0.25)
        await lock.rollback()
        await pool.release(holder)
        assert await asyncio.wait_for(rotation, 10) is None
        _, generation = await _family(pool, store)
        assert generation["state"] == "active", "the presented generation is not consumed"
    finally:
        await _drop(pool, store)


async def _hold_family_lock(pool, store, access_id="aut_card"):
    holder = await pool.acquire()
    lock = holder.transaction()
    await lock.start()
    await holder.execute(
        f"SELECT 1 FROM {store.schema}.{TABLE_FAMILIES} WHERE registry_access_id = $1 FOR UPDATE", access_id)
    return holder, lock


async def _release(pool, holder, lock):
    await lock.rollback()
    await pool.release(holder)


@pytest.mark.asyncio
async def test_w585_a_no_cap_rotation_that_waits_past_the_generation_expiry_is_refused_unconsumed() -> None:
    """Ops T5 (16:32): no cap at all, so only generation_live and family_live
    guard; both read the clock after the lock."""

    import asyncio

    pool, store = await _capped_store()
    try:
        token = await store.create_refresh_token(RECORD, ttl_seconds=3)
        holder, lock = await _hold_family_lock(pool, store)
        rotation = asyncio.create_task(store.rotate_refresh_token(token, RECORD, ttl_seconds=3600))
        await asyncio.sleep(0.5)
        assert not rotation.done(), "the rotation must be waiting on the family lock"
        await asyncio.sleep(3.5)
        await _release(pool, holder, lock)
        assert await asyncio.wait_for(rotation, 10) is None
        _, generation = await _family(pool, store)
        assert generation["state"] == "active", "the presented generation is not consumed"
    finally:
        await _drop(pool, store)


@pytest.mark.asyncio
async def test_w585_a_retry_that_waits_past_the_family_end_mints_nothing_and_revives_nothing() -> None:
    """Ops C1 (16:32): the W408 retry branch decided with now() after the lock,
    so a retry that started before the family ended minted a successor and
    revived the family to +ttl."""

    import asyncio

    pool, store = await _capped_store()
    try:
        token = await store.create_refresh_token(RECORD, ttl_seconds=4)
        rotated = await store.rotate_refresh_token(token, RECORD, ttl_seconds=4, refresh_request_fingerprint="fp-1")
        assert rotated
        family_before, _ = await _family(pool, store)
        holder, lock = await _hold_family_lock(pool, store)
        retry = asyncio.create_task(
            store.rotate_refresh_token(token, RECORD, ttl_seconds=3600, refresh_request_fingerprint="fp-1"))
        await asyncio.sleep(0.5)
        assert not retry.done(), "the retry must be waiting on the family lock"
        while int(time.time()) <= family_before["expires_at"] + 1:
            await asyncio.sleep(0.25)
        await _release(pool, holder, lock)
        assert await asyncio.wait_for(retry, 10) is None
        family_after, _ = await _family(pool, store)
        assert family_after["expires_at"] == family_before["expires_at"], "the family is not revived"
    finally:
        await _drop(pool, store)

from __future__ import annotations

import asyncio
import os
import uuid
from collections import deque
from contextlib import asynccontextmanager
from typing import Any

import pytest

from connection_hub.delegated_credentials.cards.handle_authority import (
    PostgresCardHandleMetadataStore,
)
from connection_hub.delegated_credentials.cards.handle_metadata import (
    CardHandleMetadata,
    CardHandleMetadataConflict,
    HANDLE_STATE_REVOKED,
    RetiredResidentSecret,
)
from connection_hub.delegated_credentials.cards.handle_schema import (
    TABLE_CARD_HANDLE_METADATA,
    TABLE_RETIRED_RESIDENT_SECRETS,
    card_handle_schema_sql,
)


class _Connection:
    def __init__(
        self,
        *,
        rows: list[dict[str, Any] | None] | None = None,
        values: list[Any] | None = None,
        execute_results: list[str] | None = None,
    ) -> None:
        self.rows = deque(rows or [])
        self.values = deque(values or [])
        self.execute_results = deque(execute_results or [])
        self.calls: list[tuple[str, str, tuple[Any, ...], int]] = []
        self.transaction_depth = 0

    @asynccontextmanager
    async def transaction(self):
        self.transaction_depth += 1
        try:
            yield
        finally:
            self.transaction_depth -= 1

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append(("execute", sql, args, self.transaction_depth))
        return self.execute_results.popleft() if self.execute_results else "OK"

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.calls.append(("fetchrow", sql, args, self.transaction_depth))
        return self.rows.popleft() if self.rows else None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.calls.append(("fetchval", sql, args, self.transaction_depth))
        return self.values.popleft() if self.values else None

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append(("fetch", sql, args, self.transaction_depth))
        row = self.rows.popleft() if self.rows else None
        return list(row) if isinstance(row, list) else []


class _Pool:
    def __init__(
        self,
        connection: _Connection,
        *,
        direct_rows: list[dict[str, Any] | None] | None = None,
        direct_lists: list[list[dict[str, Any]]] | None = None,
    ) -> None:
        self.connection = connection
        self.direct_rows = deque(direct_rows or [])
        self.direct_lists = deque(direct_lists or [])
        self.calls: list[tuple[str, str, tuple[Any, ...]]] = []

    @asynccontextmanager
    async def acquire(self):
        yield self.connection

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.calls.append(("fetchrow", sql, args))
        return self.direct_rows.popleft() if self.direct_rows else None

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append(("fetch", sql, args))
        return self.direct_lists.popleft() if self.direct_lists else []


def _row(
    *,
    revision: int = 1,
    card_revision: int = 3,
    state: str = "active",
    secret_ref: str = "vault://resident/card-1",
    fingerprint: str = "a" * 64,
    session_id: str = "session-1",
    expires_at: int = 2_000,
) -> dict[str, Any]:
    return {
        "access_id": "agent_card_1",
        "card_revision": card_revision,
        "resident_access_secret_ref": secret_ref,
        "resident_access_sha256": fingerprint,
        "session_id": session_id,
        "state": state,
        "revision": revision,
        "created_at": 1_000,
        "updated_at": 1_000 + revision,
        "retired_at": 1_500 if state != "active" else 0,
        "expires_at": expires_at,
    }


def _metadata(**changes: Any) -> CardHandleMetadata:
    values = _row()
    values.update(changes)
    return CardHandleMetadata(
        access_id=values["access_id"],
        card_revision=values["card_revision"],
        resident_access_secret_ref=values["resident_access_secret_ref"],
        resident_access_sha256=values["resident_access_sha256"],
        session_id=values["session_id"],
        state=values["state"],
        expires_at=values["expires_at"],
    )


def _store(
    connection: _Connection, **pool_kwargs: Any
) -> PostgresCardHandleMetadataStore:
    return PostgresCardHandleMetadataStore(
        pg_pool=_Pool(connection, **pool_kwargs),
        tenant="demo-tenant",
        project="demo-project",
    )


def test_schema_is_minimized_and_contains_no_raw_bearer_columns() -> None:
    sql = card_handle_schema_sql("kdcube_demo_tenant_demo_project")

    assert "resident_access_secret_ref" in sql
    assert "resident_access_sha256" in sql
    assert "session_id" in sql
    assert "access_token" not in sql
    assert "refresh_token" not in sql
    assert "oauth_family" not in sql
    assert "revision" in sql
    assert "^[0-9a-f]{64}$" in sql
    assert TABLE_RETIRED_RESIDENT_SECRETS in sql
    assert "ON DELETE RESTRICT" in sql
    assert "connection_hub_card_handle_current_secret_ref_uidx" in sql


def test_metadata_requires_secret_reference_and_fingerprint_together() -> None:
    with pytest.raises(ValueError, match="must travel together"):
        _metadata(resident_access_sha256="").validated()


@pytest.mark.asyncio
async def test_put_inserts_under_one_transaction() -> None:
    connection = _Connection(rows=[None, None, _row()])
    saved = await _store(connection).put(_metadata(), expected_revision=0)

    assert saved.revision == 1
    assert [kind for kind, _sql, _args, _depth in connection.calls] == [
        "fetchrow",
        "execute",
        "fetchrow",
        "fetchrow",
    ]
    assert all(depth == 1 for _kind, _sql, _args, depth in connection.calls)
    assert "FOR UPDATE" in connection.calls[0][1]
    assert "pg_advisory_xact_lock" in connection.calls[1][1]
    assert "INSERT INTO" in connection.calls[3][1]


@pytest.mark.asyncio
async def test_identical_put_does_not_write_or_bump_revision() -> None:
    connection = _Connection(rows=[_row()])

    saved = await _store(connection).put(_metadata(), expected_revision=1)

    assert saved.revision == 1
    assert len(connection.calls) == 1
    assert "FOR UPDATE" in connection.calls[0][1]


@pytest.mark.asyncio
async def test_put_refuses_a_stale_metadata_revision() -> None:
    connection = _Connection(rows=[_row(revision=4)])

    with pytest.raises(CardHandleMetadataConflict) as error:
        await _store(connection).put(
            _metadata(session_id="session-2"), expected_revision=3
        )

    assert error.value.reason == "card_handle_revision_conflict"
    assert error.value.current_revision == 4
    assert len(connection.calls) == 1


@pytest.mark.asyncio
async def test_put_records_replaced_secret_for_cleanup_in_same_transaction() -> None:
    replacement_ref = "vault://resident/card-2"
    replacement_fingerprint = "b" * 64
    connection = _Connection(
        rows=[
            _row(revision=2),
            None,
            _row(
                revision=3,
                secret_ref=replacement_ref,
                fingerprint=replacement_fingerprint,
            ),
            {"secret_ref": "vault://resident/card-1"},
        ]
    )

    saved = await _store(connection).put(
        _metadata(
            resident_access_secret_ref=replacement_ref,
            resident_access_sha256=replacement_fingerprint,
        ),
        expected_revision=2,
    )

    assert saved.revision == 3
    assert [kind for kind, _sql, _args, _depth in connection.calls] == [
        "fetchrow",
        "execute",
        "execute",
        "fetchrow",
        "fetchrow",
        "fetchrow",
    ]
    ledger_call = connection.calls[-1]
    assert TABLE_RETIRED_RESIDENT_SECRETS in ledger_call[1]
    assert ledger_call[2] == (
        "vault://resident/card-1",
        "agent_card_1",
        "a" * 64,
    )
    assert all(depth == 1 for _kind, _sql, _args, depth in connection.calls)


@pytest.mark.asyncio
async def test_put_refuses_a_secret_reference_pending_cleanup() -> None:
    connection = _Connection(
        rows=[
            _row(
                revision=3,
                secret_ref="vault://resident/card-2",
                fingerprint="b" * 64,
            ),
            {
                "conflict_kind": "pending_cleanup",
                "access_id": "agent_card_1",
            },
        ]
    )

    with pytest.raises(CardHandleMetadataConflict) as error:
        await _store(connection).put(_metadata(), expected_revision=3)

    assert error.value.reason == "card_handle_secret_ref_pending_cleanup"
    assert (
        sum(
            "pg_advisory_xact_lock" in sql
            for _kind, sql, _args, _depth in connection.calls
        )
        == 2
    )
    assert not any(
        sql.lstrip().startswith("UPDATE")
        for _kind, sql, _args, _depth in connection.calls
    )


@pytest.mark.asyncio
async def test_put_rejects_changed_fingerprint_for_same_secret_reference() -> None:
    connection = _Connection(rows=[_row(revision=2)])

    with pytest.raises(CardHandleMetadataConflict) as error:
        await _store(connection).put(
            _metadata(resident_access_sha256="b" * 64),
            expected_revision=2,
        )

    assert (
        error.value.reason == "card_handle_secret_fingerprint_changed_without_new_ref"
    )
    assert len(connection.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        (
            {"card_revision": 4},
            "card_handle_secret_card_revision_changed_without_new_ref",
        ),
        (
            {"expires_at": 2_500},
            "card_handle_secret_expiry_changed_without_new_ref",
        ),
    ],
)
async def test_put_rejects_changed_envelope_binding_for_same_secret_reference(
    changes: dict[str, Any],
    reason: str,
) -> None:
    connection = _Connection(rows=[_row(revision=2)])

    with pytest.raises(CardHandleMetadataConflict) as error:
        await _store(connection).put(
            _metadata(**changes),
            expected_revision=2,
        )

    assert error.value.reason == reason
    assert len(connection.calls) == 1


@pytest.mark.asyncio
async def test_reactivation_requires_a_fresh_resident_secret_reference() -> None:
    connection = _Connection(rows=[_row(revision=3, state="revoked")])

    with pytest.raises(CardHandleMetadataConflict) as error:
        await _store(connection).put(
            _metadata(card_revision=4),
            expected_revision=3,
        )

    assert error.value.reason == "card_handle_reactivation_requires_fresh_secret_ref"
    assert len(connection.calls) == 1


@pytest.mark.asyncio
async def test_negative_revision_fences_fail_before_database_access() -> None:
    connection = _Connection()
    store = _store(connection)

    with pytest.raises(ValueError, match="must not be negative"):
        await store.retire(
            "agent_card_1",
            expected_revision=-1,
            state=HANDLE_STATE_REVOKED,
        )
    with pytest.raises(ValueError, match="must not be negative"):
        await store.clear_retired_resident_secret(
            "agent_card_1",
            expected_revision=-1,
        )

    assert connection.calls == []


@pytest.mark.asyncio
async def test_retirement_keeps_secret_reference_until_delete_confirmation() -> None:
    connection = _Connection(
        rows=[
            _row(revision=2),
            _row(revision=3, state="revoked"),
            _row(revision=3, state="revoked"),
            _row(
                revision=4,
                state="revoked",
                secret_ref="",
                fingerprint="",
            ),
        ]
    )
    store = _store(connection)

    retired = await store.retire(
        "agent_card_1",
        expected_revision=2,
        state=HANDLE_STATE_REVOKED,
    )
    assert retired is not None
    assert retired.resident_access_secret_ref == "vault://resident/card-1"

    cleared = await store.clear_retired_resident_secret(
        "agent_card_1",
        expected_revision=3,
    )
    assert cleared is not None
    assert cleared.resident_access_secret_ref == ""
    assert cleared.revision == 4
    assert "resident_access_secret_ref = ''" in connection.calls[-1][1]


@pytest.mark.asyncio
async def test_purge_is_bounded_and_excludes_pending_secret_cleanup() -> None:
    connection = _Connection(values=[5])

    removed = await _store(connection).purge_terminal(
        retired_before=2_000,
        limit=25,
    )

    assert removed == 5
    _kind, sql, args, depth = connection.calls[0]
    assert depth == 1
    assert "resident_access_secret_ref = ''" in sql
    assert TABLE_RETIRED_RESIDENT_SECRETS in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert args == (2_000, 25)


@pytest.mark.asyncio
async def test_expire_due_is_bounded_and_returns_cleanup_metadata() -> None:
    connection = _Connection(
        rows=[[_row(revision=3, state="expired", expires_at=1_500)]]
    )

    expired = await _store(connection).expire_due(now=2_000, limit=10)

    assert len(expired) == 1
    assert expired[0].state == "expired"
    assert expired[0].resident_access_secret_ref == "vault://resident/card-1"
    _kind, sql, args, depth = connection.calls[0]
    assert depth == 1
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "SET state = 'expired'" in sql
    assert args == (2_000, 10)


@pytest.mark.asyncio
async def test_retired_secret_cleanup_is_listed_and_acknowledged() -> None:
    retired_row = {
        "access_id": "agent_card_1",
        "secret_ref": "vault://resident/card-1",
        "resident_access_sha256": "a" * 64,
        "retired_at": 1_500,
    }
    connection = _Connection(execute_results=["DELETE 1", "DELETE 0"])
    store = _store(connection, direct_lists=[[retired_row]])

    candidates = await store.list_retired_secret_cleanup_candidates(limit=3)

    assert candidates == [
        RetiredResidentSecret(
            access_id="agent_card_1",
            secret_ref="vault://resident/card-1",
            resident_access_sha256="a" * 64,
            retired_at=1_500,
        )
    ]
    assert await store.delete_retired_secret_record(
        access_id="agent_card_1",
        secret_ref="vault://resident/card-1",
    )
    assert not await store.delete_retired_secret_record(
        access_id="agent_card_1",
        secret_ref="vault://resident/card-1",
    )
    assert all(depth == 1 for _kind, _sql, _args, depth in connection.calls)


@pytest.mark.asyncio
async def test_card_handle_contract_against_real_postgres() -> None:
    dsn = os.environ.get("CONNECTION_HUB_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("CONNECTION_HUB_TEST_POSTGRES_DSN is not set")

    import asyncpg

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3)
    store = PostgresCardHandleMetadataStore(
        pg_pool=pool,
        tenant=f"w253-test-{uuid.uuid4().hex}",
        project="card-handles",
    )
    try:
        await store.ensure_schema()
        candidate = CardHandleMetadata(
            access_id="agent_card_1",
            card_revision=7,
            resident_access_secret_ref="vault://resident/card-1",
            resident_access_sha256="b" * 64,
            session_id="session-1",
            expires_at=4_000_000_000,
        )
        created = await store.put(candidate, expected_revision=0)
        assert created.revision == 1

        assert (await store.read_active("agent_card_1", now=2_000)) == created
        assert (await store.put(candidate, expected_revision=1)).revision == 1

        changed = CardHandleMetadata(
            access_id="agent_card_1",
            card_revision=7,
            resident_access_secret_ref="vault://resident/card-1",
            resident_access_sha256="b" * 64,
            session_id="session-2",
            expires_at=4_000_000_000,
        )
        outcomes = await asyncio.gather(
            store.put(changed, expected_revision=1),
            store.put(changed, expected_revision=1),
            return_exceptions=True,
        )
        assert sum(isinstance(value, CardHandleMetadata) for value in outcomes) == 1
        assert (
            sum(isinstance(value, CardHandleMetadataConflict) for value in outcomes)
            == 1
        )

        current = await store.read_current("agent_card_1")
        assert current is not None
        assert current.revision == 2
        unsafe_same_ref_binding = CardHandleMetadata(
            access_id="agent_card_1",
            card_revision=8,
            resident_access_secret_ref="vault://resident/card-1",
            resident_access_sha256="b" * 64,
            session_id="session-2",
            expires_at=4_000_000_000,
        )
        with pytest.raises(CardHandleMetadataConflict) as binding_error:
            await store.put(
                unsafe_same_ref_binding,
                expected_revision=current.revision,
            )
        assert (
            binding_error.value.reason
            == "card_handle_secret_card_revision_changed_without_new_ref"
        )
        assert await store.read_current("agent_card_1") == current

        replacement = CardHandleMetadata(
            access_id="agent_card_1",
            card_revision=7,
            resident_access_secret_ref="vault://resident/card-2",
            resident_access_sha256="c" * 64,
            session_id="session-2",
            expires_at=4_000_000_000,
        )
        other = CardHandleMetadata(
            access_id="other_card_1",
            card_revision=1,
            resident_access_secret_ref="vault://resident/card-other",
            resident_access_sha256="d" * 64,
            session_id="session-other",
            expires_at=4_000_000_000,
        )
        assert (await store.put(other, expected_revision=0)).revision == 1
        unsafe_adoption = CardHandleMetadata(
            access_id="other_card_1",
            card_revision=1,
            resident_access_secret_ref="vault://resident/card-1",
            resident_access_sha256="b" * 64,
            session_id="session-other",
            expires_at=4_000_000_000,
        )
        rotation_race = await asyncio.gather(
            store.put(replacement, expected_revision=current.revision),
            store.put(unsafe_adoption, expected_revision=1),
            return_exceptions=True,
        )
        assert isinstance(rotation_race[0], CardHandleMetadata)
        assert isinstance(rotation_race[1], CardHandleMetadataConflict)
        assert rotation_race[1].reason in {
            "card_handle_secret_ref_already_current",
            "card_handle_secret_ref_pending_cleanup",
        }
        rotated = rotation_race[0]
        assert rotated.revision == 3
        retired_secrets = await store.list_retired_secret_cleanup_candidates()
        assert len(retired_secrets) == 1
        assert retired_secrets[0].secret_ref == "vault://resident/card-1"
        with pytest.raises(CardHandleMetadataConflict) as reuse_error:
            await store.put(candidate, expected_revision=rotated.revision)
        assert reuse_error.value.reason == "card_handle_secret_ref_pending_cleanup"
        assert await store.read_current("agent_card_1") == rotated

        retired = await store.retire(
            "agent_card_1",
            expected_revision=rotated.revision,
            state=HANDLE_STATE_REVOKED,
        )
        assert retired is not None
        assert retired.revision == 4
        assert await store.read_active("agent_card_1", now=2_000) is None

        unsafe_reactivation = CardHandleMetadata(
            access_id="agent_card_1",
            card_revision=8,
            resident_access_secret_ref="vault://resident/card-2",
            resident_access_sha256="c" * 64,
            session_id="session-2",
            expires_at=4_000_000_000,
        )
        with pytest.raises(CardHandleMetadataConflict) as reactivation_error:
            await store.put(
                unsafe_reactivation,
                expected_revision=retired.revision,
            )
        assert (
            reactivation_error.value.reason
            == "card_handle_reactivation_requires_fresh_secret_ref"
        )

        cleared = await store.clear_retired_resident_secret(
            "agent_card_1",
            expected_revision=retired.revision,
        )
        assert cleared is not None
        assert cleared.revision == 5
        assert cleared.resident_access_secret_ref == ""
        assert (
            await store.purge_terminal(
                retired_before=4_100_000_000,
                limit=1,
            )
            == 0
        )
        assert await store.delete_retired_secret_record(
            access_id="agent_card_1",
            secret_ref="vault://resident/card-1",
        )
        assert await store.list_retired_secret_cleanup_candidates() == []
        assert (
            await store.purge_terminal(
                retired_before=4_100_000_000,
                limit=1,
            )
            == 1
        )

        expiring = CardHandleMetadata(
            access_id="manual_card_1",
            card_revision=1,
            session_id="session-3",
            expires_at=1_000,
        )
        assert (await store.put(expiring, expected_revision=0)).revision == 1
        expired = await store.expire_due(now=2_000, limit=1)
        assert len(expired) == 1
        assert expired[0].access_id == "manual_card_1"
        assert expired[0].state == "expired"
        assert (
            await store.purge_terminal(
                retired_before=4_100_000_000,
                limit=1,
            )
            == 1
        )
    finally:
        async with pool.acquire() as connection:
            await connection.execute(f"DROP SCHEMA IF EXISTS {store.schema} CASCADE")
        await pool.close()


def test_table_name_is_stable() -> None:
    assert TABLE_CARD_HANDLE_METADATA == "connection_hub_card_handle_metadata"
    assert (
        TABLE_RETIRED_RESIDENT_SECRETS == "connection_hub_card_retired_resident_secrets"
    )

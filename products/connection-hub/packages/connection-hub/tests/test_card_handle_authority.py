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
    CLEANUP_SOURCE_PREPARED,
    CLEANUP_SOURCE_RETIRED,
    CLEANUP_SOURCE_TERMINAL,
    HANDLE_STATE_REVOKED,
    CardHandleMetadata,
    CardHandleMetadataConflict,
    CardHandleMutationResult,
    PreparedResidentSecret,
    ResidentSecretCleanupAcknowledgement,
    ResidentSecretCleanupClaim,
    RetiredResidentSecret,
)
from connection_hub.delegated_credentials.cards.handle_schema import (
    TABLE_CARD_HANDLE_METADATA,
    TABLE_PREPARED_RESIDENT_SECRETS,
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

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append(("execute", sql, args))
        return await self.connection.execute(sql, *args)


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


def _prepared_row(
    *,
    secret_ref: str = "vault://resident/card-2",
    fingerprint: str = "b" * 64,
    card_revision: int = 3,
    created_at: int = 1_100,
    expires_at: int = 2_000,
    state: str = "installable",
) -> dict[str, Any]:
    return {
        "access_id": "agent_card_1",
        "secret_ref": secret_ref,
        "resident_access_sha256": fingerprint,
        "card_revision": card_revision,
        "created_at": created_at,
        "expires_at": expires_at,
        "state": state,
    }


def _prepared(**changes: Any) -> PreparedResidentSecret:
    values = _prepared_row()
    values.update(changes)
    return PreparedResidentSecret(**values).validated()


def _cleanup_claim_row(
    *,
    source: str = CLEANUP_SOURCE_PREPARED,
    secret_ref: str = "vault://resident/card-2",
    fingerprint: str = "b" * 64,
    card_revision: int = 3,
    created_at: int = 1_100,
    expires_at: int = 2_000,
    metadata_revision: int = 0,
    attempt: int = 1,
) -> dict[str, Any]:
    return {
        "source": source,
        "access_id": "agent_card_1",
        "secret_ref": secret_ref,
        "resident_access_sha256": fingerprint,
        "claim_token": "c" * 32,
        "attempt": attempt,
        "claimed_at": 2_100,
        "claim_expires_at": 2_160,
        "card_revision": card_revision,
        "created_at": created_at,
        "expires_at": expires_at,
        "metadata_revision": metadata_revision,
    }


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
    assert TABLE_PREPARED_RESIDENT_SECRETS in sql
    assert "ON DELETE RESTRICT" in sql
    assert "connection_hub_card_handle_current_secret_ref_uidx" in sql
    assert "cleanup_claim_token" in sql
    assert "cleanup_next_attempt_at" in sql
    assert "cleanup_last_error" in sql
    assert "ADD COLUMN IF NOT EXISTS cleanup_attempts" in sql
    assert "ch_card_handle_cleanup_claim_ck" in sql
    assert "ch_card_prepared_state_ck" in sql
    assert "ch_card_prepared_cleanup_claim_ck" in sql
    assert "ch_card_retired_cleanup_claim_ck" in sql
    assert "connection_hub_card_retired_secret_cleanup_due_idx" in sql


@pytest.mark.asyncio
async def test_prepare_resident_secret_reserves_reference_in_one_transaction() -> None:
    connection = _Connection(rows=[{"secret_ref": "vault://resident/card-2"}])

    created = await _store(connection).prepare_resident_secret(_prepared())

    assert created
    assert [kind for kind, _sql, _args, _depth in connection.calls] == [
        "execute",
        "fetchrow",
    ]
    assert "pg_advisory_xact_lock" in connection.calls[0][1]
    assert TABLE_PREPARED_RESIDENT_SECRETS in connection.calls[1][1]
    assert all(depth == 1 for _kind, _sql, _args, depth in connection.calls)


@pytest.mark.asyncio
async def test_prepared_cleanup_claim_is_durable_bounded_and_irreversible() -> None:
    connection = _Connection(rows=[[_cleanup_claim_row()]])

    claims = await _store(connection).claim_prepared_secret_cleanup(
        now=2_100,
        limit=2,
    )

    assert claims == [ResidentSecretCleanupClaim(**_cleanup_claim_row()).validated()]
    _kind, sql, args, depth = connection.calls[0]
    assert depth == 1
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "SET state = 'cleanup'" in sql
    assert "cleanup_next_attempt_at <=" in sql
    assert "ORDER BY" in sql
    assert args[0:2] == (2_100, 2)


@pytest.mark.asyncio
async def test_install_refuses_a_prepared_reference_claimed_for_cleanup() -> None:
    connection = _Connection(rows=[None, _prepared_row(state="cleanup")])
    store = _store(connection)

    with pytest.raises(CardHandleMetadataConflict) as error:
        await store.install_prepared_resident_secret(
            _metadata(
                resident_access_secret_ref="vault://resident/card-2",
                resident_access_sha256="b" * 64,
            ),
            expected_revision=0,
        )

    assert error.value.reason == "card_handle_prepared_secret_cleanup_started"
    assert not any(
        sql.lstrip().startswith("INSERT INTO")
        and TABLE_CARD_HANDLE_METADATA in sql
        for _kind, sql, _args, _depth in connection.calls
    )


@pytest.mark.asyncio
async def test_cleanup_acknowledgement_is_fenced_by_claim_token() -> None:
    connection = _Connection(execute_results=["OK", "DELETE 1"])
    claim = ResidentSecretCleanupClaim(**_cleanup_claim_row()).validated()

    acknowledgement = await _store(
        connection
    ).acknowledge_resident_secret_cleanup(claim)

    assert acknowledgement.acknowledged
    assert acknowledgement.metadata is None
    assert connection.calls[-1][2] == (
        claim.secret_ref,
        claim.access_id,
        claim.claim_token,
    )
    assert all(depth == 1 for _kind, _sql, _args, depth in connection.calls)


@pytest.mark.asyncio
async def test_terminal_cleanup_ack_locks_card_row_before_secret_reference() -> None:
    claim = ResidentSecretCleanupClaim(
        **_cleanup_claim_row(
            source=CLEANUP_SOURCE_TERMINAL,
            secret_ref="vault://resident/card-1",
            fingerprint="a" * 64,
            metadata_revision=2,
            created_at=0,
        )
    ).validated()
    connection = _Connection(
        rows=[
            _row(
                revision=3,
                state=HANDLE_STATE_REVOKED,
                secret_ref="",
                fingerprint="",
            )
        ],
        values=[claim.access_id],
        execute_results=["OK"],
    )

    acknowledgement = await _store(
        connection
    ).acknowledge_resident_secret_cleanup(claim)

    assert acknowledgement.acknowledged
    assert [kind for kind, _sql, _args, _depth in connection.calls] == [
        "fetchval",
        "execute",
        "fetchrow",
    ]
    assert "FOR UPDATE" in connection.calls[0][1]
    assert "pg_advisory_xact_lock" in connection.calls[1][1]
    assert connection.calls[2][1].lstrip().startswith("UPDATE")
    assert all(depth == 1 for _kind, _sql, _args, depth in connection.calls)


@pytest.mark.asyncio
async def test_failed_cleanup_is_rescheduled_with_bounded_backoff() -> None:
    connection = _Connection(execute_results=["UPDATE 1"])
    claim = ResidentSecretCleanupClaim(**_cleanup_claim_row()).validated()

    recorded = await _store(connection).defer_resident_secret_cleanup(
        claim,
        now=2_200,
        reason="resident_secret_cleanup_fingerprint_mismatch",
    )

    assert recorded
    _kind, sql, args, _depth = connection.calls[-1]
    assert "cleanup_next_attempt_at" in sql
    assert "cleanup_last_error" in sql
    assert args == (
        claim.secret_ref,
        claim.claim_token,
        2_205,
        "resident_secret_cleanup_fingerprint_mismatch",
    )


@pytest.mark.asyncio
async def test_cleanup_retry_backoff_is_capped_at_one_hour() -> None:
    connection = _Connection(execute_results=["UPDATE 1"])
    claim = ResidentSecretCleanupClaim(
        **_cleanup_claim_row(attempt=20)
    ).validated()

    recorded = await _store(connection).defer_resident_secret_cleanup(
        claim,
        now=2_200,
        reason="resident_secret_cleanup_delete_failed",
    )

    assert recorded
    assert connection.calls[-1][2][2] == 5_800


@pytest.mark.asyncio
async def test_cleanup_retry_records_a_reason_when_the_caller_supplies_whitespace() -> (
    None
):
    connection = _Connection(execute_results=["UPDATE 1"])
    claim = ResidentSecretCleanupClaim(**_cleanup_claim_row()).validated()

    recorded = await _store(connection).defer_resident_secret_cleanup(
        claim,
        now=2_200,
        reason="   ",
    )

    assert recorded
    assert connection.calls[-1][2][3] == "resident_secret_cleanup_failed"


@pytest.mark.asyncio
async def test_install_consumes_prepared_intent_and_returns_exact_retired_secret() -> None:
    replacement_ref = "vault://resident/card-2"
    replacement_fingerprint = "b" * 64
    connection = _Connection(
        rows=[
            _row(revision=2),
            _prepared_row(),
            _row(
                revision=3,
                secret_ref=replacement_ref,
                fingerprint=replacement_fingerprint,
            ),
            {
                "access_id": "agent_card_1",
                "secret_ref": "vault://resident/card-1",
                "resident_access_sha256": "a" * 64,
                "retired_at": 1_500,
            },
        ],
        execute_results=["OK", "OK", "DELETE 1"],
    )

    result = await _store(connection).install_prepared_resident_secret(
        _metadata(
            resident_access_secret_ref=replacement_ref,
            resident_access_sha256=replacement_fingerprint,
        ),
        expected_revision=2,
    )

    assert result.metadata.revision == 3
    assert result.retired_secret == RetiredResidentSecret(
        access_id="agent_card_1",
        secret_ref="vault://resident/card-1",
        resident_access_sha256="a" * 64,
        retired_at=1_500,
    )
    assert TABLE_PREPARED_RESIDENT_SECRETS in connection.calls[-1][1]
    assert connection.calls[-1][0] == "execute"
    assert all(depth == 1 for _kind, _sql, _args, depth in connection.calls)


def test_metadata_requires_secret_reference_and_fingerprint_together() -> None:
    with pytest.raises(ValueError, match="must travel together"):
        _metadata(resident_access_sha256="").validated()


@pytest.mark.asyncio
async def test_put_inserts_non_secret_metadata_under_one_transaction() -> None:
    connection = _Connection(
        rows=[None, _row(secret_ref="", fingerprint="")]
    )
    saved = await _store(connection).put(
        _metadata(resident_access_secret_ref="", resident_access_sha256=""),
        expected_revision=0,
    )

    assert saved.revision == 1
    assert [kind for kind, _sql, _args, _depth in connection.calls] == [
        "fetchrow",
        "fetchrow",
    ]
    assert all(depth == 1 for _kind, _sql, _args, depth in connection.calls)
    assert "FOR UPDATE" in connection.calls[0][1]
    assert "INSERT INTO" in connection.calls[1][1]


@pytest.mark.asyncio
async def test_put_cannot_introduce_a_resident_reference_without_preparation() -> (
    None
):
    connection = _Connection(rows=[None])

    with pytest.raises(CardHandleMetadataConflict) as error:
        await _store(connection).put(_metadata(), expected_revision=0)

    assert error.value.reason == "card_handle_prepared_secret_required"
    assert len(connection.calls) == 1
    assert not any(
        sql.lstrip().startswith("INSERT INTO")
        for _kind, sql, _args, _depth in connection.calls
    )


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
async def test_put_cannot_replace_a_resident_reference_without_custody_protocol() -> (
    None
):
    replacement_ref = "vault://resident/card-2"
    replacement_fingerprint = "b" * 64
    connection = _Connection(rows=[_row(revision=2)])

    with pytest.raises(CardHandleMetadataConflict) as error:
        await _store(connection).put(
            _metadata(
                resident_access_secret_ref=replacement_ref,
                resident_access_sha256=replacement_fingerprint,
            ),
            expected_revision=2,
        )

    assert error.value.reason == (
        "card_handle_resident_secret_mutation_requires_custody_protocol"
    )
    assert len(connection.calls) == 1


@pytest.mark.asyncio
async def test_put_cannot_clear_a_resident_reference_without_custody_protocol() -> (
    None
):
    connection = _Connection(rows=[_row(revision=3)])

    with pytest.raises(CardHandleMetadataConflict) as error:
        await _store(connection).put(
            _metadata(resident_access_secret_ref="", resident_access_sha256=""),
            expected_revision=3,
        )

    assert error.value.reason == (
        "card_handle_resident_secret_mutation_requires_custody_protocol"
    )
    assert len(connection.calls) == 1


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

    assert connection.calls == []


@pytest.mark.asyncio
async def test_retirement_keeps_secret_reference_until_delete_confirmation() -> None:
    connection = _Connection(
        rows=[
            _row(revision=2),
            _row(revision=3, state="revoked"),
            [
                _cleanup_claim_row(
                    source=CLEANUP_SOURCE_TERMINAL,
                    secret_ref="vault://resident/card-1",
                    fingerprint="a" * 64,
                    metadata_revision=3,
                )
            ],
            _row(
                revision=4,
                state="revoked",
                secret_ref="",
                fingerprint="",
            ),
        ],
        values=["agent_card_1"],
    )
    store = _store(connection)

    retired = await store.retire(
        "agent_card_1",
        expected_revision=2,
        state=HANDLE_STATE_REVOKED,
    )
    assert retired is not None
    assert retired.resident_access_secret_ref == "vault://resident/card-1"

    claims = await store.claim_terminal_secret_cleanup(
        now=2_100,
        candidate=retired,
    )
    assert len(claims) == 1
    acknowledgement = await store.acknowledge_resident_secret_cleanup(claims[0])
    cleared = acknowledgement.metadata
    assert acknowledgement.acknowledged
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
async def test_retired_secret_cleanup_is_claimed_and_token_acknowledged() -> None:
    claim_row = _cleanup_claim_row(
        source=CLEANUP_SOURCE_RETIRED,
        secret_ref="vault://resident/card-1",
        fingerprint="a" * 64,
        card_revision=0,
        created_at=0,
        expires_at=0,
    )
    connection = _Connection(
        rows=[[claim_row]],
        execute_results=["OK", "DELETE 1", "OK", "DELETE 0"],
    )
    store = _store(connection)

    claims = await store.claim_retired_secret_cleanup(now=2_100, limit=3)

    assert claims == [ResidentSecretCleanupClaim(**claim_row).validated()]
    assert (await store.acknowledge_resident_secret_cleanup(claims[0])).acknowledged
    assert not (
        await store.acknowledge_resident_secret_cleanup(claims[0])
    ).acknowledged
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
        await store.ensure_schema()
        candidate = CardHandleMetadata(
            access_id="agent_card_1",
            card_revision=7,
            resident_access_secret_ref="vault://resident/card-1",
            resident_access_sha256="b" * 64,
            session_id="session-1",
            expires_at=4_000_000_000,
        )
        candidate_intent = PreparedResidentSecret(
            access_id=candidate.access_id,
            secret_ref=candidate.resident_access_secret_ref,
            resident_access_sha256=candidate.resident_access_sha256,
            card_revision=candidate.card_revision,
            created_at=1_000,
            expires_at=candidate.expires_at,
        )
        assert await store.prepare_resident_secret(candidate_intent)
        created_result = await store.install_prepared_resident_secret(
            candidate,
            expected_revision=0,
        )
        created = created_result.metadata
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
        other_intent = PreparedResidentSecret(
            access_id=other.access_id,
            secret_ref=other.resident_access_secret_ref,
            resident_access_sha256=other.resident_access_sha256,
            card_revision=other.card_revision,
            created_at=1_000,
            expires_at=other.expires_at,
        )
        assert await store.prepare_resident_secret(other_intent)
        assert (
            await store.install_prepared_resident_secret(
                other,
                expected_revision=0,
            )
        ).metadata.revision == 1
        replacement_intent = PreparedResidentSecret(
            access_id=replacement.access_id,
            secret_ref=replacement.resident_access_secret_ref,
            resident_access_sha256=replacement.resident_access_sha256,
            card_revision=replacement.card_revision,
            created_at=1_100,
            expires_at=replacement.expires_at,
        )
        assert await store.prepare_resident_secret(replacement_intent)
        unsafe_adoption = CardHandleMetadata(
            access_id="other_card_1",
            card_revision=1,
            resident_access_secret_ref="vault://resident/card-1",
            resident_access_sha256="b" * 64,
            session_id="session-other",
            expires_at=4_000_000_000,
        )
        rotation_race = await asyncio.gather(
            store.install_prepared_resident_secret(
                replacement,
                expected_revision=current.revision,
            ),
            store.put(unsafe_adoption, expected_revision=1),
            return_exceptions=True,
        )
        assert isinstance(rotation_race[0], CardHandleMutationResult)
        assert isinstance(rotation_race[1], CardHandleMetadataConflict)
        assert rotation_race[1].reason == (
            "card_handle_resident_secret_mutation_requires_custody_protocol"
        )
        rotation_result = rotation_race[0]
        rotated = rotation_result.metadata
        assert rotated.revision == 3
        assert rotation_result.retired_secret is not None
        assert rotation_result.retired_secret.secret_ref == (
            "vault://resident/card-1"
        )
        assert not await store.prepare_resident_secret(candidate_intent)
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

        terminal_claims = await store.claim_terminal_secret_cleanup(
            now=2_000,
            limit=1,
            candidate=retired,
        )
        assert len(terminal_claims) == 1
        terminal_ack = await store.acknowledge_resident_secret_cleanup(
            terminal_claims[0]
        )
        assert terminal_ack.acknowledged
        cleared = terminal_ack.metadata
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
        retired_claims = await store.claim_retired_secret_cleanup(
            now=2_000,
            limit=1,
            candidate=rotation_result.retired_secret,
        )
        assert len(retired_claims) == 1
        assert (
            await store.acknowledge_resident_secret_cleanup(retired_claims[0])
        ).acknowledged
        assert await store.claim_retired_secret_cleanup(
            now=2_000,
            limit=1,
            candidate=rotation_result.retired_secret,
        ) == []
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

        prepared = PreparedResidentSecret(
            access_id="prepared_card_1",
            secret_ref="vault://resident/prepared-1",
            resident_access_sha256="e" * 64,
            card_revision=1,
            created_at=2_000,
            expires_at=4_000_000_000,
        )
        assert await store.prepare_resident_secret(prepared)
        assert not await store.prepare_resident_secret(prepared)
        prepared_candidate = CardHandleMetadata(
            access_id="prepared_card_1",
            card_revision=1,
            resident_access_secret_ref=prepared.secret_ref,
            resident_access_sha256=prepared.resident_access_sha256,
            session_id="session-prepared",
            expires_at=prepared.expires_at,
        )
        prepared_install = await store.install_prepared_resident_secret(
            prepared_candidate,
            expected_revision=0,
        )
        assert prepared_install.metadata.resident_access_secret_ref == (
            prepared.secret_ref
        )
        assert prepared_install.retired_secret is None
        assert await store.claim_prepared_secret_cleanup(
            now=2_000,
            limit=1,
            candidate=prepared,
        ) == []

        replacement_intent = PreparedResidentSecret(
            access_id="prepared_card_1",
            secret_ref="vault://resident/prepared-2",
            resident_access_sha256="f" * 64,
            card_revision=2,
            created_at=3_000,
            expires_at=4_000_000_100,
        )
        assert await store.prepare_resident_secret(replacement_intent)
        replacement_candidate = CardHandleMetadata(
            access_id="prepared_card_1",
            card_revision=2,
            resident_access_secret_ref=replacement_intent.secret_ref,
            resident_access_sha256=replacement_intent.resident_access_sha256,
            session_id="session-prepared-2",
            expires_at=replacement_intent.expires_at,
        )
        replacement_install = await store.install_prepared_resident_secret(
            replacement_candidate,
            expected_revision=prepared_install.metadata.revision,
        )
        assert replacement_install.retired_secret is not None
        assert replacement_install.retired_secret.secret_ref == prepared.secret_ref

        cleanup_intent = PreparedResidentSecret(
            access_id="cleanup_card_1",
            secret_ref="vault://resident/prepared-cleanup",
            resident_access_sha256="1" * 64,
            card_revision=1,
            created_at=4_000,
            expires_at=4_000_000_200,
        )
        assert await store.prepare_resident_secret(cleanup_intent)
        cleanup_claims = await store.claim_prepared_secret_cleanup(
            now=5_000,
            limit=1,
            candidate=cleanup_intent,
        )
        assert len(cleanup_claims) == 1
        assert cleanup_claims[0].source == CLEANUP_SOURCE_PREPARED
        cleanup_candidate = CardHandleMetadata(
            access_id=cleanup_intent.access_id,
            card_revision=cleanup_intent.card_revision,
            resident_access_secret_ref=cleanup_intent.secret_ref,
            resident_access_sha256=cleanup_intent.resident_access_sha256,
            session_id="session-cleanup",
            expires_at=cleanup_intent.expires_at,
        )
        with pytest.raises(CardHandleMetadataConflict) as cleanup_started:
            await store.install_prepared_resident_secret(
                cleanup_candidate,
                expected_revision=0,
            )
        assert (
            cleanup_started.value.reason
            == "card_handle_prepared_secret_cleanup_started"
        )
        cleanup_ack = await store.acknowledge_resident_secret_cleanup(
            cleanup_claims[0]
        )
        assert cleanup_ack.acknowledged
        assert cleanup_ack.metadata is None
        assert await store.claim_prepared_secret_cleanup(
            now=5_000,
            limit=1,
            candidate=cleanup_intent,
        ) == []

        terminal_race_old_ref = "vault://resident/terminal-race-a-old"
        terminal_race_new_ref = "vault://resident/terminal-race-z-new"
        terminal_race_initial = CardHandleMetadata(
            access_id="terminal_race_card_1",
            card_revision=1,
            resident_access_secret_ref=terminal_race_old_ref,
            resident_access_sha256="3" * 64,
            session_id="session-terminal-race-old",
            expires_at=4_000_000_300,
        )
        terminal_race_initial_intent = PreparedResidentSecret(
            access_id=terminal_race_initial.access_id,
            secret_ref=terminal_race_old_ref,
            resident_access_sha256=terminal_race_initial.resident_access_sha256,
            card_revision=terminal_race_initial.card_revision,
            created_at=5_000,
            expires_at=terminal_race_initial.expires_at,
        )
        assert await store.prepare_resident_secret(terminal_race_initial_intent)
        terminal_race_created = await store.install_prepared_resident_secret(
            terminal_race_initial,
            expected_revision=0,
        )
        terminal_race_retired = await store.retire(
            terminal_race_initial.access_id,
            expected_revision=terminal_race_created.metadata.revision,
            state=HANDLE_STATE_REVOKED,
        )
        assert terminal_race_retired is not None
        terminal_race_claims = await store.claim_terminal_secret_cleanup(
            now=6_000,
            limit=1,
            candidate=terminal_race_retired,
        )
        assert len(terminal_race_claims) == 1
        terminal_race_replacement_intent = PreparedResidentSecret(
            access_id=terminal_race_initial.access_id,
            secret_ref=terminal_race_new_ref,
            resident_access_sha256="4" * 64,
            card_revision=2,
            created_at=5_100,
            expires_at=4_000_000_400,
        )
        assert await store.prepare_resident_secret(
            terminal_race_replacement_intent
        )
        terminal_race_replacement = CardHandleMetadata(
            access_id=terminal_race_initial.access_id,
            card_revision=terminal_race_replacement_intent.card_revision,
            resident_access_secret_ref=terminal_race_new_ref,
            resident_access_sha256=(
                terminal_race_replacement_intent.resident_access_sha256
            ),
            session_id="session-terminal-race-new",
            expires_at=terminal_race_replacement_intent.expires_at,
        )

        blocker = await asyncpg.connect(dsn)
        install_task: asyncio.Task[CardHandleMutationResult] | None = None
        acknowledge_task: asyncio.Task[
            ResidentSecretCleanupAcknowledgement
        ] | None = None
        lock_held = False
        try:
            await blocker.execute(
                "SELECT pg_advisory_lock(hashtextextended($1, $2))",
                terminal_race_new_ref,
                0,
            )
            lock_held = True
            install_task = asyncio.create_task(
                store.install_prepared_resident_secret(
                    terminal_race_replacement,
                    expected_revision=terminal_race_retired.revision,
                )
            )
            for _attempt in range(200):
                install_waiting = bool(
                    await blocker.fetchval(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM pg_stat_activity
                            WHERE datname = current_database()
                              AND pid <> pg_backend_pid()
                              AND wait_event = 'advisory'
                        )
                        """
                    )
                )
                if install_waiting:
                    break
                await asyncio.sleep(0.01)
            assert install_waiting
            acknowledge_task = asyncio.create_task(
                store.acknowledge_resident_secret_cleanup(
                    terminal_race_claims[0]
                )
            )
            await asyncio.sleep(0.05)
            assert await blocker.fetchval(
                "SELECT pg_advisory_unlock(hashtextextended($1, $2))",
                terminal_race_new_ref,
                0,
            )
            lock_held = False
            terminal_race_install, terminal_race_ack = await asyncio.wait_for(
                asyncio.gather(install_task, acknowledge_task),
                timeout=5,
            )
        finally:
            if lock_held:
                await blocker.execute(
                    "SELECT pg_advisory_unlock(hashtextextended($1, $2))",
                    terminal_race_new_ref,
                    0,
                )
            await blocker.close()
            pending = [
                task
                for task in (install_task, acknowledge_task)
                if task is not None and not task.done()
            ]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        assert terminal_race_install.metadata.resident_access_secret_ref == (
            terminal_race_new_ref
        )
        assert not terminal_race_ack.acknowledged
        assert await store.read_current(terminal_race_initial.access_id) == (
            terminal_race_install.metadata
        )

        race_intent = PreparedResidentSecret(
            access_id="race_card_1",
            secret_ref="vault://resident/prepared-race",
            resident_access_sha256="2" * 64,
            card_revision=1,
            created_at=5_000,
            expires_at=4_000_000_300,
        )
        assert await store.prepare_resident_secret(race_intent)
        race_candidate = CardHandleMetadata(
            access_id=race_intent.access_id,
            card_revision=race_intent.card_revision,
            resident_access_secret_ref=race_intent.secret_ref,
            resident_access_sha256=race_intent.resident_access_sha256,
            session_id="session-race",
            expires_at=race_intent.expires_at,
        )
        install_outcome, claim_outcome = await asyncio.gather(
            store.install_prepared_resident_secret(
                race_candidate,
                expected_revision=0,
            ),
            store.claim_prepared_secret_cleanup(
                now=6_000,
                limit=1,
                candidate=race_intent,
            ),
            return_exceptions=True,
        )
        if isinstance(install_outcome, CardHandleMutationResult):
            assert claim_outcome == []
        else:
            assert isinstance(install_outcome, CardHandleMetadataConflict)
            assert (
                install_outcome.reason
                == "card_handle_prepared_secret_cleanup_started"
            )
            assert isinstance(claim_outcome, list)
            assert len(claim_outcome) == 1
            race_ack = await store.acknowledge_resident_secret_cleanup(
                claim_outcome[0]
            )
            assert race_ack.acknowledged
    finally:
        async with pool.acquire() as connection:
            await connection.execute(f"DROP SCHEMA IF EXISTS {store.schema} CASCADE")
        await pool.close()


def test_table_name_is_stable() -> None:
    assert TABLE_CARD_HANDLE_METADATA == "connection_hub_card_handle_metadata"
    assert (
        TABLE_PREPARED_RESIDENT_SECRETS
        == "connection_hub_card_prepared_resident_secrets"
    )
    assert (
        TABLE_RETIRED_RESIDENT_SECRETS == "connection_hub_card_retired_resident_secrets"
    )

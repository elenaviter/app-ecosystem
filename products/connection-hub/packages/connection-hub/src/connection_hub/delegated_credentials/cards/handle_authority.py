# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Durable, non-secret metadata for delegated-Card credential handles.

Bearer values never enter this module. A resident-agent bearer lives in host
secret custody and is named here by an opaque reference plus SHA-256
fingerprint. OAuth credentials remain in their credential-family and
access-binding tables, linked by ``registry_access_id``. Session ids are
durable identifiers and are validated by the host session authority.
"""

from __future__ import annotations

import time
from typing import Any, Mapping

from connection_hub.delegated_credentials.cards.handle_metadata import (
    CardHandleMetadata,
    CardHandleMetadataConflict,
    CardHandleMetadataStore,
    HANDLE_STATE_ACTIVE,
    HANDLE_STATE_EXPIRED,
    HANDLE_STATE_REVOKED,
    HANDLE_STATES,
    RetiredResidentSecret,
)
from connection_hub.delegated_credentials.cards.handle_schema import (
    TABLE_CARD_HANDLE_METADATA,
    TABLE_RETIRED_RESIDENT_SECRETS,
    card_handle_schema,
    card_handle_schema_sql,
)
from connection_hub.delegated_credentials.cards.store import validated_access_id


class PostgresCardHandleMetadataStore:
    """Transactional PostgreSQL authority for minimized Card handles."""

    def __init__(self, *, pg_pool: Any, tenant: str, project: str) -> None:
        if pg_pool is None:
            raise RuntimeError("PostgresCardHandleMetadataStore requires pg_pool")
        self._pool = pg_pool
        self.tenant = str(tenant or "").strip() or "default"
        self.project = str(project or "").strip() or "default"
        self.schema = card_handle_schema(tenant=self.tenant, project=self.project)

    async def ensure_schema(self) -> None:
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(card_handle_schema_sql(self.schema))

    @property
    def _columns(self) -> str:
        return """
            access_id,
            card_revision,
            resident_access_secret_ref,
            resident_access_sha256,
            session_id,
            state,
            revision,
            EXTRACT(EPOCH FROM created_at)::bigint AS created_at,
            EXTRACT(EPOCH FROM updated_at)::bigint AS updated_at,
            COALESCE(EXTRACT(EPOCH FROM retired_at)::bigint, 0) AS retired_at,
            EXTRACT(EPOCH FROM expires_at)::bigint AS expires_at
        """

    def _record(self, row: Mapping[str, Any] | None) -> CardHandleMetadata | None:
        if row is None:
            return None
        return CardHandleMetadata(
            access_id=str(row.get("access_id") or ""),
            card_revision=int(row.get("card_revision") or 0),
            expires_at=int(row.get("expires_at") or 0),
            resident_access_secret_ref=str(
                row.get("resident_access_secret_ref") or ""
            ),
            resident_access_sha256=str(row.get("resident_access_sha256") or ""),
            session_id=str(row.get("session_id") or ""),
            state=str(row.get("state") or ""),
            revision=int(row.get("revision") or 0),
            created_at=int(row.get("created_at") or 0),
            updated_at=int(row.get("updated_at") or 0),
            retired_at=int(row.get("retired_at") or 0),
        ).validated()

    @staticmethod
    def _retired_secret(
        row: Mapping[str, Any] | None,
    ) -> RetiredResidentSecret | None:
        if row is None:
            return None
        return RetiredResidentSecret(
            access_id=str(row.get("access_id") or ""),
            secret_ref=str(row.get("secret_ref") or ""),
            resident_access_sha256=str(row.get("resident_access_sha256") or ""),
            retired_at=int(row.get("retired_at") or 0),
        ).validated()

    async def _locked(
        self, connection: Any, access_id: str
    ) -> CardHandleMetadata | None:
        row = await connection.fetchrow(
            f"""
            SELECT {self._columns}
            FROM {self.schema}.{TABLE_CARD_HANDLE_METADATA}
            WHERE access_id = $1
            FOR UPDATE
            """,
            validated_access_id(access_id),
        )
        return self._record(row)

    async def _lock_secret_refs(self, connection: Any, *secret_refs: str) -> None:
        references = sorted(
            {
                str(value or "").strip()
                for value in secret_refs
                if str(value or "").strip()
            }
        )
        for reference in references:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, $2))",
                reference,
                0,
            )

    async def _require_secret_ref_available(
        self,
        connection: Any,
        *,
        access_id: str,
        secret_ref: str,
        expected_revision: int,
        current_revision: int,
    ) -> None:
        conflict = await connection.fetchrow(
            f"""
            SELECT 'pending_cleanup' AS conflict_kind, access_id
            FROM {self.schema}.{TABLE_RETIRED_RESIDENT_SECRETS}
            WHERE secret_ref = $1
            UNION ALL
            SELECT 'already_current' AS conflict_kind, access_id
            FROM {self.schema}.{TABLE_CARD_HANDLE_METADATA}
            WHERE resident_access_secret_ref = $1
              AND access_id <> $2
            LIMIT 1
            """,
            secret_ref,
            access_id,
        )
        if conflict is None:
            return
        reason = (
            "card_handle_secret_ref_pending_cleanup"
            if str(conflict.get("conflict_kind") or "") == "pending_cleanup"
            else "card_handle_secret_ref_already_current"
        )
        raise self._conflict(
            reason,
            expected_revision=expected_revision,
            current_revision=current_revision,
        )

    @staticmethod
    def _conflict(
        reason: str, *, expected_revision: int, current_revision: int
    ) -> CardHandleMetadataConflict:
        return CardHandleMetadataConflict(
            reason,
            expected_revision=expected_revision,
            current_revision=current_revision,
        )

    async def read_current(self, access_id: str) -> CardHandleMetadata | None:
        row = await self._pool.fetchrow(
            f"""
            SELECT {self._columns}
            FROM {self.schema}.{TABLE_CARD_HANDLE_METADATA}
            WHERE access_id = $1
            """,
            validated_access_id(access_id),
        )
        return self._record(row)

    async def read_active(
        self, access_id: str, *, now: int | None = None
    ) -> CardHandleMetadata | None:
        moment = int(now if now is not None else time.time())
        row = await self._pool.fetchrow(
            f"""
            SELECT {self._columns}
            FROM {self.schema}.{TABLE_CARD_HANDLE_METADATA}
            WHERE access_id = $1
              AND state = 'active'
              AND expires_at > to_timestamp($2)
            """,
            validated_access_id(access_id),
            moment,
        )
        return self._record(row)

    async def put(
        self,
        metadata: CardHandleMetadata,
        *,
        expected_revision: int,
    ) -> CardHandleMetadata:
        candidate = metadata.validated()
        expected = int(expected_revision)
        if expected < 0:
            raise ValueError("expected Card-handle revision must not be negative")
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                current = await self._locked(connection, candidate.access_id)
                if current is None:
                    if expected != 0:
                        raise self._conflict(
                            "card_handle_revision_conflict",
                            expected_revision=expected,
                            current_revision=0,
                        )
                    if candidate.resident_access_secret_ref:
                        await self._lock_secret_refs(
                            connection, candidate.resident_access_secret_ref
                        )
                        await self._require_secret_ref_available(
                            connection,
                            access_id=candidate.access_id,
                            secret_ref=candidate.resident_access_secret_ref,
                            expected_revision=expected,
                            current_revision=0,
                        )
                    row = await connection.fetchrow(
                        f"""
                        INSERT INTO {self.schema}.{TABLE_CARD_HANDLE_METADATA} (
                            access_id, tenant, project, card_revision,
                            resident_access_secret_ref, resident_access_sha256,
                            session_id, state, retired_at, expires_at
                        ) VALUES (
                            $1, $2, $3, $4,
                            $5, $6,
                            $7, $8,
                            CASE WHEN $8 = 'active' THEN NULL ELSE now() END,
                            to_timestamp($9)
                        )
                        RETURNING {self._columns}
                        """,
                        candidate.access_id,
                        self.tenant,
                        self.project,
                        candidate.card_revision,
                        candidate.resident_access_secret_ref,
                        candidate.resident_access_sha256,
                        candidate.session_id,
                        candidate.state,
                        candidate.expires_at,
                    )
                    saved = self._record(row)
                    assert saved is not None
                    return saved
                if current.revision != expected:
                    raise self._conflict(
                        "card_handle_revision_conflict",
                        expected_revision=expected,
                        current_revision=current.revision,
                    )
                if current.payload_identity == candidate.payload_identity:
                    return current
                if candidate.card_revision < current.card_revision:
                    raise self._conflict(
                        "card_handle_card_revision_regressed",
                        expected_revision=expected,
                        current_revision=current.revision,
                    )
                if (
                    current.state != HANDLE_STATE_ACTIVE
                    and candidate.state == HANDLE_STATE_ACTIVE
                    and candidate.card_revision <= current.card_revision
                ):
                    raise self._conflict(
                        "card_handle_reactivation_requires_new_card_revision",
                        expected_revision=expected,
                        current_revision=current.revision,
                    )
                if (
                    current.resident_access_secret_ref
                    and candidate.resident_access_secret_ref
                    == current.resident_access_secret_ref
                    and candidate.resident_access_sha256
                    != current.resident_access_sha256
                ):
                    raise self._conflict(
                        "card_handle_secret_fingerprint_changed_without_new_ref",
                        expected_revision=expected,
                        current_revision=current.revision,
                    )
                secret_replaced = bool(
                    current.resident_access_secret_ref
                    and candidate.resident_access_secret_ref
                    != current.resident_access_secret_ref
                )
                candidate_ref_changed = bool(
                    candidate.resident_access_secret_ref
                    and candidate.resident_access_secret_ref
                    != current.resident_access_secret_ref
                )
                if secret_replaced or candidate_ref_changed:
                    await self._lock_secret_refs(
                        connection,
                        current.resident_access_secret_ref,
                        candidate.resident_access_secret_ref,
                    )
                if candidate_ref_changed:
                    await self._require_secret_ref_available(
                        connection,
                        access_id=candidate.access_id,
                        secret_ref=candidate.resident_access_secret_ref,
                        expected_revision=expected,
                        current_revision=current.revision,
                    )
                row = await connection.fetchrow(
                    f"""
                    UPDATE {self.schema}.{TABLE_CARD_HANDLE_METADATA}
                    SET card_revision = $2,
                        resident_access_secret_ref = $3,
                        resident_access_sha256 = $4,
                        session_id = $5,
                        state = $6,
                        revision = revision + 1,
                        updated_at = now(),
                        retired_at = CASE
                            WHEN $6 = 'active' THEN NULL
                            WHEN state = 'active' THEN now()
                            ELSE retired_at
                        END,
                        expires_at = to_timestamp($7)
                    WHERE access_id = $1
                    RETURNING {self._columns}
                    """,
                    candidate.access_id,
                    candidate.card_revision,
                    candidate.resident_access_secret_ref,
                    candidate.resident_access_sha256,
                    candidate.session_id,
                    candidate.state,
                    candidate.expires_at,
                )
                if secret_replaced:
                    retired_row = await connection.fetchrow(
                        f"""
                        INSERT INTO {self.schema}.{TABLE_RETIRED_RESIDENT_SECRETS} (
                            secret_ref, access_id, resident_access_sha256, retired_at
                        )
                        SELECT $1, $2, $3, now()
                        WHERE NOT EXISTS (
                            SELECT 1
                            FROM {self.schema}.{TABLE_CARD_HANDLE_METADATA}
                            WHERE resident_access_secret_ref = $1
                        )
                        RETURNING secret_ref
                        """,
                        current.resident_access_secret_ref,
                        current.access_id,
                        current.resident_access_sha256,
                    )
                    if retired_row is None:
                        raise self._conflict(
                            "card_handle_secret_ref_still_current",
                            expected_revision=expected,
                            current_revision=current.revision,
                        )
                saved = self._record(row)
                assert saved is not None
                return saved

    async def retire(
        self,
        access_id: str,
        *,
        expected_revision: int,
        state: str,
    ) -> CardHandleMetadata | None:
        target = str(state or "").strip().lower()
        if target not in {HANDLE_STATE_REVOKED, HANDLE_STATE_EXPIRED}:
            raise ValueError("Card-handle retirement state must be revoked or expired")
        expected = int(expected_revision)
        if expected < 0:
            raise ValueError("expected Card-handle revision must not be negative")
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                current = await self._locked(connection, access_id)
                if current is None:
                    if expected != 0:
                        raise self._conflict(
                            "card_handle_revision_conflict",
                            expected_revision=expected,
                            current_revision=0,
                        )
                    return None
                if current.revision != expected:
                    raise self._conflict(
                        "card_handle_revision_conflict",
                        expected_revision=expected,
                        current_revision=current.revision,
                    )
                if current.state == target:
                    return current
                if current.state != HANDLE_STATE_ACTIVE:
                    raise self._conflict(
                        "card_handle_terminal_state_conflict",
                        expected_revision=expected,
                        current_revision=current.revision,
                    )
                row = await connection.fetchrow(
                    f"""
                    UPDATE {self.schema}.{TABLE_CARD_HANDLE_METADATA}
                    SET state = $2,
                        revision = revision + 1,
                        updated_at = now(),
                        retired_at = now()
                    WHERE access_id = $1
                    RETURNING {self._columns}
                    """,
                    current.access_id,
                    target,
                )
                saved = self._record(row)
                assert saved is not None
                return saved

    async def clear_retired_resident_secret(
        self,
        access_id: str,
        *,
        expected_revision: int,
    ) -> CardHandleMetadata | None:
        expected = int(expected_revision)
        if expected < 0:
            raise ValueError("expected Card-handle revision must not be negative")
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                current = await self._locked(connection, access_id)
                if current is None:
                    if expected != 0:
                        raise self._conflict(
                            "card_handle_revision_conflict",
                            expected_revision=expected,
                            current_revision=0,
                        )
                    return None
                if current.revision != expected:
                    raise self._conflict(
                        "card_handle_revision_conflict",
                        expected_revision=expected,
                        current_revision=current.revision,
                    )
                if current.state == HANDLE_STATE_ACTIVE:
                    raise ValueError(
                        "an active resident secret reference cannot be cleared"
                    )
                if not current.resident_access_secret_ref:
                    return current
                row = await connection.fetchrow(
                    f"""
                    UPDATE {self.schema}.{TABLE_CARD_HANDLE_METADATA}
                    SET resident_access_secret_ref = '',
                        resident_access_sha256 = '',
                        revision = revision + 1,
                        updated_at = now()
                    WHERE access_id = $1
                    RETURNING {self._columns}
                    """,
                    current.access_id,
                )
                saved = self._record(row)
                assert saved is not None
                return saved

    async def expire_due(
        self,
        *,
        now: int | None = None,
        limit: int = 100,
    ) -> list[CardHandleMetadata]:
        """Move a bounded batch of elapsed rows into terminal cleanup state."""

        bounded = int(limit)
        if bounded < 1 or bounded > 10_000:
            raise ValueError("Card-handle expiry limit is invalid")
        moment = int(now if now is not None else time.time())
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                rows = await connection.fetch(
                    f"""
                    WITH victims AS (
                        SELECT access_id
                        FROM {self.schema}.{TABLE_CARD_HANDLE_METADATA}
                        WHERE state = 'active'
                          AND expires_at <= to_timestamp($1)
                        ORDER BY expires_at, access_id
                        LIMIT $2
                        FOR UPDATE SKIP LOCKED
                    )
                    UPDATE {self.schema}.{TABLE_CARD_HANDLE_METADATA} AS metadata
                    SET state = 'expired',
                        revision = metadata.revision + 1,
                        updated_at = now(),
                        retired_at = now()
                    FROM victims
                    WHERE metadata.access_id = victims.access_id
                    RETURNING
                        metadata.access_id,
                        metadata.card_revision,
                        metadata.resident_access_secret_ref,
                        metadata.resident_access_sha256,
                        metadata.session_id,
                        metadata.state,
                        metadata.revision,
                        EXTRACT(EPOCH FROM metadata.created_at)::bigint AS created_at,
                        EXTRACT(EPOCH FROM metadata.updated_at)::bigint AS updated_at,
                        EXTRACT(EPOCH FROM metadata.retired_at)::bigint AS retired_at,
                        EXTRACT(EPOCH FROM metadata.expires_at)::bigint AS expires_at
                    """,
                    moment,
                    bounded,
                )
        return [record for row in rows if (record := self._record(row)) is not None]

    async def list_secret_cleanup_candidates(
        self,
        *,
        limit: int = 100,
    ) -> list[CardHandleMetadata]:
        bounded = int(limit)
        if bounded < 1 or bounded > 10_000:
            raise ValueError("Card-handle cleanup limit is invalid")
        rows = await self._pool.fetch(
            f"""
            SELECT {self._columns}
            FROM {self.schema}.{TABLE_CARD_HANDLE_METADATA}
            WHERE state <> 'active'
              AND resident_access_secret_ref <> ''
            ORDER BY retired_at, access_id
            LIMIT $1
            """,
            bounded,
        )
        return [record for row in rows if (record := self._record(row)) is not None]

    async def list_retired_secret_cleanup_candidates(
        self,
        *,
        limit: int = 100,
    ) -> list[RetiredResidentSecret]:
        bounded = int(limit)
        if bounded < 1 or bounded > 10_000:
            raise ValueError("retired resident-secret cleanup limit is invalid")
        rows = await self._pool.fetch(
            f"""
            SELECT
                access_id,
                secret_ref,
                resident_access_sha256,
                EXTRACT(EPOCH FROM retired_at)::bigint AS retired_at
            FROM {self.schema}.{TABLE_RETIRED_RESIDENT_SECRETS}
                AS retired_secret
            WHERE NOT EXISTS (
                SELECT 1
                FROM {self.schema}.{TABLE_CARD_HANDLE_METADATA} AS current_metadata
                WHERE current_metadata.resident_access_secret_ref =
                    retired_secret.secret_ref
            )
            ORDER BY retired_at, access_id, secret_ref
            LIMIT $1
            """,
            bounded,
        )
        return [
            record
            for row in rows
            if (record := self._retired_secret(row)) is not None
        ]

    async def delete_retired_secret_record(
        self,
        *,
        access_id: str,
        secret_ref: str,
    ) -> bool:
        card_id = validated_access_id(access_id)
        reference = str(secret_ref or "").strip()
        if not reference:
            raise ValueError("retired resident secret reference is required")
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                result = await connection.execute(
                    f"""
                    DELETE FROM {self.schema}.{TABLE_RETIRED_RESIDENT_SECRETS}
                        AS retired_secret
                    WHERE retired_secret.access_id = $1
                      AND retired_secret.secret_ref = $2
                      AND NOT EXISTS (
                          SELECT 1
                          FROM {self.schema}.{TABLE_CARD_HANDLE_METADATA}
                              AS current_metadata
                          WHERE current_metadata.resident_access_secret_ref =
                              retired_secret.secret_ref
                      )
                    """,
                    card_id,
                    reference,
                )
        return str(result or "").strip().endswith(" 1")

    async def purge_terminal(
        self,
        *,
        retired_before: int,
        limit: int = 1000,
    ) -> int:
        bounded = int(limit)
        if bounded < 1 or bounded > 10_000:
            raise ValueError("Card-handle retention limit is invalid")
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                removed = await connection.fetchval(
                    f"""
                    WITH victims AS (
                        SELECT candidate.access_id
                        FROM {self.schema}.{TABLE_CARD_HANDLE_METADATA} AS candidate
                        WHERE candidate.state <> 'active'
                          AND candidate.resident_access_secret_ref = ''
                          AND candidate.retired_at <= to_timestamp($1)
                          AND NOT EXISTS (
                              SELECT 1
                              FROM {self.schema}.{TABLE_RETIRED_RESIDENT_SECRETS}
                                  AS retired_secret
                              WHERE retired_secret.access_id =
                                  candidate.access_id
                          )
                        ORDER BY candidate.retired_at, candidate.access_id
                        LIMIT $2
                        FOR UPDATE SKIP LOCKED
                    ), deleted AS (
                        DELETE FROM
                            {self.schema}.{TABLE_CARD_HANDLE_METADATA} AS metadata
                        USING victims
                        WHERE metadata.access_id = victims.access_id
                        RETURNING 1
                    )
                    SELECT count(*) FROM deleted
                    """,
                    int(retired_before),
                    bounded,
                )
        return int(removed or 0)


__all__ = [
    "CardHandleMetadata",
    "CardHandleMetadataConflict",
    "CardHandleMetadataStore",
    "HANDLE_STATE_ACTIVE",
    "HANDLE_STATE_EXPIRED",
    "HANDLE_STATE_REVOKED",
    "HANDLE_STATES",
    "PostgresCardHandleMetadataStore",
    "RetiredResidentSecret",
]

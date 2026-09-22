# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""PostgreSQL authority for durable resident-secret cleanup claims."""

from __future__ import annotations

import secrets
from typing import Any

from connection_hub.delegated_credentials.cards.handle_metadata import (
    CLEANUP_SOURCE_PREPARED,
    CLEANUP_SOURCE_RETIRED,
    CLEANUP_SOURCE_TERMINAL,
    CardHandleMetadata,
    PreparedResidentSecret,
    ResidentSecretCleanupClaim,
    RetiredResidentSecret,
)
from connection_hub.delegated_credentials.cards.handle_records import (
    cleanup_claim_from_row,
)
from connection_hub.delegated_credentials.cards.handle_schema import (
    TABLE_CARD_HANDLE_METADATA,
    TABLE_PREPARED_RESIDENT_SECRETS,
    TABLE_RETIRED_RESIDENT_SECRETS,
)
from connection_hub.delegated_credentials.cards.resident_secret_locking import (
    lock_resident_secret_refs,
)

CLEANUP_CLAIM_SECONDS = 60


def _bounded_limit(limit: int, *, label: str) -> int:
    bounded = int(limit)
    if bounded < 1 or bounded > 10_000:
        raise ValueError(f"{label} cleanup limit is invalid")
    return bounded


class PostgresResidentSecretCleanupClaimAuthority:
    """Lease external secret deletion work before host I/O begins."""

    def __init__(self, *, pg_pool: Any, schema: str) -> None:
        self._pool = pg_pool
        self.schema = str(schema or "").strip()
        if self._pool is None or not self.schema:
            raise RuntimeError("cleanup claim authority requires PostgreSQL scope")

    @staticmethod
    def _claims(rows: list[Any]) -> list[ResidentSecretCleanupClaim]:
        return [
            claim
            for row in rows
            if (claim := cleanup_claim_from_row(row)) is not None
        ]

    async def claim_prepared_secret_cleanup(
        self,
        *,
        now: int,
        limit: int = 100,
        candidate: PreparedResidentSecret | None = None,
    ) -> list[ResidentSecretCleanupClaim]:
        moment = int(now)
        bounded = _bounded_limit(limit, label="prepared resident-secret")
        claim_token = secrets.token_hex(16)
        if candidate is not None:
            intent = candidate.validated()
            async with (
                self._pool.acquire() as connection,
                connection.transaction(),
            ):
                    await lock_resident_secret_refs(connection, intent.secret_ref)
                    rows = await connection.fetch(
                        f"""
                        WITH claimed AS (
                            UPDATE {self.schema}.{TABLE_PREPARED_RESIDENT_SECRETS}
                                AS prepared_secret
                            SET state = 'cleanup',
                                cleanup_attempts = cleanup_attempts + 1,
                                cleanup_claim_token = $7,
                                cleanup_claimed_at = to_timestamp($8),
                                cleanup_claim_expires_at =
                                    to_timestamp($8 + $9)
                            WHERE prepared_secret.secret_ref = $1
                              AND prepared_secret.access_id = $2
                              AND prepared_secret.resident_access_sha256 = $3
                              AND prepared_secret.card_revision = $4
                              AND prepared_secret.envelope_created_at =
                                  to_timestamp($5)
                              AND prepared_secret.expires_at = to_timestamp($6)
                              AND (
                                  prepared_secret.cleanup_claim_token = ''
                                  OR prepared_secret.cleanup_claim_expires_at <=
                                      to_timestamp($8)
                              )
                              AND NOT EXISTS (
                                  SELECT 1
                                  FROM {self.schema}.{TABLE_CARD_HANDLE_METADATA}
                                      AS current_metadata
                                  WHERE current_metadata.resident_access_secret_ref =
                                      prepared_secret.secret_ref
                              )
                            RETURNING prepared_secret.*
                        )
                        SELECT
                            '{CLEANUP_SOURCE_PREPARED}' AS source,
                            access_id,
                            secret_ref,
                            resident_access_sha256,
                            cleanup_claim_token AS claim_token,
                            cleanup_attempts AS attempt,
                            EXTRACT(EPOCH FROM cleanup_claimed_at)::bigint
                                AS claimed_at,
                            EXTRACT(EPOCH FROM cleanup_claim_expires_at)::bigint
                                AS claim_expires_at,
                            card_revision,
                            EXTRACT(EPOCH FROM envelope_created_at)::bigint
                                AS created_at,
                            EXTRACT(EPOCH FROM expires_at)::bigint AS expires_at,
                            0::bigint AS metadata_revision
                        FROM claimed
                        """,
                        intent.secret_ref,
                        intent.access_id,
                        intent.resident_access_sha256,
                        intent.card_revision,
                        intent.created_at,
                        intent.expires_at,
                        claim_token,
                        moment,
                        CLEANUP_CLAIM_SECONDS,
                    )
            return self._claims(rows)
        async with self._pool.acquire() as connection, connection.transaction():
            rows = await connection.fetch(
                f"""
                    WITH candidates AS (
                        SELECT prepared_secret.secret_ref
                        FROM {self.schema}.{TABLE_PREPARED_RESIDENT_SECRETS}
                            AS prepared_secret
                        WHERE prepared_secret.cleanup_next_attempt_at <=
                                to_timestamp($1)
                          AND (
                              prepared_secret.cleanup_claim_token = ''
                              OR prepared_secret.cleanup_claim_expires_at <=
                                  to_timestamp($1)
                          )
                          AND NOT EXISTS (
                              SELECT 1
                              FROM {self.schema}.{TABLE_CARD_HANDLE_METADATA}
                                  AS current_metadata
                              WHERE current_metadata.resident_access_secret_ref =
                                  prepared_secret.secret_ref
                          )
                        ORDER BY
                            prepared_secret.cleanup_next_attempt_at,
                            prepared_secret.prepared_at,
                            prepared_secret.access_id,
                            prepared_secret.secret_ref
                        LIMIT $2
                        FOR UPDATE SKIP LOCKED
                    ), claimed AS (
                        UPDATE {self.schema}.{TABLE_PREPARED_RESIDENT_SECRETS}
                            AS prepared_secret
                        SET state = 'cleanup',
                            cleanup_attempts = cleanup_attempts + 1,
                            cleanup_claim_token = $3,
                            cleanup_claimed_at = to_timestamp($1),
                            cleanup_claim_expires_at = to_timestamp($1 + $4)
                        FROM candidates
                        WHERE prepared_secret.secret_ref = candidates.secret_ref
                        RETURNING prepared_secret.*
                    )
                    SELECT
                        '{CLEANUP_SOURCE_PREPARED}' AS source,
                        access_id,
                        secret_ref,
                        resident_access_sha256,
                        cleanup_claim_token AS claim_token,
                        cleanup_attempts AS attempt,
                        EXTRACT(EPOCH FROM cleanup_claimed_at)::bigint AS claimed_at,
                        EXTRACT(EPOCH FROM cleanup_claim_expires_at)::bigint
                            AS claim_expires_at,
                        card_revision,
                        EXTRACT(EPOCH FROM envelope_created_at)::bigint AS created_at,
                        EXTRACT(EPOCH FROM expires_at)::bigint AS expires_at,
                        0::bigint AS metadata_revision
                    FROM claimed
                    """,
                moment,
                bounded,
                claim_token,
                CLEANUP_CLAIM_SECONDS,
            )
        return self._claims(rows)

    async def claim_retired_secret_cleanup(
        self,
        *,
        now: int,
        limit: int = 100,
        candidate: RetiredResidentSecret | None = None,
    ) -> list[ResidentSecretCleanupClaim]:
        moment = int(now)
        bounded = _bounded_limit(limit, label="retired resident-secret")
        claim_token = secrets.token_hex(16)
        conditions = "" if candidate is None else """
            AND retired_secret.secret_ref = $6
            AND retired_secret.access_id = $7
            AND retired_secret.resident_access_sha256 = $8
        """
        arguments: tuple[Any, ...] = (
            moment,
            bounded if candidate is None else 1,
            claim_token,
            CLEANUP_CLAIM_SECONDS,
            candidate is not None,
        )
        if candidate is not None:
            record = candidate.validated()
            arguments += (
                record.secret_ref,
                record.access_id,
                record.resident_access_sha256,
            )
        async with self._pool.acquire() as connection, connection.transaction():
            rows = await connection.fetch(
                f"""
                    WITH candidates AS (
                        SELECT retired_secret.secret_ref
                        FROM {self.schema}.{TABLE_RETIRED_RESIDENT_SECRETS}
                            AS retired_secret
                        WHERE (
                                retired_secret.cleanup_claim_token = ''
                                OR retired_secret.cleanup_claim_expires_at <=
                                    to_timestamp($1)
                              )
                          AND (
                                retired_secret.cleanup_next_attempt_at <=
                                    to_timestamp($1)
                                OR $5::boolean
                              )
                          {conditions}
                          AND NOT EXISTS (
                              SELECT 1
                              FROM {self.schema}.{TABLE_CARD_HANDLE_METADATA}
                                  AS current_metadata
                              WHERE current_metadata.resident_access_secret_ref =
                                  retired_secret.secret_ref
                          )
                        ORDER BY
                            retired_secret.cleanup_next_attempt_at,
                            retired_secret.retired_at,
                            retired_secret.access_id,
                            retired_secret.secret_ref
                        LIMIT $2
                        FOR UPDATE SKIP LOCKED
                    ), claimed AS (
                        UPDATE {self.schema}.{TABLE_RETIRED_RESIDENT_SECRETS}
                            AS retired_secret
                        SET cleanup_attempts = cleanup_attempts + 1,
                            cleanup_claim_token = $3,
                            cleanup_claimed_at = to_timestamp($1),
                            cleanup_claim_expires_at = to_timestamp($1 + $4)
                        FROM candidates
                        WHERE retired_secret.secret_ref = candidates.secret_ref
                        RETURNING retired_secret.*
                    )
                    SELECT
                        '{CLEANUP_SOURCE_RETIRED}' AS source,
                        access_id,
                        secret_ref,
                        resident_access_sha256,
                        cleanup_claim_token AS claim_token,
                        cleanup_attempts AS attempt,
                        EXTRACT(EPOCH FROM cleanup_claimed_at)::bigint AS claimed_at,
                        EXTRACT(EPOCH FROM cleanup_claim_expires_at)::bigint
                            AS claim_expires_at,
                        0::bigint AS card_revision,
                        0::bigint AS created_at,
                        0::bigint AS expires_at,
                        0::bigint AS metadata_revision
                    FROM claimed
                    """,
                *arguments,
            )
        return self._claims(rows)

    async def claim_terminal_secret_cleanup(
        self,
        *,
        now: int,
        limit: int = 100,
        candidate: CardHandleMetadata | None = None,
    ) -> list[ResidentSecretCleanupClaim]:
        moment = int(now)
        bounded = _bounded_limit(limit, label="terminal resident-secret")
        claim_token = secrets.token_hex(16)
        conditions = "" if candidate is None else """
            AND metadata.access_id = $6
            AND metadata.revision = $7
            AND metadata.resident_access_secret_ref = $8
        """
        arguments: tuple[Any, ...] = (
            moment,
            bounded if candidate is None else 1,
            claim_token,
            CLEANUP_CLAIM_SECONDS,
            candidate is not None,
        )
        if candidate is not None:
            record = candidate.validated()
            arguments += (
                record.access_id,
                record.revision,
                record.resident_access_secret_ref,
            )
        async with self._pool.acquire() as connection, connection.transaction():
            rows = await connection.fetch(
                f"""
                    WITH candidates AS (
                        SELECT metadata.access_id
                        FROM {self.schema}.{TABLE_CARD_HANDLE_METADATA} AS metadata
                        WHERE metadata.state <> 'active'
                          AND metadata.resident_access_secret_ref <> ''
                          AND (
                                metadata.cleanup_claim_token = ''
                                OR metadata.cleanup_claim_expires_at <=
                                    to_timestamp($1)
                              )
                          AND (
                                metadata.cleanup_next_attempt_at <= to_timestamp($1)
                                OR $5::boolean
                              )
                          {conditions}
                        ORDER BY
                            metadata.cleanup_next_attempt_at,
                            metadata.retired_at,
                            metadata.access_id
                        LIMIT $2
                        FOR UPDATE SKIP LOCKED
                    ), claimed AS (
                        UPDATE {self.schema}.{TABLE_CARD_HANDLE_METADATA} AS metadata
                        SET cleanup_attempts = cleanup_attempts + 1,
                            cleanup_claim_token = $3,
                            cleanup_claimed_at = to_timestamp($1),
                            cleanup_claim_expires_at = to_timestamp($1 + $4)
                        FROM candidates
                        WHERE metadata.access_id = candidates.access_id
                        RETURNING metadata.*
                    )
                    SELECT
                        '{CLEANUP_SOURCE_TERMINAL}' AS source,
                        access_id,
                        resident_access_secret_ref AS secret_ref,
                        resident_access_sha256,
                        cleanup_claim_token AS claim_token,
                        cleanup_attempts AS attempt,
                        EXTRACT(EPOCH FROM cleanup_claimed_at)::bigint AS claimed_at,
                        EXTRACT(EPOCH FROM cleanup_claim_expires_at)::bigint
                            AS claim_expires_at,
                        card_revision,
                        0::bigint AS created_at,
                        EXTRACT(EPOCH FROM expires_at)::bigint AS expires_at,
                        revision AS metadata_revision
                    FROM claimed
                    """,
                *arguments,
            )
        return self._claims(rows)

__all__ = [
    "CLEANUP_CLAIM_SECONDS",
    "PostgresResidentSecretCleanupClaimAuthority",
]

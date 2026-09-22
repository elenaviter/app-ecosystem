# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""PostgreSQL settlement for claimed resident-secret cleanup work."""

from __future__ import annotations

from typing import Any

from connection_hub.delegated_credentials.cards.handle_metadata import (
    CLEANUP_SOURCE_PREPARED,
    CLEANUP_SOURCE_RETIRED,
    CLEANUP_SOURCE_TERMINAL,
    ResidentSecretCleanupAcknowledgement,
    ResidentSecretCleanupClaim,
)
from connection_hub.delegated_credentials.cards.handle_records import (
    CARD_HANDLE_COLUMNS,
    card_handle_from_row,
)
from connection_hub.delegated_credentials.cards.handle_schema import (
    TABLE_CARD_HANDLE_METADATA,
    TABLE_PREPARED_RESIDENT_SECRETS,
    TABLE_RETIRED_RESIDENT_SECRETS,
)
from connection_hub.delegated_credentials.cards.resident_secret_locking import (
    lock_resident_secret_refs,
)

CLEANUP_RETRY_BASE_SECONDS = 5
CLEANUP_RETRY_MAX_SECONDS = 3_600


def _retry_delay(attempt: int) -> int:
    exponent = min(max(0, int(attempt) - 1), 10)
    return min(
        CLEANUP_RETRY_MAX_SECONDS,
        CLEANUP_RETRY_BASE_SECONDS * (2**exponent),
    )


class PostgresResidentSecretCleanupSettlementAuthority:
    """Acknowledge or durably reschedule one fenced cleanup claim."""

    def __init__(self, *, pg_pool: Any, schema: str) -> None:
        self._pool = pg_pool
        self.schema = str(schema or "").strip()
        if self._pool is None or not self.schema:
            raise RuntimeError("cleanup settlement authority requires PostgreSQL scope")

    async def acknowledge_resident_secret_cleanup(
        self,
        claim: ResidentSecretCleanupClaim,
    ) -> ResidentSecretCleanupAcknowledgement:
        item = claim.validated()
        async with self._pool.acquire() as connection, connection.transaction():
            if item.source == CLEANUP_SOURCE_PREPARED:
                await lock_resident_secret_refs(connection, item.secret_ref)
                result = await connection.execute(
                    f"""
                        DELETE FROM {self.schema}.{TABLE_PREPARED_RESIDENT_SECRETS}
                            AS prepared_secret
                        WHERE prepared_secret.secret_ref = $1
                          AND prepared_secret.access_id = $2
                          AND prepared_secret.cleanup_claim_token = $3
                          AND prepared_secret.state = 'cleanup'
                          AND NOT EXISTS (
                              SELECT 1
                              FROM {self.schema}.{TABLE_CARD_HANDLE_METADATA}
                                  AS current_metadata
                              WHERE current_metadata.resident_access_secret_ref =
                                  prepared_secret.secret_ref
                          )
                        """,
                    item.secret_ref,
                    item.access_id,
                    item.claim_token,
                )
                return ResidentSecretCleanupAcknowledgement(
                    acknowledged=str(result or "").strip().endswith(" 1")
                )
            if item.source == CLEANUP_SOURCE_RETIRED:
                await lock_resident_secret_refs(connection, item.secret_ref)
                result = await connection.execute(
                    f"""
                        DELETE FROM {self.schema}.{TABLE_RETIRED_RESIDENT_SECRETS}
                            AS retired_secret
                        WHERE retired_secret.secret_ref = $1
                          AND retired_secret.access_id = $2
                          AND retired_secret.cleanup_claim_token = $3
                          AND NOT EXISTS (
                              SELECT 1
                              FROM {self.schema}.{TABLE_CARD_HANDLE_METADATA}
                                  AS current_metadata
                              WHERE current_metadata.resident_access_secret_ref =
                                  retired_secret.secret_ref
                          )
                        """,
                    item.secret_ref,
                    item.access_id,
                    item.claim_token,
                )
                return ResidentSecretCleanupAcknowledgement(
                    acknowledged=str(result or "").strip().endswith(" 1")
                )
            locked_access_id = await connection.fetchval(
                f"""
                SELECT access_id
                FROM {self.schema}.{TABLE_CARD_HANDLE_METADATA}
                WHERE access_id = $1
                FOR UPDATE
                """,
                item.access_id,
            )
            if locked_access_id is None:
                return ResidentSecretCleanupAcknowledgement(acknowledged=False)
            await lock_resident_secret_refs(connection, item.secret_ref)
            row = await connection.fetchrow(
                f"""
                    UPDATE {self.schema}.{TABLE_CARD_HANDLE_METADATA}
                    SET resident_access_secret_ref = '',
                        resident_access_sha256 = '',
                        revision = revision + 1,
                        updated_at = now(),
                        cleanup_claim_token = '',
                        cleanup_claimed_at = NULL,
                        cleanup_claim_expires_at = NULL,
                        cleanup_last_error = ''
                    WHERE access_id = $1
                      AND revision = $2
                      AND state <> 'active'
                      AND resident_access_secret_ref = $3
                      AND cleanup_claim_token = $4
                    RETURNING {CARD_HANDLE_COLUMNS}
                    """,
                item.access_id,
                item.metadata_revision,
                item.secret_ref,
                item.claim_token,
            )
            metadata = card_handle_from_row(row)
            return ResidentSecretCleanupAcknowledgement(
                acknowledged=metadata is not None,
                metadata=metadata,
            )

    async def defer_resident_secret_cleanup(
        self,
        claim: ResidentSecretCleanupClaim,
        *,
        now: int,
        reason: str,
    ) -> bool:
        item = claim.validated()
        retry_at = int(now) + _retry_delay(item.attempt)
        failure = str(reason or "").strip() or "resident_secret_cleanup_failed"
        failure = failure[:512]
        table = {
            CLEANUP_SOURCE_PREPARED: TABLE_PREPARED_RESIDENT_SECRETS,
            CLEANUP_SOURCE_RETIRED: TABLE_RETIRED_RESIDENT_SECRETS,
            CLEANUP_SOURCE_TERMINAL: TABLE_CARD_HANDLE_METADATA,
        }[item.source]
        identity_column = (
            "access_id"
            if item.source == CLEANUP_SOURCE_TERMINAL
            else "secret_ref"
        )
        identity_value = (
            item.access_id
            if item.source == CLEANUP_SOURCE_TERMINAL
            else item.secret_ref
        )
        result = await self._pool.execute(
            f"""
            UPDATE {self.schema}.{table}
            SET cleanup_claim_token = '',
                cleanup_claimed_at = NULL,
                cleanup_claim_expires_at = NULL,
                cleanup_next_attempt_at = to_timestamp($3),
                cleanup_last_error = $4
            WHERE {identity_column} = $1
              AND cleanup_claim_token = $2
            """,
            identity_value,
            item.claim_token,
            retry_at,
            failure,
        )
        return str(result or "").strip().endswith(" 1")


__all__ = [
    "CLEANUP_RETRY_BASE_SECONDS",
    "CLEANUP_RETRY_MAX_SECONDS",
    "PostgresResidentSecretCleanupSettlementAuthority",
]

# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Retirement, expiry, cleanup listing, and retention for Card handles."""

from __future__ import annotations

import time

from connection_hub.delegated_credentials.cards.handle_metadata import (
    HANDLE_STATE_ACTIVE,
    HANDLE_STATE_EXPIRED,
    HANDLE_STATE_REVOKED,
    CardHandleMetadata,
)
from connection_hub.delegated_credentials.cards.handle_record_authority import (
    PostgresCardHandleRecordAuthority,
)
from connection_hub.delegated_credentials.cards.handle_records import (
    CARD_HANDLE_COLUMNS,
    card_handle_columns,
)
from connection_hub.delegated_credentials.cards.handle_schema import (
    TABLE_CARD_HANDLE_METADATA,
    TABLE_RETIRED_RESIDENT_SECRETS,
)


class PostgresCardHandleLifecycleAuthority:
    """Move current handles through terminal cleanup and retention."""

    def __init__(self, *, records: PostgresCardHandleRecordAuthority) -> None:
        self._records = records

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
        async with (
            self._records.pool.acquire() as connection,
            connection.transaction(),
        ):
                current = await self._records.locked(connection, access_id)
                if current is None:
                    if expected != 0:
                        raise self._records.conflict(
                            "card_handle_revision_conflict",
                            expected_revision=expected,
                            current_revision=0,
                        )
                    return None
                if current.revision != expected:
                    raise self._records.conflict(
                        "card_handle_revision_conflict",
                        expected_revision=expected,
                        current_revision=current.revision,
                    )
                if current.state == target:
                    return current
                if current.state != HANDLE_STATE_ACTIVE:
                    raise self._records.conflict(
                        "card_handle_terminal_state_conflict",
                        expected_revision=expected,
                        current_revision=current.revision,
                    )
                row = await connection.fetchrow(
                    f"""
                    UPDATE {self._records.schema}.{TABLE_CARD_HANDLE_METADATA}
                    SET state = $2,
                        revision = revision + 1,
                        updated_at = now(),
                        retired_at = now(),
                        cleanup_claim_token = '',
                        cleanup_claimed_at = NULL,
                        cleanup_claim_expires_at = NULL,
                        cleanup_next_attempt_at = now(),
                        cleanup_last_error = ''
                    WHERE access_id = $1
                    RETURNING {CARD_HANDLE_COLUMNS}
                    """,
                    current.access_id,
                    target,
                )
                saved = self._records.record(row)
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
        async with (
            self._records.pool.acquire() as connection,
            connection.transaction(),
        ):
                rows = await connection.fetch(
                    f"""
                    WITH victims AS (
                        SELECT access_id
                        FROM {self._records.schema}.{TABLE_CARD_HANDLE_METADATA}
                        WHERE state = 'active'
                          AND expires_at <= to_timestamp($1)
                        ORDER BY expires_at, access_id
                        LIMIT $2
                        FOR UPDATE SKIP LOCKED
                    )
                    UPDATE
                        {self._records.schema}.{TABLE_CARD_HANDLE_METADATA} AS metadata
                    SET state = 'expired',
                        revision = metadata.revision + 1,
                        updated_at = now(),
                        retired_at = now(),
                        cleanup_claim_token = '',
                        cleanup_claimed_at = NULL,
                        cleanup_claim_expires_at = NULL,
                        cleanup_next_attempt_at = now(),
                        cleanup_last_error = ''
                    FROM victims
                    WHERE metadata.access_id = victims.access_id
                    RETURNING {card_handle_columns('metadata')}
                    """,
                    moment,
                    bounded,
                )
        return [
            record
            for row in rows
            if (record := self._records.record(row)) is not None
        ]

    async def purge_terminal(
        self,
        *,
        retired_before: int,
        limit: int = 1000,
    ) -> int:
        bounded = int(limit)
        if bounded < 1 or bounded > 10_000:
            raise ValueError("Card-handle retention limit is invalid")
        async with (
            self._records.pool.acquire() as connection,
            connection.transaction(),
        ):
                removed = await connection.fetchval(
                    f"""
                    WITH victims AS (
                        SELECT candidate.access_id
                        FROM {self._records.schema}.{TABLE_CARD_HANDLE_METADATA}
                            AS candidate
                        WHERE candidate.state <> 'active'
                          AND candidate.resident_access_secret_ref = ''
                          AND candidate.retired_at <= to_timestamp($1)
                          AND NOT EXISTS (
                              SELECT 1
                              FROM
                                {self._records.schema}.{TABLE_RETIRED_RESIDENT_SECRETS}
                                  AS retired_secret
                              WHERE retired_secret.access_id = candidate.access_id
                          )
                        ORDER BY candidate.retired_at, candidate.access_id
                        LIMIT $2
                        FOR UPDATE SKIP LOCKED
                    ), deleted AS (
                        DELETE FROM
                            {self._records.schema}.{TABLE_CARD_HANDLE_METADATA}
                                AS metadata
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


__all__ = ["PostgresCardHandleLifecycleAuthority"]

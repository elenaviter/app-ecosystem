# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""PostgreSQL reads and row locks for delegated-Card handle metadata."""

from __future__ import annotations

import time
from typing import Any

from connection_hub.delegated_credentials.cards.handle_metadata import (
    CardHandleMetadata,
    CardHandleMetadataConflict,
)
from connection_hub.delegated_credentials.cards.handle_records import (
    CARD_HANDLE_COLUMNS,
    card_handle_from_row,
)
from connection_hub.delegated_credentials.cards.handle_schema import (
    TABLE_CARD_HANDLE_METADATA,
)
from connection_hub.delegated_credentials.cards.store import validated_access_id


class PostgresCardHandleRecordAuthority:
    """Read current rows and provide transaction-scoped row locking."""

    def __init__(self, *, pg_pool: Any, schema: str) -> None:
        self.pool = pg_pool
        self.schema = str(schema or "").strip()
        if self.pool is None or not self.schema:
            raise RuntimeError("Card handle record authority requires PostgreSQL scope")

    @staticmethod
    def record(row: Any) -> CardHandleMetadata | None:
        return card_handle_from_row(row)

    @staticmethod
    def conflict(
        reason: str,
        *,
        expected_revision: int,
        current_revision: int,
    ) -> CardHandleMetadataConflict:
        return CardHandleMetadataConflict(
            reason,
            expected_revision=expected_revision,
            current_revision=current_revision,
        )

    async def locked(
        self,
        connection: Any,
        access_id: str,
    ) -> CardHandleMetadata | None:
        row = await connection.fetchrow(
            f"""
            SELECT {CARD_HANDLE_COLUMNS}
            FROM {self.schema}.{TABLE_CARD_HANDLE_METADATA}
            WHERE access_id = $1
            FOR UPDATE
            """,
            validated_access_id(access_id),
        )
        return self.record(row)

    async def read_current(self, access_id: str) -> CardHandleMetadata | None:
        row = await self.pool.fetchrow(
            f"""
            SELECT {CARD_HANDLE_COLUMNS}
            FROM {self.schema}.{TABLE_CARD_HANDLE_METADATA}
            WHERE access_id = $1
            """,
            validated_access_id(access_id),
        )
        return self.record(row)

    async def read_active(
        self,
        access_id: str,
        *,
        now: int | None = None,
    ) -> CardHandleMetadata | None:
        moment = int(now if now is not None else time.time())
        row = await self.pool.fetchrow(
            f"""
            SELECT {CARD_HANDLE_COLUMNS}
            FROM {self.schema}.{TABLE_CARD_HANDLE_METADATA}
            WHERE access_id = $1
              AND state = 'active'
              AND expires_at > to_timestamp($2)
            """,
            validated_access_id(access_id),
            moment,
        )
        return self.record(row)

    async def list_active(self, *, now: int | None = None) -> list[CardHandleMetadata]:
        moment = int(now if now is not None else time.time())
        rows = await self.pool.fetch(
            f"""
            SELECT {CARD_HANDLE_COLUMNS}
            FROM {self.schema}.{TABLE_CARD_HANDLE_METADATA}
            WHERE state = 'active'
              AND expires_at > to_timestamp($1)
            ORDER BY access_id
            """,
            moment,
        )
        return [record for row in rows if (record := self.record(row)) is not None]


__all__ = ["PostgresCardHandleRecordAuthority"]

# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""PostgreSQL authority for resident-secret preparation intents."""

from __future__ import annotations

from typing import Any

from connection_hub.delegated_credentials.cards.handle_metadata import (
    PREPARED_SECRET_STATE_INSTALLABLE,
    CardHandleMetadata,
    CardHandleMetadataConflict,
    PreparedResidentSecret,
)
from connection_hub.delegated_credentials.cards.handle_records import (
    PREPARED_SECRET_COLUMNS,
    prepared_secret_from_row,
)
from connection_hub.delegated_credentials.cards.handle_schema import (
    TABLE_CARD_HANDLE_METADATA,
    TABLE_PREPARED_RESIDENT_SECRETS,
    TABLE_RETIRED_RESIDENT_SECRETS,
)
from connection_hub.delegated_credentials.cards.resident_secret_locking import (
    lock_resident_secret_refs,
)


class PostgresPreparedSecretAuthority:
    """Reserve, validate, consume, and release prepared secret references."""

    def __init__(self, *, pg_pool: Any, schema: str) -> None:
        self._pool = pg_pool
        self.schema = str(schema or "").strip()
        if self._pool is None or not self.schema:
            raise RuntimeError("prepared secret authority requires PostgreSQL scope")

    @staticmethod
    def _conflict(
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

    async def locked_prepared_secret(
        self,
        connection: Any,
        secret_ref: str,
    ) -> PreparedResidentSecret | None:
        row = await connection.fetchrow(
            f"""
            SELECT {PREPARED_SECRET_COLUMNS}
            FROM {self.schema}.{TABLE_PREPARED_RESIDENT_SECRETS}
            WHERE secret_ref = $1
            FOR UPDATE
            """,
            str(secret_ref or "").strip(),
        )
        return prepared_secret_from_row(row)

    @staticmethod
    def prepared_matches_candidate(
        prepared: PreparedResidentSecret,
        candidate: CardHandleMetadata,
    ) -> bool:
        return (
            prepared.access_id == candidate.access_id
            and prepared.secret_ref == candidate.resident_access_secret_ref
            and prepared.resident_access_sha256
            == candidate.resident_access_sha256
            and prepared.card_revision == candidate.card_revision
            and prepared.expires_at == candidate.expires_at
        )

    async def require_prepared_intent(
        self,
        connection: Any,
        *,
        candidate: CardHandleMetadata,
        expected_revision: int,
        current_revision: int,
    ) -> PreparedResidentSecret:
        prepared = await self.locked_prepared_secret(
            connection,
            candidate.resident_access_secret_ref,
        )
        if prepared is None:
            raise self._conflict(
                "card_handle_prepared_secret_missing",
                expected_revision=expected_revision,
                current_revision=current_revision,
            )
        if prepared.state != PREPARED_SECRET_STATE_INSTALLABLE:
            raise self._conflict(
                "card_handle_prepared_secret_cleanup_started",
                expected_revision=expected_revision,
                current_revision=current_revision,
            )
        if not self.prepared_matches_candidate(prepared, candidate):
            raise self._conflict(
                "card_handle_prepared_secret_mismatch",
                expected_revision=expected_revision,
                current_revision=current_revision,
            )
        return prepared

    async def consume_prepared_intent(
        self,
        connection: Any,
        *,
        prepared: PreparedResidentSecret,
    ) -> None:
        result = await connection.execute(
            f"""
            DELETE FROM {self.schema}.{TABLE_PREPARED_RESIDENT_SECRETS}
            WHERE secret_ref = $1
              AND access_id = $2
              AND resident_access_sha256 = $3
              AND card_revision = $4
              AND envelope_created_at = to_timestamp($5)
              AND expires_at = to_timestamp($6)
              AND state = 'installable'
              AND cleanup_claim_token = ''
            """,
            prepared.secret_ref,
            prepared.access_id,
            prepared.resident_access_sha256,
            prepared.card_revision,
            prepared.created_at,
            prepared.expires_at,
        )
        if not str(result or "").strip().endswith(" 1"):
            raise RuntimeError("prepared resident-secret intent was not consumed")

    async def prepare_resident_secret(
        self,
        prepared: PreparedResidentSecret,
    ) -> bool:
        intent = prepared.validated()
        if intent.state != PREPARED_SECRET_STATE_INSTALLABLE:
            raise ValueError("new prepared resident secret must be installable")
        async with self._pool.acquire() as connection, connection.transaction():
            await lock_resident_secret_refs(connection, intent.secret_ref)
            row = await connection.fetchrow(
                f"""
                    INSERT INTO {self.schema}.{TABLE_PREPARED_RESIDENT_SECRETS} (
                        secret_ref,
                        access_id,
                        resident_access_sha256,
                        card_revision,
                        envelope_created_at,
                        expires_at
                    )
                    SELECT $1, $2, $3, $4, to_timestamp($5), to_timestamp($6)
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM {self.schema}.{TABLE_CARD_HANDLE_METADATA}
                        WHERE resident_access_secret_ref = $1
                    )
                      AND NOT EXISTS (
                        SELECT 1
                        FROM {self.schema}.{TABLE_RETIRED_RESIDENT_SECRETS}
                        WHERE secret_ref = $1
                    )
                      AND NOT EXISTS (
                        SELECT 1
                        FROM {self.schema}.{TABLE_PREPARED_RESIDENT_SECRETS}
                        WHERE secret_ref = $1
                    )
                    RETURNING secret_ref
                    """,
                intent.secret_ref,
                intent.access_id,
                intent.resident_access_sha256,
                intent.card_revision,
                intent.created_at,
                intent.expires_at,
            )
        return row is not None

__all__ = ["PostgresPreparedSecretAuthority"]

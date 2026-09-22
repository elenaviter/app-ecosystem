# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Composed PostgreSQL store for durable delegated-Card handle metadata."""

from __future__ import annotations

from typing import Any

from connection_hub.delegated_credentials.cards.cleanup_claim_authority import (
    PostgresResidentSecretCleanupClaimAuthority,
)
from connection_hub.delegated_credentials.cards.cleanup_settlement_authority import (
    PostgresResidentSecretCleanupSettlementAuthority,
)
from connection_hub.delegated_credentials.cards.handle_lifecycle_authority import (
    PostgresCardHandleLifecycleAuthority,
)
from connection_hub.delegated_credentials.cards.handle_metadata import (
    HANDLE_STATE_ACTIVE,
    HANDLE_STATE_EXPIRED,
    HANDLE_STATE_REVOKED,
    HANDLE_STATES,
    CardHandleMetadata,
    CardHandleMetadataConflict,
    CardHandleMetadataStore,
    CardHandleMutationResult,
    PreparedResidentSecret,
    ResidentSecretCleanupAcknowledgement,
    ResidentSecretCleanupClaim,
    RetiredResidentSecret,
)
from connection_hub.delegated_credentials.cards.handle_mutation_authority import (
    PostgresCardHandleMutationAuthority,
)
from connection_hub.delegated_credentials.cards.handle_record_authority import (
    PostgresCardHandleRecordAuthority,
)
from connection_hub.delegated_credentials.cards.handle_schema import (
    card_handle_schema,
    card_handle_schema_sql,
)
from connection_hub.delegated_credentials.cards.prepared_secret_authority import (
    PostgresPreparedSecretAuthority,
)


class PostgresCardHandleMetadataStore:
    """Compose focused PostgreSQL authorities behind the portable store port."""

    def __init__(self, *, pg_pool: Any, tenant: str, project: str) -> None:
        if pg_pool is None:
            raise RuntimeError("PostgresCardHandleMetadataStore requires pg_pool")
        self._pool = pg_pool
        self.tenant = str(tenant or "").strip() or "default"
        self.project = str(project or "").strip() or "default"
        self.schema = card_handle_schema(tenant=self.tenant, project=self.project)
        self._records = PostgresCardHandleRecordAuthority(
            pg_pool=self._pool,
            schema=self.schema,
        )
        self._prepared_secrets = PostgresPreparedSecretAuthority(
            pg_pool=self._pool,
            schema=self.schema,
        )
        self._cleanup_claims = PostgresResidentSecretCleanupClaimAuthority(
            pg_pool=self._pool,
            schema=self.schema,
        )
        self._cleanup_settlement = PostgresResidentSecretCleanupSettlementAuthority(
            pg_pool=self._pool,
            schema=self.schema,
        )
        self._mutations = PostgresCardHandleMutationAuthority(
            records=self._records,
            prepared_secrets=self._prepared_secrets,
            tenant=self.tenant,
            project=self.project,
        )
        self._lifecycle = PostgresCardHandleLifecycleAuthority(
            records=self._records,
        )

    async def ensure_schema(self) -> None:
        async with self._pool.acquire() as connection, connection.transaction():
            await connection.execute(card_handle_schema_sql(self.schema))

    async def read_current(self, access_id: str) -> CardHandleMetadata | None:
        return await self._records.read_current(access_id)

    async def read_active(
        self,
        access_id: str,
        *,
        now: int | None = None,
    ) -> CardHandleMetadata | None:
        return await self._records.read_active(access_id, now=now)

    async def put(
        self,
        metadata: CardHandleMetadata,
        *,
        expected_revision: int,
    ) -> CardHandleMetadata:
        return await self._mutations.put(
            metadata,
            expected_revision=expected_revision,
        )

    async def prepare_resident_secret(
        self,
        prepared: PreparedResidentSecret,
    ) -> bool:
        return await self._prepared_secrets.prepare_resident_secret(prepared)

    async def install_prepared_resident_secret(
        self,
        metadata: CardHandleMetadata,
        *,
        expected_revision: int,
    ) -> CardHandleMutationResult:
        return await self._mutations.install_prepared_resident_secret(
            metadata,
            expected_revision=expected_revision,
        )

    async def claim_prepared_secret_cleanup(
        self,
        *,
        now: int,
        limit: int = 100,
        candidate: PreparedResidentSecret | None = None,
    ) -> list[ResidentSecretCleanupClaim]:
        return await self._cleanup_claims.claim_prepared_secret_cleanup(
            now=now,
            limit=limit,
            candidate=candidate,
        )

    async def retire(
        self,
        access_id: str,
        *,
        expected_revision: int,
        state: str,
    ) -> CardHandleMetadata | None:
        return await self._lifecycle.retire(
            access_id,
            expected_revision=expected_revision,
            state=state,
        )

    async def expire_due(
        self,
        *,
        now: int | None = None,
        limit: int = 100,
    ) -> list[CardHandleMetadata]:
        return await self._lifecycle.expire_due(now=now, limit=limit)

    async def claim_retired_secret_cleanup(
        self,
        *,
        now: int,
        limit: int = 100,
        candidate: RetiredResidentSecret | None = None,
    ) -> list[ResidentSecretCleanupClaim]:
        return await self._cleanup_claims.claim_retired_secret_cleanup(
            now=now,
            limit=limit,
            candidate=candidate,
        )

    async def claim_terminal_secret_cleanup(
        self,
        *,
        now: int,
        limit: int = 100,
        candidate: CardHandleMetadata | None = None,
    ) -> list[ResidentSecretCleanupClaim]:
        return await self._cleanup_claims.claim_terminal_secret_cleanup(
            now=now,
            limit=limit,
            candidate=candidate,
        )

    async def acknowledge_resident_secret_cleanup(
        self,
        claim: ResidentSecretCleanupClaim,
    ) -> ResidentSecretCleanupAcknowledgement:
        return await self._cleanup_settlement.acknowledge_resident_secret_cleanup(
            claim
        )

    async def defer_resident_secret_cleanup(
        self,
        claim: ResidentSecretCleanupClaim,
        *,
        now: int,
        reason: str,
    ) -> bool:
        return await self._cleanup_settlement.defer_resident_secret_cleanup(
            claim,
            now=now,
            reason=reason,
        )

    async def purge_terminal(
        self,
        *,
        retired_before: int,
        limit: int = 1000,
    ) -> int:
        return await self._lifecycle.purge_terminal(
            retired_before=retired_before,
            limit=limit,
        )


__all__ = [
    "HANDLE_STATES",
    "HANDLE_STATE_ACTIVE",
    "HANDLE_STATE_EXPIRED",
    "HANDLE_STATE_REVOKED",
    "CardHandleMetadata",
    "CardHandleMetadataConflict",
    "CardHandleMetadataStore",
    "CardHandleMutationResult",
    "PostgresCardHandleMetadataStore",
    "PreparedResidentSecret",
    "ResidentSecretCleanupAcknowledgement",
    "ResidentSecretCleanupClaim",
    "RetiredResidentSecret",
]

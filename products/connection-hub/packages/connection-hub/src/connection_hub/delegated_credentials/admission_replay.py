# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Durable replay claims for signed protected-service admission requests.

The signed nonce is a bearer-adjacent request value and never enters SQL.
PostgreSQL stores only ``sha256(service_id + "\\n" + nonce)``. A successful
insert is the durable fact that the proof has been evaluated once.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Protocol

from connection_hub.hub.authenticator_store import schema_for_scope


TABLE_ADMISSION_REPLAY_CLAIMS = "connection_hub_admission_replay_claims"


def admission_replay_schema(*, tenant: str, project: str) -> str:
    return schema_for_scope(tenant=tenant, project=project)


def admission_replay_schema_sql(schema: str) -> str:
    """Return DDL for immutable, digest-keyed replay claims."""

    return f"""
CREATE SCHEMA IF NOT EXISTS {schema};

CREATE TABLE IF NOT EXISTS {schema}.{TABLE_ADMISSION_REPLAY_CLAIMS} (
    service_id       TEXT NOT NULL,
    nonce_sha256     CHAR(64) NOT NULL,
    tenant           TEXT NOT NULL,
    project          TEXT NOT NULL,
    claimed_at       TIMESTAMPTZ NOT NULL,
    expires_at       TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (service_id, nonce_sha256),
    CHECK (expires_at > claimed_at)
);

CREATE INDEX IF NOT EXISTS connection_hub_admission_replay_expiry_idx
    ON {schema}.{TABLE_ADMISSION_REPLAY_CLAIMS} (expires_at);
"""


def admission_nonce_digest(*, service_id: str, nonce: str) -> str:
    """Return the non-secret identity used by the replay ledger."""

    service = str(service_id or "").strip()
    raw_nonce = str(nonce or "")
    if not service:
        raise ValueError("admission replay service id is required")
    if not raw_nonce.strip():
        raise ValueError("admission replay nonce is required")
    return hashlib.sha256(f"{service}\n{raw_nonce}".encode("utf-8")).hexdigest()


class AdmissionReplayClaimStore(Protocol):
    """Durably claim one signed nonce exactly once for its validity window."""

    async def claim(
        self,
        *,
        service_id: str,
        nonce: str,
        ttl_seconds: int,
        now: int | None = None,
    ) -> bool: ...


class PostgresAdmissionReplayClaimStore:
    """PostgreSQL uniqueness authority for protected-service proof nonces."""

    def __init__(self, *, pg_pool: Any, tenant: str, project: str) -> None:
        if pg_pool is None:
            raise RuntimeError("PostgresAdmissionReplayClaimStore requires pg_pool")
        self._pool = pg_pool
        self.tenant = str(tenant or "").strip() or "default"
        self.project = str(project or "").strip() or "default"
        self.schema = admission_replay_schema(
            tenant=self.tenant,
            project=self.project,
        )

    async def ensure_schema(self) -> None:
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(admission_replay_schema_sql(self.schema))

    async def claim(
        self,
        *,
        service_id: str,
        nonce: str,
        ttl_seconds: int,
        now: int | None = None,
    ) -> bool:
        service = str(service_id or "").strip()
        digest = admission_nonce_digest(service_id=service, nonce=nonce)
        ttl = int(ttl_seconds)
        if ttl < 1:
            raise ValueError("admission replay claim TTL must be positive")
        moment = int(now if now is not None else time.time())
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                claimed = await connection.fetchval(
                    f"""
                    INSERT INTO {self.schema}.{TABLE_ADMISSION_REPLAY_CLAIMS} (
                        service_id, nonce_sha256, tenant, project,
                        claimed_at, expires_at
                    ) VALUES (
                        $1, $2, $3, $4,
                        to_timestamp($5),
                        to_timestamp($5) + ($6 * interval '1 second')
                    )
                    ON CONFLICT (service_id, nonce_sha256) DO NOTHING
                    RETURNING nonce_sha256
                    """,
                    service,
                    digest,
                    self.tenant,
                    self.project,
                    moment,
                    ttl,
                )
        return claimed is not None

    async def purge_expired(self, *, now: int | None = None, limit: int = 1000) -> int:
        """Remove a bounded batch after the proof validity window has ended."""

        bounded_limit = int(limit)
        if bounded_limit < 1 or bounded_limit > 10_000:
            raise ValueError("admission replay purge limit is invalid")
        moment = int(now if now is not None else time.time())
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                removed = await connection.fetchval(
                    f"""
                    WITH victims AS (
                        SELECT service_id, nonce_sha256
                        FROM {self.schema}.{TABLE_ADMISSION_REPLAY_CLAIMS}
                        WHERE expires_at <= to_timestamp($1)
                        ORDER BY expires_at, service_id, nonce_sha256
                        LIMIT $2
                        FOR UPDATE SKIP LOCKED
                    ), deleted AS (
                        DELETE FROM {self.schema}.{TABLE_ADMISSION_REPLAY_CLAIMS} AS claim
                        USING victims
                        WHERE claim.service_id = victims.service_id
                          AND claim.nonce_sha256 = victims.nonce_sha256
                        RETURNING 1
                    )
                    SELECT count(*) FROM deleted
                    """,
                    moment,
                    bounded_limit,
                )
        return int(removed or 0)


__all__ = [
    "AdmissionReplayClaimStore",
    "PostgresAdmissionReplayClaimStore",
    "TABLE_ADMISSION_REPLAY_CLAIMS",
    "admission_nonce_digest",
    "admission_replay_schema",
    "admission_replay_schema_sql",
]

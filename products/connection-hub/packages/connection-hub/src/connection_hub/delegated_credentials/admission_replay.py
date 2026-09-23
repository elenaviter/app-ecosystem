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
MIGRATED_DIGEST_ONLY_SERVICE_ID = "migration:digest-only-source"


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

CREATE UNIQUE INDEX IF NOT EXISTS connection_hub_admission_replay_digest_idx
    ON {schema}.{TABLE_ADMISSION_REPLAY_CLAIMS} (nonce_sha256);
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
                    ON CONFLICT (nonce_sha256) DO NOTHING
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

    async def import_digest(
        self,
        *,
        nonce_sha256: str,
        expires_at_ms: int,
    ) -> None:
        """Import a Redis claim whose source retained only its digest.

        The digest is the conflict authority for both migrated and newly
        claimed rows. The source did not retain ``service_id``; the explicit
        marker records that fact without weakening replay protection.
        """

        digest = str(nonce_sha256 or "").strip().lower()
        if len(digest) != 64 or any(value not in "0123456789abcdef" for value in digest):
            raise ValueError("admission replay digest must be a SHA-256 digest")
        expiry_ms = int(expires_at_ms)
        if expiry_ms <= int(time.time() * 1000):
            raise ValueError("admission replay source claim already expired")
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    f"""
                    INSERT INTO {self.schema}.{TABLE_ADMISSION_REPLAY_CLAIMS} (
                        service_id, nonce_sha256, tenant, project,
                        claimed_at, expires_at
                    ) VALUES (
                        $1, $2, $3, $4,
                        LEAST(now(), to_timestamp($5::double precision / 1000.0)
                            - interval '1 millisecond'),
                        to_timestamp($5::double precision / 1000.0)
                    )
                    ON CONFLICT (nonce_sha256) DO NOTHING
                    """,
                    MIGRATED_DIGEST_ONLY_SERVICE_ID,
                    digest,
                    self.tenant,
                    self.project,
                    expiry_ms,
                )
                row = await connection.fetchrow(
                    f"""
                    SELECT service_id, tenant, project,
                           floor(extract(epoch FROM expires_at) * 1000)::bigint
                               AS expires_at_ms
                    FROM {self.schema}.{TABLE_ADMISSION_REPLAY_CLAIMS}
                    WHERE nonce_sha256 = $1
                    FOR UPDATE
                    """,
                    digest,
                )
                value = dict(row) if row is not None else {}
                if (
                    str(value.get("tenant") or "") != self.tenant
                    or str(value.get("project") or "") != self.project
                    or int(value.get("expires_at_ms") or 0) != expiry_ms
                    or str(value.get("service_id") or "")
                    != MIGRATED_DIGEST_ONLY_SERVICE_ID
                ):
                    raise RuntimeError("admission_replay_migration_target_conflict")

    async def migration_rows(
        self,
        *,
        captured_at_ms: int,
    ) -> list[dict[str, Any]]:
        """Return active digest-only evidence for target reconciliation."""

        captured = int(captured_at_ms)
        if captured <= 0:
            raise ValueError("migration capture time must be a Unix millisecond")
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                f"""
                SELECT service_id, nonce_sha256,
                       floor(extract(epoch FROM expires_at) * 1000)::bigint
                           AS expires_at_ms
                FROM {self.schema}.{TABLE_ADMISSION_REPLAY_CLAIMS}
                WHERE expires_at > to_timestamp($1::double precision / 1000.0)
                ORDER BY nonce_sha256
                """,
                captured,
            )
        return [dict(row) for row in rows]

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
    "MIGRATED_DIGEST_ONLY_SERVICE_ID",
    "PostgresAdmissionReplayClaimStore",
    "TABLE_ADMISSION_REPLAY_CLAIMS",
    "admission_nonce_digest",
    "admission_replay_schema",
    "admission_replay_schema_sql",
]

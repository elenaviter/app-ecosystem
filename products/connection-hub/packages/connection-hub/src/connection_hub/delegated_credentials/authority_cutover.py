# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Durable activation receipt for Connection Hub authority generations."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from connection_hub.hub.authenticator_store import schema_for_scope

TABLE_AUTHORITY_CUTOVERS = "connection_hub_authority_cutovers"

FAMILY_OAUTH_CLIENTS = "oauth_clients"
FAMILY_OAUTH_REFRESH = "oauth_refresh_families"
FAMILY_OAUTH_ACCESS = "oauth_access_bindings"
FAMILY_CARD_HANDLES = "card_handle_metadata"
FAMILY_RESIDENT_CARD_SECRETS = "resident_card_secrets"
FAMILY_ADMISSION_REPLAY = "admission_replay_claims"
FAMILY_BUNDLE_USERS = "bundle_users"
FAMILY_BUNDLE_SESSIONS = "bundle_sessions"
FAMILY_BUNDLE_AUTHORITY_VERSION = "bundle_authority_version"
FAMILY_PLATFORM_SESSIONS = "platform_sessions"

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class AuthorityCutoverRequired(RuntimeError):
    """A runtime cannot use PostgreSQL before its activation receipt exists."""

    def __init__(self, reason: str, *, generation_id: str) -> None:
        self.reason = str(reason or "authority_cutover_required")
        self.generation_id = str(generation_id or "")
        super().__init__(f"{self.reason}: {self.generation_id or '<missing>'}")


class AuthorityCutoverConflict(RuntimeError):
    """One generation id was presented with different immutable evidence."""


def authority_cutover_schema(*, tenant: str, project: str) -> str:
    return schema_for_scope(tenant=tenant, project=project)


def authority_cutover_schema_sql(schema: str) -> str:
    return f"""
CREATE SCHEMA IF NOT EXISTS {schema};

CREATE TABLE IF NOT EXISTS {schema}.{TABLE_AUTHORITY_CUTOVERS} (
    generation_id                 TEXT PRIMARY KEY,
    activated_revision           BIGSERIAL UNIQUE,
    source_generation            CHAR(64) NOT NULL,
    target_generation            CHAR(64) NOT NULL,
    source_counts                JSONB NOT NULL,
    target_counts                JSONB NOT NULL,
    prerequisites                JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    preview_sha256               CHAR(64) NOT NULL,
    applied_at                   TIMESTAMPTZ NOT NULL DEFAULT now()
);

LOCK TABLE {schema}.{TABLE_AUTHORITY_CUTOVERS} IN ACCESS EXCLUSIVE MODE;

DO $authority_cutover_generation_id$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = '{schema}'
          AND table_name = '{TABLE_AUTHORITY_CUTOVERS}'
          AND column_name = 'migration_id'
    ) AND EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = '{schema}'
          AND table_name = '{TABLE_AUTHORITY_CUTOVERS}'
          AND column_name = 'generation_id'
    ) THEN
        RAISE EXCEPTION
            'authority cutover table has both migration_id and generation_id';
    ELSIF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = '{schema}'
          AND table_name = '{TABLE_AUTHORITY_CUTOVERS}'
          AND column_name = 'migration_id'
    ) THEN
        ALTER TABLE {schema}.{TABLE_AUTHORITY_CUTOVERS}
            RENAME COLUMN migration_id TO generation_id;
    END IF;
END
$authority_cutover_generation_id$;

ALTER TABLE {schema}.{TABLE_AUTHORITY_CUTOVERS}
    ADD COLUMN IF NOT EXISTS prerequisites JSONB NOT NULL DEFAULT '{{}}'::jsonb;
"""


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _validated_counts(value: Mapping[str, Any], *, field_name: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for raw_name, raw_count in dict(value or {}).items():
        name = str(raw_name or "").strip()
        if not name or isinstance(raw_count, bool):
            raise ValueError(f"{field_name} contains an invalid family")
        if not isinstance(raw_count, int):
            raise ValueError(f"{field_name}.{name} must be an integer")
        count = int(raw_count)
        if count < 0:
            raise ValueError(f"{field_name}.{name} must be a non-negative integer")
        result[name] = count
    return dict(sorted(result.items()))


@dataclass(frozen=True)
class AuthorityCutoverReceipt:
    generation_id: str
    source_generation: str
    target_generation: str
    source_counts: Mapping[str, int]
    target_counts: Mapping[str, int]
    preview_sha256: str
    prerequisites: Mapping[str, Any] = field(default_factory=dict)
    activated_revision: int = 0
    applied_at: datetime | None = None

    def validated(self) -> "AuthorityCutoverReceipt":
        generation_id = str(self.generation_id or "").strip()
        source_generation = str(self.source_generation or "").strip().lower()
        target_generation = str(self.target_generation or "").strip().lower()
        preview_sha256 = str(self.preview_sha256 or "").strip().lower()
        if not _IDENTIFIER_PATTERN.fullmatch(generation_id):
            raise ValueError("generation_id must be a bounded stable identifier")
        for name, value in (
            ("source_generation", source_generation),
            ("target_generation", target_generation),
            ("preview_sha256", preview_sha256),
        ):
            if not _SHA256_PATTERN.fullmatch(value):
                raise ValueError(f"{name} must be a SHA-256 digest")
        source_counts = _validated_counts(
            self.source_counts,
            field_name="source_counts",
        )
        target_counts = _validated_counts(
            self.target_counts,
            field_name="target_counts",
        )
        if source_counts != target_counts:
            raise ValueError("source_counts and target_counts must reconcile exactly")
        prerequisites = dict(self.prerequisites or {})
        json.dumps(prerequisites, sort_keys=True, separators=(",", ":"))
        activated_revision = int(self.activated_revision or 0)
        if activated_revision < 0:
            raise ValueError("activated_revision must be non-negative")
        return AuthorityCutoverReceipt(
            generation_id=generation_id,
            source_generation=source_generation,
            target_generation=target_generation,
            source_counts=source_counts,
            target_counts=target_counts,
            preview_sha256=preview_sha256,
            prerequisites=prerequisites,
            activated_revision=activated_revision,
            applied_at=self.applied_at,
        )

    def require_families(self, required_families: Sequence[str]) -> None:
        required = {str(value or "").strip() for value in required_families}
        required.discard("")
        missing = sorted(required.difference(self.source_counts))
        if missing:
            raise AuthorityCutoverRequired(
                "authority_cutover_families_missing:" + ",".join(missing),
                generation_id=self.generation_id,
            )


def _receipt_from_row(row: Mapping[str, Any]) -> AuthorityCutoverReceipt:
    value = dict(row)
    return AuthorityCutoverReceipt(
        generation_id=str(value.get("generation_id") or ""),
        source_generation=str(value.get("source_generation") or ""),
        target_generation=str(value.get("target_generation") or ""),
        source_counts=_json_object(value.get("source_counts")),
        target_counts=_json_object(value.get("target_counts")),
        prerequisites=_json_object(value.get("prerequisites")),
        preview_sha256=str(value.get("preview_sha256") or ""),
        activated_revision=int(value.get("activated_revision") or 0),
        applied_at=value.get("applied_at"),
    ).validated()


def _immutable_evidence(receipt: AuthorityCutoverReceipt) -> tuple[Any, ...]:
    return (
        receipt.generation_id,
        receipt.source_generation,
        receipt.target_generation,
        dict(receipt.source_counts),
        dict(receipt.target_counts),
        dict(receipt.prerequisites),
        receipt.preview_sha256,
    )


class PostgresAuthorityCutoverStore:
    """Record and verify one immutable, idempotent generation activation."""

    def __init__(self, *, pg_pool: Any, tenant: str, project: str) -> None:
        if pg_pool is None:
            raise RuntimeError("PostgresAuthorityCutoverStore requires pg_pool")
        self._pool = pg_pool
        self.tenant = str(tenant or "").strip() or "default"
        self.project = str(project or "").strip() or "default"
        self.schema = authority_cutover_schema(
            tenant=self.tenant,
            project=self.project,
        )

    async def ensure_schema(self) -> None:
        async with self._pool.acquire() as connection, connection.transaction():
            await connection.execute(authority_cutover_schema_sql(self.schema))

    async def activate(
        self,
        receipt: AuthorityCutoverReceipt,
    ) -> AuthorityCutoverReceipt:
        candidate = receipt.validated()
        async with self._pool.acquire() as connection, connection.transaction():
            await connection.execute(
                f"""
                DELETE FROM {self.schema}.{TABLE_AUTHORITY_CUTOVERS}
                WHERE generation_id <> $1
                """,
                candidate.generation_id,
            )
            await connection.execute(
                f"""
                INSERT INTO {self.schema}.{TABLE_AUTHORITY_CUTOVERS} (
                    generation_id, source_generation, target_generation,
                    source_counts, target_counts, prerequisites, preview_sha256
                ) VALUES (
                    $1, $2, $3,
                    ($4::text)::jsonb, ($5::text)::jsonb,
                    ($6::text)::jsonb, $7
                )
                ON CONFLICT (generation_id) DO NOTHING
                """,
                candidate.generation_id,
                candidate.source_generation,
                candidate.target_generation,
                json.dumps(candidate.source_counts, sort_keys=True, separators=(",", ":")),
                json.dumps(candidate.target_counts, sort_keys=True, separators=(",", ":")),
                json.dumps(candidate.prerequisites, sort_keys=True, separators=(",", ":")),
                candidate.preview_sha256,
            )
            row = await connection.fetchrow(
                f"""
                SELECT generation_id, activated_revision,
                       source_generation, target_generation,
                       source_counts, target_counts, prerequisites,
                       preview_sha256, applied_at
                FROM {self.schema}.{TABLE_AUTHORITY_CUTOVERS}
                WHERE generation_id = $1
                FOR UPDATE
                """,
                candidate.generation_id,
            )
            if row is None:
                raise RuntimeError("authority_cutover_activation_outcome_unknown")
            activated = _receipt_from_row(row)
            if _immutable_evidence(activated) != _immutable_evidence(candidate):
                raise AuthorityCutoverConflict(
                    "authority_cutover_generation_id_already_has_different_evidence"
                )
            return activated

    async def read(self, generation_id: str) -> AuthorityCutoverReceipt | None:
        identifier = str(generation_id or "").strip()
        if not identifier:
            return None
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                SELECT generation_id, activated_revision,
                       source_generation, target_generation,
                       source_counts, target_counts, prerequisites,
                       preview_sha256, applied_at
                FROM {self.schema}.{TABLE_AUTHORITY_CUTOVERS}
                WHERE generation_id = $1
                """,
                identifier,
            )
        return _receipt_from_row(row) if row is not None else None

    async def require_activated(
        self,
        generation_id: str,
        *,
        required_families: Sequence[str],
    ) -> AuthorityCutoverReceipt:
        receipt = await self.read(generation_id)
        if receipt is None:
            raise AuthorityCutoverRequired(
                "authority_cutover_receipt_missing",
                generation_id=generation_id,
            )
        receipt.require_families(required_families)
        return receipt


__all__ = [
    "FAMILY_ADMISSION_REPLAY",
    "FAMILY_BUNDLE_AUTHORITY_VERSION",
    "FAMILY_BUNDLE_SESSIONS",
    "FAMILY_BUNDLE_USERS",
    "FAMILY_CARD_HANDLES",
    "FAMILY_OAUTH_ACCESS",
    "FAMILY_OAUTH_CLIENTS",
    "FAMILY_OAUTH_REFRESH",
    "FAMILY_PLATFORM_SESSIONS",
    "FAMILY_RESIDENT_CARD_SECRETS",
    "TABLE_AUTHORITY_CUTOVERS",
    "AuthorityCutoverConflict",
    "AuthorityCutoverReceipt",
    "AuthorityCutoverRequired",
    "PostgresAuthorityCutoverStore",
    "authority_cutover_schema",
    "authority_cutover_schema_sql",
]

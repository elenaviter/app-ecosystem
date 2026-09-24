# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Insert-only OAuth authority migration into PostgreSQL."""

from __future__ import annotations

import json
import time
from typing import Any, Mapping

from connection_hub.delegated_credentials.authority_cutover import (
    FAMILY_OAUTH_ACCESS,
    FAMILY_OAUTH_CLIENTS,
    FAMILY_OAUTH_REFRESH,
)
from connection_hub.delegated_credentials.migration.model import (
    AuthorityMigrationRecord,
    AuthorityMigrationSnapshot,
)
from connection_hub.delegated_credentials.oauth.authority_schema import (
    TABLE_ACCESS_BINDINGS,
    TABLE_CLIENTS,
    TABLE_FAMILIES,
    TABLE_REFRESH_GENERATIONS,
)
from connection_hub.delegated_credentials.oauth.authority_store import (
    PostgresOAuthAuthorityStore,
    _json_array,
    _json_object,
)
from connection_hub.delegated_credentials.oauth.bearers import bearer_sha256
from connection_hub.delegated_credentials.oauth.client_records import (
    canonical_oauth_client_record,
)


class OAuthMigrationTargetConflict(RuntimeError):
    """A target identity already exists with different immutable content."""


def _json_text(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"))


def _status_created(status: str) -> bool:
    return str(status or "").strip().endswith(" 1")


class PostgresOAuthMigrationTarget:
    """Import legacy Redis OAuth records without minting or extending them."""

    def __init__(self, store: PostgresOAuthAuthorityStore) -> None:
        if store is None:
            raise ValueError("PostgresOAuthMigrationTarget requires a store")
        self._store = store
        self._pool = store._pool
        self.schema = store.schema
        self.tenant = store.tenant
        self.project = store.project

    @staticmethod
    def _require_live(record: AuthorityMigrationRecord) -> AuthorityMigrationRecord:
        source = record.validated()
        if source.record_type != "oauth_client" and source.expires_at_ms is None:
            raise ValueError("OAuth migration records require an absolute expiry")
        if (
            source.expires_at_ms is not None
            and source.expires_at_ms <= time.time_ns() // 1_000_000
        ):
            raise ValueError("OAuth migration source record already expired")
        return source

    async def import_record(self, record: AuthorityMigrationRecord) -> bool:
        source = self._require_live(record)
        if source.record_type == "oauth_client":
            return await self._import_client(source)
        if source.record_type == "oauth_refresh":
            return await self._import_refresh(source)
        if source.record_type == "oauth_access":
            return await self._import_access(source)
        raise ValueError(f"unsupported OAuth migration record: {source.record_type}")

    async def remove_record(self, record: AuthorityMigrationRecord) -> bool:
        """Remove one stale migration identity inside the caller's transaction."""

        target = record.validated()
        async with self._pool.acquire() as connection, connection.transaction():
            if target.record_type == "oauth_client":
                status = await connection.execute(
                    f"DELETE FROM {self.schema}.{TABLE_CLIENTS} WHERE client_id = $1",
                    target.identity,
                )
            elif target.record_type == "oauth_access":
                status = await connection.execute(
                    f"DELETE FROM {self.schema}.{TABLE_ACCESS_BINDINGS} "
                    "WHERE token_sha256 = $1",
                    target.identity,
                )
            elif target.record_type == "oauth_refresh":
                status = await connection.execute(
                    f"""
                    DELETE FROM {self.schema}.{TABLE_FAMILIES}
                    WHERE family_id IN (
                        SELECT family_id
                        FROM {self.schema}.{TABLE_REFRESH_GENERATIONS}
                        WHERE token_sha256 = $1
                    )
                    """,
                    target.identity,
                )
            else:
                raise ValueError(
                    f"unsupported OAuth migration record: {target.record_type}"
                )
        return not str(status or "").endswith(" 0")

    async def synchronize_records(
        self,
        records: tuple[AuthorityMigrationRecord, ...],
        *,
        captured_at_ms: int,
    ) -> None:
        """Remove target records absent from, or changed in, the source snapshot."""

        source_by_key = {
            (record.record_type, record.identity): record.validated()
            for record in records
        }
        current = await self.snapshot(captured_at_ms=int(captured_at_ms))
        for target in current.records:
            source = source_by_key.get((target.record_type, target.identity))
            if source is None or source.evidence() != target.evidence():
                await self.remove_record(target)

    async def _import_client(self, source: AuthorityMigrationRecord) -> bool:
        payload = canonical_oauth_client_record(
            _json_object(source.payload.get("record"))
        )
        migration_state = str(
            source.payload.get("migration_state") or ""
        ).strip()
        if migration_state not in {"active", "retired"}:
            raise ValueError("OAuth client migration state is invalid")
        client_id = str(payload.get("client_id") or "").strip()
        if client_id != source.identity:
            raise ValueError("OAuth client migration identity mismatch")
        async with self._pool.acquire() as connection, connection.transaction():
            status = await connection.execute(
                f"""
                INSERT INTO {self.schema}.{TABLE_CLIENTS} (
                    client_id, tenant, project, redirect_uris, grant_types,
                    token_endpoint_auth_method, application_type, metadata,
                    expires_at, retired_at
                ) VALUES (
                    $1, $2, $3, ($4::text)::jsonb, ($5::text)::jsonb,
                    $6, $7, ($8::text)::jsonb,
                    CASE
                        WHEN $9::bigint IS NULL THEN 'infinity'::timestamptz
                        ELSE to_timestamp($9::double precision / 1000.0)
                    END,
                    CASE WHEN $10 = 'retired' THEN now() ELSE NULL END
                )
                ON CONFLICT (client_id) DO NOTHING
                """,
                client_id,
                self.tenant,
                self.project,
                json.dumps(payload["redirect_uris"]),
                json.dumps(payload["grant_types"]),
                payload["token_endpoint_auth_method"],
                payload["application_type"],
                json.dumps(payload["metadata"], sort_keys=True),
                source.expires_at_ms,
                migration_state,
            )
            row = await connection.fetchrow(
                f"""
                SELECT tenant, project, redirect_uris, grant_types,
                       token_endpoint_auth_method, application_type, metadata,
                       retired_at,
                       CASE WHEN isfinite(expires_at)
                            THEN floor(extract(epoch FROM expires_at) * 1000)::bigint
                            ELSE NULL END AS expires_at_ms
                FROM {self.schema}.{TABLE_CLIENTS}
                WHERE client_id = $1
                FOR UPDATE
                """,
                client_id,
            )
            value = dict(row) if row is not None else {}
            actual = {
                "client_id": client_id,
                "redirect_uris": _json_array(value.get("redirect_uris")),
                "grant_types": _json_array(value.get("grant_types")),
                "token_endpoint_auth_method": str(
                    value.get("token_endpoint_auth_method") or "none"
                ),
                "application_type": str(value.get("application_type") or "native"),
                "metadata": _json_object(value.get("metadata")),
            }
            if (
                str(value.get("tenant") or "") != self.tenant
                or str(value.get("project") or "") != self.project
                or ("retired" if value.get("retired_at") is not None else "active")
                != migration_state
                or value.get("expires_at_ms") != source.expires_at_ms
                or actual != payload
            ):
                raise OAuthMigrationTargetConflict("oauth_client_target_conflict")
        return _status_created(status)

    async def _import_refresh(self, source: AuthorityMigrationRecord) -> bool:
        token = str(source.secrets.get("bearer") or "")
        digest = bearer_sha256(token)
        if not token or digest != source.identity:
            raise ValueError("OAuth refresh migration bearer digest mismatch")
        payload = _json_object(source.payload.get("record"))
        if str(source.payload.get("bearer_sha256") or "") != digest:
            raise ValueError("OAuth refresh migration evidence mismatch")
        migration_state = str(
            source.payload.get("migration_state") or ""
        ).strip()
        if migration_state not in {"active", "expired", "revoked"}:
            raise ValueError("OAuth refresh migration state is invalid")
        generation_state = migration_state
        family_id = f"ofam_migration_{digest}"
        generation_id = f"ogen_migration_{digest}"
        async with self._pool.acquire() as connection, connection.transaction():
            family_status = await connection.execute(
                f"""
                INSERT INTO {self.schema}.{TABLE_FAMILIES} (
                    family_id, tenant, project, registry_access_id,
                    card_kind, client_id, subject, identity_scope,
                    current_generation_id, state, expires_at
                ) VALUES (
                    $1, $2, $3, $4,
                    $5, $6, $7, $8,
                    $9, $10, to_timestamp($11::double precision / 1000.0)
                )
                ON CONFLICT (family_id) DO NOTHING
                """,
                family_id,
                self.tenant,
                self.project,
                str(payload.get("registry_access_id") or "").strip(),
                str(payload.get("card_kind") or "").strip(),
                str(payload.get("client_id") or "").strip(),
                str(payload.get("sub") or "").strip(),
                str(payload.get("identity_scope") or "").strip(),
                generation_id,
                migration_state,
                source.expires_at_ms,
            )
            await connection.execute(
                f"""
                INSERT INTO {self.schema}.{TABLE_REFRESH_GENERATIONS} (
                    generation_id, family_id, token_sha256, record,
                    state, expires_at
                ) VALUES (
                    $1, $2, $3, ($4::text)::jsonb,
                    $5, to_timestamp($6::double precision / 1000.0)
                )
                ON CONFLICT DO NOTHING
                """,
                generation_id,
                family_id,
                digest,
                _json_text(payload),
                generation_state,
                source.expires_at_ms,
            )
            row = await connection.fetchrow(
                f"""
                SELECT generation.generation_id, generation.family_id,
                       generation.record, generation.state AS generation_state,
                       floor(extract(epoch FROM generation.expires_at) * 1000)::bigint
                           AS generation_expires_at_ms,
                       family.tenant, family.project,
                       family.registry_access_id, family.card_kind,
                       family.client_id, family.subject, family.identity_scope,
                       family.current_generation_id,
                       family.state AS family_state,
                       floor(extract(epoch FROM family.expires_at) * 1000)::bigint
                           AS family_expires_at_ms
                FROM {self.schema}.{TABLE_REFRESH_GENERATIONS} AS generation
                JOIN {self.schema}.{TABLE_FAMILIES} AS family
                  ON family.family_id = generation.family_id
                WHERE generation.token_sha256 = $1
                FOR UPDATE OF generation, family
                """,
                digest,
            )
            value = dict(row) if row is not None else {}
            if (
                str(value.get("generation_id") or "") != generation_id
                or str(value.get("family_id") or "") != family_id
                or str(value.get("current_generation_id") or "") != generation_id
                or str(value.get("tenant") or "") != self.tenant
                or str(value.get("project") or "") != self.project
                or str(value.get("registry_access_id") or "")
                != str(payload.get("registry_access_id") or "").strip()
                or str(value.get("card_kind") or "")
                != str(payload.get("card_kind") or "").strip()
                or str(value.get("client_id") or "")
                != str(payload.get("client_id") or "").strip()
                or str(value.get("subject") or "")
                != str(payload.get("sub") or "").strip()
                or str(value.get("identity_scope") or "")
                != str(payload.get("identity_scope") or "").strip()
                or str(value.get("generation_state") or "") != generation_state
                or str(value.get("family_state") or "") != migration_state
                or int(value.get("generation_expires_at_ms") or 0)
                != source.expires_at_ms
                or int(value.get("family_expires_at_ms") or 0)
                != source.expires_at_ms
                or _json_object(value.get("record")) != payload
            ):
                raise OAuthMigrationTargetConflict("oauth_refresh_target_conflict")
        return _status_created(family_status)

    async def _import_access(self, source: AuthorityMigrationRecord) -> bool:
        digest = source.identity.lower()
        if len(digest) != 64 or any(value not in "0123456789abcdef" for value in digest):
            raise ValueError("OAuth access migration digest is invalid")
        payload = _json_object(source.payload.get("record"))
        async with self._pool.acquire() as connection, connection.transaction():
            status = await connection.execute(
                f"""
                INSERT INTO {self.schema}.{TABLE_ACCESS_BINDINGS} (
                    token_sha256, tenant, project, registry_access_id,
                    record, state, expires_at
                ) VALUES (
                    $1, $2, $3, $4,
                    ($5::text)::jsonb, 'active',
                    to_timestamp($6::double precision / 1000.0)
                )
                ON CONFLICT (token_sha256) DO NOTHING
                """,
                digest,
                self.tenant,
                self.project,
                str(payload.get("registry_access_id") or "").strip(),
                _json_text(payload),
                source.expires_at_ms,
            )
            row = await connection.fetchrow(
                f"""
                SELECT tenant, project, registry_access_id, record, state,
                       floor(extract(epoch FROM expires_at) * 1000)::bigint
                           AS expires_at_ms
                FROM {self.schema}.{TABLE_ACCESS_BINDINGS}
                WHERE token_sha256 = $1
                FOR UPDATE
                """,
                digest,
            )
            value = dict(row) if row is not None else {}
            if (
                str(value.get("tenant") or "") != self.tenant
                or str(value.get("project") or "") != self.project
                or str(value.get("registry_access_id") or "")
                != str(payload.get("registry_access_id") or "").strip()
                or str(value.get("state") or "") != "active"
                or int(value.get("expires_at_ms") or 0) != source.expires_at_ms
                or _json_object(value.get("record")) != payload
            ):
                raise OAuthMigrationTargetConflict("oauth_access_target_conflict")
        return _status_created(status)

    async def snapshot(self, *, captured_at_ms: int) -> AuthorityMigrationSnapshot:
        async with self._pool.acquire() as connection:
            clients = await connection.fetch(
                f"""
                SELECT client_id, redirect_uris, grant_types,
                       token_endpoint_auth_method, application_type, metadata,
                       retired_at,
                       CASE WHEN isfinite(expires_at)
                            THEN floor(extract(epoch FROM expires_at) * 1000)::bigint
                            ELSE NULL END AS expires_at_ms
                FROM {self.schema}.{TABLE_CLIENTS}
                WHERE (retired_at IS NOT NULL)
                   OR expires_at > to_timestamp($1::double precision / 1000.0)
                ORDER BY client_id
                """,
                int(captured_at_ms),
            )
            refresh = await connection.fetch(
                f"""
                SELECT generation.token_sha256, generation.record,
                       generation.state AS generation_state,
                       family.state AS family_state,
                       floor(extract(epoch FROM generation.expires_at) * 1000)::bigint
                           AS expires_at_ms
                FROM {self.schema}.{TABLE_REFRESH_GENERATIONS} AS generation
                JOIN {self.schema}.{TABLE_FAMILIES} AS family
                  ON family.family_id = generation.family_id
                 AND family.current_generation_id = generation.generation_id
                ORDER BY generation.token_sha256
                """
            )
            access = await connection.fetch(
                f"""
                SELECT token_sha256, record,
                       floor(extract(epoch FROM expires_at) * 1000)::bigint
                           AS expires_at_ms
                FROM {self.schema}.{TABLE_ACCESS_BINDINGS}
                WHERE state = 'active' AND expires_at > now()
                ORDER BY token_sha256
                """
            )
        records: list[AuthorityMigrationRecord] = []
        for row in clients:
            value = dict(row)
            client_id = str(value.get("client_id") or "")
            records.append(
                AuthorityMigrationRecord(
                    record_type="oauth_client",
                    identity=client_id,
                    families=(FAMILY_OAUTH_CLIENTS,),
                    payload={
                        "record": {
                            "client_id": client_id,
                            "redirect_uris": _json_array(value.get("redirect_uris")),
                            "grant_types": _json_array(value.get("grant_types")),
                            "token_endpoint_auth_method": str(
                                value.get("token_endpoint_auth_method") or "none"
                            ),
                            "application_type": str(
                                value.get("application_type") or "native"
                            ),
                            "metadata": _json_object(value.get("metadata")),
                        },
                        "migration_state": (
                            "retired"
                            if value.get("retired_at") is not None
                            else "active"
                        ),
                    },
                    expires_at_ms=(
                        int(value["expires_at_ms"])
                        if value.get("expires_at_ms") is not None
                        else None
                    ),
                )
            )
        for row in refresh:
            value = dict(row)
            digest = str(value.get("token_sha256") or "")
            records.append(
                AuthorityMigrationRecord(
                    record_type="oauth_refresh",
                    identity=digest,
                    families=(FAMILY_OAUTH_REFRESH,),
                    payload={
                        "bearer_sha256": digest,
                        "migration_state": str(
                            value.get("family_state") or ""
                        ),
                        "record": _json_object(value.get("record")),
                    },
                    expires_at_ms=int(value.get("expires_at_ms") or 0),
                )
            )
        for row in access:
            value = dict(row)
            records.append(
                AuthorityMigrationRecord(
                    record_type="oauth_access",
                    identity=str(value.get("token_sha256") or ""),
                    families=(FAMILY_OAUTH_ACCESS,),
                    payload={"record": _json_object(value.get("record"))},
                    expires_at_ms=int(value.get("expires_at_ms") or 0),
                )
            )
        return AuthorityMigrationSnapshot(
            tenant=self.tenant,
            project=self.project,
            records=records,
            declared_families=(
                FAMILY_OAUTH_CLIENTS,
                FAMILY_OAUTH_REFRESH,
                FAMILY_OAUTH_ACCESS,
            ),
            captured_at_ms=int(captured_at_ms),
        ).validated()


__all__ = ["OAuthMigrationTargetConflict", "PostgresOAuthMigrationTarget"]

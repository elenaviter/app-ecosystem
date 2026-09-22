from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from connection_hub.delegated_credentials.oauth.authority_schema import (
    TABLE_ACCESS_BINDINGS,
    TABLE_CLIENTS,
    TABLE_FAMILIES,
    TABLE_REFRESH_GENERATIONS,
    oauth_authority_schema,
    oauth_authority_schema_sql,
)


def bearer_sha256(value: str) -> str:
    """Return the non-recoverable identity of a high-entropy bearer."""

    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


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


def _json_array(value: Any) -> list[Any]:
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    return list(value) if isinstance(value, (list, tuple)) else []


@dataclass(frozen=True)
class RefreshTokenState:
    """Live refresh generation resolved from an authority store.

    ``raw`` is a store-owned compare token. Redis uses the encoded record;
    PostgreSQL uses the immutable generation id. It never contains the bearer.
    """

    token: str
    raw: Any
    record: dict[str, Any]


class RefreshTokenReuseDetected(RuntimeError):
    """A consumed refresh generation was presented again."""


class OAuthAuthorityStore(Protocol):
    async def create_refresh_token(
        self, record: Mapping[str, Any], *, ttl_seconds: int
    ) -> str: ...

    async def get_refresh_token_state(
        self, refresh_token: str
    ) -> RefreshTokenState | None: ...

    async def rotate_refresh_token(
        self,
        refresh_token: str,
        replacement: Mapping[str, Any],
        *,
        ttl_seconds: int,
        expected_generation: Any = None,
    ) -> str | None: ...

    async def revoke_refresh_token(self, refresh_token: str) -> bool: ...

    async def extend_refresh_token(
        self, refresh_token: str, ttl_seconds: int
    ) -> bool: ...

    async def register_client(
        self, record: Mapping[str, Any], *, ttl_seconds: int
    ) -> dict[str, Any]: ...

    async def get_client_record(
        self, client_id: str, *, ttl_seconds: int
    ) -> dict[str, Any] | None: ...

    async def bind_access_grant(
        self,
        access_token: str,
        record: Mapping[str, Any],
        *,
        ttl_seconds: int,
    ) -> None: ...

    async def get_access_grant_record(
        self, access_token: str
    ) -> dict[str, Any] | None: ...

    async def extend_access_grant(
        self, access_token: str, ttl_seconds: int
    ) -> bool: ...

    async def revoke_access_grant(self, access_token: str) -> bool: ...


class PostgresOAuthAuthorityStore:
    """Transactional PostgreSQL authority for OAuth clients and credentials.

    Bearers are hashed before entering a SQL argument. Rotation locks and
    advances one credential family in a single transaction, so a failed insert
    cannot consume the prior generation.
    """

    def __init__(self, *, pg_pool: Any, tenant: str, project: str) -> None:
        if pg_pool is None:
            raise RuntimeError("PostgresOAuthAuthorityStore requires pg_pool")
        self._pool = pg_pool
        self.tenant = str(tenant or "").strip() or "default"
        self.project = str(project or "").strip() or "default"
        self.schema = oauth_authority_schema(
            tenant=self.tenant,
            project=self.project,
        )

    async def ensure_schema(self) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(oauth_authority_schema_sql(self.schema))

    async def _revoke_refresh_family(
        self,
        connection: Any,
        family_id: str,
    ) -> None:
        await connection.execute(
            f"""
            UPDATE {self.schema}.{TABLE_FAMILIES}
            SET state = 'revoked',
                revision = revision + 1,
                updated_at = now()
            WHERE family_id = $1 AND state = 'active'
            """,
            family_id,
        )
        await connection.execute(
            f"""
            UPDATE {self.schema}.{TABLE_REFRESH_GENERATIONS}
            SET state = 'revoked',
                revision = revision + 1,
                revoked_at = now()
            WHERE family_id = $1 AND state = 'active'
            """,
            family_id,
        )

    async def create_refresh_token(
        self,
        record: Mapping[str, Any],
        *,
        ttl_seconds: int,
    ) -> str:
        token = secrets.token_urlsafe(40)
        family_id = f"ofam_{uuid.uuid4().hex}"
        generation_id = f"ogen_{uuid.uuid4().hex}"
        payload = dict(record)
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    f"""
                    INSERT INTO {self.schema}.{TABLE_FAMILIES} (
                        family_id, tenant, project, registry_access_id,
                        card_kind, client_id, subject, identity_scope,
                        current_generation_id, state, expires_at
                    ) VALUES (
                        $1, $2, $3, $4,
                        $5, $6, $7, $8,
                        $9, 'active', now() + ($10 * interval '1 second')
                    )
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
                    max(1, int(ttl_seconds)),
                )
                await connection.execute(
                    f"""
                    INSERT INTO {self.schema}.{TABLE_REFRESH_GENERATIONS} (
                        generation_id, family_id, token_sha256, record,
                        state, expires_at
                    ) VALUES (
                        $1, $2, $3, ($4::text)::jsonb,
                        'active', now() + ($5 * interval '1 second')
                    )
                    """,
                    generation_id,
                    family_id,
                    bearer_sha256(token),
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    max(1, int(ttl_seconds)),
                )
        return token

    async def get_refresh_token_state(
        self,
        refresh_token: str,
    ) -> RefreshTokenState | None:
        token = str(refresh_token or "").strip()
        if not token:
            return None
        reuse_detected = False
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    f"""
                    SELECT generation.generation_id,
                           generation.family_id,
                           generation.record,
                           generation.state AS generation_state,
                           generation.expires_at > now() AS generation_live,
                           family.state AS family_state,
                           family.expires_at > now() AS family_live
                    FROM {self.schema}.{TABLE_REFRESH_GENERATIONS} AS generation
                    JOIN {self.schema}.{TABLE_FAMILIES} AS family
                      ON family.family_id = generation.family_id
                    WHERE generation.token_sha256 = $1
                    FOR UPDATE OF generation, family
                    """,
                    bearer_sha256(token),
                )
                if row is not None:
                    value = dict(row)
                    if (
                        str(value.get("generation_state") or "") == "consumed"
                        and str(value.get("family_state") or "") == "active"
                    ):
                        family_id = str(value.get("family_id") or "")
                        await self._revoke_refresh_family(connection, family_id)
                        reuse_detected = True
        if reuse_detected:
            raise RefreshTokenReuseDetected(
                "consumed refresh generation was presented again"
            )
        if row is None:
            return None
        raw = dict(row)
        if (
            str(raw.get("generation_state") or "") != "active"
            or str(raw.get("family_state") or "") != "active"
            or not bool(raw.get("generation_live"))
            or not bool(raw.get("family_live"))
        ):
            return None
        generation_id = str(raw.get("generation_id") or "").strip()
        record = _json_object(raw.get("record"))
        if not generation_id or not record:
            return None
        return RefreshTokenState(
            token=token,
            raw=generation_id,
            record=record,
        )

    async def rotate_refresh_token(
        self,
        refresh_token: str,
        replacement: Mapping[str, Any],
        *,
        ttl_seconds: int,
        expected_generation: Any = None,
    ) -> str | None:
        token = str(refresh_token or "").strip()
        if not token:
            return None
        new_token = secrets.token_urlsafe(40)
        generation_id = f"ogen_{uuid.uuid4().hex}"
        reuse_detected = False
        rotated = False
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    f"""
                    SELECT generation.generation_id,
                           generation.family_id,
                           generation.state AS generation_state,
                           generation.expires_at > now() AS generation_live,
                           family.state AS family_state,
                           family.expires_at > now() AS family_live
                    FROM {self.schema}.{TABLE_REFRESH_GENERATIONS} AS generation
                    JOIN {self.schema}.{TABLE_FAMILIES} AS family
                      ON family.family_id = generation.family_id
                    WHERE generation.token_sha256 = $1
                    FOR UPDATE OF generation, family
                    """,
                    bearer_sha256(token),
                )
                if row is None:
                    return None
                current = dict(row)
                current_generation = str(
                    current.get("generation_id") or ""
                ).strip()
                family_id = str(current.get("family_id") or "").strip()
                if (
                    str(current.get("generation_state") or "") == "consumed"
                    and str(current.get("family_state") or "") == "active"
                ):
                    await self._revoke_refresh_family(connection, family_id)
                    reuse_detected = True
                elif (
                    str(current.get("generation_state") or "") != "active"
                    or str(current.get("family_state") or "") != "active"
                    or not bool(current.get("generation_live"))
                    or not bool(current.get("family_live"))
                    or (
                        expected_generation is not None
                        and str(expected_generation) != current_generation
                    )
                ):
                    return None
                else:
                    await connection.execute(
                        f"""
                        UPDATE {self.schema}.{TABLE_REFRESH_GENERATIONS}
                        SET state = 'consumed',
                            revision = revision + 1,
                            consumed_at = now()
                        WHERE generation_id = $1 AND state = 'active'
                        """,
                        current_generation,
                    )
                    await connection.execute(
                        f"""
                        INSERT INTO {self.schema}.{TABLE_REFRESH_GENERATIONS} (
                            generation_id, family_id, token_sha256, record,
                            state, expires_at
                        ) VALUES (
                            $1, $2, $3, ($4::text)::jsonb,
                            'active', now() + ($5 * interval '1 second')
                        )
                        """,
                        generation_id,
                        family_id,
                        bearer_sha256(new_token),
                        json.dumps(
                            dict(replacement),
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        max(1, int(ttl_seconds)),
                    )
                    await connection.execute(
                        f"""
                        UPDATE {self.schema}.{TABLE_FAMILIES}
                        SET current_generation_id = $2,
                            revision = revision + 1,
                            updated_at = now(),
                            expires_at = now() + ($3 * interval '1 second')
                        WHERE family_id = $1 AND state = 'active'
                        """,
                        family_id,
                        generation_id,
                        max(1, int(ttl_seconds)),
                    )
                    rotated = True
        if reuse_detected:
            raise RefreshTokenReuseDetected(
                "consumed refresh generation was presented again"
            )
        return new_token if rotated else None

    async def revoke_refresh_token(self, refresh_token: str) -> bool:
        token = str(refresh_token or "").strip()
        if not token:
            return False
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    f"""
                    SELECT generation.generation_id, generation.family_id
                    FROM {self.schema}.{TABLE_REFRESH_GENERATIONS} AS generation
                    JOIN {self.schema}.{TABLE_FAMILIES} AS family
                      ON family.family_id = generation.family_id
                    WHERE generation.token_sha256 = $1
                      AND generation.state = 'active'
                      AND family.state = 'active'
                    FOR UPDATE OF generation, family
                    """,
                    bearer_sha256(token),
                )
                if row is None:
                    return False
                current = dict(row)
                generation_id = str(current.get("generation_id") or "")
                family_id = str(current.get("family_id") or "")
                await connection.execute(
                    f"""
                    UPDATE {self.schema}.{TABLE_REFRESH_GENERATIONS}
                    SET state = 'revoked',
                        revision = revision + 1,
                        revoked_at = now()
                    WHERE generation_id = $1 AND state = 'active'
                    """,
                    generation_id,
                )
                await connection.execute(
                    f"""
                    UPDATE {self.schema}.{TABLE_FAMILIES}
                    SET state = 'revoked',
                        revision = revision + 1,
                        updated_at = now()
                    WHERE family_id = $1 AND state = 'active'
                    """,
                    family_id,
                )
        return True

    async def extend_refresh_token(
        self,
        refresh_token: str,
        ttl_seconds: int,
    ) -> bool:
        token = str(refresh_token or "").strip()
        if not token:
            return False
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    f"""
                    SELECT generation.generation_id, generation.family_id
                    FROM {self.schema}.{TABLE_REFRESH_GENERATIONS} AS generation
                    JOIN {self.schema}.{TABLE_FAMILIES} AS family
                      ON family.family_id = generation.family_id
                    WHERE generation.token_sha256 = $1
                      AND generation.state = 'active'
                      AND family.state = 'active'
                      AND generation.expires_at > now()
                      AND family.expires_at > now()
                    FOR UPDATE OF generation, family
                    """,
                    bearer_sha256(token),
                )
                if row is None:
                    return False
                current = dict(row)
                seconds = max(1, int(ttl_seconds))
                await connection.execute(
                    f"""
                    UPDATE {self.schema}.{TABLE_REFRESH_GENERATIONS}
                    SET expires_at = now() + ($2 * interval '1 second'),
                        revision = revision + 1
                    WHERE generation_id = $1 AND state = 'active'
                    """,
                    str(current.get("generation_id") or ""),
                    seconds,
                )
                await connection.execute(
                    f"""
                    UPDATE {self.schema}.{TABLE_FAMILIES}
                    SET expires_at = now() + ($2 * interval '1 second'),
                        revision = revision + 1,
                        updated_at = now()
                    WHERE family_id = $1 AND state = 'active'
                    """,
                    str(current.get("family_id") or ""),
                    seconds,
                )
        return True

    async def register_client(
        self,
        record: Mapping[str, Any],
        *,
        ttl_seconds: int,
    ) -> dict[str, Any]:
        payload = dict(record)
        async with self._pool.acquire() as connection:
            await connection.execute(
                f"""
                INSERT INTO {self.schema}.{TABLE_CLIENTS} (
                    client_id, tenant, project, redirect_uris,
                    token_endpoint_auth_method, application_type, metadata,
                    expires_at
                ) VALUES (
                    $1, $2, $3, ($4::text)::jsonb,
                    $5, $6, ($7::text)::jsonb,
                    now() + ($8 * interval '1 second')
                )
                """,
                str(payload.get("client_id") or "").strip(),
                self.tenant,
                self.project,
                json.dumps(list(payload.get("redirect_uris") or [])),
                str(payload.get("token_endpoint_auth_method") or "none"),
                str(payload.get("application_type") or "native"),
                json.dumps(dict(payload.get("metadata") or {}), sort_keys=True),
                max(1, int(ttl_seconds)),
            )
        return payload

    async def get_client_record(
        self,
        client_id: str,
        *,
        ttl_seconds: int,
    ) -> dict[str, Any] | None:
        client = str(client_id or "").strip()
        if not client:
            return None
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                WITH candidate AS (
                    SELECT client_id, redirect_uris,
                           token_endpoint_auth_method,
                           application_type, metadata, expires_at
                    FROM {self.schema}.{TABLE_CLIENTS}
                    WHERE client_id = $1
                      AND retired_at IS NULL
                      AND expires_at > now()
                    FOR UPDATE
                ), extended AS (
                    UPDATE {self.schema}.{TABLE_CLIENTS} AS oauth_client
                    SET expires_at = now() + ($2 * interval '1 second'),
                        revision = revision + 1
                    FROM candidate
                    WHERE oauth_client.client_id = candidate.client_id
                      AND candidate.expires_at < now() + (
                          ($2 * interval '1 second') / 2
                      )
                    RETURNING oauth_client.client_id,
                              oauth_client.redirect_uris,
                              oauth_client.token_endpoint_auth_method,
                              oauth_client.application_type,
                              oauth_client.metadata
                )
                SELECT client_id, redirect_uris,
                       token_endpoint_auth_method,
                       application_type, metadata
                FROM extended
                UNION ALL
                SELECT client_id, redirect_uris,
                       token_endpoint_auth_method,
                       application_type, metadata
                FROM candidate
                WHERE NOT EXISTS (SELECT 1 FROM extended)
                LIMIT 1
                """,
                client,
                max(1, int(ttl_seconds)),
            )
        if row is None:
            return None
        raw = dict(row)
        return {
            "client_id": str(raw.get("client_id") or ""),
            "redirect_uris": _json_array(raw.get("redirect_uris")),
            "token_endpoint_auth_method": str(
                raw.get("token_endpoint_auth_method") or "none"
            ),
            "application_type": str(raw.get("application_type") or "native"),
            "metadata": _json_object(raw.get("metadata")),
        }

    async def bind_access_grant(
        self,
        access_token: str,
        record: Mapping[str, Any],
        *,
        ttl_seconds: int,
    ) -> None:
        token = str(access_token or "").strip()
        if not token:
            raise ValueError("access_token is required")
        payload = dict(record)
        async with self._pool.acquire() as connection:
            await connection.execute(
                f"""
                INSERT INTO {self.schema}.{TABLE_ACCESS_BINDINGS} (
                    token_sha256, tenant, project, registry_access_id,
                    record, state, expires_at
                ) VALUES (
                    $1, $2, $3, $4,
                    ($5::text)::jsonb, 'active',
                    now() + ($6 * interval '1 second')
                )
                ON CONFLICT (token_sha256) DO UPDATE SET
                    registry_access_id = EXCLUDED.registry_access_id,
                    record = EXCLUDED.record,
                    revision = {TABLE_ACCESS_BINDINGS}.revision + 1,
                    updated_at = now(),
                    expires_at = EXCLUDED.expires_at
                WHERE {TABLE_ACCESS_BINDINGS}.state <> 'revoked'
                """,
                bearer_sha256(token),
                self.tenant,
                self.project,
                str(payload.get("registry_access_id") or "").strip(),
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                max(1, int(ttl_seconds)),
            )

    async def get_access_grant_record(
        self,
        access_token: str,
    ) -> dict[str, Any] | None:
        token = str(access_token or "").strip()
        if not token:
            return None
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                SELECT record
                FROM {self.schema}.{TABLE_ACCESS_BINDINGS}
                WHERE token_sha256 = $1
                  AND state = 'active'
                  AND expires_at > now()
                """,
                bearer_sha256(token),
            )
        if row is None:
            return None
        return _json_object(dict(row).get("record"))

    async def extend_access_grant(
        self,
        access_token: str,
        ttl_seconds: int,
    ) -> bool:
        token = str(access_token or "").strip()
        if not token:
            return False
        async with self._pool.acquire() as connection:
            status = await connection.execute(
                f"""
                UPDATE {self.schema}.{TABLE_ACCESS_BINDINGS}
                SET expires_at = now() + ($2 * interval '1 second'),
                    revision = revision + 1,
                    updated_at = now()
                WHERE token_sha256 = $1
                  AND state = 'active'
                  AND expires_at > now()
                """,
                bearer_sha256(token),
                max(1, int(ttl_seconds)),
            )
        return status != "UPDATE 0"

    async def revoke_access_grant(self, access_token: str) -> bool:
        token = str(access_token or "").strip()
        if not token:
            return False
        async with self._pool.acquire() as connection:
            status = await connection.execute(
                f"""
                UPDATE {self.schema}.{TABLE_ACCESS_BINDINGS}
                SET state = 'revoked',
                    revision = revision + 1,
                    revoked_at = now(),
                    updated_at = now()
                WHERE token_sha256 = $1 AND state = 'active'
                """,
                bearer_sha256(token),
            )
        return status != "UPDATE 0"

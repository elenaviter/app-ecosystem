from __future__ import annotations

import json
import secrets
import uuid
from dataclasses import dataclass
import logging
from typing import Any, Mapping, Protocol

from connection_hub.delegated_credentials.devices.authority import (
    PostgresProfileDeviceAuthority,
)
from connection_hub.delegated_credentials.devices.authority_schema import (
    profile_device_authority_schema_sql,
)
from connection_hub.delegated_credentials.oauth.authority_schema import (
    TABLE_ACCESS_BINDINGS,
    TABLE_CLIENTS,
    TABLE_FAMILIES,
    TABLE_REFRESH_GENERATIONS,
    oauth_authority_schema,
    oauth_authority_schema_sql,
)
from connection_hub.delegated_credentials.oauth.bearers import bearer_sha256
from connection_hub.delegated_credentials.oauth.device import DEVICE_GRANT_TYPE

LOGGER = logging.getLogger(__name__)


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


# W408: how long after a lost refresh response a retry of that same refresh
# is recognised. The relay's channel backoff retries at 60, 180, 420 and 900 s
# after a failure (cumulative, doubling from 60 s), and each retry can wait up
# to one 60 s relay cycle more. 1200 s covers the fourth retry at 900 s plus
# four cycles (1140 s) with slack. The live retry came 105 s after the loss.
REFRESH_RETRY_WINDOW_SECONDS = 1200

# W408 review: how many times one attempt may be retried. Each retry replaces
# the previous unused successor, so repeated post-send losses (a flaky tunnel)
# do not end the family. The cap bounds what one attempt id can mint.
MAX_REFRESH_RETRIES = 5


def refresh_request_fingerprint(
    *,
    refresh_attempt: str,
    client_id: str,
    resource: str,
    scope: str,
) -> str:
    """What binds a refresh retry to the request it retries, or empty (W408).

    The client's attempt id together with the client, resource and requested
    scope of the request. Only this hash is stored, never the attempt id.
    """

    attempt = str(refresh_attempt or "").strip()
    if not attempt or len(attempt) > 256:
        return ""
    return bearer_sha256(
        json.dumps(
            [attempt, str(client_id or ""), str(resource or ""), str(scope or "")],
            separators=(",", ":"),
        )
    )


@dataclass(frozen=True)
class RefreshTokenState:
    """Live refresh generation resolved from an authority store.

    ``raw`` is a store-owned compare token. Redis uses the encoded record;
    PostgreSQL uses the immutable generation id. It never contains the bearer.
    ``retry_of`` names the unused successor a retried refresh replaces
    (W408), and is empty for an ordinary live generation.
    """

    token: str
    raw: Any
    record: dict[str, Any]
    retry_of: str = ""


class RefreshTokenReuseDetected(RuntimeError):
    """A consumed refresh generation was presented again."""


class OAuthAuthorityStore(Protocol):
    async def create_refresh_token(
        self, record: Mapping[str, Any], *, ttl_seconds: int
    ) -> str: ...

    async def get_refresh_token_state(
        self, refresh_token: str, *, refresh_request_fingerprint: str = ""
    ) -> RefreshTokenState | None: ...

    async def rotate_refresh_token(
        self,
        refresh_token: str,
        replacement: Mapping[str, Any],
        *,
        ttl_seconds: int,
        expected_generation: Any = None,
        refresh_request_fingerprint: str = "",
    ) -> str | None: ...

    async def rollback_refresh_token_rotation(
        self,
        refresh_token: str,
        replacement_token: str,
        *,
        expected_generation: Any = None,
        expected_replacement_generation: Any = None,
    ) -> bool: ...

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

    async def revoke_access_grant_by_digest(self, token_sha256: str) -> str: ...

    async def extend_card_credentials(
        self, registry_access_id: str, ttl_seconds: int
    ) -> bool: ...

    async def set_card_credentials_expiry(
        self, registry_access_id: str, expires_at: int
    ) -> str: ...

    async def revoke_card_credentials(self, registry_access_id: str) -> bool: ...

    async def card_continuity_proven(
        self, *, refresh_token: str, client_id: str, access_id: str
    ) -> bool: ...


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
        self.profile_devices = PostgresProfileDeviceAuthority(
            pg_pool=self._pool,
            schema=self.schema,
            tenant=self.tenant,
            project=self.project,
        )

    async def ensure_schema(self) -> None:
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(oauth_authority_schema_sql(self.schema))
                await connection.execute(
                    profile_device_authority_schema_sql(self.schema)
                )

    async def card_continuity_proven(
        self,
        *,
        refresh_token: str,
        client_id: str,
        access_id: str,
    ) -> bool:
        """Whether a refresh token is one this Card's credential family issued (W414).

        A device login that re-authorizes an existing Card must come from the
        machine that held it. The proof is the Card's last refresh token: its
        hash is one of the family's generations, for this client and this
        Card. A revoked or expired family still proves continuity, because
        revocation only marks rows, and consent is still required.
        """

        token = str(refresh_token or "")
        client = str(client_id or "").strip()
        access = str(access_id or "").strip()
        if not token or not client or not access:
            return False
        async with self._pool.acquire() as connection:
            found = await connection.fetchval(
                f"""
                SELECT 1
                  FROM {self.schema}.{TABLE_REFRESH_GENERATIONS} generation
                  JOIN {self.schema}.{TABLE_FAMILIES} family
                    ON family.family_id = generation.family_id
                 WHERE generation.token_sha256 = $1
                   AND family.client_id = $2
                   AND family.registry_access_id = $3
                   AND family.tenant = $4
                   AND family.project = $5
                 LIMIT 1
                """,
                bearer_sha256(token),
                client,
                access,
                self.tenant,
                self.project,
            )
        return found is not None

    async def grant_device_to_public_native_clients(self) -> int:
        """W414 release migration (operator, 2026-09-30): the device grant for existing clients.

        Every stored public native dynamic client (``dcr-`` id, application
        type ``native``, token endpoint authentication ``none``) that lacks the
        device grant gains it, and nothing else about the row changes: not its
        revision, redirect URIs, metadata, last use or expiry. Idempotent; it
        logs how many rows it changed.

        It is never part of :meth:`ensure_schema`. A handler older than the
        W414 Card continuity check would let a migrated client re-authorize
        any Card without proof, so the release runs this explicitly, from the
        KDCube side, only after every process serves the checked handler.
        """

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                return await self._grant_device_to_public_native_clients(connection)

    async def _grant_device_to_public_native_clients(self, connection: Any) -> int:
        status = await connection.execute(
            f"""
            UPDATE {self.schema}.{TABLE_CLIENTS}
               SET grant_types = grant_types || to_jsonb($1::text)
             WHERE client_id LIKE 'dcr-%'
               AND application_type = 'native'
               AND token_endpoint_auth_method = 'none'
               AND jsonb_typeof(grant_types) = 'array'
               AND NOT grant_types ? $1
            """,
            DEVICE_GRANT_TYPE,
        )
        changed = int(str(status or "UPDATE 0").rsplit(" ", 1)[-1] or 0)
        LOGGER.info(
            "[connection_hub.oauth] device_grant_migration schema=%s clients_updated=%d",
            self.schema,
            changed,
        )
        return changed

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

    async def _lock_family_of_token(self, connection: Any, token_sha256: str) -> None:
        """Lock the family of the generation a token names, before any generation row.

        Every transaction that locks refresh rows takes the family first, by
        id, so two requests on one family queue on that row and never lock
        generations in opposite orders. A retried refresh locked its successor
        after the presented generation while an ordinary refresh of that
        successor locked successor then family, and PostgreSQL aborted one of
        them as a deadlock (W408 review, 2026-10-01).
        """

        # One statement: the subquery reads the family id without locking the
        # generation, then only the family row is locked.
        await connection.execute(
            f"""
            SELECT 1
            FROM {self.schema}.{TABLE_FAMILIES}
            WHERE family_id = (
                SELECT family_id
                FROM {self.schema}.{TABLE_REFRESH_GENERATIONS}
                WHERE token_sha256 = $1
            )
            FOR UPDATE
            """,
            token_sha256,
        )

    async def _lock_card_families(self, connection: Any, registry_access_id: str) -> None:
        """Lock one Card's families in id order, before any of their generations."""

        await connection.execute(
            f"""
            SELECT 1
            FROM {self.schema}.{TABLE_FAMILIES}
            WHERE registry_access_id = $1
            ORDER BY family_id
            FOR UPDATE
            """,
            registry_access_id,
        )

    async def _retried_successor(
        self,
        connection: Any,
        *,
        family_id: str,
        presented_generation: str,
        fingerprint: str,
    ) -> tuple[str, int]:
        """The unused successor a retried refresh may replace, and its retry count (W408).

        A refresh whose response was lost leaves the client holding the
        generation it sent, now consumed. Presenting it again is a retry of
        that same refresh only when every one of these holds: the request
        carries the fingerprint of the request that minted the family's
        current generation, that generation's parent is the presented one,
        it was never used, fewer than ``MAX_REFRESH_RETRIES`` retries minted
        it, the family is live, and the presented generation was consumed
        within ``REFRESH_RETRY_WINDOW_SECONDS``. The window runs from that
        first consumption, so retries never extend it. Anything else is reuse.
        Returns ``("", 0)`` when it is not a retry.
        """

        if not fingerprint:
            return "", 0
        row = await connection.fetchrow(
            f"""
            SELECT successor.generation_id,
                   COALESCE((successor.record->>'refresh_retry_count')::int, 0) AS retries
            FROM {self.schema}.{TABLE_FAMILIES} AS family
            JOIN {self.schema}.{TABLE_REFRESH_GENERATIONS} AS successor
              ON successor.generation_id = family.current_generation_id
            JOIN {self.schema}.{TABLE_REFRESH_GENERATIONS} AS presented
              ON presented.generation_id = $2
             AND presented.family_id = family.family_id
            WHERE family.family_id = $1
              AND family.state = 'active'
              AND family.expires_at > now()
              AND successor.state = 'active'
              AND successor.expires_at > now()
              AND successor.record->>'parent_generation_id' = $2
              AND successor.record->>'refresh_request_sha256' = $3
              AND COALESCE((successor.record->>'refresh_retry_count')::int, 0) < $5
              AND presented.state = 'consumed'
              AND presented.consumed_at > now() - ($4 * interval '1 second')
            FOR UPDATE OF successor
            """,
            family_id,
            presented_generation,
            fingerprint,
            REFRESH_RETRY_WINDOW_SECONDS,
            MAX_REFRESH_RETRIES,
        )
        if row is None:
            return "", 0
        return str(row["generation_id"] or ""), int(row["retries"] or 0)

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
        *,
        refresh_request_fingerprint: str = "",
    ) -> RefreshTokenState | None:
        token = str(refresh_token or "").strip()
        if not token:
            return None
        fingerprint = str(refresh_request_fingerprint or "")
        retry_of = ""
        reuse_detected = False
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await self._lock_family_of_token(connection, bearer_sha256(token))
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
                        retry_of, _retries = await self._retried_successor(
                            connection,
                            family_id=family_id,
                            presented_generation=str(value.get("generation_id") or ""),
                            fingerprint=fingerprint,
                        )
                        if not retry_of:
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
            (str(raw.get("generation_state") or "") != "active" and not retry_of)
            or str(raw.get("family_state") or "") != "active"
            or (not bool(raw.get("generation_live")) and not retry_of)
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
            retry_of=retry_of,
        )

    async def rotate_refresh_token(
        self,
        refresh_token: str,
        replacement: Mapping[str, Any],
        *,
        ttl_seconds: int,
        expected_generation: Any = None,
        refresh_request_fingerprint: str = "",
    ) -> str | None:
        token = str(refresh_token or "").strip()
        if not token:
            return None
        fingerprint = str(refresh_request_fingerprint or "")
        new_token = secrets.token_urlsafe(40)
        generation_id = f"ogen_{uuid.uuid4().hex}"
        reuse_detected = False
        rotated = False
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await self._lock_family_of_token(connection, bearer_sha256(token))
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
                retry_of = ""
                retries = 0
                if (
                    str(current.get("generation_state") or "") == "consumed"
                    and str(current.get("family_state") or "") == "active"
                ):
                    retry_of, retries = await self._retried_successor(
                        connection,
                        family_id=family_id,
                        presented_generation=current_generation,
                        fingerprint=fingerprint,
                    )
                    if not retry_of:
                        await self._revoke_refresh_family(connection, family_id)
                        reuse_detected = True
                    elif (
                        expected_generation is not None
                        and str(expected_generation) != current_generation
                    ):
                        return None
                    else:
                        # W408: the retried refresh replaces its own unused
                        # successor, and a new successor is minted below.
                        await connection.execute(
                            f"""
                            UPDATE {self.schema}.{TABLE_REFRESH_GENERATIONS}
                            SET state = 'revoked',
                                revision = revision + 1,
                                revoked_at = now()
                            WHERE generation_id = $1 AND state = 'active'
                            """,
                            retry_of,
                        )
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
                if not reuse_detected:
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
                            {
                                **dict(replacement),
                                # W408: which generation and which client
                                # attempt minted this one, so a retry of a
                                # refresh whose response was lost is known.
                                "parent_generation_id": current_generation,
                                "refresh_request_sha256": fingerprint,
                                # How many retries of this attempt minted it,
                                # capped by MAX_REFRESH_RETRIES.
                                "refresh_retry_count": (retries + 1) if retry_of else 0,
                            },
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

    async def rollback_refresh_token_rotation(
        self,
        refresh_token: str,
        replacement_token: str,
        *,
        expected_generation: Any = None,
        expected_replacement_generation: Any = None,
    ) -> bool:
        """Restore a generation when its replacement was never delivered.

        The replacement must still be this active family's current generation.
        A concurrent rotation, revocation, or reuse decision therefore wins and
        can never be undone by failure compensation.
        """

        token = str(refresh_token or "").strip()
        replacement = str(replacement_token or "").strip()
        if not token or not replacement or token == replacement:
            return False
        restored = False
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await self._lock_family_of_token(connection, bearer_sha256(token))
                row = await connection.fetchrow(
                    f"""
                    SELECT prior.generation_id AS prior_generation_id,
                           prior.state AS prior_state,
                           successor.generation_id AS replacement_generation_id,
                           successor.state AS replacement_state,
                           successor.expires_at AS replacement_expires_at,
                           successor.expires_at > now() AS replacement_live,
                           family.family_id,
                           family.current_generation_id,
                           family.state AS family_state,
                           family.expires_at > now() AS family_live
                    FROM {self.schema}.{TABLE_REFRESH_GENERATIONS} AS prior
                    JOIN {self.schema}.{TABLE_FAMILIES} AS family
                      ON family.family_id = prior.family_id
                    JOIN {self.schema}.{TABLE_REFRESH_GENERATIONS} AS successor
                      ON successor.family_id = family.family_id
                    WHERE prior.token_sha256 = $1
                      AND successor.token_sha256 = $2
                    FOR UPDATE OF prior, successor, family
                    """,
                    bearer_sha256(token),
                    bearer_sha256(replacement),
                )
                if row is None:
                    return False
                current = dict(row)
                prior_generation = str(
                    current.get("prior_generation_id") or ""
                ).strip()
                replacement_generation = str(
                    current.get("replacement_generation_id") or ""
                ).strip()
                if (
                    str(current.get("prior_state") or "") != "consumed"
                    or str(current.get("replacement_state") or "") != "active"
                    or str(current.get("family_state") or "") != "active"
                    or not bool(current.get("replacement_live"))
                    or not bool(current.get("family_live"))
                    or str(current.get("current_generation_id") or "")
                    != replacement_generation
                    or (
                        expected_generation is not None
                        and str(expected_generation) != prior_generation
                    )
                    or (
                        expected_replacement_generation is not None
                        and str(expected_replacement_generation)
                        != replacement_generation
                    )
                ):
                    return False
                replacement_expires_at = current.get("replacement_expires_at")
                family_id = str(current.get("family_id") or "").strip()
                replacement_status = await connection.execute(
                    f"""
                    UPDATE {self.schema}.{TABLE_REFRESH_GENERATIONS}
                    SET state = 'revoked',
                        revision = revision + 1,
                        revoked_at = now()
                    WHERE generation_id = $1 AND state = 'active'
                    """,
                    replacement_generation,
                )
                prior_status = await connection.execute(
                    f"""
                    UPDATE {self.schema}.{TABLE_REFRESH_GENERATIONS}
                    SET state = 'active',
                        revision = revision + 1,
                        consumed_at = NULL,
                        expires_at = $2
                    WHERE generation_id = $1 AND state = 'consumed'
                    """,
                    prior_generation,
                    replacement_expires_at,
                )
                family_status = await connection.execute(
                    f"""
                    UPDATE {self.schema}.{TABLE_FAMILIES}
                    SET current_generation_id = $2,
                        revision = revision + 1,
                        updated_at = now(),
                        expires_at = $3
                    WHERE family_id = $1
                      AND state = 'active'
                      AND current_generation_id = $4
                    """,
                    family_id,
                    prior_generation,
                    replacement_expires_at,
                    replacement_generation,
                )
                if (
                    replacement_status,
                    prior_status,
                    family_status,
                ) != ("UPDATE 1", "UPDATE 1", "UPDATE 1"):
                    raise RuntimeError(
                        "refresh token rotation rollback lost its locked state"
                    )
                restored = True
        return restored

    async def revoke_refresh_token(self, refresh_token: str) -> bool:
        token = str(refresh_token or "").strip()
        if not token:
            return False
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await self._lock_family_of_token(connection, bearer_sha256(token))
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
                await self._lock_family_of_token(connection, bearer_sha256(token))
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
        payload["redirect_uris"] = list(payload.get("redirect_uris") or [])
        payload["grant_types"] = list(
            payload.get("grant_types")
            or ("authorization_code", "refresh_token")
        )
        payload["metadata"] = dict(payload.get("metadata") or {})
        async with self._pool.acquire() as connection:
            await connection.execute(
                f"""
                INSERT INTO {self.schema}.{TABLE_CLIENTS} (
                    client_id, tenant, project, redirect_uris, grant_types,
                    token_endpoint_auth_method, application_type, metadata,
                    expires_at
                ) VALUES (
                    $1, $2, $3, ($4::text)::jsonb, ($5::text)::jsonb,
                    $6, $7, ($8::text)::jsonb,
                    now() + ($9 * interval '1 second')
                )
                """,
                str(payload.get("client_id") or "").strip(),
                self.tenant,
                self.project,
                json.dumps(payload["redirect_uris"]),
                json.dumps(payload["grant_types"]),
                str(payload.get("token_endpoint_auth_method") or "none"),
                str(payload.get("application_type") or "native"),
                json.dumps(payload["metadata"], sort_keys=True),
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
                           grant_types,
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
                              oauth_client.grant_types,
                              oauth_client.token_endpoint_auth_method,
                              oauth_client.application_type,
                              oauth_client.metadata
                )
                SELECT client_id, redirect_uris,
                       grant_types,
                       token_endpoint_auth_method,
                       application_type, metadata
                FROM extended
                UNION ALL
                SELECT client_id, redirect_uris,
                       grant_types,
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
            "grant_types": _json_array(raw.get("grant_types")),
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

    async def revoke_access_grant_by_digest(self, token_sha256: str) -> str:
        """W582: revoke exactly one pinned binding by its token digest; no raw bearer needed.

        Replay-stable outcome (Ops N-O): ``revoked`` whenever the pinned row
        is revoked after the call, whoever revoked it, and ``absent`` when no
        such binding exists. The digest is pinned at STAGE from the old
        handle, so a late retry can never reach a replacement bearer.
        """
        digest = str(token_sha256 or "").strip().lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("token_sha256_invalid")
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
                digest,
            )
            if status != "UPDATE 0":
                return "revoked"
            state = await connection.fetchval(
                f"SELECT state FROM {self.schema}.{TABLE_ACCESS_BINDINGS} WHERE token_sha256 = $1", digest)
        return "absent" if state is None else ("revoked" if state == "revoked" else str(state))

    async def extend_card_credentials(
        self,
        registry_access_id: str,
        ttl_seconds: int,
    ) -> bool:
        """Extend every live OAuth credential owned by one stable Card id."""

        access_id = str(registry_access_id or "").strip()
        if not access_id:
            return False
        seconds = max(1, int(ttl_seconds))
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await self._lock_card_families(connection, access_id)
                rows = await connection.fetch(
                    f"""
                    SELECT family.family_id
                    FROM {self.schema}.{TABLE_FAMILIES} AS family
                    JOIN {self.schema}.{TABLE_REFRESH_GENERATIONS} AS generation
                      ON generation.generation_id = family.current_generation_id
                    WHERE family.registry_access_id = $1
                      AND family.state = 'active'
                      AND family.expires_at > now()
                      AND generation.state = 'active'
                      AND generation.expires_at > now()
                    ORDER BY family.family_id
                    FOR UPDATE OF family, generation
                    """,
                    access_id,
                )
                family_ids = [str(dict(row).get("family_id") or "") for row in rows]
                family_ids = [value for value in family_ids if value]
                if not family_ids:
                    return False
                await connection.execute(
                    f"""
                    UPDATE {self.schema}.{TABLE_REFRESH_GENERATIONS}
                    SET expires_at = now() + ($2 * interval '1 second'),
                        revision = revision + 1
                    WHERE family_id = ANY($1::text[])
                      AND state = 'active'
                      AND expires_at > now()
                    """,
                    family_ids,
                    seconds,
                )
                await connection.execute(
                    f"""
                    UPDATE {self.schema}.{TABLE_FAMILIES}
                    SET expires_at = now() + ($2 * interval '1 second'),
                        revision = revision + 1,
                        updated_at = now()
                    WHERE family_id = ANY($1::text[])
                      AND state = 'active'
                    """,
                    family_ids,
                    seconds,
                )
                await connection.execute(
                    f"""
                    UPDATE {self.schema}.{TABLE_ACCESS_BINDINGS}
                    SET expires_at = now() + ($2 * interval '1 second'),
                        revision = revision + 1,
                        updated_at = now()
                    WHERE registry_access_id = $1
                      AND state = 'active'
                      AND expires_at > now()
                    """,
                    access_id,
                    seconds,
                )
        return True

    async def set_card_credentials_expiry(self, registry_access_id: str, expires_at: int) -> str:
        """W582: set every live OAuth credential of one Card to ONE absolute deadline.

        Replay-safe: rows already at that deadline are not written (no
        revision bump), so re-applying a committed effect after a crash lands
        on the same state. A deadline in the past expires them. With no live
        family or binding the outcome is ``no_active_credentials``: the effect
        applies as a no-op, because the Card's committed revision already
        carries its expiry.
        """

        access_id = str(registry_access_id or "").strip()
        if not access_id or type(expires_at) is not int or expires_at <= 0:
            raise ValueError("card_credentials_expiry_invalid")
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await self._lock_card_families(connection, access_id)
                rows = await connection.fetch(
                    f"""
                    SELECT family.family_id
                    FROM {self.schema}.{TABLE_FAMILIES} AS family
                    WHERE family.registry_access_id = $1
                      AND family.state = 'active'
                    ORDER BY family.family_id
                    FOR UPDATE OF family
                    """,
                    access_id,
                )
                family_ids = [str(dict(row).get("family_id") or "") for row in rows]
                family_ids = [value for value in family_ids if value]
                bindings = await connection.fetchval(
                    f"""
                    SELECT count(*) FROM {self.schema}.{TABLE_ACCESS_BINDINGS}
                    WHERE registry_access_id = $1 AND state = 'active'
                    """,
                    access_id,
                )
                if not family_ids and not bindings:
                    return "no_active_credentials"
                await connection.execute(
                    f"""
                    UPDATE {self.schema}.{TABLE_REFRESH_GENERATIONS}
                    SET expires_at = to_timestamp($2), revision = revision + 1
                    WHERE family_id = ANY($1::text[])
                      AND state = 'active'
                      AND expires_at IS DISTINCT FROM to_timestamp($2)
                    """,
                    family_ids,
                    expires_at,
                )
                await connection.execute(
                    f"""
                    UPDATE {self.schema}.{TABLE_FAMILIES}
                    SET expires_at = to_timestamp($2), revision = revision + 1, updated_at = now()
                    WHERE family_id = ANY($1::text[])
                      AND state = 'active'
                      AND expires_at IS DISTINCT FROM to_timestamp($2)
                    """,
                    family_ids,
                    expires_at,
                )
                await connection.execute(
                    f"""
                    UPDATE {self.schema}.{TABLE_ACCESS_BINDINGS}
                    SET expires_at = to_timestamp($2), revision = revision + 1, updated_at = now()
                    WHERE registry_access_id = $1
                      AND state = 'active'
                      AND expires_at IS DISTINCT FROM to_timestamp($2)
                    """,
                    access_id,
                    expires_at,
                )
        return "applied"

    async def revoke_card_credentials(self, registry_access_id: str) -> bool:
        """Revoke every OAuth credential owned by one stable Card id."""

        access_id = str(registry_access_id or "").strip()
        if not access_id:
            return False
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                rows = await connection.fetch(
                    f"""
                    SELECT family_id
                    FROM {self.schema}.{TABLE_FAMILIES}
                    WHERE registry_access_id = $1 AND state = 'active'
                    ORDER BY family_id
                    FOR UPDATE
                    """,
                    access_id,
                )
                family_ids = [str(dict(row).get("family_id") or "") for row in rows]
                family_ids = [value for value in family_ids if value]
                if family_ids:
                    await connection.execute(
                        f"""
                        UPDATE {self.schema}.{TABLE_REFRESH_GENERATIONS}
                        SET state = 'revoked',
                            revision = revision + 1,
                            revoked_at = now()
                        WHERE family_id = ANY($1::text[])
                          AND state = 'active'
                        """,
                        family_ids,
                    )
                    await connection.execute(
                        f"""
                        UPDATE {self.schema}.{TABLE_FAMILIES}
                        SET state = 'revoked',
                            revision = revision + 1,
                            updated_at = now()
                        WHERE family_id = ANY($1::text[])
                          AND state = 'active'
                        """,
                        family_ids,
                    )
                access_status = await connection.execute(
                    f"""
                    UPDATE {self.schema}.{TABLE_ACCESS_BINDINGS}
                    SET state = 'revoked',
                        revision = revision + 1,
                        revoked_at = now(),
                        updated_at = now()
                    WHERE registry_access_id = $1 AND state = 'active'
                    """,
                    access_id,
                )
        return bool(family_ids) or access_status != "UPDATE 0"

# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Host-neutral resolution of the descriptor-selected OAuth grant store."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from connection_hub.delegated_credentials.authority_config import (
    CONNECTION_HUB_AUTHORITY_FAMILIES,
    DurableAuthorityConfig,
)
from connection_hub.delegated_credentials.authority_cutover import (
    AuthorityCutoverRequired,
    PostgresAuthorityCutoverStore,
)
from connection_hub.delegated_credentials.oauth.authority_store import (
    PostgresOAuthAuthorityStore,
)
from connection_hub.delegated_credentials.oauth.store import (
    GrantStore,
    GrantStoreUnavailable,
)


@dataclass(frozen=True)
class OAuthGrantStoreProvider:
    """Resolve one grant store from a reviewed authority selector.

    The provider is cheap to compose. ``resolve`` checks the activation receipt
    on every request before exposing PostgreSQL-backed authority, while legacy
    migration-source deployments continue to use Redis explicitly.
    """

    config: DurableAuthorityConfig
    grant_store: GrantStore
    authority_store: PostgresOAuthAuthorityStore | None = None
    cutover_store: PostgresAuthorityCutoverStore | None = None

    @classmethod
    def from_connections(
        cls,
        *,
        connections: Mapping[str, Any],
        redis: Any,
        pg_pool: Any,
        tenant: str,
        project: str,
    ) -> "OAuthGrantStoreProvider":
        return cls.from_config(
            config=DurableAuthorityConfig.from_connections(connections),
            redis=redis,
            pg_pool=pg_pool,
            tenant=tenant,
            project=project,
        )

    @classmethod
    def from_config(
        cls,
        *,
        config: DurableAuthorityConfig,
        redis: Any,
        pg_pool: Any,
        tenant: str,
        project: str,
    ) -> "OAuthGrantStoreProvider":
        if redis is None:
            raise GrantStoreUnavailable("shared_redis_unavailable")

        authority_store: PostgresOAuthAuthorityStore | None = None
        cutover_store: PostgresAuthorityCutoverStore | None = None
        if config.uses_postgresql:
            if pg_pool is None:
                raise GrantStoreUnavailable(
                    "selected_authority.postgresql_pool_unavailable"
                )
            authority_store = PostgresOAuthAuthorityStore(
                pg_pool=pg_pool,
                tenant=tenant,
                project=project,
            )
            cutover_store = PostgresAuthorityCutoverStore(
                pg_pool=pg_pool,
                tenant=tenant,
                project=project,
            )

        return cls(
            config=config,
            grant_store=GrantStore(
                redis,
                tenant,
                project,
                authority_store=authority_store,
            ),
            authority_store=authority_store,
            cutover_store=cutover_store,
        )

    async def ensure_schema(self) -> None:
        if self.authority_store is None or self.cutover_store is None:
            return
        await self.authority_store.ensure_schema()
        await self.cutover_store.ensure_schema()

    async def ensure_ready(self) -> None:
        if self.cutover_store is None:
            return
        await self.cutover_store.require_activated(
            self.config.generation_id,
            required_families=CONNECTION_HUB_AUTHORITY_FAMILIES,
        )

    async def resolve(self) -> GrantStore:
        try:
            await self.ensure_ready()
        except AuthorityCutoverRequired as exc:
            raise GrantStoreUnavailable(
                f"selected_authority.{exc.reason}"
            ) from exc
        except GrantStoreUnavailable:
            raise
        except Exception as exc:
            raise GrantStoreUnavailable(
                "selected_authority.readiness_unavailable"
            ) from exc
        return self.grant_store


__all__ = ["OAuthGrantStoreProvider"]

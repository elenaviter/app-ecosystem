from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from connection_hub.delegated_credentials.admission_replay import (
    PostgresAdmissionReplayClaimStore,
)
from connection_hub.delegated_credentials.authority_config import (
    DelegatedAuthorityConfig,
)
from connection_hub.delegated_credentials.authority_cutover import (
    PostgresAuthorityCutoverStore,
)
from connection_hub.delegated_credentials.cards.credential_handles import (
    PostgresCardCredentialHandleStore,
)
from connection_hub.delegated_credentials.oauth.runtime_store import (
    OAuthGrantStoreProvider,
)
from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.cards.credential_handles import (
    postgres_card_credential_handle_store,
)


@dataclass
class ConnectionHubDurableAuthority:
    """One PostgreSQL authority generation for the bundle."""

    config: DelegatedAuthorityConfig
    oauth_grants: OAuthGrantStoreProvider
    card_handles: PostgresCardCredentialHandleStore
    admission_replay: PostgresAdmissionReplayClaimStore

    @property
    def oauth(self) -> Any:
        store = self.oauth_grants.authority_store
        if store is None:
            raise RuntimeError("PostgreSQL OAuth authority store is unavailable")
        return store

    @property
    def cutovers(self) -> PostgresAuthorityCutoverStore:
        store = self.oauth_grants.cutover_store
        if store is None:
            raise RuntimeError("PostgreSQL authority cutover store is unavailable")
        return store

    @classmethod
    def compose(
        cls,
        *,
        config: DelegatedAuthorityConfig,
        redis: Any,
        pg_pool: Any,
        tenant: str,
        project: str,
        settings: Any | None = None,
    ) -> "ConnectionHubDurableAuthority":
        if not config.uses_postgresql:
            raise RuntimeError(
                "ConnectionHubDurableAuthority requires PostgreSQL configuration"
            )
        oauth_grants = OAuthGrantStoreProvider.from_config(
            config=config,
            redis=redis,
            pg_pool=pg_pool,
            tenant=tenant,
            project=project,
        )
        return cls(
            config=config,
            oauth_grants=oauth_grants,
            card_handles=postgres_card_credential_handle_store(
                pg_pool=pg_pool,
                tenant=tenant,
                project=project,
                settings=settings,
            ),
            admission_replay=PostgresAdmissionReplayClaimStore(
                pg_pool=pg_pool,
                tenant=tenant,
                project=project,
            ),
        )

    async def prepare(self) -> None:
        await self.oauth.ensure_schema()
        await self.card_handles.ensure_schema()
        await self.admission_replay.ensure_schema()
        await self.cutovers.ensure_schema()
        await self.ensure_ready()
        await self.card_handles.reconcile_cleanup(limit=100)
        await self.admission_replay.purge_expired(limit=1000)

    async def ensure_ready(self) -> None:
        """Verify the selected generation from durable activation evidence."""

        await self.oauth_grants.ensure_ready()


__all__ = ["ConnectionHubDurableAuthority"]

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from connection_hub.delegated_credentials.admission_replay import (
    PostgresAdmissionReplayClaimStore,
)
from connection_hub.delegated_credentials.authority_config import (
    CONNECTION_HUB_AUTHORITY_FAMILIES,
    DelegatedAuthorityConfig,
)
from connection_hub.delegated_credentials.authority_cutover import (
    PostgresAuthorityCutoverStore,
)
from connection_hub.delegated_credentials.cards.credential_handles import (
    PostgresCardCredentialHandleStore,
)
from connection_hub.delegated_credentials.oauth.authority_store import (
    PostgresOAuthAuthorityStore,
)
from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.cards.credential_handles import (
    postgres_card_credential_handle_store,
)


@dataclass
class ConnectionHubDurableAuthority:
    """One prepared PostgreSQL authority generation for the bundle."""

    config: DelegatedAuthorityConfig
    oauth: PostgresOAuthAuthorityStore
    card_handles: PostgresCardCredentialHandleStore
    admission_replay: PostgresAdmissionReplayClaimStore
    cutovers: PostgresAuthorityCutoverStore
    ready: bool = False

    @classmethod
    def compose(
        cls,
        *,
        config: DelegatedAuthorityConfig,
        pg_pool: Any,
        tenant: str,
        project: str,
        settings: Any | None = None,
    ) -> "ConnectionHubDurableAuthority":
        if not config.uses_postgresql:
            raise RuntimeError(
                "ConnectionHubDurableAuthority requires PostgreSQL configuration"
            )
        return cls(
            config=config,
            oauth=PostgresOAuthAuthorityStore(
                pg_pool=pg_pool,
                tenant=tenant,
                project=project,
            ),
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
            cutovers=PostgresAuthorityCutoverStore(
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
        await self.cutovers.require_activated(
            self.config.migration_id,
            required_families=CONNECTION_HUB_AUTHORITY_FAMILIES,
        )
        await self.card_handles.reconcile_cleanup(limit=100)
        await self.admission_replay.purge_expired(limit=1000)
        self.ready = True

    def require_ready(self) -> None:
        if not self.ready:
            raise RuntimeError("connection_hub_durable_authority_not_prepared")


__all__ = ["ConnectionHubDurableAuthority"]

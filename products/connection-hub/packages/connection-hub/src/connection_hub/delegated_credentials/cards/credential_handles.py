# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Storage boundary for live Card credential handles."""

from __future__ import annotations

import time
from typing import Any, Protocol

from connection_hub.delegated_credentials.cards.handle_metadata import (
    HANDLE_STATE_ACTIVE,
    HANDLE_STATE_REVOKED,
    CardHandleMetadata,
    CardHandleMetadataStore,
)
from connection_hub.delegated_credentials.cards.handles import (
    DelegatedCardHandleStore,
)
from connection_hub.delegated_credentials.cards.identity import CARD_KIND_AGENT
from connection_hub.delegated_credentials.cards.model import (
    CardAuthority,
    CardCredentialHandles,
)
from connection_hub.delegated_credentials.cards.resident_secrets import (
    ResidentCardSecretService,
)


class CardCredentialHandleUnavailable(RuntimeError):
    """Credential metadata or host custody cannot safely serve the Card."""

    def __init__(self, reason: str, *, access_id: str = "") -> None:
        self.reason = str(reason or "card_credential_handles_unavailable")
        self.access_id = str(access_id or "")
        super().__init__(self.reason)


class CardCredentialHandleStore(Protocol):
    async def read(self, authority: CardAuthority) -> CardCredentialHandles: ...

    async def read_current(
        self, authority: CardAuthority
    ) -> CardCredentialHandles: ...

    async def write(
        self,
        authority: CardAuthority,
        handles: CardCredentialHandles,
    ) -> None: ...

    async def remove(self, authority: CardAuthority) -> None: ...


class RedisCardCredentialHandleStore:
    """Explicit compatibility adapter for the pre-cutover Redis records."""

    def __init__(self, redis: Any, *, tenant: str, project: str) -> None:
        self._store = DelegatedCardHandleStore(
            redis,
            tenant=tenant,
            project=project,
        )

    async def read(self, authority: CardAuthority) -> CardCredentialHandles:
        return await self._store.read(authority.access_id)

    async def read_current(
        self,
        authority: CardAuthority,
    ) -> CardCredentialHandles:
        return await self._store.read(authority.access_id)

    async def write(
        self,
        authority: CardAuthority,
        handles: CardCredentialHandles,
    ) -> None:
        await self._store.write(
            handles,
            ttl_seconds=max(0, int(authority.expires_at) - int(time.time())),
        )

    async def remove(self, authority: CardAuthority) -> None:
        await self._store.remove(authority.access_id)


class PostgresCardCredentialHandleStore:
    """Minimized PostgreSQL metadata plus host-owned resident bearer custody.

    OAuth refresh and access bearers are represented by hashes in the OAuth
    authority tables and never copied here. Manual access tokens are
    self-contained and likewise are not retained. Only a hosted agent's
    reusable bearer enters the host secret store; PostgreSQL holds its opaque
    reference, fingerprint, Card binding, optional session id, and expiry.
    """

    def __init__(
        self,
        *,
        metadata_store: CardHandleMetadataStore,
        resident_secrets: ResidentCardSecretService,
    ) -> None:
        self._metadata = metadata_store
        self._resident_secrets = resident_secrets

    @staticmethod
    def _validate_binding(
        authority: CardAuthority,
        metadata: CardHandleMetadata,
    ) -> None:
        if metadata.access_id != authority.access_id:
            raise CardCredentialHandleUnavailable(
                "card_handle_access_id_mismatch",
                access_id=authority.access_id,
            )
        if metadata.card_revision != authority.card_revision:
            raise CardCredentialHandleUnavailable(
                "card_handle_revision_mismatch",
                access_id=authority.access_id,
            )
        if metadata.expires_at != authority.expires_at:
            raise CardCredentialHandleUnavailable(
                "card_handle_expiry_mismatch",
                access_id=authority.access_id,
            )

    async def read(self, authority: CardAuthority) -> CardCredentialHandles:
        try:
            metadata = await self._metadata.read_active(authority.access_id)
        except Exception as exc:
            raise CardCredentialHandleUnavailable(
                "card_handle_metadata_unavailable",
                access_id=authority.access_id,
            ) from exc
        if metadata is None:
            raise CardCredentialHandleUnavailable(
                "card_handle_metadata_missing",
                access_id=authority.access_id,
            )
        self._validate_binding(authority, metadata)
        access_token = ""
        if authority.card_kind == CARD_KIND_AGENT:
            try:
                access_token = await self._resident_secrets.resolve(
                    authority.access_id
                )
            except Exception as exc:
                raise CardCredentialHandleUnavailable(
                    "resident_card_secret_unavailable",
                    access_id=authority.access_id,
                ) from exc
        return CardCredentialHandles(
            access_id=authority.access_id,
            access_token=access_token,
            session_id=metadata.session_id,
        )

    async def read_current(
        self,
        authority: CardAuthority,
    ) -> CardCredentialHandles:
        try:
            metadata = await self._metadata.read_current(authority.access_id)
        except Exception as exc:
            raise CardCredentialHandleUnavailable(
                "card_handle_metadata_unavailable",
                access_id=authority.access_id,
            ) from exc
        if (
            metadata is None
            or metadata.state != HANDLE_STATE_ACTIVE
            or metadata.expires_at <= int(time.time())
        ):
            return CardCredentialHandles(access_id=authority.access_id)
        return await self.read(authority)

    async def write(
        self,
        authority: CardAuthority,
        handles: CardCredentialHandles,
    ) -> None:
        if handles.access_id != authority.access_id:
            raise CardCredentialHandleUnavailable(
                "card_handle_access_id_mismatch",
                access_id=authority.access_id,
            )
        try:
            current = await self._metadata.read_current(authority.access_id)
        except Exception as exc:
            raise CardCredentialHandleUnavailable(
                "card_handle_metadata_unavailable",
                access_id=authority.access_id,
            ) from exc
        expected_revision = current.revision if current is not None else 0
        if authority.card_kind == CARD_KIND_AGENT:
            if not handles.access_token:
                raise CardCredentialHandleUnavailable(
                    "resident_card_bearer_missing",
                    access_id=authority.access_id,
                )
            await self._resident_secrets.install(
                access_id=authority.access_id,
                card_revision=authority.card_revision,
                bearer=handles.access_token,
                session_id=handles.session_id,
                expires_at=authority.expires_at,
                expected_revision=expected_revision,
            )
            return
        candidate = CardHandleMetadata(
            access_id=authority.access_id,
            card_revision=authority.card_revision,
            expires_at=authority.expires_at,
            session_id=handles.session_id,
            state=HANDLE_STATE_ACTIVE,
        ).validated()
        try:
            await self._metadata.put(
                candidate,
                expected_revision=expected_revision,
            )
        except Exception as exc:
            raise CardCredentialHandleUnavailable(
                "card_handle_metadata_commit_failed",
                access_id=authority.access_id,
            ) from exc

    async def remove(self, authority: CardAuthority) -> None:
        try:
            current = await self._metadata.read_current(authority.access_id)
        except Exception as exc:
            raise CardCredentialHandleUnavailable(
                "card_handle_metadata_unavailable",
                access_id=authority.access_id,
            ) from exc
        if current is None or current.state != HANDLE_STATE_ACTIVE:
            return
        try:
            await self._resident_secrets.retire(
                authority.access_id,
                expected_revision=current.revision,
                state=HANDLE_STATE_REVOKED,
            )
        except Exception as exc:
            raise CardCredentialHandleUnavailable(
                "card_handle_retirement_failed",
                access_id=authority.access_id,
            ) from exc


__all__ = [
    "CardCredentialHandleStore",
    "CardCredentialHandleUnavailable",
    "PostgresCardCredentialHandleStore",
    "RedisCardCredentialHandleStore",
]

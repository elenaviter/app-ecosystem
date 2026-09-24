# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Storage boundary for live Card credential handles."""

from __future__ import annotations

import hashlib
import hmac
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

    async def ensure_schema(self) -> None:
        ensure = getattr(self._metadata, "ensure_schema", None)
        if ensure is None:
            raise CardCredentialHandleUnavailable(
                "card_handle_schema_installer_missing"
            )
        await ensure()

    async def reconcile_cleanup(self, *, limit: int = 100) -> None:
        """Retry bounded host-secret cleanup left by interrupted mutations."""

        await self._resident_secrets.cleanup_prepared_secrets(limit=limit)
        await self._resident_secrets.cleanup_retired_secrets(limit=limit)
        await self._resident_secrets.cleanup_terminal_secrets(limit=limit)

    async def migration_metadata(
        self,
        *,
        captured_at_ms: int,
    ) -> list[CardHandleMetadata]:
        """Active metadata included in one migration reconciliation snapshot."""

        list_active = getattr(self._metadata, "list_active", None)
        if list_active is None:
            raise CardCredentialHandleUnavailable(
                "card_handle_migration_inventory_unavailable"
            )
        return await list_active(now=max(0, int(captured_at_ms)) // 1000)

    @classmethod
    def validate_migration_binding(
        cls,
        authority: CardAuthority,
        metadata: CardHandleMetadata,
    ) -> None:
        cls._validate_binding(authority, metadata)

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

    async def import_current(
        self,
        authority: CardAuthority,
        handles: CardCredentialHandles,
    ) -> bool:
        """Insert one current source record, or prove an exact prior import.

        An exact rerun performs no metadata or host-secret write. Any existing
        row with different Card binding, session, fingerprint, or bearer is a
        migration conflict and remains untouched.
        """

        if handles.access_id != authority.access_id:
            raise CardCredentialHandleUnavailable(
                "card_handle_access_id_mismatch",
                access_id=authority.access_id,
            )
        current = await self._metadata.read_current(authority.access_id)
        if current is None:
            await self.write(authority, handles)
            current = await self._metadata.read_current(authority.access_id)
            created = True
        else:
            created = False
        if current is None:
            raise CardCredentialHandleUnavailable(
                "card_handle_migration_outcome_unknown",
                access_id=authority.access_id,
            )
        if (
            current.state != HANDLE_STATE_ACTIVE
            or current.access_id != authority.access_id
            or current.card_revision != authority.card_revision
            or current.expires_at != authority.expires_at
            or current.session_id != handles.session_id
        ):
            raise CardCredentialHandleUnavailable(
                "card_handle_migration_target_conflict",
                access_id=authority.access_id,
            )
        if authority.card_kind == CARD_KIND_AGENT:
            fingerprint = hashlib.sha256(
                handles.access_token.encode("utf-8")
            ).hexdigest()
            if (
                not handles.access_token
                or not current.resident_access_secret_ref
                or not hmac.compare_digest(
                    current.resident_access_sha256,
                    fingerprint,
                )
            ):
                raise CardCredentialHandleUnavailable(
                    "card_handle_migration_target_conflict",
                    access_id=authority.access_id,
                )
            resident_bearer = await self._resident_secrets.resolve(
                authority.access_id
            )
            if not hmac.compare_digest(resident_bearer, handles.access_token):
                raise CardCredentialHandleUnavailable(
                    "card_handle_migration_target_conflict",
                    access_id=authority.access_id,
                )
        elif current.resident_access_secret_ref or current.resident_access_sha256:
            raise CardCredentialHandleUnavailable(
                "card_handle_migration_target_conflict",
                access_id=authority.access_id,
            )
        return created

    async def replace_current(
        self,
        authority: CardAuthority,
        handles: CardCredentialHandles,
    ) -> None:
        """Install changed source evidence, then prove the exact replacement."""

        await self.write(authority, handles)
        await self.import_current(authority, handles)

    async def remove_current(self, access_id: str) -> None:
        """Retire one stale active migration identity with durable cleanup custody."""

        identifier = str(access_id or "").strip()
        if not identifier:
            raise CardCredentialHandleUnavailable("card_handle_access_id_missing")
        try:
            current = await self._metadata.read_current(identifier)
        except Exception as exc:
            raise CardCredentialHandleUnavailable(
                "card_handle_metadata_unavailable",
                access_id=identifier,
            ) from exc
        if current is None or current.state != HANDLE_STATE_ACTIVE:
            return
        try:
            await self._resident_secrets.retire(
                identifier,
                expected_revision=current.revision,
                state=HANDLE_STATE_REVOKED,
            )
        except Exception as exc:
            raise CardCredentialHandleUnavailable(
                "card_handle_retirement_failed",
                access_id=identifier,
            ) from exc

    async def remove(self, authority: CardAuthority) -> None:
        await self.remove_current(authority.access_id)


__all__ = [
    "CardCredentialHandleStore",
    "CardCredentialHandleUnavailable",
    "PostgresCardCredentialHandleStore",
    "RedisCardCredentialHandleStore",
]

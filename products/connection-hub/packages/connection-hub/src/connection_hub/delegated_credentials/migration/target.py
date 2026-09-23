# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Connection Hub PostgreSQL target for durable-authority migration."""

from __future__ import annotations

import time
from typing import Any

from connection_hub.delegated_credentials.admission_replay import (
    MIGRATED_DIGEST_ONLY_SERVICE_ID,
    PostgresAdmissionReplayClaimStore,
)
from connection_hub.delegated_credentials.authority_cutover import (
    FAMILY_ADMISSION_REPLAY,
    FAMILY_CARD_HANDLES,
    FAMILY_RESIDENT_CARD_SECRETS,
)
from connection_hub.delegated_credentials.cards.credential_handles import (
    PostgresCardCredentialHandleStore,
)
from connection_hub.delegated_credentials.cards.handle_metadata import (
    HANDLE_STATE_ACTIVE,
)
from connection_hub.delegated_credentials.cards.model import (
    CardAuthority,
    CardCredentialHandles,
)
from connection_hub.delegated_credentials.migration.model import (
    AuthorityMigrationRecord,
    AuthorityMigrationSnapshot,
    combine_migration_snapshots,
)
from connection_hub.delegated_credentials.migration.redis_source import (
    CONNECTION_HUB_MIGRATION_FAMILIES,
    CardAuthorityLoader,
)
from connection_hub.delegated_credentials.oauth.migration import (
    PostgresOAuthMigrationTarget,
)


class ConnectionHubPostgresMigrationTarget:
    """Import and inventory every Connection Hub-owned durable family."""

    def __init__(
        self,
        *,
        oauth: PostgresOAuthMigrationTarget,
        card_handles: PostgresCardCredentialHandleStore,
        card_authorities: CardAuthorityLoader,
        admission_replay: PostgresAdmissionReplayClaimStore,
    ) -> None:
        self.oauth = oauth
        self.card_handles = card_handles
        self.card_authorities = card_authorities
        self.admission_replay = admission_replay
        self.tenant = oauth.tenant
        self.project = oauth.project
        if (admission_replay.tenant, admission_replay.project) != (
            self.tenant,
            self.project,
        ):
            raise ValueError("Connection Hub migration targets must share one scope")

    async def import_record(self, record: AuthorityMigrationRecord) -> bool:
        source = record.validated()
        if source.record_type.startswith("oauth_"):
            return await self.oauth.import_record(source)
        if source.record_type == "card_handles":
            authority = CardAuthority.from_mapping(source.payload.get("authority") or {})
            handles_payload = dict(source.payload.get("handles") or {})
            handles_payload["access_token"] = str(
                source.secrets.get("resident_bearer") or ""
            )
            handles = CardCredentialHandles.from_mapping(handles_payload)
            return await self.card_handles.import_current(authority, handles)
        if source.record_type == "admission_replay":
            if source.expires_at_ms is None:
                raise ValueError("admission replay migration record requires expiry")
            await self.admission_replay.import_digest(
                nonce_sha256=source.identity,
                expires_at_ms=source.expires_at_ms,
            )
            return True
        raise ValueError(
            f"unsupported Connection Hub migration record: {source.record_type}"
        )

    async def _card_snapshot(self, *, captured_at_ms: int) -> AuthorityMigrationSnapshot:
        rows = await self.card_handles.migration_metadata(
            captured_at_ms=captured_at_ms
        )
        records: list[AuthorityMigrationRecord] = []
        for metadata in rows:
            if metadata.state != HANDLE_STATE_ACTIVE:
                continue
            authority = await self.card_authorities.load_card_authority(
                metadata.access_id
            )
            if authority is None:
                raise RuntimeError("card_handle_migration_authority_missing")
            self.card_handles.validate_migration_binding(authority, metadata)
            families = [FAMILY_CARD_HANDLES]
            if metadata.resident_access_secret_ref:
                families.append(FAMILY_RESIDENT_CARD_SECRETS)
            records.append(
                AuthorityMigrationRecord(
                    record_type="card_handles",
                    identity=metadata.access_id,
                    families=families,
                    payload={
                        "authority": authority.to_dict(),
                        "handles": {
                            "access_id": metadata.access_id,
                            "session_id": metadata.session_id,
                        },
                        "resident_access_sha256": metadata.resident_access_sha256,
                    },
                    expires_at_ms=metadata.expires_at * 1000,
                )
            )
        return AuthorityMigrationSnapshot(
            tenant=self.tenant,
            project=self.project,
            records=records,
            declared_families=(FAMILY_CARD_HANDLES, FAMILY_RESIDENT_CARD_SECRETS),
            captured_at_ms=captured_at_ms,
        ).validated()

    async def _admission_snapshot(
        self,
        *,
        captured_at_ms: int,
    ) -> AuthorityMigrationSnapshot:
        rows = await self.admission_replay.migration_rows(
            captured_at_ms=captured_at_ms
        )
        return AuthorityMigrationSnapshot(
            tenant=self.tenant,
            project=self.project,
            records=[
                AuthorityMigrationRecord(
                    record_type="admission_replay",
                    identity=str(dict(row).get("nonce_sha256") or ""),
                    families=(FAMILY_ADMISSION_REPLAY,),
                    payload={
                        "service_id_recorded": str(
                            dict(row).get("service_id") or ""
                        )
                        != MIGRATED_DIGEST_ONLY_SERVICE_ID
                    },
                    expires_at_ms=int(dict(row).get("expires_at_ms") or 0),
                )
                for row in rows
            ],
            declared_families=(FAMILY_ADMISSION_REPLAY,),
            captured_at_ms=captured_at_ms,
        ).validated()

    async def snapshot(
        self,
        *,
        captured_at_ms: int | None = None,
    ) -> AuthorityMigrationSnapshot:
        captured = int(
            captured_at_ms
            if captured_at_ms is not None
            else time.time_ns() // 1_000_000
        )
        combined = combine_migration_snapshots(
            (
                await self.oauth.snapshot(captured_at_ms=captured),
                await self._card_snapshot(captured_at_ms=captured),
                await self._admission_snapshot(captured_at_ms=captured),
            ),
            captured_at_ms=captured,
        )
        if tuple(combined.declared_families) != tuple(
            sorted(CONNECTION_HUB_MIGRATION_FAMILIES)
        ):
            raise RuntimeError("connection_hub_migration_family_inventory_incomplete")
        return combined


__all__ = ["ConnectionHubPostgresMigrationTarget"]

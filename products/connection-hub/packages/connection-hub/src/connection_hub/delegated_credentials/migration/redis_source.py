# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Read-only Redis source adapter for Connection Hub durable authorities."""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any, Protocol

from connection_hub.delegated_credentials.authority_cutover import (
    FAMILY_ADMISSION_REPLAY,
    FAMILY_CARD_HANDLES,
    FAMILY_OAUTH_ACCESS,
    FAMILY_OAUTH_CLIENTS,
    FAMILY_OAUTH_REFRESH,
    FAMILY_RESIDENT_CARD_SECRETS,
)
from connection_hub.delegated_credentials.cards.identity import CARD_KIND_AGENT
from connection_hub.delegated_credentials.cards.model import (
    CARD_STATE_ACTIVE,
    CARD_STATE_REVOKED,
    CardAuthority,
    CardCredentialHandles,
    authority_is_credentialless,
)
from connection_hub.delegated_credentials.migration.model import (
    AuthorityMigrationInspection,
    AuthorityMigrationRecord,
    AuthorityMigrationSnapshot,
)
from connection_hub.delegated_credentials.migration.redis_scanner import (
    DurableRedisRecordError,
    ReadOnlyRedisMigrationScanner,
    decode_redis_json_object,
)
from connection_hub.delegated_credentials.oauth.client_records import (
    canonical_oauth_client_record,
)


CONNECTION_HUB_MIGRATION_FAMILIES = (
    FAMILY_ADMISSION_REPLAY,
    FAMILY_CARD_HANDLES,
    FAMILY_OAUTH_ACCESS,
    FAMILY_OAUTH_CLIENTS,
    FAMILY_OAUTH_REFRESH,
    FAMILY_RESIDENT_CARD_SECRETS,
)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class CardAuthorityLoader(Protocol):
    async def load_card_authority(self, access_id: str) -> CardAuthority | None: ...


class ConnectionHubRedisMigrationSource:
    """Scan only Connection Hub key families that carry durable authority."""

    def __init__(
        self,
        redis: Any,
        *,
        tenant: str,
        project: str,
        card_authorities: CardAuthorityLoader,
        scan_count: int = 200,
    ) -> None:
        if redis is None:
            raise ValueError("ConnectionHubRedisMigrationSource requires redis")
        if card_authorities is None:
            raise ValueError("Connection Hub Card authority loader is required")
        self._redis = redis
        self._tenant = str(tenant or "").strip() or "default"
        self._project = str(project or "").strip() or "default"
        self._card_authorities = card_authorities
        self._scanner = ReadOnlyRedisMigrationScanner(
            redis,
            scan_count=scan_count,
        )

    async def _keys(self, pattern: str) -> list[str]:
        return await self._scanner.keys(pattern)

    async def _read(self, key: str, *, expiry_required: bool = True) -> tuple[Any, int | None]:
        return await self._scanner.read(key, expiry_required=expiry_required)

    async def _oauth_clients(
        self,
        *,
        referenced_client_ids: set[str],
    ) -> list[AuthorityMigrationRecord]:
        prefix = f"{self._tenant}:{self._project}:kdcube:oauth:client:"
        records: list[AuthorityMigrationRecord] = []
        for key in await self._keys(prefix + "*"):
            raw, expires_at_ms = await self._read(key, expiry_required=False)
            client_id = key.removeprefix(prefix)
            payload = canonical_oauth_client_record(
                decode_redis_json_object(raw, key=key)
            )
            if not client_id or str(payload.get("client_id") or "") != client_id:
                raise DurableRedisRecordError(
                    "oauth_client_identity_mismatch",
                    key=key,
                )
            records.append(
                AuthorityMigrationRecord(
                    record_type="oauth_client",
                    identity=client_id,
                    families=(FAMILY_OAUTH_CLIENTS,),
                    payload={
                        "migration_state": (
                            "active"
                            if client_id in referenced_client_ids
                            else "retired"
                        ),
                        "record": payload,
                    },
                    expires_at_ms=expires_at_ms,
                )
            )
        return records

    async def _oauth_refresh_records(
        self,
        *,
        captured_at_ms: int,
    ) -> tuple[list[AuthorityMigrationRecord], dict[str, int]]:
        prefix = f"{self._tenant}:{self._project}:kdcube:oauth:refresh:"
        records: list[AuthorityMigrationRecord] = []
        classifications = {
            "active": 0,
            "expired": 0,
            "missing": 0,
            "revoked": 0,
        }
        for key in await self._keys(prefix + "*"):
            raw, expires_at_ms = await self._read(key)
            token = key.removeprefix(prefix)
            if not token:
                raise DurableRedisRecordError(
                    "oauth_refresh_bearer_missing",
                    key=key,
                )
            payload = decode_redis_json_object(raw, key=key)
            access_id = str(payload.get("registry_access_id") or "").strip()
            authority = (
                await self._card_authorities.load_card_authority(access_id)
                if access_id
                else None
            )
            if authority is None:
                card_state = "missing"
                migration_state = "revoked"
            elif authority.state == CARD_STATE_REVOKED:
                card_state = "revoked"
                migration_state = "revoked"
            elif authority.state != CARD_STATE_ACTIVE:
                raise DurableRedisRecordError(
                    "oauth_refresh_card_state_invalid",
                    key=key,
                )
            elif (
                not authority_is_credentialless(authority)
                and int(authority.expires_at) * 1000 <= captured_at_ms
            ):
                card_state = "expired"
                migration_state = "expired"
            else:
                card_state = "active"
                migration_state = "active"
            classifications[card_state] += 1
            records.append(
                AuthorityMigrationRecord(
                    record_type="oauth_refresh",
                    identity=hashlib.sha256(token.encode("utf-8")).hexdigest(),
                    families=(FAMILY_OAUTH_REFRESH,),
                    payload={
                        "bearer_sha256": hashlib.sha256(
                            token.encode("utf-8")
                        ).hexdigest(),
                        "migration_state": migration_state,
                        "record": payload,
                    },
                    expires_at_ms=expires_at_ms,
                    secrets={"bearer": token},
                )
            )
        return records, classifications

    async def _oauth_access(self) -> list[AuthorityMigrationRecord]:
        prefix = f"{self._tenant}:{self._project}:kdcube:oauth:agrant:"
        records: list[AuthorityMigrationRecord] = []
        for key in await self._keys(prefix + "*"):
            raw, expires_at_ms = await self._read(key)
            digest = key.removeprefix(prefix).lower()
            if not _SHA256_PATTERN.fullmatch(digest):
                raise DurableRedisRecordError(
                    "oauth_access_digest_invalid",
                    key=key,
                )
            records.append(
                AuthorityMigrationRecord(
                    record_type="oauth_access",
                    identity=digest,
                    families=(FAMILY_OAUTH_ACCESS,),
                    payload={"record": decode_redis_json_object(raw, key=key)},
                    expires_at_ms=expires_at_ms,
                )
            )
        return records

    async def _read_card_handle_record(
        self,
        *,
        key: str,
        access_id: str,
        authority: CardAuthority,
    ) -> tuple[AuthorityMigrationRecord, CardCredentialHandles]:
        if authority.access_id != access_id:
            raise DurableRedisRecordError(
                "card_handle_authority_identity_mismatch",
                key=key,
            )
        raw, expires_at_ms = await self._read(key)
        handles = CardCredentialHandles.from_mapping(
            decode_redis_json_object(raw, key=key)
        )
        if handles.access_id != access_id:
            raise DurableRedisRecordError(
                "card_handle_identity_mismatch",
                key=key,
            )
        authority_expiry_ms = int(authority.expires_at) * 1000
        if abs(int(expires_at_ms) - authority_expiry_ms) > 1500:
            raise DurableRedisRecordError(
                "card_handle_authority_expiry_mismatch",
                key=key,
            )
        families = [FAMILY_CARD_HANDLES]
        secrets: dict[str, Any] = {}
        resident_access_sha256 = ""
        if authority.card_kind == CARD_KIND_AGENT:
            if not handles.access_token:
                raise DurableRedisRecordError(
                    "resident_card_bearer_missing",
                    key=key,
                )
            families.append(FAMILY_RESIDENT_CARD_SECRETS)
            resident_access_sha256 = hashlib.sha256(
                handles.access_token.encode("utf-8")
            ).hexdigest()
            secrets["resident_bearer"] = handles.access_token
        return (
            AuthorityMigrationRecord(
                record_type="card_handles",
                identity=access_id,
                families=families,
                payload={
                    "authority": authority.to_dict(),
                    "handles": {
                        "access_id": handles.access_id,
                        "session_id": handles.session_id,
                    },
                    "resident_access_sha256": resident_access_sha256,
                },
                expires_at_ms=authority_expiry_ms,
                secrets=secrets,
            ),
            handles,
        )

    async def _card_handles(self) -> list[AuthorityMigrationRecord]:
        prefix = (
            f"{self._tenant}:{self._project}:kdcube:delegated-access:"
            "card-handles:"
        )
        records: list[AuthorityMigrationRecord] = []
        for key in await self._keys(prefix + "*"):
            access_id = key.removeprefix(prefix)
            authority = await self._card_authorities.load_card_authority(access_id)
            if authority is None:
                raise DurableRedisRecordError(
                    "card_handle_authority_missing",
                    key=key,
                )
            record, _ = await self._read_card_handle_record(
                key=key,
                access_id=access_id,
                authority=authority,
            )
            records.append(record)
        return records

    async def _admission_replay(self) -> list[AuthorityMigrationRecord]:
        prefix = (
            f"connection-hub:admission:{self._tenant}:{self._project}:nonce:"
        )
        records: list[AuthorityMigrationRecord] = []
        for key in await self._keys(prefix + "*"):
            _raw, expires_at_ms = await self._read(key)
            digest = key.removeprefix(prefix).lower()
            if not _SHA256_PATTERN.fullmatch(digest):
                raise DurableRedisRecordError(
                    "admission_replay_digest_invalid",
                    key=key,
                )
            records.append(
                AuthorityMigrationRecord(
                    record_type="admission_replay",
                    identity=digest,
                    families=(FAMILY_ADMISSION_REPLAY,),
                    payload={"service_id_recorded": False},
                    expires_at_ms=expires_at_ms,
                )
            )
        return records

    async def inspect(
        self,
        *,
        captured_at_ms: int | None = None,
    ) -> AuthorityMigrationInspection:
        captured = int(
            captured_at_ms
            if captured_at_ms is not None
            else time.time_ns() // 1_000_000
        )
        refresh, refresh_classifications = await self._oauth_refresh_records(
            captured_at_ms=captured,
        )
        referenced_client_ids = {
            str(record.payload.get("record", {}).get("client_id") or "").strip()
            for record in refresh
            if record.payload.get("migration_state") == "active"
        }
        referenced_client_ids.discard("")
        clients = await self._oauth_clients(
            referenced_client_ids=referenced_client_ids,
        )
        access = await self._oauth_access()
        handles = await self._card_handles()
        replay = await self._admission_replay()
        records = [
            *clients,
            *refresh,
            *access,
            *handles,
            *replay,
        ]
        legacy_control = len(
            await self._keys(
                f"{self._tenant}:{self._project}:kdcube:delegated-access:"
                "control-card:*"
            )
        )
        legacy_automation = len(
            await self._keys(
                f"{self._tenant}:{self._project}:kdcube:delegated-access:"
                "automation:*"
            )
        )
        client_states = {"active": 0, "retired": 0}
        for record in clients:
            client_states[str(record.payload.get("migration_state") or "retired")] += 1
        snapshot = AuthorityMigrationSnapshot(
            tenant=self._tenant,
            project=self._project,
            records=records,
            declared_families=CONNECTION_HUB_MIGRATION_FAMILIES,
            captured_at_ms=captured,
        ).validated()
        record_types: dict[str, int] = {}
        for record in snapshot.records:
            record_types[record.record_type] = record_types.get(record.record_type, 0) + 1
        blockers = []
        if legacy_control:
            blockers.append("legacy_control_card_reconciliation_required")
        if legacy_automation:
            blockers.append("legacy_automation_card_reconciliation_required")
        return AuthorityMigrationInspection(
            snapshot=snapshot,
            source_summary={
                "legacy_card_rows": {
                    "automation": legacy_automation,
                    "control": legacy_control,
                },
                "oauth_clients": client_states,
                "oauth_refresh_card_state": refresh_classifications,
                "record_types": record_types,
            },
            blockers=blockers,
        ).validated()

    async def snapshot(
        self,
        *,
        captured_at_ms: int | None = None,
    ) -> AuthorityMigrationSnapshot:
        return (
            await self.inspect(captured_at_ms=captured_at_ms)
        ).snapshot


__all__ = [
    "CONNECTION_HUB_MIGRATION_FAMILIES",
    "CardAuthorityLoader",
    "ConnectionHubRedisMigrationSource",
    "DurableRedisRecordError",
    "ReadOnlyRedisMigrationScanner",
]

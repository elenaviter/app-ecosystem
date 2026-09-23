# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Redis source policy for a reset-oriented durable authority cutover."""

from __future__ import annotations

import time

from connection_hub.delegated_credentials.cards.identity import CARD_KIND_AGENT
from connection_hub.delegated_credentials.cards.model import CARD_STATE_ACTIVE
from connection_hub.delegated_credentials.migration.model import (
    AuthorityMigrationInspection,
    AuthorityMigrationRecord,
    AuthorityMigrationSnapshot,
)
from connection_hub.delegated_credentials.migration.redis_scanner import (
    DurableRedisRecordError,
)
from connection_hub.delegated_credentials.migration.redis_source import (
    CONNECTION_HUB_MIGRATION_FAMILIES,
    ConnectionHubRedisMigrationSource,
)


class ConnectionHubRedisResetSource(ConnectionHubRedisMigrationSource):
    """Preserve hosted Agent credentials and count reconstructable resets.

    Sessions, OAuth grants, dynamic registrations, replay claims, and
    non-agent handle rows are rebuilt after activation. A hosted Agent Card
    bearer is a non-reconstructable host credential, so that record is copied
    with its durable Card binding.
    """

    async def _resident_card_handles(
        self,
        *,
        captured_at_ms: int,
    ) -> tuple[list[AuthorityMigrationRecord], dict[str, int]]:
        prefix = (
            f"{self._tenant}:{self._project}:kdcube:delegated-access:"
            "card-handles:"
        )
        resident: list[AuthorityMigrationRecord] = []
        reset_counts: dict[str, int] = {}
        for key in await self._keys(prefix + "*"):
            access_id = key.removeprefix(prefix)
            if not access_id:
                raise DurableRedisRecordError(
                    "card_handle_access_id_missing",
                    key=key,
                )
            authority = await self._card_authorities.load_card_authority(access_id)
            if authority is None:
                reset_counts["orphaned"] = reset_counts.get("orphaned", 0) + 1
                continue
            if authority.access_id != access_id:
                raise DurableRedisRecordError(
                    "card_handle_authority_identity_mismatch",
                    key=key,
                )
            if authority.card_kind != CARD_KIND_AGENT:
                reset_counts[authority.card_kind] = (
                    reset_counts.get(authority.card_kind, 0) + 1
                )
                continue
            if authority.state != CARD_STATE_ACTIVE:
                bucket = f"{authority.card_kind}_{authority.state}"
                reset_counts[bucket] = reset_counts.get(bucket, 0) + 1
                continue
            if int(authority.expires_at) * 1000 <= captured_at_ms:
                bucket = f"{authority.card_kind}_expired"
                reset_counts[bucket] = reset_counts.get(bucket, 0) + 1
                continue
            resident.append(
                await self._read_card_handle_record(
                    key=key,
                    access_id=access_id,
                    authority=authority,
                )
            )
        return resident, reset_counts

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
        resident_handles, reset_handle_counts = await self._resident_card_handles(
            captured_at_ms=captured,
        )

        reset_counts = {
            "admission_replay": len(
                await self._keys(
                    f"connection-hub:admission:{self._tenant}:"
                    f"{self._project}:nonce:*"
                )
            ),
            "legacy_automation_cards": len(
                await self._keys(
                    f"{self._tenant}:{self._project}:kdcube:"
                    "delegated-access:automation:*"
                )
            ),
            "legacy_control_cards": len(
                await self._keys(
                    f"{self._tenant}:{self._project}:kdcube:"
                    "delegated-access:control-card:*"
                )
            ),
            "oauth_access": len(
                await self._keys(
                    f"{self._tenant}:{self._project}:kdcube:oauth:agrant:*"
                )
            ),
            "oauth_clients": len(
                await self._keys(
                    f"{self._tenant}:{self._project}:kdcube:oauth:client:*"
                )
            ),
            "oauth_refresh": len(
                await self._keys(
                    f"{self._tenant}:{self._project}:kdcube:oauth:refresh:*"
                )
            ),
        }
        for card_kind, count in reset_handle_counts.items():
            reset_counts[f"card_handles_{card_kind}"] = count

        snapshot = AuthorityMigrationSnapshot(
            tenant=self._tenant,
            project=self._project,
            records=resident_handles,
            declared_families=CONNECTION_HUB_MIGRATION_FAMILIES,
            captured_at_ms=captured,
        ).validated()
        return AuthorityMigrationInspection(
            snapshot=snapshot,
            source_summary={
                "preserved": {
                    "resident_agent_card_handles": len(resident_handles),
                },
                "reset": reset_counts,
            },
        ).validated()


__all__ = ["ConnectionHubRedisResetSource"]

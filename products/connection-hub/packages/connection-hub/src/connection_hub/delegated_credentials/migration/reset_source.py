# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Redis source policy for a reset-oriented durable authority cutover."""

from __future__ import annotations

import hashlib
import time

from connection_hub.delegated_credentials.cards.identity import CARD_KIND_AGENT
from connection_hub.delegated_credentials.cards.model import (
    CARD_STATE_ACTIVE,
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
)
from connection_hub.delegated_credentials.migration.redis_source import (
    CONNECTION_HUB_MIGRATION_FAMILIES,
    ConnectionHubRedisMigrationSource,
)


class ConnectionHubRedisResetSource(ConnectionHubRedisMigrationSource):
    """Preserve live Card credential chains and count intentional resets.

    A live Card credential is one authority unit: handle metadata plus either
    its current access binding or its active refresh generation. A refresh
    generation carries the client identity needed to rotate itself; a client
    registration is preserved when present but is not a refresh prerequisite.
    Copying only the Card row leaves a caller visible but unable to authenticate
    or renew. Expired, revoked, orphaned, and unreferenced rows remain reset
    policy.
    """

    async def _live_card_handles(
        self,
        *,
        captured_at_ms: int,
    ) -> tuple[
        list[AuthorityMigrationRecord],
        dict[str, CardAuthority],
        dict[str, CardCredentialHandles],
        dict[str, int],
    ]:
        prefix = (
            f"{self._tenant}:{self._project}:kdcube:delegated-access:"
            "card-handles:"
        )
        live: list[AuthorityMigrationRecord] = []
        authorities: dict[str, CardAuthority] = {}
        credentials: dict[str, CardCredentialHandles] = {}
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
            if authority_is_credentialless(authority):
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
            record, handles = await self._read_card_handle_record(
                key=key,
                access_id=access_id,
                authority=authority,
            )
            live.append(record)
            authorities[access_id] = authority
            credentials[access_id] = handles
        return live, authorities, credentials, reset_counts

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
        handles, live_authorities, live_credentials, reset_handle_counts = (
            await self._live_card_handles(captured_at_ms=captured)
        )
        live_access_ids = set(live_authorities)

        all_refresh, refresh_classifications = await self._oauth_refresh_records(
            captured_at_ms=captured,
        )
        refresh = [
            record
            for record in all_refresh
            if record.payload.get("migration_state") == "active"
            and str(record.payload.get("record", {}).get("registry_access_id") or "")
            in live_access_ids
        ]
        referenced_client_ids = {
            str(record.payload.get("record", {}).get("client_id") or "").strip()
            for record in refresh
        }
        referenced_client_ids.discard("")
        all_clients = await self._oauth_clients(
            referenced_client_ids=referenced_client_ids,
        )
        clients = [
            record
            for record in all_clients
            if record.identity in referenced_client_ids
            and record.payload.get("migration_state") == "active"
        ]
        all_access = await self._oauth_access()
        access = [
            record
            for record in all_access
            if str(record.payload.get("record", {}).get("registry_access_id") or "")
            in live_access_ids
        ]

        access_by_card: dict[str, set[str]] = {}
        for record in access:
            access_id = str(
                record.payload.get("record", {}).get("registry_access_id") or ""
            )
            access_by_card.setdefault(access_id, set()).add(record.identity)
        refresh_by_card: dict[str, set[str]] = {}
        for record in refresh:
            access_id = str(
                record.payload.get("record", {}).get("registry_access_id") or ""
            )
            refresh_by_card.setdefault(access_id, set()).add(record.identity)
        blockers: list[str] = []
        current_access_cards = 0
        current_refresh_cards = 0
        refresh_recoverable_cards = 0
        for handle in handles:
            authority = live_authorities[handle.identity]
            held = live_credentials[handle.identity]
            access_digest = (
                hashlib.sha256(held.access_token.encode("utf-8")).hexdigest()
                if held.access_token
                else ""
            )
            refresh_digest = (
                hashlib.sha256(held.refresh_token.encode("utf-8")).hexdigest()
                if held.refresh_token
                else ""
            )
            card_access = access_by_card.get(authority.access_id, set())
            card_refresh = refresh_by_card.get(authority.access_id, set())
            access_is_current = bool(
                access_digest and access_digest in card_access
            )
            refresh_is_current = bool(
                refresh_digest and refresh_digest in card_refresh
            )
            current_access_cards += int(access_is_current)
            current_refresh_cards += int(refresh_is_current)
            refresh_recoverable_cards += int(
                not access_is_current and refresh_is_current
            )
            if access_digest and not access_is_current and not refresh_is_current:
                blockers.append(
                    f"live_card_access_binding_missing:{authority.access_id}"
                )
            if refresh_digest and not refresh_is_current:
                blockers.append(
                    f"live_card_refresh_generation_missing:{authority.access_id}"
                )
            if authority.card_kind == CARD_KIND_AGENT and not access_digest:
                blockers.append(
                    f"live_card_resident_bearer_missing:{authority.access_id}"
                )
            elif not access_digest and not refresh_digest and not (
                card_access or card_refresh
            ):
                blockers.append(
                    f"live_card_oauth_chain_missing:{authority.access_id}"
                )

        available_clients = {record.identity for record in clients}

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
            "oauth_access": len(all_access) - len(access),
            "oauth_clients": len(all_clients) - len(clients),
            "oauth_refresh": len(all_refresh) - len(refresh),
        }
        for card_kind, count in reset_handle_counts.items():
            reset_counts[f"card_handles_{card_kind}"] = count

        snapshot = AuthorityMigrationSnapshot(
            tenant=self._tenant,
            project=self._project,
            records=[*clients, *refresh, *access, *handles],
            declared_families=CONNECTION_HUB_MIGRATION_FAMILIES,
            captured_at_ms=captured,
        ).validated()
        return AuthorityMigrationInspection(
            snapshot=snapshot,
            source_summary={
                "preserved": {
                    "card_handles": len(handles),
                    **{
                        f"card_handles_{card_kind}": sum(
                            1
                            for authority in live_authorities.values()
                            if authority.card_kind == card_kind
                        )
                        for card_kind in sorted(
                            {
                                authority.card_kind
                                for authority in live_authorities.values()
                            }
                        )
                    },
                    "oauth_access": len(access),
                    "oauth_clients": len(clients),
                    "oauth_refresh": len(refresh),
                    "card_handles_current_access": current_access_cards,
                    "card_handles_current_refresh": current_refresh_cards,
                    "card_handles_refresh_recoverable": refresh_recoverable_cards,
                    "oauth_refresh_self_contained_clients": len(
                        referenced_client_ids - available_clients
                    ),
                    "resident_agent_card_handles": sum(
                        1
                        for authority in live_authorities.values()
                        if authority.card_kind == CARD_KIND_AGENT
                    ),
                },
                "reset": reset_counts,
                "oauth_refresh_card_state": refresh_classifications,
            },
            blockers=blockers,
        ).validated()


__all__ = ["ConnectionHubRedisResetSource"]

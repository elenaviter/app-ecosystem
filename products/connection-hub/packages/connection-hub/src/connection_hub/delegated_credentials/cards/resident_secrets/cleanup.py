# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Delete-before-ack cleanup for resident delegated-Card bearers."""

from __future__ import annotations

import hmac
import time

from connection_hub.delegated_credentials.cards.handle_metadata import (
    CLEANUP_SOURCE_PREPARED,
    CLEANUP_SOURCE_RETIRED,
    CLEANUP_SOURCE_TERMINAL,
    CardHandleMetadata,
    CardHandleMetadataStore,
    PreparedResidentSecret,
    ResidentSecretCleanupClaim,
    RetiredResidentSecret,
)
from connection_hub.delegated_credentials.cards.resident_secrets.model import (
    ResidentSecretCleanupFailure,
    ResidentSecretCleanupResult,
    ResidentSecretEnvelope,
    ResidentSecretError,
    ResidentSecretStore,
    validated_batch_limit,
)


class ResidentSecretCleanupService:
    """Retire host secrets without losing their durable cleanup addresses."""

    def __init__(
        self,
        *,
        metadata_store: CardHandleMetadataStore,
        secret_store: ResidentSecretStore,
    ) -> None:
        self._metadata = metadata_store
        self._secrets = secret_store

    async def _verified_delete(
        self,
        *,
        access_id: str,
        secret_ref: str,
        fingerprint: str,
        source: str,
        card_revision: int | None = None,
        created_at: int | None = None,
        expires_at: int | None = None,
    ) -> ResidentSecretCleanupFailure | None:
        try:
            raw = await self._secrets.get(secret_ref=secret_ref)
        except Exception:  # noqa: BLE001 - provider failures become durable retries
            return ResidentSecretCleanupFailure(
                access_id=access_id,
                source=source,
                phase="read",
                reason="resident_secret_cleanup_read_failed",
            )
        if raw is not None:
            try:
                envelope = ResidentSecretEnvelope.from_json(raw)
            except ResidentSecretError as exc:
                return ResidentSecretCleanupFailure(
                    access_id=access_id,
                    source=source,
                    phase="verify",
                    reason=exc.reason,
                )
            if not hmac.compare_digest(envelope.access_id, access_id):
                return ResidentSecretCleanupFailure(
                    access_id=access_id,
                    source=source,
                    phase="verify",
                    reason="resident_secret_cleanup_access_id_mismatch",
                )
            if not hmac.compare_digest(envelope.fingerprint, fingerprint):
                return ResidentSecretCleanupFailure(
                    access_id=access_id,
                    source=source,
                    phase="verify",
                    reason="resident_secret_cleanup_fingerprint_mismatch",
                )
            if card_revision is not None and envelope.card_revision != int(
                card_revision
            ):
                return ResidentSecretCleanupFailure(
                    access_id=access_id,
                    source=source,
                    phase="verify",
                    reason="resident_secret_cleanup_card_revision_mismatch",
                )
            if created_at is not None and envelope.created_at != int(created_at):
                return ResidentSecretCleanupFailure(
                    access_id=access_id,
                    source=source,
                    phase="verify",
                    reason="resident_secret_cleanup_created_at_mismatch",
                )
            if expires_at is not None and envelope.expires_at != int(expires_at):
                return ResidentSecretCleanupFailure(
                    access_id=access_id,
                    source=source,
                    phase="verify",
                    reason="resident_secret_cleanup_expiry_mismatch",
                )
        try:
            await self._secrets.delete(secret_ref=secret_ref)
        except Exception:  # noqa: BLE001 - provider failures become durable retries
            return ResidentSecretCleanupFailure(
                access_id=access_id,
                source=source,
                phase="delete",
                reason="resident_secret_cleanup_delete_failed",
            )
        return None

    async def _defer_claim(
        self,
        claim: ResidentSecretCleanupClaim,
        failure: ResidentSecretCleanupFailure,
    ) -> ResidentSecretCleanupFailure:
        try:
            recorded = await self._metadata.defer_resident_secret_cleanup(
                claim,
                now=int(time.time()),
                reason=failure.reason,
            )
        except Exception:  # noqa: BLE001 - persistence failures stay observable
            recorded = False
        if recorded:
            return failure
        return ResidentSecretCleanupFailure(
            access_id=claim.access_id,
            source=claim.source,
            phase="retry",
            reason="resident_secret_cleanup_retry_record_failed",
        )

    async def _cleanup_claim(
        self,
        claim: ResidentSecretCleanupClaim,
    ) -> tuple[CardHandleMetadata | None, ResidentSecretCleanupFailure | None]:
        item = claim.validated()
        if item.source == CLEANUP_SOURCE_PREPARED:
            try:
                current = await self._metadata.read_current(item.access_id)
            except Exception:  # noqa: BLE001 - persistence failures become retries
                failure = ResidentSecretCleanupFailure(
                    access_id=item.access_id,
                    source=item.source,
                    phase="guard",
                    reason="resident_secret_prepared_current_read_failed",
                )
                return None, await self._defer_claim(item, failure)
            if (
                current is not None
                and current.resident_access_secret_ref == item.secret_ref
            ):
                failure = ResidentSecretCleanupFailure(
                    access_id=item.access_id,
                    source=item.source,
                    phase="guard",
                    reason="resident_secret_prepared_reference_is_current",
                )
                return current, await self._defer_claim(item, failure)
        failure = await self._verified_delete(
            access_id=item.access_id,
            secret_ref=item.secret_ref,
            fingerprint=item.resident_access_sha256,
            source=item.source,
            card_revision=(
                item.card_revision
                if item.source
                in {CLEANUP_SOURCE_PREPARED, CLEANUP_SOURCE_TERMINAL}
                else None
            ),
            created_at=(
                item.created_at if item.source == CLEANUP_SOURCE_PREPARED else None
            ),
            expires_at=(
                item.expires_at if item.source == CLEANUP_SOURCE_PREPARED else None
            ),
        )
        if failure is not None:
            return None, await self._defer_claim(item, failure)
        try:
            acknowledgement = (
                await self._metadata.acknowledge_resident_secret_cleanup(item)
            )
        except Exception:  # noqa: BLE001 - persistence failures become retries
            acknowledgement = None
        if acknowledgement is None or not acknowledgement.acknowledged:
            if item.source == CLEANUP_SOURCE_TERMINAL:
                try:
                    current = await self._metadata.read_current(item.access_id)
                except Exception:  # noqa: BLE001 - absence must be proven
                    failure = ResidentSecretCleanupFailure(
                        access_id=item.access_id,
                        source=item.source,
                        phase="guard",
                        reason="resident_secret_terminal_post_ack_read_failed",
                    )
                    return None, await self._defer_claim(item, failure)
                if (
                    current is None
                    or current.resident_access_secret_ref != item.secret_ref
                ):
                    return current, None
            reason = {
                CLEANUP_SOURCE_PREPARED: (
                    "resident_secret_prepared_cleanup_ack_failed"
                ),
                CLEANUP_SOURCE_TERMINAL: (
                    "resident_secret_terminal_ack_failed"
                ),
            }.get(item.source, "resident_secret_cleanup_ack_failed")
            failure = ResidentSecretCleanupFailure(
                access_id=item.access_id,
                source=item.source,
                phase="ack",
                reason=reason,
            )
            return None, await self._defer_claim(item, failure)
        return acknowledgement.metadata, None

    @staticmethod
    def _claim_failure(
        *,
        access_id: str,
        source: str,
    ) -> ResidentSecretCleanupFailure:
        return ResidentSecretCleanupFailure(
            access_id=access_id,
            source=source,
            phase="claim",
            reason="resident_secret_cleanup_claim_unavailable",
        )

    async def cleanup_prepared_candidate(
        self,
        candidate: PreparedResidentSecret,
    ) -> ResidentSecretCleanupFailure | None:
        record = candidate.validated()
        try:
            claims = await self._metadata.claim_prepared_secret_cleanup(
                now=int(time.time()),
                limit=1,
                candidate=record,
            )
        except Exception:  # noqa: BLE001 - persistence failures become claim failures
            return self._claim_failure(
                access_id=record.access_id,
                source=CLEANUP_SOURCE_PREPARED,
            )
        if not claims:
            try:
                current = await self._metadata.read_current(record.access_id)
            except Exception:  # noqa: BLE001 - absence must be proven
                current = None
            if (
                current is not None
                and current.resident_access_secret_ref == record.secret_ref
            ):
                return ResidentSecretCleanupFailure(
                    access_id=record.access_id,
                    source=CLEANUP_SOURCE_PREPARED,
                    phase="guard",
                    reason="resident_secret_prepared_reference_is_current",
                )
            return self._claim_failure(
                access_id=record.access_id,
                source=CLEANUP_SOURCE_PREPARED,
            )
        _metadata, failure = await self._cleanup_claim(claims[0])
        return failure

    async def quarantine_prepared_collision(
        self,
        candidate: PreparedResidentSecret,
    ) -> bool:
        """Move a refused host-create reference into durable cleanup state."""

        record = candidate.validated()
        try:
            claims = await self._metadata.claim_prepared_secret_cleanup(
                now=int(time.time()),
                limit=1,
                candidate=record,
            )
        except Exception:  # noqa: BLE001 - caller reports an unknown quarantine
            return False
        if len(claims) != 1:
            return False
        try:
            return bool(
                await self._metadata.defer_resident_secret_cleanup(
                    claims[0],
                    now=int(time.time()),
                    reason="resident_secret_reference_collision",
                )
            )
        except Exception:  # noqa: BLE001 - caller reports an unknown quarantine
            return False

    async def cleanup_retired_candidate(
        self,
        candidate: RetiredResidentSecret,
    ) -> ResidentSecretCleanupFailure | None:
        record = candidate.validated()
        try:
            claims = await self._metadata.claim_retired_secret_cleanup(
                now=int(time.time()),
                limit=1,
                candidate=record,
            )
        except Exception:  # noqa: BLE001 - persistence failures become claim failures
            return self._claim_failure(
                access_id=record.access_id,
                source=CLEANUP_SOURCE_RETIRED,
            )
        if not claims:
            return self._claim_failure(
                access_id=record.access_id,
                source=CLEANUP_SOURCE_RETIRED,
            )
        _metadata, failure = await self._cleanup_claim(claims[0])
        return failure

    async def cleanup_terminal_candidate(
        self,
        candidate: CardHandleMetadata,
    ) -> tuple[CardHandleMetadata, ResidentSecretCleanupFailure | None]:
        record = candidate.validated()
        if not record.resident_access_secret_ref:
            return record, None
        try:
            claims = await self._metadata.claim_terminal_secret_cleanup(
                now=int(time.time()),
                limit=1,
                candidate=record,
            )
        except Exception:  # noqa: BLE001 - persistence failures become claim failures
            claims = []
        if not claims:
            try:
                current = await self._metadata.read_current(record.access_id)
            except Exception:  # noqa: BLE001 - absence must be proven
                current = record
            if (
                current is None
                or current.resident_access_secret_ref
                != record.resident_access_secret_ref
            ):
                return current or record, None
            return record, self._claim_failure(
                access_id=record.access_id,
                source=CLEANUP_SOURCE_TERMINAL,
            )
        cleared, failure = await self._cleanup_claim(claims[0])
        return cleared or record, failure

    async def cleanup_retired_secrets(
        self,
        *,
        limit: int = 100,
    ) -> ResidentSecretCleanupResult:
        bounded = validated_batch_limit(limit)
        moment = int(time.time())
        try:
            claims = await self._metadata.claim_retired_secret_cleanup(
                now=moment,
                limit=bounded,
            )
        except Exception as exc:
            raise ResidentSecretError(
                "resident_secret_retired_cleanup_claim_unavailable"
            ) from exc
        failures: list[ResidentSecretCleanupFailure] = []
        completed = 0
        for claim in claims:
            _metadata, failure = await self._cleanup_claim(claim)
            if failure is None:
                completed += 1
            else:
                failures.append(failure)
        return ResidentSecretCleanupResult(
            scanned=len(claims),
            completed=completed,
            failures=tuple(failures),
        )

    async def cleanup_prepared_secrets(
        self,
        *,
        limit: int = 100,
    ) -> ResidentSecretCleanupResult:
        bounded = validated_batch_limit(limit)
        moment = int(time.time())
        try:
            claims = await self._metadata.claim_prepared_secret_cleanup(
                now=moment,
                limit=bounded,
            )
        except Exception as exc:
            raise ResidentSecretError(
                "resident_secret_prepared_cleanup_claim_unavailable"
            ) from exc
        failures: list[ResidentSecretCleanupFailure] = []
        completed = 0
        for claim in claims:
            _metadata, failure = await self._cleanup_claim(claim)
            if failure is None:
                completed += 1
            else:
                failures.append(failure)
        return ResidentSecretCleanupResult(
            scanned=len(claims),
            completed=completed,
            failures=tuple(failures),
        )

    async def cleanup_terminal_secrets(
        self,
        *,
        limit: int = 100,
    ) -> ResidentSecretCleanupResult:
        bounded = validated_batch_limit(limit)
        moment = int(time.time())
        try:
            claims = await self._metadata.claim_terminal_secret_cleanup(
                now=moment,
                limit=bounded,
            )
        except Exception as exc:
            raise ResidentSecretError(
                "resident_secret_terminal_cleanup_claim_unavailable"
            ) from exc
        failures: list[ResidentSecretCleanupFailure] = []
        completed = 0
        for claim in claims:
            _metadata, failure = await self._cleanup_claim(claim)
            if failure is None:
                completed += 1
            else:
                failures.append(failure)
        return ResidentSecretCleanupResult(
            scanned=len(claims),
            completed=completed,
            failures=tuple(failures),
        )

    async def expire_due_and_cleanup(
        self,
        *,
        now: int | None = None,
        limit: int = 100,
    ) -> ResidentSecretCleanupResult:
        bounded = validated_batch_limit(limit)
        moment = int(now if now is not None else time.time())
        try:
            candidates = await self._metadata.expire_due(now=moment, limit=bounded)
        except Exception as exc:
            raise ResidentSecretError(
                "resident_secret_expiry_scan_unavailable"
            ) from exc
        failures: list[ResidentSecretCleanupFailure] = []
        completed = 0
        for candidate in candidates:
            _metadata, failure = await self.cleanup_terminal_candidate(candidate)
            if failure is None:
                completed += 1
            else:
                failures.append(failure)
        return ResidentSecretCleanupResult(
            scanned=len(candidates),
            completed=completed,
            failures=tuple(failures),
        )

    async def purge_expired_secret_records(
        self,
        *,
        now: int | None = None,
        limit: int = 100,
    ) -> int:
        bounded = validated_batch_limit(limit)
        moment = int(now if now is not None else time.time())
        try:
            return int(await self._secrets.purge_expired(now=moment, limit=bounded))
        except Exception as exc:
            raise ResidentSecretError("resident_secret_purge_unavailable") from exc


__all__ = ["ResidentSecretCleanupService"]

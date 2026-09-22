# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Delete-before-ack cleanup for resident delegated-Card bearers."""

from __future__ import annotations

import hmac
import time

from connection_hub.delegated_credentials.cards.handle_metadata import (
    CardHandleMetadata,
    CardHandleMetadataConflict,
    CardHandleMetadataStore,
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
    ) -> ResidentSecretCleanupFailure | None:
        try:
            raw = await self._secrets.get(secret_ref=secret_ref)
        except Exception:
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
        try:
            await self._secrets.delete(secret_ref=secret_ref)
        except Exception:
            return ResidentSecretCleanupFailure(
                access_id=access_id,
                source=source,
                phase="delete",
                reason="resident_secret_cleanup_delete_failed",
            )
        return None

    async def cleanup_retired_candidate(
        self,
        candidate: RetiredResidentSecret,
    ) -> ResidentSecretCleanupFailure | None:
        record = candidate.validated()
        failure = await self._verified_delete(
            access_id=record.access_id,
            secret_ref=record.secret_ref,
            fingerprint=record.resident_access_sha256,
            source="retired",
        )
        if failure is not None:
            return failure
        try:
            await self._metadata.delete_retired_secret_record(
                access_id=record.access_id,
                secret_ref=record.secret_ref,
            )
        except Exception:
            return ResidentSecretCleanupFailure(
                access_id=record.access_id,
                source="retired",
                phase="ack",
                reason="resident_secret_cleanup_ack_failed",
            )
        return None

    async def cleanup_terminal_candidate(
        self,
        candidate: CardHandleMetadata,
    ) -> tuple[CardHandleMetadata, ResidentSecretCleanupFailure | None]:
        record = candidate.validated()
        if not record.resident_access_secret_ref:
            return record, None
        failure = await self._verified_delete(
            access_id=record.access_id,
            secret_ref=record.resident_access_secret_ref,
            fingerprint=record.resident_access_sha256,
            source="terminal",
            card_revision=record.card_revision,
        )
        if failure is not None:
            return record, failure
        try:
            cleared = await self._metadata.clear_retired_resident_secret(
                record.access_id,
                expected_revision=record.revision,
            )
        except CardHandleMetadataConflict:
            try:
                current = await self._metadata.read_current(record.access_id)
            except Exception:
                current = record
            if (
                current is None
                or current.resident_access_secret_ref
                != record.resident_access_secret_ref
            ):
                return current or record, None
            return record, ResidentSecretCleanupFailure(
                access_id=record.access_id,
                source="terminal",
                phase="ack",
                reason="resident_secret_terminal_ack_failed",
            )
        except Exception:
            return record, ResidentSecretCleanupFailure(
                access_id=record.access_id,
                source="terminal",
                phase="ack",
                reason="resident_secret_terminal_ack_failed",
            )
        return cleared or record, None

    async def cleanup_retired_secrets(
        self,
        *,
        limit: int = 100,
    ) -> ResidentSecretCleanupResult:
        bounded = validated_batch_limit(limit)
        try:
            candidates = await self._metadata.list_retired_secret_cleanup_candidates(
                limit=bounded
            )
        except Exception as exc:
            raise ResidentSecretError(
                "resident_secret_retired_cleanup_list_unavailable"
            ) from exc
        failures: list[ResidentSecretCleanupFailure] = []
        completed = 0
        for candidate in candidates:
            failure = await self.cleanup_retired_candidate(candidate)
            if failure is None:
                completed += 1
            else:
                failures.append(failure)
        return ResidentSecretCleanupResult(
            scanned=len(candidates),
            completed=completed,
            failures=tuple(failures),
        )

    async def cleanup_terminal_secrets(
        self,
        *,
        limit: int = 100,
    ) -> ResidentSecretCleanupResult:
        bounded = validated_batch_limit(limit)
        try:
            candidates = await self._metadata.list_secret_cleanup_candidates(
                limit=bounded
            )
        except Exception as exc:
            raise ResidentSecretError(
                "resident_secret_terminal_cleanup_list_unavailable"
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

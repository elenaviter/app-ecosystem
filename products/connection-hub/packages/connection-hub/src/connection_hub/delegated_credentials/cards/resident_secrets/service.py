# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Active lifecycle for resident delegated-Card bearer custody."""

from __future__ import annotations

import hmac
import secrets
import time
from collections.abc import Callable

from connection_hub.delegated_credentials.cards.handle_metadata import (
    HANDLE_STATE_ACTIVE,
    CardHandleMetadata,
    CardHandleMetadataConflict,
    CardHandleMetadataStore,
    PreparedResidentSecret,
)
from connection_hub.delegated_credentials.cards.resident_secrets.cleanup import (
    ResidentSecretCleanupService,
)
from connection_hub.delegated_credentials.cards.resident_secrets.model import (
    SECRET_REF_PATTERN,
    ResidentSecretCleanupResult,
    ResidentSecretEnvelope,
    ResidentSecretError,
    ResidentSecretInstallResult,
    ResidentSecretRetirementResult,
    ResidentSecretStore,
)

_SECRET_REF_CREATE_ATTEMPTS = 8


class ResidentCardSecretService:
    """Coordinate durable metadata with host-owned resident bearer custody."""

    def __init__(
        self,
        *,
        metadata_store: CardHandleMetadataStore,
        secret_store: ResidentSecretStore,
        secret_ref_factory: Callable[[], str] | None = None,
    ) -> None:
        self._metadata = metadata_store
        self._secrets = secret_store
        self._secret_ref_factory = secret_ref_factory or (lambda: secrets.token_hex(16))
        self._cleanup = ResidentSecretCleanupService(
            metadata_store=metadata_store,
            secret_store=secret_store,
        )

    def _new_secret_ref(self) -> str:
        reference = str(self._secret_ref_factory() or "").strip().lower()
        if not SECRET_REF_PATTERN.fullmatch(reference):
            raise ResidentSecretError("resident_secret_reference_invalid")
        return reference

    async def _read_current(self, access_id: str) -> CardHandleMetadata | None:
        try:
            return await self._metadata.read_current(access_id)
        except Exception as exc:
            raise ResidentSecretError(
                "resident_secret_metadata_unavailable",
                access_id=str(access_id or ""),
            ) from exc

    async def _delete_prepared_after_definitive_failure(
        self,
        *,
        prepared: PreparedResidentSecret,
        original: Exception,
    ) -> None:
        failure = await self._cleanup.cleanup_prepared_candidate(prepared)
        if failure is not None:
            error = ResidentSecretError(
                "resident_secret_metadata_rejected_cleanup_pending",
                access_id=prepared.access_id,
                secret_ref=prepared.secret_ref,
                operation_error_type=type(original).__name__,
                cleanup_error_type=failure.reason,
            )
            raise error from original

    async def _create_prepared_secret(
        self,
        envelope: ResidentSecretEnvelope,
    ) -> PreparedResidentSecret:
        for _attempt in range(_SECRET_REF_CREATE_ATTEMPTS):
            secret_ref = self._new_secret_ref()
            prepared = PreparedResidentSecret(
                access_id=envelope.access_id,
                secret_ref=secret_ref,
                resident_access_sha256=envelope.fingerprint,
                card_revision=envelope.card_revision,
                created_at=envelope.created_at,
                expires_at=envelope.expires_at,
            ).validated()
            try:
                reserved = await self._metadata.prepare_resident_secret(prepared)
            except Exception as intent_error:
                raise ResidentSecretError(
                    "resident_secret_intent_create_outcome_unknown",
                    access_id=envelope.access_id,
                    secret_ref=secret_ref,
                    operation_error_type=type(intent_error).__name__,
                ) from intent_error
            if reserved is not True:
                if reserved is False:
                    continue
                raise ResidentSecretError(
                    "resident_secret_intent_create_outcome_unknown",
                    access_id=envelope.access_id,
                    secret_ref=secret_ref,
                )
            try:
                created = await self._secrets.create(
                    secret_ref=secret_ref,
                    value=envelope.to_json(),
                    expires_at=envelope.expires_at,
                )
            except Exception as write_error:
                raise ResidentSecretError(
                    "resident_secret_create_outcome_unknown",
                    access_id=envelope.access_id,
                    secret_ref=secret_ref,
                    operation_error_type=type(write_error).__name__,
                ) from write_error
            if created is True:
                return prepared
            if created is not False:
                raise ResidentSecretError(
                    "resident_secret_create_outcome_unknown",
                    access_id=envelope.access_id,
                    secret_ref=secret_ref,
                )
            quarantined = await self._cleanup.quarantine_prepared_collision(prepared)
            if quarantined is not True:
                raise ResidentSecretError(
                    "resident_secret_collision_quarantine_outcome_unknown",
                    access_id=envelope.access_id,
                    secret_ref=secret_ref,
                )
        raise ResidentSecretError(
            "resident_secret_reference_collision",
            access_id=envelope.access_id,
        )

    async def install(
        self,
        *,
        access_id: str,
        card_revision: int,
        bearer: str,
        session_id: str,
        expires_at: int,
        expected_revision: int,
        now: int | None = None,
    ) -> ResidentSecretInstallResult:
        """Write a fresh secret first, then install its metadata reference.

        A generic metadata exception is outcome-unknown and deliberately leaves
        the prepared, expiring secret in custody. Deleting it could break a SQL
        commit whose response was lost. Conflicts and validation errors are
        definitive non-commits, so they receive compensating deletion.
        """

        moment = int(now if now is not None else time.time())
        envelope = ResidentSecretEnvelope.create(
            access_id=access_id,
            card_revision=card_revision,
            value=bearer,
            created_at=moment,
            expires_at=expires_at,
        )
        prepared = await self._create_prepared_secret(envelope)

        candidate = CardHandleMetadata(
            access_id=envelope.access_id,
            card_revision=envelope.card_revision,
            expires_at=envelope.expires_at,
            resident_access_secret_ref=prepared.secret_ref,
            resident_access_sha256=envelope.fingerprint,
            session_id=session_id,
            state=HANDLE_STATE_ACTIVE,
        ).validated()
        try:
            mutation = await self._metadata.install_prepared_resident_secret(
                candidate,
                expected_revision=int(expected_revision),
            )
        except (CardHandleMetadataConflict, ValueError) as exc:
            await self._delete_prepared_after_definitive_failure(
                prepared=prepared,
                original=exc,
            )
            raise
        except Exception as exc:
            raise ResidentSecretError(
                "resident_secret_metadata_commit_outcome_unknown",
                access_id=envelope.access_id,
                secret_ref=prepared.secret_ref,
            ) from exc

        cleanup_failure = None
        if mutation.retired_secret is not None:
            cleanup_failure = await self._cleanup.cleanup_retired_candidate(
                mutation.retired_secret
            )
        return ResidentSecretInstallResult(
            metadata=mutation.metadata,
            cleanup_failure=cleanup_failure,
        )

    async def resolve(self, access_id: str, *, now: int | None = None) -> str:
        """Return the bearer only after metadata and envelope agree exactly."""

        moment = int(now if now is not None else time.time())
        current = await self._read_current(access_id)
        if current is None:
            raise ResidentSecretError(
                "resident_secret_metadata_missing", access_id=str(access_id or "")
            )
        if current.state != HANDLE_STATE_ACTIVE:
            raise ResidentSecretError(
                "resident_secret_handle_not_active", access_id=current.access_id
            )
        if current.expires_at <= moment:
            raise ResidentSecretError(
                "resident_secret_handle_expired", access_id=current.access_id
            )
        if not current.resident_access_secret_ref:
            raise ResidentSecretError(
                "resident_secret_not_configured", access_id=current.access_id
            )
        try:
            raw = await self._secrets.get(secret_ref=current.resident_access_secret_ref)
        except Exception as exc:
            raise ResidentSecretError(
                "resident_secret_store_unavailable", access_id=current.access_id
            ) from exc
        if raw is None:
            raise ResidentSecretError(
                "resident_secret_missing", access_id=current.access_id
            )
        try:
            envelope = ResidentSecretEnvelope.from_json(raw)
        except ResidentSecretError as exc:
            raise ResidentSecretError(
                exc.reason,
                access_id=current.access_id,
            ) from exc
        if not hmac.compare_digest(envelope.access_id, current.access_id):
            raise ResidentSecretError(
                "resident_secret_access_id_mismatch", access_id=current.access_id
            )
        if envelope.card_revision != current.card_revision:
            raise ResidentSecretError(
                "resident_secret_card_revision_mismatch", access_id=current.access_id
            )
        if not hmac.compare_digest(
            envelope.fingerprint, current.resident_access_sha256
        ):
            raise ResidentSecretError(
                "resident_secret_metadata_fingerprint_mismatch",
                access_id=current.access_id,
            )
        if envelope.expires_at != current.expires_at:
            raise ResidentSecretError(
                "resident_secret_expiry_mismatch", access_id=current.access_id
            )
        if envelope.expires_at <= moment:
            raise ResidentSecretError(
                "resident_secret_expired", access_id=current.access_id
            )
        return envelope.value

    async def retire(
        self,
        access_id: str,
        *,
        expected_revision: int,
        state: str,
    ) -> ResidentSecretRetirementResult:
        retired = await self._metadata.retire(
            access_id,
            expected_revision=expected_revision,
            state=state,
        )
        if retired is None:
            return ResidentSecretRetirementResult(metadata=None)
        cleaned, failure = await self._cleanup.cleanup_terminal_candidate(retired)
        return ResidentSecretRetirementResult(
            metadata=cleaned,
            cleanup_failure=failure,
        )

    async def cleanup_retired_secrets(
        self,
        *,
        limit: int = 100,
    ) -> ResidentSecretCleanupResult:
        return await self._cleanup.cleanup_retired_secrets(limit=limit)

    async def cleanup_prepared_secrets(
        self,
        *,
        limit: int = 100,
    ) -> ResidentSecretCleanupResult:
        return await self._cleanup.cleanup_prepared_secrets(limit=limit)

    async def cleanup_terminal_secrets(
        self,
        *,
        limit: int = 100,
    ) -> ResidentSecretCleanupResult:
        return await self._cleanup.cleanup_terminal_secrets(limit=limit)

    async def expire_due_and_cleanup(
        self,
        *,
        now: int | None = None,
        limit: int = 100,
    ) -> ResidentSecretCleanupResult:
        return await self._cleanup.expire_due_and_cleanup(now=now, limit=limit)

    async def purge_expired_secret_records(
        self,
        *,
        now: int | None = None,
        limit: int = 100,
    ) -> int:
        return await self._cleanup.purge_expired_secret_records(now=now, limit=limit)


__all__ = ["ResidentCardSecretService"]

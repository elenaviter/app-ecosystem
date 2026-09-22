# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Transactional installation and rotation of delegated-Card handles."""

from __future__ import annotations

from connection_hub.delegated_credentials.cards.handle_metadata import (
    HANDLE_STATE_ACTIVE,
    PREPARED_SECRET_STATE_INSTALLABLE,
    CardHandleMetadata,
    CardHandleMutationResult,
    PreparedResidentSecret,
)
from connection_hub.delegated_credentials.cards.handle_record_authority import (
    PostgresCardHandleRecordAuthority,
)
from connection_hub.delegated_credentials.cards.handle_records import (
    CARD_HANDLE_COLUMNS,
    retired_secret_from_row,
)
from connection_hub.delegated_credentials.cards.handle_schema import (
    TABLE_CARD_HANDLE_METADATA,
    TABLE_RETIRED_RESIDENT_SECRETS,
)
from connection_hub.delegated_credentials.cards.prepared_secret_authority import (
    PostgresPreparedSecretAuthority,
)
from connection_hub.delegated_credentials.cards.resident_secret_locking import (
    lock_resident_secret_refs,
)


class PostgresCardHandleMutationAuthority:
    """Install current metadata and record outgoing references atomically."""

    def __init__(
        self,
        *,
        records: PostgresCardHandleRecordAuthority,
        prepared_secrets: PostgresPreparedSecretAuthority,
        tenant: str,
        project: str,
    ) -> None:
        self._records = records
        self._prepared_secrets = prepared_secrets
        self.tenant = str(tenant or "").strip() or "default"
        self.project = str(project or "").strip() or "default"

    async def put(
        self,
        metadata: CardHandleMetadata,
        *,
        expected_revision: int,
    ) -> CardHandleMetadata:
        result = await self._put(
            metadata,
            expected_revision=expected_revision,
            require_prepared=False,
        )
        return result.metadata

    async def install_prepared_resident_secret(
        self,
        metadata: CardHandleMetadata,
        *,
        expected_revision: int,
    ) -> CardHandleMutationResult:
        return await self._put(
            metadata,
            expected_revision=expected_revision,
            require_prepared=True,
        )

    async def _put(
        self,
        metadata: CardHandleMetadata,
        *,
        expected_revision: int,
        require_prepared: bool,
    ) -> CardHandleMutationResult:
        candidate = metadata.validated()
        expected = int(expected_revision)
        if expected < 0:
            raise ValueError("expected Card-handle revision must not be negative")
        if require_prepared and not candidate.resident_access_secret_ref:
            raise ValueError("prepared resident secret reference is required")
        async with (
            self._records.pool.acquire() as connection,
            connection.transaction(),
        ):
                current = await self._records.locked(
                    connection,
                    candidate.access_id,
                )
                prepared_intent: PreparedResidentSecret | None = None
                if current is None:
                    if expected != 0:
                        raise self._records.conflict(
                            "card_handle_revision_conflict",
                            expected_revision=expected,
                            current_revision=0,
                        )
                    if candidate.resident_access_secret_ref and not require_prepared:
                        raise self._records.conflict(
                            "card_handle_prepared_secret_required",
                            expected_revision=expected,
                            current_revision=0,
                        )
                    if candidate.resident_access_secret_ref:
                        await lock_resident_secret_refs(
                            connection,
                            candidate.resident_access_secret_ref,
                        )
                        if require_prepared:
                            prepared_intent = (
                                await self._prepared_secrets.require_prepared_intent(
                                    connection,
                                    candidate=candidate,
                                    expected_revision=expected,
                                    current_revision=0,
                                )
                            )
                    row = await connection.fetchrow(
                        f"""
                        INSERT INTO
                            {self._records.schema}.{TABLE_CARD_HANDLE_METADATA} (
                            access_id, tenant, project, card_revision,
                            resident_access_secret_ref, resident_access_sha256,
                            session_id, state, retired_at, expires_at
                        ) VALUES (
                            $1, $2, $3, $4,
                            $5, $6,
                            $7, $8,
                            CASE WHEN $8 = 'active' THEN NULL ELSE now() END,
                            to_timestamp($9)
                        )
                        RETURNING {CARD_HANDLE_COLUMNS}
                        """,
                        candidate.access_id,
                        self.tenant,
                        self.project,
                        candidate.card_revision,
                        candidate.resident_access_secret_ref,
                        candidate.resident_access_sha256,
                        candidate.session_id,
                        candidate.state,
                        candidate.expires_at,
                    )
                    saved = self._records.record(row)
                    assert saved is not None
                    if prepared_intent is not None:
                        await self._prepared_secrets.consume_prepared_intent(
                            connection,
                            prepared=prepared_intent,
                        )
                    return CardHandleMutationResult(metadata=saved)
                if current.revision != expected:
                    raise self._records.conflict(
                        "card_handle_revision_conflict",
                        expected_revision=expected,
                        current_revision=current.revision,
                    )
                if current.payload_identity == candidate.payload_identity:
                    if require_prepared:
                        replay_intent = (
                            await self._prepared_secrets.locked_prepared_secret(
                                connection,
                                candidate.resident_access_secret_ref,
                            )
                        )
                        if replay_intent is not None:
                            if not self._prepared_secrets.prepared_matches_candidate(
                                replay_intent,
                                candidate,
                            ):
                                raise self._records.conflict(
                                    "card_handle_prepared_secret_mismatch",
                                    expected_revision=expected,
                                    current_revision=current.revision,
                                )
                            if (
                                replay_intent.state
                                != PREPARED_SECRET_STATE_INSTALLABLE
                            ):
                                raise self._records.conflict(
                                    "card_handle_prepared_secret_cleanup_started",
                                    expected_revision=expected,
                                    current_revision=current.revision,
                                )
                            await self._prepared_secrets.consume_prepared_intent(
                                connection,
                                prepared=replay_intent,
                            )
                    return CardHandleMutationResult(metadata=current)
                if candidate.card_revision < current.card_revision:
                    raise self._records.conflict(
                        "card_handle_card_revision_regressed",
                        expected_revision=expected,
                        current_revision=current.revision,
                    )
                if (
                    current.state != HANDLE_STATE_ACTIVE
                    and candidate.state == HANDLE_STATE_ACTIVE
                    and candidate.card_revision <= current.card_revision
                ):
                    raise self._records.conflict(
                        "card_handle_reactivation_requires_new_card_revision",
                        expected_revision=expected,
                        current_revision=current.revision,
                    )
                if (
                    current.state != HANDLE_STATE_ACTIVE
                    and candidate.state == HANDLE_STATE_ACTIVE
                    and current.resident_access_secret_ref
                    and candidate.resident_access_secret_ref
                    == current.resident_access_secret_ref
                ):
                    raise self._records.conflict(
                        "card_handle_reactivation_requires_fresh_secret_ref",
                        expected_revision=expected,
                        current_revision=current.revision,
                    )
                if (
                    current.resident_access_secret_ref
                    and candidate.resident_access_secret_ref
                    == current.resident_access_secret_ref
                    and candidate.card_revision != current.card_revision
                ):
                    raise self._records.conflict(
                        "card_handle_secret_card_revision_changed_without_new_ref",
                        expected_revision=expected,
                        current_revision=current.revision,
                    )
                if (
                    current.resident_access_secret_ref
                    and candidate.resident_access_secret_ref
                    == current.resident_access_secret_ref
                    and candidate.expires_at != current.expires_at
                ):
                    raise self._records.conflict(
                        "card_handle_secret_expiry_changed_without_new_ref",
                        expected_revision=expected,
                        current_revision=current.revision,
                    )
                if (
                    current.resident_access_secret_ref
                    and candidate.resident_access_secret_ref
                    == current.resident_access_secret_ref
                    and candidate.resident_access_sha256
                    != current.resident_access_sha256
                ):
                    raise self._records.conflict(
                        "card_handle_secret_fingerprint_changed_without_new_ref",
                        expected_revision=expected,
                        current_revision=current.revision,
                    )
                secret_replaced = bool(
                    current.resident_access_secret_ref
                    and candidate.resident_access_secret_ref
                    != current.resident_access_secret_ref
                )
                candidate_ref_changed = bool(
                    candidate.resident_access_secret_ref
                    and candidate.resident_access_secret_ref
                    != current.resident_access_secret_ref
                )
                secret_ref_changed = (
                    candidate.resident_access_secret_ref
                    != current.resident_access_secret_ref
                )
                if secret_ref_changed and not require_prepared:
                    raise self._records.conflict(
                        "card_handle_resident_secret_mutation_requires_custody_protocol",
                        expected_revision=expected,
                        current_revision=current.revision,
                    )
                if require_prepared and not candidate_ref_changed:
                    raise self._records.conflict(
                        "card_handle_prepared_secret_not_new",
                        expected_revision=expected,
                        current_revision=current.revision,
                    )
                if secret_replaced or candidate_ref_changed:
                    await lock_resident_secret_refs(
                        connection,
                        current.resident_access_secret_ref,
                        candidate.resident_access_secret_ref,
                    )
                if candidate_ref_changed and require_prepared:
                    prepared_intent = (
                        await self._prepared_secrets.require_prepared_intent(
                            connection,
                            candidate=candidate,
                            expected_revision=expected,
                            current_revision=current.revision,
                        )
                    )
                row = await connection.fetchrow(
                    f"""
                    UPDATE {self._records.schema}.{TABLE_CARD_HANDLE_METADATA}
                    SET card_revision = $2,
                        resident_access_secret_ref = $3,
                        resident_access_sha256 = $4,
                        session_id = $5,
                        state = $6,
                        revision = revision + 1,
                        updated_at = now(),
                        retired_at = CASE
                            WHEN $6 = 'active' THEN NULL
                            WHEN state = 'active' THEN now()
                            ELSE retired_at
                        END,
                        expires_at = to_timestamp($7),
                        cleanup_claim_token = '',
                        cleanup_claimed_at = NULL,
                        cleanup_claim_expires_at = NULL,
                        cleanup_next_attempt_at = now(),
                        cleanup_last_error = ''
                    WHERE access_id = $1
                    RETURNING {CARD_HANDLE_COLUMNS}
                    """,
                    candidate.access_id,
                    candidate.card_revision,
                    candidate.resident_access_secret_ref,
                    candidate.resident_access_sha256,
                    candidate.session_id,
                    candidate.state,
                    candidate.expires_at,
                )
                retired_secret = None
                if secret_replaced:
                    retired_row = await connection.fetchrow(
                        f"""
                        INSERT INTO
                            {self._records.schema}.{TABLE_RETIRED_RESIDENT_SECRETS} (
                            secret_ref, access_id, resident_access_sha256, retired_at
                        )
                        SELECT $1, $2, $3, now()
                        WHERE NOT EXISTS (
                            SELECT 1
                            FROM
                                {self._records.schema}.{TABLE_CARD_HANDLE_METADATA}
                            WHERE resident_access_secret_ref = $1
                        )
                        RETURNING
                            access_id,
                            secret_ref,
                            resident_access_sha256,
                            EXTRACT(EPOCH FROM retired_at)::bigint AS retired_at
                        """,
                        current.resident_access_secret_ref,
                        current.access_id,
                        current.resident_access_sha256,
                    )
                    if retired_row is None:
                        raise self._records.conflict(
                            "card_handle_secret_ref_still_current",
                            expected_revision=expected,
                            current_revision=current.revision,
                        )
                    retired_secret = retired_secret_from_row(retired_row)
                saved = self._records.record(row)
                assert saved is not None
                if prepared_intent is not None:
                    await self._prepared_secrets.consume_prepared_intent(
                        connection,
                        prepared=prepared_intent,
                    )
                return CardHandleMutationResult(
                    metadata=saved,
                    retired_secret=retired_secret,
                )


__all__ = ["PostgresCardHandleMutationAuthority"]

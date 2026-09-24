# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Fenced apply protocol for a reviewed durable-authority preview."""

from __future__ import annotations

import re
from typing import Protocol

from connection_hub.delegated_credentials.authority_cutover import (
    AuthorityCutoverConflict,
    AuthorityCutoverReceipt,
)
from connection_hub.delegated_credentials.migration.model import (
    AuthorityMigrationInspection,
    AuthorityMigrationPreview,
    AuthorityMigrationRecord,
    AuthorityMigrationSnapshot,
    MigrationEvidenceMismatch,
    verify_migration_preview,
)


class AuthorityMigrationSource(Protocol):
    async def inspect(
        self,
        *,
        captured_at_ms: int | None = None,
    ) -> AuthorityMigrationInspection: ...


class AuthorityMigrationTarget(Protocol):
    async def synchronize(self, source: AuthorityMigrationSnapshot) -> None: ...

    async def import_record(self, record: AuthorityMigrationRecord) -> bool: ...

    async def snapshot(
        self,
        *,
        captured_at_ms: int | None = None,
    ) -> AuthorityMigrationSnapshot: ...


class AuthorityMigrationImportFailed(RuntimeError):
    """One named source record could not be reproduced in the target."""

    def __init__(
        self,
        *,
        record_type: str,
        identity: str,
        reason: str,
    ) -> None:
        self.record_type = str(record_type or "")
        self.identity = str(identity or "")
        self.reason = str(reason or "authority_migration_import_failed")
        super().__init__(
            "authority_migration_record_import_failed:"
            f"{self.record_type}:{self.identity}:{self.reason}"
        )


_SAFE_IMPORT_REASON = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,255}$")


def _import_failure_reason(exc: Exception) -> str:
    candidate = str(getattr(exc, "reason", "") or str(exc) or "").strip()
    if _SAFE_IMPORT_REASON.fullmatch(candidate):
        return candidate
    return type(exc).__name__


class AuthorityCutoverReceiptTarget(Protocol):
    async def read(self, generation_id: str) -> AuthorityCutoverReceipt | None: ...

    async def activate(
        self,
        receipt: AuthorityCutoverReceipt,
    ) -> AuthorityCutoverReceipt: ...


def _receipt_for_preview(
    preview: AuthorityMigrationPreview,
    *,
    target: AuthorityMigrationSnapshot,
) -> AuthorityCutoverReceipt:
    reviewed = preview.validated()
    destination = target.validated()
    return AuthorityCutoverReceipt(
        generation_id=reviewed.generation_id,
        source_generation=reviewed.source_generation,
        target_generation=destination.generation,
        source_counts=reviewed.source_counts,
        target_counts=destination.counts,
        prerequisites=reviewed.prerequisites,
        preview_sha256=reviewed.preview_sha256,
    ).validated()


def _receipt_matches_preview(
    receipt: AuthorityCutoverReceipt,
    preview: AuthorityMigrationPreview,
) -> bool:
    activated = receipt.validated()
    reviewed = preview.validated()
    return (
        activated.generation_id == reviewed.generation_id
        and activated.source_generation == reviewed.source_generation
        and activated.target_generation == reviewed.source_generation
        and dict(activated.source_counts) == dict(reviewed.source_counts)
        and dict(activated.target_counts) == dict(reviewed.source_counts)
        and dict(activated.prerequisites) == dict(reviewed.prerequisites)
        and activated.preview_sha256 == reviewed.preview_sha256
    )


def _reviewed_preview(
    preview: AuthorityMigrationPreview,
    *,
    confirmed_preview_sha256: str,
) -> AuthorityMigrationPreview:
    reviewed = preview.validated()
    confirmed = str(confirmed_preview_sha256 or "").strip().lower()
    if confirmed != reviewed.preview_sha256:
        raise MigrationEvidenceMismatch("authority_migration_preview_not_confirmed")
    if reviewed.blockers:
        raise MigrationEvidenceMismatch("authority_migration_preview_has_blockers")
    return reviewed


async def _import_and_reconcile(
    *,
    reviewed: AuthorityMigrationPreview,
    source: AuthorityMigrationSource,
    target: AuthorityMigrationTarget,
) -> AuthorityMigrationSnapshot:
    inspection = (await source.inspect()).validated()
    verify_migration_preview(reviewed, inspection)
    snapshot = inspection.snapshot
    try:
        await target.synchronize(snapshot)
    except Exception as exc:
        raise AuthorityMigrationImportFailed(
            record_type="authority_snapshot",
            identity=snapshot.generation,
            reason=_import_failure_reason(exc),
        ) from exc
    for record in snapshot.records:
        try:
            await target.import_record(record)
        except Exception as exc:
            raise AuthorityMigrationImportFailed(
                record_type=record.record_type,
                identity=record.identity,
                reason=_import_failure_reason(exc),
            ) from exc
    destination = (
        await target.snapshot(captured_at_ms=snapshot.captured_at_ms)
    ).validated()
    destination_by_key = {
        (record.record_type, record.identity): record
        for record in destination.records
    }
    source_keys: set[tuple[str, str]] = set()
    for record in snapshot.records:
        key = (record.record_type, record.identity)
        source_keys.add(key)
        target_record = destination_by_key.get(key)
        if target_record is None:
            raise AuthorityMigrationImportFailed(
                record_type=record.record_type,
                identity=record.identity,
                reason="target_record_missing",
            )
        if target_record.evidence() != record.evidence():
            raise AuthorityMigrationImportFailed(
                record_type=record.record_type,
                identity=record.identity,
                reason="target_record_content_mismatch",
            )
    for record in destination.records:
        if (record.record_type, record.identity) not in source_keys:
            raise AuthorityMigrationImportFailed(
                record_type=record.record_type,
                identity=record.identity,
                reason="target_record_unexpected",
            )
    if destination.counts != snapshot.counts:
        raise MigrationEvidenceMismatch("authority_migration_target_counts_mismatch")
    if destination.generation != snapshot.generation:
        raise MigrationEvidenceMismatch(
            "authority_migration_target_generation_mismatch"
        )
    return destination


async def rehearse_reviewed_migration(
    *,
    preview: AuthorityMigrationPreview,
    source: AuthorityMigrationSource,
    target: AuthorityMigrationTarget,
    confirmed_preview_sha256: str,
) -> AuthorityMigrationSnapshot:
    """Import and reconcile reviewed data within a caller-owned rollback fence."""

    reviewed = _reviewed_preview(
        preview,
        confirmed_preview_sha256=confirmed_preview_sha256,
    )
    return await _import_and_reconcile(
        reviewed=reviewed,
        source=source,
        target=target,
    )


async def apply_reviewed_migration(
    *,
    preview: AuthorityMigrationPreview,
    source: AuthorityMigrationSource,
    target: AuthorityMigrationTarget,
    receipts: AuthorityCutoverReceiptTarget,
    confirmed_preview_sha256: str,
    source_is_quiesced: bool,
) -> AuthorityCutoverReceipt:
    """Apply once, reconcile exact logical data, then activate the receipt.

    The operator supplies the reviewed preview hash and an explicit quiescence
    assertion. Only then is the source read again. A receipt is the final
    write. Its presence makes an exact rerun return immediately without
    reading or mutating the retired Redis source.
    """

    reviewed = _reviewed_preview(
        preview,
        confirmed_preview_sha256=confirmed_preview_sha256,
    )
    existing = await receipts.read(reviewed.generation_id)
    if existing is not None:
        if not _receipt_matches_preview(existing, reviewed):
            raise AuthorityCutoverConflict(
                "authority_cutover_generation_id_already_has_different_evidence"
            )
        return existing
    if not source_is_quiesced:
        raise MigrationEvidenceMismatch("authority_migration_source_not_quiesced")
    destination = await _import_and_reconcile(
        reviewed=reviewed,
        source=source,
        target=target,
    )
    receipt = _receipt_for_preview(reviewed, target=destination)
    return await receipts.activate(receipt)


__all__ = [
    "AuthorityCutoverReceiptTarget",
    "AuthorityMigrationImportFailed",
    "AuthorityMigrationSource",
    "AuthorityMigrationTarget",
    "apply_reviewed_migration",
    "rehearse_reviewed_migration",
]

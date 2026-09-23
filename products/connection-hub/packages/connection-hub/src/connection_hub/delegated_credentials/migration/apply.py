# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Fenced apply protocol for a reviewed durable-authority preview."""

from __future__ import annotations

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
    async def import_record(self, record: AuthorityMigrationRecord) -> bool: ...

    async def snapshot(
        self,
        *,
        captured_at_ms: int | None = None,
    ) -> AuthorityMigrationSnapshot: ...


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

    reviewed = preview.validated()
    confirmed = str(confirmed_preview_sha256 or "").strip().lower()
    if confirmed != reviewed.preview_sha256:
        raise MigrationEvidenceMismatch("authority_migration_preview_not_confirmed")
    if reviewed.blockers:
        raise MigrationEvidenceMismatch("authority_migration_preview_has_blockers")
    existing = await receipts.read(reviewed.generation_id)
    if existing is not None:
        if not _receipt_matches_preview(existing, reviewed):
            raise AuthorityCutoverConflict(
                "authority_cutover_generation_id_already_has_different_evidence"
            )
        return existing
    if not source_is_quiesced:
        raise MigrationEvidenceMismatch("authority_migration_source_not_quiesced")
    inspection = (await source.inspect()).validated()
    verify_migration_preview(reviewed, inspection)
    snapshot = inspection.snapshot
    for record in snapshot.records:
        await target.import_record(record)
    destination = await target.snapshot(captured_at_ms=snapshot.captured_at_ms)
    if destination.counts != snapshot.counts:
        raise MigrationEvidenceMismatch("authority_migration_target_counts_mismatch")
    if destination.generation != snapshot.generation:
        raise MigrationEvidenceMismatch(
            "authority_migration_target_generation_mismatch"
        )
    receipt = _receipt_for_preview(reviewed, target=destination)
    return await receipts.activate(receipt)


__all__ = [
    "AuthorityCutoverReceiptTarget",
    "AuthorityMigrationSource",
    "AuthorityMigrationTarget",
    "apply_reviewed_migration",
]

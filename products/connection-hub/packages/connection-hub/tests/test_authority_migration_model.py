from __future__ import annotations

import json

import pytest

from connection_hub.delegated_credentials.authority_cutover import (
    AuthorityCutoverReceipt,
)
from connection_hub.delegated_credentials.migration.apply import (
    apply_reviewed_migration,
)
from connection_hub.delegated_credentials.migration.artifact import (
    read_migration_preview,
    write_migration_preview,
)
from connection_hub.delegated_credentials.migration.model import (
    AuthorityMigrationInspection,
    AuthorityMigrationPreview,
    AuthorityMigrationRecord,
    AuthorityMigrationSnapshot,
    MigrationEvidenceMismatch,
    build_migration_preview,
)


def _snapshot(*, payload_value: str = "value") -> AuthorityMigrationSnapshot:
    return AuthorityMigrationSnapshot(
        tenant="demo-tenant",
        project="demo-project",
        records=(
            AuthorityMigrationRecord(
                record_type="oauth_refresh",
                identity="a" * 64,
                families=("oauth_refresh_families",),
                payload={
                    "bearer_sha256": "a" * 64,
                    "migration_state": "active",
                    "record": {"x": payload_value},
                },
                expires_at_ms=2_000_000_000_000,
                secrets={"bearer": "raw-secret-never-in-preview"},
            ),
        ),
        declared_families=("oauth_refresh_families", "oauth_access_bindings"),
        captured_at_ms=1_900_000_000_000,
    ).validated()


def _preview(snapshot: AuthorityMigrationSnapshot) -> AuthorityMigrationPreview:
    return build_migration_preview(
        snapshot,
        generation_id="durable-authority-v1",
        prerequisites={
            "card_identity_cutover": {
                "receipt_sha256": "b" * 64,
                "reviewed": True,
            }
        },
    )


def test_preview_contains_counts_and_digests_without_record_or_secret_material() -> None:
    snapshot = _snapshot()
    preview = _preview(snapshot)

    encoded = json.dumps(preview.to_dict(), sort_keys=True)

    assert preview.source_counts == {
        "oauth_access_bindings": 0,
        "oauth_refresh_families": 1,
    }
    assert preview.source_generation == snapshot.generation
    assert preview.source_summary == {"record_types": {"oauth_refresh": 1}}
    assert preview.blockers == ()
    assert "raw-secret-never-in-preview" not in encoded
    assert '"record"' not in encoded
    assert '"identity"' not in encoded
    assert AuthorityMigrationPreview.from_mapping(preview.to_dict()) == preview


def test_preview_hash_covers_prerequisites_and_source_generation() -> None:
    preview = _preview(_snapshot())
    changed = dict(preview.to_dict())
    changed["prerequisites"] = {"card_identity_cutover": {"reviewed": False}}

    with pytest.raises(ValueError, match="preview_sha256"):
        AuthorityMigrationPreview.from_mapping(changed)


class _Target:
    def __init__(self, source: AuthorityMigrationSnapshot) -> None:
        self.source = source
        self.imported: list[str] = []

    async def import_record(self, record: AuthorityMigrationRecord) -> bool:
        self.imported.append(record.identity)
        return True

    async def snapshot(self, *, captured_at_ms=None) -> AuthorityMigrationSnapshot:
        return self.source


class _Source:
    def __init__(self, snapshot: AuthorityMigrationSnapshot) -> None:
        self.snapshot = snapshot
        self.inspect_count = 0

    async def inspect(self, *, captured_at_ms=None):
        from connection_hub.delegated_credentials.migration.model import (
            inspection_from_snapshot,
        )

        self.inspect_count += 1
        return inspection_from_snapshot(self.snapshot)


class _Receipts:
    def __init__(self, existing: AuthorityCutoverReceipt | None = None) -> None:
        self.existing = existing
        self.activated: list[AuthorityCutoverReceipt] = []

    async def read(self, generation_id: str) -> AuthorityCutoverReceipt | None:
        return self.existing

    async def activate(
        self,
        receipt: AuthorityCutoverReceipt,
    ) -> AuthorityCutoverReceipt:
        self.activated.append(receipt)
        self.existing = receipt
        return receipt


@pytest.mark.asyncio
async def test_apply_imports_then_writes_the_exact_reconciled_receipt() -> None:
    source = _snapshot()
    preview = _preview(source)
    target = _Target(source)
    migration_source = _Source(source)
    receipts = _Receipts()

    receipt = await apply_reviewed_migration(
        preview=preview,
        source=migration_source,
        target=target,
        receipts=receipts,
        confirmed_preview_sha256=preview.preview_sha256,
        source_is_quiesced=True,
    )

    assert target.imported == ["a" * 64]
    assert migration_source.inspect_count == 1
    assert receipts.activated == [receipt]
    assert receipt.source_counts == source.counts
    assert receipt.target_counts == source.counts
    assert receipt.source_generation == source.generation
    assert receipt.target_generation == source.generation


@pytest.mark.asyncio
async def test_exact_receipt_rerun_does_not_read_or_mutate_retired_source() -> None:
    source = _snapshot()
    preview = _preview(source)
    receipt = AuthorityCutoverReceipt(
        generation_id=preview.generation_id,
        source_generation=preview.source_generation,
        target_generation=preview.source_generation,
        source_counts=preview.source_counts,
        target_counts=preview.source_counts,
        prerequisites=preview.prerequisites,
        preview_sha256=preview.preview_sha256,
    ).validated()
    target = _Target(_snapshot(payload_value="different-live-target"))
    migration_source = _Source(_snapshot(payload_value="retired-source-is-no-longer-read"))
    receipts = _Receipts(existing=receipt)

    repeated = await apply_reviewed_migration(
        preview=preview,
        source=migration_source,
        target=target,
        receipts=receipts,
        confirmed_preview_sha256=preview.preview_sha256,
        source_is_quiesced=True,
    )

    assert repeated == receipt
    assert migration_source.inspect_count == 0
    assert target.imported == []
    assert receipts.activated == []


@pytest.mark.asyncio
async def test_apply_requires_explicit_quiescence_and_exact_preview_confirmation() -> None:
    source = _snapshot()
    preview = _preview(source)

    with pytest.raises(MigrationEvidenceMismatch, match="preview_not_confirmed"):
        await apply_reviewed_migration(
            preview=preview,
            source=_Source(source),
            target=_Target(source),
            receipts=_Receipts(),
            confirmed_preview_sha256="0" * 64,
            source_is_quiesced=True,
        )

    with pytest.raises(MigrationEvidenceMismatch, match="source_not_quiesced"):
        await apply_reviewed_migration(
            preview=preview,
            source=_Source(source),
            target=_Target(source),
            receipts=_Receipts(),
            confirmed_preview_sha256=preview.preview_sha256,
            source_is_quiesced=False,
        )


@pytest.mark.asyncio
async def test_preview_artifact_round_trips_through_async_file_boundary(tmp_path) -> None:
    preview = _preview(_snapshot())
    path = tmp_path / "authority-preview.json"

    written = await write_migration_preview(path, preview)
    loaded = await read_migration_preview(path)

    assert written == path
    assert loaded == preview


@pytest.mark.asyncio
async def test_apply_refuses_reviewed_blockers_before_reading_source() -> None:
    snapshot = _snapshot()
    preview = build_migration_preview(
        AuthorityMigrationInspection(
            snapshot=snapshot,
            source_summary={"record_types": {"oauth_refresh": 1}},
            blockers=("legacy_card_reconciliation_required",),
        ),
        generation_id="durable-authority-v1",
        prerequisites={"card_identity_cutover": {"reviewed": True}},
    )
    source = _Source(snapshot)

    with pytest.raises(MigrationEvidenceMismatch, match="preview_has_blockers"):
        await apply_reviewed_migration(
            preview=preview,
            source=source,
            target=_Target(snapshot),
            receipts=_Receipts(),
            confirmed_preview_sha256=preview.preview_sha256,
            source_is_quiesced=True,
        )

    assert source.inspect_count == 0

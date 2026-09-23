# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Canonical, secret-safe evidence for a durable-authority migration.

Snapshots are transient and may contain source credentials needed by an import.
Previews never contain record identities or payloads. They carry only scope,
aggregate count buckets, immutable prerequisite evidence, blocker codes, and
SHA-256 generations.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence


AUTHORITY_MIGRATION_SNAPSHOT_SCHEMA = "connection-hub.authority-snapshot.v1"
AUTHORITY_MIGRATION_PREVIEW_SCHEMA = "connection-hub.authority-preview.v1"

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,511}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_RECORD_TYPE_ORDER = {
    "bundle_user_authority": 10,
    "oauth_client": 10,
    "bundle_session": 20,
    "oauth_refresh": 20,
    "oauth_access": 30,
    "card_handles": 40,
    "platform_session": 50,
    "admission_replay": 60,
}


class MigrationEvidenceMismatch(RuntimeError):
    """The live source differs from the operator-reviewed preview."""

    def __init__(self, reason: str) -> None:
        self.reason = str(reason or "authority_migration_evidence_mismatch")
        super().__init__(self.reason)


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("authority migration evidence must be JSON serializable") from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _validated_identifier(value: Any, *, field_name: str) -> str:
    candidate = str(value or "").strip()
    if not _IDENTIFIER_PATTERN.fullmatch(candidate):
        raise ValueError(f"{field_name} must be a bounded stable identifier")
    return candidate


def _validated_families(values: Iterable[Any]) -> tuple[str, ...]:
    families = tuple(
        sorted(
            {
                _validated_identifier(value, field_name="record family")
                for value in values
            }
        )
    )
    if not families:
        raise ValueError("authority migration record requires at least one family")
    return families


def _validated_counts(value: Mapping[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for raw_family, raw_count in dict(value or {}).items():
        family = _validated_identifier(raw_family, field_name="family count name")
        if isinstance(raw_count, bool) or not isinstance(raw_count, int):
            raise ValueError(f"family count {family} must be an integer")
        count = int(raw_count)
        if count < 0:
            raise ValueError(f"family count {family} must be non-negative")
        counts[family] = count
    return dict(sorted(counts.items()))


def _validated_summary(
    value: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, int]]:
    summary: dict[str, dict[str, int]] = {}
    for raw_category, raw_counts in dict(value or {}).items():
        category = _validated_identifier(
            raw_category,
            field_name="source summary category",
        )
        if not isinstance(raw_counts, Mapping):
            raise ValueError(
                f"source summary category {category} must contain count buckets"
            )
        summary[category] = _validated_counts(raw_counts)
    if not summary:
        raise ValueError("source_summary must contain aggregate count buckets")
    return dict(sorted(summary.items()))


def _validated_blockers(values: Iterable[Any]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                _validated_identifier(value, field_name="migration blocker")
                for value in values
            }
        )
    )


def _record_type_counts(
    snapshot: "AuthorityMigrationSnapshot",
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in snapshot.records:
        counts[record.record_type] = counts.get(record.record_type, 0) + 1
    return dict(sorted(counts.items()))


@dataclass(frozen=True)
class AuthorityMigrationRecord:
    """One logical source record and every authority family it satisfies.

    ``identity`` must already be non-secret. For bearer-keyed records it is the
    bearer SHA-256, never the bearer itself. ``payload`` remains process-local;
    only its digest contributes to preview evidence.
    """

    record_type: str
    identity: str
    families: Sequence[str]
    payload: Mapping[str, Any]
    expires_at_ms: int | None = None
    secrets: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def validated(self) -> "AuthorityMigrationRecord":
        record_type = _validated_identifier(
            self.record_type,
            field_name="record_type",
        )
        identity = _validated_identifier(self.identity, field_name="record identity")
        families = _validated_families(self.families)
        payload = dict(self.payload or {})
        _canonical_json(payload)
        secrets = dict(self.secrets or {})
        _canonical_json(secrets)
        expires_at_ms = self.expires_at_ms
        if expires_at_ms is not None:
            expires_at_ms = int(expires_at_ms)
            if expires_at_ms <= 0:
                raise ValueError("record expiry must be a positive Unix millisecond")
        return AuthorityMigrationRecord(
            record_type=record_type,
            identity=identity,
            families=families,
            payload=payload,
            expires_at_ms=expires_at_ms,
            secrets=secrets,
        )

    def evidence(self) -> dict[str, Any]:
        record = self.validated()
        return {
            "record_type": record.record_type,
            "identity_sha256": hashlib.sha256(
                record.identity.encode("utf-8")
            ).hexdigest(),
            "families": list(record.families),
            "payload_sha256": _sha256_json(record.payload),
            "expires_at_ms": record.expires_at_ms,
        }


@dataclass(frozen=True)
class AuthorityMigrationSnapshot:
    tenant: str
    project: str
    records: Sequence[AuthorityMigrationRecord]
    declared_families: Sequence[str]
    captured_at_ms: int
    schema: str = AUTHORITY_MIGRATION_SNAPSHOT_SCHEMA

    def validated(self) -> "AuthorityMigrationSnapshot":
        if self.schema != AUTHORITY_MIGRATION_SNAPSHOT_SCHEMA:
            raise ValueError("authority migration snapshot schema is unsupported")
        tenant = _validated_identifier(self.tenant, field_name="tenant")
        project = _validated_identifier(self.project, field_name="project")
        declared_families = _validated_families(self.declared_families)
        captured_at_ms = int(self.captured_at_ms)
        if captured_at_ms <= 0:
            raise ValueError("snapshot capture time must be a Unix millisecond")
        records = tuple(record.validated() for record in self.records)
        seen: set[tuple[str, str]] = set()
        for record in records:
            key = (record.record_type, record.identity)
            if key in seen:
                raise ValueError(
                    "authority migration snapshot contains a duplicate logical record"
                )
            seen.add(key)
            unknown = set(record.families).difference(declared_families)
            if unknown:
                raise ValueError(
                    "authority migration record names undeclared families: "
                    + ",".join(sorted(unknown))
                )
        return AuthorityMigrationSnapshot(
            tenant=tenant,
            project=project,
            records=tuple(
                sorted(
                    records,
                    key=lambda item: (
                        _RECORD_TYPE_ORDER.get(item.record_type, 100),
                        item.record_type,
                        item.identity,
                    ),
                )
            ),
            declared_families=declared_families,
            captured_at_ms=captured_at_ms,
        )

    @property
    def counts(self) -> dict[str, int]:
        snapshot = self.validated()
        counts = {family: 0 for family in snapshot.declared_families}
        for record in snapshot.records:
            for family in record.families:
                counts[family] += 1
        return dict(sorted(counts.items()))

    @property
    def generation(self) -> str:
        snapshot = self.validated()
        return _sha256_json(
            {
                "schema": snapshot.schema,
                "tenant": snapshot.tenant,
                "project": snapshot.project,
                "counts": snapshot.counts,
                "records": [record.evidence() for record in snapshot.records],
            }
        )


@dataclass(frozen=True)
class AuthorityMigrationInspection:
    """One transient source read plus its secret-safe operator evidence."""

    snapshot: AuthorityMigrationSnapshot
    source_summary: Mapping[str, Mapping[str, int]]
    blockers: Sequence[str] = ()

    def validated(self) -> "AuthorityMigrationInspection":
        return AuthorityMigrationInspection(
            snapshot=self.snapshot.validated(),
            source_summary=_validated_summary(self.source_summary),
            blockers=_validated_blockers(self.blockers),
        )


def inspection_from_snapshot(
    snapshot: AuthorityMigrationSnapshot,
) -> AuthorityMigrationInspection:
    source = snapshot.validated()
    return AuthorityMigrationInspection(
        snapshot=source,
        source_summary={"record_types": _record_type_counts(source)},
    ).validated()


@dataclass(frozen=True)
class AuthorityMigrationPreview:
    generation_id: str
    tenant: str
    project: str
    source_generation: str
    source_counts: Mapping[str, int]
    source_summary: Mapping[str, Mapping[str, int]]
    prerequisites: Mapping[str, Any]
    blockers: Sequence[str]
    created_at_ms: int
    preview_sha256: str = ""
    schema: str = AUTHORITY_MIGRATION_PREVIEW_SCHEMA

    def _body(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "generation_id": self.generation_id,
            "tenant": self.tenant,
            "project": self.project,
            "source_generation": self.source_generation,
            "expected_target_generation": self.source_generation,
            "source_counts": dict(self.source_counts),
            "source_summary": {
                category: dict(counts)
                for category, counts in self.source_summary.items()
            },
            "prerequisites": dict(self.prerequisites),
            "blockers": list(self.blockers),
            "created_at_ms": self.created_at_ms,
        }

    def validated(self) -> "AuthorityMigrationPreview":
        if self.schema != AUTHORITY_MIGRATION_PREVIEW_SCHEMA:
            raise ValueError("authority migration preview schema is unsupported")
        generation_id = _validated_identifier(
            self.generation_id,
            field_name="generation_id",
        )
        tenant = _validated_identifier(self.tenant, field_name="tenant")
        project = _validated_identifier(self.project, field_name="project")
        source_generation = str(self.source_generation or "").strip().lower()
        if not _SHA256_PATTERN.fullmatch(source_generation):
            raise ValueError("source_generation must be a SHA-256 digest")
        source_counts = _validated_counts(self.source_counts)
        if not source_counts:
            raise ValueError("source_counts must name every durable family")
        source_summary = _validated_summary(self.source_summary)
        prerequisites = dict(self.prerequisites or {})
        if not prerequisites:
            raise ValueError("migration prerequisite evidence is required")
        _canonical_json(prerequisites)
        blockers = _validated_blockers(self.blockers)
        created_at_ms = int(self.created_at_ms)
        if created_at_ms <= 0:
            raise ValueError("preview creation time must be a Unix millisecond")
        candidate = AuthorityMigrationPreview(
            generation_id=generation_id,
            tenant=tenant,
            project=project,
            source_generation=source_generation,
            source_counts=source_counts,
            source_summary=source_summary,
            prerequisites=prerequisites,
            blockers=blockers,
            created_at_ms=created_at_ms,
            schema=self.schema,
        )
        expected_hash = _sha256_json(candidate._body())
        supplied_hash = str(self.preview_sha256 or "").strip().lower()
        if supplied_hash and supplied_hash != expected_hash:
            raise ValueError("preview_sha256 does not match the preview body")
        return AuthorityMigrationPreview(
            **{**candidate.__dict__, "preview_sha256": expected_hash}
        )

    def to_dict(self) -> dict[str, Any]:
        preview = self.validated()
        return {**preview._body(), "preview_sha256": preview.preview_sha256}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AuthorityMigrationPreview":
        payload = dict(value or {})
        expected_target = str(payload.pop("expected_target_generation", "") or "")
        preview = cls(
            schema=str(payload.get("schema") or ""),
            generation_id=str(payload.get("generation_id") or ""),
            tenant=str(payload.get("tenant") or ""),
            project=str(payload.get("project") or ""),
            source_generation=str(payload.get("source_generation") or ""),
            source_counts=dict(payload.get("source_counts") or {}),
            source_summary=dict(payload.get("source_summary") or {}),
            prerequisites=dict(payload.get("prerequisites") or {}),
            blockers=tuple(payload.get("blockers") or ()),
            created_at_ms=int(payload.get("created_at_ms") or 0),
            preview_sha256=str(payload.get("preview_sha256") or ""),
        ).validated()
        if expected_target and expected_target != preview.source_generation:
            raise ValueError(
                "expected_target_generation must equal the logical source generation"
            )
        return preview


def build_migration_preview(
    source: AuthorityMigrationInspection | AuthorityMigrationSnapshot,
    *,
    generation_id: str,
    prerequisites: Mapping[str, Any],
) -> AuthorityMigrationPreview:
    inspection = (
        source.validated()
        if isinstance(source, AuthorityMigrationInspection)
        else inspection_from_snapshot(source)
    )
    snapshot = inspection.snapshot
    return AuthorityMigrationPreview(
        generation_id=generation_id,
        tenant=snapshot.tenant,
        project=snapshot.project,
        source_generation=snapshot.generation,
        source_counts=snapshot.counts,
        source_summary=inspection.source_summary,
        prerequisites=dict(prerequisites or {}),
        blockers=inspection.blockers,
        created_at_ms=snapshot.captured_at_ms,
    ).validated()


def verify_migration_preview(
    preview: AuthorityMigrationPreview,
    source: AuthorityMigrationInspection | AuthorityMigrationSnapshot,
) -> None:
    reviewed = preview.validated()
    inspection = (
        source.validated()
        if isinstance(source, AuthorityMigrationInspection)
        else inspection_from_snapshot(source)
    )
    snapshot = inspection.snapshot
    if (reviewed.tenant, reviewed.project) != (snapshot.tenant, snapshot.project):
        raise MigrationEvidenceMismatch("authority_migration_scope_changed")
    if reviewed.source_counts != snapshot.counts:
        raise MigrationEvidenceMismatch("authority_migration_source_counts_changed")
    if reviewed.source_generation != snapshot.generation:
        raise MigrationEvidenceMismatch("authority_migration_source_generation_changed")
    if reviewed.source_summary != inspection.source_summary:
        raise MigrationEvidenceMismatch("authority_migration_source_summary_changed")
    if tuple(reviewed.blockers) != tuple(inspection.blockers):
        raise MigrationEvidenceMismatch("authority_migration_source_blockers_changed")


def combine_migration_snapshots(
    snapshots: Sequence[AuthorityMigrationSnapshot],
    *,
    captured_at_ms: int | None = None,
) -> AuthorityMigrationSnapshot:
    if not snapshots:
        raise ValueError("at least one authority migration snapshot is required")
    validated = [snapshot.validated() for snapshot in snapshots]
    scopes = {(snapshot.tenant, snapshot.project) for snapshot in validated}
    if len(scopes) != 1:
        raise ValueError("authority migration snapshots must share one scope")
    tenant, project = next(iter(scopes))
    return AuthorityMigrationSnapshot(
        tenant=tenant,
        project=project,
        records=tuple(record for snapshot in validated for record in snapshot.records),
        declared_families=tuple(
            family
            for snapshot in validated
            for family in snapshot.declared_families
        ),
        captured_at_ms=int(
            captured_at_ms
            if captured_at_ms is not None
            else max(snapshot.captured_at_ms for snapshot in validated)
        ),
    ).validated()


def combine_migration_inspections(
    inspections: Sequence[AuthorityMigrationInspection],
    *,
    captured_at_ms: int | None = None,
) -> AuthorityMigrationInspection:
    if not inspections:
        raise ValueError("at least one authority migration inspection is required")
    validated = [inspection.validated() for inspection in inspections]
    combined_summary: dict[str, dict[str, int]] = {}
    for inspection in validated:
        for category, counts in inspection.source_summary.items():
            target = combined_summary.setdefault(category, {})
            for bucket, count in counts.items():
                target[bucket] = target.get(bucket, 0) + count
    return AuthorityMigrationInspection(
        snapshot=combine_migration_snapshots(
            [inspection.snapshot for inspection in validated],
            captured_at_ms=captured_at_ms,
        ),
        source_summary=combined_summary,
        blockers=tuple(
            blocker
            for inspection in validated
            for blocker in inspection.blockers
        ),
    ).validated()


__all__ = [
    "AUTHORITY_MIGRATION_PREVIEW_SCHEMA",
    "AUTHORITY_MIGRATION_SNAPSHOT_SCHEMA",
    "AuthorityMigrationInspection",
    "AuthorityMigrationPreview",
    "AuthorityMigrationRecord",
    "AuthorityMigrationSnapshot",
    "MigrationEvidenceMismatch",
    "build_migration_preview",
    "combine_migration_inspections",
    "combine_migration_snapshots",
    "inspection_from_snapshot",
    "verify_migration_preview",
]

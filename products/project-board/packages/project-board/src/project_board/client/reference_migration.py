from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from ..contract.errors import DomainError
from ..contract.reference_records import (
    discover_reference_mapping,
    merge_reference_mappings,
    reference_for_legacy_observation,
    referenced_legacy_refs,
)
from ..contract.refs import make_ref, normalize_reference_mapping, parse_ref
from .io import atomic_write_json, component, content_hash, read_json, utc_now
from .plan_migration import _busy_refs, _project_record_paths, _rehash, _rewrite


REFERENCE_MIGRATION_PREVIEW_SCHEMA = (
    "problem-board.local-reference-migration-preview.v2"
)
REFERENCE_MIGRATION_RECEIPT_SCHEMA = (
    "problem-board.local-reference-migration-receipt.v1"
)

_LOCAL_OBSERVATION_POLICIES = (
    (
        frozenset({"problem-board.service-outbox.v1"}),
        ("remote_ref",),
        ("created_at", "settled_at", "updated_at"),
        frozenset({"control"}),
    ),
    (
        frozenset({"problem-board.service-outbox.v1"}),
        ("payload", "source_message_ref"),
        ("created_at", "updated_at"),
        frozenset({"mail"}),
    ),
    (
        frozenset({"problem-board.service-outbox.v1"}),
        ("payload", "reply_to"),
        ("created_at", "updated_at"),
        frozenset({"mail"}),
    ),
    (
        frozenset({"problem-board.service-outbox.v1"}),
        ("payload", "correlation_id"),
        ("created_at", "updated_at"),
        frozenset({"control", "mail"}),
    ),
    (
        frozenset({"problem-board.local-mail.v1"}),
        ("payload", "command", "report_ref"),
        ("created_at", "updated_at"),
        frozenset({"report"}),
    ),
    (
        frozenset({"problem-board.service-outbox.v1"}),
        ("object_ref",),
        ("created_at", "updated_at"),
        frozenset({"report"}),
    ),
)


def _path_value(value: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = value
    for segment in path:
        if not isinstance(current, Mapping):
            return ""
        current = current.get(segment)
    return current


def _local_observation_mapping(
    documents: list[Mapping[str, Any]],
    *,
    known_mapping: Mapping[str, str],
) -> dict[str, str]:
    """Recover ownerless refs from the first allowed durable observation."""

    recovered: dict[str, str] = {}
    for document in documents:
        for schemas, path, timestamp_fields, allowed_kinds in (
            _LOCAL_OBSERVATION_POLICIES
        ):
            if document.get("schema") not in schemas:
                continue
            source = str(_path_value(document, path) or "").strip()
            if not source or source in known_mapping:
                continue
            try:
                parsed = parse_ref(source)
            except DomainError:
                continue
            if parsed.is_canonical or parsed.kind not in allowed_kinds:
                continue
            observed_at = next(
                (
                    document.get(field)
                    for field in timestamp_fields
                    if str(document.get(field) or "").strip()
                ),
                None,
            )
            if observed_at is None:
                continue
            target = reference_for_legacy_observation(
                source,
                observed_at=observed_at,
            )
            previous = recovered.get(source)
            recovered[source] = (
                target
                if previous is None
                else merge_reference_mappings(
                    {source: previous},
                    {source: target},
                )[source]
            )
    return normalize_reference_mapping(recovered)


def _plan_owner_documents(field: Any, project_id: str) -> list[Mapping[str, Any]]:
    """Read plan owners without admitting immutable plan history to the rewrite."""

    plans_root = field._project_dir(project_id) / "plans"
    return [
        value
        for path in sorted(plans_root.glob("*/manifest.json"))
        if (value := read_json(path, required=False))
    ]


def _reference_record_paths(field: Any, project_id: str) -> list[Path]:
    """Select mutable records while preserving every migration receipt."""

    receipt_root = (
        field._project_dir(project_id) / "receipts" / "reference-migrations"
    )
    return [
        path
        for path in _project_record_paths(field, project_id)
        if receipt_root not in path.parents
    ]


def _canonical_related_reference(
    value: str,
    mapping: Mapping[str, str],
    targets: frozenset[str],
) -> str:
    rewritten, changed = _rewrite(value, mapping)
    if changed:
        return str(rewritten)
    if value in targets:
        return value
    if not value.startswith("work:"):
        return ""
    try:
        parsed = parse_ref(value)
    except DomainError:
        return ""
    return value if parsed.identity_ref in targets else ""


def _reference_projection_hash(
    value: Any,
    mapping: Mapping[str, str],
) -> str:
    """Hash mapped references and their locations, independent of other fields."""

    targets = frozenset(mapping.values())
    occurrences: list[dict[str, Any]] = []

    def visit(current: Any, path: tuple[dict[str, Any], ...]) -> None:
        if isinstance(current, str):
            canonical = _canonical_related_reference(current, mapping, targets)
            if canonical:
                occurrences.append(
                    {"path": list(path), "reference": canonical}
                )
            return
        if isinstance(current, list):
            for index, item in enumerate(current):
                visit(item, (*path, {"sequence_index": index}))
            return
        if not isinstance(current, Mapping):
            return
        entries: list[tuple[str, str, str, Any]] = []
        for key, item in current.items():
            key_text = str(key)
            canonical_key = _canonical_related_reference(
                key_text,
                mapping,
                targets,
            )
            entries.append(
                (canonical_key or key_text, key_text, canonical_key, item)
            )
        for path_key, key_text, canonical_key, item in sorted(entries):
            if canonical_key:
                occurrences.append(
                    {
                        "path": [*path, {"mapping_key": path_key}],
                        "reference": canonical_key,
                    }
                )
            visit(item, (*path, {"mapping_value": path_key or key_text}))

    visit(value, ())
    return content_hash(occurrences)


def discover_local_reference_mapping(field, project_id: str) -> dict[str, str]:
    """Derive canonical identities from authoritative project-local records."""

    clean_project = component(project_id, field="project_id")
    documents = [
        value
        for path in _reference_record_paths(field, clean_project)
        if (value := read_json(path, required=False))
    ]
    authoritative = discover_reference_mapping(
        [*documents, *_plan_owner_documents(field, clean_project)]
    )
    observed = _local_observation_mapping(
        documents,
        known_mapping=authoritative,
    )
    return merge_reference_mappings(authoritative, observed)


def preview_local_reference_migration(
    field,
    project_id: str,
    *,
    reference_mapping: Mapping[str, str],
    lease_owner: str = "",
) -> dict[str, Any]:
    """Project the exact local JSON rewrite without changing the field."""

    clean_project = component(project_id, field="project_id")
    project_ref = make_ref("project", clean_project)
    mapping = normalize_reference_mapping(reference_mapping)
    files: list[dict[str, Any]] = []
    projected_documents: list[Any] = []
    for path in _reference_record_paths(field, clean_project):
        value = read_json(path, required=False)
        if not value:
            continue
        rewritten, changed = _rewrite(value, mapping)
        projected = _rehash(rewritten)
        projected_documents.append(projected)
        if not changed:
            continue
        reference_projection_hash = _reference_projection_hash(value, mapping)
        if reference_projection_hash != _reference_projection_hash(
            projected,
            mapping,
        ):
            raise DomainError(
                "field_reference_migration_projection_invalid",
                "Canonical reference projection changed while building the migration preview.",
                status=500,
                details={"path": path.relative_to(field.control).as_posix()},
            )
        files.append(
            {
                "path": path.relative_to(field.control).as_posix(),
                "reference_projection_hash": reference_projection_hash,
            }
        )
    unresolved = sorted(
        set().union(
            *(referenced_legacy_refs(value) for value in projected_documents)
        )
        if projected_documents
        else set()
    )
    if unresolved:
        raise DomainError(
            "field_reference_migration_unresolved",
            "Stored local references remain unresolved; supply their canonical mapping before applying the migration.",
            status=409,
            details={
                "unresolved_reference_count": len(unresolved),
                "unresolved_references": unresolved[:100],
            },
        )
    stable = {
        "schema": REFERENCE_MIGRATION_PREVIEW_SCHEMA,
        "project_ref": project_ref,
        "reference_mapping": mapping,
        "reference_mapping_hash": content_hash(mapping),
        "files": sorted(files, key=lambda row: str(row["path"])),
        "unresolved_reference_count": 0,
        "unresolved_references": [],
    }
    return {
        **stable,
        "busy_refs": _busy_refs(
            field,
            clean_project,
            mapping,
            allowed_lease_owner=lease_owner,
        ),
        "local_migration_hash": content_hash(stable),
        "rewritten_file_count": len(files),
    }


def apply_local_reference_migration(
    field,
    project_id: str,
    *,
    reviewed_preview: Mapping[str, Any],
    lease_owner: str = "",
    allow_reference_catch_up: bool = False,
) -> dict[str, Any]:
    """Apply one reviewed local rewrite, accepting an interrupted partial replay."""

    clean_project = component(project_id, field="project_id")
    project_ref = make_ref("project", clean_project)
    preview = dict(reviewed_preview or {})
    if (
        preview.get("schema") != REFERENCE_MIGRATION_PREVIEW_SCHEMA
        or preview.get("project_ref") != project_ref
    ):
        raise DomainError(
            "field_reference_migration_preview_invalid",
            "The reviewed local reference preview belongs to another project or schema.",
            status=409,
        )
    mapping = normalize_reference_mapping(preview.get("reference_mapping"))
    expected_hash = str(preview.get("local_migration_hash") or "")
    stable = {
        key: preview.get(key)
        for key in (
            "schema",
            "project_ref",
            "reference_mapping",
            "reference_mapping_hash",
            "files",
            "unresolved_reference_count",
            "unresolved_references",
        )
    }
    if not expected_hash or content_hash(stable) != expected_hash:
        raise DomainError(
            "field_reference_migration_preview_invalid",
            "The reviewed local reference preview hash does not match its contents.",
            status=409,
        )
    expected_files = {
        str(row.get("path") or ""): dict(row)
        for row in preview.get("files") or []
        if isinstance(row, Mapping) and str(row.get("path") or "")
    }

    with field._project_lock_context(clean_project):
        busy = _busy_refs(
            field,
            clean_project,
            mapping,
            allowed_lease_owner=lease_owner,
        )
        if busy:
            raise DomainError(
                "field_reference_migration_busy",
                "A local record that must be rewritten is currently leased.",
                status=409,
                details={"retryable": True, "leased_refs": busy},
            )
        current_paths = _reference_record_paths(field, clean_project)
        current_by_relative = {
            path.relative_to(field.control).as_posix(): path
            for path in current_paths
        }
        catch_up_files: dict[str, dict[str, Any]] = {}
        projected_documents: list[Any] = []
        for relative, path in current_by_relative.items():
            value = read_json(path, required=False)
            if not value:
                continue
            rewritten, changed = _rewrite(value, mapping)
            projected_documents.append(_rehash(rewritten))
            if changed and relative not in expected_files:
                catch_up_files[relative] = {
                    "path": relative,
                    "reference_projection_hash": _reference_projection_hash(
                        value,
                        mapping,
                    ),
                }
        unresolved = sorted(
            set().union(
                *(
                    referenced_legacy_refs(value)
                    for value in projected_documents
                )
            )
            if projected_documents
            else set()
        )
        if unresolved:
            raise DomainError(
                "field_reference_migration_unresolved",
                "New local records contain legacy references outside the reviewed mapping.",
                status=409,
                details={
                    "unresolved_reference_count": len(unresolved),
                    "unresolved_references": unresolved[:100],
                },
            )

        rewritten_files: list[str] = []
        replayed_files: list[str] = []
        retired_files: list[str] = []
        advanced_files: list[str] = []
        files_to_apply = {**expected_files, **catch_up_files}
        for relative, expected in sorted(files_to_apply.items()):
            path = current_by_relative.get(relative)
            value = read_json(path, required=False) if path is not None else {}
            if not value:
                retired_files.append(relative)
                continue
            expected_projection_hash = str(
                expected.get("reference_projection_hash") or ""
            )
            observed_projection_hash = _reference_projection_hash(
                value,
                mapping,
            )
            if (
                not expected_projection_hash
                or observed_projection_hash != expected_projection_hash
            ):
                if not allow_reference_catch_up:
                    raise DomainError(
                        "field_reference_migration_preview_changed",
                        "A reviewed local record's reference-bearing state changed before migration.",
                        status=409,
                        details={
                            "path": relative,
                            "expected_reference_projection_hash": expected_projection_hash,
                            "observed_reference_projection_hash": observed_projection_hash,
                        },
                    )
                advanced_files.append(relative)
                expected_projection_hash = observed_projection_hash
            rewritten, changed = _rewrite(value, mapping)
            projected = _rehash(rewritten)
            if not changed:
                replayed_files.append(relative)
                continue
            if (
                _reference_projection_hash(projected, mapping)
                != expected_projection_hash
            ):
                raise DomainError(
                    "field_reference_migration_preview_changed",
                    "The current rewrite no longer matches the reviewed reference projection.",
                    status=409,
                    details={"path": relative},
                )
            atomic_write_json(path, projected)
            rewritten_files.append(relative)
    return {
        "schema": REFERENCE_MIGRATION_RECEIPT_SCHEMA,
        "project_ref": project_ref,
        "local_migration_hash": expected_hash,
        "reference_mapping_hash": content_hash(mapping),
        "rewritten_files": rewritten_files,
        "replayed_files": replayed_files,
        "catch_up_files": sorted(catch_up_files),
        "retired_files": retired_files,
        "advanced_files": advanced_files,
        "completed_at": utc_now(),
    }


def reference_migration_receipt_path(
    field,
    project_id: str,
    migration_id: str,
) -> Path:
    clean_project = component(project_id, field="project_id")
    clean_migration = component(migration_id, field="migration_id")
    return (
        field._project_dir(clean_project)
        / "receipts"
        / "reference-migrations"
        / f"{clean_migration}.json"
    )


__all__ = [
    "REFERENCE_MIGRATION_PREVIEW_SCHEMA",
    "REFERENCE_MIGRATION_RECEIPT_SCHEMA",
    "apply_local_reference_migration",
    "discover_local_reference_mapping",
    "preview_local_reference_migration",
    "reference_migration_receipt_path",
]

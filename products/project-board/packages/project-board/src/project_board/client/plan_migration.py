from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..contract.errors import DomainError
from ..contract.plan_nodes import parse_plan_node_ref, plan_node_ref_for_item
from ..contract.reference_records import (
    discover_reference_mapping,
    merge_reference_mappings,
    referenced_legacy_refs,
)
from ..contract.refs import make_ref, normalize_reference_mapping, parse_ref
from .io import atomic_write_json, component, content_hash, read_json, utc_now
from .plan_storage import (
    BucketedPlanStore,
    PLAN_MIGRATION_RECEIPT_SCHEMA,
    PLAN_STORAGE_BUCKETED,
)


PLAN_MIGRATION_PREPARATION_SCHEMA = "problem-board.plan-storage-migration-preparation.v1"


@dataclass(frozen=True)
class PlanMigrationResult:
    migration_id: str
    state: str
    mapping_hash: str
    plan_count: int
    item_count: int
    rewritten_files: tuple[str, ...]
    removed_legacy_files: tuple[str, ...]
    receipt_path: Path

    def as_dict(self) -> dict[str, Any]:
        return {
            "migration_id": self.migration_id,
            "state": self.state,
            "mapping_hash": self.mapping_hash,
            "plan_count": self.plan_count,
            "item_count": self.item_count,
            "rewritten_files": list(self.rewritten_files),
            "removed_legacy_files": list(self.removed_legacy_files),
            "receipt_path": str(self.receipt_path),
        }


def _legacy_plan_files(plans_root: Path) -> list[Path]:
    return sorted(
        path
        for path in plans_root.glob("*.json")
        if path.name != ".storage.json" and not path.name.startswith(".")
    )


def _canonicalize_plans(
    plans: Sequence[Mapping[str, Any]],
    *,
    preferred_plan_refs: Sequence[str] = (),
    reference_restorations: Mapping[str, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str], int]:
    canonical: list[dict[str, Any]] = []
    candidates: dict[str, list[tuple[str, str, str]]] = {}
    item_count = 0
    restorations = _normalize_reference_restorations(reference_restorations)
    restored_sources: set[str] = set()

    for raw_plan in plans:
        plan = dict(raw_plan)
        plan_id = component(plan.get("plan_id"), field="plan_id")
        items: list[dict[str, Any]] = []
        plan_mapping: dict[str, str] = {}
        plan_item_refs: set[str] = set()
        for raw_item in plan.get("items") or []:
            if not isinstance(raw_item, Mapping):
                continue
            item = dict(raw_item)
            item_id = component(item.get("item_id"), field="item_id")
            legacy_plan_ref = f"work:plan:node:{item_id}"
            old_ref = str(item.get("item_ref") or legacy_plan_ref)
            try:
                canonical_ref = parse_plan_node_ref(old_ref).ref
            except DomainError:
                new_ref = plan_node_ref_for_item(item)
            else:
                # A canonical URI records the creation-time semantic label.
                # The title is mutable and must never rename established
                # identity during a later storage or cutover check.
                new_ref = restorations.get(canonical_ref, canonical_ref)
                if new_ref != canonical_ref:
                    restored_sources.add(canonical_ref)
            if new_ref in plan_item_refs:
                raise DomainError(
                    "field_plan_migration_uri_collision",
                    "Two nodes in one plan resolve to the same canonical URI.",
                    status=409,
                    details={"item_ref": new_ref},
                )
            plan_item_refs.add(new_ref)
            aliases = {
                old_ref,
                legacy_plan_ref,
                f"work:item:{item_id}",
            }
            for alias in aliases:
                existing = plan_mapping.get(alias)
                if existing is not None and existing != new_ref:
                    raise DomainError(
                        "field_plan_migration_reference_ambiguous",
                        "One legacy node reference resolves to more than one node in the same plan.",
                        status=409,
                        details={"item_ref": alias},
                    )
                plan_mapping[alias] = new_ref
                candidates.setdefault(alias, []).append(
                    (
                        str(plan.get("plan_ref") or ""),
                        str(plan.get("created_at") or plan.get("updated_at") or ""),
                        new_ref,
                    )
                )
            item["item_ref"] = new_ref
            items.append(item)
            item_count += 1

        def resolve(value: str) -> str:
            if value in plan_mapping:
                return plan_mapping[value]
            if ":" not in value:
                legacy_ref = f"work:plan:node:{value}"
                if legacy_ref in plan_mapping:
                    return plan_mapping[legacy_ref]
            return value

        dependencies: list[dict[str, Any]] = []
        item_refs = {str(item["item_ref"]) for item in items}
        for raw_edge in plan.get("dependencies") or []:
            if not isinstance(raw_edge, Mapping):
                continue
            edge = dict(raw_edge)
            source = str(edge.get("item_ref") or edge.get("item_id") or "")
            target = str(
                edge.get("depends_on_ref") or edge.get("depends_on_item_id") or ""
            )
            edge["item_ref"] = resolve(source)
            edge["depends_on_ref"] = resolve(target)
            edge.pop("item_id", None)
            edge.pop("depends_on_item_id", None)
            if edge["item_ref"] not in item_refs or edge["depends_on_ref"] not in item_refs:
                raise DomainError(
                    "field_plan_migration_dependency_unresolved",
                    "A legacy dependency could not be resolved to canonical node URIs.",
                    status=409,
                    details={
                        "item_ref": edge["item_ref"],
                        "depends_on_ref": edge["depends_on_ref"],
                    },
                )
            dependencies.append(edge)
        plan["items"] = items
        plan["dependencies"] = dependencies
        # The canonical URI changes the published index and projection. A new
        # generation is required even though the node's authored content and
        # user-visible update time did not change.
        plan["revision"] = int(plan.get("revision") or 0) + 1
        canonical.append(plan)

    missing_restorations = sorted(set(restorations) - restored_sources)
    if missing_restorations:
        raise DomainError(
            "field_plan_migration_restore_source_missing",
            "Every restored URI must name a current canonical plan node.",
            status=409,
            details={"work_refs": missing_restorations[:20]},
        )

    preference = {
        ref: ordinal
        for ordinal, ref in enumerate(
            dict.fromkeys(str(value or "") for value in preferred_plan_refs)
        )
        if ref
    }
    mapping: dict[str, str] = {}
    for alias, rows in candidates.items():
        preferred = [row for row in rows if row[0] in preference]
        if preferred:
            selected = min(preferred, key=lambda row: preference[row[0]])
        else:
            # Legacy refs carried no plan identity. For an alias reused across
            # historical plans, the newest occurrence is the only deterministic
            # target once active/current plans have been considered.
            selected = max(rows, key=lambda row: (row[1], row[0], row[2]))
        mapping[alias] = selected[2]

    return canonical, mapping, item_count


def _normalize_reference_restorations(
    value: Mapping[str, str] | None,
) -> dict[str, str]:
    """Validate explicit repairs without allowing identity-prefix changes."""

    normalized: dict[str, str] = {}
    for raw_source, raw_target in dict(value or {}).items():
        source = parse_plan_node_ref(raw_source)
        target = parse_plan_node_ref(raw_target)
        if source.version_timestamp is not None or target.version_timestamp is not None:
            raise DomainError(
                "field_plan_migration_restore_versioned_ref",
                "A current plan-node identity repair requires unversioned URIs.",
                status=409,
            )
        if source.prefix != target.prefix:
            raise DomainError(
                "field_plan_migration_restore_identity_mismatch",
                "A restored URI must retain the node creation timestamp and key.",
                status=409,
                details={"source_ref": source.ref, "target_ref": target.ref},
            )
        if source.ref == target.ref:
            raise DomainError(
                "field_plan_migration_restore_identity",
                "A restored URI must differ from the current URI.",
                status=409,
                details={"work_ref": source.ref},
            )
        normalized[source.ref] = target.ref
    return dict(sorted(normalized.items()))


def _require_restoration_revision(
    *,
    project: Mapping[str, Any],
    storage_state: Mapping[str, Any],
    restorations: Mapping[str, str],
    expected_project_revision: int | None,
) -> None:
    if not restorations:
        return
    if expected_project_revision is None:
        raise DomainError(
            "field_plan_migration_restore_revision_required",
            "A canonical URI restoration requires the current project revision.",
            status=409,
        )
    current_revision = int(project.get("revision") or 0)
    source_revision = (
        int(storage_state.get("source_project_revision") or 0)
        if storage_state.get("state") == "migrating"
        else current_revision
    )
    if int(expected_project_revision) != source_revision:
        raise DomainError(
            "field_plan_migration_restore_revision_conflict",
            "The supplied revision does not match the restoration source revision.",
            status=409,
            details={
                "expected_revision": int(expected_project_revision),
                "source_revision": source_revision,
                "current_revision": current_revision,
            },
        )


def _contains(value: Any, expected: str) -> bool:
    if isinstance(value, str):
        return value == expected
    if isinstance(value, Mapping):
        return any(_contains(key, expected) or _contains(item, expected) for key, item in value.items())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains(item, expected) for item in value)
    return False


def _rewrite(value: Any, mapping: Mapping[str, str]) -> tuple[Any, bool]:
    if isinstance(value, str):
        replacement = mapping.get(value, value)
        if replacement == value and value.startswith("work:"):
            try:
                address = parse_ref(value)
            except DomainError:
                address = None
            if address is not None and address.version is not None:
                target = mapping.get(address.identity_ref)
                if target:
                    replacement = f"{target}:{address.version}"
        return replacement, replacement != value
    if isinstance(value, list):
        changed = False
        result = []
        for item in value:
            rewritten, item_changed = _rewrite(item, mapping)
            result.append(rewritten)
            changed = changed or item_changed
        return result, changed
    if isinstance(value, Mapping):
        changed = False
        result: dict[Any, Any] = {}
        for key, item in value.items():
            rewritten_key, key_changed = _rewrite(key, mapping)
            rewritten_item, item_changed = _rewrite(item, mapping)
            if rewritten_key in result and rewritten_key != key:
                raise DomainError(
                    "field_plan_migration_key_collision",
                    "Canonicalizing plan references would merge two stored fields.",
                    status=409,
                )
            result[rewritten_key] = rewritten_item
            changed = changed or key_changed or item_changed
        return result, changed
    return value, False


def _rehash(value: Any) -> Any:
    if isinstance(value, list):
        return [_rehash(item) for item in value]
    if not isinstance(value, Mapping):
        return value
    row = {key: _rehash(item) for key, item in value.items()}
    schema = str(row.get("schema") or "")
    if "payload_hash" in row and isinstance(row.get("payload"), Mapping):
        row["payload_hash"] = content_hash(row["payload"])
    if schema in {
        "problem-board.plan-index.v1",
        "problem-board.plan-index.v2",
        "problem-board.plan-nodes.v1",
        "problem-board.project-projection.v1",
    } and "content_hash" in row:
        row["content_hash"] = content_hash(
            {key: item for key, item in row.items() if key != "content_hash"}
        )
    if schema == "problem-board.local-mail.v1" and "content_hash" in row:
        row["content_hash"] = content_hash(
            {key: item for key, item in row.items() if key != "content_hash"}
        )
    if schema == "problem-board.local-assignment.v1" and "content_hash" in row:
        row["content_hash"] = content_hash(
            {
                key: item
                for key, item in row.items()
                if key not in {"content_hash", "received_at"}
            }
        )
    if schema == "problem-board.service-outbox.v1":
        kind = str(row.get("kind") or "")
        payload = row.get("payload")
        if kind in {
            "plan.index.publish",
            "plan.nodes.publish",
            "projection.publish",
        } and isinstance(payload, Mapping):
            row["content_hash"] = str(payload.get("content_hash") or "")
        elif kind in {
            "assignment.report",
            "control.worker_settle",
            "event.publish",
            "mail.route",
            "project.plan.index",
        } and isinstance(payload, Mapping):
            row["content_hash"] = content_hash(payload)
        elif kind == "project.report.publish" and isinstance(payload, Mapping):
            document = payload.get("document")
            attachments = payload.get("attachment_files")
            manifest = [
                {
                    key: item.get(key)
                    for key in ("filename", "mime", "size", "sha256")
                }
                for item in (attachments or [])
                if isinstance(item, Mapping)
            ]
            if isinstance(document, Mapping):
                row["content_hash"] = content_hash(
                    {
                        "action": "project.report.publish",
                        "report_ref": str(row.get("object_ref") or ""),
                        "document": document,
                        "attachments": manifest,
                    }
                )
        elif kind == "project.report.fail" and isinstance(payload, Mapping):
            row["content_hash"] = content_hash(
                {
                    "action": "project.report.fail",
                    "report_ref": str(row.get("object_ref") or ""),
                    **payload,
                }
            )
    return row


def _project_record_paths(field, project_id: str) -> list[Path]:
    project_dir = field._project_dir(project_id)
    plans_root = project_dir / "plans"
    project_ref = make_ref("project", project_id)
    paths: set[Path] = set()
    for path in project_dir.rglob("*.json"):
        if path == plans_root or plans_root in path.parents:
            continue
        paths.add(path)
    for path in field.control.rglob("*.json"):
        if path in paths or path == field.manifest_path:
            continue
        if project_dir == path or project_dir in path.parents:
            continue
        value = read_json(path, required=False)
        if value and _contains(value, project_ref):
            paths.add(path)
    return sorted(paths)


def _busy_refs(
    field,
    project_id: str,
    reference_mapping: Mapping[str, str],
    *,
    allowed_lease_owner: str = "",
) -> list[str]:
    project_ref = make_ref("project", project_id)
    sources = tuple(reference_mapping)
    if not sources:
        return []
    refs: list[str] = []
    for path in field.control.rglob("*.json"):
        if path.parent.name != "leased":
            continue
        row = read_json(path, required=False)
        if (
            not row
            or not _contains(row, project_ref)
            or not any(_contains(row, source) for source in sources)
        ):
            continue
        lease = row.get("lease")
        if (
            allowed_lease_owner
            and isinstance(lease, Mapping)
            and str(lease.get("owner") or "") == allowed_lease_owner
        ):
            continue
        refs.append(
            str(
                row.get("message_ref")
                or row.get("command_ref")
                or row.get("outbox_id")
                or path.stem
            )
        )
    return sorted(set(refs))


def _bucketed_plans(storage: BucketedPlanStore) -> list[dict[str, Any]]:
    if not storage.plans_root.is_dir():
        return []
    return [
        storage.read(path.name)
        for path in sorted(storage.plans_root.iterdir())
        if path.is_dir() and storage.manifest_path(path.name).is_file()
    ]


def _preparation_path(plans_root: Path, migration_id: str) -> Path:
    return plans_root / "migrations" / f"{migration_id}.prepared.json"


def _used_reference_mapping(
    mapping: Mapping[str, str],
    documents: Sequence[Any],
) -> dict[str, str]:
    """Keep aliases that are actually present in this project package."""

    return {
        str(source): str(target)
        for source, target in mapping.items()
        if str(source) != str(target)
        and any(_contains(document, str(source)) for document in documents)
    }


def _prepare_reference_migration(
    raw_plans: Sequence[Mapping[str, Any]],
    record_documents: Sequence[Mapping[str, Any]],
    *,
    preferred_plan_refs: Sequence[str],
    reference_restorations: Mapping[str, str],
) -> tuple[list[dict[str, Any]], dict[str, str], int]:
    """Prepare one complete local rewrite or refuse before changing storage."""

    source_documents: list[Any] = [*raw_plans, *record_documents]
    plans, plan_candidates, item_count = _canonicalize_plans(
        raw_plans,
        preferred_plan_refs=preferred_plan_refs,
        reference_restorations=reference_restorations,
    )
    discovered = discover_reference_mapping(source_documents)
    mapping = merge_reference_mappings(
        discovered,
        _used_reference_mapping(plan_candidates, source_documents),
    )
    rewritten_plans, _changed = _rewrite(plans, mapping)
    rewritten_records = [_rewrite(record, mapping)[0] for record in record_documents]
    unresolved = sorted(
        set().union(
            *(referenced_legacy_refs(value) for value in rewritten_plans),
            *(referenced_legacy_refs(value) for value in rewritten_records),
        )
    )
    if unresolved:
        raise DomainError(
            "field_reference_migration_unresolved",
            "Stored Problem Board references remain unresolved; repair or retire them before retrying the migration.",
            status=409,
            details={
                "unresolved_reference_count": len(unresolved),
                "unresolved_references": unresolved[:100],
            },
        )
    return [dict(plan) for plan in rewritten_plans], mapping, item_count


def migrate_project_plan_storage(
    field,
    project_id: str,
    *,
    migration_id: str = "",
    publish_nodes: bool = True,
    reference_restorations: Mapping[str, str] | None = None,
    expected_project_revision: int | None = None,
) -> PlanMigrationResult:
    clean_project = component(project_id, field="project_id")
    project_ref = make_ref("project", clean_project)
    plans_root = field._project_dir(clean_project) / "plans"
    storage = BucketedPlanStore(plans_root)
    restorations = _normalize_reference_restorations(reference_restorations)
    # A restoration prepares the exact local package for operator-reviewed cutover.
    # Publishing here would bypass that review even when a caller leaves the
    # generic migration default enabled.
    publish_nodes = bool(publish_nodes and not restorations)
    restore_operation_id = ""
    if restorations:
        if not migration_id:
            raise DomainError(
                "field_plan_migration_restore_id_required",
                "A canonical URI restoration requires one stable migration ID.",
                status=409,
            )
        restore_operation_id = component(migration_id, field="migration_id")

    with field._project_lock_context(clean_project):
        project = field.read_project(clean_project)
        legacy_files = _legacy_plan_files(plans_root)
        state = storage.state()
        record_paths = _project_record_paths(field, clean_project)
        record_documents = [
            value
            for path in record_paths
            if (value := read_json(path, required=False))
        ]
        if restorations and expected_project_revision is None:
            raise DomainError(
                "field_plan_migration_restore_revision_required",
                "A canonical URI restoration requires the current project revision.",
                status=409,
            )
        completed_restore_path = (
            plans_root / "migrations" / f"{restore_operation_id}.json"
            if restore_operation_id
            else None
        )
        completed_restore = (
            read_json(completed_restore_path, required=False)
            if completed_restore_path is not None
            else {}
        )
        if completed_restore:
            if (
                completed_restore.get("schema") != PLAN_MIGRATION_RECEIPT_SCHEMA
                or completed_restore.get("project_ref") != project_ref
                or dict(completed_restore.get("reference_restorations") or {})
                != restorations
                or int(completed_restore.get("source_project_revision") or 0)
                != int(expected_project_revision)
            ):
                raise DomainError(
                    "field_plan_migration_restore_replay_conflict",
                    "The migration ID already belongs to a different URI restoration.",
                    status=409,
                    details={"migration_id": restore_operation_id},
                )
            return PlanMigrationResult(
                migration_id=restore_operation_id,
                state="already_migrated",
                mapping_hash=str(completed_restore.get("mapping_hash") or ""),
                plan_count=int(completed_restore.get("plan_count") or 0),
                item_count=int(completed_restore.get("item_count") or 0),
                rewritten_files=(),
                removed_legacy_files=(),
                receipt_path=completed_restore_path,
            )
        _require_restoration_revision(
            project=project,
            storage_state=state,
            restorations=restorations,
            expected_project_revision=expected_project_revision,
        )
        preparation: dict[str, Any] = {}
        if state.get("state") == "migrating":
            operation_id = str(state.get("migration_id") or "")
            if restore_operation_id and operation_id != restore_operation_id:
                raise DomainError(
                    "field_plan_migration_preparation_invalid",
                    "The active plan migration has a different migration ID.",
                    status=409,
                )
            preparation = read_json(_preparation_path(plans_root, operation_id))
            if (
                preparation.get("schema") != PLAN_MIGRATION_PREPARATION_SCHEMA
                or preparation.get("mapping_hash") != state.get("mapping_hash")
                or dict(preparation.get("reference_restorations") or {})
                != restorations
            ):
                raise DomainError(
                    "field_plan_migration_preparation_invalid",
                    "The prepared plan migration does not match the active migration.",
                    status=409,
                )
            plans = [dict(row) for row in preparation.get("plans") or []]
            mapping = normalize_reference_mapping(
                preparation.get("reference_mapping")
            )
            item_count = int(preparation.get("item_count") or 0)
            source_storage_state = str(
                preparation.get("source_storage_state") or "legacy"
            )
            mapping_hash = str(preparation.get("mapping_hash") or "")
        elif legacy_files:
            raw_plans = [read_json(path) for path in legacy_files]
            source_storage_state = "legacy"
            plans, mapping, item_count = _prepare_reference_migration(
                raw_plans,
                record_documents,
                preferred_plan_refs=(
                    str(project.get("active_plan_ref") or ""),
                    str(project.get("current_plan_ref") or ""),
                ),
                reference_restorations=restorations,
            )
            mapping_hash = content_hash(
                {
                    "project_ref": project_ref,
                    "plans": [str(plan.get("plan_ref") or "") for plan in plans],
                    "references": sorted(mapping.items()),
                }
            )
            operation_id = component(
                migration_id or f"plan-storage-v2-{mapping_hash[:12]}",
                field="migration_id",
            )
        elif state.get("state") == PLAN_STORAGE_BUCKETED:
            raw_plans = _bucketed_plans(storage)
            source_storage_state = PLAN_STORAGE_BUCKETED
            plans, mapping, item_count = _prepare_reference_migration(
                raw_plans,
                record_documents,
                preferred_plan_refs=(
                    str(project.get("active_plan_ref") or ""),
                    str(project.get("current_plan_ref") or ""),
                ),
                reference_restorations=restorations,
            )
            if not mapping:
                receipt = plans_root / str(state.get("receipt") or "")
                return PlanMigrationResult(
                    migration_id=str(state.get("migration_id") or ""),
                    state="already_migrated",
                    mapping_hash=str(state.get("mapping_hash") or ""),
                    plan_count=int(state.get("plan_count") or 0),
                    item_count=int(state.get("item_count") or 0),
                    rewritten_files=(),
                    removed_legacy_files=(),
                    receipt_path=receipt,
                )
            mapping_hash = content_hash(
                {
                    "project_ref": project_ref,
                    "plans": [str(plan.get("plan_ref") or "") for plan in plans],
                    "references": sorted(mapping.items()),
                }
            )
            operation_id = component(
                restore_operation_id
                or migration_id
                or f"plan-uri-v2-{mapping_hash[:12]}",
                field="migration_id",
            )
        else:
            raise DomainError(
                "field_plan_migration_source_missing",
                "No plan documents are available for migration.",
                status=404,
            )
        busy = _busy_refs(field, clean_project, mapping)
        if busy:
            raise DomainError(
                "field_plan_migration_busy",
                "Plan references cannot be rewritten while project records are leased.",
                status=409,
                details={"retryable": True, "leased_refs": busy},
            )

        if state.get("state") == "migrating":
            source_project_revision = int(
                state.get("source_project_revision") or 0
            )
            target_project_revision = int(
                state.get("target_project_revision") or 0
            )
        else:
            source_project_revision = int(project.get("revision") or 0)
            target_project_revision = source_project_revision + 1
            preparation = {
                "schema": PLAN_MIGRATION_PREPARATION_SCHEMA,
                "migration_id": operation_id,
                "project_ref": project_ref,
                "mapping_hash": mapping_hash,
                "source_storage_state": source_storage_state,
                "source_project_revision": source_project_revision,
                "target_project_revision": target_project_revision,
                "item_count": item_count,
                "plans": plans,
                "reference_mapping": dict(sorted(mapping.items())),
                "reference_restorations": restorations,
                "prepared_at": utc_now(),
            }
            atomic_write_json(_preparation_path(plans_root, operation_id), preparation)
        migration_state = storage.begin_migration(
            migration_id=operation_id,
            mapping_hash=mapping_hash,
            plan_count=len(plans),
            source_project_revision=source_project_revision,
            target_project_revision=target_project_revision,
            rewrite_bucketed=source_storage_state == PLAN_STORAGE_BUCKETED,
        )

        for plan in plans:
            storage.write(plan)

        rewritten: list[str] = []
        for path in record_paths:
            record = read_json(path, required=False)
            if not record:
                continue
            value, changed = _rewrite(record, mapping)
            if not changed:
                continue
            atomic_write_json(path, _rehash(value))
            rewritten.append(path.relative_to(field.control).as_posix())

        project_path = field._project_path(clean_project)
        migrated_project = read_json(project_path)
        observed_project_revision = int(migrated_project.get("revision") or 0)
        source_project_revision = int(
            migration_state.get("source_project_revision") or 0
        )
        target_project_revision = int(
            migration_state.get("target_project_revision") or 0
        )
        if observed_project_revision == source_project_revision:
            migrated_project["revision"] = target_project_revision
            migrated_project["updated_at"] = str(
                migration_state.get("started_at") or utc_now()
            )
            atomic_write_json(project_path, migrated_project)
            relative_project_path = project_path.relative_to(field.control).as_posix()
            if relative_project_path not in rewritten:
                rewritten.append(relative_project_path)
        elif observed_project_revision != target_project_revision:
            raise DomainError(
                "field_plan_migration_project_changed",
                "The project changed after plan migration began.",
                status=409,
                details={
                    "source_revision": source_project_revision,
                    "target_revision": target_project_revision,
                    "observed_revision": observed_project_revision,
                },
            )

        receipt_relative = f"migrations/{operation_id}.json"
        receipt_path = plans_root / receipt_relative
        receipt = {
            "schema": PLAN_MIGRATION_RECEIPT_SCHEMA,
            "migration_id": operation_id,
            "project_ref": project_ref,
            "mapping_hash": mapping_hash,
            "plan_refs": [str(plan.get("plan_ref") or "") for plan in plans],
            "plan_count": len(plans),
            "item_count": item_count,
            "source_project_revision": source_project_revision,
            "target_project_revision": target_project_revision,
            "source_storage_state": source_storage_state,
            "reference_mapping": dict(sorted(mapping.items())),
            "unresolved_reference_count": 0,
            "unresolved_references": [],
            "reference_restorations": restorations,
            "ambiguous_reference_policy": (
                "active plan, then current plan, then newest historical occurrence"
            ),
            "rewritten_files": sorted(rewritten),
            "completed_at": utc_now(),
        }
        atomic_write_json(receipt_path, receipt)
        activated = storage.activate(
            migration_id=operation_id,
            mapping_hash=mapping_hash,
            plan_count=len(plans),
            item_count=item_count,
            receipt=receipt_relative,
        )

        removed: list[str] = []
        if source_storage_state == "legacy":
            for path in legacy_files:
                os.unlink(path)
                removed.append(path.name)

        prepared_path = _preparation_path(plans_root, operation_id)
        if prepared_path.exists():
            os.unlink(prepared_path)

        if publish_nodes:
            migrated_active_plan = field.current_plan(clean_project)
            field._publish_plan_nodes_unlocked(
                clean_project,
                expected_revisions={
                    str(item.get("item_ref") or ""): 0
                    for item in migrated_active_plan.get("items") or []
                },
            )
        return PlanMigrationResult(
            migration_id=operation_id,
            state=str(activated.get("state") or ""),
            mapping_hash=mapping_hash,
            plan_count=len(plans),
            item_count=item_count,
            rewritten_files=tuple(sorted(rewritten)),
            removed_legacy_files=tuple(sorted(removed)),
            receipt_path=receipt_path,
        )


__all__ = [
    "PLAN_MIGRATION_RECEIPT_SCHEMA",
    "PLAN_MIGRATION_PREPARATION_SCHEMA",
    "PlanMigrationResult",
    "migrate_project_plan_storage",
]

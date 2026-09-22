from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..contract.errors import DomainError
from ..contract.plan_nodes import parse_plan_node_ref
from .io import atomic_write_json, component, content_hash, read_json, utc_now


PLAN_MANIFEST_SCHEMA = "problem-board.plan-manifest.v2"
PLAN_BUCKET_SCHEMA = "problem-board.plan-bucket.v1"
PLAN_MIGRATION_RECEIPT_SCHEMA = "problem-board.plan-storage-migration.v1"
PLAN_STORAGE_STATE_SCHEMA = "problem-board.plan-storage-state.v1"
PLAN_STORAGE_MIGRATING = "migrating"
PLAN_STORAGE_BUCKETED = "bucketed"


@dataclass(frozen=True)
class PlanWriteResult:
    manifest_path: Path
    written_buckets: tuple[str, ...]
    unchanged_buckets: tuple[str, ...]
    removed_buckets: tuple[str, ...]


class BucketedPlanStore:
    """A small plan manifest plus month buckets selected by the node URI."""

    def __init__(self, plans_root: str | Path) -> None:
        self.plans_root = Path(plans_root)

    def plan_root(self, plan_id: str) -> Path:
        return self.plans_root / component(plan_id, field="plan_id")

    def manifest_path(self, plan_id: str) -> Path:
        return self.plan_root(plan_id) / "manifest.json"

    @property
    def state_path(self) -> Path:
        return self.plans_root / ".storage.json"

    def bucket_path(self, plan_id: str, bucket: str) -> Path:
        return self.plan_root(plan_id) / "buckets" / f"{component(bucket, field='bucket')}.json"

    def exists(self, plan_id: str) -> bool:
        return self.manifest_path(plan_id).is_file()

    def state(self) -> dict[str, Any]:
        value = read_json(self.state_path, required=False)
        if not value:
            return {"schema": PLAN_STORAGE_STATE_SCHEMA, "state": "legacy"}
        if value.get("schema") != PLAN_STORAGE_STATE_SCHEMA or value.get("state") not in {
            PLAN_STORAGE_MIGRATING,
            PLAN_STORAGE_BUCKETED,
        }:
            raise DomainError(
                "field_plan_storage_state_invalid",
                "The plan storage state is unsupported.",
            )
        return value

    def is_bucketed(self) -> bool:
        return self.state().get("state") == PLAN_STORAGE_BUCKETED

    def begin_migration(
        self,
        *,
        migration_id: str,
        mapping_hash: str,
        plan_count: int,
        source_project_revision: int = 0,
        target_project_revision: int = 0,
        rewrite_bucketed: bool = False,
    ) -> dict[str, Any]:
        current = self.state()
        clean_id = component(migration_id, field="migration_id")
        clean_hash = str(mapping_hash or "").strip()
        if not clean_hash:
            raise DomainError(
                "field_plan_migration_invalid",
                "The plan migration requires its reference-mapping hash.",
            )
        if current.get("state") == PLAN_STORAGE_BUCKETED:
            if not rewrite_bucketed:
                return current
            record = {
                "schema": PLAN_STORAGE_STATE_SCHEMA,
                "state": PLAN_STORAGE_MIGRATING,
                "migration_id": clean_id,
                "mapping_hash": clean_hash,
                "plan_count": int(plan_count),
                "source_project_revision": int(source_project_revision),
                "target_project_revision": int(target_project_revision),
                "source_storage_state": PLAN_STORAGE_BUCKETED,
                "started_at": utc_now(),
            }
            atomic_write_json(self.state_path, record)
            return record
        if current.get("state") == PLAN_STORAGE_MIGRATING:
            if (
                current.get("migration_id") != clean_id
                or current.get("mapping_hash") != clean_hash
            ):
                raise DomainError(
                    "field_plan_migration_conflict",
                    "Another plan-storage migration is already in progress.",
                    status=409,
                )
            return current
        record = {
            "schema": PLAN_STORAGE_STATE_SCHEMA,
            "state": PLAN_STORAGE_MIGRATING,
            "migration_id": clean_id,
            "mapping_hash": clean_hash,
            "plan_count": int(plan_count),
            "source_project_revision": int(source_project_revision),
            "target_project_revision": int(target_project_revision),
            "started_at": utc_now(),
        }
        atomic_write_json(self.state_path, record)
        return record

    def activate(
        self,
        *,
        migration_id: str,
        mapping_hash: str,
        plan_count: int,
        item_count: int = 0,
        receipt: str = "",
    ) -> dict[str, Any]:
        current = self.state()
        clean_id = component(migration_id, field="migration_id")
        clean_hash = str(mapping_hash or "").strip()
        if current.get("state") == PLAN_STORAGE_BUCKETED:
            if current.get("mapping_hash") == clean_hash:
                return current
            raise DomainError(
                "field_plan_migration_conflict",
                "Bucketed plan storage was activated by another migration.",
                status=409,
            )
        if (
            current.get("state") != PLAN_STORAGE_MIGRATING
            or current.get("migration_id") != clean_id
            or current.get("mapping_hash") != clean_hash
        ):
            raise DomainError(
                "field_plan_migration_state_invalid",
                "The plan migration must be prepared before it is activated.",
                status=409,
            )
        record = {
            "schema": PLAN_STORAGE_STATE_SCHEMA,
            "state": PLAN_STORAGE_BUCKETED,
            "migration_id": clean_id,
            "mapping_hash": clean_hash,
            "plan_count": int(plan_count),
            "item_count": int(item_count),
            "receipt": str(receipt or ""),
            "source_project_revision": int(
                current.get("source_project_revision") or 0
            ),
            "target_project_revision": int(
                current.get("target_project_revision") or 0
            ),
            "started_at": str(current.get("started_at") or ""),
            "activated_at": utc_now(),
        }
        atomic_write_json(self.state_path, record)
        return record

    def initialize_bucketed(self) -> dict[str, Any]:
        current = self.state()
        if current.get("state") == PLAN_STORAGE_BUCKETED:
            return current
        if current.get("state") == PLAN_STORAGE_MIGRATING:
            raise DomainError(
                "field_plan_migration_in_progress",
                "Plan storage is being migrated. Retry after it completes.",
                status=409,
                details={"retryable": True},
            )
        mapping_hash = content_hash({"plans": [], "references": {}})
        migration_id = "created-bucketed"
        self.begin_migration(
            migration_id=migration_id,
            mapping_hash=mapping_hash,
            plan_count=0,
            source_project_revision=0,
            target_project_revision=0,
        )
        return self.activate(
            migration_id=migration_id,
            mapping_hash=mapping_hash,
            plan_count=0,
            item_count=0,
        )

    def completed_reference_mapping(self) -> dict[str, str]:
        """Collapse every completed migration's URI changes to current refs."""
        migrations_roots = (
            self.plans_root.parent / "receipts" / "plan-migrations",
            self.plans_root / "migrations",
        )
        receipt_paths: dict[str, Path] = {}
        receipt_values: dict[str, dict[str, Any]] = {}
        for migrations_root in migrations_roots:
            if not migrations_root.is_dir():
                continue
            for path in sorted(migrations_root.glob("*.json")):
                if path.name.endswith(".prepared.json"):
                    continue
                value = read_json(path, required=False)
                previous = receipt_values.get(path.name)
                if previous is not None and previous != value:
                    raise DomainError(
                        "field_plan_migration_history_ambiguous",
                        "Archived and recovery plan migration receipts disagree.",
                        status=409,
                        details={"path": path.name},
                    )
                receipt_paths[path.name] = path
                receipt_values[path.name] = value
        if not receipt_paths:
            return {}

        receipts: list[tuple[int, int, str, str, Path, dict[str, str]]] = []
        seen_target_revisions: dict[int, str] = {}
        for name in sorted(receipt_paths):
            path = receipt_paths[name]
            receipt = receipt_values[name]
            if not receipt:
                continue
            if (
                receipt.get("schema") != PLAN_MIGRATION_RECEIPT_SCHEMA
                or not str(receipt.get("completed_at") or "")
                or not isinstance(receipt.get("reference_mapping"), Mapping)
            ):
                raise DomainError(
                    "field_plan_migration_receipt_invalid",
                    "A completed plan migration receipt is invalid.",
                    status=409,
                    details={"path": path.name},
                )
            source_revision = int(receipt.get("source_project_revision") or 0)
            target_revision = int(receipt.get("target_project_revision") or 0)
            migration_id = str(receipt.get("migration_id") or path.stem)
            if target_revision <= source_revision:
                raise DomainError(
                    "field_plan_migration_receipt_invalid",
                    "A completed plan migration receipt has an invalid revision range.",
                    status=409,
                    details={
                        "path": path.name,
                        "source_project_revision": source_revision,
                        "target_project_revision": target_revision,
                    },
                )
            previous = seen_target_revisions.get(target_revision)
            if previous is not None and previous != migration_id:
                raise DomainError(
                    "field_plan_migration_history_ambiguous",
                    "Two plan migrations claim the same target project revision.",
                    status=409,
                    details={
                        "target_project_revision": target_revision,
                        "migration_ids": sorted((previous, migration_id)),
                    },
                )
            seen_target_revisions[target_revision] = migration_id
            step: dict[str, str] = {}
            for raw_source, raw_target in receipt["reference_mapping"].items():
                source = str(raw_source or "").strip()
                target = str(raw_target or "").strip()
                if not source or not target:
                    raise DomainError(
                        "field_plan_migration_receipt_invalid",
                        "A completed plan migration receipt contains an empty reference.",
                        status=409,
                        details={"path": path.name},
                    )
                step[source] = target
            receipts.append(
                (
                    target_revision,
                    source_revision,
                    str(receipt["completed_at"]),
                    migration_id,
                    path,
                    step,
                )
            )

        history: dict[str, str] = {}
        for _, _, _, migration_id, path, step in sorted(receipts):
            resolved_step = self._collapse_reference_mapping(
                step,
                migration_id=migration_id,
                path=path,
                retain_identities=True,
            )
            history = {
                source: resolved_step.get(target, target)
                for source, target in history.items()
            }
            for source, target in resolved_step.items():
                if source == target:
                    history.pop(source, None)
                else:
                    history[source] = target

        return self._collapse_reference_mapping(history)

    @staticmethod
    def _collapse_reference_mapping(
        mapping: Mapping[str, str],
        *,
        migration_id: str = "",
        path: Path | None = None,
        retain_identities: bool = False,
    ) -> dict[str, str]:
        direct = {str(source): str(target) for source, target in mapping.items()}
        collapsed: dict[str, str] = {}
        for source in sorted(direct):
            current = source
            visited: set[str] = set()
            while current in direct and direct[current] != current:
                if current in visited:
                    raise DomainError(
                        "field_plan_migration_reference_cycle",
                        "Plan migration receipts contain a reference cycle.",
                        status=409,
                        details={
                            "migration_id": migration_id,
                            "path": path.name if path is not None else "",
                            "reference": source,
                        },
                    )
                visited.add(current)
                current = direct[current]
            if retain_identities or current != source:
                collapsed[source] = current
        return collapsed

    def write(self, plan: Mapping[str, Any]) -> PlanWriteResult:
        plan_id = component(plan.get("plan_id"), field="plan_id")
        items = []
        for ordinal, raw in enumerate(plan.get("items") or []):
            if not isinstance(raw, Mapping):
                continue
            row = dict(raw)
            row["ordinal"] = int(row.get("ordinal", ordinal))
            items.append(row)
        edges = [dict(row) for row in plan.get("dependencies") or [] if isinstance(row, Mapping)]
        item_refs = {str(row.get("item_ref") or "") for row in items}
        if "" in item_refs or len(item_refs) != len(items):
            raise DomainError(
                "field_plan_invalid",
                "Every plan node requires one unique canonical URI.",
            )
        addresses = {item_ref: parse_plan_node_ref(item_ref) for item_ref in item_refs}
        for edge in edges:
            source = str(edge.get("item_ref") or "")
            target = str(edge.get("depends_on_ref") or "")
            if source not in addresses or target not in addresses or source == target:
                raise DomainError(
                    "field_dependency_invalid",
                    "Every dependency must join two different canonical node URIs in this plan.",
                    details={"item_ref": source, "depends_on_ref": target},
                )

        bucket_items: dict[str, list[dict[str, Any]]] = {}
        for row in items:
            bucket = addresses[str(row["item_ref"])].bucket
            bucket_items.setdefault(bucket, []).append(row)
        bucket_edges: dict[str, list[dict[str, Any]]] = {}
        for edge in edges:
            bucket = addresses[str(edge["item_ref"])].bucket
            bucket_edges.setdefault(bucket, []).append(edge)

        root = self.plan_root(plan_id)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        (root / "buckets").mkdir(parents=True, exist_ok=True, mode=0o700)
        previous = read_json(self.manifest_path(plan_id), required=False)
        previous_buckets = {
            str(row.get("bucket") or ""): dict(row)
            for row in previous.get("buckets") or []
            if isinstance(row, Mapping) and row.get("bucket")
        }
        written: list[str] = []
        unchanged: list[str] = []
        bucket_manifest: list[dict[str, Any]] = []
        for bucket in sorted(bucket_items):
            rows = sorted(
                bucket_items[bucket],
                key=lambda row: (
                    int(row.get("ordinal") or 0),
                    str(row.get("item_ref") or ""),
                ),
            )
            dependencies = sorted(
                bucket_edges.get(bucket, []),
                key=lambda row: (
                    str(row.get("item_ref") or ""),
                    str(row.get("depends_on_ref") or ""),
                    str(row.get("dependency_id") or ""),
                ),
            )
            payload = {
                "schema": PLAN_BUCKET_SCHEMA,
                "plan_ref": str(plan.get("plan_ref") or ""),
                "bucket": bucket,
                "items": rows,
                "dependencies": dependencies,
            }
            payload["content_hash"] = content_hash(payload)
            prior_hash = str(previous_buckets.get(bucket, {}).get("content_hash") or "")
            if prior_hash == payload["content_hash"] and self.bucket_path(plan_id, bucket).is_file():
                unchanged.append(bucket)
            else:
                atomic_write_json(self.bucket_path(plan_id, bucket), payload)
                written.append(bucket)
            bucket_manifest.append(
                {
                    "bucket": bucket,
                    "path": f"buckets/{bucket}.json",
                    "item_count": len(rows),
                    "dependency_count": len(dependencies),
                    "content_hash": payload["content_hash"],
                }
            )

        removed = sorted(set(previous_buckets) - set(bucket_items))
        for bucket in removed:
            path = self.bucket_path(plan_id, bucket)
            if path.exists():
                os.unlink(path)

        manifest = {
            "schema": PLAN_MANIFEST_SCHEMA,
            "plan_schema": str(plan.get("schema") or ""),
            "plan_id": plan_id,
            "plan_ref": str(plan.get("plan_ref") or ""),
            "project_ref": str(plan.get("project_ref") or ""),
            "status": str(plan.get("status") or "draft"),
            "revision": int(plan.get("revision") or 0),
            "authored_by": str(plan.get("authored_by") or ""),
            "rationale": str(plan.get("rationale") or ""),
            "item_count": len(items),
            "dependency_count": len(edges),
            "buckets": bucket_manifest,
            "created_at": str(plan.get("created_at") or ""),
            "updated_at": str(plan.get("updated_at") or ""),
        }
        manifest["content_hash"] = content_hash(manifest)
        atomic_write_json(self.manifest_path(plan_id), manifest)
        return PlanWriteResult(
            manifest_path=self.manifest_path(plan_id),
            written_buckets=tuple(written),
            unchanged_buckets=tuple(unchanged),
            removed_buckets=tuple(removed),
        )

    def read(self, plan_id: str) -> dict[str, Any]:
        manifest = read_json(self.manifest_path(plan_id))
        if manifest.get("schema") != PLAN_MANIFEST_SCHEMA:
            raise DomainError(
                "field_plan_schema_invalid",
                "The plan manifest schema is unsupported.",
            )
        items: list[dict[str, Any]] = []
        dependencies: list[dict[str, Any]] = []
        for descriptor in manifest.get("buckets") or []:
            if not isinstance(descriptor, Mapping):
                continue
            bucket = str(descriptor.get("bucket") or "")
            payload = read_json(self.bucket_path(plan_id, bucket))
            self._verify_bucket(payload, descriptor)
            items.extend(dict(row) for row in payload.get("items") or [])
            dependencies.extend(
                dict(row) for row in payload.get("dependencies") or []
            )
        items.sort(key=lambda row: (int(row.get("ordinal") or 0), str(row.get("item_ref") or "")))
        return {
            "schema": str(manifest.get("plan_schema") or ""),
            "plan_id": str(manifest.get("plan_id") or ""),
            "plan_ref": str(manifest.get("plan_ref") or ""),
            "project_ref": str(manifest.get("project_ref") or ""),
            "status": str(manifest.get("status") or "draft"),
            "revision": int(manifest.get("revision") or 0),
            "authored_by": str(manifest.get("authored_by") or ""),
            "rationale": str(manifest.get("rationale") or ""),
            "items": items,
            "dependencies": dependencies,
            "created_at": str(manifest.get("created_at") or ""),
            "updated_at": str(manifest.get("updated_at") or ""),
        }

    def read_node(self, plan_id: str, item_ref: str) -> dict[str, Any]:
        address = parse_plan_node_ref(item_ref)
        payload = read_json(self.bucket_path(plan_id, address.bucket))
        item = next(
            (
                dict(row)
                for row in payload.get("items") or []
                if str(row.get("item_ref") or "") == address.ref
            ),
            None,
        )
        if item is None:
            raise DomainError(
                "field_item_not_found",
                "The work item does not exist.",
                status=404,
                details={"item_ref": address.ref, "bucket": address.bucket},
            )
        return {
            "item": item,
            "dependencies": [
                dict(row)
                for row in payload.get("dependencies") or []
                if str(row.get("item_ref") or "") == address.ref
            ],
            "bucket": address.bucket,
        }

    def resolve_dependency_target(self, plan_id: str, item_ref: str) -> dict[str, Any]:
        return self.read_node(plan_id, item_ref)["item"]

    @staticmethod
    def _verify_bucket(
        payload: Mapping[str, Any], descriptor: Mapping[str, Any]
    ) -> None:
        if payload.get("schema") != PLAN_BUCKET_SCHEMA:
            raise DomainError(
                "field_plan_schema_invalid",
                "The plan bucket schema is unsupported.",
            )
        without_hash = dict(payload)
        declared = str(without_hash.pop("content_hash", ""))
        actual = content_hash(without_hash)
        expected = str(descriptor.get("content_hash") or "")
        if not declared or declared != actual or declared != expected:
            raise DomainError(
                "field_plan_bucket_hash_invalid",
                "The plan bucket content does not match its manifest.",
                status=409,
                details={"bucket": str(payload.get("bucket") or "")},
            )


__all__ = [
    "BucketedPlanStore",
    "PLAN_BUCKET_SCHEMA",
    "PLAN_MANIFEST_SCHEMA",
    "PLAN_MIGRATION_RECEIPT_SCHEMA",
    "PLAN_STORAGE_BUCKETED",
    "PLAN_STORAGE_MIGRATING",
    "PLAN_STORAGE_STATE_SCHEMA",
    "PlanWriteResult",
]

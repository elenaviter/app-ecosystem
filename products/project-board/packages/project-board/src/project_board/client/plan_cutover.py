from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
import shutil
from typing import Any, Mapping, Sequence

from ..contract.errors import DomainError
from ..contract.plan_assignment_outcomes import (
    MISSING_PLAN_REFUSAL_CODE,
    normalize_plan_assignment_outcomes,
)
from ..contract.plan_index import PLAN_INDEX_SCHEMA, indexed_plan_item
from ..contract.plan_nodes import (
    normalize_plan_reference_mapping,
    parse_plan_node_ref,
)
from ..contract.refs import normalize_reference_mapping, parse_ref
from .io import (
    atomic_write_json,
    component,
    content_hash,
    exclusive_lock,
    parse_utc,
    read_json,
    utc_now,
)
from .outbox_store import OutboxStore
from .plan_storage import BucketedPlanStore, PLAN_STORAGE_BUCKETED


PLAN_CUTOVER_RECEIPT_SCHEMA = "problem-board.local-plan-cutover.v1"
PLAN_CUTOVER_RESULT_SCHEMA = "problem-board.plan-cutover.v1"
PLAN_CUTOVER_PREVIEW_SCHEMA = "problem-board.local-plan-cutover-preview.v1"
PLAN_CUTOVER_READINESS_SCHEMA = "problem-board.local-plan-cutover-readiness.v1"
PLAN_MIGRATION_ARCHIVE = Path("receipts") / "plan-migrations"
PLAN_CUTOVER_RECEIPTS = Path("receipts") / "plan-cutovers"
ACTIVE_ASSIGNMENT_STATES = frozenset({"routing", "assigned", "working", "blocked"})


def _plan_reference_mapping(
    mapping: Mapping[str, str],
    *,
    target_refs: set[str] | None = None,
) -> dict[str, str]:
    """Select and validate the plan-node part of a complete URI mapping."""

    selected = {
        str(source): str(target)
        for source, target in mapping.items()
        if parse_ref(target).selector == "plan:node"
        and (target_refs is None or str(target) in target_refs)
    }
    return normalize_plan_reference_mapping(selected, target_refs=target_refs)


@dataclass(frozen=True)
class PreparedLocalPlan:
    project_id: str
    project_ref: str
    plan_ref: str
    plan_revision: int
    package: dict[str, Any]
    reference_mapping: dict[str, str]
    assignment_outcomes: list[dict[str, Any]]
    active_assignment_count: int
    active_assignment_bindings: list[dict[str, Any]]

    @property
    def package_content_hash(self) -> str:
        return str(self.package["package_content_hash"])

    @property
    def item_count(self) -> int:
        return int(self.package["item_count"])

    @property
    def note_count(self) -> int:
        return len(self.package.get("notes") or [])

    @property
    def reference_mapping_hash(self) -> str:
        return content_hash(self.reference_mapping)

    @property
    def assignment_outcomes_hash(self) -> str:
        return content_hash(self.assignment_outcomes)

    @property
    def active_assignment_bindings_hash(self) -> str:
        return content_hash(self.active_assignment_bindings)

    @property
    def cutover_content_hash(self) -> str:
        return content_hash(
            {
                "package_content_hash": self.package_content_hash,
                "reference_mapping_hash": self.reference_mapping_hash,
                "assignment_outcomes_hash": self.assignment_outcomes_hash,
                "active_assignment_bindings_hash": (
                    self.active_assignment_bindings_hash
                ),
            }
        )


def local_plan_cutover_preview(
    prepared: PreparedLocalPlan, *, readiness: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "schema": PLAN_CUTOVER_PREVIEW_SCHEMA,
        "state": "ready_for_operator_review",
        "project_ref": prepared.project_ref,
        "source_plan_ref": prepared.plan_ref,
        "source_plan_revision": prepared.plan_revision,
        "package": prepared.package,
        "package_content_hash": prepared.package_content_hash,
        "cutover_content_hash": prepared.cutover_content_hash,
        "item_count": prepared.item_count,
        "note_count": prepared.note_count,
        "active_assignment_count": prepared.active_assignment_count,
        "active_assignment_bindings_hash": (
            prepared.active_assignment_bindings_hash
        ),
        "active_assignment_bindings": [
            dict(row) for row in prepared.active_assignment_bindings
        ],
        "unresolved_assignment_count": 0,
        "reference_mapping": dict(prepared.reference_mapping),
        "reference_mapping_count": len(prepared.reference_mapping),
        "reference_mapping_hash": prepared.reference_mapping_hash,
        "reference_rewrites": [
            {"from_ref": source_ref, "to_ref": target_ref}
            for source_ref, target_ref in prepared.reference_mapping.items()
        ],
        "assignment_outcome_count": len(prepared.assignment_outcomes),
        "assignment_outcomes_hash": prepared.assignment_outcomes_hash,
        "assignment_outcomes": [dict(row) for row in prepared.assignment_outcomes],
        "cutover_readiness": dict(readiness),
        "commit_guard": {
            "argument": "--expected-cutover-hash",
            "value": prepared.cutover_content_hash,
        },
    }


def prepared_local_plan_from_preview(
    value: Mapping[str, Any], *, project_id: str
) -> PreparedLocalPlan:
    """Validate and recover the exact import material shown in one preview."""

    def invalid(message: str, *, fields: list[str] | None = None) -> DomainError:
        return DomainError(
            "field_plan_cutover_preview_artifact_invalid",
            message,
            status=409,
            details={"fields": fields or []},
        )

    preview = dict(value)
    if (
        preview.get("schema") != PLAN_CUTOVER_PREVIEW_SCHEMA
        or preview.get("state") != "ready_for_operator_review"
    ):
        raise invalid("The reviewed file is not a ready plan-cutover preview.")
    readiness = preview.get("cutover_readiness")
    expected_readiness = {
        "schema": PLAN_CUTOVER_READINESS_SCHEMA,
        "source_storage_state": PLAN_STORAGE_BUCKETED,
        "prepared_migration_count": 0,
        "outbox": {
            "plan.nodes.publish": {"pending_count": 0, "leased_count": 0},
            "assignment.report": {"pending_count": 0, "leased_count": 0},
        },
    }
    if readiness != expected_readiness:
        raise invalid("The reviewed preview does not prove a ready local cutover.")
    raw_package = preview.get("package")
    if not isinstance(raw_package, Mapping):
        raise invalid("The reviewed preview does not contain its import package.")
    package = dict(raw_package)
    supplied_package_hash = str(package.get("package_content_hash") or "")
    unhashed_package = dict(package)
    unhashed_package.pop("package_content_hash", None)
    if (
        not supplied_package_hash
        or content_hash(unhashed_package) != supplied_package_hash
    ):
        raise invalid("The reviewed preview's import package failed its integrity check.")

    raw_items = package.get("items")
    if not isinstance(raw_items, list):
        raise invalid("The reviewed preview's import package has no item list.")
    target_refs = {
        parse_plan_node_ref(row.get("item_ref")).ref
        for row in raw_items
        if isinstance(row, Mapping)
    }
    if len(target_refs) != len(raw_items):
        raise invalid("The reviewed preview contains an invalid or duplicate plan item.")
    raw_mapping = preview.get("reference_mapping")
    if not isinstance(raw_mapping, Mapping):
        raise invalid("The reviewed preview does not contain its reference mapping.")
    reference_mapping = normalize_reference_mapping(raw_mapping)
    _plan_reference_mapping(reference_mapping, target_refs=target_refs)
    raw_outcomes = preview.get("assignment_outcomes")
    if not isinstance(raw_outcomes, list):
        raise invalid("The reviewed preview has no assignment-outcome list.")
    assignment_outcomes = normalize_plan_assignment_outcomes(
        raw_outcomes,
        target_refs=target_refs,
    )
    raw_bindings = preview.get("active_assignment_bindings")
    if not isinstance(raw_bindings, list) or any(
        not isinstance(row, Mapping) for row in raw_bindings
    ):
        raise invalid("The reviewed preview has an invalid active-assignment list.")
    active_assignment_bindings = [dict(row) for row in raw_bindings]
    try:
        active_assignment_count = int(preview.get("active_assignment_count"))
        source_plan_revision = int(preview.get("source_plan_revision"))
    except (TypeError, ValueError) as exc:
        raise invalid("The reviewed preview has invalid revision or count metadata.") from exc

    prepared = PreparedLocalPlan(
        project_id=component(project_id, field="project_id"),
        project_ref=str(preview.get("project_ref") or ""),
        plan_ref=str(preview.get("source_plan_ref") or ""),
        plan_revision=source_plan_revision,
        package=package,
        reference_mapping=reference_mapping,
        assignment_outcomes=assignment_outcomes,
        active_assignment_count=active_assignment_count,
        active_assignment_bindings=active_assignment_bindings,
    )
    expected = local_plan_cutover_preview(prepared, readiness=expected_readiness)
    mismatched = sorted(
        key
        for key in set(preview) | set(expected)
        if preview.get(key) != expected.get(key)
    )
    if mismatched:
        raise invalid(
            "The reviewed preview's evidence and import material do not agree.",
            fields=mismatched,
        )
    return prepared


def _timestamp(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return parse_utc(text).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _dependencies(plan: Mapping[str, Any]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for edge in plan.get("dependencies") or []:
        if not isinstance(edge, Mapping):
            continue
        item_ref = parse_plan_node_ref(edge.get("item_ref")).ref
        target_ref = parse_plan_node_ref(edge.get("depends_on_ref")).ref
        result.setdefault(item_ref, []).append(target_ref)
    return result


def _package_item(
    raw: Mapping[str, Any],
    *,
    position: int,
    depends_on: Sequence[str],
) -> dict[str, Any]:
    item = dict(raw)
    ordinal = int(item.get("ordinal") if item.get("ordinal") is not None else position)
    if ordinal != position:
        raise DomainError(
            "field_plan_cutover_order_invalid",
            "The local recovery plan must carry each authored ordinal exactly once.",
            status=409,
            details={"position": position, "ordinal": ordinal},
        )
    for field in ("item_id", "item_ref", "item_key", "title", "created_at", "updated_at"):
        if not str(item.get(field) or "").strip():
            raise DomainError(
                "field_plan_cutover_item_incomplete",
                "A local recovery item is missing state required for exact verification.",
                status=409,
                details={"position": position, "field": field},
            )
    item.update(
        revision=max(1, int(item.get("revision") or 1)),
        status=str(item.get("status") or "ready"),
        created_at=_timestamp(item.get("created_at")),
        started_at=_timestamp(item.get("started_at")),
        cancelled_at=_timestamp(item.get("cancelled_at")),
        cancelled_by=str(item.get("cancelled_by") or ""),
        updated_at=_timestamp(item.get("updated_at")),
        result=item.get("result") if item.get("result") is not None else "",
        result_ref=str(item.get("result_ref") or ""),
        blocked_reason=str(item.get("blocked_reason") or ""),
        cancel_reason=str(item.get("cancel_reason") or ""),
        review=item.get("review") if item.get("review") is not None else {},
    )
    indexed = indexed_plan_item(item, ordinal=ordinal, depends_on=depends_on)
    item.pop("notes", None)
    return {
        **item,
        **indexed,
        "description": str(item.get("description") or ""),
        "acceptance": [str(value) for value in item.get("acceptance") or []],
        "attachment_refs": [
            str(value) for value in item.get("attachment_refs") or []
        ],
        "attachment_count": len(item.get("attachment_refs") or []),
    }


def _package_notes(
    raw_item: Mapping[str, Any], *, item_ref: str
) -> list[dict[str, Any]]:
    notes: list[dict[str, Any]] = []
    for position, raw in enumerate(raw_item.get("notes") or []):
        if not isinstance(raw, Mapping):
            raise DomainError(
                "field_plan_cutover_note_invalid",
                "A local recovery note must be an object.",
                status=409,
                details={"work_ref": item_ref, "position": position},
            )
        note = dict(raw)
        ordinal = int(
            note.get("ordinal") if note.get("ordinal") is not None else position
        )
        if ordinal != position:
            raise DomainError(
                "field_plan_cutover_note_order_invalid",
                "The local recovery notes must carry each item ordinal exactly once.",
                status=409,
                details={"work_ref": item_ref, "position": position, "ordinal": ordinal},
            )
        for field in ("note_id", "note_ref", "author", "author_kind", "created_at"):
            if not str(note.get(field) or "").strip():
                raise DomainError(
                    "field_plan_cutover_note_incomplete",
                    "A local recovery note is missing state required for exact verification.",
                    status=409,
                    details={"work_ref": item_ref, "position": position, "field": field},
                )
        notes.append(
            {
                "item_ref": item_ref,
                "note_id": str(note["note_id"]),
                "note_ref": str(note["note_ref"]),
                "ordinal": ordinal,
                "author": str(note["author"]),
                "author_kind": str(note["author_kind"]),
                "text": str(note.get("text") or ""),
                "created_at": _timestamp(note["created_at"]),
            }
        )
    return notes


def _item_identity_rewrites(
    item_ref: str,
    reference_mapping: Mapping[str, str],
) -> list[dict[str, str]]:
    """Expose canonical URI remints even when assignments moved with the item."""

    target = parse_plan_node_ref(item_ref)
    if target.version_timestamp is not None:
        return []
    rewrites: list[dict[str, str]] = []
    for raw_source, raw_target in reference_mapping.items():
        if str(raw_target or "") != target.ref:
            continue
        try:
            source = parse_plan_node_ref(raw_source)
        except DomainError:
            continue
        if (
            source.version_timestamp is None
            and source.prefix == target.prefix
            and source.ref != target.ref
        ):
            rewrites.append({"from_ref": source.ref, "to_ref": target.ref})
    return sorted(rewrites, key=lambda row: row["from_ref"])


def _reconcile_active_assignments(
    field: Any,
    *,
    project_id: str,
    items: list[dict[str, Any]],
    reference_mapping: Mapping[str, str],
    recovered_outcomes: Sequence[Mapping[str, Any]],
) -> tuple[int, list[dict[str, Any]]]:
    by_ref = {
        parse_plan_node_ref(item.get("item_ref")).ref: item
        for item in items
    }
    recovered_ownerships = {
        (
            str(outcome.get("assignment_ref") or ""),
            int(outcome.get("ownership_version") or 0),
        )
        for outcome in recovered_outcomes
    }
    grouped: dict[str, list[dict[str, Any]]] = {}
    unresolved_assignments: list[dict[str, Any]] = []
    for raw in field.list_assignments(project_id):
        assignment = dict(raw)
        if (
            str(assignment.get("assignment_ref") or ""),
            int(assignment.get("ownership_version") or 0),
        ) in recovered_ownerships:
            continue
        state = str(assignment.get("state") or "")
        if state not in ACTIVE_ASSIGNMENT_STATES:
            continue
        source_ref = str(assignment.get("work_ref") or "")
        target_ref = str(reference_mapping.get(source_ref) or source_ref)
        if target_ref not in by_ref:
            unresolved_assignments.append(
                {
                    "assignment_ref": str(assignment.get("assignment_ref") or ""),
                    "ownership_version": int(
                        assignment.get("ownership_version") or 0
                    ),
                    "worker_name": str(assignment.get("worker_name") or ""),
                    "source_state": state,
                    "from_ref": source_ref,
                    "to_ref": target_ref,
                    "reason": "target_not_in_recovery_plan",
                }
            )
            continue
        assignment["resolved_work_ref"] = target_ref
        grouped.setdefault(target_ref, []).append(assignment)

    if unresolved_assignments:
        unresolved_assignments.sort(key=lambda row: row["assignment_ref"])
        raise DomainError(
            "field_plan_cutover_assignment_missing",
            "Active assignments do not resolve to items in the recovery plan.",
            status=409,
            details={
                "unresolved_assignment_count": len(unresolved_assignments),
                "unresolved_assignments": unresolved_assignments,
            },
        )

    active_assignment_bindings: list[dict[str, Any]] = []
    for item_ref in sorted(grouped):
        assignments = grouped[item_ref]
        maximum_version = max(
            int(row.get("ownership_version") or 0) for row in assignments
        )
        current = [
            row
            for row in assignments
            if int(row.get("ownership_version") or 0) == maximum_version
        ]
        signatures = {
            (
                str(row.get("worker_name") or ""),
                "blocked" if str(row.get("state") or "") == "blocked" else "working",
            )
            for row in current
        }
        if len(signatures) != 1 or not next(iter(signatures))[0]:
            raise DomainError(
                "field_plan_cutover_assignment_ambiguous",
                "Equal-version assignments disagree about the current owner or state.",
                status=409,
                details={
                    "work_ref": item_ref,
                    "ownership_version": maximum_version,
                    "assignment_refs": sorted(
                        str(row.get("assignment_ref") or "") for row in current
                    ),
                },
            )
        selected = max(
            current,
            key=lambda row: (
                str(row.get("received_at") or ""),
                str(row.get("assignment_ref") or ""),
            ),
        )
        worker_name, status = next(iter(signatures))
        item = by_ref[item_ref]
        changed = (
            str(item.get("assignee") or "") != worker_name
            or str(item.get("status") or "") != status
        )
        item["assignee"] = worker_name
        item["status"] = status
        if changed:
            item["revision"] = max(1, int(item.get("revision") or 1)) + 1
            item["updated_at"] = max(
                filter(
                    None,
                    (
                        _timestamp(item.get("updated_at")),
                        _timestamp(selected.get("received_at")),
                    ),
                ),
                default=_timestamp(item.get("updated_at")),
            )
        source_ref = str(selected.get("work_ref") or "")
        assignment_reference_change = source_ref != item_ref
        item_identity_rewrites = _item_identity_rewrites(
            item_ref,
            reference_mapping,
        )
        active_assignment_bindings.append(
            {
                "assignment_ref": str(selected.get("assignment_ref") or ""),
                "ownership_version": maximum_version,
                "worker_name": worker_name,
                "source_state": str(selected.get("state") or ""),
                "from_ref": source_ref,
                "to_ref": item_ref,
                "assignment_reference_change": assignment_reference_change,
                "item_identity_rewrites": item_identity_rewrites,
                "reference_change": (
                    assignment_reference_change or bool(item_identity_rewrites)
                ),
                "imported_item_status": status,
            }
        )
    return len(grouped), active_assignment_bindings


def _assignment_outcomes(
    field: Any,
    *,
    project_id: str,
    project_ref: str,
    item_refs: set[str],
    reference_mapping: Mapping[str, str],
) -> list[dict[str, Any]]:
    assignments = {
        str(row.get("assignment_ref") or ""): dict(row)
        for row in field.list_assignments(project_id)
    }
    outbox = OutboxStore(field.control)
    candidates: list[dict[str, Any]] = []
    with exclusive_lock(outbox.lock):
        for path in outbox.settled_paths(project_ref=project_ref, op="plan_cutover"):
            row = read_json(path)
            if (
                row.get("kind") != "assignment.report"
                or row.get("project_ref") != project_ref
                or row.get("state") != "refused"
            ):
                continue
            refusal_code = str(row.get("remote_disposition") or "").split(
                maxsplit=1
            )[0]
            if refusal_code != MISSING_PLAN_REFUSAL_CODE:
                raise DomainError(
                    "field_plan_cutover_assignment_report_unresolved",
                    "A refused assignment report needs operator resolution before plan cutover.",
                    status=409,
                    details={
                        "outbox_id": str(row.get("outbox_id") or path.stem),
                        "remote_disposition": str(
                            row.get("remote_disposition") or ""
                        ),
                    },
                )
            payload = row.get("payload")
            if not isinstance(payload, Mapping) or str(
                row.get("content_hash") or ""
            ) != content_hash(payload):
                raise DomainError(
                    "field_plan_cutover_assignment_report_invalid",
                    "A refused assignment report does not retain its exact request evidence.",
                    status=409,
                    details={"outbox_id": str(row.get("outbox_id") or path.stem)},
                )
            assignment_ref = str(payload.get("assignment_ref") or "")
            assignment = assignments.get(assignment_ref)
            if assignment is None:
                raise DomainError(
                    "field_plan_cutover_assignment_report_missing",
                    "A refused assignment report has no matching local assignment.",
                    status=409,
                    details={"assignment_ref": assignment_ref},
                )
            expected_ownership = {
                "worker_name": str(row.get("worker_name") or "").lower(),
                "ownership_version": int(payload.get("ownership_version") or 0),
            }
            observed_ownership = {
                "worker_name": str(assignment.get("worker_name") or "").lower(),
                "ownership_version": int(assignment.get("ownership_version") or 0),
            }
            if observed_ownership != expected_ownership:
                raise DomainError(
                    "field_plan_cutover_assignment_report_conflict",
                    "The refused assignment report no longer matches the local assignment ownership.",
                    status=409,
                    details={
                        "assignment_ref": assignment_ref,
                        "expected": expected_ownership,
                        "observed": observed_ownership,
                    },
                )
            reported_at = str(row.get("created_at") or "")
            if not reported_at:
                raise DomainError(
                    "field_plan_cutover_assignment_report_invalid",
                    "A refused assignment report does not record when it was created.",
                    status=409,
                    details={"outbox_id": str(row.get("outbox_id") or path.stem)},
                )
            source_work_ref = str(assignment.get("work_ref") or "")
            candidates.append(
                {
                    "assignment_ref": assignment_ref,
                    "source_work_ref": source_work_ref,
                    "work_ref": str(
                        reference_mapping.get(source_work_ref) or source_work_ref
                    ),
                    **expected_ownership,
                    "state": str(payload.get("state") or ""),
                    "summary": str(payload.get("summary") or ""),
                    "result_ref": str(payload.get("result_ref") or ""),
                    "source_event_ref": str(payload.get("source_event_ref") or ""),
                    "reported_at": reported_at,
                    "outbox_id": str(row.get("outbox_id") or path.stem),
                    "refusal_code": refusal_code,
                }
            )
    return normalize_plan_assignment_outcomes(candidates, target_refs=item_refs)


def _apply_assignment_outcomes(
    items: Sequence[dict[str, Any]], outcomes: Sequence[Mapping[str, Any]]
) -> None:
    by_ref = {
        parse_plan_node_ref(item.get("item_ref")).ref: item
        for item in items
    }
    for outcome in outcomes:
        item = by_ref[str(outcome["work_ref"])]
        updates: dict[str, Any] = {
            "status": (
                "awaiting_operator"
                if outcome["state"] == "completed"
                else "blocked"
            ),
            "assignee": str(outcome["worker_name"]),
        }
        if outcome["state"] == "completed":
            updates.update(
                result=str(outcome["summary"]),
                result_ref=str(outcome.get("result_ref") or ""),
            )
        else:
            updates["blocked_reason"] = str(outcome["summary"])
        changed = any(item.get(key) != value for key, value in updates.items())
        item.update(updates)
        if changed:
            item["revision"] = max(1, int(item.get("revision") or 1)) + 1
            item["updated_at"] = max(
                _timestamp(item.get("updated_at")),
                _timestamp(outcome.get("reported_at")),
            )


def _prepare_unlocked(field: Any, project_id: str) -> PreparedLocalPlan:
    clean_project = component(project_id, field="project_id")
    project = field.read_project(clean_project)
    plan_ref = str(
        project.get("active_plan_ref") or project.get("current_plan_ref") or ""
    )
    if not plan_ref:
        raise DomainError(
            "field_plan_cutover_source_missing",
            "This project has no local recovery plan to cut over.",
            status=404,
        )
    plan = field.current_plan(clean_project)
    dependency_map = _dependencies(plan)
    raw_items = [
        dict(row) for row in plan.get("items") or [] if isinstance(row, Mapping)
    ]
    item_refs = {
        parse_plan_node_ref(row.get("item_ref")).ref for row in raw_items
    }
    plans_root = field._project_dir(clean_project) / "plans"
    completed_mapping = normalize_reference_mapping(
        BucketedPlanStore(plans_root).completed_reference_mapping()
    )
    _plan_reference_mapping(completed_mapping, target_refs=item_refs)
    reference_mapping = completed_mapping
    assignment_outcomes = _assignment_outcomes(
        field,
        project_id=clean_project,
        project_ref=str(project.get("project_ref") or ""),
        item_refs=item_refs,
        reference_mapping=completed_mapping,
    )
    _apply_assignment_outcomes(raw_items, assignment_outcomes)
    active_assignment_count, active_assignment_bindings = _reconcile_active_assignments(
        field,
        project_id=clean_project,
        items=raw_items,
        reference_mapping=completed_mapping,
        recovered_outcomes=assignment_outcomes,
    )
    rows: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    for position, raw in enumerate(raw_items):
        item_ref = parse_plan_node_ref(raw.get("item_ref")).ref
        rows.append(
            _package_item(
                raw,
                position=position,
                depends_on=dependency_map.get(item_ref, ()),
            )
        )
        notes.extend(_package_notes(raw, item_ref=item_ref))
    project_ref = str(project.get("project_ref") or "")
    plan_revision = int(plan.get("revision") or 0)
    source_hash = content_hash(
        {
            "project_ref": project_ref,
            "plan_ref": plan_ref,
            "plan_revision": plan_revision,
            "items": rows,
            "notes": notes,
        }
    )
    package = {
        "schema": PLAN_INDEX_SCHEMA,
        "project_ref": project_ref,
        "plan_revision": plan_revision,
        "generation_token": f"local-recovery-{source_hash[:32]}",
        "item_count": len(rows),
        "items": rows,
        "notes": notes,
    }
    package["package_content_hash"] = content_hash(package)
    return PreparedLocalPlan(
        project_id=clean_project,
        project_ref=project_ref,
        plan_ref=plan_ref,
        plan_revision=plan_revision,
        package=package,
        reference_mapping=reference_mapping,
        assignment_outcomes=assignment_outcomes,
        active_assignment_count=active_assignment_count,
        active_assignment_bindings=active_assignment_bindings,
    )


def prepare_local_plan_cutover(field: Any, project_id: str) -> PreparedLocalPlan:
    clean_project = component(project_id, field="project_id")
    with field._project_lock_context(clean_project):
        return _prepare_unlocked(field, clean_project)


def _cutover_outbox_state(
    field: Any, project_ref: str
) -> tuple[dict[str, dict[str, int]], list[str]]:
    outbox = OutboxStore(field.control)
    counts = {
        "plan.nodes.publish": {"pending_count": 0, "leased_count": 0},
        "assignment.report": {"pending_count": 0, "leased_count": 0},
    }
    busy: list[str] = []
    with exclusive_lock(outbox.lock):
        for state in ("pending", "leased"):
            for path in outbox.in_flight(state, project_ref=project_ref):
                row = read_json(path)
                kind = str(row.get("kind") or "")
                if kind not in counts or row.get("project_ref") != project_ref:
                    continue
                counts[kind][f"{state}_count"] += 1
                busy.append(str(row.get("outbox_id") or path.stem))
    return counts, busy


def require_local_plan_cutover_ready(
    field: Any, prepared: PreparedLocalPlan
) -> dict[str, Any]:
    plans_root = field._project_dir(prepared.project_id) / "plans"
    state = BucketedPlanStore(plans_root).state()
    if state.get("state") != PLAN_STORAGE_BUCKETED:
        raise DomainError(
            "field_plan_cutover_source_unprepared",
            "Canonicalize the local recovery plan before importing it.",
            status=409,
            details={"state": str(state.get("state") or "")},
        )
    prepared_migrations = sorted(
        path.name for path in (plans_root / "migrations").glob("*.prepared.json")
    )
    if prepared_migrations:
        raise DomainError(
            "field_plan_cutover_migration_incomplete",
            "A local plan migration is still prepared and cannot be retired.",
            status=409,
            details={"prepared": prepared_migrations},
        )
    outbox, busy = _cutover_outbox_state(field, prepared.project_ref)
    if busy:
        raise DomainError(
            "field_plan_cutover_outbox_busy",
            "A plan publication or assignment report is still pending or leased.",
            status=409,
            details={"outbox_ids": busy},
        )
    return {
        "schema": PLAN_CUTOVER_READINESS_SCHEMA,
        "source_storage_state": PLAN_STORAGE_BUCKETED,
        "prepared_migration_count": 0,
        "outbox": outbox,
    }


def validate_import_result(
    prepared: PreparedLocalPlan, imported: Mapping[str, Any]
) -> dict[str, Any]:
    result = dict(imported)
    if str(result.get("project_ref") or "") != prepared.project_ref:
        raise DomainError(
            "plan_cutover_import_response_invalid",
            "The plan import response names another project.",
            status=502,
        )
    if (
        str(result.get("import_package_content_hash") or "")
        != prepared.package_content_hash
        or int(result.get("item_count") or 0) != prepared.item_count
        or int(result.get("note_count") or 0) != prepared.note_count
        or int(result.get("reference_mapping_count") or 0)
        != len(prepared.reference_mapping)
        or str(result.get("reference_mapping_hash") or "")
        != prepared.reference_mapping_hash
        or int(result.get("assignment_outcome_count") or 0)
        != len(prepared.assignment_outcomes)
        or str(result.get("assignment_outcomes_hash") or "")
        != prepared.assignment_outcomes_hash
        or not str(result.get("generation_token") or "")
    ):
        raise DomainError(
            "plan_cutover_import_response_invalid",
            "The plan import response does not prove the complete prepared package.",
            status=502,
            details={
                "expected_package_content_hash": prepared.package_content_hash,
                "expected_item_count": prepared.item_count,
                "expected_note_count": prepared.note_count,
                "expected_reference_mapping_count": len(prepared.reference_mapping),
                "expected_reference_mapping_hash": prepared.reference_mapping_hash,
                "expected_assignment_outcome_count": len(
                    prepared.assignment_outcomes
                ),
                "expected_assignment_outcomes_hash": (
                    prepared.assignment_outcomes_hash
                ),
            },
        )
    return result


def _comparison_item(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "item_id": str(row.get("item_id") or ""),
        "item_ref": str(row.get("item_ref") or ""),
        "item_key": str(row.get("item_key") or ""),
        "title": str(row.get("title") or ""),
        "description": str(row.get("description") or ""),
        "acceptance": [str(value) for value in row.get("acceptance") or []],
        "tags": [str(value) for value in row.get("tags") or []],
        "keywords": [str(value) for value in row.get("keywords") or []],
        "attachment_refs": [
            str(value) for value in row.get("attachment_refs") or []
        ],
        "status": str(row.get("status") or ""),
        "revision": int(row.get("revision") or row.get("item_revision") or 0),
        "assignee": str(row.get("assignee") or ""),
        "depends_on": sorted(str(value) for value in row.get("depends_on") or []),
        "ordinal": int(row.get("ordinal") or 0),
        "created_at": _timestamp(row.get("created_at")),
        "started_at": _timestamp(row.get("started_at")),
        "cancelled_at": _timestamp(row.get("cancelled_at")),
        "cancelled_by": str(row.get("cancelled_by") or ""),
        "updated_at": _timestamp(row.get("updated_at")),
        "note_count": int(row.get("note_count") or 0),
        "attachment_count": int(row.get("attachment_count") or 0),
        "result": row.get("result") if row.get("result") is not None else "",
        "result_ref": str(row.get("result_ref") or ""),
        "blocked_reason": str(row.get("blocked_reason") or ""),
        "cancel_reason": str(row.get("cancel_reason") or ""),
        "review": row.get("review") if row.get("review") is not None else {},
        "summary": str(row.get("summary") or ""),
        "source_content_hash": str(row.get("source_content_hash") or ""),
        "summary_source_hash": str(row.get("summary_source_hash") or ""),
        "search_text": str(row.get("search_text") or ""),
        "search_content_hash": str(row.get("search_content_hash") or ""),
    }


def _comparison_note(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "item_ref": str(row.get("item_ref") or ""),
        "note_id": str(row.get("note_id") or ""),
        "note_ref": str(row.get("note_ref") or ""),
        "ordinal": int(row.get("ordinal") or 0),
        "author": str(row.get("author") or row.get("author_label") or ""),
        "author_kind": str(row.get("author_kind") or ""),
        "text": str(row.get("text") or ""),
        "created_at": _timestamp(row.get("created_at")),
    }


def _state_hash(items: Sequence[Mapping[str, Any]], notes: Sequence[Mapping[str, Any]]) -> str:
    return content_hash(
        {
            "items": sorted(
                (_comparison_item(row) for row in items),
                key=lambda row: (row["ordinal"], row["item_ref"]),
            ),
            "notes": sorted(
                (_comparison_note(row) for row in notes),
                key=lambda row: (row["item_ref"], row["ordinal"], row["note_id"]),
            ),
        }
    )


def verify_remote_plan(
    prepared: PreparedLocalPlan,
    imported: Mapping[str, Any],
    remote: Mapping[str, Any],
) -> dict[str, Any]:
    import_result = validate_import_result(prepared, imported)
    remote_items = [
        dict(row) for row in remote.get("items") or [] if isinstance(row, Mapping)
    ]
    remote_notes = [
        dict(row) for row in remote.get("notes") or [] if isinstance(row, Mapping)
    ]
    expected_hash = _state_hash(
        prepared.package.get("items") or (), prepared.package.get("notes") or ()
    )
    observed_hash = _state_hash(remote_items, remote_notes)
    remote_revision = int(remote.get("plan_revision") or 0)
    remote_generation = str(remote.get("generation_token") or "")
    if (
        str(remote.get("schema") or "") != PLAN_INDEX_SCHEMA
        or str(remote.get("project_ref") or "") != prepared.project_ref
        or int(remote.get("item_count") or 0) != prepared.item_count
        or len(remote_items) != prepared.item_count
        or len(remote_notes) != prepared.note_count
        or remote_revision != int(import_result.get("plan_revision") or 0)
        or remote_generation != str(import_result.get("generation_token") or "")
        or not remote_generation
        or observed_hash != expected_hash
    ):
        raise DomainError(
            "plan_cutover_verification_failed",
            "PostgreSQL does not contain the exact local recovery plan generation.",
            status=409,
            details={
                "expected_item_count": prepared.item_count,
                "observed_item_count": len(remote_items),
                "expected_note_count": prepared.note_count,
                "observed_note_count": len(remote_notes),
                "expected_state_hash": expected_hash,
                "observed_state_hash": observed_hash,
                "imported_plan_revision": int(import_result.get("plan_revision") or 0),
                "observed_plan_revision": remote_revision,
            },
        )
    return {
        "plan_revision": remote_revision,
        "generation_token": remote_generation,
        "item_count": len(remote_items),
        "note_count": len(remote_notes),
        "state_hash": observed_hash,
        "reference_mapping_count": len(prepared.reference_mapping),
        "reference_mapping_hash": prepared.reference_mapping_hash,
        "assignment_outcome_count": len(prepared.assignment_outcomes),
        "assignment_outcomes_hash": prepared.assignment_outcomes_hash,
        "verified_at": utc_now(),
    }


def _cutover_receipt_path(field: Any, project_id: str, cutover_id: str) -> Path:
    return (
        field._project_dir(project_id)
        / PLAN_CUTOVER_RECEIPTS
        / f"{component(cutover_id, field='cutover_id')}.json"
    )


def _redact_plan_outbox_unlocked(
    field: Any, *, project_ref: str, cutover_receipt: str
) -> int:
    outbox = OutboxStore(field.control)
    now = utc_now()
    redacted = 0
    with exclusive_lock(outbox.lock):
        busy: list[str] = []
        for state in ("pending", "leased"):
            for path in outbox.in_flight(state, project_ref=project_ref):
                row = read_json(path)
                if (
                    row.get("kind") == "plan.nodes.publish"
                    and row.get("project_ref") == project_ref
                ):
                    busy.append(str(row.get("outbox_id") or path.stem))
        if busy:
            raise DomainError(
                "field_plan_cutover_outbox_busy",
                "A legacy plan publication became pending or leased during cutover.",
                status=409,
                details={"outbox_ids": busy},
            )
        for path in list(outbox.settled_paths(project_ref=project_ref, op="plan_cutover")):
            row = read_json(path)
            if (
                row.get("kind") != "plan.nodes.publish"
                or row.get("project_ref") != project_ref
                or "payload" not in row
            ):
                continue
            row["payload_content_hash"] = str(row.get("content_hash") or "")
            row["payload_retired_at"] = now
            row["plan_cutover_receipt"] = cutover_receipt
            row.pop("payload", None)
            atomic_write_json(path, row)
            redacted += 1
    return redacted


def _archive_migration_receipts(field: Any, prepared: PreparedLocalPlan) -> list[str]:
    project_root = field._project_dir(prepared.project_id)
    plans_root = project_root / "plans"
    storage = BucketedPlanStore(plans_root)
    storage.completed_reference_mapping()
    source_root = plans_root / "migrations"
    archive_root = project_root / PLAN_MIGRATION_ARCHIVE
    archived: list[str] = []
    for path in sorted(source_root.glob("*.json")):
        if path.name.endswith(".prepared.json"):
            raise DomainError(
                "field_plan_cutover_migration_incomplete",
                "A prepared migration cannot be archived as a completed receipt.",
                status=409,
                details={"path": path.name},
            )
        value = read_json(path)
        target = archive_root / path.name
        existing = read_json(target, required=False)
        if existing and existing != value:
            raise DomainError(
                "field_plan_migration_history_ambiguous",
                "An archived migration receipt disagrees with local recovery history.",
                status=409,
                details={"path": path.name},
            )
        atomic_write_json(target, value)
        archived.append(path.name)
    return archived


def _finish_retirement(
    field: Any,
    *,
    project_id: str,
    project: dict[str, Any],
    receipt_path: Path,
    receipt: dict[str, Any],
) -> dict[str, Any]:
    relative_receipt = receipt_path.relative_to(
        field._project_dir(project_id)
    ).as_posix()
    redacted = _redact_plan_outbox_unlocked(
        field,
        project_ref=str(receipt["project_ref"]),
        cutover_receipt=relative_receipt,
    )
    project.pop("active_plan_ref", None)
    project.pop("current_plan_ref", None)
    project["plan_authority"] = "postgresql"
    project["plan_cutover_receipt"] = relative_receipt
    project["revision"] = int(project.get("revision") or 0) + 1
    project["updated_at"] = utc_now()
    atomic_write_json(field._project_path(project_id), project)
    plans_root = field._project_dir(project_id) / "plans"
    if plans_root.exists():
        shutil.rmtree(plans_root)
    receipt.update(
        state="complete",
        outbox_payloads_redacted=int(receipt.get("outbox_payloads_redacted") or 0)
        + redacted,
        local_plan_removed=True,
        completed_at=utc_now(),
    )
    atomic_write_json(receipt_path, receipt)
    return {
        "schema": PLAN_CUTOVER_RESULT_SCHEMA,
        "state": "complete",
        "project_ref": str(receipt["project_ref"]),
        "source_plan_ref": str(receipt["source_plan_ref"]),
        "package_content_hash": str(receipt["package_content_hash"]),
        "cutover_content_hash": _receipt_cutover_content_hash(receipt),
        "plan_revision": int(receipt["remote_verification"]["plan_revision"]),
        "generation_token": str(receipt["remote_verification"]["generation_token"]),
        "item_count": int(receipt["item_count"]),
        "note_count": int(receipt["note_count"]),
        "migration_receipts": list(receipt.get("migration_receipts") or []),
        "reference_mapping_count": int(receipt.get("reference_mapping_count") or 0),
        "reference_mapping_hash": str(receipt.get("reference_mapping_hash") or ""),
        "assignment_outcome_count": int(
            receipt.get("assignment_outcome_count") or 0
        ),
        "assignment_outcomes_hash": str(
            receipt.get("assignment_outcomes_hash") or ""
        ),
        "active_assignment_count": int(receipt.get("active_assignment_count") or 0),
        "active_assignment_bindings_hash": str(
            receipt.get("active_assignment_bindings_hash") or ""
        ),
        "outbox_payloads_redacted": int(receipt["outbox_payloads_redacted"]),
        "receipt": relative_receipt,
    }


def _completed_result(
    field: Any,
    project_id: str,
    receipt_path: Path,
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema": PLAN_CUTOVER_RESULT_SCHEMA,
        "state": "already_complete",
        "project_ref": str(receipt["project_ref"]),
        "source_plan_ref": str(receipt["source_plan_ref"]),
        "package_content_hash": str(receipt["package_content_hash"]),
        "cutover_content_hash": _receipt_cutover_content_hash(receipt),
        "plan_revision": int(receipt["remote_verification"]["plan_revision"]),
        "generation_token": str(receipt["remote_verification"]["generation_token"]),
        "item_count": int(receipt["item_count"]),
        "note_count": int(receipt["note_count"]),
        "migration_receipts": list(receipt.get("migration_receipts") or []),
        "reference_mapping_count": int(receipt.get("reference_mapping_count") or 0),
        "reference_mapping_hash": str(receipt.get("reference_mapping_hash") or ""),
        "assignment_outcome_count": int(
            receipt.get("assignment_outcome_count") or 0
        ),
        "assignment_outcomes_hash": str(
            receipt.get("assignment_outcomes_hash") or ""
        ),
        "active_assignment_count": int(receipt.get("active_assignment_count") or 0),
        "active_assignment_bindings_hash": str(
            receipt.get("active_assignment_bindings_hash") or ""
        ),
        "outbox_payloads_redacted": int(receipt.get("outbox_payloads_redacted") or 0),
        "receipt": receipt_path.relative_to(field._project_dir(project_id)).as_posix(),
    }


def _receipt_cutover_content_hash(receipt: Mapping[str, Any]) -> str:
    supplied = str(receipt.get("cutover_content_hash") or "")
    if supplied:
        return supplied
    return content_hash(
        {
            "package_content_hash": str(receipt.get("package_content_hash") or ""),
            "reference_mapping_hash": str(
                receipt.get("reference_mapping_hash") or ""
            ),
            "assignment_outcomes_hash": str(
                receipt.get("assignment_outcomes_hash") or ""
            ),
            "active_assignment_bindings_hash": str(
                receipt.get("active_assignment_bindings_hash") or ""
            ),
        }
    )


def resume_local_plan_retirement(
    field: Any,
    project_id: str,
    *,
    expected_cutover_content_hash: str = "",
    expected_idempotency_key: str = "",
) -> dict[str, Any] | None:
    clean_project = component(project_id, field="project_id")
    with field._project_lock_context(clean_project):
        project = field.read_project(clean_project)
        relative = str(project.get("plan_cutover_receipt") or "")
        if not relative:
            return None
        receipt_path = field._project_dir(clean_project) / relative
        receipt = read_json(receipt_path)
        if receipt.get("schema") != PLAN_CUTOVER_RECEIPT_SCHEMA:
            raise DomainError(
                "field_plan_cutover_receipt_invalid",
                "The local plan cutover receipt is invalid.",
                status=409,
            )
        if receipt.get("state") not in {"retiring", "complete"}:
            raise DomainError(
                "field_plan_cutover_receipt_invalid",
                "The local plan cutover receipt has an unsupported state.",
                status=409,
            )
        observed_cutover_hash = _receipt_cutover_content_hash(receipt)
        if (
            expected_cutover_content_hash
            and expected_cutover_content_hash != observed_cutover_hash
        ):
            raise DomainError(
                "field_plan_cutover_preview_changed",
                "The retained cutover receipt differs from the operator-reviewed preview.",
                status=409,
                details={
                    "expected_cutover_hash": expected_cutover_content_hash,
                    "observed_cutover_hash": observed_cutover_hash,
                },
            )
        receipt_idempotency_key = str(receipt.get("idempotency_key") or "")
        if (
            expected_idempotency_key
            and expected_idempotency_key != receipt_idempotency_key
        ):
            raise DomainError(
                "field_plan_cutover_retry_identity_changed",
                "Resume the cutover with the idempotency key recorded in its receipt.",
                status=409,
                details={"receipt_idempotency_key": receipt_idempotency_key},
            )
        if (
            receipt.get("state") == "complete"
            and not (field._project_dir(clean_project) / "plans").exists()
            and not project.get("active_plan_ref")
            and not project.get("current_plan_ref")
        ):
            return _completed_result(field, clean_project, receipt_path, receipt)
        return _finish_retirement(
            field,
            project_id=clean_project,
            project=project,
            receipt_path=receipt_path,
            receipt=receipt,
        )


def retire_local_plan(
    field: Any,
    prepared: PreparedLocalPlan,
    *,
    import_result: Mapping[str, Any],
    remote_verification: Mapping[str, Any],
    idempotency_key: str,
) -> dict[str, Any]:
    with field._project_lock_context(prepared.project_id):
        current = _prepare_unlocked(field, prepared.project_id)
        if (
            current.package_content_hash != prepared.package_content_hash
            or current.reference_mapping_hash != prepared.reference_mapping_hash
            or current.assignment_outcomes_hash != prepared.assignment_outcomes_hash
            or current.active_assignment_bindings_hash
            != prepared.active_assignment_bindings_hash
        ):
            raise DomainError(
                "field_plan_cutover_source_changed",
                "The local recovery plan changed while PostgreSQL was being verified.",
                status=409,
                details={
                    "prepared_package_content_hash": prepared.package_content_hash,
                    "current_package_content_hash": current.package_content_hash,
                    "prepared_reference_mapping_hash": prepared.reference_mapping_hash,
                    "current_reference_mapping_hash": current.reference_mapping_hash,
                    "prepared_assignment_outcomes_hash": (
                        prepared.assignment_outcomes_hash
                    ),
                    "current_assignment_outcomes_hash": (
                        current.assignment_outcomes_hash
                    ),
                    "prepared_active_assignment_bindings_hash": (
                        prepared.active_assignment_bindings_hash
                    ),
                    "current_active_assignment_bindings_hash": (
                        current.active_assignment_bindings_hash
                    ),
                },
            )
        require_local_plan_cutover_ready(field, current)
        archived = _archive_migration_receipts(field, current)
        cutover_id = f"cutover-{prepared.cutover_content_hash[:20]}"
        receipt_path = _cutover_receipt_path(field, prepared.project_id, cutover_id)
        relative_receipt = receipt_path.relative_to(
            field._project_dir(prepared.project_id)
        ).as_posix()
        receipt = {
            "schema": PLAN_CUTOVER_RECEIPT_SCHEMA,
            "state": "retiring",
            "cutover_id": cutover_id,
            "project_ref": prepared.project_ref,
            "source_plan_ref": prepared.plan_ref,
            "source_plan_revision": prepared.plan_revision,
            "package_content_hash": prepared.package_content_hash,
            "cutover_content_hash": prepared.cutover_content_hash,
            "item_count": prepared.item_count,
            "note_count": prepared.note_count,
            "reference_mapping_count": len(prepared.reference_mapping),
            "reference_mapping_hash": prepared.reference_mapping_hash,
            "assignment_outcome_count": len(prepared.assignment_outcomes),
            "assignment_outcomes_hash": prepared.assignment_outcomes_hash,
            "active_assignment_count": prepared.active_assignment_count,
            "active_assignment_bindings_hash": (
                prepared.active_assignment_bindings_hash
            ),
            "idempotency_key": str(idempotency_key),
            "import_result": dict(import_result),
            "remote_verification": dict(remote_verification),
            "migration_receipts": archived,
            "outbox_payloads_redacted": 0,
            "local_plan_removed": False,
            "prepared_at": utc_now(),
        }
        atomic_write_json(receipt_path, receipt)
        project = field.read_project(prepared.project_id)
        project["plan_cutover_receipt"] = relative_receipt
        atomic_write_json(field._project_path(prepared.project_id), project)
        return _finish_retirement(
            field,
            project_id=prepared.project_id,
            project=project,
            receipt_path=receipt_path,
            receipt=receipt,
        )


__all__ = [
    "PLAN_CUTOVER_RECEIPT_SCHEMA",
    "PLAN_CUTOVER_RESULT_SCHEMA",
    "PLAN_CUTOVER_PREVIEW_SCHEMA",
    "PLAN_CUTOVER_READINESS_SCHEMA",
    "PreparedLocalPlan",
    "local_plan_cutover_preview",
    "prepared_local_plan_from_preview",
    "prepare_local_plan_cutover",
    "require_local_plan_cutover_ready",
    "resume_local_plan_retirement",
    "retire_local_plan",
    "validate_import_result",
    "verify_remote_plan",
]

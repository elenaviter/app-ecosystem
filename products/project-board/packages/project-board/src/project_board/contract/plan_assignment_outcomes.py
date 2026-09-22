from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .errors import DomainError
from .plan_nodes import plan_node_identity_ref
from .refs import parse_ref
from .work_lifecycle import REVIEW, TODO, canonical_work_status


MAX_PLAN_ASSIGNMENT_OUTCOMES = 1_000
TERMINAL_ASSIGNMENT_STATES = frozenset({"completed", "refused"})
MISSING_PLAN_REFUSAL_CODE = "work_plan_item_not_found"


def _text(
    value: Any,
    *,
    field: str,
    maximum: int,
    required: bool = False,
) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise DomainError(
            "plan_import_assignment_outcome_incomplete",
            "An assignment outcome is missing required cutover evidence.",
            status=409,
            details={"field": field},
        )
    if len(text.encode("utf-8")) > maximum:
        raise DomainError(
            "plan_import_assignment_outcome_too_large",
            "An assignment outcome exceeds the guarded import limit.",
            status=409,
            details={"field": field, "maximum": maximum},
        )
    return text


def _timestamp(value: Any) -> str:
    text = _text(value, field="reported_at", maximum=128, required=True)
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DomainError(
            "plan_import_assignment_outcome_timestamp_invalid",
            "An assignment outcome needs a valid report timestamp.",
            status=409,
            details={"reported_at": text},
        ) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_plan_assignment_outcomes(
    value: Sequence[Mapping[str, Any]] | None,
    *,
    target_refs: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Validate terminal assignment effects preserved for one plan cutover."""

    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise DomainError(
            "plan_import_assignment_outcomes_invalid",
            "Assignment outcomes must be a list.",
        )
    if len(value) > MAX_PLAN_ASSIGNMENT_OUTCOMES:
        raise DomainError(
            "plan_import_assignment_outcomes_too_large",
            "The assignment outcome list exceeds the guarded import limit.",
            status=409,
            details={
                "outcome_count": len(value),
                "maximum": MAX_PLAN_ASSIGNMENT_OUTCOMES,
            },
        )

    normalized: list[dict[str, Any]] = []
    seen_assignments: set[str] = set()
    seen_targets: set[str] = set()
    for position, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            raise DomainError(
                "plan_import_assignment_outcome_invalid",
                "Every assignment outcome must be an object.",
                status=409,
                details={"position": position},
            )
        assignment_ref = _text(
            raw.get("assignment_ref"),
            field="assignment_ref",
            maximum=256,
            required=True,
        )
        parsed_assignment = parse_ref(assignment_ref)
        if parsed_assignment.kind != "assignment":
            raise DomainError(
                "plan_import_assignment_outcome_ref_invalid",
                "An assignment outcome must name a work:assignment URI.",
                status=409,
                details={"assignment_ref": assignment_ref},
            )
        if assignment_ref in seen_assignments:
            raise DomainError(
                "plan_import_assignment_outcome_duplicate",
                "The import repeats one assignment outcome.",
                status=409,
                details={"assignment_ref": assignment_ref},
            )
        seen_assignments.add(assignment_ref)

        source_work_ref = _text(
            raw.get("source_work_ref"),
            field="source_work_ref",
            maximum=512,
            required=True,
        )
        work_ref = plan_node_identity_ref(raw.get("work_ref"))
        if target_refs is not None and work_ref not in target_refs:
            raise DomainError(
                "plan_import_assignment_outcome_target_missing",
                "An assignment outcome points outside the imported plan.",
                status=409,
                details={"assignment_ref": assignment_ref, "work_ref": work_ref},
            )
        if work_ref in seen_targets:
            raise DomainError(
                "plan_import_assignment_outcome_target_duplicate",
                "Only one terminal assignment outcome may own an imported item.",
                status=409,
                details={"work_ref": work_ref},
            )
        seen_targets.add(work_ref)
        worker_name = _text(
            raw.get("worker_name"),
            field="worker_name",
            maximum=256,
            required=True,
        ).lower()
        try:
            ownership_version = int(raw.get("ownership_version"))
        except (TypeError, ValueError) as exc:
            raise DomainError(
                "plan_import_assignment_outcome_version_invalid",
                "An assignment outcome needs a positive ownership version.",
                status=409,
                details={"assignment_ref": assignment_ref},
            ) from exc
        if ownership_version < 1:
            raise DomainError(
                "plan_import_assignment_outcome_version_invalid",
                "An assignment outcome needs a positive ownership version.",
                status=409,
                details={"assignment_ref": assignment_ref},
            )
        state = _text(
            raw.get("state"), field="state", maximum=32, required=True
        ).lower()
        if state not in TERMINAL_ASSIGNMENT_STATES:
            raise DomainError(
                "plan_import_assignment_outcome_state_invalid",
                "A cutover assignment outcome must be completed or refused.",
                status=409,
                details={"assignment_ref": assignment_ref, "state": state},
            )
        refusal_code = _text(
            raw.get("refusal_code"),
            field="refusal_code",
            maximum=128,
            required=True,
        )
        if refusal_code != MISSING_PLAN_REFUSAL_CODE:
            raise DomainError(
                "plan_import_assignment_outcome_refusal_invalid",
                "Only an assignment report refused because its plan item was absent can be recovered by plan cutover.",
                status=409,
                details={
                    "assignment_ref": assignment_ref,
                    "refusal_code": refusal_code,
                },
            )
        normalized.append(
            {
                "assignment_ref": assignment_ref,
                "source_work_ref": source_work_ref,
                "work_ref": work_ref,
                "worker_name": worker_name,
                "ownership_version": ownership_version,
                "state": state,
                "summary": _text(
                    raw.get("summary"),
                    field="summary",
                    maximum=8_000,
                    required=True,
                ),
                "result_ref": _text(
                    raw.get("result_ref"), field="result_ref", maximum=1_000
                ),
                "source_event_ref": _text(
                    raw.get("source_event_ref"),
                    field="source_event_ref",
                    maximum=1_000,
                    required=True,
                ),
                "reported_at": _timestamp(raw.get("reported_at")),
                "outbox_id": _text(
                    raw.get("outbox_id"),
                    field="outbox_id",
                    maximum=256,
                    required=True,
                ),
                "refusal_code": refusal_code,
            }
        )
    return sorted(normalized, key=lambda row: row["assignment_ref"])


def validate_assignment_outcome_items(
    outcomes: Sequence[Mapping[str, Any]],
    items_by_ref: Mapping[str, Mapping[str, Any]],
) -> None:
    """Require the imported item body to carry each recovered report effect."""

    for outcome in outcomes:
        assignment_ref = str(outcome.get("assignment_ref") or "")
        work_ref = str(outcome.get("work_ref") or "")
        item = items_by_ref.get(work_ref)
        if item is None:
            raise DomainError(
                "plan_import_assignment_outcome_target_missing",
                "An assignment outcome points outside the imported plan.",
                status=409,
                details={"assignment_ref": assignment_ref, "work_ref": work_ref},
            )
        state = str(outcome.get("state") or "")
        expected_status = REVIEW if state == "completed" else TODO
        expected_body = (
            str(item.get("result") or "") == str(outcome.get("summary") or "")
            and str(item.get("result_ref") or "")
            == str(outcome.get("result_ref") or "")
            if state == "completed"
            else str(item.get("blocked_reason") or "")
            == str(outcome.get("summary") or "")
        )
        if (
            canonical_work_status(item.get("status")) != expected_status
            or str(item.get("assignee") or "")
            != (
                str(outcome.get("worker_name") or "")
                if state == "completed"
                else ""
            )
            or not expected_body
        ):
            raise DomainError(
                "plan_import_assignment_outcome_item_conflict",
                "The imported item does not carry the assignment report effect being recovered.",
                status=409,
                details={
                    "assignment_ref": assignment_ref,
                    "work_ref": work_ref,
                    "assignment_state": state,
                    "item_status": str(item.get("status") or ""),
                    "item_assignee": str(item.get("assignee") or ""),
                },
            )


__all__ = [
    "MAX_PLAN_ASSIGNMENT_OUTCOMES",
    "MISSING_PLAN_REFUSAL_CODE",
    "TERMINAL_ASSIGNMENT_STATES",
    "normalize_plan_assignment_outcomes",
    "validate_assignment_outcome_items",
]

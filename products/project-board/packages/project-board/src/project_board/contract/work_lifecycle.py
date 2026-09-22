from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .errors import DomainError


TODO = "todo"
WORKING = "working"
REVIEW = "review"
DONE = "done"
CANCELLED = "cancelled"

CANONICAL_WORK_STATUSES = (TODO, WORKING, REVIEW, DONE, CANCELLED)
REVIEW_DECISIONS = ("accept", "return", "cancel")
ACTIVE_ASSIGNMENT_STATES = frozenset({"routing", "assigned", "working", "blocked"})

# Old names remain accepted at every boundary during the rolling migration.
# Stored rows are rewritten by the schema migration, while URLs, filters,
# events, and older clients can continue to send or display the old values.
WORK_STATUS_ALIASES = {
    "draft": TODO,
    "ready": TODO,
    "waiting": TODO,
    "blocked": WORKING,
    "awaiting_operator": REVIEW,
    "accepted": DONE,
    "complete": DONE,
    "completed": DONE,
    "finished": DONE,
    "resolved": DONE,
    "canceled": CANCELLED,
}

_STORAGE_VALUES = {
    TODO: (TODO, "draft", "ready", "waiting"),
    WORKING: (WORKING, "blocked"),
    REVIEW: (REVIEW, "awaiting_operator"),
    DONE: (DONE, "accepted", "complete", "completed", "finished", "resolved"),
    CANCELLED: (CANCELLED, "canceled"),
}

_STATUS_FIELDS = frozenset(
    {
        "status",
        "item_status",
        "expected_item_status",
        "requested_status",
        "previous_status",
        "next_status",
        "from_status",
        "to_status",
    }
)


@dataclass(frozen=True)
class WorkStatusMutation:
    """The coupled item/assignment state written by one SQL transaction."""

    status: str
    assignee: str
    preferred_reworker: str
    assignment_state: str
    fence_ownership: bool
    clear_cancellation: bool


def plan_status_mutation(
    *,
    current_status: Any,
    requested_status: Any,
    current_assignee: Any = "",
    preferred_reworker: Any = "",
    assignment_worker: Any = "",
    assignment_state: Any = "",
) -> WorkStatusMutation:
    """Plan a direct status edit without performing any writes.

    A status edit changes the status and nothing else: the assignee, the
    preferred reworker, the assignment state and the ownership version stay as
    they are. Assignment and status are independent in both directions
    (operator rulings 2026-09-22, W245 and W270): ownership moves only through
    assignment.assign, assignment.return, a reassignment, or a named review
    decision. The one business rule kept here is that Working needs an assignee.
    Why: on 2026-09-22 the operator moved W239 to Todo and the status edit
    silently released codex-main's assignment.
    """

    del assignment_worker, assignment_state
    current = canonical_work_status(current_status, strict=True)
    target = canonical_work_status(requested_status, strict=True)
    assignee = str(current_assignee or "").strip()
    if target == WORKING and not assignee:
        raise DomainError(
            "work_item_status_requires_assignment",
            "Working status requires an assignee. Assign the item first.",
            status=409,
            details={"status": target, "required_operation": "assignment.assign"},
        )
    return WorkStatusMutation(
        status=target,
        assignee=assignee,
        preferred_reworker=str(preferred_reworker or "").strip(),
        assignment_state="",
        fence_ownership=False,
        clear_cancellation=current == CANCELLED and target != CANCELLED,
    )


def canonical_work_status(value: Any, *, strict: bool = False) -> str:
    status = str(value or "").strip().lower()
    canonical = WORK_STATUS_ALIASES.get(status, status)
    if strict and canonical not in CANONICAL_WORK_STATUSES:
        raise DomainError(
            "work_item_status_invalid",
            "Work-item status must be todo, working, review, done, or cancelled.",
            details={
                "status": status,
                "allowed": list(CANONICAL_WORK_STATUSES),
            },
        )
    return canonical


def storage_status_values(values: Iterable[Any]) -> list[str]:
    expanded: set[str] = set()
    for value in values:
        canonical = canonical_work_status(value, strict=True)
        expanded.update(_STORAGE_VALUES[canonical])
    return sorted(expanded)


def canonical_status_counts(
    rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    counts: defaultdict[str, int] = defaultdict(int)
    for row in rows:
        status = canonical_work_status(row.get("status"))
        if status:
            counts[status] += int(row.get("count") or 0)
    return [
        {"status": status, "count": counts[status]}
        for status in CANONICAL_WORK_STATUSES
        if counts.get(status)
    ]


def canonicalize_status_fields(value: Any) -> Any:
    """Project legacy lifecycle names without rewriting immutable history."""

    if isinstance(value, Mapping):
        return {
            key: (
                canonical_work_status(item)
                if str(key) in _STATUS_FIELDS and isinstance(item, str)
                else canonicalize_status_fields(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [canonicalize_status_fields(item) for item in value]
    return value


def valid_transition_operations(status: Any) -> tuple[str, ...]:
    canonical = canonical_work_status(status)
    if canonical == TODO:
        return ("assignment.assign", "work.cancel")
    if canonical == WORKING:
        return ("work.cancel",)
    if canonical == REVIEW:
        return ("review.accept", "review.return", "review.cancel")
    return ()


__all__ = [
    "ACTIVE_ASSIGNMENT_STATES",
    "CANONICAL_WORK_STATUSES",
    "CANCELLED",
    "DONE",
    "REVIEW",
    "REVIEW_DECISIONS",
    "TODO",
    "WORKING",
    "WORK_STATUS_ALIASES",
    "WorkStatusMutation",
    "canonical_status_counts",
    "canonical_work_status",
    "canonicalize_status_fields",
    "plan_status_mutation",
    "storage_status_values",
    "valid_transition_operations",
]

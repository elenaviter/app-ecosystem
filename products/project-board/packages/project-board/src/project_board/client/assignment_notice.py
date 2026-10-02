"""Assignment reactions from committed status, with a trusted explicit-reopen exception.

An assignee is who the item is with, in every status (operator ruling,
2026-09-30). Assigning a Done item makes that person or agent its assignee and
tells them so: it is not a request to work on it. Before W406 every assignment
notice said "work to begin", and an agent given a Done item reported working
on it, undoing the operator's completed state.

W451 distinguishes an authorized assignment.assign reopen under new ownership
from an ordinary assignee edit. Only its validated Board evidence asks the new
owner to begin work while the item's old status remains unchanged.
"""

from __future__ import annotations

from ..contract.work_lifecycle import CANCELLED, DONE, REVIEW, TODO, WORKING

# What the assignee does on receipt, carried as ``expected_reaction``.
REACTION_BEGIN_WORK = "begin_work"
REACTION_AWAIT_REVIEW = "await_review"
REACTION_ACKNOWLEDGE_ONLY = "acknowledge_only"

_STATUS_LABELS = {
    TODO: "Todo",
    WORKING: "Working",
    REVIEW: "Review",
    DONE: "Done",
    CANCELLED: "Cancelled",
}

_READ_COMMANDS = (
    "  pb coordinate project.plan.item --object-ref <project-ref> "
    "--payload-json '{\"item_key\":\"<Wn>\"}'\n"
)

_CURRENT_ITEM_DECIDES = (
    "Read the item before you act: its current status decides, and a later "
    "edit to the item wins over this notice.\n\n"
)


def assignment_notice_text(
    *,
    status: str,
    work_ref: str,
    assignment_ref: str,
    ownership_version: int,
    explicit_reopen: bool = False,
) -> tuple[str, str, str]:
    """Format a notice; callers must validate Board evidence before setting explicit_reopen."""

    state = str(status or "").strip().lower()
    label = _STATUS_LABELS.get(state, "")
    item = work_ref or "(not recorded)"
    header = (
        f"Work item: {item}\n"
        f"Assignment: {assignment_ref}\n"
        f"Ownership version: {ownership_version}\n"
        + (f"Item status: {label}\n" if label else "")
        + "\n"
    )
    if explicit_reopen:
        subject = f"Explicitly reopened work: {work_ref}" if work_ref else "Explicitly reopened work"
        body = (
            "This assignment was explicitly reopened under a new ownership version. "
            f"The item still shows {label or state} until your first `working` report "
            "sets Working. Begin work now: no preliminary status edit is required. "
            "Assignment did not change status or started_at.\n\n"
            + header
            + "Read the current item and assignment before acting. This notice is work "
            "to begin only while its ownership remains current and active and the "
            "item still names you. A later closure or ownership change wins; the "
            "unchanged Review, Done or Cancelled status alone does not cancel this "
            "explicit reopen. Report against this new ownership, never the former one.\n\n"
            + _READ_COMMANDS
            + "  pb worker report --assignment-ref <assignment> "
            "--ownership-version <version> --state working\n"
        )
        return subject, body, REACTION_BEGIN_WORK
    if state in {DONE, CANCELLED}:
        subject = (
            f"Assigned a {label} item, for information: {work_ref}"
            if work_ref
            else f"Assigned a {label} item, for information"
        )
        body = (
            f"You are now the assignee of an item that is {label}. This notice "
            "is information only. Do not start implementation, do not report "
            "working, and do not reopen the item or change its status because "
            f"of it. The item stays {label} and is listed with you. Settle this "
            "notice with what you read.\n\n"
            + header
            + _CURRENT_ITEM_DECIDES
            + _READ_COMMANDS
        )
        return subject, body, REACTION_ACKNOWLEDGE_ONLY
    if state == REVIEW:
        subject = (
            f"Assigned an item in Review: {work_ref}"
            if work_ref
            else "Assigned an item in Review"
        )
        body = (
            "You are now the assignee of an item in Review. Its implementation "
            "waits for the reviewer's decision, so do not report working or "
            "restart the implementation because of this notice. Read the item "
            "and its review. A return from review reaches you as its own "
            "notice, and that is when the work resumes.\n\n"
            + header
            + _CURRENT_ITEM_DECIDES
            + _READ_COMMANDS
        )
        return subject, body, REACTION_AWAIT_REVIEW
    subject = (
        f"Assignment available: {work_ref}" if work_ref else "Assignment available"
    )
    body = (
        "You have been assigned work.\n\n"
        + header
        + "Read the item, do the work, report against this assignment ref "
        "and this ownership version, and commit what you wrote. This is "
        "work to begin, not a notification to acknowledge.\n\n"
        + (_CURRENT_ITEM_DECIDES if label else "")
        + _READ_COMMANDS
        + "  pb worker report --assignment-ref <assignment> "
        "--ownership-version <version>\n"
    )
    return subject, body, REACTION_BEGIN_WORK


__all__ = [
    "REACTION_ACKNOWLEDGE_ONLY",
    "REACTION_AWAIT_REVIEW",
    "REACTION_BEGIN_WORK",
    "assignment_notice_text",
]

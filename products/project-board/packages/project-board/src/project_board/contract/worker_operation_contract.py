from __future__ import annotations

from typing import Any


# These identifiers belong to the Problem Board service contract. Transport
# adapters expose or carry them unchanged; neither MCP, Named Services, nor
# Data Bus owns a second operation namespace.
PROBLEM_BOARD_OPERATION_POLICIES: dict[str, dict[str, Any]] = {
    "project.register": {
        "description": "Register a project and its owner.",
        "grants": ("work:coordinate",),
    },
    "project.set_journal_home": {
        "description": "Set the owner's portable Git-backed journal home.",
        "grants": ("work:coordinate",),
    },
    "project.plan.index": {
        "description": (
            "Open any generation-pinned plan page directly in authored order, "
            "including compact item fields, complete-plan dependency facts, and "
            "complete-plan status and graph-state totals."
        ),
        "grants": ("work:observe",),
    },
    "project.plan.item": {
        "description": (
            "Read one complete authoritative plan item by canonical URI or its "
            "case-insensitive key within the project, including attached file links."
        ),
        "grants": ("work:observe",),
    },
    "project.plan.resolve": {
        "description": (
            "Resolve a bounded explicit set of canonical work-item references and "
            "project-scoped keys through one indexed PostgreSQL query, naming every "
            "selector that is absent."
        ),
        "grants": ("work:observe",),
    },
    "project.plan.search": {
        "description": (
            "Create one immutable complete plan ranking and open any snapshot page "
            "directly; semantic ranking uses one explicitly requested, accounted "
            "query embedding."
        ),
        "grants": ("work:observe",),
    },
    "project.plan.import": {
        "description": (
            "Validate a complete plan package and atomically replace its PostgreSQL "
            "plan under the read generation and a stable retry key, including guarded "
            "reference reconciliation and exact terminal assignment outcomes that were "
            "refused only because their plan item was not yet present."
        ),
        "grants": ("work:coordinate",),
    },
    "project.references.preview": {
        "description": (
            "Preview the exact project-wide URI migration and store its complete "
            "integrity-bound mapping artifact without changing project references."
        ),
        "grants": ("work:coordinate",),
    },
    "project.references.migrate": {
        "description": (
            "Apply one reviewed and idempotent project-wide URI migration, "
            "including assignment identities and orphaned plan references."
        ),
        "grants": ("work:coordinate",),
    },
    "plan.item.create": {
        "description": "Create one authoritative plan item.",
        "grants": ("work:coordinate",),
    },
    "work.accept": {
        "description": (
            "Compatibility alias for review.accept."
        ),
        "grants": ("work:review",),
    },
    "review.accept": {
        "description": (
            "Accept submitted result evidence under the current item revision and "
            "move the work from review to done."
        ),
        "grants": ("work:review",),
    },
    "review.return": {
        "description": (
            "Return reviewed work to todo for rework, recording the reason. The "
            "assignee keeps the assignment and its ownership version advances, so "
            "the rework has its own reports."
        ),
        "grants": ("work:review",),
    },
    "review.cancel": {
        "description": (
            "Cancel reviewed work with a durable reason and evidence record."
        ),
        "grants": ("work:review",),
    },
    "plan.item.update": {
        "description": "Update one plan item under its current revision; new attachment refs must be staged uploads.",
        "grants": ("work:coordinate",),
    },
    "work.status.set": {
        "description": (
            "Set one work item's canonical status under its current revision. "
            "The assignee and the assignment stay as they are."
        ),
        "grants": ("work:coordinate",),
    },
    "plan.item.delete": {
        "description": "Delete one unassigned plan item under its current revision.",
        "grants": ("work:coordinate",),
    },
    "plan.note.append": {
        "description": "Append one note and advance the plan item's revision atomically.",
        "grants": ("work:coordinate",),
    },
    "plan.notes.list": {
        "description": (
            "Page the authoritative notes attached to one plan item selected by "
            "canonical URI or project-scoped key."
        ),
        "grants": ("work:observe",),
    },
    "project.plan.embedding_status": {
        "description": "Read missing or stale plan embeddings without model use or writes.",
        "grants": ("work:observe",),
    },
    "project.control.get": {
        "description": "Read the project's linked Connection Hub Control Card and participant link state.",
        "grants": ("work:observe",),
    },
    "project.control.initialize": {
        "description": "Create the project's credentialless Connection Hub Card and link current participants.",
        "grants": ("work:coordinate",),
    },
    "project.control.update": {
        "description": "Update the project's version-control choice or AND/OR access rule on its current Control Card revision.",
        "grants": ("work:coordinate",),
    },
    "journal.view.request": {
        "description": "Request one expiring project-journal catalog page or document view.",
        "grants": ("work:observe",),
    },
    "journal.view.close": {
        "description": "Close and erase an expiring project-journal view.",
        "grants": ("work:observe",),
    },
    "project.link_worker": {
        "description": "Link an idle worker to a project.",
        "grants": ("work:coordinate",),
    },
    "project.unlink_worker": {
        "description": "End one worker's current project attendance.",
        "grants": ("work:coordinate",),
    },
    "worker.rename": {
        "description": "Change a worker's display alias without changing its identity.",
        "grants": ("work:coordinate",),
    },
    "worker.evict": {
        "description": "Suspend a worker registration in reversible limbo.",
        "grants": ("work:coordinate",),
    },
    "worker.restore": {
        "description": "Return a suspended worker registration to the pool.",
        "grants": ("work:coordinate",),
    },
    "worker.retire": {
        "description": "Permanently retire one exact coding-agent session.",
        "grants": ("work:coordinate",),
    },
    "control.enqueue": {
        "description": "Queue one bounded command for a linked worker.",
        "grants": ("work:coordinate",),
    },
    "control.discard": {
        "description": "Discard selected messages previously sent to a worker.",
        "grants": ("work:coordinate",),
    },
    "assignment.assign": {
        "description": "Assign work with an ownership version and the repositories it touches, one entry each.",
        "grants": ("work:coordinate",),
    },
    "assignment.return": {
        "description": (
            "Release the active assignment of stalled work with the owner's "
            "reason; the item keeps its status."
        ),
        "grants": ("work:coordinate",),
    },
    "assignment.list": {
        "description": (
            "Page assignments owned by this worker in one attended project, "
            "with independent assignment-state, work-state, creation-time, "
            "and edit-time filters in newest-edit order."
        ),
        "grants": ("work:relay",),
    },
    "workspace.shared_write.publish": {
        "description": (
            "Publish or replace this worker's expiring shared-workspace status."
        ),
        "grants": ("work:relay",),
    },
    "workspace.shared_write.list": {
        "description": "Read every current shared-workspace write status in the project.",
        "grants": ("work:relay",),
    },
    "workspace.shared_write.clear": {
        "description": "Remove this worker's shared-workspace write status.",
        "grants": ("work:relay",),
    },
    "worker.publish": {
        "description": "Publish this coding-agent session and its logical host identity.",
    },
    "worker.heartbeat": {
        "description": "Refresh worker presence and read its current project attendance.",
    },
    "worker.estimate": {
        "description": "Record or clear until when (UTC) this worker expects to finish what it is on, with a one-line note.",
    },
    "control.pull": {
        "description": "Lease controls addressed to this worker.",
    },
    "control.acknowledge": {
        "description": "Acknowledge one control after local materialization.",
    },
    "control.refuse": {
        "description": "Refuse one leased control with a bounded reason.",
    },
    "control.discard_complete": {
        "description": "Report the local outcome of a message-discard request.",
    },
    "control.worker_settle": {
        "description": "Report how the coding-agent session handled delivered input.",
    },
    "mail.route": {
        "description": "Route one addressed message to another worker or the operator.",
    },
    "mail.reconciliation.list": {
        "description": (
            "Page retained mailbox reconciliation run headers for one governed "
            "project without reading private host mailbox files."
        ),
        "grants": ("work:observe",),
    },
    "mail.reconciliation.read": {
        "description": (
            "Page the normalized evidence recorded by one completed mailbox "
            "reconciliation run."
        ),
        "grants": ("work:observe",),
    },
    "mail.reconciliation.publish": {
        "description": (
            "Publish one bounded, integrity-bound batch from a host-local mailbox "
            "reconciliation receipt."
        ),
    },
    "assignment.report": {
        "description": (
            "Append progress or a terminal result under the exact ownership "
            "version. State plus source_event_ref identifies one immutable "
            "report: an unchanged retry replays it, while a later stage uses "
            "a new source event. A current report updates the assignment and "
            "item; a superseded report remains durable without changing "
            "current state, and an accepted terminal report is final for that "
            "ownership version."
        ),
    },
    "plan.nodes.publish": {
        "description": (
            "Atomically publish a batch of plan nodes. Each node becomes one "
            "compact PostgreSQL row; omitted nodes remain unchanged."
        ),
    },
    "plan.index.embed": {
        "description": (
            "Explicitly spend on accounted embeddings for stale plan-index rows."
        ),
    },
    "event.publish": {
        "description": "Publish one bounded project service event.",
    },
    "journal.view.publish": {
        "description": "Publish a requested journal page or document, including its next-page cursor.",
        "grants": ("work:relay", "work:journal:view"),
    },
    "journal.view.fail": {
        "description": "Report a bounded failure for a requested journal view.",
        "grants": ("work:relay", "work:journal:view"),
    },
    "note.view.publish": {
        "description": "Publish one requested page of work-item notes.",
    },
    "note.view.fail": {
        "description": "Report a bounded failure for a requested note page.",
    },
    "project.report.publish": {
        "description": "Publish one immutable project progress report and its evidence refs.",
    },
    "project.report.fail": {
        "description": "Report a bounded failure for a requested project report.",
    },
    "session.resume.publish": {
        "description": "Publish an expiring local command for resuming this agent session.",
    },
    "session.resume.fail": {
        "description": "Report a bounded failure to prepare a session-resume command.",
    },
    "attachment.request_upload": {
        "description": "Reserve a governed upload slot for a worker-produced file.",
    },
}

PROBLEM_BOARD_OPERATIONS = frozenset(PROBLEM_BOARD_OPERATION_POLICIES)

# A service operation has one owning object kind even when transports present
# it differently. Both the direct Problem Board MCP and named services project
# this inventory; neither surface owns a private subset.
PROBLEM_BOARD_OPERATIONS_BY_KIND: dict[str, tuple[str, ...]] = {
    "work.project": (
        "project.register",
        "project.set_journal_home",
        "project.plan.index",
        "project.plan.item",
        "project.plan.resolve",
        "project.plan.search",
        "project.plan.import",
        "project.references.preview",
        "project.references.migrate",
        "project.plan.embedding_status",
        "plan.item.create",
        "plan.item.update",
        "work.status.set",
        "review.accept",
        "review.return",
        "review.cancel",
        "work.accept",
        "plan.item.delete",
        "plan.note.append",
        "plan.notes.list",
        "project.control.get",
        "project.control.initialize",
        "project.control.update",
        "project.link_worker",
        "project.unlink_worker",
        "control.enqueue",
        "assignment.assign",
        "assignment.return",
        "assignment.list",
        "workspace.shared_write.publish",
        "workspace.shared_write.list",
        "workspace.shared_write.clear",
        "mail.route",
        "mail.reconciliation.list",
        "mail.reconciliation.read",
        "mail.reconciliation.publish",
        "plan.nodes.publish",
        "plan.index.embed",
        "event.publish",
        "journal.view.request",
        "attachment.request_upload",
    ),
    "work.worker": (
        "worker.publish",
        "worker.rename",
        "worker.estimate",
        "worker.heartbeat",
        "worker.evict",
        "worker.restore",
        "worker.retire",
        "control.pull",
        "control.discard",
    ),
    "work.control": (
        "control.acknowledge",
        "control.refuse",
        "control.discard_complete",
        "control.worker_settle",
    ),
    "work.assignment": ("assignment.report",),
    "work.event": (),
    "work.journal_view": (
        "journal.view.publish",
        "journal.view.fail",
        "journal.view.close",
    ),
    "work.note_view": (
        "note.view.publish",
        "note.view.fail",
    ),
    "work.report": (
        "project.report.publish",
        "project.report.fail",
    ),
    "work.session_resume": (
        "session.resume.publish",
        "session.resume.fail",
    ),
}


def operation_inventory_by_kind() -> frozenset[str]:
    return frozenset(
        operation
        for operations in PROBLEM_BOARD_OPERATIONS_BY_KIND.values()
        for operation in operations
    )


def canonical_problem_board_operation(operation: str) -> str:
    """Return the service ID carried behind either published presentation."""

    value = str(operation or "").strip()
    named_service_prefix = "object.action."
    if value.startswith(named_service_prefix):
        value = value[len(named_service_prefix) :]
    return value if value in PROBLEM_BOARD_OPERATIONS else ""


if operation_inventory_by_kind() != PROBLEM_BOARD_OPERATIONS:
    missing = sorted(PROBLEM_BOARD_OPERATIONS - operation_inventory_by_kind())
    extra = sorted(operation_inventory_by_kind() - PROBLEM_BOARD_OPERATIONS)
    raise RuntimeError(
        "Problem Board operation placement must cover the canonical inventory "
        f"exactly (missing={missing}, extra={extra})."
    )

# Transitional aliases for local callers that imported the earlier worker-only
# contract. They now resolve to the complete service operation policy.
WORKER_OPERATION_POLICIES = PROBLEM_BOARD_OPERATION_POLICIES
WORKER_OPERATIONS = PROBLEM_BOARD_OPERATIONS


def required_grants_for_operation(operation: str) -> frozenset[str]:
    policy = PROBLEM_BOARD_OPERATION_POLICIES.get(str(operation or "").strip(), {})
    return frozenset(policy.get("grants") or ("work:relay",))


__all__ = [
    "canonical_problem_board_operation",
    "PROBLEM_BOARD_OPERATIONS",
    "PROBLEM_BOARD_OPERATIONS_BY_KIND",
    "PROBLEM_BOARD_OPERATION_POLICIES",
    "WORKER_OPERATIONS",
    "WORKER_OPERATION_POLICIES",
    "operation_inventory_by_kind",
    "required_grants_for_operation",
]

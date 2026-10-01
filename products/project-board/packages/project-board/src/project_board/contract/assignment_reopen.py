"""Ownership-bound evidence of an explicitly authorized assignment reopen.

Only the board's assignment reservation mints this evidence, after existing
assignment authorization. It records an act, not a grant. Consumers accept it
only beside the matching current active assignment from a board control/view;
ordinary mail text, field edits and stale ownership cannot promote a notice.
"""

from __future__ import annotations

from typing import Any, Mapping

REOPEN_EVIDENCE_SCHEMA = "problem-board.assignment-reopen.v1"
ACTIVE_ASSIGNMENT_STATES = frozenset({"routing", "assigned", "working", "blocked"})


def make_reopen_evidence(*, assignment_ref: str, project_ref: str,
                         ownership_version: int, worker_name: str) -> dict[str, Any]:
    return {
        "schema": REOPEN_EVIDENCE_SCHEMA,
        "operation": "assignment.assign",
        "assignment_ref": assignment_ref,
        "project_ref": project_ref,
        "ownership_version": ownership_version,
        "worker_name": worker_name,
    }


def validated_reopen_evidence(assignment: Mapping[str, Any], *, recipient: str = "") -> dict[str, Any]:
    """Fail closed unless evidence matches the active, currently selected owner."""
    proof = assignment.get("reopen_evidence")
    if not isinstance(proof, Mapping):
        return {}
    version = assignment.get("ownership_version")
    proof_version = proof.get("ownership_version")
    if type(version) is not int or version < 1 or type(proof_version) is not int:
        return {}
    worker = str(assignment.get("worker_name") or "").strip().lower()
    if (not worker or str(assignment.get("state") or "") not in ACTIVE_ASSIGNMENT_STATES
            or str(assignment.get("item_assignee") or "").strip().lower() != worker
            or (recipient and recipient.strip().lower() != worker)):
        return {}
    expected = make_reopen_evidence(
        assignment_ref=str(assignment.get("assignment_ref") or ""),
        project_ref=str(assignment.get("project_ref") or ""),
        ownership_version=version, worker_name=worker,
    )
    if not expected["assignment_ref"] or not expected["project_ref"]:
        return {}
    return expected if dict(proof) == expected else {}

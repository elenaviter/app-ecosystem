from __future__ import annotations

import time
from typing import Any, Mapping, Protocol

from ..contract.errors import DomainError
from ..contract.plan_nodes import parse_plan_node_ref
from ..contract.refs import make_ref


PLAN_REF_RESOLUTION_SCHEMA = "problem-board.plan-ref-resolution.v1"
DEFAULT_RESOLUTION_DEADLINE_SECONDS = 90.0
# Compatibility for callers that imported the original name. The value is a
# caller-side deadline, not evidence that the remote authority is unreachable.
DEFAULT_RESOLUTION_TIMEOUT_SECONDS = DEFAULT_RESOLUTION_DEADLINE_SECONDS


class PlanAuthorityField(Protocol):
    def enqueue_plan_ref_resolution(
        self,
        project_id: str,
        *,
        worker_name: str,
        item_ref: str,
        outbox_id: str = "",
    ) -> Mapping[str, Any]: ...

    def outbox_record(self, outbox_id: str) -> Mapping[str, Any] | None: ...


def require_plan_item(
    field: PlanAuthorityField,
    *,
    project_id: str,
    worker_name: str,
    work_ref: str,
    timeout_seconds: float = DEFAULT_RESOLUTION_DEADLINE_SECONDS,
) -> dict[str, Any] | None:
    """Resolve one work URI against PostgreSQL before a local side effect."""

    text = str(work_ref or "").strip()
    if not text:
        return None
    item_ref = parse_plan_node_ref(text).ref
    request = field.enqueue_plan_ref_resolution(
        project_id,
        worker_name=worker_name,
        item_ref=item_ref,
    )
    outbox_id = str(request.get("outbox_id") or "")
    return await_plan_item_resolution(
        field,
        project_id=project_id,
        work_ref=item_ref,
        outbox_id=outbox_id,
        timeout_seconds=timeout_seconds,
    )


def await_plan_item_resolution(
    field: PlanAuthorityField,
    *,
    project_id: str,
    work_ref: str,
    outbox_id: str,
    timeout_seconds: float = DEFAULT_RESOLUTION_DEADLINE_SECONDS,
) -> dict[str, Any]:
    """Wait for one already-enqueued plan resolution request."""

    item_ref = parse_plan_node_ref(str(work_ref or "").strip()).ref
    project_ref = make_ref("project", project_id)
    clean_outbox_id = str(outbox_id or "").strip()
    if not clean_outbox_id:
        raise DomainError(
            "field_plan_authority_outbox_id_required",
            "Waiting for plan authority requires the queued outbox identity.",
        )
    deadline_seconds = max(0.1, float(timeout_seconds))
    started_at = time.monotonic()
    deadline = started_at + deadline_seconds
    last_state = ""
    last_retry_count = 0
    last_error_code = ""
    while True:
        row = field.outbox_record(clean_outbox_id)
        if row is None:
            raise DomainError(
                "field_plan_authority_response_lost",
                "The plan authority response disappeared before it could be checked.",
                status=503,
                details={"project_ref": project_ref, "item_ref": item_ref},
            )
        state = str(row.get("state") or "")
        last_state = state
        last_retry_count = int(row.get("retry_count") or 0)
        last_error_code = str(row.get("last_error_code") or "")
        if state == "sent":
            proof = row.get("remote_result")
            proof = dict(proof) if isinstance(proof, Mapping) else {}
            if proof.get("schema") != PLAN_REF_RESOLUTION_SCHEMA:
                raise DomainError(
                    "field_plan_authority_response_invalid",
                    "The plan authority returned an invalid reference-resolution result.",
                    status=502,
                )
            if (
                str(proof.get("project_ref") or "") != project_ref
                or str(proof.get("item_ref") or "") != item_ref
            ):
                raise DomainError(
                    "field_plan_authority_response_mismatch",
                    "The plan authority answered for a different project or work item.",
                    status=502,
                )
            if not proof.get("generation_present"):
                raise DomainError(
                    "field_plan_authority_not_ready",
                    "The project plan has not been published to PostgreSQL yet.",
                    status=503,
                    details={"project_ref": project_ref},
                )
            if not proof.get("found"):
                if proof.get("error_code") == "work_plan_item_version_stale":
                    raise DomainError(
                        "work_plan_item_version_stale",
                        "The work item changed after this version was issued.",
                        status=409,
                        details={
                            "project_ref": project_ref,
                            "requested_work_ref": item_ref,
                            "current_work_ref": str(
                                proof.get("current_work_ref") or ""
                            ),
                            "identity_ref": str(
                                proof.get("identity_ref") or ""
                            ),
                        },
                    )
                raise DomainError(
                    "field_work_ref_not_found",
                    "The work reference does not exist in the project's current plan.",
                    status=404,
                    details={"project_ref": project_ref, "work_ref": item_ref},
                )
            return proof
        if state in {"refused", "ignored", "superseded"}:
            raise DomainError(
                "field_plan_authority_refused",
                "The governed plan authority refused the work-reference check.",
                status=403,
                details={
                    "project_ref": project_ref,
                    "work_ref": item_ref,
                    "reason": str(row.get("remote_disposition") or state),
                },
            )
        now = time.monotonic()
        if now >= deadline:
            break
        time.sleep(min(0.05, deadline - now))
    elapsed_seconds = round(max(0.0, time.monotonic() - started_at), 3)
    details: dict[str, Any] = {
        "project_ref": project_ref,
        "work_ref": item_ref,
        "outbox_id": clean_outbox_id,
        "deadline_seconds": deadline_seconds,
        "elapsed_seconds": elapsed_seconds,
        "last_observed_state": last_state or "unknown",
        "retry_count": last_retry_count,
    }
    if last_error_code:
        details["last_error_code"] = last_error_code
    raise DomainError(
        "field_plan_authority_deadline_exceeded",
        (
            f"The local {deadline_seconds:g}-second deadline expired while waiting "
            "for the governed plan authority to validate this work reference. "
            "The command did not receive an authority result and did not report "
            "success; the queued request may still finish after this deadline."
        ),
        status=504,
        details=details,
    )


__all__ = [
    "DEFAULT_RESOLUTION_DEADLINE_SECONDS",
    "DEFAULT_RESOLUTION_TIMEOUT_SECONDS",
    "PLAN_REF_RESOLUTION_SCHEMA",
    "await_plan_item_resolution",
    "require_plan_item",
]

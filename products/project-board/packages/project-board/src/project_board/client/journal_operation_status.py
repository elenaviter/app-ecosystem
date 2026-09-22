from __future__ import annotations

from typing import Any, Mapping

from ..contract.errors import DomainError
from ..contract.refs import make_ref
from .journal_operation_contract import (
    JOURNAL_INDEX_OPERATION_SCHEMA,
    JOURNAL_INDEX_STEPS,
    JournalOperationField,
    JournalOperationWorkspace,
    error_record,
    normalize_outbox_id,
    plan_outbox_id,
    require_operation_kind,
)
from .journal_receipt_identity import (
    has_receipt_identity_conflict,
    observe_receipt_identity,
    receipt_identity_conflict_error,
    superseding_entry_action,
)
from .operation_ledger import LocalOperationLedger
from .plan_authority import PLAN_REF_RESOLUTION_SCHEMA


class JournalIndexStatusReader:
    """Pure observation over workflow checkpoints and their authorities."""

    def __init__(
        self,
        *,
        field: JournalOperationField,
        workspace: JournalOperationWorkspace,
        ledger: LocalOperationLedger,
    ) -> None:
        self.field = field
        self.workspace = workspace
        self.ledger = ledger

    def status(self, operation_id: str, *, project_id: str = "") -> dict[str, Any]:
        record = self.ledger.read(operation_id)
        require_operation_kind(record)
        self._require_project(record, project_id=project_id)
        return self._status(record)

    def status_by_outbox(
        self,
        outbox_id: str,
        *,
        project_id: str,
        repository_journal_ref: str = "",
    ) -> dict[str, Any]:
        clean_outbox_id = normalize_outbox_id(outbox_id)
        record = self.ledger.find_by_step_value(
            "plan_validation", "outbox_id", clean_outbox_id
        )
        if record:
            require_operation_kind(record)
            self._require_project(record, project_id=project_id)
            return self._status(record)
        if not repository_journal_ref:
            raise DomainError(
                "journal_operation_source_required",
                "A historical outbox row needs its repository journal ref.",
                details={"outbox_id": clean_outbox_id},
            )
        project_ref = make_ref("project", project_id)
        entry = self.workspace.prepare_entry(
            project_ref=project_ref,
            repository_journal_ref=repository_journal_ref,
            readonly=True,
        )
        return self._historical_status(
            project_id=project_id,
            project_ref=project_ref,
            outbox_id=clean_outbox_id,
            entry=entry,
        )

    @staticmethod
    def _require_project(record: Mapping[str, Any], *, project_id: str) -> None:
        request = dict(record.get("request") or {})
        if project_id and str(request.get("project_id") or "") != project_id:
            raise DomainError(
                "field_operation_project_mismatch",
                "The journal operation belongs to another project.",
                status=403,
                details={"operation_id": str(record.get("operation_id") or "")},
            )

    def index_observation(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return self.workspace.indexed_entry_state(
                entry_ref=str(entry.get("entry_ref") or ""),
                content_hash=str(entry.get("content_hash") or ""),
            )
        except DomainError as exc:
            return {
                "state": "unavailable",
                "matches": False,
                "error": error_record(exc),
            }

    @staticmethod
    def receipt_observation(
        entry: Mapping[str, Any], receipt: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        return observe_receipt_identity(entry, receipt)

    @staticmethod
    def plan_observation(
        *,
        row: Mapping[str, Any] | None,
        project_ref: str,
        work_ref: str,
        outbox_id: str,
    ) -> dict[str, Any]:
        if not work_ref:
            return {
                "state": "not_required",
                "accepted": True,
                "reason": "journal_entry_has_no_work_ref",
            }
        if row is None:
            return {
                "state": "absent",
                "accepted": False,
                "outbox_id": outbox_id,
            }
        state = str(row.get("state") or "unknown")
        result = row.get("remote_result")
        proof = dict(result) if isinstance(result, Mapping) else {}
        accepted = bool(
            state == "sent"
            and proof.get("schema") == PLAN_REF_RESOLUTION_SCHEMA
            and proof.get("generation_present")
            and proof.get("found")
            and str(proof.get("project_ref") or "") == project_ref
            and str(proof.get("item_ref") or "") == work_ref
        )
        return {
            "state": "accepted" if accepted else state,
            "accepted": accepted,
            "outbox_id": outbox_id,
            "plan_revision": int(proof.get("plan_revision") or 0),
            "item_ref": str(proof.get("item_ref") or ""),
            "generation_present": bool(proof.get("generation_present")),
            "found": bool(proof.get("found")),
            "error_code": str(
                proof.get("error_code") or row.get("last_error_code") or ""
            ),
        }

    @staticmethod
    def _effect_observed(name: str, authority: Mapping[str, Any]) -> bool:
        state = str(authority.get("state") or "")
        if name == "plan_validation":
            return state not in {"absent", "not_required"}
        if name == "local_receipt":
            return state in {"matched", "identity_conflict"}
        return state in {"matched", "content_mismatch"}

    @staticmethod
    def _step_view(
        checkpoint: Mapping[str, Any], authority: Mapping[str, Any]
    ) -> dict[str, Any]:
        attempts = int(checkpoint.get("attempts") or 0)
        name = str(checkpoint.get("name") or "")
        checkpoint_state = str(checkpoint.get("state") or "pending")
        result = dict(checkpoint.get("result") or {})
        skipped = bool(result.get("skipped"))
        effect_observed = JournalIndexStatusReader._effect_observed(name, authority)
        identity_conflict = (
            name == "local_receipt"
            and has_receipt_identity_conflict(authority)
        )
        effect_satisfied = (
            bool(authority.get("matches"))
            if name in {"index_sync", "local_receipt"}
            else bool(authority.get("accepted"))
        )
        if identity_conflict:
            execution_state = "terminal_identity_conflict"
            ran = True if checkpoint_state == "completed" else None
        elif skipped:
            execution_state = "not_required"
            ran: bool | None = False
        elif checkpoint_state == "completed" and effect_satisfied:
            execution_state = "completed"
            ran = True
        elif checkpoint_state == "completed":
            execution_state = "completed_effect_now_missing"
            ran = True
        elif effect_observed:
            execution_state = "effect_observed_checkpoint_incomplete"
            ran = True
        elif attempts == 0:
            execution_state = "not_started"
            ran = False
        else:
            execution_state = "started_effect_not_observed"
            ran = None
        view = {
            "name": name,
            "ordinal": int(checkpoint.get("ordinal") or 0),
            "checkpoint_state": checkpoint_state,
            "execution_state": execution_state,
            "started": attempts > 0,
            "effect_observed": effect_observed,
            "effect_satisfied": effect_satisfied,
            "ran": ran,
            "did_not_run": (not ran) if isinstance(ran, bool) else None,
            "attempts": attempts,
            "completed": checkpoint.get("state") == "completed",
            "adopted": bool(checkpoint.get("adopted")),
            "repair_count": len(checkpoint.get("completion_history") or []),
            "completion_history": [
                dict(item)
                for item in checkpoint.get("completion_history") or []
                if isinstance(item, Mapping)
            ],
            "intent": dict(checkpoint.get("intent") or {}),
            "result": result,
            "error": dict(checkpoint.get("error") or {}),
            "authority": dict(authority),
        }
        if identity_conflict:
            view.update({"terminal": True, "resumable": False})
        terminal_completion = dict(checkpoint.get("terminal_completion") or {})
        if terminal_completion:
            view["terminal_completion"] = terminal_completion
        if identity_conflict:
            view["error"] = error_record(
                receipt_identity_conflict_error(authority)
            )
        return view

    def _status(self, record: Mapping[str, Any]) -> dict[str, Any]:
        request = dict(record.get("request") or {})
        entry = dict(request.get("entry") or {})
        operation_id = str(record.get("operation_id") or "")
        project_id = str(request.get("project_id") or "")
        project_ref = str(request.get("project_ref") or "")
        work_ref = str(entry.get("work_ref") or "")
        outbox_id = plan_outbox_id(operation_id) if work_ref else ""
        outbox = self.field.read_outbox_record(outbox_id) if outbox_id else None
        receipt = self.field.read_journal_receipt(
            project_id,
            entry_ref=str(entry.get("entry_ref") or ""),
        )
        authorities = {
            "index_sync": self.index_observation(entry),
            "plan_validation": self.plan_observation(
                row=outbox,
                project_ref=project_ref,
                work_ref=work_ref,
                outbox_id=outbox_id,
            ),
            "local_receipt": self.receipt_observation(entry, receipt),
        }
        steps = [
            self._step_view(step, authorities[str(step.get("name") or "")])
            for step in record.get("steps") or []
        ]
        checkpoint_state = str(record.get("state") or "active")
        receipt_authority = authorities["local_receipt"]
        identity_conflict = has_receipt_identity_conflict(receipt_authority)
        effect_complete = all(
            step["completed"] and step["effect_satisfied"] for step in steps
        )
        next_incomplete_step = (
            ""
            if identity_conflict
            else next(
                (
                    step["name"]
                    for step in steps
                    if not step["completed"] or not step["effect_satisfied"]
                ),
                "",
            )
        )
        conflict_error = (
            error_record(receipt_identity_conflict_error(receipt_authority))
            if identity_conflict
            else {}
        )
        return {
            "schema": JOURNAL_INDEX_OPERATION_SCHEMA,
            "operation_id": operation_id,
            "operation_identity": "durable_local_ledger",
            "state": (
                "failed"
                if identity_conflict
                else "incomplete"
                if checkpoint_state == "completed" and not effect_complete
                else checkpoint_state
            ),
            "checkpoint_state": checkpoint_state,
            "project_ref": project_ref,
            "worker_name": str(request.get("worker_name") or ""),
            "repository_journal_ref": str(
                request.get("repository_journal_ref") or ""
            ),
            "entry_ref": str(entry.get("entry_ref") or ""),
            "content_hash": str(entry.get("content_hash") or ""),
            "work_ref": work_ref,
            "plan_outbox_id": outbox_id,
            "steps": steps,
            "terminal": identity_conflict,
            "resumable": bool(next_incomplete_step) and not identity_conflict,
            "terminal_error": conflict_error,
            "next_action": (
                superseding_entry_action(receipt_authority)
                if identity_conflict
                else {}
            ),
            "next_incomplete_step": next_incomplete_step,
            "created_at": str(record.get("created_at") or ""),
            "updated_at": str(record.get("updated_at") or ""),
        }

    def _historical_status(
        self,
        *,
        project_id: str,
        project_ref: str,
        outbox_id: str,
        entry: Mapping[str, Any],
    ) -> dict[str, Any]:
        outbox = self.field.read_outbox_record(outbox_id)
        if outbox is None:
            raise DomainError(
                "field_outbox_record_not_found",
                "No outbox record has that id on this machine.",
                status=404,
                details={"outbox_id": outbox_id},
            )
        if str(outbox.get("project_ref") or "") != project_ref:
            raise DomainError(
                "field_operation_project_mismatch",
                "The journal operation belongs to another project.",
                status=403,
                details={"project_ref": project_ref, "outbox_id": outbox_id},
            )
        work_ref = str(entry.get("work_ref") or "")
        receipt = self.field.read_journal_receipt(
            project_id,
            entry_ref=str(entry.get("entry_ref") or ""),
        )
        authorities = (
            self.index_observation(entry),
            self.plan_observation(
                row=outbox,
                project_ref=project_ref,
                work_ref=work_ref,
                outbox_id=outbox_id,
            ),
            self.receipt_observation(entry, receipt),
        )
        steps = []
        for ordinal, (name, authority) in enumerate(
            zip(JOURNAL_INDEX_STEPS, authorities), start=1
        ):
            completed = (
                bool(authority.get("matches"))
                if name != "plan_validation"
                else bool(authority.get("accepted"))
            )
            steps.append(
                {
                    "name": name,
                    "ordinal": ordinal,
                    "checkpoint_state": "ledger_absent",
                    "execution_state": "historical_execution_unknown",
                    "started": None,
                    "effect_observed": self._effect_observed(name, authority),
                    "effect_satisfied": bool(completed),
                    "ran": None,
                    "did_not_run": None,
                    "attempts": None,
                    "completed": bool(completed),
                    "adopted": False,
                    "repair_count": 0,
                    "completion_history": [],
                    "intent": {},
                    "result": {},
                    "error": {},
                    "authority": authority,
                }
            )
        receipt_authority = authorities[2]
        identity_conflict = has_receipt_identity_conflict(receipt_authority)
        next_incomplete_step = (
            ""
            if identity_conflict
            else next(
                (step["name"] for step in steps if not step["completed"]), ""
            )
        )
        return {
            "schema": JOURNAL_INDEX_OPERATION_SCHEMA,
            "operation_id": "",
            "operation_identity": "historical_without_workflow_ledger",
            "state": (
                "failed"
                if identity_conflict
                else "completed"
                if all(step["completed"] for step in steps)
                else "incomplete"
            ),
            "checkpoint_state": "ledger_absent",
            "project_ref": project_ref,
            "worker_name": str(outbox.get("worker_name") or ""),
            "repository_journal_ref": str(
                entry.get("repository_journal_ref") or ""
            ),
            "entry_ref": str(entry.get("entry_ref") or ""),
            "content_hash": str(entry.get("content_hash") or ""),
            "work_ref": work_ref,
            "plan_outbox_id": outbox_id,
            "steps": steps,
            "terminal": identity_conflict,
            "resumable": bool(next_incomplete_step) and not identity_conflict,
            "terminal_error": (
                error_record(receipt_identity_conflict_error(receipt_authority))
                if identity_conflict
                else {}
            ),
            "next_action": (
                superseding_entry_action(receipt_authority)
                if identity_conflict
                else {}
            ),
            "next_incomplete_step": next_incomplete_step,
            "created_at": str(outbox.get("created_at") or ""),
            "updated_at": str(
                outbox.get("sent_at") or outbox.get("created_at") or ""
            ),
        }


__all__ = ["JournalIndexStatusReader"]

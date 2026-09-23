from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, NoReturn

from ..contract.errors import DomainError
from ..contract.refs import make_ref
from .io import content_hash
from .journal_operation_contract import (
    JOURNAL_INDEX_OPERATION_KIND,
    JOURNAL_INDEX_OPERATION_SCHEMA,
    JOURNAL_INDEX_STEPS,
    JournalOperationField,
    JournalOperationWorkspace,
    error_record,
    plan_outbox_id,
    require_operation_kind,
)
from .journal_operation_status import JournalIndexStatusReader
from .journal_receipt_identity import (
    has_receipt_identity_conflict,
    receipt_identity_conflict_error,
)
from .operation_ledger import LocalOperationLedger
from .plan_authority import await_plan_item_resolution


class JournalIndexWorkflow:
    """Crash-resumable orchestration for journal indexing's three authorities."""

    def __init__(
        self,
        *,
        field: JournalOperationField,
        workspace: JournalOperationWorkspace,
        operation_root: str | Path,
        resolution_timeout_seconds: float = 90.0,
    ) -> None:
        self.field = field
        self.workspace = workspace
        self.ledger = LocalOperationLedger(operation_root)
        self.status_reader = JournalIndexStatusReader(
            field=field,
            workspace=workspace,
            ledger=self.ledger,
        )
        self.resolution_timeout_seconds = float(resolution_timeout_seconds)

    def start(
        self,
        *,
        project_id: str,
        worker_name: str,
        repository_journal_ref: str,
    ) -> dict[str, Any]:
        project = dict(self.field.read_project(project_id))
        worker = dict(self.field.read_worker(worker_name))
        if worker.get("pool_status") != "active":
            raise DomainError(
                "field_worker_limbo",
                "A worker in limbo cannot index a journal entry.",
                status=403,
            )
        project_ref = str(project.get("project_ref") or make_ref("project", project_id))
        entry = self.workspace.prepare_entry(
            project_ref=project_ref,
            repository_journal_ref=repository_journal_ref,
        )
        request = {
            "project_id": project_id,
            "project_ref": project_ref,
            "worker_name": str(worker.get("worker_name") or worker_name),
            "repository_journal_ref": str(entry["repository_journal_ref"]),
            "entry": {
                key: value
                for key, value in entry.items()
                if key != "local_path"
            },
        }
        record = self.ledger.ensure(
            kind=JOURNAL_INDEX_OPERATION_KIND,
            request_hash=content_hash(request),
            request=request,
            steps=JOURNAL_INDEX_STEPS,
        )
        return self._run(record, entry=entry)

    def resume(
        self,
        operation_id: str,
        *,
        worker_name: str = "",
    ) -> dict[str, Any]:
        record = self.ledger.read(operation_id)
        require_operation_kind(record)
        request = dict(record.get("request") or {})
        expected_worker = str(request.get("worker_name") or "")
        if worker_name and worker_name != expected_worker:
            raise DomainError(
                "field_operation_worker_mismatch",
                "Only the worker that started this local operation may resume it.",
                status=403,
                details={
                    "operation_id": operation_id,
                    "worker_name": worker_name,
                    "expected_worker_name": expected_worker,
                },
            )
        expected_entry = dict(request.get("entry") or {})
        with self.ledger.execution(operation_id):
            self._require_receipt_identity_available(
                operation_id,
                project_id=str(request.get("project_id") or ""),
                entry=expected_entry,
            )
        entry = self.workspace.prepare_entry(
            project_ref=str(request.get("project_ref") or ""),
            repository_journal_ref=str(request.get("repository_journal_ref") or ""),
            readonly=True,
        )
        if (
            str(entry.get("entry_ref") or "")
            != str(expected_entry.get("entry_ref") or "")
            or str(entry.get("content_hash") or "")
            != str(expected_entry.get("content_hash") or "")
        ):
            raise DomainError(
                "journal_operation_source_changed",
                "The journal file changed after this operation started.",
                status=409,
                details={
                    "operation_id": operation_id,
                    "repository_journal_ref": request.get(
                        "repository_journal_ref", ""
                    ),
                    "expected_content_hash": expected_entry.get("content_hash", ""),
                    "observed_content_hash": entry.get("content_hash", ""),
                },
            )
        return self._run(record, entry=entry)

    def status(self, operation_id: str, *, project_id: str = "") -> dict[str, Any]:
        return self.status_reader.status(operation_id, project_id=project_id)

    def status_by_outbox(
        self,
        outbox_id: str,
        *,
        project_id: str,
        repository_journal_ref: str = "",
    ) -> dict[str, Any]:
        return self.status_reader.status_by_outbox(
            outbox_id,
            project_id=project_id,
            repository_journal_ref=repository_journal_ref,
        )

    def _run(
        self,
        record: Mapping[str, Any],
        *,
        entry: Mapping[str, Any],
    ) -> dict[str, Any]:
        operation_id = str(record.get("operation_id") or "")
        request = dict(record.get("request") or {})
        project_id = str(request.get("project_id") or "")
        project_ref = str(request.get("project_ref") or "")
        worker_name = str(request.get("worker_name") or "")
        work_ref = str(entry.get("work_ref") or "")
        with self.ledger.execution(operation_id):
            self._require_receipt_identity_available(
                operation_id,
                project_id=project_id,
                entry=entry,
            )
            self._run_index_step(operation_id, entry=entry)
            self._run_plan_step(
                operation_id,
                project_id=project_id,
                project_ref=project_ref,
                worker_name=worker_name,
                work_ref=work_ref,
            )
            self._run_receipt_step(
                operation_id,
                project_id=project_id,
                worker_name=worker_name,
                entry=entry,
            )
        result = self.status(operation_id)
        result["entry"] = dict(entry)
        receipt = self.field.read_journal_receipt(
            project_id,
            entry_ref=str(entry.get("entry_ref") or ""),
        )
        result["receipt"] = dict(receipt or {})
        return result

    def _run_index_step(
        self, operation_id: str, *, entry: Mapping[str, Any]
    ) -> None:
        observed = self.status_reader.index_observation(entry)
        completed = self._completed(operation_id, "index_sync")
        if completed and observed.get("matches"):
            return
        if observed.get("matches"):
            self.ledger.complete_step(
                operation_id,
                "index_sync",
                result={"indexed_entries": None},
                adopted=True,
            )
            return
        self._begin_or_reopen_step(
            operation_id,
            "index_sync",
            intent={
                "entry_ref": entry.get("entry_ref", ""),
                "content_hash": entry.get("content_hash", ""),
            },
            completed=completed,
            authority_state=str(observed.get("state") or "unknown"),
        )
        try:
            indexed_entries = self.workspace.rebuild_index()
            observed = self.status_reader.index_observation(entry)
            if not observed.get("matches"):
                raise DomainError(
                    "journal_index_effect_missing",
                    "The index rebuild did not store the requested journal version.",
                    status=503,
                    details={"authority": observed},
                )
        except DomainError as exc:
            self.ledger.fail_step(
                operation_id, "index_sync", error=error_record(exc)
            )
            raise
        self.ledger.complete_step(
            operation_id,
            "index_sync",
            result={"indexed_entries": indexed_entries},
        )

    def _run_plan_step(
        self,
        operation_id: str,
        *,
        project_id: str,
        project_ref: str,
        worker_name: str,
        work_ref: str,
    ) -> None:
        completed = self._completed(operation_id, "plan_validation")
        if not work_ref:
            if completed:
                return
            self.ledger.complete_step(
                operation_id,
                "plan_validation",
                result={
                    "skipped": True,
                    "reason": "journal_entry_has_no_work_ref",
                },
            )
            return
        outbox_id = plan_outbox_id(operation_id)
        existing = self.field.read_outbox_record(outbox_id)
        observed = self.status_reader.plan_observation(
            row=existing,
            project_ref=project_ref,
            work_ref=work_ref,
            outbox_id=outbox_id,
        )
        if completed and observed.get("accepted"):
            return
        if observed.get("accepted"):
            self.ledger.complete_step(
                operation_id,
                "plan_validation",
                result={"outbox_id": outbox_id},
                adopted=True,
            )
            return
        self._begin_or_reopen_step(
            operation_id,
            "plan_validation",
            intent={"outbox_id": outbox_id, "item_ref": work_ref},
            completed=completed,
            authority_state=str(observed.get("state") or "unknown"),
        )
        if existing is None:
            self.field.enqueue_plan_ref_resolution(
                project_id,
                worker_name=worker_name,
                item_ref=work_ref,
                outbox_id=outbox_id,
            )
        try:
            await_plan_item_resolution(
                self.field,
                project_id=project_id,
                work_ref=work_ref,
                outbox_id=outbox_id,
                timeout_seconds=self.resolution_timeout_seconds,
            )
        except DomainError as exc:
            operation = (
                self.ledger.wait_step
                if exc.code == "field_plan_authority_deadline_exceeded"
                else self.ledger.fail_step
            )
            operation(operation_id, "plan_validation", error=error_record(exc))
            raise
        self.ledger.complete_step(
            operation_id,
            "plan_validation",
            result={"outbox_id": outbox_id},
            adopted=existing is not None,
        )

    def _run_receipt_step(
        self,
        operation_id: str,
        *,
        project_id: str,
        worker_name: str,
        entry: Mapping[str, Any],
    ) -> None:
        completed = self._completed(operation_id, "local_receipt")
        existing = self.field.read_journal_receipt(
            project_id,
            entry_ref=str(entry.get("entry_ref") or ""),
        )
        observed = self.status_reader.receipt_observation(entry, existing)
        if completed and observed.get("matches"):
            return
        if observed.get("matches"):
            self.ledger.complete_step(
                operation_id,
                "local_receipt",
                result={"entry_ref": str(entry.get("entry_ref") or "")},
                adopted=True,
            )
            return
        if existing is not None:
            self._raise_receipt_identity_conflict(
                operation_id,
                observation=observed,
            )
        self._begin_or_reopen_step(
            operation_id,
            "local_receipt",
            intent={"entry_ref": entry.get("entry_ref", "")},
            completed=completed,
            authority_state=str(observed.get("state") or "unknown"),
        )
        try:
            receipt = self.field.record_journal_receipt(
                project_id,
                worker_name=worker_name,
                entry=entry,
            )
        except DomainError as exc:
            self.ledger.fail_step(
                operation_id, "local_receipt", error=error_record(exc)
            )
            raise
        self.ledger.complete_step(
            operation_id,
            "local_receipt",
            result={"entry_ref": str(receipt.get("entry_ref") or "")},
        )

    def _require_receipt_identity_available(
        self,
        operation_id: str,
        *,
        project_id: str,
        entry: Mapping[str, Any],
    ) -> None:
        existing = self.field.read_journal_receipt(
            project_id,
            entry_ref=str(entry.get("entry_ref") or ""),
        )
        observation = self.status_reader.receipt_observation(entry, existing)
        if has_receipt_identity_conflict(observation):
            self._raise_receipt_identity_conflict(
                operation_id,
                observation=observation,
            )

    def _raise_receipt_identity_conflict(
        self,
        operation_id: str,
        *,
        observation: Mapping[str, Any],
    ) -> NoReturn:
        exc = receipt_identity_conflict_error(
            observation,
            operation_id=operation_id,
        )
        record = self.ledger.read(operation_id)
        step = next(
            row
            for row in record.get("steps") or []
            if str(row.get("name") or "") == "local_receipt"
        )
        state = str(step.get("state") or "pending")
        if state == "completed":
            self.ledger.terminate_completed_step(
                operation_id,
                "local_receipt",
                error=error_record(exc),
                reason={
                    "code": "authority_identity_conflict",
                    "authority_state": str(observation.get("state") or ""),
                },
            )
        elif state != "failed":
            if state == "pending":
                self.ledger.begin_step(
                    operation_id,
                    "local_receipt",
                    intent={
                        "entry_ref": str(
                            observation.get("expected_entry_ref") or ""
                        )
                    },
                )
            self.ledger.fail_step(
                operation_id,
                "local_receipt",
                error=error_record(exc),
            )
        raise exc

    def _completed(self, operation_id: str, step_name: str) -> bool:
        record = self.ledger.read(operation_id)
        return any(
            str(step.get("name") or "") == step_name
            and str(step.get("state") or "") == "completed"
            for step in record.get("steps") or []
        )

    def _begin_or_reopen_step(
        self,
        operation_id: str,
        step_name: str,
        *,
        intent: Mapping[str, Any],
        completed: bool,
        authority_state: str,
    ) -> None:
        if completed:
            self.ledger.reopen_step(
                operation_id,
                step_name,
                intent=intent,
                reason={
                    "code": "authority_effect_missing",
                    "authority_state": authority_state,
                },
            )
            return
        self.ledger.begin_step(operation_id, step_name, intent=intent)


__all__ = [
    "JOURNAL_INDEX_OPERATION_KIND",
    "JOURNAL_INDEX_OPERATION_SCHEMA",
    "JOURNAL_INDEX_STEPS",
    "JournalIndexWorkflow",
]

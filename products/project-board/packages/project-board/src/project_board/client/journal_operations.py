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
import subprocess

from .journal_history import ContentChain, published_chain
from .journal_operation_status import JournalIndexStatusReader
from .journal_receipt_identity import (
    has_receipt_identity_conflict,
    receipt_identity_conflict_error,
    update_stale_error,
)


def _entry_publication(entry: Mapping[str, Any]) -> tuple[ContentChain | None, dict[str, Any]]:
    """The entry's published chain on its clone's HEAD and the version equal to its content (W1031).

    Returns ``(None, {})`` when the file is not in a readable Git clone; an
    unflagged index keeps working there, as before. ``--update`` requires a
    published version.
    """

    local_path = Path(str(entry.get("local_path") or ""))
    if not local_path.is_file():
        return None, {}
    top = subprocess.run(["git", "-C", str(local_path.parent), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True, timeout=60)
    if top.returncode != 0:
        return None, {}
    root = Path(top.stdout.strip())
    relative = local_path.resolve().relative_to(root.resolve()).as_posix()
    try:
        chain = published_chain(root, relative, ref="HEAD")
    except DomainError:
        return None, {}
    wanted = str(entry.get("content_hash") or "")
    for version in reversed(chain.versions):
        if version.content_hash == wanted:
            return chain, version.summary()
    return chain, {}
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
        update: bool = False,
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
        warnings: list[dict[str, Any]] = []
        transition = self._transition(project_id, entry=entry, update=update, warnings=warnings)
        if transition:
            # W1031 R1: the verified target commit, the precondition and the mode are
            # part of the immutable request, so the canonical request hash (the
            # operation id) follows publication history: a retry replays, while the
            # same bytes published again at a later commit are a new operation.
            request["transition"] = transition
        record = self.ledger.ensure(
            kind=JOURNAL_INDEX_OPERATION_KIND,
            request_hash=content_hash(request),
            request=request,
            steps=JOURNAL_INDEX_STEPS,
        )
        result = self._run(record, entry=entry)
        if warnings:
            result["warnings"] = warnings
        return result

    def _transition(
        self,
        project_id: str,
        *,
        entry: Mapping[str, Any],
        update: bool,
        warnings: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """W1031: mode, precondition and verified target for this run; {} keeps the old request shape."""

        existing = self.field.read_journal_receipt(
            project_id, entry_ref=str(entry.get("entry_ref") or "")
        )
        chain, target = _entry_publication(entry)
        if update and not target:
            raise DomainError(
                "journal_entry_not_published",
                "--update indexes a published version: merge the amended entry, fetch and "
                "fast-forward the journal clone, then run journal-index --update again.",
                status=409,
                details={
                    "repository_journal_ref": str(entry.get("repository_journal_ref") or ""),
                    "content_hash": str(entry.get("content_hash") or ""),
                    "history_complete": bool(chain.complete) if chain else False,
                },
            )
        if existing is None:
            if update:
                mode = "update"
            elif chain is not None and target and len(chain.versions) > 1:
                mode = "materialize"
            elif target:
                mode = "index"
            else:
                return {}
            precondition: dict[str, Any] = {"kind": "absent"}
        elif update:
            mode = "update"
            precondition = {
                "kind": "hash",
                "previous_content_hash": str(existing.get("content_hash") or ""),
                "previous_commit": str(existing.get("commit") or ""),
            }
        else:
            return {}
        if update and target and target.get("intent") != "update" and target.get("intent") != "first":
            warnings.append({
                "code": "journal_update_intent_unrecorded",
                "message": (
                    "The indexed version has no Journal-Update and Journal-Worker trailers; "
                    "Git history will show its intent and worker as unknown."
                ),
                "commit": str(target.get("commit") or ""),
            })
        return {
            "mode": mode,
            "precondition": precondition,
            "target_commit": str(target.get("commit") or ""),
            "target_content_hash": str(entry.get("content_hash") or ""),
        }

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
                transition=dict(request.get("transition") or {}),
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
        transition = dict(request.get("transition") or {})
        with self.ledger.execution(operation_id):
            self._require_receipt_identity_available(
                operation_id,
                project_id=project_id,
                entry=entry,
                transition=transition,
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
                transition=transition,
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
        transition: Mapping[str, Any] | None = None,
    ) -> None:
        transition = dict(transition or {})
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
            self._check_transition(operation_id, observation=observed, existing=existing,
                                   transition=transition)
        elif str(dict(transition.get("precondition") or {}).get("kind") or "") == "hash":
            self._fail_receipt_step(operation_id, observation=observed, exc=update_stale_error(
                observed,
                previous_content_hash=str(dict(transition["precondition"]).get("previous_content_hash") or ""),
                operation_id=operation_id,
            ))
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
                **(
                    {
                        "mode": str(transition.get("mode") or "index"),
                        "precondition": dict(transition.get("precondition") or {}),
                        "target_commit": str(transition.get("target_commit") or ""),
                        "operation_id": operation_id,
                    }
                    if transition
                    else {}
                ),
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
        transition: Mapping[str, Any] | None = None,
    ) -> None:
        existing = self.field.read_journal_receipt(
            project_id,
            entry_ref=str(entry.get("entry_ref") or ""),
        )
        observation = self.status_reader.receipt_observation(entry, existing)
        if has_receipt_identity_conflict(observation):
            self._check_transition(operation_id, observation=observation, existing=existing,
                                   transition=dict(transition or {}))

    def _check_transition(
        self,
        operation_id: str,
        *,
        observation: Mapping[str, Any],
        existing: Mapping[str, Any] | None,
        transition: Mapping[str, Any],
    ) -> None:
        """W1031 R1, the same rule the store applies under its lock.

        Content that differs from the receipt is accepted only by an update whose
        fixed identity (entry_ref, repository_journal_ref) matches and whose
        precondition names the receipt's current hash. A matching identity with a
        moved receipt is stale; anything else is the unchanged conflict.
        """

        if existing is None or not has_receipt_identity_conflict(observation):
            return
        fixed = (
            str(observation.get("entry_ref") or "") == str(observation.get("expected_entry_ref") or "")
            and str(observation.get("repository_journal_ref") or "")
            == str(observation.get("expected_repository_journal_ref") or "")
        )
        precondition = dict(transition.get("precondition") or {})
        if str(transition.get("mode") or "") == "update" and fixed:
            previous = str(precondition.get("previous_content_hash") or "")
            if precondition.get("kind") == "hash" and str(existing.get("content_hash") or "") == previous:
                return
            self._fail_receipt_step(operation_id, observation=observation, exc=update_stale_error(
                observation, previous_content_hash=previous, operation_id=operation_id,
            ))
        self._raise_receipt_identity_conflict(operation_id, observation=observation)

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
        self._fail_receipt_step(operation_id, observation=observation, exc=exc)

    def _fail_receipt_step(
        self,
        operation_id: str,
        *,
        observation: Mapping[str, Any],
        exc: DomainError,
    ) -> NoReturn:
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

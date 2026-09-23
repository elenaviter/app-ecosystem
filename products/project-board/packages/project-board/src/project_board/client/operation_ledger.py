from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from ..contract.errors import DomainError
from .io import atomic_write_json, component, exclusive_lock, read_json, utc_now


LOCAL_OPERATION_SCHEMA = "problem-board.local-operation.v1"
STEP_STATES = frozenset({"pending", "running", "waiting", "completed", "failed"})


def operation_id_for(kind: str, request_hash: str) -> str:
    clean_kind = component(kind, field="operation_kind")
    clean_hash = component(request_hash, field="request_hash")
    return component(f"{clean_kind}_{clean_hash[:32]}", field="operation_id")


class LocalOperationLedger:
    """Durable local workflow state, separate from every step's own authority.

    The ledger says which step the workflow reached. It does not copy a remote
    outbox row, a search-index document, or a local receipt. Callers probe those
    authorities before continuing so a crash after an effect but before its
    checkpoint adopts the effect instead of performing it twice.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    def _path(self, operation_id: str) -> Path:
        return self.root / f"{component(operation_id, field='operation_id')}.json"

    def _record_lock(self, operation_id: str) -> Path:
        return self.root / "locks" / f"{component(operation_id, field='operation_id')}.lock"

    def _execution_lock(self, operation_id: str) -> Path:
        return self.root / "locks" / f"{component(operation_id, field='operation_id')}.execute.lock"

    def ensure(
        self,
        *,
        kind: str,
        request_hash: str,
        request: Mapping[str, Any],
        steps: Sequence[str],
    ) -> dict[str, Any]:
        operation_id = operation_id_for(kind, request_hash)
        clean_steps = [component(step, field="step") for step in steps]
        if not clean_steps or len(set(clean_steps)) != len(clean_steps):
            raise DomainError(
                "field_operation_steps_invalid",
                "A local operation needs distinct ordered steps.",
            )
        with exclusive_lock(self._record_lock(operation_id)):
            existing = read_json(self._path(operation_id), required=False)
            if existing:
                if (
                    existing.get("schema") != LOCAL_OPERATION_SCHEMA
                    or str(existing.get("kind") or "") != kind
                    or str(existing.get("request_hash") or "") != request_hash
                    or dict(existing.get("request") or {}) != dict(request)
                    or [str(row.get("name") or "") for row in existing.get("steps") or []]
                    != clean_steps
                ):
                    raise DomainError(
                        "field_operation_identity_conflict",
                        "The local operation identity already describes different work.",
                        status=409,
                        details={"operation_id": operation_id},
                    )
                return dict(existing)
            now = utc_now()
            record = {
                "schema": LOCAL_OPERATION_SCHEMA,
                "operation_id": operation_id,
                "kind": kind,
                "request_hash": request_hash,
                "request": dict(request),
                "state": "active",
                "steps": [
                    {"name": step, "ordinal": ordinal, "state": "pending"}
                    for ordinal, step in enumerate(clean_steps, start=1)
                ],
                "created_at": now,
                "updated_at": now,
            }
            atomic_write_json(self._path(operation_id), record)
            return record

    def read(self, operation_id: str) -> dict[str, Any]:
        record = read_json(self._path(operation_id))
        if record.get("schema") != LOCAL_OPERATION_SCHEMA:
            raise DomainError(
                "field_operation_schema_invalid",
                "The local operation record uses an unsupported schema.",
                details={"operation_id": operation_id},
            )
        return record

    @contextmanager
    def execution(self, operation_id: str) -> Iterator[None]:
        with exclusive_lock(self._execution_lock(operation_id)):
            yield

    def begin_step(
        self,
        operation_id: str,
        step_name: str,
        *,
        intent: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._update_step(
            operation_id,
            step_name,
            state="running",
            intent=dict(intent or {}),
            started=True,
        )

    def complete_step(
        self,
        operation_id: str,
        step_name: str,
        *,
        result: Mapping[str, Any] | None = None,
        adopted: bool = False,
    ) -> dict[str, Any]:
        return self._update_step(
            operation_id,
            step_name,
            state="completed",
            result=dict(result or {}),
            adopted=bool(adopted),
        )

    def reopen_step(
        self,
        operation_id: str,
        step_name: str,
        *,
        intent: Mapping[str, Any] | None = None,
        reason: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Reopen a completed checkpoint whose authoritative effect disappeared."""

        return self._update_step(
            operation_id,
            step_name,
            state="running",
            intent=dict(intent or {}),
            started=True,
            reopen_completed=True,
            reopen_reason=dict(reason),
        )

    def wait_step(
        self,
        operation_id: str,
        step_name: str,
        *,
        error: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self._update_step(
            operation_id,
            step_name,
            state="waiting",
            error=dict(error),
        )

    def fail_step(
        self,
        operation_id: str,
        step_name: str,
        *,
        error: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self._update_step(
            operation_id,
            step_name,
            state="failed",
            error=dict(error),
        )

    def terminate_completed_step(
        self,
        operation_id: str,
        step_name: str,
        *,
        error: Mapping[str, Any],
        reason: Mapping[str, Any],
    ) -> dict[str, Any]:
        """End a completed checkpoint whose authority now has a terminal conflict."""

        return self._update_step(
            operation_id,
            step_name,
            state="failed",
            error=dict(error),
            started=True,
            terminate_completed=True,
            termination_reason=dict(reason),
        )

    def find_by_step_value(self, step_name: str, field: str, value: str) -> dict[str, Any] | None:
        clean_step = component(step_name, field="step")
        clean_field = component(field, field="step_field")
        expected = str(value or "")
        if not self.root.is_dir():
            return None
        for path in sorted(self.root.glob("*.json"), reverse=True):
            record = read_json(path, required=False)
            if record.get("schema") != LOCAL_OPERATION_SCHEMA:
                continue
            for step in record.get("steps") or []:
                if str(step.get("name") or "") != clean_step:
                    continue
                intent = step.get("intent") if isinstance(step.get("intent"), Mapping) else {}
                result = step.get("result") if isinstance(step.get("result"), Mapping) else {}
                if str(intent.get(clean_field) or result.get(clean_field) or "") == expected:
                    return record
        return None

    def _update_step(
        self,
        operation_id: str,
        step_name: str,
        *,
        state: str,
        intent: Mapping[str, Any] | None = None,
        result: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
        started: bool = False,
        adopted: bool = False,
        reopen_completed: bool = False,
        reopen_reason: Mapping[str, Any] | None = None,
        terminate_completed: bool = False,
        termination_reason: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if state not in STEP_STATES:
            raise DomainError("field_operation_step_state_invalid", "The operation step state is invalid.")
        clean_step = component(step_name, field="step")
        with exclusive_lock(self._record_lock(operation_id)):
            record = self.read(operation_id)
            rows = [dict(row) for row in record.get("steps") or []]
            row = next((item for item in rows if item.get("name") == clean_step), None)
            if row is None:
                raise DomainError(
                    "field_operation_step_unknown",
                    "The local operation has no such step.",
                    details={"operation_id": operation_id, "step": clean_step},
                )
            checkpoint_was_completed = row.get("state") == "completed"
            if (
                checkpoint_was_completed
                and state != "completed"
                and not reopen_completed
                and not terminate_completed
            ):
                raise DomainError(
                    "field_operation_step_completed",
                    "A completed local operation step cannot move backward.",
                    status=409,
                    details={"operation_id": operation_id, "step": clean_step},
                )
            now = utc_now()
            if reopen_completed:
                if not checkpoint_was_completed or state != "running":
                    raise DomainError(
                        "field_operation_step_reopen_invalid",
                        "Only a completed local operation step can be reopened.",
                        status=409,
                        details={"operation_id": operation_id, "step": clean_step},
                    )
                history = [
                    dict(item)
                    for item in row.get("completion_history") or []
                    if isinstance(item, Mapping)
                ]
                history.append(
                    {
                        "completed_at": str(row.get("completed_at") or ""),
                        "intent": dict(row.get("intent") or {}),
                        "result": dict(row.get("result") or {}),
                        "adopted": bool(row.get("adopted")),
                        "invalidated_at": now,
                        "reason": dict(reopen_reason or {}),
                    }
                )
                row["completion_history"] = history
                row.pop("completed_at", None)
                row.pop("result", None)
                row.pop("adopted", None)
            if terminate_completed:
                if not checkpoint_was_completed or state != "failed":
                    raise DomainError(
                        "field_operation_step_termination_invalid",
                        "Only a completed local operation step can end in a terminal conflict.",
                        status=409,
                        details={"operation_id": operation_id, "step": clean_step},
                    )
                row["terminal_completion"] = {
                    "completed_at": str(row.get("completed_at") or ""),
                    "intent": dict(row.get("intent") or {}),
                    "result": dict(row.get("result") or {}),
                    "adopted": bool(row.get("adopted")),
                    "invalidated_at": now,
                    "reason": dict(termination_reason or {}),
                }
                row.pop("completed_at", None)
                row.pop("result", None)
                row.pop("adopted", None)
            row["state"] = state
            row["updated_at"] = now
            if started:
                row.setdefault("started_at", now)
                row["attempts"] = int(row.get("attempts") or 0) + 1
            if intent is not None:
                row["intent"] = dict(intent)
            if result is not None:
                row["result"] = dict(result)
            if error is not None:
                row["error"] = dict(error)
            elif state in {"running", "completed"}:
                row.pop("error", None)
            if state == "completed":
                row["completed_at"] = now
                row["adopted"] = adopted
            record["steps"] = rows
            states = [str(item.get("state") or "pending") for item in rows]
            record["state"] = (
                "failed"
                if "failed" in states
                else "completed"
                if states and all(value == "completed" for value in states)
                else "active"
            )
            record["updated_at"] = now
            if record["state"] == "completed":
                record["completed_at"] = now
            else:
                record.pop("completed_at", None)
            atomic_write_json(self._path(operation_id), record)
            return record


__all__ = [
    "LOCAL_OPERATION_SCHEMA",
    "LocalOperationLedger",
    "operation_id_for",
]

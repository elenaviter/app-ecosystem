from __future__ import annotations

import hashlib
from typing import Any, Mapping, Protocol

from ..contract.errors import DomainError


JOURNAL_INDEX_OPERATION_SCHEMA = "problem-board.journal-index-operation-status.v1"
JOURNAL_INDEX_OPERATION_KIND = "journal-index"
JOURNAL_INDEX_STEPS = ("index_sync", "plan_validation", "local_receipt")


class JournalOperationField(Protocol):
    def read_project(self, project_id: str) -> Mapping[str, Any]: ...

    def read_worker(self, worker_name: str) -> Mapping[str, Any]: ...

    def enqueue_plan_ref_resolution(
        self,
        project_id: str,
        *,
        worker_name: str,
        item_ref: str,
        outbox_id: str = "",
    ) -> Mapping[str, Any]: ...

    def outbox_record(self, outbox_id: str) -> Mapping[str, Any] | None: ...

    def read_outbox_record(self, outbox_id: str) -> Mapping[str, Any] | None: ...

    def record_journal_receipt(
        self,
        project_id: str,
        *,
        worker_name: str,
        entry: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...

    def read_journal_receipt(
        self,
        project_id: str,
        *,
        entry_ref: str,
    ) -> Mapping[str, Any] | None: ...


class JournalOperationWorkspace(Protocol):
    def prepare_entry(
        self,
        *,
        project_ref: str,
        repository_journal_ref: str,
        readonly: bool = False,
    ) -> dict[str, Any]: ...

    def rebuild_index(self) -> int: ...

    def indexed_entry_state(
        self,
        *,
        entry_ref: str,
        content_hash: str,
    ) -> dict[str, Any]: ...


def plan_outbox_id(operation_id: str) -> str:
    digest = hashlib.sha256(
        f"{operation_id}:plan_validation".encode("utf-8")
    ).hexdigest()
    return f"outbox_{digest[:32]}"


def normalize_outbox_id(value: str) -> str:
    text = str(value or "").strip()
    if text and not text.startswith("outbox_"):
        return f"outbox_{text}"
    return text


def error_record(exc: DomainError) -> dict[str, Any]:
    return {
        "code": exc.code,
        "message": str(exc),
        "status": exc.status,
        "details": dict(exc.details),
    }


def require_operation_kind(record: Mapping[str, Any]) -> None:
    if str(record.get("kind") or "") != JOURNAL_INDEX_OPERATION_KIND:
        raise DomainError(
            "field_operation_kind_mismatch",
            "The local operation is not a journal-index operation.",
            status=409,
        )


__all__ = [
    "JOURNAL_INDEX_OPERATION_KIND",
    "JOURNAL_INDEX_OPERATION_SCHEMA",
    "JOURNAL_INDEX_STEPS",
    "JournalOperationField",
    "JournalOperationWorkspace",
    "error_record",
    "normalize_outbox_id",
    "plan_outbox_id",
    "require_operation_kind",
]

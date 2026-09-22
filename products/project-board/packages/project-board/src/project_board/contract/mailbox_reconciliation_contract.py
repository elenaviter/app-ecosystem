from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Mapping, Sequence

from .errors import DomainError
from .refs import parse_ref


MAILBOX_RECONCILIATION_RECEIPT_SCHEMA = (
    "problem-board.mailbox-reconciliation-receipt.v1"
)
MAILBOX_RECONCILIATION_PUBLICATION_SCHEMA = (
    "problem-board.mailbox-reconciliation-publication.v1"
)
MAILBOX_RECONCILIATION_PAGE_SCHEMA = (
    "problem-board.mailbox-reconciliation-receipts.v1"
)
MAILBOX_RECONCILIATION_ENTRY_PAGE_SCHEMA = (
    "problem-board.mailbox-reconciliation-evidence.v1"
)
MAILBOX_RECONCILIATION_RETENTION_DAYS = 30
MAILBOX_RECONCILIATION_PENDING_RETENTION_DAYS = 7
MAX_MAILBOX_RECONCILIATION_PUBLICATION_BYTES = 48 * 1024
MAX_MAILBOX_RECONCILIATION_ENTRY_BYTES = 16 * 1024
MAX_MAILBOX_RECONCILIATION_ENTRIES = 1_000_000

EVIDENCE_CATEGORIES = (
    "examined_mailbox",
    "archived_mailbox",
    "failure_notice",
    "report_failure",
)


def retention_contract() -> dict[str, Any]:
    return {
        "policy": "rolling_window",
        "completed_receipt_days": MAILBOX_RECONCILIATION_RETENTION_DAYS,
        "incomplete_publication_days": (
            MAILBOX_RECONCILIATION_PENDING_RETENTION_DAYS
        ),
        "basis": "published_at",
        "pruned_on": "mail.reconciliation.publish",
        "local_unpublished_receipts": "retained_until_publication",
    }


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DomainError(
            "work_mail_reconciliation_value_invalid",
            "Mailbox reconciliation evidence must be JSON serializable.",
        ) from exc


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def bounded_text(
    value: Any,
    *,
    field: str,
    maximum: int,
    required: bool = False,
) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise DomainError(
            "work_mail_reconciliation_value_required",
            f"{field} is required.",
            details={"field": field},
        )
    if len(text.encode("utf-8")) > maximum:
        raise DomainError(
            "work_mail_reconciliation_value_too_large",
            f"{field} exceeds its bounded reconciliation receipt limit.",
            details={"field": field, "maximum_bytes": maximum},
        )
    return text


def timestamp(value: Any, *, field: str) -> str:
    text = bounded_text(value, field=field, maximum=64, required=True)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DomainError(
            "work_mail_reconciliation_timestamp_invalid",
            f"{field} must be an ISO-8601 timestamp.",
            details={"field": field},
        ) from exc
    if parsed.tzinfo is None:
        raise DomainError(
            "work_mail_reconciliation_timestamp_invalid",
            f"{field} must include a timezone.",
            details={"field": field},
        )
    return text


def bounded_count(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        number = -1
    else:
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = -1
    if not 0 <= number <= MAX_MAILBOX_RECONCILIATION_ENTRIES:
        raise DomainError(
            "work_mail_reconciliation_count_invalid",
            f"{field} must be a non-negative bounded integer.",
            details={
                "field": field,
                "maximum": MAX_MAILBOX_RECONCILIATION_ENTRIES,
            },
        )
    return number


def bounded_rows(value: Any, *, field: str) -> list[dict[str, Any]]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(
        value, Sequence
    ):
        raise DomainError(
            "work_mail_reconciliation_receipt_invalid",
            f"{field} must be an array.",
            details={"field": field},
        )
    if len(value) > MAX_MAILBOX_RECONCILIATION_ENTRIES:
        raise DomainError(
            "work_mail_reconciliation_receipt_too_large",
            f"{field} contains too many rows.",
            details={
                "field": field,
                "maximum_rows": MAX_MAILBOX_RECONCILIATION_ENTRIES,
            },
        )
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(value):
        if not isinstance(row, Mapping):
            raise DomainError(
                "work_mail_reconciliation_receipt_invalid",
                f"Every {field} row must be an object.",
                details={"field": field, "index": index},
            )
        normalized = json.loads(canonical_bytes(dict(row)).decode("utf-8"))
        if len(canonical_bytes(normalized)) > MAX_MAILBOX_RECONCILIATION_ENTRY_BYTES:
            raise DomainError(
                "work_mail_reconciliation_entry_too_large",
                f"{field}[{index}] exceeds the evidence-row limit.",
                details={
                    "field": field,
                    "index": index,
                    "maximum_bytes": MAX_MAILBOX_RECONCILIATION_ENTRY_BYTES,
                },
            )
        rows.append(normalized)
    return rows


def normalize_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DomainError(
            "work_mail_reconciliation_receipt_invalid",
            "A mailbox reconciliation receipt must be an object.",
        )
    if value.get("schema") != MAILBOX_RECONCILIATION_RECEIPT_SCHEMA:
        raise DomainError(
            "work_mail_reconciliation_receipt_invalid",
            "The mailbox reconciliation receipt schema is unsupported.",
        )
    receipt_id = bounded_text(
        value.get("receipt_id"),
        field="receipt_id",
        maximum=128,
        required=True,
    )
    receipt_ref = bounded_text(
        value.get("receipt_ref"),
        field="receipt_ref",
        maximum=512,
        required=True,
    )
    parsed_receipt = parse_ref(receipt_ref)
    if parsed_receipt.kind != "mail_reconciliation" or (
        parsed_receipt.object_id != receipt_id
    ):
        raise DomainError(
            "work_mail_reconciliation_ref_invalid",
            "The receipt reference must identify its mailbox reconciliation row.",
            details={"receipt_ref": receipt_ref},
        )
    project_ref = bounded_text(
        value.get("project_ref"),
        field="project_ref",
        maximum=256,
        required=True,
    )
    if parse_ref(project_ref).kind != "project":
        raise DomainError(
            "work_project_ref_invalid", "Expected a work:project reference."
        )
    started_at = timestamp(value.get("started_at"), field="started_at")
    completed_at = timestamp(value.get("completed_at"), field="completed_at")
    if datetime.fromisoformat(completed_at.replace("Z", "+00:00")) < (
        datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    ):
        raise DomainError(
            "work_mail_reconciliation_timestamp_invalid",
            "completed_at must not precede started_at.",
        )
    examined = value.get("examined")
    if not isinstance(examined, Mapping):
        raise DomainError(
            "work_mail_reconciliation_receipt_invalid",
            "examined must be an object.",
        )
    result: dict[str, Any] = {
        "schema": MAILBOX_RECONCILIATION_RECEIPT_SCHEMA,
        "receipt_id": receipt_id,
        "receipt_ref": receipt_ref,
        "project_ref": project_ref,
        "reporter_worker_name": bounded_text(
            value.get("reporter_worker_name"),
            field="reporter_worker_name",
            maximum=512,
            required=True,
        ).lower(),
        "host_id": bounded_text(
            value.get("host_id"), field="host_id", maximum=512
        ),
        "relay_id": bounded_text(
            value.get("relay_id"), field="relay_id", maximum=512
        ),
        "started_at": started_at,
        "completed_at": completed_at,
        "examined": {
            "recipient_directory_entries": bounded_count(
                examined.get("recipient_directory_entries"),
                field="examined.recipient_directory_entries",
            ),
            "undeliverable_records": bounded_count(
                examined.get("undeliverable_records"),
                field="examined.undeliverable_records",
            ),
            "mailboxes": bounded_rows(
                examined.get("mailboxes"), field="examined.mailboxes"
            ),
        },
        "archived_count": bounded_count(
            value.get("archived_count"), field="archived_count"
        ),
        "archived_mailboxes": bounded_rows(
            value.get("archived_mailboxes"), field="archived_mailboxes"
        ),
        "failure_notices": bounded_rows(
            value.get("failure_notices"), field="failure_notices"
        ),
        "report_failures": bounded_rows(
            value.get("report_failures"), field="report_failures"
        ),
    }
    declared_hash = bounded_text(
        value.get("content_hash"),
        field="content_hash",
        maximum=64,
    )
    actual_hash = content_hash(result)
    if declared_hash and declared_hash != actual_hash:
        raise DomainError(
            "work_mail_reconciliation_hash_mismatch",
            "The receipt content does not match its declared hash.",
        )
    result["content_hash"] = actual_hash
    return result


__all__ = [
    "EVIDENCE_CATEGORIES",
    "MAILBOX_RECONCILIATION_ENTRY_PAGE_SCHEMA",
    "MAILBOX_RECONCILIATION_PAGE_SCHEMA",
    "MAILBOX_RECONCILIATION_PENDING_RETENTION_DAYS",
    "MAILBOX_RECONCILIATION_PUBLICATION_SCHEMA",
    "MAILBOX_RECONCILIATION_RECEIPT_SCHEMA",
    "MAILBOX_RECONCILIATION_RETENTION_DAYS",
    "MAX_MAILBOX_RECONCILIATION_PUBLICATION_BYTES",
    "bounded_count",
    "bounded_rows",
    "bounded_text",
    "canonical_bytes",
    "content_hash",
    "normalize_receipt",
    "retention_contract",
    "timestamp",
]

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .errors import DomainError
from .mailbox_reconciliation_contract import (
    EVIDENCE_CATEGORIES,
    MAILBOX_RECONCILIATION_PUBLICATION_SCHEMA,
    MAILBOX_RECONCILIATION_RECEIPT_SCHEMA,
    MAX_MAILBOX_RECONCILIATION_ENTRIES,
    MAX_MAILBOX_RECONCILIATION_PUBLICATION_BYTES,
    bounded_count,
    bounded_rows,
    bounded_text,
    canonical_bytes,
    content_hash,
    normalize_receipt,
)


_RECEIPT_LIST_FIELDS = {
    "examined_mailbox": "mailboxes",
    "archived_mailbox": "archived_mailboxes",
    "failure_notice": "failure_notices",
    "report_failure": "report_failures",
}


def receipt_header(receipt: Mapping[str, Any]) -> dict[str, Any]:
    normalized = normalize_receipt(receipt)
    counts = {
        category: len(
            normalized["examined"]["mailboxes"]
            if category == "examined_mailbox"
            else normalized[_RECEIPT_LIST_FIELDS[category]]
        )
        for category in EVIDENCE_CATEGORIES
    }
    return {
        key: normalized[key]
        for key in (
            "schema",
            "receipt_id",
            "receipt_ref",
            "project_ref",
            "reporter_worker_name",
            "host_id",
            "relay_id",
            "started_at",
            "completed_at",
            "archived_count",
            "content_hash",
        )
    } | {
        "recipient_directory_entries": normalized["examined"][
            "recipient_directory_entries"
        ],
        "undeliverable_records": normalized["examined"][
            "undeliverable_records"
        ],
        "evidence_counts": counts,
    }


def receipt_entries(receipt: Mapping[str, Any]) -> list[dict[str, Any]]:
    normalized = normalize_receipt(receipt)
    entries: list[dict[str, Any]] = []
    for category in EVIDENCE_CATEGORIES:
        rows = (
            normalized["examined"]["mailboxes"]
            if category == "examined_mailbox"
            else normalized[_RECEIPT_LIST_FIELDS[category]]
        )
        for ordinal, payload in enumerate(rows):
            entries.append(
                {
                    "category": category,
                    "ordinal": ordinal,
                    "payload": payload,
                    "entry_hash": content_hash(payload),
                }
            )
    return entries


def publication_batches(receipt: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Split one complete local receipt into independently retryable batches."""

    header = receipt_header(receipt)
    entries = receipt_entries(receipt)
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for entry in entries:
        candidate = [*current, entry]
        estimate = {
            "schema": MAILBOX_RECONCILIATION_PUBLICATION_SCHEMA,
            "receipt": header,
            "batch_index": len(groups),
            "batch_count": MAX_MAILBOX_RECONCILIATION_ENTRIES,
            "entries": candidate,
            "batch_hash": content_hash(candidate),
        }
        if (
            current
            and len(canonical_bytes(estimate))
            > MAX_MAILBOX_RECONCILIATION_PUBLICATION_BYTES
        ):
            groups.append(current)
            current = [entry]
        else:
            current = candidate
    groups.append(current)

    publications: list[dict[str, Any]] = []
    for index, group in enumerate(groups):
        publication = {
            "schema": MAILBOX_RECONCILIATION_PUBLICATION_SCHEMA,
            "receipt": header,
            "batch_index": index,
            "batch_count": len(groups),
            "entries": group,
            "batch_hash": content_hash(group),
        }
        encoded = canonical_bytes(publication)
        if len(encoded) > MAX_MAILBOX_RECONCILIATION_PUBLICATION_BYTES:
            raise DomainError(
                "work_mail_reconciliation_publication_too_large",
                "One reconciliation evidence row cannot fit the publication envelope.",
                details={
                    "batch_index": index,
                    "payload_bytes": len(encoded),
                    "maximum_bytes": MAX_MAILBOX_RECONCILIATION_PUBLICATION_BYTES,
                },
            )
        publications.append(publication)
    return publications


def normalize_publication(
    value: Mapping[str, Any], *, project_ref: str
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("schema") != (
        MAILBOX_RECONCILIATION_PUBLICATION_SCHEMA
    ):
        raise DomainError(
            "work_mail_reconciliation_publication_invalid",
            "The mailbox reconciliation publication schema is unsupported.",
        )
    if len(canonical_bytes(value)) > MAX_MAILBOX_RECONCILIATION_PUBLICATION_BYTES:
        raise DomainError(
            "work_mail_reconciliation_publication_too_large",
            "The reconciliation publication exceeds its transport envelope.",
            details={
                "maximum_bytes": MAX_MAILBOX_RECONCILIATION_PUBLICATION_BYTES
            },
        )
    header = dict(value.get("receipt") or {})
    if str(header.get("project_ref") or "") != str(project_ref or ""):
        raise DomainError(
            "work_mail_reconciliation_project_mismatch",
            "The receipt belongs to another project.",
        )
    skeleton = {
        **header,
        "examined": {
            "recipient_directory_entries": header.get(
                "recipient_directory_entries"
            ),
            "undeliverable_records": header.get("undeliverable_records"),
            "mailboxes": [],
        },
        "archived_mailboxes": [],
        "failure_notices": [],
        "report_failures": [],
        "content_hash": "",
    }
    scalar = normalize_receipt(skeleton)
    clean_header = receipt_header(scalar)
    clean_header["content_hash"] = _hash_text(
        header.get("content_hash"), field="receipt.content_hash"
    )
    counts_value = header.get("evidence_counts")
    if not isinstance(counts_value, Mapping):
        raise DomainError(
            "work_mail_reconciliation_publication_invalid",
            "receipt.evidence_counts must be an object.",
        )
    clean_header["evidence_counts"] = {
        category: bounded_count(
            counts_value.get(category),
            field=f"receipt.evidence_counts.{category}",
        )
        for category in EVIDENCE_CATEGORIES
    }

    batch_count = bounded_count(value.get("batch_count"), field="batch_count")
    batch_index = bounded_count(value.get("batch_index"), field="batch_index")
    if batch_count < 1 or batch_index >= batch_count:
        raise DomainError(
            "work_mail_reconciliation_batch_invalid",
            "The publication batch index must fall inside its declared batch count.",
        )
    raw_entries = value.get("entries")
    if isinstance(raw_entries, (str, bytes, bytearray)) or not isinstance(
        raw_entries, Sequence
    ):
        raise DomainError(
            "work_mail_reconciliation_batch_invalid",
            "entries must be an array.",
        )
    entries: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_entries):
        if not isinstance(raw, Mapping):
            raise DomainError(
                "work_mail_reconciliation_batch_invalid",
                "Every publication entry must be an object.",
                details={"index": index},
            )
        category = str(raw.get("category") or "")
        if category not in EVIDENCE_CATEGORIES:
            raise DomainError(
                "work_mail_reconciliation_category_invalid",
                "The reconciliation evidence category is unsupported.",
                details={"category": category},
            )
        payload = bounded_rows(
            [raw.get("payload")], field="entries.payload"
        )[0]
        entry_hash = _hash_text(
            raw.get("entry_hash"), field="entries.entry_hash"
        )
        if entry_hash != content_hash(payload):
            raise DomainError(
                "work_mail_reconciliation_hash_mismatch",
                "A reconciliation evidence row does not match its hash.",
            )
        entries.append(
            {
                "category": category,
                "ordinal": bounded_count(
                    raw.get("ordinal"), field="entries.ordinal"
                ),
                "payload": payload,
                "entry_hash": entry_hash,
            }
        )
    batch_hash = _hash_text(value.get("batch_hash"), field="batch_hash")
    if batch_hash != content_hash(entries):
        raise DomainError(
            "work_mail_reconciliation_hash_mismatch",
            "The reconciliation batch does not match its hash.",
        )
    return {
        "schema": MAILBOX_RECONCILIATION_PUBLICATION_SCHEMA,
        "receipt": clean_header,
        "batch_index": batch_index,
        "batch_count": batch_count,
        "entries": entries,
        "batch_hash": batch_hash,
    }


def reconstruct_receipt(
    header: Mapping[str, Any], entries: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    grouped = {category: [] for category in EVIDENCE_CATEGORIES}
    for entry in sorted(
        entries,
        key=lambda row: (
            EVIDENCE_CATEGORIES.index(str(row.get("category") or "")),
            int(row.get("ordinal") or 0),
        ),
    ):
        category = str(entry.get("category") or "")
        if category not in grouped:
            raise DomainError(
                "work_mail_reconciliation_category_invalid",
                "The stored reconciliation evidence category is unsupported.",
            )
        grouped[category].append(dict(entry.get("payload") or {}))
    receipt = {
        "schema": MAILBOX_RECONCILIATION_RECEIPT_SCHEMA,
        "receipt_id": header.get("receipt_id"),
        "receipt_ref": header.get("receipt_ref"),
        "project_ref": header.get("project_ref"),
        "reporter_worker_name": header.get("reporter_worker_name"),
        "host_id": header.get("host_id"),
        "relay_id": header.get("relay_id"),
        "started_at": header.get("started_at"),
        "completed_at": header.get("completed_at"),
        "examined": {
            "recipient_directory_entries": header.get(
                "recipient_directory_entries"
            ),
            "undeliverable_records": header.get("undeliverable_records"),
            "mailboxes": grouped["examined_mailbox"],
        },
        "archived_count": header.get("archived_count"),
        "archived_mailboxes": grouped["archived_mailbox"],
        "failure_notices": grouped["failure_notice"],
        "report_failures": grouped["report_failure"],
    }
    normalized = normalize_receipt(receipt)
    if normalized["content_hash"] != str(header.get("content_hash") or ""):
        raise DomainError(
            "work_mail_reconciliation_hash_mismatch",
            "The stored reconciliation evidence does not reconstruct its receipt.",
            status=409,
        )
    return normalized


def _hash_text(value: Any, *, field: str) -> str:
    text = bounded_text(value, field=field, maximum=64, required=True)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise DomainError(
            "work_mail_reconciliation_hash_invalid",
            f"{field} must be a lowercase SHA-256 digest.",
            details={"field": field},
        )
    return text


__all__ = [
    "normalize_publication",
    "publication_batches",
    "receipt_entries",
    "receipt_header",
    "reconstruct_receipt",
]

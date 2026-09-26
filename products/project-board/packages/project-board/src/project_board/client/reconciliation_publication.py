"""Queue a stored reconciliation receipt for the service, and read its outcome.

This module is the one place that decides whether receipt evidence leaves the
host. Every publication is one outbox row per bounded batch, with an id derived
from the batch's content hash, so finding a batch is one direct path lookup and
never a scan of the outbox history (W287, rule LS3 in
``docs/project-board/storage-and-retention.md``).

A receipt's publication is ``queued`` until every batch row is terminal, then
``published`` when the service accepted every batch, or ``refused`` when it
refused any. A refused publication is a finished outcome: the receipt leaves
``pending/`` with the refusal named in its file, and retention never removes it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from ..contract.errors import DomainError
from ..contract.mailbox_reconciliation_contract import normalize_receipt
from ..contract.mailbox_reconciliation_publication import publication_batches
from .io import atomic_write_json, content_hash, exclusive_lock, read_json, utc_now
from .outbox_store import OutboxStore


OUTBOX_KIND = "mail.reconciliation.publish"
PUBLICATION_QUEUED = "queued"
# A batch row the receipt names is gone (pruned or lost): the receipt is
# re-queued, or it would stay in pending/ and be re-read by every recovery.
PUBLICATION_MISSING = "missing"
PUBLICATION_PUBLISHED = "published"
PUBLICATION_REFUSED = "refused"
# Outcomes after which a receipt leaves pending/ and retention may consider it.
TERMINAL_PUBLICATION_STATES = frozenset({PUBLICATION_PUBLISHED, PUBLICATION_REFUSED})
# Outcomes whose receipts retention may remove once they are old enough. A
# refusal is kept: its evidence never reached the service (W287 acceptance 1).
PRUNABLE_PUBLICATION_STATES = frozenset({PUBLICATION_PUBLISHED})


def queue_publication(
    field: Any,
    project_id: str,
    *,
    worker_name: str,
    record_path: Path,
) -> dict[str, Any]:
    """Create any missing batch row for one stored receipt, idempotently."""

    record = read_json(record_path)
    receipt = normalize_receipt(record.get("receipt") or {})
    publications = publication_batches(receipt)
    outbox = OutboxStore(field.control)
    outbox_ids: list[str] = []
    with exclusive_lock(outbox.lock):
        for publication in publications:
            batch_index = int(publication["batch_index"])
            publication_hash = content_hash(publication)
            outbox_id = publication_outbox_id(publication_hash)
            row = outbox.read(outbox_id, worker_name=worker_name, project_ref=receipt["project_ref"])
            if row:
                _require_publication_row(
                    row,
                    receipt_ref=receipt["receipt_ref"],
                    batch_index=batch_index,
                    publication_hash=publication_hash,
                )
                outbox_ids.append(outbox_id)
                continue
            outbox.write_pending(
                {
                    "schema": "problem-board.service-outbox.v1",
                    "outbox_id": outbox_id,
                    "kind": OUTBOX_KIND,
                    "worker_name": str(worker_name),
                    "project_ref": receipt["project_ref"],
                    "content_hash": publication_hash,
                    "payload": publication,
                    "state": "pending",
                    "created_at": utc_now(),
                    "retry_count": 0,
                    "next_attempt_at": "",
                },
            )
            outbox_ids.append(outbox_id)

    with exclusive_lock(field._project_lock(project_id)):
        current = read_json(record_path)
        current["publication"] = {
            "kind": OUTBOX_KIND,
            "outbox_ids": outbox_ids,
            "batch_count": len(publications),
            "queued_at": str(
                (current.get("publication") or {}).get("queued_at") or utc_now()
            ),
        }
        atomic_write_json(record_path, current)
        return current


def publication_is_queued(record: Mapping[str, Any]) -> bool:
    """Whether every batch of the receipt has an outbox row recorded."""

    publication = dict(record.get("publication") or {})
    expected = int(publication.get("batch_count") or 0)
    outbox_ids = [str(value) for value in publication.get("outbox_ids") or [] if value]
    return expected > 0 and len(outbox_ids) == expected


def publication_state(field: Any, record: Mapping[str, Any]) -> str:
    """``queued``, ``published``, ``refused`` or ``missing``, from the batch rows by id."""

    if not publication_is_queued(record):
        return PUBLICATION_QUEUED
    outbox = OutboxStore(field.control)
    receipt = dict(record.get("receipt") or {})
    refused = False
    for outbox_id in (record.get("publication") or {}).get("outbox_ids") or []:
        row = outbox.read(
            str(outbox_id),
            worker_name=str(receipt.get("reporter_worker_name") or ""),
            project_ref=str(receipt.get("project_ref") or ""),
        )
        if not row:
            return PUBLICATION_MISSING
        state = str(row.get("state") or "")
        if state == "refused":
            refused = True
        elif state not in {"sent", "ignored"}:
            return PUBLICATION_QUEUED
    return PUBLICATION_REFUSED if refused else PUBLICATION_PUBLISHED


def publication_outbox_id(publication_hash: str) -> str:
    return f"outbox_mailrecon_{publication_hash}"


def _require_publication_row(
    row: Mapping[str, Any],
    *,
    receipt_ref: str,
    batch_index: int,
    publication_hash: str,
) -> None:
    payload = dict(row.get("payload") or {})
    header = dict(payload.get("receipt") or {})
    if row.get("kind") == OUTBOX_KIND and str(row.get("content_hash") or "") == publication_hash:
        # A sent row no longer carries its payload, and its id is the content
        # hash, so the hash alone proves it is this batch.
        if not payload:
            return
        if header.get("receipt_ref") == receipt_ref and int(payload.get("batch_index") or 0) == batch_index:
            return
    raise DomainError(
        "field_mail_reconciliation_idempotency_conflict",
        "A reconciliation publication batch has different content.",
        status=409,
        details={"receipt_ref": receipt_ref, "batch_index": batch_index},
    )


__all__ = [
    "OUTBOX_KIND",
    "PRUNABLE_PUBLICATION_STATES",
    "PUBLICATION_PUBLISHED",
    "PUBLICATION_MISSING",
    "PUBLICATION_QUEUED",
    "PUBLICATION_REFUSED",
    "TERMINAL_PUBLICATION_STATES",
    "publication_is_queued",
    "publication_outbox_id",
    "publication_state",
    "queue_publication",
]

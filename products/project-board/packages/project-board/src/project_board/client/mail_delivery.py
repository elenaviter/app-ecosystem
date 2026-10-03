from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..contract.delivery_failures import (
    is_terminal_system_notice, RETIREMENT_DELIVERY_SCHEMA, RETIREMENT_DELIVERY_PURPOSE,
)
from ..contract.errors import DomainError
from .io import atomic_write_json, content_hash, exclusive_lock, read_json, utc_now


def retirement_delivery_publication(
    field: Any, *, project_ref: str, reporter_worker_name: str,
    retired_worker_name: str, message: Mapping[str, Any],
) -> dict[str, Any]:
    """Durably publish evidence, retaining originals until exact server coverage.

    The original holder's history/outbox remains pending. A sent publication
    alone is insufficient: only this original's queued/unavailable canonical
    notice binding covers it. Restart and lost response reuse the same input
    outbox key, while the server merges late evidence into its global claim.
    """
    payload = dict(message.get('payload') or {})
    control_ref = str(payload.get('command_ref') or '')
    source_ref = str(payload.get('source_message_ref') or control_ref or message.get('message_ref') or '')
    proof: dict[str, Any] = {'source_message_ref': source_ref}
    if control_ref:
        proof['control_ref'] = control_ref
        command = payload.get('retirement_command') or payload.get('command')
        if isinstance(command, Mapping):
            proof['command_payload'] = dict(command)
        else:
            # Older admitted envelopes still retain enough for plain mail.
            # Hash verification on the server refuses incomplete attachment
            # reconstruction; the original remains pending, never suppressed.
            proof['command_payload'] = {'mail': {
                'kind': message.get('kind') or '', 'subject': message.get('subject') or '',
                'body': message.get('body') or '', 'payload': dict(payload.get('payload') or {}),
                'source_message_ref': source_ref, 'correlation_id': message.get('correlation_id') or '',
                'reply_to': message.get('reply_to') or '', 'work_ref': payload.get('work_ref') or '',
                'identity_ref': payload.get('identity_ref') or '',
            }}
    else:
        identity = dict(message.get('sender_identity') or {})
        proof['sender_worker_name'] = str(identity.get('worker_name') or message.get('sender') or '')
        proof['original_mail'] = {
            'source_message_ref': source_ref, 'recipient': retired_worker_name,
            'kind': message.get('kind') or '', 'subject': message.get('subject') or '',
            'body': message.get('body') or '', 'payload': dict(payload),
            'correlation_id': message.get('correlation_id') or '', 'reply_to': message.get('reply_to') or '',
            'work_ref': message.get('work_ref') or '',
        }
    if not source_ref or not project_ref:
        return {'schema': RETIREMENT_DELIVERY_SCHEMA, 'delivery_status': 'pending',
                'reason': 'canonical_original_context_pending'}
    publication = {'schema': RETIREMENT_DELIVERY_SCHEMA, 'purpose': RETIREMENT_DELIVERY_PURPOSE,
                   'retired_worker_name': retired_worker_name, 'members': [proof]}
    digest = content_hash(publication)
    outbox_id = 'outbox_retirement_' + digest
    outbox = field._outbox
    with exclusive_lock(outbox.lock):
        row = outbox.read(outbox_id, worker_name=reporter_worker_name, project_ref=project_ref)
        if row is None:
            row = {'schema': 'problem-board.service-outbox.v1', 'outbox_id': outbox_id,
                   'kind': 'mail.reconciliation.publish', 'worker_name': reporter_worker_name,
                   'project_ref': project_ref, 'content_hash': digest, 'payload': publication,
                   'state': 'pending', 'created_at': utc_now(), 'retry_count': 0, 'next_attempt_at': ''}
            outbox.write_pending(row)
        elif row.get('content_hash') != digest:
            raise DomainError('field_retirement_publication_conflict', 'Immutable retirement evidence changed.', status=409)
    result = dict(row.get('remote_result') or {})
    for coverage in result.get('coverage') or []:
        if (isinstance(coverage, Mapping) and result.get('schema') == RETIREMENT_DELIVERY_SCHEMA
            and coverage.get('source_message_ref') == source_ref
            and coverage.get('notice_state') in {'queued', 'unavailable'} and coverage.get('receipt_ref')):
            return {**dict(coverage), 'schema': RETIREMENT_DELIVERY_SCHEMA,
                    'delivery_status': coverage['notice_state'], 'outbox_id': outbox_id}
    return {'schema': RETIREMENT_DELIVERY_SCHEMA, 'delivery_status': 'pending', 'outbox_id': outbox_id,
            'reason': 'canonical_notice_coverage_pending'}


ACTIVE_MAILBOX_STATES = ("inbox", "leased")
# A recipient that left the project keeps what it holds (W325).
UNLEASED_MAILBOX_STATES = ("inbox",)
ALL_MAILBOX_STATES = ("inbox", "leased", "processed", "quarantine")


def delivery_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    """Project one outbox delivery without returning its retained full body."""

    payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
    body = str(payload.get("body") or "")
    failure = row.get("delivery_failure") if isinstance(row.get("delivery_failure"), Mapping) else None
    return {
        "outbox_id": str(row.get("outbox_id") or ""),
        "project_ref": str(row.get("project_ref") or ""),
        "state": str(row.get("state") or ""),
        # A sent row keeps these beside the dropped body (W304 finding 38).
        "recipient": str(payload.get("recipient") or row.get("recipient") or ""),
        "kind": str(payload.get("kind") or row.get("kind_sent") or ""),
        "subject": str(payload.get("subject") or row.get("subject") or ""),
        "source_message_ref": str(payload.get("source_message_ref") or row.get("source_message_ref") or ""),
        **({"delivery_failure": dict(failure)} if failure is not None else {}),
        "remote_disposition": str(row.get("remote_disposition") or ""),
        "created_at": str(row.get("created_at") or ""),
        "settled_at": str(row.get("settled_at") or ""),
        "body_bytes": len(body.encode("utf-8")),
        "body_preview": body[:240],
        "replayed_at": str(row.get("replayed_at") or ""),
        "replayed_to": str(row.get("replayed_to") or ""),
        "replayed_kind": str(row.get("replayed_kind") or ""),
        "replay_message_ref": str(row.get("replay_message_ref") or ""),
        "replay_outbox_id": str(row.get("replay_outbox_id") or ""),
    }


def archive_mailbox_messages(
    source_root: Path,
    destination_root: Path,
    *,
    states: Sequence[str],
    disposition: str,
    reason: str,
    details: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Move mailbox records into an inspectable undeliverable archive."""

    moved: list[dict[str, Any]] = []
    destination_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for state in states:
        folder = source_root / state
        for source in sorted(folder.glob("*.json")):
            row = read_json(source)
            now = utc_now()
            row.update(
                mailbox_state="undeliverable",
                previous_mailbox_state=state,
                state=disposition,
                delivery_status=disposition,
                undeliverable_at=now,
                updated_at=now,
                recipient_failure={
                    "code": disposition,
                    "reason": str(reason or ""),
                    "details": dict(details or {}),
                    "notify_sender": (
                        state in ACTIVE_MAILBOX_STATES
                        and not is_terminal_system_notice(row)
                    ),
                },
            )
            row.pop("lease", None)
            atomic_write_json(source, row)
            destination = destination_root / source.name
            if destination.exists():
                source.unlink(missing_ok=True)
                row = read_json(destination)
            else:
                os.replace(source, destination)
            moved.append(row)
    return moved


__all__ = [
    "ACTIVE_MAILBOX_STATES",
    "ALL_MAILBOX_STATES",
    "UNLEASED_MAILBOX_STATES",
    "archive_mailbox_messages",
    "delivery_summary",
]

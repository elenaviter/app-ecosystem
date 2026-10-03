from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..contract.delivery_failures import (
    is_terminal_system_notice, RETIREMENT_DELIVERY_SCHEMA, RETIREMENT_DELIVERY_PURPOSE,
)
from ..contract.errors import DomainError
from .io import atomic_write_json, content_hash, exclusive_lock, read_json, utc_now


def _retirement_original_proof(message: Mapping[str, Any], retired_worker_name: str) -> tuple[str, dict[str, Any] | None, str]:
    """Build exact private evidence, or name why a retained original is pending."""
    payload = dict(message.get('payload') or {})
    control_ref = str(payload.get('command_ref') or '')
    source_ref = str(payload.get('source_message_ref') or control_ref or message.get('message_ref') or '')
    proof: dict[str, Any] = {'source_message_ref': source_ref}
    if control_ref:
        proof['control_ref'] = control_ref
        command = payload.get('retirement_command')
        if not isinstance(command, Mapping):
            command = payload.get('command')
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
            if not payload.get('payload_hash') or content_hash(proof['command_payload']) != payload['payload_hash']:
                # A lossy old envelope is incomplete proof, not evidence that
                # the canonical original changed. Keep it local and visibly
                # pending rather than sending a known false content conflict.
                return source_ref, None, 'legacy_original_proof_incomplete'
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
    return source_ref, proof, ''


def retirement_delivery_publication(
    field: Any, *, project_ref: str, reporter_worker_name: str,
    retired_worker_name: str, message: Mapping[str, Any],
    additional_messages: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Publish a bounded backlog together; retain originals until exact coverage.

    A lost response/restart uses the same durable publication binding. Each
    binding also hashes this original's proof, so a changed retained original
    cannot borrow an earlier batch's coverage. A queued notice is not delivery.
    """
    source_ref, proof, reason = _retirement_original_proof(message, retired_worker_name)
    if not source_ref or not project_ref or proof is None:
        return {'schema': RETIREMENT_DELIVERY_SCHEMA, 'delivery_status': 'pending',
                'reason': reason or 'canonical_original_context_pending',
                'guidance': 'Recover the complete admitted command; the original remains retained.'}
    proofs = [proof]
    # Stable input order and an encoded byte cap make each batch bounded.
    # Invalid extra originals remain unbound and get their own diagnostic.
    for other in additional_messages:
        _, extra, _ = _retirement_original_proof(other, retired_worker_name)
        if extra is None or extra in proofs:
            continue
        if len(proofs) >= 100 or len(json.dumps([*proofs, extra], ensure_ascii=True).encode()) > 900_000:
            break
        proofs.append(extra)
    proofs.sort(key=lambda item: item['source_message_ref'])
    publication = {'schema': RETIREMENT_DELIVERY_SCHEMA, 'purpose': RETIREMENT_DELIVERY_PURPOSE,
                   'retired_worker_name': retired_worker_name, 'members': proofs}
    digest = content_hash(publication)
    outbox_id = 'outbox_retirement_' + digest
    outbox = field._outbox
    with exclusive_lock(outbox.lock):
        binding = message.get('retirement_publication')
        binding = binding if isinstance(binding, Mapping) else {}
        bound_id = str(binding.get('outbox_id') or '')
        if bound_id and binding.get('proof_hash') != content_hash(proof):
            raise DomainError('field_retirement_publication_conflict', 'The retained original proof changed.', status=409)
        row = outbox.read(bound_id or outbox_id, worker_name=reporter_worker_name, project_ref=project_ref)
        if row is None:
            row = {'schema': 'problem-board.service-outbox.v1', 'outbox_id': outbox_id,
                   'kind': 'mail.reconciliation.publish', 'worker_name': reporter_worker_name,
                   'project_ref': project_ref, 'content_hash': digest, 'payload': publication,
                   'state': 'pending', 'created_at': utc_now(), 'retry_count': 0, 'next_attempt_at': ''}
            outbox.write_pending(row)
        elif not bound_id and row.get('content_hash') != digest:
            raise DomainError('field_retirement_publication_conflict', 'Immutable retirement evidence changed.', status=409)
        else:
            outbox_id = str(row['outbox_id'])
    bindings = [{'source_message_ref': item['source_message_ref'], 'proof_hash': content_hash(item)} for item in proofs]
    result = dict(row.get('remote_result') or {})
    if row.get('state') == 'refused':
        error = result.get('error') if isinstance(result.get('error'), Mapping) else {}
        return {'schema': RETIREMENT_DELIVERY_SCHEMA, 'delivery_status': 'pending', 'outbox_id': outbox_id,
                'reason': 'canonical_publication_refused', 'error_code': str(error.get('code') or ''),
                'proof_hash': content_hash(proof), 'publication_bindings': bindings,
                'guidance': 'Inspect the private publication refusal; no original has been marked covered.'}
    for coverage in (result.get('coverage') or []) if row.get('state') == 'sent' else []:
        if (isinstance(coverage, Mapping) and result.get('schema') == RETIREMENT_DELIVERY_SCHEMA
            and coverage.get('source_message_ref') == source_ref
            and coverage.get('notice_state') in {'queued', 'unavailable'} and coverage.get('receipt_ref')):
            return {**dict(coverage), 'schema': RETIREMENT_DELIVERY_SCHEMA,
                    'delivery_status': coverage['notice_state'], 'outbox_id': outbox_id,
                    'proof_hash': content_hash(proof),
                    'publication_bindings': bindings}
    return {'schema': RETIREMENT_DELIVERY_SCHEMA, 'delivery_status': 'pending', 'outbox_id': outbox_id,
            'reason': 'canonical_notice_coverage_pending', 'publication_bindings': bindings,
            'proof_hash': content_hash(proof)}


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

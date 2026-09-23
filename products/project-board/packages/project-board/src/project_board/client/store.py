from __future__ import annotations

import hashlib
import json
import mimetypes

# Markdown is the journal format; not every interpreter registers it.
mimetypes.add_type("text/markdown", ".md")
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..contract.delivery_failures import (
    DeliveryFailureTarget,
    delivery_failure_target_lines,
    resolve_delivery_failure_target,
)
from ..contract.errors import DomainError
from ..contract.mail_addresses import resolve_mail_recipient
from ..contract.mailbox_reconciliation_contract import (
    MAILBOX_RECONCILIATION_RECEIPT_SCHEMA,
)
from ..contract.operator_mail_contract import (
    OPERATOR_RECIPIENTS,
    require_operator_mail_kind,
)
from ..contract.plan_index import PLAN_NODES_SCHEMA, attachment_refs, indexed_plan_item
from ..contract.plan_nodes import MAX_WORK_ITEM_REF_BYTES, make_plan_node_ref
from ..contract.plan_nodes import parse_plan_node_ref
from ..contract.plan_nodes import plan_node_identity_ref
from ..contract.portable_refs import normalize_repository_ref
from ..contract.reference_records import reference_for_record
from ..contract.refs import make_ref, parse_ref
from ..contract.scoped_collection import CollectionError, ScopedKeysetCursor


# A local plan is a shard until a complete server generation is mirrored
# here. These name the two states so no reader has to guess which it has.
PLAN_AUTHORITY_LOCAL_SHARD = "local_shard"
PLAN_AUTHORITY_SERVER_SNAPSHOT = "server_snapshot"
from ..contract.worker_identity import (
    RUNTIME_KINDS,
    WorkerSessionIdentity,
    normalize_worker_alias,
)
from .io import (
    atomic_write_json,
    bounded_text,
    component,
    content_hash,
    exclusive_lock,
    json_records,
    newest_json_records,
    new_id,
    new_keyed_id,
    new_named_id,
    new_timed_id,
    parse_utc,
    read_json,
    utc_now,
)
from .mail_delivery import (
    ACTIVE_MAILBOX_STATES,
    ALL_MAILBOX_STATES,
    archive_mailbox_messages,
    delivery_summary,
)
from .contested import VERDICTS, matrix, normalize_positions, record_for
from .mail_budget import (
    MAX_RECEIVABLE_MESSAGE_BYTES,
    RECEIVE_ENVELOPE_RESERVE_BYTES,
    MailPullBudget,
)
from .mail_attachments import (
    WORKER_ATTACHMENT_READ_SCHEMA,
    attachment_by_ref,
    materialized_attachment_manifest,
    stored_attachment_manifest,
    verified_attachment,
)
from .projection import build_projection
from .plan_storage import BucketedPlanStore
from .outbox_layout import (
    OUTBOX_FOLDERS,
    OUTBOX_IN_FLIGHT_FOLDERS,
    terminal_folder,
)
from .reconciliation_receipts import (
    record_receipt as record_mailbox_reconciliation_receipt,
)
from .reconciliation_receipts import recover_unpublished_receipts

_LOGGER = logging.getLogger("problem_board.local.store")

SKIPPED_BODY_HEAD_BYTES = 2048


def _utf8_head(text: str, limit: int) -> str:
    """The longest prefix of ``text`` within ``limit`` UTF-8 bytes."""

    return text.encode("utf-8")[: max(0, int(limit))].decode("utf-8", errors="ignore")


def _undeliverable_reason(
    *, message_bytes: int, response_bytes: int, nothing_claimed: bool
) -> tuple[str, str] | None:
    """Why no receive can carry this message, or ``None`` when a later one can.

    The message is judged on its own serialized size against a fixed envelope
    reserve, never against a response inflated by session state. A message
    that fits the reserve but still does not fit an otherwise empty response
    means the envelope outgrew its reserve: that is a platform fault, it is
    logged as one, and the message is still skipped rather than left to block
    the inbox.
    """

    if int(message_bytes) > MAX_RECEIVABLE_MESSAGE_BYTES:
        return (
            "message_exceeds_receive_budget",
            "The message is larger than one receive can carry "
            f"({int(message_bytes)} of at most {MAX_RECEIVABLE_MESSAGE_BYTES} "
            "bytes), so it was not delivered or processed. Ask the sender to "
            "resend a shorter body and to put the long part in a file in the "
            "shared project repository, referenced by its path.",
        )
    if nothing_claimed:
        _LOGGER.error(
            "[problem-board.receive] receive envelope exceeds its reserve: "
            "message_bytes=%s response_bytes=%s reserve=%s",
            message_bytes,
            response_bytes,
            RECEIVE_ENVELOPE_RESERVE_BYTES,
        )
        return (
            "receive_envelope_exceeds_budget",
            "The message fits the receive limit, but this receive's own envelope "
            "was too large to carry it. This is a Problem Board fault, not the "
            "sender's. Ask the sender to resend it unchanged.",
        )
    return None


def _receive_stub_marker(
    row: Mapping[str, Any], *, code: str, explanation: str, message_bytes: int
) -> dict[str, Any]:
    """What a receive tells the worker about a message it cannot carry whole."""

    body = str(row.get("body") or "")
    payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
    attachments = row.get("attachments") if isinstance(row.get("attachments"), list) else []
    omitted = sorted(str(key) for key in row if str(key) not in _STUB_FIELDS)
    return {
        "reason": code,
        "explanation": explanation,
        "message_bytes": int(message_bytes),
        "maximum_message_bytes": MAX_RECEIVABLE_MESSAGE_BYTES,
        "body_bytes": len(body.encode("utf-8")),
        "payload_bytes": len(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")),
        "attachment_count": len(attachments),
        "attachments_bytes": len(
            json.dumps(attachments, sort_keys=True, default=str).encode("utf-8")
        ),
        # Envelope fields the stub leaves out, by name only. Both the count of
        # names and each name are bounded: review 2026-09-21 built a 123 KB
        # stub from 32 unknown keys of 4,000 characters each.
        "omitted_field_count": len(omitted),
        "omitted_fields": [
            _escaped_head(name[:_STUB_FIELD_NAME_BYTES], _STUB_FIELD_NAME_BYTES)
            for name in omitted[:_STUB_FIELD_NAMES]
        ],
        # Bounded by its escaped size like every other part of the stub: a
        # control character escapes to 6 bytes, an emoji to 12.
        "body_head": _escaped_head(body[:SKIPPED_BODY_HEAD_BYTES], SKIPPED_BODY_HEAD_BYTES),
        # Replaced by the receive once it has written the sender's notice.
        "sender_notice": "pending",
    }


# The fields a stub keeps: what identifies the message, lets the receiver reply
# to it, and lets it settle the lease. Everything else (attachment descriptors,
# enrichment, any field a sender or a later version adds) is left out, so a
# message too large in any part still yields a bounded stub. Review 2026-09-21:
# a stub that copied every field reached 90 KB with 40 attachment descriptors.
_STUB_FIELDS = frozenset(
    {
        "schema", "message_id", "message_ref", "project_ref", "work_ref",
        "sender", "recipient", "kind", "subject", "correlation_id", "reply_to",
        "idempotency_key", "created_at", "updated_at", "state",
        "delivery_status", "content_hash", "lease",
    }
)
_STUB_TEXT_LIMIT = 512
_STUB_FIELD_NAMES = 16
_STUB_FIELD_NAME_BYTES = 64
# Upper bound on a serialized stub: 18 allowlisted fields of at most 512
# escaped bytes, the sender identity, the marker with 16 names of at most 64
# escaped bytes, and the body with a head of at most 2 KiB escaped. That sums
# to about 13 KiB, so 24 KiB leaves room for a new field or a looser cap.
# The receive reserve (RECEIVE_ENVELOPE_RESERVE_BYTES) must stay above it.
STUB_MAX_BYTES = 24 * 1024


def _escaped_head(text: str, limit: int) -> str:
    """The longest prefix of ``text`` whose JSON form (ASCII-escaped, as the
    receive response is written) stays within ``limit`` bytes. A character
    cap is not enough: one emoji escapes to 12 bytes."""

    used = 0
    for index, char in enumerate(text):
        used += len(json.dumps(char, ensure_ascii=True)) - 2
        if used > limit:
            return text[:index]
    return text


def _stub_value(key: str, value: Any) -> Any:
    """One allowlisted field as a stub carries it, bounded whatever its type."""

    if key == "lease" and isinstance(value, Mapping):
        return value  # written by the claim itself, never by the sender
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and abs(value) < 2**63:
        return value
    return _escaped_head(str(value)[: _STUB_TEXT_LIMIT * 2], _STUB_TEXT_LIMIT)


def _stub_message(row: Mapping[str, Any], marker: Mapping[str, Any]) -> dict[str, Any]:
    """The bounded message a receive carries in place of one too large for it.

    It keeps only ``_STUB_FIELDS``, each short text capped, so the lease,
    counts, reply and settle work as for any message while the stub stays
    small whatever made the original large. The body states the reason and
    the sizes and carries the first bytes of the original.
    """

    head = str(marker.get("body_head") or "")
    stub = {key: _stub_value(key, value) for key, value in row.items() if key in _STUB_FIELDS}
    identity = row.get("sender_identity") if isinstance(row.get("sender_identity"), Mapping) else {}
    if identity:
        stub["sender_identity"] = {
            key: _escaped_head(str(identity.get(key) or "")[:320], 160)
            for key in ("kind", "label")
            if identity.get(key)
        }
    stub["body"] = (
        f"[Not delivered in full: {marker.get('explanation')} "
        f"Message {marker.get('message_bytes')} bytes, body {marker.get('body_bytes')} "
        f"bytes, payload {marker.get('payload_bytes')} bytes; at most "
        f"{marker.get('maximum_message_bytes')} bytes per message. Its sender is told "
        f"automatically. The first {len(head.encode('utf-8'))} bytes of the body "
        "follow.]\n\n" + head
    )
    stub["payload"] = {}
    stub["skipped_by_receive"] = {
        key: value for key, value in dict(marker).items() if key != "body_head"
    }
    return stub


def _delivered_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """A stored message as a worker receives it: whole, or its stub."""

    marker = row.get("skipped_by_receive")
    if isinstance(marker, Mapping):
        return _stub_message(row, marker)
    return dict(row)



FIELD_SCHEMA = "problem-board.shared-field.v1"
MAIL_SCHEMA = "problem-board.local-mail.v1"
MAIL_LEASE_PAGE_SCHEMA = "problem-board.local-mail-leases.v1"
MAIL_DELIVERY_PAGE_SCHEMA = "problem-board.local-mail-deliveries.v1"
JOURNAL_SCHEMA = "problem-board.local-journal-receipt.v2"
OUTBOX_SCHEMA = "problem-board.service-outbox.v1"
# The ceiling on renewing a claim on one message. A worker that has needed
# an hour on a single message is not still working, it is stuck, and the
# message should become visible to someone else.
MAX_MAIL_HOLD_SECONDS = 3600
# A message that cannot be enriched for model delivery gets a small number of
# independent retries. After that it leaves the active inbox so healthy mail
# keeps flowing, while the original envelope and sender remain inspectable.
MAX_MAIL_RECEIVE_FAILURES = 3
# Retry a Codex queue submission that has not succeeded yet. Once the queue
# accepts it, the wake stays outstanding for model acknowledgement without
# enqueueing another copy.
WAKE_ACK_BASE_SECONDS = 60
WAKE_ACK_MAX_SECONDS = 300
# The operator-report threshold. It is not the retry deadline: the
# retry deadline governs what the relay does to the queue, this governs when
# a human is told. It applies to two of the three terminal wake states. A
# wake consumed twice without acknowledgement is reported once its retry has
# been spent for this long. A wake still queued for this long produces a
# factual "wake still queued" notice, not a verdict that the session is gone:
# a Codex turn can exceed five minutes with nothing wrong, and the board says
# working and wake delayed at once. A retry that never reached the queue is
# an observed transport failure and is reported at once, with no threshold.
#
# Why 300: the retry backoff re-lists the native queue at 60, 120 and 240
# seconds, so at 300 a human is told only after the relay has independently
# found the same submission untaken three times. The number is tied to what
# the relay does, not to how long a turn runs. It counts from the first
# expired acknowledgement deadline (wake_queued_overdue_since, about 60 s
# after enqueue), so the notice falls due about 360 s after enqueue, and it
# is derived only from a listing taken after it fell due: the relay forces
# that listing when the report is due and the last one predates it.
#
# Measured cases distinguish a wake taken shortly after 60 seconds from one
# overdue for several hours. Ninety seconds reports a healthy slow wake on
# fewer than two listings; 180 seconds, the second listing, is defensible and rejected for
# asymmetry, since a report that fires on a transient teaches its reader to
# ignore it and two minutes are nothing against an outage measured in hours;
# 600 s, the fourth listing, adds no evidence the third did not. Whoever
# changes this number argues with those cases, not with the number.
WAKE_OVERDUE_GRACE_SECONDS = 300
MAX_WAKE_MESSAGE_REFS = 100
MAX_WAKE_ACKNOWLEDGEMENTS = 20
MAX_WAKE_QUEUE_SUBMISSIONS = 20
# Per-wake observation fields describe one outstanding wake and
# are cleared with it, on acknowledgement and when a fresh wake replaces it.
WAKE_OBSERVATION_FIELDS = (
    "wake_overdue_checks",
    "wake_queued_overdue_since",
    "wake_consumed_observed_at",
    "wake_consumed_retries",
    "wake_retry_exhausted_since",
    "wake_queued_confirmed_at",
)
OUTBOX_RETRY_BASE_SECONDS = 5
OUTBOX_RETRY_MAX_SECONDS = 300
SESSION_SCHEMA = "problem-board.local-worker-session.v1"
ASSIGNMENT_SCHEMA = "problem-board.local-assignment.v1"


def _assignment_sources(assignment: Mapping[str, Any], *, repository_ref: str) -> list[dict[str, str]]:
    """The repository entries the notice binds, each normalized, or the scalar source as one entry."""

    raw = assignment.get("sources")
    entries: list[dict[str, str]] = []
    for index, entry in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(entry, Mapping):
            continue
        ref = str(entry.get("repository_ref") or "")
        if not ref:
            continue
        entries.append(
            {
                "repository_ref": normalize_repository_ref(ref, field=f"sources[{index}].repository_ref"),
                "base_commit": bounded_text(entry.get("base_commit"), field=f"sources[{index}].base_commit", maximum=64).lower(),
                "branch": bounded_text(entry.get("branch"), field=f"sources[{index}].branch", maximum=512),
            }
        )
    if not entries and repository_ref:
        source = assignment.get("source") if isinstance(assignment.get("source"), Mapping) else {}
        entries = [
            {
                "repository_ref": repository_ref,
                "base_commit": bounded_text(source.get("base_commit"), field="source.base_commit", maximum=64).lower(),
                "branch": bounded_text(source.get("branch"), field="source.branch", maximum=512),
            }
        ]
    return entries
ASSIGNMENT_RECONCILIATION_ISSUE_SCHEMA = (
    "problem-board.assignment-reconciliation.v2"
)
RELAY_DIAGNOSTIC_SCHEMA = "problem-board.relay-channel-diagnostic.v1"
HOST_KINDS = {"local", "hosted", "remote"}
SESSION_STATES = {"waiting", "working", "blocked", "detached"}
# A worker's workload is its live assignment rows, and no
# persisted focus field may exist in any shape. The service and the UI dropped
# current_work_ref, but records written before that still carry the value they
# last held, and a copy-modify-write preserves whatever it does not name. A
# frozen pointer reads as fact, so it is stripped wherever a listener is loaded
# rather than left to age out of records nothing rewrites.
LEGACY_LISTENER_FIELDS = ("current_work_ref",)


def listener_without_legacy_fields(value: Any) -> dict[str, Any]:
    listener = dict(value) if isinstance(value, Mapping) else {}
    for field in LEGACY_LISTENER_FIELDS:
        listener.pop(field, None)
    subscription = listener.get("subscription")
    if (
        isinstance(subscription, Mapping)
        and str(subscription.get("wake_delivery_state") or "") == "delivered"
    ):
        # Legacy rows say "delivered" for a submission the native
        # queue accepted, with no deadline. Read them as what they were: a
        # queued wake whose deadline has passed, so the refusal, the
        # reconciliation trigger and the queue-incident report (a factual
        # "wake still queued" notice, not a dead-path verdict) all see them.
        listener["subscription"] = {
            **dict(subscription),
            "wake_delivery_state": "queued",
        }
    return listener


MAX_REMOTE_MAIL_BYTES = 64 * 1024
# One cap, both routes: what a worker cannot be handed must not be accepted
# from a sender. A local send is written straight into the recipient's inbox,
# so an oversized one is only discovered when that worker tries to read it,
# where nothing can be reported back.
MAX_MAIL_BYTES = MAX_REMOTE_MAIL_BYTES
DIRECT_CONTROL_KINDS = {"discard.notice", "ping", "reply", "request"}
DIRECT_MAIL_KINDS = DIRECT_CONTROL_KINDS | {"delivery_failed"}
LOCAL_WORK_ITEM_STATE_FIELDS = (
    "item_id",
    "item_ref",
    "item_key",
    "title",
    "description",
    "acceptance",
    "tags",
    "keywords",
    "attachment_refs",
    "status",
    "assignee",
    "ordinal",
    "started_at",
    "cancelled_at",
    "cancelled_by",
    "result",
    "result_ref",
    "blocked_reason",
    "cancel_reason",
    "review",
)
_UNREPORTED_DELIVERY_VALUE = object()


def _local_work_item_state(value: Mapping[str, Any]) -> dict[str, Any]:
    """Authoritative local item state, excluding revisions and timestamps."""

    state = {field: value.get(field) for field in LOCAL_WORK_ITEM_STATE_FIELDS}
    state["item_ref"] = plan_node_identity_ref(value.get("item_ref"))
    state["tags"] = sorted({str(item) for item in value.get("tags") or ()})
    state["keywords"] = sorted(
        {str(item) for item in value.get("keywords") or ()}
    )
    state["attachment_refs"] = attachment_refs(value)
    return state


def _sender_identity(value: Mapping[str, Any] | None, *, fallback: str) -> dict[str, str]:
    """Normalize who wrote a message: kind user|worker|agent|system, a display label, and the worker name."""
    row = dict(value or {})
    kind = str(row.get("kind") or "").strip().lower()
    worker_name = str(row.get("worker_name") or "").strip().lower()
    if kind not in {"user", "worker", "agent", "system"}:
        kind = "worker" if fallback not in {"control-plane", ""} else "system"
        worker_name = worker_name or (fallback if kind == "worker" else "")
    label = str(row.get("label") or "").strip() or (worker_name or fallback or "control-plane")
    return {"kind": kind, "label": bounded_text(label, field="sender_identity.label", maximum=512), "worker_name": worker_name}


def _future(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=max(1, seconds))).isoformat().replace(
        "+00:00", "Z"
    )


def _wake_deadline_passed(subscription: Mapping[str, Any], now: str) -> bool:
    deadline = str(subscription.get("wake_ack_deadline_at") or "")
    if not deadline:
        return True
    try:
        return parse_utc(deadline) <= parse_utc(now)
    except DomainError:
        return True


def _renew_overdue_wake_deadline(subscription: dict[str, Any]) -> None:
    """Push the deadline out with the same bounded backoff a retry uses.

    Each overdue observation is one more check, not one more submission, so
    it keeps its own counter and leaves wake_attempts to the submissions.
    """

    checks = int(subscription.get("wake_overdue_checks") or 0) + 1
    subscription["wake_overdue_checks"] = checks
    subscription["wake_ack_deadline_at"] = _future(
        min(WAKE_ACK_BASE_SECONDS * (2 ** min(checks - 1, 10)), WAKE_ACK_MAX_SECONDS)
    )


def _overdue_wake_state(subscription: Mapping[str, Any]) -> tuple[str, str]:
    """The terminal wake condition a session can sit in, and since when.

    queued_overdue: the native queue still holds the submission for longer
    than the report threshold, a fact about the queue and not a verdict on
    the session. consumed_overdue: the session took the submission and took
    its one retry, acknowledging neither, for longer than the threshold.
    failed_overdue: the session took the submission without acknowledging
    it and the one retry never reached the queue, reported at once. The name
    follows the persisted wake_delivery_state, so the report says what
    happened rather than one story for two transport outcomes. All three
    hold the one-retry ceiling. This is the attention state; it is
    derived beside presence and never rewrites it. Anything else returns
    empty.
    """

    state = str(subscription.get("wake_delivery_state") or "")
    if state == "queued":
        since = str(subscription.get("wake_queued_overdue_since") or "")
        if not since or _seconds_since(since) < WAKE_OVERDUE_GRACE_SECONDS:
            return "", ""
        # Due. The notice says the queue still holds the wake, so it is
        # derived only from a listing taken after the report became due:
        # the backoff can leave the last listing at T+240 while the report
        # falls due at T+360, and the wake may have left in between.
        confirmed = str(subscription.get("wake_queued_confirmed_at") or "")
        if confirmed and _seconds_since(confirmed) <= _seconds_since(since) - WAKE_OVERDUE_GRACE_SECONDS:
            return "queued_overdue", since
        return "", ""
    exhausted_since = str(subscription.get("wake_retry_exhausted_since") or "")
    if int(subscription.get("wake_consumed_retries") or 0) >= 1 and exhausted_since:
        if state == "failed":
            # An observed transport failure: the retry never reached the
            # queue. Waiting adds no evidence, so no threshold.
            return "failed_overdue", exhausted_since
        if state == "consumed" and _seconds_since(exhausted_since) >= WAKE_OVERDUE_GRACE_SECONDS:
            return "consumed_overdue", exhausted_since
    return "", ""


def _observe_outstanding_wake(
    subscription: dict[str, Any], *, listed: bool, expected: bool, now: str
) -> None:
    """Record what a queue listing says about the outstanding wake.

    Listed means the native queue still holds the submission and the model
    has not taken it: the wake is queued. Not listed, for a wake the queue
    had accepted, means the session consumed the turn without acknowledging
    it: the wake is consumed. Neither is delivery, and neither clears the
    deadline. A wake still listed past its deadline is stamped overdue once
    and its deadline renewed with backoff, so the relay re-reads the queue
    at a bounded cadence instead of enqueueing again. A submission
    still in flight (attempting) or refused (failed) that is not listed keeps
    its state, and so does a queued wake whose submission id was never known
    (expected is False): the listing alone cannot tell those from consumed.
    """

    state = str(subscription.get("wake_delivery_state") or "")
    if listed:
        subscription["wake_delivery_state"] = "queued"
        # The listing is the evidence: remember when the queue was last seen
        # holding this submission, so a queued report is only ever derived
        # from a listing taken after the report became due, never from an
        # earlier snapshot the wake may have left since.
        subscription["wake_queued_confirmed_at"] = now
        if _wake_deadline_passed(subscription, now):
            subscription.setdefault("wake_queued_overdue_since", now)
            _renew_overdue_wake_deadline(subscription)
        return
    retries_spent = int(subscription.get("wake_consumed_retries") or 0) >= 1
    if state == "queued":
        if not expected:
            # The submission id was never known, so an empty listing says
            # nothing. Treat the wake as still queued and, past the deadline,
            # overdue.
            if _wake_deadline_passed(subscription, now):
                subscription.setdefault("wake_queued_overdue_since", now)
                _renew_overdue_wake_deadline(subscription)
            return
        # The queue accepted it and no longer lists it: the session took the
        # turn and did not acknowledge. This is the persisted truth whether
        # it is the first submission or the accepted retry.
        subscription["wake_delivery_state"] = "consumed"
        subscription.setdefault("wake_consumed_observed_at", now)
        state = "consumed"
        if not retries_spent:
            # The deadline is left as it stands: past it, the one bounded
            # retry is due now, and prepare_worker_session_wake counts it.
            return
    if retries_spent and state == "failed":
        # The one retry never reached the queue. That is an observed
        # transport failure and the listing has just confirmed nothing sits
        # in the queue: the terminal condition holds now, no deadline waits
        # on it; a failed retry is reported at once.
        subscription.setdefault("wake_retry_exhausted_since", now)
        if _wake_deadline_passed(subscription, now):
            _renew_overdue_wake_deadline(subscription)
        return
    if retries_spent and state == "consumed":
        # The retry was taken as well. Not listed and past the deadline is
        # the terminal condition. Stamp it once and renew the deadline so the
        # relay re-reads at a bounded cadence.
        if _wake_deadline_passed(subscription, now):
            subscription.setdefault("wake_retry_exhausted_since", now)
            _renew_overdue_wake_deadline(subscription)


def _bounded_message_refs(
    values: Iterable[Any], *, limit: int = MAX_WAKE_MESSAGE_REFS
) -> list[str]:
    refs: list[str] = []
    for value in values:
        ref = bounded_text(value, field="message_ref", maximum=256)
        if ref and ref not in refs:
            refs.append(ref)
    return refs[-max(1, int(limit)) :]


def _relative_scope(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or ".." in path.parts:
        raise DomainError(
            "field_scope_invalid",
            "A source scope must be a relative path inside the shared field.",
            details={"scope": text},
        )
    return path.as_posix().rstrip("/")


def _scopes_overlap(left: str, right: str) -> bool:
    return left == right or left.startswith(f"{right}/") or right.startswith(f"{left}/")


def _require_expected(record: Mapping[str, Any], expected_revision: int | None) -> None:
    if expected_revision is None:
        return
    actual = int(record.get("revision") or 0)
    if actual != int(expected_revision):
        raise DomainError(
            "field_revision_conflict",
            "The shared-field record changed after it was read.",
            status=409,
            details={"expected_revision": int(expected_revision), "current_revision": actual},
        )


def _seconds_since(stamp: str) -> int:
    """Whole seconds since an ISO stamp, 0 if it cannot be read."""

    try:
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return 0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - moment).total_seconds()))


def _contains_identity(actual: Any, expected: Any) -> bool:
    """Return whether a stored envelope still proves a declared replay identity."""

    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping):
            return False
        return all(
            key in actual and _contains_identity(actual[key], value)
            for key, value in expected.items()
        )
    return actual == expected


class SharedFieldStore:
    """Canonical project state shared directly by local workers.

    KDCube does not own this store. A local relay may publish bounded projections
    and service events from its outbox. Active work records remain under ``root``;
    canonical journal bodies live in their separately bound Git repository.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.control = self.root / ".problem-board"

    @property
    def manifest_path(self) -> Path:
        return self.control / "manifest.json"

    def initialize(self, *, field_id: str | None = None, title: str = "Shared work field") -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        with exclusive_lock(self.control / "locks" / "field.lock"):
            existing = read_json(self.manifest_path, required=False)
            if existing:
                return existing
            now = utc_now()
            record = {
                "schema": FIELD_SCHEMA,
                "field_id": component(field_id or new_id("field"), field="field_id"),
                "title": bounded_text(title, field="title", maximum=512, required=True),
                "revision": 1,
                "created_at": now,
                "updated_at": now,
            }
            atomic_write_json(self.manifest_path, record)
            for relative in ("workers", "projects", "outbox/pending", "outbox/leased", "outbox/sent", "outbox/refused"):
                (self.control / relative).mkdir(parents=True, exist_ok=True, mode=0o700)
            return record

    def manifest(self) -> dict[str, Any]:
        return read_json(self.manifest_path)

    def _project_dir(self, project_id: str) -> Path:
        return self.control / "projects" / component(project_id, field="project_id")

    def _project_path(self, project_id: str) -> Path:
        return self._project_dir(project_id) / "project.json"

    def _project_lock(self, project_id: str) -> Path:
        return self._project_dir(project_id) / ".project.lock"

    def _project_lock_context(self, project_id: str):
        return exclusive_lock(self._project_lock(project_id))

    def create_project(
        self,
        *,
        project_id: str | None,
        title: str,
        goal: str,
        owner: str,
    ) -> dict[str, Any]:
        self.initialize()
        clean_id = component(project_id or new_id("project"), field="project_id")
        path = self._project_path(clean_id)
        with exclusive_lock(self._project_lock(clean_id)):
            if path.exists():
                raise DomainError(
                    "field_project_exists",
                    "A project with this id already exists in the shared field.",
                    status=409,
                    details={"project_id": clean_id},
                )
            now = utc_now()
            record = {
                "schema": FIELD_SCHEMA,
                "project_id": clean_id,
                "project_ref": make_ref("project", clean_id),
                "title": bounded_text(title, field="title", maximum=1000, required=True),
                "goal": bounded_text(goal, field="goal", maximum=256_000, required=True),
                "owner": bounded_text(owner, field="owner", maximum=512, required=True),
                "status": "planning",
                "revision": 1,
                "created_at": now,
                "updated_at": now,
            }
            atomic_write_json(path, record)
            for relative in (
                "assignments",
                "mail/ignored",
                "sessions",
                "journals",
                "events",
                "scope-leases/active",
                "scope-leases/settled",
                "idempotency/mail",
                "idempotency/assignment-report",
            ):
                (path.parent / relative).mkdir(parents=True, exist_ok=True, mode=0o700)
            self._record_event_unlocked(
                clean_id,
                kind="project.created",
                summary=f"Project {record['title']} was created.",
                actor=owner,
                metadata={"project_ref": record["project_ref"]},
            )
            return record

    def read_project(self, project_id: str) -> dict[str, Any]:
        return read_json(self._project_path(component(project_id, field="project_id")))

    def list_projects(self) -> list[dict[str, Any]]:
        return [
            read_json(path)
            for path in sorted((self.control / "projects").glob("*/project.json"))
        ]

    @staticmethod
    def _validate_graph(items: Sequence[Mapping[str, Any]], dependencies: Sequence[Mapping[str, Any]]) -> None:
        ids = {
            plan_node_identity_ref(row.get("item_ref"))
            for row in items
        }
        if "" in ids or len(ids) != len(items):
            raise DomainError("field_plan_invalid", "Every work item requires a unique node URI.")
        incoming: dict[str, set[str]] = {item_id: set() for item_id in ids}
        outgoing: dict[str, set[str]] = {item_id: set() for item_id in ids}
        for edge in dependencies:
            item_id = plan_node_identity_ref(edge.get("item_ref"))
            depends_on = plan_node_identity_ref(edge.get("depends_on_ref"))
            if item_id not in ids or depends_on not in ids or item_id == depends_on:
                raise DomainError(
                    "field_dependency_invalid",
                    "Every dependency must join two different items in this plan.",
                    details={"item_ref": item_id, "depends_on_ref": depends_on},
                )
            incoming[item_id].add(depends_on)
            outgoing[depends_on].add(item_id)
        ready = [item_id for item_id, blockers in incoming.items() if not blockers]
        visited = 0
        while ready:
            resolved = ready.pop()
            visited += 1
            for item_id in outgoing[resolved]:
                incoming[item_id].remove(resolved)
                if not incoming[item_id]:
                    ready.append(item_id)
        if visited != len(ids):
            raise DomainError("field_dependency_cycle", "The proposed work graph contains a cycle.")

    def _bucketed_plans(self, project_id: str) -> BucketedPlanStore:
        return BucketedPlanStore(self._project_dir(project_id) / "plans")

    @staticmethod
    def _plan_item(plan: Mapping[str, Any], item_ref: str) -> dict[str, Any] | None:
        requested = plan_node_identity_ref(item_ref)
        return next(
            (
                item
                for item in plan.get("items") or []
                if plan_node_identity_ref(item.get("item_ref")) == requested
            ),
            None,
        )

    def _write_plan_unlocked(self, project_id: str, plan: Mapping[str, Any]) -> None:
        clean_project = component(project_id, field="project_id")
        plan_id = component(plan.get("plan_id"), field="plan_id")
        bucketed = self._bucketed_plans(clean_project)
        state = bucketed.state()
        if state.get("state") == "migrating":
            raise DomainError(
                "field_plan_migration_in_progress",
                "Plan storage is being migrated. Retry after it completes.",
                status=409,
                details={"retryable": True},
            )
        if bucketed.is_bucketed():
            bucketed.write(plan)
            return
        raise DomainError(
            "field_plan_migration_required",
            "This project's plan storage must be migrated before it can be written.",
            status=409,
        )

    def put_plan(
        self,
        project_id: str,
        *,
        items: Sequence[Mapping[str, Any]],
        dependencies: Sequence[Mapping[str, Any]] = (),
        authored_by: str,
        rationale: str = "",
        plan_id: str | None = None,
        expected_project_revision: int | None = None,
    ) -> dict[str, Any]:
        clean_project = (
            component(project_id, field="project_id")
            if str(project_id or "").strip()
            else ""
        )
        normalized_items: list[dict[str, Any]] = []
        now = utc_now()
        for position, raw in enumerate(items):
            item_key = bounded_text(
                raw.get("item_key") or f"W{position + 1}",
                field="item_key",
                maximum=128,
                required=True,
            )
            # Same shape as an appended item: the key leads because the id
            # carries it, so a plan written in one go and a plan grown one item
            # at a time produce refs that read alike.
            item_id = component(
                raw.get("item_id")
                or new_keyed_id(item_key, raw.get("title") or "", fallback="node"),
                field="item_id",
            )
            item_ref = make_plan_node_ref(
                timestamp=now,
                key=item_key,
                semantic_name=str(raw.get("title") or item_id),
            )
            normalized_items.append(
                {
                    "item_id": item_id,
                    "item_ref": item_ref,
                    "item_key": item_key,
                    "title": bounded_text(raw.get("title"), field="title", maximum=1000, required=True),
                    "description": bounded_text(
                        raw.get("description"), field="description", maximum=256_000
                    ),
                    "acceptance": list(raw.get("acceptance") or []),
                    "tags": [str(value) for value in raw.get("tags") or []],
                    "keywords": [str(value) for value in raw.get("keywords") or []],
                    "attachment_refs": attachment_refs(raw),
                    "status": str(raw.get("status") or "draft"),
                    "assignee": str(raw.get("assignee") or ""),
                    "ordinal": position,
                    "revision": 1,
                    "created_at": now,
                    "updated_at": now,
                }
            )
        refs_by_id = {
            str(row["item_id"]): str(row["item_ref"]) for row in normalized_items
        }
        normalized_dependencies = [
            {
                "dependency_id": component(edge.get("dependency_id") or new_id("dep"), field="dependency_id"),
                "item_ref": refs_by_id.get(
                    component(edge.get("item_id"), field="item_id"), ""
                ),
                "depends_on_ref": refs_by_id.get(
                    component(
                        edge.get("depends_on_item_id"), field="depends_on_item_id"
                    ),
                    "",
                ),
                "relation": str(edge.get("relation") or "blocks"),
            }
            for edge in dependencies
        ]
        self._validate_graph(normalized_items, normalized_dependencies)
        clean_plan = component(plan_id or new_id("plan"), field="plan_id")
        with exclusive_lock(self._project_lock(clean_project)):
            project = self.read_project(clean_project)
            _require_expected(project, expected_project_revision)
            if self._bucketed_plans(clean_project).exists(clean_plan):
                raise DomainError("field_plan_exists", "A plan with this id already exists.", status=409)
            plan = {
                "schema": FIELD_SCHEMA,
                "plan_id": clean_plan,
                "project_ref": project["project_ref"],
                "status": "draft",
                "revision": 1,
                "authored_by": bounded_text(authored_by, field="authored_by", maximum=512, required=True),
                "rationale": bounded_text(rationale, field="rationale", maximum=128_000),
                "items": normalized_items,
                "dependencies": normalized_dependencies,
                "created_at": now,
                "updated_at": now,
            }
            plan["plan_ref"] = reference_for_record("plan", plan)
            self._write_plan_unlocked(clean_project, plan)
            project.update(
                current_plan_ref=plan["plan_ref"],
                revision=int(project["revision"]) + 1,
                updated_at=now,
            )
            atomic_write_json(self._project_path(clean_project), project)
            self._record_event_unlocked(
                clean_project,
                kind="plan.drafted",
                summary=f"A plan with {len(normalized_items)} work items was drafted.",
                actor=authored_by,
                metadata={"plan_ref": plan["plan_ref"], "item_count": len(normalized_items)},
            )
            self._publish_plan_nodes_unlocked(
                clean_project,
                expected_revisions={
                    str(item.get("item_ref") or ""): 0
                    for item in normalized_items
                },
            )
            return plan

    def _plan_from_ref(self, project_id: str, plan_ref: str) -> tuple[Path, dict[str, Any]]:
        parsed = parse_ref(plan_ref)
        if parsed.kind != "plan":
            raise DomainError("field_plan_ref_invalid", "Expected a work:plan reference.")
        clean_plan = component(parsed.object_id)
        bucketed = self._bucketed_plans(project_id)
        state = bucketed.state()
        if state.get("state") == "migrating":
            raise DomainError(
                "field_plan_migration_in_progress",
                "Plan storage is being migrated. Retry after it completes.",
                status=409,
                details={"retryable": True},
            )
        if bucketed.is_bucketed():
            return bucketed.manifest_path(clean_plan), bucketed.read(clean_plan)
        raise DomainError(
            "field_plan_migration_required",
            "This project's plan storage must be migrated before it can be read.",
            status=409,
        )

    def activate_plan(
        self,
        project_id: str,
        plan_ref: str,
        *,
        actor: str,
        expected_project_revision: int | None = None,
    ) -> dict[str, Any]:
        clean_project = component(project_id, field="project_id")
        with exclusive_lock(self._project_lock(clean_project)):
            project = self.read_project(clean_project)
            _require_expected(project, expected_project_revision)
            path, plan = self._plan_from_ref(clean_project, plan_ref)
            now = utc_now()
            plan["status"] = "active"
            plan["revision"] = int(plan.get("revision") or 0) + 1
            plan["updated_at"] = now
            dependency_items = {
                str(edge.get("item_ref") or "")
                for edge in plan.get("dependencies") or []
            }
            changed_refs: list[str] = []
            expected_revisions: dict[str, int] = {}
            for item in plan.get("items") or []:
                if item.get("status") == "draft":
                    item["status"] = (
                        "waiting"
                        if str(item.get("item_ref") or "")
                        in dependency_items
                        else "ready"
                    )
                    item["revision"] = int(item.get("revision") or 0) + 1
                    item["updated_at"] = now
                    item_ref = str(item.get("item_ref") or "")
                    changed_refs.append(item_ref)
                    expected_revisions[item_ref] = int(item["revision"]) - 1
            self._write_plan_unlocked(clean_project, plan)
            project.update(
                status="active",
                current_plan_ref=plan["plan_ref"],
                active_plan_ref=plan["plan_ref"],
                revision=int(project["revision"]) + 1,
                updated_at=now,
            )
            atomic_write_json(self._project_path(clean_project), project)
            self._record_event_unlocked(
                clean_project,
                kind="plan.activated",
                summary="The operator activated the current work graph.",
                actor=actor,
                metadata={"plan_ref": plan["plan_ref"]},
            )
            if changed_refs:
                self._publish_plan_nodes_unlocked(
                    clean_project,
                    item_refs=changed_refs,
                    expected_revisions=expected_revisions,
                )
            return {"project": project, "plan": plan}

    def current_plan(self, project_id: str) -> dict[str, Any]:
        project = self.read_project(project_id)
        plan_ref = str(project.get("active_plan_ref") or project.get("current_plan_ref") or "")
        if not plan_ref:
            return {"status": "empty", "revision": 0, "items": [], "dependencies": []}
        return self._plan_from_ref(component(project_id, field="project_id"), plan_ref)[1]

    @staticmethod
    def _assignment_object_id(assignment: Mapping[str, Any]) -> str:
        """Return the stable assignment identity without trusting URI wording."""

        assignment_ref = str(assignment.get("assignment_ref") or "").strip()
        if assignment_ref:
            try:
                parsed = parse_ref(assignment_ref)
            except DomainError:
                parsed = None
            if parsed is not None and parsed.kind == "assignment":
                return parsed.object_id
        return str(assignment.get("assignment_id") or "").strip()

    @classmethod
    def _assignment_identity_token(cls, assignment: Mapping[str, Any]) -> str:
        assignment_id = cls._assignment_object_id(assignment)
        if assignment_id:
            return assignment_id
        return str(
            assignment.get("assignment_ref")
            or assignment.get("work_ref")
            or "unidentified-assignment"
        )

    def materialize_assignment(
        self, project_id: str, assignment: Mapping[str, Any]
    ) -> dict[str, Any]:
        clean_project = component(project_id, field="project_id")
        assignment_ref = bounded_text(
            assignment.get("assignment_ref"),
            field="assignment_ref",
            maximum=256,
            required=True,
        )
        parsed_assignment = parse_ref(assignment_ref)
        if parsed_assignment.kind != "assignment":
            raise DomainError(
                "field_assignment_ref_invalid",
                "Expected a work:assignment reference.",
            )
        project_ref = bounded_text(
            assignment.get("project_ref"),
            field="project_ref",
            maximum=256,
            required=True,
        )
        if project_ref != make_ref("project", clean_project):
            raise DomainError(
                "field_assignment_project_mismatch",
                "The assignment belongs to another project.",
                status=409,
            )
        work_ref = bounded_text(
            assignment.get("versioned_work_ref")
            or assignment.get("work_version_ref")
            or assignment.get("work_ref"),
            field="work_ref",
            maximum=MAX_WORK_ITEM_REF_BYTES,
            required=True,
        )
        if parse_ref(work_ref).selector != "plan:node":
            raise DomainError(
                "field_item_ref_invalid", "Expected a work:item reference."
            )
        identity_ref = plan_node_identity_ref(
            assignment.get("identity_ref") or work_ref
        )
        if plan_node_identity_ref(work_ref) != identity_ref:
            raise DomainError(
                "field_assignment_work_ref_mismatch",
                "The assignment work version belongs to another item identity.",
                status=409,
                details={
                    "identity_ref": identity_ref,
                    "work_ref": work_ref,
                },
            )
        try:
            ownership_version = int(assignment.get("ownership_version"))
        except (TypeError, ValueError) as exc:
            raise DomainError(
                "field_assignment_version_invalid",
                "The assignment ownership version must be an integer.",
            ) from exc
        if ownership_version < 1:
            raise DomainError(
                "field_assignment_version_invalid",
                "The assignment ownership version must be positive.",
            )
        source = assignment.get("source") if isinstance(assignment.get("source"), Mapping) else {}
        repository_ref = str(source.get("repository_ref") or "")
        if repository_ref:
            repository_ref = normalize_repository_ref(
                repository_ref, field="source.repository_ref"
            )
        row = {
            "schema": ASSIGNMENT_SCHEMA,
            "assignment_id": parsed_assignment.object_id,
            "assignment_ref": assignment_ref,
            "project_ref": project_ref,
            "work_ref": work_ref,
            "identity_ref": identity_ref,
            "worker_name": component(
                assignment.get("worker_name"), field="worker_name"
            ).lower(),
            "ownership_version": ownership_version,
            "title": bounded_text(
                assignment.get("title"), field="title", maximum=1000, required=True
            ),
            "task": dict(assignment.get("task") or {}),
            "source": {
                "repository_ref": repository_ref,
                "base_commit": bounded_text(
                    source.get("base_commit"), field="source.base_commit", maximum=64
                ).lower(),
                "branch": bounded_text(
                    source.get("branch"), field="source.branch", maximum=512
                ),
            },
            # One assignment, several repositories (W278): the list is the
            # binding, the scalar source mirrors its first entry. A notice
            # from a service that predates the list carries only the scalar.
            "sources": _assignment_sources(assignment, repository_ref=repository_ref),
            "source_binding": str(assignment.get("source_binding") or "unspecified"),
            "state": "assigned",
            "received_at": utc_now(),
        }
        returned = assignment.get("returned")
        if isinstance(returned, Mapping):
            # A review return keeps the worker under a new ownership version
            # and the notice must say so and name the review to cite.
            row["returned"] = {
                "review_ref": str(returned.get("review_ref") or ""),
                "reason": str(returned.get("reason") or ""),
                "source_revision": int(returned.get("source_revision") or 0),
                "decision": "return",
            }
        row["content_hash"] = content_hash(
            {key: value for key, value in row.items() if key != "received_at"}
        )
        path = (
            self._project_dir(clean_project)
            / "assignments"
            / f"{component(parsed_assignment.object_id)}.json"
        )
        with exclusive_lock(self._project_lock(clean_project)):
            self.read_project(clean_project)
            current = read_json(path, required=False)
            current_version = int(current.get("ownership_version") or 0)
            if current_version > ownership_version:
                raise DomainError(
                    "field_assignment_stale",
                    "A newer assignment ownership version is already materialized.",
                    status=409,
                )
            if current_version == ownership_version:
                current_assignment_id = self._assignment_object_id(current)
                current_identity = {
                    "assignment_id": current_assignment_id,
                    "project_ref": str(current.get("project_ref") or ""),
                    "ownership_version": current_version,
                }
                replay_identity = {
                    "assignment_id": parsed_assignment.object_id,
                    "project_ref": project_ref,
                    "ownership_version": ownership_version,
                }
                if current_identity != replay_identity:
                    raise DomainError(
                        "field_assignment_identity_conflict",
                        "The stored assignment does not match this replay identity.",
                        status=409,
                        details={
                            "stored_identity": current_identity,
                            "replay_identity": replay_identity,
                        },
                    )
                row["received_at"] = str(
                    current.get("received_at") or row["received_at"]
                )
                row["content_hash"] = content_hash(
                    {key: value for key, value in row.items() if key != "received_at"}
                )
                refreshed = any(
                    current.get(key) != value
                    for key, value in row.items()
                    if key != "received_at"
                )
                if refreshed:
                    atomic_write_json(path, row)
                return {**row, "replayed": True, "refreshed": refreshed}
            atomic_write_json(path, row)
        return {**row, "replayed": False, "refreshed": False}

    def send_assignment_notice(
        self,
        project_id: str,
        *,
        assignment: Mapping[str, Any],
        recipient: str,
        sender_identity: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create one URI-only inbox notice for an assignment ownership version."""

        parsed_assignment = parse_ref(str(assignment.get("assignment_ref") or ""))
        if parsed_assignment.kind != "assignment":
            raise DomainError(
                "field_assignment_ref_invalid",
                "Expected a work:assignment reference.",
            )
        assignment_ref = str(parsed_assignment)
        try:
            ownership_version = int(assignment.get("ownership_version"))
        except (TypeError, ValueError) as exc:
            raise DomainError(
                "field_assignment_version_invalid",
                "The assignment ownership version must be an integer.",
            ) from exc
        if ownership_version < 1:
            raise DomainError(
                "field_assignment_version_invalid",
                "The assignment ownership version must be positive.",
            )
        assignment_id = parsed_assignment.object_id
        legacy_assignment_ref = f"work:assignment:{assignment_id}"
        # The notice must say WHICH work the assignment is for. It carried the
        # assignment ref three times and the work ref not at all, so a worker
        # that cannot read the assignment row learned only that it had been
        # given something.
        notice_work_ref = str(
            assignment.get("identity_ref") or assignment.get("work_ref") or ""
        ).strip()
        returned = assignment.get("returned")
        returned = dict(returned) if isinstance(returned, Mapping) else None
        if returned is not None:
            review_ref = str(returned.get("review_ref") or "")
            subject = (
                f"Returned from review: {notice_work_ref}"
                if notice_work_ref
                else "Returned from review"
            )
            body = (
                "Your work was returned from review. You keep the item, under a "
                "new ownership version.\n\n"
                f"Work item: {notice_work_ref or '(not recorded)'}\n"
                f"Assignment: {assignment_ref}\n"
                f"Ownership version: {ownership_version}\n"
                f"Review: {review_ref or '(not recorded)'}\n"
                f"Reason: {str(returned.get('reason') or '').strip() or '(none given)'}\n\n"
                "Rework it, then report against this assignment ref and this "
                "ownership version, citing this notice (its message_ref) as the "
                "source event, as you do for a fresh assignment. The review is "
                "the reason, not the source event: the return already spent it. "
                "Your report under the previous version is final and is not "
                "repeated.\n\n"
                "  pb coordinate project.plan.item --object-ref <project-ref> "
                "--payload-json '{\"item_key\":\"<Wn>\"}'\n"
                "  pb worker report --assignment-ref <assignment> "
                "--ownership-version <version> --source-event-ref <this notice>\n"
            )
            payload = {
                "assignment_id": assignment_id,
                "assignment_ref": assignment_ref,
                "work_ref": notice_work_ref,
                "ownership_version": ownership_version,
                "review_ref": review_ref,
                "reason": str(returned.get("reason") or ""),
                "expected_reaction": "resume_work",
            }
        else:
            subject = (
                f"Assignment available: {notice_work_ref}"
                if notice_work_ref
                else "Assignment available"
            )
            body = (
                "You have been assigned work.\n\n"
                f"Work item: {notice_work_ref or '(not recorded)'}\n"
                f"Assignment: {assignment_ref}\n"
                f"Ownership version: {ownership_version}\n\n"
                "Read the item, do the work, report against this assignment ref "
                "and this ownership version, and commit what you wrote. This is "
                "work to begin, not a notification to acknowledge.\n\n"
                "  pb coordinate project.plan.item --object-ref <project-ref> "
                "--payload-json '{\"item_key\":\"<Wn>\"}'\n"
                "  pb worker report --assignment-ref <assignment> "
                "--ownership-version <version>\n"
            )
            payload = {
                "assignment_id": assignment_id,
                "assignment_ref": assignment_ref,
                "work_ref": notice_work_ref,
                "ownership_version": ownership_version,
                "expected_reaction": "begin_work",
            }
        return self.send_mail(
            project_id,
            sender="control-plane",
            recipient=recipient,
            kind="assign",
            subject=subject,
            body=body,
            payload=payload,
            work_ref=notice_work_ref,
            correlation_id=assignment_ref,
            idempotency_key=(
                f"assignment-notice:{assignment_id}:{ownership_version}"
            ),
            idempotency_alias_keys=(
                f"assignment-notice:{assignment_ref}:{ownership_version}",
                f"assignment-notice:{legacy_assignment_ref}:{ownership_version}",
            ),
            sender_identity=sender_identity,
            idempotency_identity={
                "recipient": component(recipient, field="recipient").lower(),
                "kind": "assign",
                "payload": {
                    "assignment_id": assignment_id,
                    "ownership_version": ownership_version,
                },
            },
            idempotency_identity_aliases=(
                {
                    "recipient": component(recipient, field="recipient").lower(),
                    "kind": "assign",
                    "payload": {"assignment_ref": assignment_ref},
                },
                {
                    "recipient": component(recipient, field="recipient").lower(),
                    "kind": "assign",
                    "payload": {"assignment_ref": legacy_assignment_ref},
                },
            ),
        )

    def list_assignments(
        self, project_id: str, *, work_ref: str = ""
    ) -> list[dict[str, Any]]:
        rows = json_records(
            self._project_dir(component(project_id, field="project_id"))
            / "assignments"
        )
        if work_ref:
            identity_ref = plan_node_identity_ref(work_ref)
            rows = [
                row
                for row in rows
                if plan_node_identity_ref(row.get("identity_ref") or row.get("work_ref"))
                == identity_ref
            ]
        return sorted(
            rows,
            key=lambda row: int(row.get("ownership_version") or 0),
            reverse=True,
        )

    def _assignment_reconciliation_issue_path(
        self, project_id: str, assignment: Mapping[str, Any]
    ) -> Path:
        token = content_hash(
            {
                "assignment_id": self._assignment_identity_token(assignment),
                "ownership_version": str(assignment.get("ownership_version") or ""),
            }
        )
        return (
            self._project_dir(project_id)
            / "reconciliation"
            / "assignments"
            / f"{token}.json"
        )

    def _assignment_reconciliation_issue_paths(
        self, project_id: str, assignment: Mapping[str, Any]
    ) -> list[Path]:
        """Find the stable receipt plus URI-keyed receipts written before cutover."""

        stable_path = self._assignment_reconciliation_issue_path(
            project_id, assignment
        )
        root = stable_path.parent
        expected_identity = self._assignment_identity_token(assignment)
        expected_version = str(assignment.get("ownership_version") or "")
        paths = [stable_path]
        for path in sorted(root.glob("*.json")):
            if path == stable_path:
                continue
            row = read_json(path, required=False)
            if (
                row
                and self._assignment_identity_token(row) == expected_identity
                and str(row.get("ownership_version") or "") == expected_version
            ):
                paths.append(path)
        return paths

    def _assignment_reconciliation_record_unlocked(
        self, project_id: str, assignment: Mapping[str, Any]
    ) -> tuple[dict[str, Any], list[Path]]:
        stable_path = self._assignment_reconciliation_issue_path(
            project_id, assignment
        )
        stable_record = read_json(stable_path, required=False)
        if stable_record:
            return stable_record, [stable_path]
        paths = self._assignment_reconciliation_issue_paths(
            project_id, assignment
        )
        records = [
            (path, row)
            for path in paths
            if (row := read_json(path, required=False))
        ]
        if not records:
            return {}, paths
        source_hash = self.assignment_reconciliation_source_hash(assignment)
        for _path, row in records:
            if str(row.get("source_hash") or "") == source_hash:
                return row, paths
        return records[0][1], paths

    @classmethod
    def assignment_reconciliation_can_refresh_identity(
        cls,
        reconciliation: Mapping[str, Any],
        assignment: Mapping[str, Any],
    ) -> bool:
        """Allow one stopped URI-spelling conflict to re-enter materialization."""

        error = (
            reconciliation.get("error")
            if isinstance(reconciliation.get("error"), Mapping)
            else {}
        )
        return bool(
            error.get("code") == "field_assignment_identity_conflict"
            and cls._assignment_object_id(reconciliation)
            and cls._assignment_object_id(reconciliation)
            == cls._assignment_object_id(assignment)
            and str(reconciliation.get("project_ref") or "")
            == str(assignment.get("project_ref") or "")
            and str(reconciliation.get("ownership_version") or "")
            == str(assignment.get("ownership_version") or "")
        )

    @staticmethod
    def assignment_reconciliation_source_hash(
        assignment: Mapping[str, Any],
    ) -> str:
        """Hash fields whose repair can change materialization or notification."""

        return content_hash(
            {
                "assignment_ref": assignment.get("assignment_ref"),
                "project_ref": assignment.get("project_ref"),
                "work_ref": (
                    assignment.get("versioned_work_ref")
                    or assignment.get("work_version_ref")
                    or assignment.get("work_ref")
                ),
                "identity_ref": assignment.get("identity_ref"),
                "worker_name": assignment.get("worker_name"),
                "ownership_version": assignment.get("ownership_version"),
                "title": assignment.get("title"),
                "task": assignment.get("task"),
                "source": assignment.get("source"),
            }
        )

    def assignment_reconciliation_record(
        self, project_id: str, assignment: Mapping[str, Any]
    ) -> dict[str, Any]:
        clean_project = component(project_id, field="project_id")
        return self._assignment_reconciliation_record_unlocked(
            clean_project, assignment
        )[0]

    def send_assignment_reconciliation_failure_notice(
        self,
        project_id: str,
        assignment: Mapping[str, Any],
        *,
        recipient: str,
        error: Mapping[str, Any],
        sender_identity: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Tell the worker once that assigned work could not be materialized."""

        source_hash = self.assignment_reconciliation_source_hash(assignment)
        assignment_ref = str(assignment.get("assignment_ref") or "(not recorded)")
        work_ref = str(
            assignment.get("identity_ref")
            or assignment.get("work_ref")
            or "(not recorded)"
        )
        error_code = str(error.get("code") or "assignment_reconciliation_failed")
        error_message = str(error.get("message") or error_code)
        retry_when = {
            "condition": "assignment_source_changes",
            "source_hash": source_hash,
        }
        return self.send_mail(
            project_id,
            sender="control-plane",
            recipient=recipient,
            kind="assignment_reconciliation_failed",
            subject="Assignment could not be retrieved",
            body=(
                "Problem Board assigned work to this worker, but the local relay "
                "could not retrieve a usable assignment record.\n\n"
                f"Assignment: {assignment_ref}\n"
                f"Work item: {work_ref}\n"
                f"Ownership version: {assignment.get('ownership_version')}\n"
                f"Reason: {error_code}: {error_message}\n\n"
                "This source row is stopped. It becomes eligible again when its "
                "assignment source changes."
            ),
            payload={
                "assignment_ref": str(assignment.get("assignment_ref") or ""),
                "work_ref": str(
                    assignment.get("identity_ref")
                    or assignment.get("work_ref")
                    or ""
                ),
                "ownership_version": assignment.get("ownership_version"),
                "error": dict(error),
                "retry_when": retry_when,
                "expected_reaction": "inspect_assignment_failure",
            },
            work_ref="",
            correlation_id="",
            idempotency_key=(
                f"assignment-reconciliation-failed:{source_hash}"
            ),
            sender_identity=sender_identity,
            idempotency_identity={
                "recipient": component(recipient, field="recipient").lower(),
                "kind": "assignment_reconciliation_failed",
                "source_hash": source_hash,
            },
        )

    def record_assignment_reconciliation_success(
        self,
        project_id: str,
        assignment: Mapping[str, Any],
        *,
        notice: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Persist the proof that this exact source already produced its notice."""

        clean_project = component(project_id, field="project_id")
        path = self._assignment_reconciliation_issue_path(
            clean_project, assignment
        )
        now = utc_now()
        with exclusive_lock(self._project_lock(clean_project)):
            self.read_project(clean_project)
            current, receipt_paths = self._assignment_reconciliation_record_unlocked(
                clean_project, assignment
            )
            row = {
                "schema": ASSIGNMENT_RECONCILIATION_ISSUE_SCHEMA,
                "state": "notified",
                "eligible": False,
                "project_ref": make_ref("project", clean_project),
                "assignment_ref": str(assignment.get("assignment_ref") or ""),
                "ownership_version": assignment.get("ownership_version"),
                "worker_name": str(assignment.get("worker_name") or ""),
                "work_ref": str(
                    assignment.get("identity_ref")
                    or assignment.get("work_ref")
                    or ""
                ),
                "source_hash": self.assignment_reconciliation_source_hash(
                    assignment
                ),
                "notice_message_ref": str(notice.get("message_ref") or ""),
                "notice_replayed": bool(notice.get("replayed")),
                "retry_when": {
                    "condition": "assignment_source_changes",
                    "source_hash": self.assignment_reconciliation_source_hash(
                        assignment
                    ),
                },
                "first_seen_at": str(current.get("first_seen_at") or now),
                "completed_at": now,
            }
            atomic_write_json(path, row)
            for legacy_path in receipt_paths:
                if legacy_path != path:
                    legacy_path.unlink(missing_ok=True)
        return row

    def park_assignment_reconciliation_issue(
        self,
        project_id: str,
        assignment: Mapping[str, Any],
        *,
        error: DomainError | Mapping[str, Any],
        notice: Mapping[str, Any] | None = None,
        report_error: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Stop one unchanged bad row after reporting, or await that report."""

        clean_project = component(project_id, field="project_id")
        path = self._assignment_reconciliation_issue_path(
            clean_project, assignment
        )
        now = utc_now()
        with exclusive_lock(self._project_lock(clean_project)):
            self.read_project(clean_project)
            current, receipt_paths = self._assignment_reconciliation_record_unlocked(
                clean_project, assignment
            )
            source_hash = self.assignment_reconciliation_source_hash(assignment)
            error_record = (
                error.to_dict() if isinstance(error, DomainError) else dict(error)
            )
            report_pending = notice is None
            row = {
                "schema": ASSIGNMENT_RECONCILIATION_ISSUE_SCHEMA,
                "state": "report_pending" if report_pending else "stopped",
                "eligible": report_pending,
                "project_ref": make_ref("project", clean_project),
                "assignment_ref": str(assignment.get("assignment_ref") or ""),
                "ownership_version": assignment.get("ownership_version"),
                "worker_name": str(assignment.get("worker_name") or ""),
                "work_ref": str(
                    assignment.get("identity_ref")
                    or assignment.get("work_ref")
                    or ""
                ),
                "source_hash": source_hash,
                "error": error_record,
                "attempts": (
                    int(current.get("attempts") or 0)
                    if current.get("source_hash") == source_hash
                    else int(current.get("attempts") or 0) + 1
                ),
                "first_failed_at": str(current.get("first_failed_at") or now),
                "last_failed_at": now,
                "notice_message_ref": str((notice or {}).get("message_ref") or ""),
                "notice_replayed": bool((notice or {}).get("replayed")),
                "report_error": dict(report_error or {}),
                "retry_when": {
                    "condition": (
                        "failure_notice_succeeds"
                        if report_pending
                        else "assignment_source_changes"
                    ),
                    "source_hash": source_hash,
                },
            }
            atomic_write_json(path, row)
            for legacy_path in receipt_paths:
                if legacy_path != path:
                    legacy_path.unlink(missing_ok=True)
        return row

    def clear_assignment_reconciliation_issue(
        self, project_id: str, assignment: Mapping[str, Any]
    ) -> bool:
        clean_project = component(project_id, field="project_id")
        with exclusive_lock(self._project_lock(clean_project)):
            paths = self._assignment_reconciliation_issue_paths(
                clean_project, assignment
            )
            removed = False
            for path in paths:
                if path.exists():
                    path.unlink()
                    removed = True
        return removed

    def list_assignment_reconciliation_issues(
        self, project_id: str
    ) -> list[dict[str, Any]]:
        return [
            row
            for row in json_records(
                self._project_dir(component(project_id, field="project_id"))
                / "reconciliation"
                / "assignments"
            )
            if row.get("state") != "notified"
        ]

    def sync_active_assignments(
        self,
        project_id: str,
        *,
        worker_name: str,
        assignment_refs: Sequence[str],
    ) -> int:
        """Drop stale active projections absent from the worker heartbeat."""

        clean_project = component(project_id, field="project_id")
        clean_worker = component(worker_name, field="worker_name").lower()
        active_assignment_ids: set[str] = set()
        for value in assignment_refs:
            parsed = parse_ref(str(value or ""))
            if parsed.kind != "assignment":
                raise DomainError(
                    "field_assignment_ref_invalid",
                    "Expected a work:assignment reference.",
                )
            active_assignment_ids.add(parsed.object_id)
        removed = 0
        with exclusive_lock(self._project_lock(clean_project)):
            for row in self.list_assignments(clean_project):
                if (
                    str(row.get("worker_name") or "").lower() != clean_worker
                    or str(row.get("state") or "")
                    not in {"assigned", "working", "blocked"}
                    or self._assignment_object_id(row) in active_assignment_ids
                ):
                    continue
                parsed = parse_ref(str(row.get("assignment_ref") or ""))
                path = (
                    self._project_dir(clean_project)
                    / "assignments"
                    / f"{component(parsed.object_id)}.json"
                )
                path.unlink(missing_ok=True)
                for issue_path in self._assignment_reconciliation_issue_paths(
                    clean_project, row
                ):
                    issue_path.unlink(missing_ok=True)
                removed += 1
        return removed

    def update_work(
        self,
        project_id: str,
        item_ref: str,
        *,
        actor: str,
        patch: Mapping[str, Any],
        expected_revision: int,
        actor_kind: str = "",
    ) -> dict[str, Any]:
        clean_project = component(project_id, field="project_id")
        parsed = parse_ref(item_ref)
        if parsed.selector != "plan:node":
            raise DomainError("field_item_ref_invalid", "Expected a work:item reference.")
        allowed = {
            # A corrected label does not rename the durable item identity.
            "title",
            "status",
            "assignee",
            "description",
            "acceptance",
            "tags",
            "keywords",
            "result",
            "blocked_reason",
            # What the agent finished but could not check itself, and what the
            # operator should look at. An item that says "finished, please look"
            # without saying at what leaves the operator to rediscover the work.
            "review",
        }
        unknown = sorted(set(patch) - allowed)
        if unknown:
            raise DomainError("field_patch_invalid", "The work patch contains unsupported fields.", details={"fields": unknown})
        with exclusive_lock(self._project_lock(clean_project)):
            project = self.read_project(clean_project)
            path, plan = self._plan_from_ref(clean_project, str(project.get("active_plan_ref") or project.get("current_plan_ref")))
            row = self._plan_item(plan, item_ref)
            if row is None:
                raise DomainError("field_item_not_found", "The work item does not exist.", status=404)
            if str(row.get("status") or "") == "cancelled":
                raise DomainError(
                    "field_item_cancelled",
                    "A cancelled item is terminal. Create a new item if the "
                    "work resumes, so the stated reason stays true.",
                    status=409,
                    details={"cancel_reason": str(row.get("cancel_reason") or "")},
                )
            _require_expected(row, expected_revision)
            # An agent finishing work cannot also accept it. Before this there
            # were two bad choices: mark it done, which breaks done meaning
            # confirmed, or leave it working, which reads as activity that is
            # not happening. awaiting_operator is the honest third option, and
            # it is only honest if the agent cannot walk it to done itself.
            acceptance_actor_kind = str(actor_kind or "").strip().lower()
            if (
                str(row.get("status") or "") == "awaiting_operator"
                and str(patch.get("status") or "") == "done"
                and acceptance_actor_kind != "user"
            ):
                raise DomainError(
                    "field_acceptance_is_the_operators",
                    "Only a server-attributed signed-in user can accept work. "
                    "The actor label records who acted; it does not grant "
                    "operator authority.",
                    status=403,
                    details={
                        "status": "awaiting_operator",
                        "actor_label": str(actor or ""),
                        "actor_kind": acceptance_actor_kind or "unattributed",
                        "required_actor_kind": "user",
                        "authority_source": "server-attributed sender identity",
                    },
                )
            previous_status = str(row.get("status") or "")
            candidate = {**row, **patch}
            now = utc_now()
            if candidate.get("status") == "working" and not candidate.get("started_at"):
                candidate["started_at"] = now
            if _local_work_item_state(row) == _local_work_item_state(candidate):
                return {**dict(row), "changed": False}
            candidate["revision"] = int(row.get("revision") or 0) + 1
            candidate["updated_at"] = now
            row.clear()
            row.update(candidate)
            # A dependency is satisfied only by work that actually finished.
            # This compared against "completed", which no plan item has ever
            # been: items finish as "done". The set was therefore always
            # empty, so an item only became ready if it had no blockers at
            # all, and everything with a satisfied dependency stayed waiting
            # forever. Nothing failed, the board simply never promoted
            # anything, which is invisible unless you look for it.
            #
            # Cancelled work deliberately does not satisfy a dependency. An
            # item that depends on abandoned work usually needs rethinking
            # rather than silently starting, so its dependents stay blocked
            # and can name the cancelled blocker.
            satisfied = {
                str(item.get("item_ref") or "")
                for item in plan.get("items") or []
                if item.get("status") == "done"
            }
            blockers: dict[str, set[str]] = {}
            for edge in plan.get("dependencies") or []:
                blockers.setdefault(
                    str(edge.get("item_ref") or ""), set()
                ).add(
                    str(
                        edge.get("depends_on_ref") or ""
                    )
                )
            promoted: list[dict[str, Any]] = []
            for candidate in plan.get("items") or []:
                candidate_id = str(
                    candidate.get("item_ref") or ""
                )
                if candidate.get("status") == "waiting" and blockers.get(candidate_id, set()) <= satisfied:
                    candidate["status"] = "ready"
                    candidate["revision"] = int(candidate.get("revision") or 0) + 1
                    candidate["updated_at"] = row["updated_at"]
                    promoted.append(candidate)
            plan["revision"] = int(plan.get("revision") or 0) + 1
            plan["updated_at"] = row["updated_at"]
            self._write_plan_unlocked(clean_project, plan)
            project["revision"] = int(project.get("revision") or 0) + 1
            project["updated_at"] = row["updated_at"]
            atomic_write_json(self._project_path(clean_project), project)
            self._record_event_unlocked(
                clean_project,
                kind="work.updated",
                summary=f"{row['item_key']} moved to {row['status']}.",
                actor=actor,
                work_ref=row["item_ref"],
                metadata={
                    "revision": row["revision"],
                    "assignee": str(row.get("assignee") or ""),
                    "from_status": previous_status,
                    "to_status": str(row.get("status") or ""),
                },
            )
            # Promotion used to change an item's status and record nothing, so
            # the history could not say what became ready. That is the single
            # transition a "where are we now" report most needs, and it was the
            # only one leaving no trace.
            for candidate in promoted:
                self._record_event_unlocked(
                    clean_project,
                    kind="work.updated",
                    summary=f"{candidate['item_key']} moved to ready.",
                    actor=actor,
                    work_ref=candidate["item_ref"],
                    metadata={
                        "revision": candidate["revision"],
                        "assignee": str(candidate.get("assignee") or ""),
                        "from_status": "waiting",
                        "to_status": "ready",
                        "promoted_by": row["item_ref"],
                    },
                )
            changed = [row, *promoted]
            self._publish_plan_nodes_unlocked(
                clean_project,
                item_refs=[str(item.get("item_ref") or "") for item in changed],
                expected_revisions={
                    str(item.get("item_ref") or ""): int(item.get("revision") or 1) - 1
                    for item in changed
                },
            )
            return dict(row)

    def append_item_note(
        self,
        project_id: str,
        item_ref: str,
        *,
        actor: str,
        actor_kind: str,
        text: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Append one note to a work item. Notes are never edited or removed.

        A note is where the reasoning behind a decision lives, next to the
        decision rather than in a message thread nobody finds later. It is
        append-only for the same reason a journal entry is: a record that can
        be quietly rewritten is not a record, and the value of a note is that
        it says what someone thought at the time.

        Fenced by the item's own revision, so two people commenting at once
        cannot lose one of the notes.
        """
        clean_project = component(project_id, field="project_id")
        parsed = parse_ref(item_ref)
        if parsed.selector != "plan:node":
            raise DomainError("field_item_ref_invalid", "Expected a work:item reference.")
        kind = str(actor_kind or "").strip().lower()
        if kind not in {"user", "worker", "agent", "system"}:
            raise DomainError(
                "field_note_author_kind_invalid",
                "A note author is a user, worker, agent, or system.",
                details={"actor_kind": actor_kind},
            )
        body = bounded_text(text, field="text", maximum=8000, required=True)
        with exclusive_lock(self._project_lock(clean_project)):
            project = self.read_project(clean_project)
            path, plan = self._plan_from_ref(
                clean_project,
                str(project.get("active_plan_ref") or project.get("current_plan_ref")),
            )
            row = self._plan_item(plan, item_ref)
            if row is None:
                raise DomainError("field_item_not_found", "The work item does not exist.", status=404)
            _require_expected(row, expected_revision)
            note_id = new_id("note")
            note = {
                "note_id": note_id,
                "author": bounded_text(actor, field="actor", maximum=512, required=True),
                "author_kind": kind,
                "text": body,
                "created_at": utc_now(),
            }
            note["note_ref"] = reference_for_record("note", note)
            notes = list(row.get("notes") or [])
            notes.append(note)
            row["notes"] = notes
            row["revision"] = int(row.get("revision") or 0) + 1
            row["updated_at"] = note["created_at"]
            plan["revision"] = int(plan.get("revision") or 0) + 1
            plan["updated_at"] = note["created_at"]
            self._write_plan_unlocked(clean_project, plan)
            project["revision"] = int(project["revision"]) + 1
            project["updated_at"] = note["created_at"]
            atomic_write_json(self._project_path(clean_project), project)
            self._record_event_unlocked(
                clean_project,
                kind="work.noted",
                summary=f"A note was added to {row['item_key']}.",
                actor=actor,
                work_ref=row["item_ref"],
                metadata={"note_ref": note["note_ref"], "author_kind": kind},
            )
            return dict(note)

    def list_item_notes(self, project_id: str, item_ref: str) -> list[dict[str, Any]]:
        """Every note on one item, oldest first."""
        clean_project = component(project_id, field="project_id")
        parsed = parse_ref(item_ref)
        if parsed.selector != "plan:node":
            raise DomainError("field_item_ref_invalid", "Expected a work:item reference.")
        plan = self.current_plan(clean_project)
        row = self._plan_item(plan, item_ref)
        if row is None:
            raise DomainError("field_item_not_found", "The work item does not exist.", status=404)
        return [dict(note) for note in row.get("notes") or []]

    def cancel_work_item(
        self,
        project_id: str,
        item_ref: str,
        *,
        actor: str,
        reason: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Abandon an item with a stated reason, keeping it in the record.

        Cancelling rather than deleting, because a plan that can silently
        lose items cannot be trusted as a record of what happened, and this
        history is going to be read by people who were not here.

        The reason is required and kept in its own field, so it can never be
        confused with the result of work that actually happened. Cancelling
        is terminal: the status cannot be edited back, because that would let
        the stated reason become a lie. Work that resumes is a new item.

        A cancelled item does not satisfy anything that depended on it. Its
        dependents stay blocked and can name it, since a dependency on
        abandoned work usually needs rethinking rather than silently starting.
        """
        clean_project = component(project_id, field="project_id")
        parsed = parse_ref(item_ref)
        if parsed.selector != "plan:node":
            raise DomainError("field_item_ref_invalid", "Expected a work:item reference.")
        clean_reason = bounded_text(reason, field="reason", maximum=4000, required=True)
        with exclusive_lock(self._project_lock(clean_project)):
            project = self.read_project(clean_project)
            path, plan = self._plan_from_ref(
                clean_project,
                str(project.get("active_plan_ref") or project.get("current_plan_ref")),
            )
            row = self._plan_item(plan, item_ref)
            if row is None:
                raise DomainError("field_item_not_found", "The work item does not exist.", status=404)
            if str(row.get("status") or "") == "cancelled":
                raise DomainError(
                    "field_item_already_cancelled",
                    "This item was already cancelled.",
                    status=409,
                    details={"cancel_reason": str(row.get("cancel_reason") or "")},
                )
            _require_expected(row, expected_revision)
            now = utc_now()
            row.update(
                status="cancelled",
                cancel_reason=clean_reason,
                cancelled_at=now,
                cancelled_by=bounded_text(actor, field="actor", maximum=512, required=True),
                revision=int(row.get("revision") or 0) + 1,
                updated_at=now,
            )
            plan["revision"] = int(plan.get("revision") or 0) + 1
            plan["updated_at"] = now
            self._write_plan_unlocked(clean_project, plan)
            project["revision"] = int(project["revision"]) + 1
            project["updated_at"] = now
            atomic_write_json(self._project_path(clean_project), project)
            self._record_event_unlocked(
                clean_project,
                kind="work.cancelled",
                summary=f"{row['item_key']} was cancelled.",
                actor=actor,
                work_ref=row["item_ref"],
                metadata={"reason": clean_reason},
            )
            self._publish_plan_nodes_unlocked(
                clean_project,
                item_refs=[str(row.get("item_ref") or "")],
                expected_revisions={
                    str(row.get("item_ref") or ""): int(row.get("revision") or 1) - 1
                },
            )
            return dict(row)

    def add_work_item(
        self,
        project_id: str,
        *,
        actor: str,
        title: str,
        description: str = "",
        acceptance: Sequence[str] = (),
        tags: Sequence[str] = (),
        keywords: Sequence[str] = (),
        attachment_refs: Sequence[str] = (),
        assignee: str = "",
        status: str = "ready",
        depends_on: Sequence[str] = (),
        item_id: str | None = None,
        item_key: str | None = None,
        stem: str = "",
        expected_project_revision: int | None = None,
    ) -> dict[str, Any]:
        """Append one item to the active plan, keeping every other item intact.

        Until this existed a plan could be created but never grown. ``put_plan``
        only creates, refusing an existing plan id, and re-putting the whole
        document normalizes every item back to revision 1 with a fresh
        timestamp and drops anything outside its own field list, so notes,
        cancellations, and start times would be erased to add a line.

        The practical result was a plan that stopped describing the work the
        moment the work outgrew it: new items lived in conversation instead,
        which is precisely where a record must not live. So this appends, and
        nothing already in the plan is rewritten.

        The item key is allocated rather than supplied, because two people
        adding work at once would otherwise both reach for the next number.
        """
        clean_project = component(project_id, field="project_id")
        clean_actor = bounded_text(actor, field="actor", maximum=512, required=True)
        with exclusive_lock(self._project_lock(clean_project)):
            project = self.read_project(clean_project)
            _require_expected(project, expected_project_revision)
            path, plan = self._plan_from_ref(
                clean_project,
                str(project.get("active_plan_ref") or project.get("current_plan_ref")),
            )
            items = list(plan.get("items") or [])
            taken = {str(row.get("item_id") or "") for row in items}
            # The key is allocated before the id, because the id carries it.
            if item_key:
                key = bounded_text(item_key, field="item_key", maximum=128, required=True)
                if any(row.get("item_key") == key for row in items):
                    raise DomainError(
                        "field_item_key_exists",
                        "A work item with this key is already in the plan.",
                        status=409,
                    )
            else:
                used = set()
                for row in items:
                    text = str(row.get("item_key") or "")
                    if text.startswith("W") and text[1:].isdigit():
                        used.add(int(text[1:]))
                key = f"W{(max(used) + 1) if used else 1}"
            if item_id:
                clean_item = component(item_id, field="item_id")
                if clean_item in taken:
                    raise DomainError(
                        "field_item_exists",
                        "A work item with this id is already in the plan.",
                        status=409,
                    )
            else:
                # Three things a reader of a bare ref should not have to look up:
                # where this sits in the sequence, what it is about, and when it
                # was made. The plan-unique key supplies the first and the
                # uniqueness, so no random suffix is needed here.
                clean_item = component(
                    new_keyed_id(key, stem or title, fallback="node"), field="item_id"
                )
            now = utc_now()
            row = {
                "item_id": clean_item,
                "item_ref": make_plan_node_ref(
                    timestamp=now,
                    key=key,
                    semantic_name=stem or title or clean_item,
                ),
                "item_key": key,
                "title": bounded_text(title, field="title", maximum=1000, required=True),
                "description": bounded_text(description, field="description", maximum=256_000),
                "acceptance": [str(value) for value in acceptance],
                "tags": [str(value) for value in tags],
                "keywords": [str(value) for value in keywords],
                "attachment_refs": [
                    str(value).strip()
                    for value in attachment_refs
                    if str(value or "").strip()
                ],
                "status": str(status or "ready"),
                "assignee": str(assignee or ""),
                "ordinal": len(items),
                "revision": 1,
                "created_at": now,
                "updated_at": now,
            }
            items.append(row)
            dependencies = list(plan.get("dependencies") or [])
            for blocker in depends_on:
                blocker_text = str(blocker or "").strip()
                blocker_row = next(
                    (
                        item
                        for item in items
                        if blocker_text
                        in {
                            str(item.get("item_ref") or ""),
                        }
                    ),
                    None,
                )
                if blocker_row is None:
                    raise DomainError(
                        "field_item_not_found",
                        "A dependency names a work item that is not in the plan.",
                        status=404,
                        details={"depends_on": blocker_text},
                    )
                dependencies.append(
                    {
                        "dependency_id": new_id("dep"),
                        "item_ref": row["item_ref"],
                        "depends_on_ref": str(blocker_row.get("item_ref") or ""),
                        "relation": "blocks",
                    }
                )
            self._validate_graph(items, dependencies)
            plan["items"] = items
            plan["dependencies"] = dependencies
            plan["revision"] = int(plan.get("revision") or 0) + 1
            plan["updated_at"] = now
            self._write_plan_unlocked(clean_project, plan)
            project["revision"] = int(project["revision"]) + 1
            project["updated_at"] = now
            atomic_write_json(self._project_path(clean_project), project)
            self._record_event_unlocked(
                clean_project,
                kind="work.added",
                summary=f"{key} was added to the plan.",
                actor=clean_actor,
                work_ref=row["item_ref"],
                metadata={"assignee": row["assignee"], "status": row["status"]},
            )
            self._publish_plan_nodes_unlocked(
                clean_project,
                item_refs=[str(row.get("item_ref") or "")],
                expected_revisions={str(row.get("item_ref") or ""): 0},
            )
            return dict(row)

    def record_contested_call(
        self,
        project_id: str,
        *,
        actor: str,
        subject: str,
        positions: Sequence[Mapping[str, Any]],
        settled_by: str,
        evidence: str = "",
        work_ref: str = "",
    ) -> dict[str, Any]:
        """Write down one disagreement once something settled it.

        Append-only, like a note and for the same reason: a record that can be
        quietly rewritten is not a record. The verdict is about the claim, never
        about the agent, and the evidence travels with it so a later reader can
        decide the verdict was wrong.

        Nothing is recorded while an argument is still live. Two people
        disagreeing is not a contested call; it becomes one when evidence
        arrives, and demanding the evidence is what stops this becoming a place
        to score points.
        """

        clean_project = component(project_id, field="project_id")
        clean_subject = bounded_text(subject, field="subject", maximum=1000, required=True)
        clean_settled = bounded_text(settled_by, field="settled_by", maximum=4000)
        rows = normalize_positions(positions)
        if len(rows) < 2:
            raise DomainError(
                "field_contested_needs_two_positions",
                "A contested call records at least two positions; one view is not a disagreement.",
            )
        resolved = any(row["verdict"] != "open" for row in rows)
        if resolved and not any(row["verdict"] == "right" for row in rows):
            raise DomainError(
                "field_contested_needs_a_right_position",
                "A call presented as settled must say which position was right.",
            )
        if resolved and not str(settled_by or "").strip():
            raise DomainError(
                "field_contested_needs_its_evidence",
                "A settled call says what settled it, so a reader can disagree with the verdict.",
            )
        call_id = new_timed_id("call")
        record = {
            "schema": FIELD_SCHEMA,
            "call_id": call_id,
            "project_ref": make_ref("project", clean_project),
            "work_ref": str(work_ref or ""),
            "subject": clean_subject,
            "positions": rows,
            "settled_by": clean_settled,
            "evidence": bounded_text(evidence, field="evidence", maximum=8000),
            "recorded_by": bounded_text(actor, field="actor", maximum=512, required=True),
            "opened_at": utc_now(),
            # Empty until something settles it. An open call is the normal state
            # of a live disagreement and is what lets this be written down while
            # the argument is running rather than reconstructed from memory.
            "resolved_at": utc_now() if resolved else "",
        }
        record["call_ref"] = reference_for_record("call", record)
        root = self._project_dir(clean_project) / "contested"
        with exclusive_lock(root / ".contested.lock"):
            atomic_write_json(root / f"{call_id}.json", record)
            self._record_event_unlocked(
                clean_project,
                kind="work.contested",
                summary=f"A contested call was recorded: {clean_subject[:160]}",
                actor=actor,
                work_ref=str(work_ref or ""),
                metadata={
                    "call_ref": record["call_ref"],
                    "right": [row["who"] for row in rows if row["verdict"] == "right"],
                    "wrong": [row["who"] for row in rows if row["verdict"] == "wrong"],
                },
            )
        return dict(record)

    def resolve_contested_call(
        self,
        project_id: str,
        call_ref: str,
        *,
        actor: str,
        verdicts: Mapping[str, str],
        settled_by: str,
        evidence: str = "",
    ) -> dict[str, Any]:
        """Attach the outcome to a call that was recorded while it was live.

        Separate from recording on purpose. A disagreement is worth writing
        down at the moment it happens, when both sides still have their
        reasoning; who turned out right is known later, sometimes much later,
        and demanding it up front means the call is reconstructed afterwards by
        whoever won, which is the failure this whole record exists to avoid.

        Resolution happens once. A verdict that could be revised quietly is not
        evidence of anything, and the point of the tally is that a later reader
        can disagree with it in the open rather than find it changed.
        """

        clean_project = component(project_id, field="project_id")
        parsed = parse_ref(call_ref)
        if parsed.kind != "call":
            raise DomainError("field_call_ref_invalid", "Expected a work:call reference.")
        clean_settled = bounded_text(settled_by, field="settled_by", maximum=4000, required=True)
        path = self._project_dir(clean_project) / "contested" / f"{component(parsed.object_id)}.json"
        with exclusive_lock(self._project_dir(clean_project) / "contested" / ".contested.lock"):
            record = read_json(path)
            if str(record.get("resolved_at") or ""):
                raise DomainError(
                    "field_contested_already_resolved",
                    "This call was already settled; a verdict is not revised quietly.",
                    status=409,
                    details={"resolved_at": record.get("resolved_at")},
                )
            chosen = {
                str(who).strip(): str(verdict or "").strip().lower()
                for who, verdict in dict(verdicts or {}).items()
            }
            rows = []
            for position in record.get("positions") or []:
                row = dict(position)
                verdict = chosen.get(row.get("who", ""))
                if verdict in VERDICTS and verdict != "open":
                    row["verdict"] = verdict
                rows.append(row)
            if not any(row.get("verdict") == "right" for row in rows):
                raise DomainError(
                    "field_contested_needs_a_right_position",
                    "Settling a call says which position was right.",
                )
            record["positions"] = rows
            record["settled_by"] = clean_settled
            record["evidence"] = bounded_text(evidence, field="evidence", maximum=8000)
            record["resolved_at"] = utc_now()
            record["resolved_by"] = bounded_text(actor, field="actor", maximum=512, required=True)
            atomic_write_json(path, record)
            self._record_event_unlocked(
                clean_project,
                kind="work.contested_settled",
                summary=f"A contested call was settled: {str(record.get('subject') or '')[:150]}",
                actor=actor,
                work_ref=str(record.get("work_ref") or ""),
                metadata={
                    "call_ref": record["call_ref"],
                    "right": [row["who"] for row in rows if row.get("verdict") == "right"],
                    "wrong": [row["who"] for row in rows if row.get("verdict") == "wrong"],
                },
            )
        return dict(record)

    def list_contested_calls(self, project_id: str) -> list[dict[str, Any]]:
        return json_records(
            self._project_dir(component(project_id, field="project_id")) / "contested"
        )

    def workers_matrix(self, project_id: str) -> dict[str, Any]:
        """Counts with every call behind them, coordinator included."""

        return matrix(self.list_contested_calls(project_id))

    def worker_contested_record(self, project_id: str, who: str) -> dict[str, Any]:
        """One participant's contested calls, without the rest of the history."""

        return record_for(self.list_contested_calls(project_id), who)

    def _worker_path(self, worker_name: str) -> Path:
        return self.control / "workers" / f"{component(worker_name, field='worker_name').lower()}.json"

    def register_worker(
        self,
        *,
        worker_name: str,
        runtime_kind: str,
        capabilities: Sequence[str],
        authority_label: str,
        host_id: str = "",
        host_label: str = "",
        host_kind: str = "local",
        relay_id: str = "",
        reconcile_ceiling_seconds: int = 60,
        worker_alias: str = "",
        worker_identity: str = "",
        runtime_session_id: str = "",
        attended_project_refs: Sequence[str] | None = None,
        worker_ref: str = "",
        worker_id: str = "",
        control_plane_state: str = "",
        authorization_state: str = "",
        authorization_reason: str = "",
        authorization_action: str = "",
    ) -> dict[str, Any]:
        self.initialize()
        clean_name = component(worker_name, field="worker_name").lower()
        if isinstance(capabilities, (str, bytes, bytearray)) or not isinstance(
            capabilities, Sequence
        ):
            raise DomainError(
                "field_worker_capabilities_invalid",
                "Worker capabilities must be an array of capability names.",
            )
        path = self._worker_path(clean_name)
        with exclusive_lock(self.control / "locks" / f"worker-{clean_name}.lock"):
            existing = read_json(path, required=False)
            now = utc_now()
            normalized_runtime_kind = bounded_text(
                runtime_kind, field="runtime_kind", maximum=128, required=True
            ).lower()
            if normalized_runtime_kind not in RUNTIME_KINDS:
                raise DomainError(
                    "field_worker_runtime_kind_invalid",
                    "Worker runtime_kind must identify a supported runtime adapter.",
                    details={"allowed": sorted(RUNTIME_KINDS)},
                )
            session_id = str(runtime_session_id or "").strip()
            identity = str(worker_identity or "").strip()
            if session_id:
                derived = WorkerSessionIdentity.create(
                    normalized_runtime_kind, session_id
                )
                if clean_name != derived.worker_name or (
                    identity and identity != derived.worker_identity
                ):
                    raise DomainError(
                        "field_worker_identity_invalid",
                        "The local worker address must be derived from its coding-agent session.",
                    )
                identity = derived.worker_identity
                session_id = derived.runtime_session_id
            elif not identity:
                identity = str(existing.get("worker_identity") or f"legacy:{clean_name}")
            if existing.get("pool_status") == "retired":
                if identity != str(existing.get("worker_identity") or ""):
                    raise DomainError(
                        "field_worker_identity_invalid",
                        "A retired worker tombstone cannot be rebound to another session.",
                        status=409,
                    )
                return existing
            alias = normalize_worker_alias(
                worker_alias or existing.get("worker_alias") or ""
            )
            for candidate in self.list_workers():
                if candidate.get("worker_name") == clean_name:
                    continue
                if identity and candidate.get("worker_identity") == identity:
                    raise DomainError(
                        "field_worker_identity_bound",
                        "This coding-agent session is already registered locally.",
                        status=409,
                    )
            normalized_host_kind = bounded_text(
                host_kind, field="host_kind", maximum=64, required=True
            ).lower()
            if normalized_host_kind not in HOST_KINDS:
                raise DomainError(
                    "field_worker_host_kind_invalid",
                    "Worker host_kind must identify a supported logical host class.",
                    details={"allowed": sorted(HOST_KINDS)},
                )
            requested_control_plane_state = bounded_text(
                control_plane_state,
                field="control_plane_state",
                maximum=64,
            )
            existing_control_plane_state = str(
                existing.get("control_plane_state") or "local_only"
            )
            selected_control_plane_state = (
                "published"
                if existing_control_plane_state == "published"
                and requested_control_plane_state == "authorized_waiting_for_relay"
                else requested_control_plane_state or existing_control_plane_state
            )
            existing_pool_status = str(existing.get("pool_status") or "active")
            previous_authorization = (
                dict(existing.get("authorization") or {})
                if isinstance(existing.get("authorization"), Mapping)
                else {}
            )
            selected_authorization_state = bounded_text(
                authorization_state,
                field="authorization_state",
                maximum=128,
            )
            if selected_control_plane_state == "published":
                selected_authorization_state = "active"
            selected_authorization_state = (
                selected_authorization_state
                or str(previous_authorization.get("state") or "relay_observation_pending")
            )
            authorization = {
                "state": selected_authorization_state,
                "reason": bounded_text(
                    authorization_reason
                    or previous_authorization.get("reason")
                    or "",
                    field="authorization_reason",
                    maximum=256,
                ),
                "observed_at": (
                    now
                    if selected_authorization_state
                    != str(previous_authorization.get("state") or "")
                    else str(previous_authorization.get("observed_at") or now)
                ),
                "action": (
                    ""
                    if selected_control_plane_state == "published"
                    else bounded_text(
                        authorization_action
                        or previous_authorization.get("action")
                        or "",
                        field="authorization_action",
                        maximum=128,
                    )
                ),
            }
            # Start from the row as it is and overwrite only what registration
            # owns. Anything a worker declared about itself (worktrees, the
            # limit state its runtime reported, whatever comes next) survives
            # the relay's per-cycle registration by construction. On
            # 2026-09-23 a rebuilt row dropped a worktree declared minutes
            # earlier, and the board never saw a file in flight.
            record = {
                **(dict(existing) if isinstance(existing, Mapping) else {}),
                "schema": FIELD_SCHEMA,
                "worker_name": clean_name,
                "worker_alias": alias,
                "worker_identity": identity,
                "worker_ref": worker_ref or existing.get("worker_ref") or make_ref("worker", worker_id or existing.get("worker_id") or new_id("worker")),
                "worker_id": worker_id or existing.get("worker_id") or "",
                "runtime_kind": normalized_runtime_kind,
                "runtime_session_id": session_id or str(existing.get("runtime_session_id") or ""),
                "capabilities": sorted({str(value).strip() for value in capabilities if str(value).strip()}),
                "authority_label": bounded_text(authority_label, field="authority_label", maximum=512),
                "host_id": bounded_text(host_id, field="host_id", maximum=256),
                "host_label": bounded_text(host_label, field="host_label", maximum=512),
                "host_kind": normalized_host_kind,
                "relay_id": bounded_text(relay_id, field="relay_id", maximum=256),
                "control_plane_state": selected_control_plane_state,
                "control_plane_connected_at": (
                    now
                    if selected_control_plane_state == "published"
                    and existing_control_plane_state != "published"
                    else str(existing.get("control_plane_connected_at") or "")
                ),
                "authorization": authorization,
                "relay_diagnostic": (
                    dict(existing.get("relay_diagnostic") or {})
                    if isinstance(existing.get("relay_diagnostic"), Mapping)
                    and existing.get("relay_diagnostic")
                    else {
                        "schema": RELAY_DIAGNOSTIC_SCHEMA,
                        "state": "ready",
                        "code": "",
                        "started_at": "",
                        "recent": [],
                    }
                ),
                "pool_status": existing_pool_status,
                "availability": (
                    "unavailable"
                    if existing_pool_status != "active"
                    else "available"
                ),
                "reconcile_ceiling_seconds": max(5, min(int(reconcile_ceiling_seconds), 86_400)),
                "attended_project_refs": sorted({
                    str(value).strip()
                    for value in (
                        attended_project_refs
                        if attended_project_refs is not None
                        else existing.get("attended_project_refs") or []
                    )
                    if str(value).strip()
                }),
                "attendances_observed_at": str(
                    existing.get("attendances_observed_at") or ""
                ),
                "listener": listener_without_legacy_fields(existing.get("listener")),
                "heartbeat_at": now,
                "published_at": existing.get("published_at") or now,
                "updated_at": now,
                "revision": int(existing.get("revision") or 0) + 1,
                "retired_at": str(existing.get("retired_at") or ""),
                "retirement_actor": str(existing.get("retirement_actor") or ""),
                "retirement_reason": str(existing.get("retirement_reason") or ""),
            }
            if not record["worker_id"]:
                record["worker_id"] = parse_ref(record["worker_ref"]).object_id
            atomic_write_json(path, record)
            return record

    def read_worker(self, worker_name: str) -> dict[str, Any]:
        requested = component(worker_name, field="worker_name").lower()
        row = read_json(self._worker_path(requested))
        if isinstance(row.get("listener"), Mapping):
            row["listener"] = listener_without_legacy_fields(row["listener"])
        return row

    def list_workers(self) -> list[dict[str, Any]]:
        """Every worker, each carrying whether it could actually be reached.

        Attached here rather than at one call site so no surface can show a
        worker as available while its own record says it has not looked at its
        inbox. The board, the projection and the packet all read this.
        """

        rows = json_records(self.control / "workers")
        # Read once rather than per worker: the relay's wake path is a property
        # of this host, not of any one worker, but it changes what every row
        # means. A worker can be listening perfectly while every message still
        # takes the full ceiling to reach it.
        transport = self.relay_transport_state()
        for row in rows:
            name = str(row.get("worker_name") or "")
            if not name:
                continue
            if isinstance(row.get("listener"), Mapping):
                row["listener"] = listener_without_legacy_fields(row["listener"])
            try:
                row["reachability"] = self.worker_reachability(name)
            except DomainError:
                continue
            row["relay_transport"] = transport
            # Reported, never inferred. The board has to show an agent with
            # nothing to do separately from one it simply cannot hear.
            row["idle_report"] = self.worker_idle_state(name)
        return rows

    def record_worker_authorization(
        self,
        worker_name: str,
        *,
        state: str,
        reason: str = "",
        action: str = "",
        control_plane_state: str | None = None,
    ) -> dict[str, Any]:
        clean_name = str(self.read_worker(worker_name).get("worker_name") or "")
        path = self._worker_path(clean_name)
        with exclusive_lock(self.control / "locks" / f"worker-{clean_name}.lock"):
            row = read_json(path)
            now = utc_now()
            row["authorization"] = {
                "state": bounded_text(
                    state, field="authorization_state", maximum=128, required=True
                ),
                "reason": bounded_text(
                    reason, field="authorization_reason", maximum=256
                ),
                "action": bounded_text(
                    action, field="authorization_action", maximum=128
                ),
                "observed_at": now,
            }
            if control_plane_state is not None:
                row["control_plane_state"] = bounded_text(
                    control_plane_state,
                    field="control_plane_state",
                    maximum=64,
                    required=True,
                )
            row.update(
                updated_at=now,
                revision=int(row.get("revision") or 0) + 1,
            )
            atomic_write_json(path, row)
            return row

    def record_relay_channel_degraded(
        self,
        worker_name: str,
        *,
        code: str,
        message: str = "",
        request: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Keep one actionable record for the current retryable channel failure."""

        clean_name = str(self.read_worker(worker_name).get("worker_name") or "")
        path = self._worker_path(clean_name)
        with exclusive_lock(self.control / "locks" / f"worker-{clean_name}.lock"):
            row = read_json(path)
            now = utc_now()
            current = (
                dict(row.get("relay_diagnostic") or {})
                if isinstance(row.get("relay_diagnostic"), Mapping)
                else {}
            )
            selected_code = bounded_text(
                code or "work_relay_transport_unavailable",
                field="relay_diagnostic.code",
                maximum=256,
                required=True,
            )
            selected_message = bounded_text(
                message,
                field="relay_diagnostic.message",
                maximum=4096,
            )
            raw_request = dict(request or {})
            selected_request: dict[str, Any] = {}
            for key, maximum in (
                ("method", 16),
                ("url", 8192),
                ("server_reason", 1024),
                ("failure_kind", 256),
                ("operation", 128),
                ("target", 512),
                ("transport_message_id", 128),
                ("transport_phase", 64),
                ("request_scope", 512),
                ("socket_id", 128),
            ):
                selected = bounded_text(
                    raw_request.get(key),
                    field=f"relay_diagnostic.request.{key}",
                    maximum=maximum,
                )
                if selected:
                    selected_request[key] = selected
            for key in (
                "ingress_accepted",
                "transport_replayed",
                "connection_active",
            ):
                if isinstance(raw_request.get(key), bool):
                    selected_request[key] = raw_request[key]
            try:
                if raw_request.get("connection_generation") is not None:
                    selected_request["connection_generation"] = max(
                        0, int(raw_request["connection_generation"])
                    )
            except (TypeError, ValueError):
                pass
            try:
                status = int(raw_request.get("status"))
            except (TypeError, ValueError):
                status = 0
            if status:
                selected_request["status"] = status
            for key in ("elapsed_seconds", "timeout_seconds"):
                try:
                    if raw_request.get(key) is not None:
                        selected_request[key] = round(
                            max(0.0, float(raw_request[key])), 3
                        )
                except (TypeError, ValueError):
                    pass
            trace_request = {
                key: selected_request[key]
                for key in (
                    "operation", "target", "transport_message_id",
                    "transport_phase", "ingress_accepted", "transport_replayed",
                    "request_scope", "elapsed_seconds", "connection_generation",
                    "socket_id", "connection_active",
                )
                if key in selected_request
            }
            attempt = {
                "at": now,
                **({"request": trace_request} if trace_request else {}),
            }
            if (
                current.get("state") == "degraded"
                and current.get("code") == selected_code
                and current.get("started_at")
            ):
                failure_attempts = int(current.get("failure_attempts") or 1) + 1
                current.update(
                    failure_attempts=failure_attempts,
                    retry_attempts=failure_attempts - 1,
                    last_attempt_at=now,
                    last_attempt_outcome="failed",
                    retryable=True,
                )
                if selected_message and current.get("message") != selected_message:
                    current["message"] = selected_message
                if selected_request and current.get("request") != selected_request:
                    current["request"] = selected_request
                current["attempts"] = [
                    *(
                        dict(item)
                        for item in current.get("attempts") or []
                        if isinstance(item, Mapping)
                    ),
                    attempt,
                ][-20:]
                row["relay_diagnostic"] = current
                row.update(updated_at=now, revision=int(row.get("revision") or 0) + 1)
                atomic_write_json(path, row)
                return current

            recent = [
                dict(item)
                for item in current.get("recent") or []
                if isinstance(item, Mapping)
            ]
            if current.get("state") == "degraded" and current.get("started_at"):
                recent.append(
                    {
                        "code": str(current.get("code") or ""),
                        "started_at": str(current.get("started_at") or ""),
                        "ended_at": now,
                        "published_at": "",
                        "failure_attempts": int(current.get("failure_attempts") or 1),
                        "retry_attempts": int(current.get("retry_attempts") or 0),
                        "retry_outcome": "superseded",
                        "recovery_scope": "channel_cycle",
                        "attempts": [
                            dict(item)
                            for item in current.get("attempts") or []
                            if isinstance(item, Mapping)
                        ][-20:],
                        **(
                            {"message": str(current.get("message") or "")}
                            if current.get("message")
                            else {}
                        ),
                        **(
                            {"request": dict(current.get("request") or {})}
                            if isinstance(current.get("request"), Mapping)
                            and current.get("request")
                            else {}
                        ),
                    }
                )
            diagnostic = {
                "schema": RELAY_DIAGNOSTIC_SCHEMA,
                "state": "degraded",
                "code": selected_code,
                "started_at": now,
                "last_attempt_at": now,
                "failure_attempts": 1,
                "retry_attempts": 0,
                "last_attempt_outcome": "failed",
                "retryable": True,
                "attempts": [attempt],
                "recent": recent[-20:],
            }
            if selected_message:
                diagnostic["message"] = selected_message
            if selected_request:
                diagnostic["request"] = selected_request
            row["relay_diagnostic"] = diagnostic
            row.update(updated_at=now, revision=int(row.get("revision") or 0) + 1)
            atomic_write_json(path, row)
            return diagnostic

    def record_relay_channel_recovered(self, worker_name: str) -> dict[str, Any]:
        """Close a durable degraded interval after an authorized call succeeds."""

        clean_name = str(self.read_worker(worker_name).get("worker_name") or "")
        path = self._worker_path(clean_name)
        with exclusive_lock(self.control / "locks" / f"worker-{clean_name}.lock"):
            row = read_json(path)
            current = (
                dict(row.get("relay_diagnostic") or {})
                if isinstance(row.get("relay_diagnostic"), Mapping)
                else {}
            )
            if current.get("state") != "degraded" or not current.get("started_at"):
                return current
            now = utc_now()
            recent = [
                dict(item)
                for item in current.get("recent") or []
                if isinstance(item, Mapping)
            ]
            recent.append(
                {
                    "code": str(current.get("code") or ""),
                    "started_at": str(current.get("started_at") or ""),
                    "ended_at": now,
                    "published_at": "",
                    "failure_attempts": int(current.get("failure_attempts") or 1),
                    "retry_attempts": int(current.get("failure_attempts") or 1),
                    "retry_outcome": "succeeded",
                    "recovery_scope": "channel_cycle",
                    "attempts": [
                        dict(item)
                        for item in current.get("attempts") or []
                        if isinstance(item, Mapping)
                    ][-20:],
                    **(
                        {"message": str(current.get("message") or "")}
                        if current.get("message")
                        else {}
                    ),
                    **(
                        {"request": dict(current.get("request") or {})}
                        if isinstance(current.get("request"), Mapping)
                        and current.get("request")
                        else {}
                    ),
                }
            )
            diagnostic = {
                "schema": RELAY_DIAGNOSTIC_SCHEMA,
                "state": "ready",
                "code": "",
                "started_at": "",
                "recent": recent[-20:],
            }
            row["relay_diagnostic"] = diagnostic
            row.update(updated_at=now, revision=int(row.get("revision") or 0) + 1)
            atomic_write_json(path, row)
            return diagnostic

    def pending_relay_degraded_intervals(
        self, worker_name: str
    ) -> list[dict[str, str]]:
        """Return completed intervals not yet accepted over an authorized route."""

        row = self.read_worker(worker_name)
        diagnostic = row.get("relay_diagnostic")
        if not isinstance(diagnostic, Mapping):
            return []
        return [
            {
                "code": str(item.get("code") or ""),
                "started_at": str(item.get("started_at") or ""),
                "ended_at": str(item.get("ended_at") or ""),
            }
            for item in diagnostic.get("recent") or []
            if isinstance(item, Mapping)
            and not item.get("published_at")
            and item.get("code")
            and item.get("started_at")
            and item.get("ended_at")
        ]

    def mark_relay_degraded_intervals_published(
        self,
        worker_name: str,
        intervals: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Mark exactly the completed intervals accepted by the control plane."""

        keys = {
            (
                str(item.get("code") or ""),
                str(item.get("started_at") or ""),
                str(item.get("ended_at") or ""),
            )
            for item in intervals
            if isinstance(item, Mapping)
        }
        clean_name = str(self.read_worker(worker_name).get("worker_name") or "")
        path = self._worker_path(clean_name)
        with exclusive_lock(self.control / "locks" / f"worker-{clean_name}.lock"):
            row = read_json(path)
            current = (
                dict(row.get("relay_diagnostic") or {})
                if isinstance(row.get("relay_diagnostic"), Mapping)
                else {}
            )
            now = utc_now()
            changed = False
            recent: list[dict[str, Any]] = []
            for raw in current.get("recent") or []:
                if not isinstance(raw, Mapping):
                    continue
                item = dict(raw)
                key = (
                    str(item.get("code") or ""),
                    str(item.get("started_at") or ""),
                    str(item.get("ended_at") or ""),
                )
                if key in keys and not item.get("published_at"):
                    item["published_at"] = now
                    changed = True
                recent.append(item)
            if not changed:
                return current
            current["recent"] = recent[-20:]
            row["relay_diagnostic"] = current
            row.update(updated_at=now, revision=int(row.get("revision") or 0) + 1)
            atomic_write_json(path, row)
            return current

    def set_worker_status(self, worker_name: str, status: str, *, actor: str, reason: str = "") -> dict[str, Any]:
        clean_name = str(self.read_worker(worker_name).get("worker_name") or "")
        if status not in {"active", "limbo"}:
            raise DomainError("field_worker_status_invalid", "Worker status must be active or limbo.")
        path = self._worker_path(clean_name)
        with exclusive_lock(self.control / "locks" / f"worker-{clean_name}.lock"):
            row = read_json(path)
            if row.get("pool_status") == "retired":
                raise DomainError(
                    "field_worker_retired",
                    "A retired coding-agent session cannot return to the pool.",
                    status=410,
                )
            row.update(
                pool_status=status,
                availability="unavailable" if status == "limbo" else "available",
                status_actor=bounded_text(actor, field="actor", maximum=512, required=True),
                status_reason=bounded_text(reason, field="reason", maximum=2000),
                updated_at=utc_now(),
                revision=int(row.get("revision") or 0) + 1,
            )
            atomic_write_json(path, row)
            return row

    def retire_worker(
        self, worker_name: str, *, actor: str, reason: str = ""
    ) -> dict[str, Any]:
        """Disable one exact local session without deleting its mailbox history."""

        clean_name = str(self.read_worker(worker_name).get("worker_name") or "")
        path = self._worker_path(clean_name)
        with exclusive_lock(self.control / "locks" / f"worker-{clean_name}.lock"):
            row = read_json(path)
            if row.get("pool_status") == "retired":
                return row
            now = utc_now()
            listener = listener_without_legacy_fields(row.get("listener"))
            if listener:
                subscription = dict(listener.get("subscription") or {})
                subscription.update(state="retired", last_attempt_at=now)
                listener.update(
                    state="detached",
                    subscription=subscription,
                    heartbeat_at=now,
                    detached_at=listener.get("detached_at") or now,
                    revision=int(listener.get("revision") or 0) + 1,
                )
            row.update(
                pool_status="retired",
                availability="unavailable",
                attended_project_refs=[],
                current_project_ref="",
                control_plane_state="retired",
                retirement_actor=bounded_text(
                    actor, field="actor", maximum=512, required=True
                ),
                retirement_reason=bounded_text(
                    reason, field="reason", maximum=2000
                ),
                retired_at=now,
                listener=listener,
                updated_at=now,
                revision=int(row.get("revision") or 0) + 1,
            )
            atomic_write_json(path, row)

        for session_path in sorted(
            (self.control / "projects").glob(
                f"*/sessions/{component(clean_name, field='worker_name')}/*.json"
            )
        ):
            with exclusive_lock(session_path.parent / ".sessions.lock"):
                session = read_json(session_path)
                if session.get("state") == "detached":
                    continue
                now = utc_now()
                session.update(
                    state="detached",
                    heartbeat_at=now,
                    detached_at=now,
                    revision=int(session.get("revision") or 0) + 1,
                )
                atomic_write_json(session_path, session)
        archived: list[dict[str, Any]] = []
        direct_root = self._mail_root("", clean_name)
        if direct_root.is_dir():
            with exclusive_lock(direct_root / ".mail.lock"):
                destination = self.control / "undeliverable-mail" / clean_name
                archived.extend(
                    archive_mailbox_messages(
                        direct_root,
                        destination,
                        states=ACTIVE_MAILBOX_STATES,
                        disposition="retired_recipient",
                        reason=reason or "The addressed worker was retired.",
                        details={"worker_name": clean_name},
                    )
                )
        for project_path in sorted((self.control / "projects").glob("*")):
            if not project_path.is_dir():
                continue
            project_id = project_path.name
            source_root = project_path / "mail" / clean_name
            if not source_root.is_dir():
                continue
            with exclusive_lock(self._project_lock(project_id)):
                archived.extend(
                    archive_mailbox_messages(
                        source_root,
                        project_path / "mail" / "undeliverable" / clean_name,
                        states=ACTIVE_MAILBOX_STATES,
                        disposition="retired_recipient",
                        reason=reason or "The addressed worker was retired.",
                        details={"worker_name": clean_name},
                    )
                )
        with exclusive_lock(self.control / "locks" / f"worker-{clean_name}.lock"):
            row = read_json(path)
            row["mail_retirement"] = {
                "policy": "archived_undeliverable_and_notify_sender",
                "archived_count": len(archived),
                "message_refs": [
                    str(message.get("message_ref") or "") for message in archived
                ],
                "recorded_at": utc_now(),
            }
            atomic_write_json(path, row)
        return read_json(path)

    def sync_worker_attendances(
        self, worker_name: str, project_refs: Sequence[str]
    ) -> dict[str, Any]:
        clean_name = str(self.read_worker(worker_name).get("worker_name") or "")
        path = self._worker_path(clean_name)
        with exclusive_lock(self.control / "locks" / f"worker-{clean_name}.lock"):
            row = read_json(path)
            normalized: list[str] = []
            for value in project_refs:
                project_ref = bounded_text(
                    value, field="project_ref", maximum=256, required=True
                )
                if parse_ref(project_ref).kind != "project":
                    raise DomainError(
                        "field_project_ref_invalid",
                        "Worker attendance requires work:project references.",
                    )
                normalized.append(project_ref)
            now = utc_now()
            row.update(
                attended_project_refs=sorted(set(normalized)),
                attendances_observed_at=now,
                updated_at=now,
                revision=int(row.get("revision") or 0) + 1,
            )
            atomic_write_json(path, row)
            return row

    def listen_worker(
        self,
        worker_name: str,
        *,
        check_interval_seconds: int = 30,
    ) -> dict[str, Any]:
        worker = self.read_worker(worker_name)
        clean_name = str(worker.get("worker_name") or "")
        if worker.get("pool_status") != "active":
            if worker.get("pool_status") == "retired":
                raise DomainError(
                    "field_worker_retired",
                    "This coding-agent session is retired from Problem Board.",
                    status=410,
                )
            raise DomainError(
                "field_worker_limbo",
                "A suspended worker cannot start listening.",
                status=403,
            )
        path = self._worker_path(clean_name)
        with exclusive_lock(self.control / "locks" / f"worker-{clean_name}.lock"):
            row = read_json(path)
            previous = row.get("listener") if isinstance(row.get("listener"), Mapping) else {}
            now = utc_now()
            listener = {
                "session_id": str(row.get("runtime_session_id") or clean_name),
                "state": "waiting",
                "check_interval_seconds": max(
                    5, min(int(check_interval_seconds), 3600)
                ),
                "last_inbox_check_at": str(previous.get("last_inbox_check_at") or ""),
                "last_message_refs": list(previous.get("last_message_refs") or []),
                "last_control_refs": list(previous.get("last_control_refs") or []),
                "observed_control_plane_state": str(
                    previous.get("observed_control_plane_state")
                    or row.get("control_plane_state")
                    or "local_only"
                ),
                "subscription": {
                    **dict(previous.get("subscription") or {}),
                    "adapter": (
                        "codex-queue"
                        if row.get("runtime_kind") == "codex"
                        else "session-owned-watch"
                    ),
                    "state": str(
                        (previous.get("subscription") or {}).get("state")
                        or (
                            "awaiting_relay"
                            if row.get("runtime_kind") == "codex"
                            else "session_owned"
                        )
                    ),
                },
                "last_inbox_result_at": str(
                    previous.get("last_inbox_result_at") or ""
                ),
                "last_mail_settled_at": str(
                    previous.get("last_mail_settled_at") or ""
                ),
                "last_settled_message_ref": str(
                    previous.get("last_settled_message_ref") or ""
                ),
                "attached_at": str(previous.get("attached_at") or now),
                "heartbeat_at": now,
                "detached_at": "",
                "revision": int(previous.get("revision") or 0) + 1,
            }
            row.update(listener=listener, updated_at=now)
            atomic_write_json(path, row)
            return self._session_with_presence(listener)

    def check_in_worker_listener(
        self,
        worker_name: str,
        *,
        state: str = "waiting",
        inbox_checked: bool = False,
        message_refs: Sequence[str] = (),
        control_refs: Sequence[str] = (),
        observed_control_plane_state: str | None = None,
        wake_id: str = "",
    ) -> dict[str, Any]:
        worker = self.read_worker(worker_name)
        clean_name = str(worker.get("worker_name") or "")
        normalized_state = str(state or "").strip().lower()
        if normalized_state not in SESSION_STATES - {"detached"}:
            raise DomainError(
                "field_session_state_invalid",
                "A listening worker state must be waiting, working, or blocked.",
            )
        path = self._worker_path(clean_name)
        with exclusive_lock(self.control / "locks" / f"worker-{clean_name}.lock"):
            row = read_json(path)
            listener = listener_without_legacy_fields(row.get("listener"))
            if not listener or listener.get("state") == "detached":
                raise DomainError(
                    "field_worker_not_listening",
                    "This coding-agent session must run worker listen before checking its inbox.",
                    status=409,
                )
            now = utc_now()
            listener.update(
                state=normalized_state,
                heartbeat_at=now,
                revision=int(listener.get("revision") or 0) + 1,
            )
            if inbox_checked:
                observed_message_refs = _bounded_message_refs(message_refs)
                listener.update(
                    last_inbox_check_at=now,
                    last_message_refs=observed_message_refs[-20:],
                    last_control_refs=[
                        bounded_text(value, field="control_ref", maximum=256)
                        for value in list(control_refs)[-20:]
                    ],
                )
                subscription = dict(listener.get("subscription") or {})
                outstanding_wake_id = str(
                    subscription.get("outstanding_wake_id") or ""
                )
                observed_wake_message_refs = _bounded_message_refs(
                    subscription.get("wake_observed_message_refs") or []
                )
                wake_provenance = (
                    dict(subscription.get("wake_provenance") or {})
                    if isinstance(subscription.get("wake_provenance"), Mapping)
                    else {}
                )
                wake_submission_ids = [
                    bounded_text(
                        value,
                        field="queued_submission_id",
                        maximum=128,
                        required=True,
                    )
                    for value in list(
                        subscription.get("wake_queued_submission_ids") or []
                    )[-MAX_WAKE_QUEUE_SUBMISSIONS:]
                ]
                if outstanding_wake_id and observed_message_refs:
                    observed_wake_message_refs = _bounded_message_refs(
                        [*observed_wake_message_refs, *observed_message_refs]
                    )
                    subscription["wake_observed_message_refs"] = (
                        observed_wake_message_refs
                    )
                acknowledged_wake_id = bounded_text(
                    wake_id, field="wake_id", maximum=128
                )
                legacy_wake = bool(
                    subscription.get("last_wake_message_refs")
                    and not outstanding_wake_id
                )
                if legacy_wake or (
                    acknowledged_wake_id
                    and acknowledged_wake_id == outstanding_wake_id
                ):
                    acknowledged_message_refs = (
                        observed_wake_message_refs
                        if outstanding_wake_id
                        else observed_message_refs
                    )
                    subscription["last_wake_message_refs"] = []
                    subscription.pop("wake_observed_message_refs", None)
                    subscription.pop("outstanding_wake_id", None)
                    subscription.pop("wake_ack_deadline_at", None)
                    subscription.pop("wake_first_attempt_at", None)
                    subscription.pop("wake_last_attempt_at", None)
                    subscription.pop("wake_attempts", None)
                    subscription.pop("wake_delivery_state", None)
                    subscription.pop("wake_provenance", None)
                    subscription.pop("wake_queued_submission_ids", None)
                    for field in WAKE_OBSERVATION_FIELDS:
                        subscription.pop(field, None)
                    subscription["queue_reconciliation_required"] = True
                    if acknowledged_wake_id:
                        previous_wake_id = str(
                            subscription.get("last_acknowledged_wake_id") or ""
                        )
                        previous_wake_refs = _bounded_message_refs(
                            subscription.get("last_acknowledged_wake_message_refs")
                            or []
                        )
                        previous_wake_acknowledged_at = str(
                            subscription.get("last_wake_acknowledged_at") or ""
                        )
                        previous_wake_provenance = (
                            dict(
                                subscription.get(
                                    "last_acknowledged_wake_provenance"
                                )
                                or {}
                            )
                            if isinstance(
                                subscription.get(
                                    "last_acknowledged_wake_provenance"
                                ),
                                Mapping,
                            )
                            else {}
                        )
                        previous_submission_ids = list(
                            subscription.get(
                                "last_acknowledged_wake_submission_ids"
                            )
                            or []
                        )
                        subscription["last_acknowledged_wake_id"] = (
                            acknowledged_wake_id
                        )
                        subscription["last_acknowledged_wake_message_refs"] = (
                            acknowledged_message_refs
                        )
                        subscription["last_wake_acknowledged_at"] = now
                        subscription["last_acknowledged_wake_provenance"] = (
                            wake_provenance
                        )
                        subscription["last_acknowledged_wake_submission_ids"] = (
                            wake_submission_ids
                        )
                        history = [
                            dict(value)
                            for value in list(
                                subscription.get("wake_acknowledgements") or []
                            )
                            if isinstance(value, Mapping)
                            and str(value.get("wake_id") or "")
                            != acknowledged_wake_id
                        ]
                        if (
                            not history
                            and previous_wake_id
                            and previous_wake_id != acknowledged_wake_id
                        ):
                            previous_record = {
                                "wake_id": previous_wake_id,
                                "message_refs": previous_wake_refs,
                                "acknowledged_at": previous_wake_acknowledged_at,
                            }
                            if previous_wake_provenance:
                                previous_record["provenance"] = (
                                    previous_wake_provenance
                                )
                            if previous_submission_ids:
                                previous_record["queued_submission_ids"] = (
                                    previous_submission_ids
                                )
                            history.append(previous_record)
                        acknowledged_record = {
                            "wake_id": acknowledged_wake_id,
                            "message_refs": acknowledged_message_refs,
                            "acknowledged_at": now,
                        }
                        if wake_provenance:
                            acknowledged_record["provenance"] = wake_provenance
                        if wake_submission_ids:
                            acknowledged_record["queued_submission_ids"] = (
                                wake_submission_ids
                            )
                        history.append(acknowledged_record)
                        subscription["wake_acknowledgements"] = history[
                            -MAX_WAKE_ACKNOWLEDGEMENTS:
                        ]
                        subscription["last_wake_receipt"] = {
                            "wake_id": acknowledged_wake_id,
                            "state": "acknowledged",
                            "recorded_at": now,
                        }
                elif acknowledged_wake_id:
                    subscription["queue_reconciliation_required"] = True
                    acknowledged_ids = {
                        str(value.get("wake_id") or "")
                        for value in list(
                            subscription.get("wake_acknowledgements") or []
                        )
                        if isinstance(value, Mapping)
                    }
                    acknowledged_ids.add(
                        str(subscription.get("last_acknowledged_wake_id") or "")
                    )
                    subscription["last_wake_receipt"] = {
                        "wake_id": acknowledged_wake_id,
                        "state": (
                            "already_acknowledged"
                            if acknowledged_wake_id in acknowledged_ids
                            else "stale"
                        ),
                        "expected_wake_id": outstanding_wake_id,
                        "recorded_at": now,
                    }
                listener["subscription"] = subscription
                if message_refs or control_refs:
                    listener["last_inbox_result_at"] = now
            if observed_control_plane_state is not None:
                listener["observed_control_plane_state"] = bounded_text(
                    observed_control_plane_state,
                    field="observed_control_plane_state",
                    maximum=64,
                )
            row.update(listener=listener, updated_at=now)
            atomic_write_json(path, row)
            return self._session_with_presence(listener)

    def record_worker_inbox_probe(
        self,
        worker_name: str,
        *,
        observed_control_plane_state: str | None = None,
    ) -> dict[str, Any]:
        """Record a notification-only inbox check without clearing delivery evidence."""

        worker = self.read_worker(worker_name)
        clean_name = str(worker.get("worker_name") or "")
        path = self._worker_path(clean_name)
        with exclusive_lock(self.control / "locks" / f"worker-{clean_name}.lock"):
            row = read_json(path)
            listener = listener_without_legacy_fields(row.get("listener"))
            if not listener or listener.get("state") == "detached":
                raise DomainError(
                    "field_worker_not_listening",
                    "This coding-agent session must run worker listen before watching its inbox.",
                    status=409,
                )
            now = utc_now()
            listener.update(
                last_inbox_check_at=now,
                heartbeat_at=now,
                revision=int(listener.get("revision") or 0) + 1,
            )
            if observed_control_plane_state is not None:
                listener["observed_control_plane_state"] = bounded_text(
                    observed_control_plane_state,
                    field="observed_control_plane_state",
                    maximum=64,
                )
            row.update(listener=listener, updated_at=now)
            atomic_write_json(path, row)
            return self._session_with_presence(listener)

    def detach_worker_listener(self, worker_name: str) -> dict[str, Any]:
        worker = self.read_worker(worker_name)
        clean_name = str(worker.get("worker_name") or "")
        path = self._worker_path(clean_name)
        with exclusive_lock(self.control / "locks" / f"worker-{clean_name}.lock"):
            row = read_json(path)
            listener = listener_without_legacy_fields(row.get("listener"))
            if not listener:
                raise DomainError(
                    "field_worker_not_listening",
                    "This coding-agent session has no listener to detach.",
                    status=404,
                )
            now = utc_now()
            listener.update(
                state="detached",
                heartbeat_at=now,
                detached_at=now,
                revision=int(listener.get("revision") or 0) + 1,
            )
            row.update(listener=listener, updated_at=now)
            atomic_write_json(path, row)
            return self._session_with_presence(listener)

    def worker_reachability(self, worker_name: str) -> dict[str, Any]:
        """Whether a worker could actually read mail sent to it right now.

        Availability was asserted at enrollment and never contradicted, so the
        board showed active and available for a worker that had been deaf for
        ninety minutes, and for one that had never authorized at all. Three
        times in one evening the operator noticed before the coordinator did.

        The evidence was always there: a worker records every inbox check, and
        its mailbox holds what it has not read. Silence past its own check
        interval, with mail waiting, is not availability.
        """

        worker = self.read_worker(worker_name)
        stable = str(worker.get("worker_name") or "")
        pending = self.pending_worker_mail_count_snapshot(stable)
        listener = listener_without_legacy_fields(worker.get("listener"))
        subscription = (
            dict(listener.get("subscription") or {})
            if isinstance(listener.get("subscription"), Mapping)
            else {}
        )
        checked_at = str(listener.get("last_inbox_check_at") or "")
        interval = max(5, int(listener.get("check_interval_seconds") or 30))

        if str(worker.get("pool_status") or "") == "retired":
            state = "retired"
        elif not listener:
            # No listener at all: enrolled and never started listening. Calling
            # that detached would claim it once was, which is the kind of small
            # lie that sends someone looking in the wrong place.
            state = "never_listened"
        elif listener.get("state") == "detached":
            state = "detached"
        elif not checked_at:
            state = "never_listened"
        else:
            silent = _seconds_since(checked_at)
            # One missed interval is visible without claiming the session is
            # unreachable. The existing three-interval stale window remains
            # the reachability alarm used by the board.
            if silent > max(30, interval * 3):
                state = "not_listening"
            elif silent > interval:
                state = "overdue"
            else:
                state = "listening"

        return {
            "state": state,
            "reachable": state in {"listening", "overdue"},
            "session_state": str(listener.get("state") or ""),
            "pending_messages": pending,
            "last_inbox_check_at": checked_at,
            "check_interval_seconds": interval,
            "stale_after_seconds": max(30, interval * 3),
            "silent_seconds": _seconds_since(checked_at) if checked_at else None,
            "overdue_by_seconds": (
                max(0, _seconds_since(checked_at) - interval)
                if checked_at
                else None
            ),
            # The attention state the incident reporter reads. For a Codex
            # session: a wake still queued past the report threshold is a
            # queue incident, reported as a factual notice and never as a
            # verdict on the session; a retry that never reached the queue,
            # or a wake taken twice without acknowledgement, is a dead path
            # It stands beside presence and does not rewrite it.
            "wake_state": (
                _overdue_wake_state(subscription)[0]
                or str(subscription.get("wake_delivery_state") or "")
            ),
            "wake_overdue_since": _overdue_wake_state(subscription)[1],
            "wake_queued_overdue_since": str(
                subscription.get("wake_queued_overdue_since") or ""
            ),
            "wake_retry_exhausted_since": str(
                subscription.get("wake_retry_exhausted_since") or ""
            ),
            "wake_overdue_grace_seconds": WAKE_OVERDUE_GRACE_SECONDS,
            "wake_queued_confirmed_at": str(subscription.get("wake_queued_confirmed_at") or ""),
            "wake_last_error": str(subscription.get("last_error") or ""),
            "outstanding_wake_id": str(subscription.get("outstanding_wake_id") or ""),
        }

    def dead_path_record(self, worker_name: str) -> dict[str, Any]:
        """The durable record of the outage the relay is reporting, or empty.

        One value that means both "an outage exists" and "the operator was
        told" cannot represent the moments between: a note enqueued and the
        process gone before it was remembered, a session recovered before the
        next cycle. The record carries the outage start and a phase for each
        of the two notes, pending until the outbox holds it and enqueued
        after, and every cycle resumes from the phase it finds.
        """

        worker = self.read_worker(worker_name)
        clean = str(worker.get("worker_name") or "")
        row = read_json(self.control / "dead-paths" / f"{clean}.json", required=False) or {}
        if not isinstance(row, Mapping):
            return {}
        record = dict(row)
        # A v1 row (one kind, two phase fields under older names) reads as a
        # dead path in v2 terms, so a relay restarted across the schema change
        # resumes its phase instead of starting the outage over.
        if "kind" not in record:
            record["kind"] = "dead_path"
        if "open_note" not in record and "dead_note" in record:
            record["open_note"] = record.pop("dead_note")
        if "close_note" not in record and "recovery_note" in record:
            record["close_note"] = record.pop("recovery_note")
        if "closed_at" not in record and "recovered_at" in record:
            record["closed_at"] = record.pop("recovered_at")
        return record

    def write_dead_path_record(
        self, worker_name: str, record: Mapping[str, Any] | None
    ) -> None:
        """Replace the outage record, or remove it when the outage is fully reported."""

        worker = self.read_worker(worker_name)
        clean = str(worker.get("worker_name") or "")
        directory = self.control / "dead-paths"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{clean}.json"
        with exclusive_lock(self.control / "locks" / f"dead-path-{clean}.lock"):
            if not record:
                if path.exists():
                    path.unlink()
                return
            atomic_write_json(
                path,
                {
                    "schema": "problem-board.dead-path-record.v2",
                    "worker_name": clean,
                    # Incident identity: what kind of condition, on which wake,
                    # since when. The kind decides the keys, the notes, the
                    # log lines and the state the reporter returns, so a wake
                    # still queued opens and closes as a queue notice and is
                    # never announced as a dead path.
                    "kind": bounded_text(
                        record.get("kind") or "dead_path", field="kind", maximum=32
                    ),
                    "wake_id": bounded_text(record.get("wake_id"), field="wake_id", maximum=128),
                    "since": bounded_text(
                        record.get("since"), field="since", maximum=64, required=True
                    ),
                    "open_note": bounded_text(
                        record.get("open_note") or "pending", field="open_note", maximum=16
                    ),
                    "closed_at": bounded_text(
                        record.get("closed_at"), field="closed_at", maximum=64
                    ),
                    "close_note": bounded_text(
                        record.get("close_note"), field="close_note", maximum=16
                    ),
                    "recorded_at": utc_now(),
                },
            )

    def remote_mail_receipt_exists(self, *, sender: str, idempotency_key: str) -> bool:
        """Whether the direct outbox already holds a mail under this sender's key.

        The dead-path reporter asks this when its record says a note is
        pending and the state has moved on: the note may have gone out with
        the process dying before the phase advanced. The receipt is the fact.
        """

        worker = self.read_worker(sender)
        clean = str(worker.get("worker_name") or "")
        return self._mail_idempotency_path("", clean, idempotency_key).exists()

    def record_relay_transport_state(
        self,
        *,
        degraded: bool,
        reason: str = "",
        ceiling_seconds: float = 0.0,
    ) -> dict[str, Any]:
        """Write down that the relay is waiting rather than being pushed.

        Losing push is invisible by construction. The relay keeps working, just
        slowly, so nothing fails and nobody is told: the log carried
        work_relay_transport_unavailable over five hundred times while the only
        thing that reached a person was that opening a plan node took half a
        minute, and the coordinator then explained the delay with a polling
        schedule that does not exist.

        This is host-local and needs no remote authorization, which is the
        point: it is readable exactly when the board looks wrong. The interval
        is kept rather than a flag, so recovery leaves a record of how long it
        lasted instead of silently healing.
        """

        path = self.control / "relay-transport.json"
        with exclusive_lock(self.control / "locks" / "relay-transport.lock"):
            current = read_json(path, required=False) or {}
            was = bool(current.get("degraded"))
            now = utc_now()
            if degraded == was:
                return dict(current)
            if degraded:
                row = {
                    "schema": "problem-board.relay-transport.v1",
                    "degraded": True,
                    # code/started_at/ended_at, matching the interval shape
                    # publishes to the board, so one vocabulary describes this
                    # locally and remotely instead of two that drift.
                    "code": bounded_text(
                        reason or "relay_push_transport_unavailable",
                        field="code",
                        maximum=200,
                        required=False,
                    ),
                    "ceiling_seconds": int(ceiling_seconds or 0),
                    "started_at": now,
                    "ended_at": "",
                }
            else:
                history = list(current.get("history") or [])
                if current.get("started_at"):
                    history.append(
                        {
                            "code": str(current.get("code") or ""),
                            "started_at": str(current.get("started_at") or ""),
                            "ended_at": now,
                        }
                    )
                row = {
                    "schema": "problem-board.relay-transport.v1",
                    "degraded": False,
                    "code": "",
                    "ceiling_seconds": int(ceiling_seconds or 0),
                    "started_at": "",
                    "ended_at": now,
                    # Bounded on purpose. This is for answering "why was it slow
                    # ten minutes ago", not for keeping a permanent ledger.
                    "history": history[-20:],
                }
            atomic_write_json(path, row)
            return row

    def relay_transport_state(self) -> dict[str, Any]:
        """What the relay's wake path is doing, for anyone reporting on it."""

        row = read_json(self.control / "relay-transport.json", required=False) or {}
        degraded = bool(row.get("degraded"))
        started_at = str(row.get("started_at") or "")
        return {
            "waking_on": "ceiling" if degraded else "push",
            "degraded": degraded,
            "code": str(row.get("code") or ""),
            "started_at": started_at,
            "degraded_seconds": (
                _seconds_since(started_at) if degraded and started_at else None
            ),
            # Completed intervals, the same rows published to the board.
            "recent": list(row.get("history") or [])[-5:],
        }

    # Why an agent stopped, in its own words. Inferring these from silence is
    # exactly what this item exists to stop: silence already has a meaning,
    # unreachable, and using it for two things makes both useless.
    IDLE_REASONS = ("finished", "blocked", "nothing_assigned")

    def declare_worker_idle(
        self,
        worker_name: str,
        *,
        reason: str,
        summary: str = "",
        last_work_ref: str = "",
    ) -> dict[str, Any]:
        """An agent says it has run out of work, and why.

        Nothing told the coordinator this. It found out by noticing an absence,
        which means it usually did not find out at all: an agent that finishes
        and has nothing next simply stops appearing, and the three signals that
        exist all mean something else. check_in_worker_listener carries waiting,
        working or blocked, but a worker sets that while handling a message.
        Reachability says whether we can hear it, which is a different question
        from whether it has anything to do.

        Carrying what it last worked on matters as much as the reason. "Nothing
        assigned" without a thread to pick up leaves the coordinator to work out
        what just finished before it can hand over anything new.
        """

        clean_reason = str(reason or "").strip().lower()
        if clean_reason not in self.IDLE_REASONS:
            raise DomainError(
                "field_idle_reason_invalid",
                "An idle report names why: finished, blocked, or nothing_assigned.",
                details={"reason": clean_reason, "allowed": list(self.IDLE_REASONS)},
            )
        worker = self.read_worker(worker_name)
        clean_name = str(worker.get("worker_name") or "")
        path = self._worker_path(clean_name)
        with exclusive_lock(self.control / "locks" / f"worker-{clean_name}.lock"):
            row = read_json(path)
            row["idle"] = {
                "reason": clean_reason,
                "summary": bounded_text(summary, field="summary", maximum=4000),
                "last_work_ref": str(last_work_ref or ""),
                "since": utc_now(),
            }
            row["updated_at"] = utc_now()
            atomic_write_json(path, row)
            return dict(row["idle"])

    def record_runtime_limit_state(
        self,
        worker_name: str,
        state: Mapping[str, Any],
    ) -> dict[str, Any]:
        """What the runtime itself said about its usage limit (W26).

        Claude Code has no file the relay can read, so the runtime's own status
        line command and StopFailure hook hand the state to ``pb worker
        limit-state``, which records it here. The relay puts it on the listener
        session at the next cycle. Codex needs none of this: its rollout file
        is read directly.
        """

        if not isinstance(state, Mapping) or not state:
            raise DomainError(
                "field_limit_state_invalid",
                "A limit state is an object with a kind.",
                status=400,
            )
        worker = self.read_worker(worker_name)
        clean_name = str(worker.get("worker_name") or "")
        path = self._worker_path(clean_name)
        incoming = {key: value for key, value in state.items() if key != "recorded_at"}
        now = utc_now()
        with exclusive_lock(self.control / "locks" / f"worker-{clean_name}.lock"):
            row = read_json(path)
            current = row.get("runtime_limit_state")
            current = dict(current) if isinstance(current, Mapping) else {}
            # The status line re-runs on every update (debounced at 300 ms), so
            # an unchanged state is written at most once a minute, and the
            # worker's updated_at is never touched: this is a reading, not
            # activity, and presence must not read it as one.
            unchanged = all(
                current.get(key) == incoming.get(key)
                for key in ("kind", "reached", "resets_at", "windows", "source")
            )
            recorded_at = str(current.get("recorded_at") or "")
            if unchanged and recorded_at and (_seconds_since(recorded_at) or 0) < 60:
                return current
            row["runtime_limit_state"] = {**incoming, "recorded_at": now}
            atomic_write_json(path, row)
            return dict(row["runtime_limit_state"])

    def runtime_limit_state(self, worker_name: str) -> dict[str, Any]:
        """The recorded runtime limit state, or empty when the runtime never said."""

        try:
            worker = self.read_worker(worker_name)
        except DomainError:
            return {}
        recorded = worker.get("runtime_limit_state")
        return dict(recorded) if isinstance(recorded, Mapping) else {}

    def declare_workspace(
        self,
        worker_name: str,
        *,
        assignment_ref: str,
        repository_ref: str,
        path: str,
    ) -> dict[str, Any]:
        """Where on this host the worker edits one repository for one assignment (W278 part B).

        Local only, never sent. The relay reads the worktree here each cycle
        and publishes the tracked paths that changed, so the board can say
        which files this worker has in flight per repository. One row per
        assignment and repository, replaced when declared again.
        """

        clean_assignment = bounded_text(assignment_ref, field="assignment_ref", maximum=1000, required=True)
        clean_repository = bounded_text(repository_ref, field="repository_ref", maximum=512, required=True)
        clean_path = str(Path(str(path or "")).expanduser().resolve())
        if not Path(clean_path).is_dir():
            raise DomainError(
                "field_workspace_path_missing",
                "The workspace path is not a directory on this host.",
                status=400,
                details={"path": clean_path},
            )
        worker = self.read_worker(worker_name)
        clean_name = str(worker.get("worker_name") or "")
        row_path = self._worker_path(clean_name)
        with exclusive_lock(self.control / "locks" / f"worker-{clean_name}.lock"):
            row = read_json(row_path)
            rows = [
                dict(item)
                for item in (row.get("workspaces") or [])
                if isinstance(item, Mapping)
                and not (
                    str(item.get("assignment_ref") or "") == clean_assignment
                    and str(item.get("repository_ref") or "") == clean_repository
                )
            ]
            declared = {
                "assignment_ref": clean_assignment,
                "repository_ref": clean_repository,
                "path": clean_path,
                "declared_at": utc_now(),
            }
            rows.append(declared)
            row["workspaces"] = rows
            atomic_write_json(row_path, row)
            return declared

    def clear_workspace(
        self,
        worker_name: str,
        *,
        assignment_ref: str,
        repository_ref: str = "",
    ) -> int:
        """Forget the declared worktree(s) of one assignment, one repository or all."""

        worker = self.read_worker(worker_name)
        clean_name = str(worker.get("worker_name") or "")
        row_path = self._worker_path(clean_name)
        with exclusive_lock(self.control / "locks" / f"worker-{clean_name}.lock"):
            row = read_json(row_path)
            before = [dict(item) for item in (row.get("workspaces") or []) if isinstance(item, Mapping)]
            kept = [
                item
                for item in before
                if not (
                    str(item.get("assignment_ref") or "") == str(assignment_ref or "")
                    and (not repository_ref or str(item.get("repository_ref") or "") == str(repository_ref))
                )
            ]
            row["workspaces"] = kept
            atomic_write_json(row_path, row)
            return len(before) - len(kept)

    def workspaces(self, worker_name: str) -> list[dict[str, Any]]:
        """The worktrees this worker declared on this host, or none."""

        try:
            worker = self.read_worker(worker_name)
        except DomainError:
            return []
        return [dict(item) for item in (worker.get("workspaces") or []) if isinstance(item, Mapping)]

    def worker_idle_state(self, worker_name: str) -> dict[str, Any]:
        """Whether this agent is still out of work, without it having to say so twice.

        Declared, then derived away. An agent that reported idle and has since
        been given something is not idle any more, and should not have to send a
        second message to stop being it. Anything addressed to it after it spoke
        is work arriving, so the report expires on its own.
        """

        worker = self.read_worker(worker_name)
        declared = worker.get("idle")
        declared = dict(declared) if isinstance(declared, Mapping) else {}
        since = str(declared.get("since") or "")
        if not since:
            return {"idle": False, "reason": "", "since": "", "last_work_ref": ""}

        # Mail is how work reaches a worker here, assignments included, so
        # anything waiting in its inbox is work that arrived after it spoke.
        # Counted lock-free, because this is diagnostic and must never make a
        # sender wait on a number that only describes it.
        clean_name = str(worker.get("worker_name") or "")
        arrived = self.pending_worker_mail_count_snapshot(clean_name) > 0
        if arrived:
            return {
                "idle": False,
                "reason": "",
                "since": "",
                "last_work_ref": str(declared.get("last_work_ref") or ""),
                "cleared_by": "work arrived",
            }
        return {
            "idle": True,
            "reason": str(declared.get("reason") or ""),
            "summary": str(declared.get("summary") or ""),
            "last_work_ref": str(declared.get("last_work_ref") or ""),
            "since": since,
            "idle_seconds": _seconds_since(since),
        }

    def worker_listener_session(self, worker_name: str) -> dict[str, Any] | None:
        worker = self.read_worker(worker_name)
        listener = worker.get("listener")
        if not isinstance(listener, Mapping) or not listener:
            return None
        return self._session_with_presence(
            listener_without_legacy_fields(listener)
        )

    def prepare_worker_session_wake(
        self,
        worker_name: str,
        *,
        message_refs: Sequence[str],
        wake_id: str,
        retry: bool = False,
        wake_origin: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist a wake before invoking an external session queue.

        A fast consumer can run the queued receive before the queue subprocess
        returns. Reserving first makes that acknowledgement visible and keeps
        the completion write from recreating an already-consumed wake.
        """

        worker = self.read_worker(worker_name)
        clean_name = str(worker.get("worker_name") or "")
        clean_wake_id = bounded_text(
            wake_id, field="wake_id", maximum=128, required=True
        )
        path = self._worker_path(clean_name)
        with exclusive_lock(self.control / "locks" / f"worker-{clean_name}.lock"):
            row = read_json(path)
            listener = listener_without_legacy_fields(row.get("listener"))
            if not listener or listener.get("state") == "detached":
                raise DomainError(
                    "field_worker_not_listening",
                    "The addressed coding-agent session is not listening.",
                    status=409,
                )
            subscription = dict(listener.get("subscription") or {})
            outstanding_wake_id = str(
                subscription.get("outstanding_wake_id") or ""
            )
            if retry and outstanding_wake_id != clean_wake_id:
                raise DomainError(
                    "field_worker_wake_already_acknowledged",
                    "The wake was acknowledged before its retry could be queued.",
                    status=409,
                    details={
                        "wake_id": clean_wake_id,
                        "outstanding_wake_id": outstanding_wake_id,
                    },
                )
            if outstanding_wake_id and outstanding_wake_id != clean_wake_id:
                raise DomainError(
                    "field_worker_wake_conflict",
                    "Another wake is already outstanding for this session.",
                    status=409,
                    details={
                        "wake_id": clean_wake_id,
                        "outstanding_wake_id": outstanding_wake_id,
                    },
                )
            now = utc_now()
            same_wake = outstanding_wake_id == clean_wake_id
            delivery_state = str(subscription.get("wake_delivery_state") or "")
            consumed_retries = int(subscription.get("wake_consumed_retries") or 0)
            if retry and same_wake and (
                delivery_state == "queued" or consumed_retries >= 1
            ):
                # A wake the native queue still holds needs no second
                # submission: the model has not taken the first, and another
                # would hand it two turns when it wakes. The same holds
                # once a consumed wake has had its one retry, whatever state
                # that retry left behind: consumed again, or failed at the
                # queue boundary. Keying on the count and not the state name
                # is what keeps the ceiling at one. Ask for a queue re-read
                # instead, renew the deadline so the caller does not spin, and
                # refuse with the code the relay already treats as "not
                # queued".
                subscription["queue_reconciliation_required"] = True
                _renew_overdue_wake_deadline(subscription)
                if delivery_state == "queued":
                    subscription.setdefault("wake_queued_overdue_since", now)
                else:
                    subscription.setdefault("wake_retry_exhausted_since", now)
                subscription["revision"] = int(subscription.get("revision") or 0) + 1
                listener.update(
                    subscription=subscription,
                    revision=int(listener.get("revision") or 0) + 1,
                )
                row.update(listener=listener, updated_at=now)
                atomic_write_json(path, row)
                raise DomainError(
                    "field_worker_wake_conflict",
                    "The outstanding wake is still queued; the queue is re-read "
                    "instead of receiving a second submission.",
                    status=409,
                    details={
                        "wake_id": clean_wake_id,
                        "outstanding_wake_id": outstanding_wake_id,
                        "wake_delivery_state": delivery_state,
                        "reason": "wake_still_queued"
                        if delivery_state == "queued"
                        else "wake_consumed_retry_exhausted",
                    },
                )
            if retry and same_wake and delivery_state == "consumed":
                subscription["wake_consumed_retries"] = consumed_retries + 1
            attempts = (
                int(subscription.get("wake_attempts") or 0) + 1
                if same_wake
                else 1
            )
            retry_seconds = min(
                WAKE_ACK_BASE_SECONDS * (2 ** min(attempts - 1, 10)),
                WAKE_ACK_MAX_SECONDS,
            )
            first_attempt_at = (
                str(subscription.get("wake_first_attempt_at") or now)
                if same_wake
                else now
            )
            prior_provenance = (
                dict(subscription.get("wake_provenance") or {})
                if same_wake
                and isinstance(subscription.get("wake_provenance"), Mapping)
                else {}
            )
            origin = dict(wake_origin or {})
            wake_provenance = {
                "wake_id": clean_wake_id,
                "created_at": str(prior_provenance.get("created_at") or now),
                "first_attempt_at": first_attempt_at,
                "attempted_at": now,
                "attempt": attempts,
                "host_id": bounded_text(
                    origin.get("host_id") or prior_provenance.get("host_id"),
                    field="wake_origin.host_id",
                    maximum=128,
                ),
                "relay_id": bounded_text(
                    origin.get("relay_id") or prior_provenance.get("relay_id"),
                    field="wake_origin.relay_id",
                    maximum=256,
                ),
                "process_id": bounded_text(
                    origin.get("process_id") or prior_provenance.get("process_id"),
                    field="wake_origin.process_id",
                    maximum=64,
                ),
            }
            subscription.update(
                outstanding_wake_id=clean_wake_id,
                last_wake_message_refs=_bounded_message_refs(
                    [
                        *(
                            list(subscription.get("last_wake_message_refs") or [])
                            if same_wake
                            else []
                        ),
                        *list(message_refs),
                    ]
                ),
                wake_attempts=attempts,
                wake_first_attempt_at=first_attempt_at,
                wake_last_attempt_at=now,
                wake_ack_deadline_at=_future(retry_seconds),
                wake_delivery_state="attempting",
                wake_provenance=wake_provenance,
                wake_queued_submission_ids=(
                    list(subscription.get("wake_queued_submission_ids") or [])
                    if same_wake
                    else []
                ),
                revision=int(subscription.get("revision") or 0) + 1,
            )
            if not same_wake:
                subscription["wake_observed_message_refs"] = []
                for field in WAKE_OBSERVATION_FIELDS:
                    subscription.pop(field, None)
            listener.update(
                subscription=subscription,
                heartbeat_at=now,
                revision=int(listener.get("revision") or 0) + 1,
            )
            row.update(listener=listener, updated_at=now)
            atomic_write_json(path, row)
            return self._session_with_presence(listener)

    def coalesce_worker_session_wake(
        self,
        worker_name: str,
        *,
        message_refs: Sequence[str],
        wake_id: str,
    ) -> dict[str, Any] | None:
        """Attach newly pending refs to one still-outstanding wake."""

        worker = self.read_worker(worker_name)
        clean_name = str(worker.get("worker_name") or "")
        clean_wake_id = bounded_text(
            wake_id, field="wake_id", maximum=128, required=True
        )
        path = self._worker_path(clean_name)
        with exclusive_lock(self.control / "locks" / f"worker-{clean_name}.lock"):
            row = read_json(path)
            listener = listener_without_legacy_fields(row.get("listener"))
            if not listener or listener.get("state") == "detached":
                return None
            subscription = dict(listener.get("subscription") or {})
            if str(subscription.get("outstanding_wake_id") or "") != clean_wake_id:
                return None
            current_refs = list(subscription.get("last_wake_message_refs") or [])
            merged_refs = _bounded_message_refs([*current_refs, *list(message_refs)])
            if merged_refs != current_refs:
                now = utc_now()
                subscription.update(
                    last_wake_message_refs=merged_refs,
                    revision=int(subscription.get("revision") or 0) + 1,
                )
                listener.update(
                    subscription=subscription,
                    revision=int(listener.get("revision") or 0) + 1,
                )
                row.update(listener=listener, updated_at=now)
                atomic_write_json(path, row)
            return self._session_with_presence(listener)

    def record_worker_session_delivery(
        self,
        worker_name: str,
        *,
        adapter: str,
        state: str,
        event_kind: str,
        delivered: bool,
        reason: str = "",
        message_refs: Sequence[str] = (),
        wake_id: str = "",
        prepared: bool = False,
        queued_submission_id: str = "",
    ) -> dict[str, Any]:
        worker = self.read_worker(worker_name)
        clean_name = str(worker.get("worker_name") or "")
        path = self._worker_path(clean_name)
        with exclusive_lock(self.control / "locks" / f"worker-{clean_name}.lock"):
            row = read_json(path)
            listener = listener_without_legacy_fields(row.get("listener"))
            if not listener or listener.get("state") == "detached":
                raise DomainError(
                    "field_worker_not_listening",
                    "The addressed coding-agent session is not listening.",
                    status=409,
                )
            now = utc_now()
            subscription = dict(listener.get("subscription") or {})
            clean_wake_id = (
                bounded_text(wake_id, field="wake_id", maximum=128)
                if wake_id
                else ""
            )
            if (
                prepared
                and clean_wake_id
                and str(subscription.get("outstanding_wake_id") or "")
                != clean_wake_id
            ):
                # The exact wake completed while the external queue call was
                # still running. Its acknowledgement, or a newer wake, is now
                # authoritative; this late completion must not rewrite either.
                return self._session_with_presence(listener)
            subscription.update(
                adapter=bounded_text(
                    adapter, field="subscription_adapter", maximum=128, required=True
                ),
                state=bounded_text(
                    state, field="subscription_state", maximum=128, required=True
                ),
                last_event_kind=bounded_text(
                    event_kind, field="subscription_event_kind", maximum=128
                ),
                last_attempt_at=now,
                last_error=bounded_text(
                    reason, field="subscription_error", maximum=256
                ),
                revision=int(subscription.get("revision") or 0) + 1,
            )
            if clean_wake_id and not prepared and delivered:
                outstanding_wake_id = str(
                    subscription.get("outstanding_wake_id") or ""
                )
                if outstanding_wake_id and outstanding_wake_id != clean_wake_id:
                    raise DomainError(
                        "field_worker_wake_conflict",
                        "Another wake is already outstanding for this session.",
                        status=409,
                    )
                same_wake = outstanding_wake_id == clean_wake_id
                attempts = (
                    int(subscription.get("wake_attempts") or 0) + 1
                    if same_wake
                    else 1
                )
                retry_seconds = min(
                    WAKE_ACK_BASE_SECONDS * (2 ** min(attempts - 1, 10)),
                    WAKE_ACK_MAX_SECONDS,
                )
                subscription.update(
                    outstanding_wake_id=clean_wake_id,
                    last_wake_message_refs=_bounded_message_refs(
                        [
                            *(
                                list(
                                    subscription.get("last_wake_message_refs") or []
                                )
                                if same_wake
                                else []
                            ),
                            *list(message_refs),
                        ]
                    ),
                    wake_attempts=attempts,
                    wake_first_attempt_at=(
                        str(subscription.get("wake_first_attempt_at") or now)
                        if same_wake
                        else now
                    ),
                    wake_last_attempt_at=now,
                    wake_ack_deadline_at=_future(retry_seconds),
                )
                if not same_wake:
                    subscription["wake_observed_message_refs"] = []
                    for field in WAKE_OBSERVATION_FIELDS:
                        subscription.pop(field, None)
            wake_is_still_outstanding = bool(
                clean_wake_id
                and str(subscription.get("outstanding_wake_id") or "")
                == clean_wake_id
            )
            if wake_is_still_outstanding and queued_submission_id:
                submission_ids = [
                    *list(subscription.get("wake_queued_submission_ids") or []),
                    bounded_text(
                        queued_submission_id,
                        field="queued_submission_id",
                        maximum=128,
                        required=True,
                    ),
                ]
                subscription["wake_queued_submission_ids"] = list(
                    dict.fromkeys(submission_ids)
                )[-MAX_WAKE_QUEUE_SUBMISSIONS:]
            if wake_is_still_outstanding:
                # A queue that accepted the submission holds it. Nothing here
                # says the model took it, so the wake is queued, not delivered,
                # and its deadline stays: when the acknowledgement does not
                # come, the relay re-reads the queue instead of trusting the
                # acceptance forever.
                subscription["wake_delivery_state"] = (
                    "queued" if delivered else "failed"
                )
                if delivered:
                    subscription["wake_queued_confirmed_at"] = now
                elif int(subscription.get("wake_consumed_retries") or 0) >= 1:
                    # The one retry a consumed wake gets failed at the queue
                    # boundary. The failure is the observation; report it now.
                    subscription.setdefault("wake_retry_exhausted_since", now)
            if delivered:
                subscription["last_delivered_at"] = now
            elif clean_wake_id and not prepared and wake_is_still_outstanding:
                # A retry attempt that could not reach the queue still advances
                # its bounded backoff. It does not replace or acknowledge the
                # outstanding wake.
                attempts = int(subscription.get("wake_attempts") or 0) + 1
                retry_seconds = min(
                    WAKE_ACK_BASE_SECONDS * (2 ** min(attempts - 1, 10)),
                    WAKE_ACK_MAX_SECONDS,
                )
                subscription.update(
                    wake_attempts=attempts,
                    wake_last_attempt_at=now,
                    wake_ack_deadline_at=_future(retry_seconds),
                )
            if clean_wake_id and not delivered and wake_is_still_outstanding:
                # A queue command can time out after committing its row. The
                # relay inspects the native queue before it is allowed to add
                # another submission for this wake.
                subscription["queue_reconciliation_required"] = True
            listener.update(
                subscription=subscription,
                heartbeat_at=now,
                revision=int(listener.get("revision") or 0) + 1,
            )
            row.update(listener=listener, updated_at=now)
            atomic_write_json(path, row)
            return self._session_with_presence(listener)

    def record_worker_session_queue_reconciliation(
        self,
        worker_name: str,
        *,
        expected_wake_id: str,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Record one native-queue comparison without overwriting a newer wake."""

        worker = self.read_worker(worker_name)
        clean_name = str(worker.get("worker_name") or "")
        clean_expected = bounded_text(
            expected_wake_id, field="wake_id", maximum=128
        )
        path = self._worker_path(clean_name)
        with exclusive_lock(self.control / "locks" / f"worker-{clean_name}.lock"):
            row = read_json(path)
            listener = listener_without_legacy_fields(row.get("listener"))
            if not listener or listener.get("state") == "detached":
                raise DomainError(
                    "field_worker_not_listening",
                    "The addressed coding-agent session is not listening.",
                    status=409,
                )
            now = utc_now()
            subscription = dict(listener.get("subscription") or {})
            current_wake_id = str(
                subscription.get("outstanding_wake_id") or ""
            )
            reconciled = bool(result.get("reconciled"))
            snapshot_is_current = current_wake_id == clean_expected
            active_submission_ids = [
                bounded_text(
                    value,
                    field="queued_submission_id",
                    maximum=128,
                    required=True,
                )
                for value in list(result.get("queued_submission_ids") or [])[
                    -MAX_WAKE_QUEUE_SUBMISSIONS:
                ]
            ]
            if reconciled and snapshot_is_current:
                subscription["queue_reconciliation_required"] = False
                if current_wake_id:
                    expected_ids_known = bool(
                        subscription.get("wake_queued_submission_ids")
                    )
                    subscription["wake_queued_submission_ids"] = (
                        active_submission_ids
                    )
                    _observe_outstanding_wake(
                        subscription,
                        listed=bool(active_submission_ids),
                        expected=expected_ids_known,
                        now=now,
                    )
            else:
                subscription["queue_reconciliation_required"] = True
            subscription["last_queue_reconciliation"] = {
                "state": bounded_text(
                    result.get("state") or "unknown",
                    field="queue_reconciliation.state",
                    maximum=64,
                    required=True,
                ),
                "expected_wake_id": clean_expected,
                "observed_outstanding_wake_id": current_wake_id,
                "snapshot_is_current": snapshot_is_current,
                "reconciled": reconciled,
                "queued_submission_ids": active_submission_ids,
                "deleted_submission_ids": [
                    bounded_text(
                        value,
                        field="queued_submission_id",
                        maximum=128,
                        required=True,
                    )
                    for value in list(result.get("deleted_submission_ids") or [])[
                        -MAX_WAKE_QUEUE_SUBMISSIONS:
                    ]
                ],
                "delete_race_submission_ids": [
                    bounded_text(
                        value,
                        field="queued_submission_id",
                        maximum=128,
                        required=True,
                    )
                    for value in list(
                        result.get("delete_race_submission_ids") or []
                    )[-MAX_WAKE_QUEUE_SUBMISSIONS:]
                ],
                "stale_wake_ids": [
                    bounded_text(
                        value, field="wake_id", maximum=128, required=True
                    )
                    for value in list(result.get("stale_wake_ids") or [])[
                        -MAX_WAKE_ACKNOWLEDGEMENTS:
                    ]
                ],
                "reason": bounded_text(
                    result.get("reason"),
                    field="queue_reconciliation.reason",
                    maximum=256,
                ),
                "recorded_at": now,
            }
            listener.update(
                subscription=subscription,
                heartbeat_at=now,
                revision=int(listener.get("revision") or 0) + 1,
            )
            row.update(listener=listener, updated_at=now)
            atomic_write_json(path, row)
            return self._session_with_presence(listener)

    def pending_worker_mail_refs(self, worker_name: str) -> list[str]:
        """The authoritative list, under the lock, with expired mail recovered.

        This holds the mailbox lock on purpose: it recovers expired mail, which
        is a write, and its callers act on what it returns. Reachability must
        not call it, because reporting already holds this lock and a second
        acquisition deadlocks the process against itself. Reachability uses
        pending_worker_mail_count_snapshot instead, which is diagnostic and
        lock-free.
        """

        worker = self.read_worker(worker_name)
        clean_name = str(worker.get("worker_name") or "")
        refs: list[str] = []

        def collect(project_id: str, root: Path) -> None:
            with exclusive_lock(root.parent / ".mail.lock"):
                self._recover_expired_mail(project_id, clean_name)
                for path in sorted(root.glob("*.json")):
                    row = read_json(path)
                    message_ref = str(row.get("message_ref") or "")
                    if message_ref:
                        refs.append(message_ref)

        collect("", self._mail_root("", clean_name) / "inbox")
        for project_ref in worker.get("attended_project_refs") or []:
            parsed = parse_ref(str(project_ref))
            if parsed.kind != "project":
                continue
            collect(parsed.object_id, self._mail_root(parsed.object_id, clean_name) / "inbox")
        return refs

    def pending_worker_mail_count_snapshot(self, worker_name: str) -> int:
        """Count readable or expired mail without taking a mailbox lock.

        Reachability is diagnostic state and is also embedded in reports made
        while a worker's mailbox is locked. Taking that lock again here
        deadlocks the reporting process. Atomic file replacement makes a
        lock-free snapshot safe: a concurrent move can make this count briefly
        old, but it cannot corrupt mail or change delivery state.
        """

        worker = self.read_worker(worker_name)
        clean_name = str(worker.get("worker_name") or "")
        project_ids = [""]
        for project_ref in worker.get("attended_project_refs") or []:
            parsed = parse_ref(str(project_ref))
            if parsed.kind == "project":
                project_ids.append(parsed.object_id)

        refs: set[str] = set()
        now = datetime.now(timezone.utc)
        for project_id in project_ids:
            root = self._mail_root(project_id, clean_name)
            for path in sorted((root / "inbox").glob("*.json")):
                row = read_json(path, required=False)
                message_ref = str(row.get("message_ref") or "")
                if message_ref:
                    refs.add(message_ref)
            for path in sorted((root / "leased").glob("*.json")):
                row = read_json(path, required=False)
                lease = (
                    row.get("lease")
                    if isinstance(row.get("lease"), Mapping)
                    else {}
                )
                expires_at = str(lease.get("expires_at") or "")
                try:
                    expired = bool(expires_at and parse_utc(expires_at) <= now)
                except DomainError:
                    expired = False
                message_ref = str(row.get("message_ref") or "")
                if expired and message_ref:
                    refs.add(message_ref)
        return len(refs)

    def _worker_mail_scopes(self, worker_name: str) -> tuple[str, list[tuple[str, str]]]:
        worker = self.read_worker(worker_name)
        clean_worker = str(worker.get("worker_name") or "")
        project_ids: set[str] = set()
        for project_ref in worker.get("attended_project_refs") or []:
            parsed = parse_ref(str(project_ref))
            if parsed.kind == "project":
                project_ids.add(parsed.object_id)
        # Detached projects can still contain leases owned by this session.
        for project_path in (self.control / "projects").glob("*"):
            if (
                project_path.is_dir()
                and (project_path / "mail" / clean_worker / "leased").is_dir()
            ):
                project_ids.add(project_path.name)
        scopes = [("@direct", "")]
        scopes.extend(
            (f"project:{project_id}", project_id)
            for project_id in sorted(project_ids)
        )
        return clean_worker, scopes

    def list_worker_mail_leases(
        self,
        worker_name: str,
        *,
        lease_owner: str,
        cursor: str = "",
        limit: int = 20,
    ) -> dict[str, Any]:
        """Page the active mail leases held by one exact worker session."""

        clean_worker, scopes = self._worker_mail_scopes(worker_name)
        clean_owner = bounded_text(
            lease_owner,
            field="lease_owner",
            maximum=512,
            required=True,
        )
        rows: list[dict[str, Any]] = []
        for scope_key, project_id in scopes:
            root = self._mail_root(project_id, clean_worker)
            with exclusive_lock(root / ".mail.lock"):
                self._recover_expired_mail(project_id, clean_worker)
                for path in sorted((root / "leased").glob("*.json")):
                    message = read_json(path, required=False)
                    lease = (
                        dict(message.get("lease") or {})
                        if isinstance(message.get("lease"), Mapping)
                        else {}
                    )
                    if str(lease.get("owner") or "") != clean_owner:
                        continue
                    message_ref = str(message.get("message_ref") or "")
                    lease_id = str(lease.get("lease_id") or "")
                    if not message_ref or not lease_id:
                        continue
                    rows.append(
                        {
                            "_scope_key": scope_key,
                            "project_ref": (
                                make_ref("project", project_id) if project_id else ""
                            ),
                            "project_id": project_id,
                            "message_ref": message_ref,
                            "lease_id": lease_id,
                            "lease_ref": str(lease.get("lease_ref") or ""),
                            "leased_at": str(lease.get("leased_at") or ""),
                            "expires_at": str(lease.get("expires_at") or ""),
                            "renewals": int(lease.get("renewals") or 0),
                            "sender": str(message.get("sender") or ""),
                            "kind": str(message.get("kind") or ""),
                            "message_bytes": len(
                                json.dumps(
                                    message,
                                    ensure_ascii=True,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                ).encode("utf-8")
                            ),
                        }
                    )

        rows.sort(key=lambda row: (str(row["_scope_key"]), row["message_ref"]))
        codec = ScopedKeysetCursor(
            scope={"worker_name": clean_worker, "lease_owner": clean_owner},
            query={"collection": "active_mail_leases"},
            key_fields=("mailbox", "message_ref"),
        )
        boundary: tuple[Any, ...] | None = None
        if str(cursor or "").strip():
            try:
                boundary = codec.decode(
                    bounded_text(cursor, field="cursor", maximum=4000)
                )
            except CollectionError as exc:
                raise DomainError(exc.code, exc.message) from exc
        available = [
            row
            for row in rows
            if boundary is None
            or (str(row["_scope_key"]), row["message_ref"])
            > (str(boundary[0]), str(boundary[1]))
        ]
        page_limit = max(1, min(int(limit), 200))
        page = available[:page_limit]
        next_cursor = ""
        if len(available) > page_limit and page:
            next_cursor = codec.encode(
                (str(page[-1]["_scope_key"]), page[-1]["message_ref"])
            )
        items = [
            {key: value for key, value in row.items() if key != "_scope_key"}
            for row in page
        ]
        return {
            "schema": MAIL_LEASE_PAGE_SCHEMA,
            "worker_name": clean_worker,
            "total": len(rows),
            "count": len(items),
            "items": items,
            "next_cursor": next_cursor,
        }

    def read_worker_mail_lease(
        self,
        project_id: str,
        *,
        worker_name: str,
        message_ref: str,
        lease_id: str,
        lease_owner: str,
    ) -> dict[str, Any]:
        """Re-read one active lease without changing or extending its claim."""

        parsed = parse_ref(message_ref)
        if parsed.kind != "mail":
            raise DomainError("field_mail_ref_invalid", "Expected a work:mail reference.")
        clean_project = (
            component(project_id, field="project_id")
            if str(project_id or "").strip()
            else ""
        )
        clean_worker, scopes = self._worker_mail_scopes(worker_name)
        clean_owner = bounded_text(
            lease_owner,
            field="lease_owner",
            maximum=512,
            required=True,
        )
        requested_ref = make_ref("project", clean_project) if clean_project else ""
        name = f"{component(parsed.object_id)}.json"
        ordered_scopes = [
            clean_project,
            *(scope for _, scope in scopes if scope != clean_project),
        ]
        for scope in ordered_scopes:
            root = self._mail_root(scope, clean_worker)
            if not root.is_dir():
                continue
            actual_ref = make_ref("project", scope) if scope else ""
            with exclusive_lock(root / ".mail.lock"):
                self._recover_expired_mail(scope, clean_worker)
                row = read_json(root / "leased" / name, required=False)
                if row and str(row.get("message_ref") or "") == message_ref:
                    lease = (
                        dict(row.get("lease") or {})
                        if isinstance(row.get("lease"), Mapping)
                        else {}
                    )
                    if str(lease.get("lease_id") or "") != str(lease_id):
                        if scope == clean_project:
                            raise DomainError(
                                "field_mail_lease_mismatch",
                                "The requested lease ID is not the message's active lease.",
                                status=409,
                            )
                        continue
                    if str(lease.get("owner") or "") != clean_owner:
                        if scope == clean_project:
                            raise DomainError(
                                "field_mail_lease_owner_mismatch",
                                "Only the session holding this lease may read it.",
                                status=403,
                            )
                        continue
                    if scope != clean_project:
                        actual_scope = actual_ref or "the direct worker mailbox"
                        requested_scope = requested_ref or "the direct worker mailbox"
                        retry = (
                            f"Use --project-ref {actual_ref}."
                            if actual_ref
                            else "Omit --project-ref for direct mail."
                        )
                        raise DomainError(
                            "field_mail_lease_scope_mismatch",
                            f"This session holds the lease in {actual_scope}, "
                            f"not {requested_scope}. {retry}",
                            status=409,
                            details={
                                "message_ref": message_ref,
                                "lease_id": lease_id,
                                "requested_project_ref": requested_ref,
                                "actual_project_ref": actual_ref,
                                "mailbox_state": "leased",
                            },
                        )
                    # A stubbed lease reads back as the same stub, never the full body.
                    return _delivered_view(dict(row))
                for mailbox_state, code, guidance in (
                    (
                        "inbox",
                        "field_mail_lease_returned_to_inbox",
                        "The message is back in the inbox. Receive it again for a new lease.",
                    ),
                    (
                        "processed",
                        "field_mail_already_settled",
                        "The message was already settled; do not handle it again.",
                    ),
                    (
                        "quarantine",
                        "field_mail_lease_quarantined",
                        "The message is quarantined; inspect the worker quarantine list.",
                    ),
                ):
                    previous = read_json(root / mailbox_state / name, required=False)
                    if not previous or str(previous.get("message_ref") or "") != message_ref:
                        continue
                    if mailbox_state == "inbox" and str(
                        previous.get("delivery_status") or ""
                    ) not in {"redelivered", "receive_rolled_back"}:
                        code = "field_mail_lease_not_active"
                        guidance = "The message is pending in the inbox and has no active lease."
                    raise DomainError(
                        code,
                        guidance,
                        status=409,
                        details={
                            "message_ref": message_ref,
                            "lease_id": lease_id,
                            "requested_project_ref": requested_ref,
                            "actual_project_ref": actual_ref,
                            "mailbox_state": mailbox_state,
                            "delivery_status": str(previous.get("delivery_status") or ""),
                        },
                    )
        raise DomainError(
            "field_mail_lease_not_found",
            "This worker has no mailbox record for the requested message.",
            status=404,
            details={
                "message_ref": message_ref,
                "lease_id": lease_id,
                "requested_project_ref": requested_ref,
                "reason": "absent",
            },
        )

    def read_worker_mail_attachment(
        self,
        project_id: str,
        *,
        worker_name: str,
        message_ref: str,
        lease_id: str,
        lease_owner: str,
        file_ref: str,
    ) -> dict[str, Any]:
        """Resolve one attachment only for the session holding its mail lease."""

        clean_project = (
            component(project_id, field="project_id")
            if str(project_id or "").strip()
            else ""
        )
        clean_worker = component(worker_name, field="worker_name").lower()
        message = self.read_worker_mail_lease(
            clean_project,
            worker_name=clean_worker,
            message_ref=message_ref,
            lease_id=lease_id,
            lease_owner=lease_owner,
        )
        attachment = attachment_by_ref(message, file_ref=file_ref)
        attachment = verified_attachment(
            attachment,
            attachment_root=(
                self._mail_root(clean_project, clean_worker) / "attachments"
            ),
            message_ref=message_ref,
            verify_hash=True,
        )
        return {
            "schema": WORKER_ATTACHMENT_READ_SCHEMA,
            "project_ref": str(message.get("project_ref") or ""),
            "message_ref": str(message.get("message_ref") or ""),
            "lease_id": str(lease_id),
            "attachment": {
                "schema": str(attachment.get("schema") or ""),
                "filename": str(attachment.get("filename") or ""),
                "mime": str(attachment.get("mime") or ""),
                "size": int(attachment.get("size") or 0),
                "file_ref": str(attachment.get("file_ref") or ""),
                "sha256": str(attachment.get("sha256") or ""),
                "local_path": str(attachment.get("local_path") or ""),
            },
            "instruction": (
                "Read the exact local_path returned here as message input before "
                "handling or settling this lease."
            ),
        }

    def record_worker_mail_settlement(
        self, worker_name: str, *, message_ref: str
    ) -> dict[str, Any]:
        worker = self.read_worker(worker_name)
        clean_name = str(worker.get("worker_name") or "")
        path = self._worker_path(clean_name)
        with exclusive_lock(self.control / "locks" / f"worker-{clean_name}.lock"):
            row = read_json(path)
            listener = listener_without_legacy_fields(row.get("listener"))
            if not listener or listener.get("state") == "detached":
                return {}
            now = utc_now()
            listener.update(
                last_mail_settled_at=now,
                last_settled_message_ref=bounded_text(
                    message_ref, field="message_ref", maximum=256, required=True
                ),
                heartbeat_at=now,
                revision=int(listener.get("revision") or 0) + 1,
            )
            row.update(listener=listener, updated_at=now)
            atomic_write_json(path, row)
            return self._session_with_presence(listener)

    def _session_root(self, project_id: str, worker_name: str) -> Path:
        return (
            self._project_dir(project_id)
            / "sessions"
            / component(worker_name, field="worker_name").lower()
        )

    def _session_path(self, project_id: str, worker_name: str, session_id: str) -> Path:
        return self._session_root(project_id, worker_name) / (
            f"{component(session_id, field='session_id')}.json"
        )

    def attach_session(
        self,
        project_id: str,
        *,
        worker_name: str,
        session_id: str,
        check_interval_seconds: int = 30,
    ) -> dict[str, Any]:
        clean_project = component(project_id, field="project_id")
        clean_worker = component(worker_name, field="worker_name").lower()
        clean_session = component(session_id, field="session_id")
        self.read_project(clean_project)
        worker = self.read_worker(clean_worker)
        if worker.get("pool_status") != "active":
            raise DomainError(
                "field_worker_limbo",
                "A worker in limbo cannot attach an agent session.",
                status=403,
            )
        interval = max(5, min(int(check_interval_seconds), 3600))
        path = self._session_path(clean_project, clean_worker, clean_session)
        lock = self._session_root(clean_project, clean_worker) / ".sessions.lock"
        with exclusive_lock(lock):
            existing = read_json(path, required=False)
            now = utc_now()
            row = {
                "schema": SESSION_SCHEMA,
                "session_id": clean_session,
                "project_ref": make_ref("project", clean_project),
                "worker_name": clean_worker,
                "runtime_kind": str(worker.get("runtime_kind") or ""),
                "state": (
                    "waiting"
                    if existing.get("state") == "detached"
                    else str(existing.get("state") or "waiting")
                ),
                "check_interval_seconds": interval,
                "last_inbox_check_at": str(existing.get("last_inbox_check_at") or ""),
                "last_message_refs": list(existing.get("last_message_refs") or []),
                "last_control_refs": list(existing.get("last_control_refs") or []),
                "attached_at": str(existing.get("attached_at") or now),
                "resumed_at": now if existing.get("state") == "detached" else str(existing.get("resumed_at") or ""),
                "heartbeat_at": now,
                "detached_at": "",
                "revision": int(existing.get("revision") or 0) + 1,
            }
            atomic_write_json(path, row)
            return self._session_with_presence(row)

    def check_in_session(
        self,
        project_id: str,
        *,
        worker_name: str,
        session_id: str,
        state: str,
        inbox_checked: bool = False,
        message_refs: Sequence[str] = (),
        control_refs: Sequence[str] = (),
    ) -> dict[str, Any]:
        clean_project = component(project_id, field="project_id")
        clean_worker = component(worker_name, field="worker_name").lower()
        clean_session = component(session_id, field="session_id")
        normalized_state = str(state or "").strip().lower()
        if normalized_state not in SESSION_STATES - {"detached"}:
            raise DomainError(
                "field_session_state_invalid",
                "An attached agent session state must be waiting, working, or blocked.",
                details={"allowed": sorted(SESSION_STATES - {"detached"})},
            )
        path = self._session_path(clean_project, clean_worker, clean_session)
        lock = self._session_root(clean_project, clean_worker) / ".sessions.lock"
        with exclusive_lock(lock):
            row = read_json(path)
            if row.get("state") == "detached":
                raise DomainError(
                    "field_session_detached",
                    "A detached agent session cannot check in.",
                    status=409,
                )
            now = utc_now()
            row.update(
                state=normalized_state,
                heartbeat_at=now,
                revision=int(row.get("revision") or 0) + 1,
            )
            if inbox_checked:
                row["last_inbox_check_at"] = now
                row["last_message_refs"] = [
                    bounded_text(value, field="message_ref", maximum=256)
                    for value in list(message_refs)[-20:]
                ]
                row["last_control_refs"] = [
                    bounded_text(value, field="control_ref", maximum=256)
                    for value in list(control_refs)[-20:]
                ]
            atomic_write_json(path, row)
            return self._session_with_presence(row)

    def detach_session(
        self,
        project_id: str,
        *,
        worker_name: str,
        session_id: str,
    ) -> dict[str, Any]:
        clean_project = component(project_id, field="project_id")
        clean_worker = component(worker_name, field="worker_name").lower()
        clean_session = component(session_id, field="session_id")
        path = self._session_path(clean_project, clean_worker, clean_session)
        lock = self._session_root(clean_project, clean_worker) / ".sessions.lock"
        with exclusive_lock(lock):
            row = read_json(path)
            if row.get("state") != "detached":
                now = utc_now()
                row.update(
                    state="detached",
                    heartbeat_at=now,
                    detached_at=now,
                    revision=int(row.get("revision") or 0) + 1,
                )
                atomic_write_json(path, row)
            return self._session_with_presence(row)

    @staticmethod
    def _session_with_presence(row: Mapping[str, Any]) -> dict[str, Any]:
        value = dict(row)
        state = str(value.get("state") or "waiting")
        interval = max(5, int(value.get("check_interval_seconds") or 30))
        value["inbox_check_interval_seconds"] = interval
        if state == "detached":
            value["presence"] = "detached"
            value["heartbeat_age_seconds"] = 0
            value["inbox_check_state"] = "detached"
            value["inbox_check_age_seconds"] = None
            value["inbox_overdue_by_seconds"] = 0
            return value
        last_inbox_check = str(value.get("last_inbox_check_at") or "")
        stale_after = max(30, interval * 3)
        value["stale_after_seconds"] = stale_after
        if not last_inbox_check:
            value["presence"] = "stale"
            value["heartbeat_age_seconds"] = None
            value["inbox_check_state"] = "not_checked"
            value["inbox_check_age_seconds"] = None
            value["inbox_overdue_by_seconds"] = None
        else:
            heartbeat = parse_utc(last_inbox_check)
            age = max(
                0, int((datetime.now(timezone.utc) - heartbeat).total_seconds())
            )
            value["heartbeat_age_seconds"] = age
            value["inbox_check_age_seconds"] = age
            value["inbox_overdue_by_seconds"] = max(0, age - interval)
            if age > stale_after:
                value["inbox_check_state"] = "stale"
                value["presence"] = "stale"
            elif age > interval:
                value["inbox_check_state"] = "overdue"
                value["presence"] = state
            else:
                value["inbox_check_state"] = "current"
                value["presence"] = state
        subscription = (
            value.get("subscription")
            if isinstance(value.get("subscription"), Mapping)
            else {}
        )
        overdue_state, _ = _overdue_wake_state(subscription)
        if overdue_state:
            # The attention state stands beside presence and does not rewrite
            # it: a live session with a delayed wake reads working and
            # queued_overdue at once. Presence is what the inbox checks say;
            # calling a session dead needs evidence about the session, not a
            # timer on its queue; keep facts apart from
            # classification).
            value["wake_state"] = overdue_state
        return value

    def list_sessions(
        self,
        project_id: str,
        *,
        worker_name: str,
        include_detached: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clean_project = component(project_id, field="project_id")
        clean_worker = component(worker_name, field="worker_name").lower()
        rows = [
            self._session_with_presence(row)
            for row in json_records(self._session_root(clean_project, clean_worker))
        ]
        if not include_detached:
            rows = [row for row in rows if row.get("state") != "detached"]
        ordered = sorted(
            rows, key=lambda row: str(row.get("heartbeat_at") or ""), reverse=True
        )
        return ordered[: max(1, min(int(limit), 1000))]

    def _mail_root(self, project_id: str, worker_name: str) -> Path:
        clean_worker = component(worker_name, field="worker_name").lower()
        if not str(project_id or "").strip():
            return self.control / "workers" / clean_worker / "mail"
        return self._project_dir(project_id) / "mail" / clean_worker

    def _mail_lock(self, project_id: str, worker_name: str) -> Path:
        if not str(project_id or "").strip():
            return self._mail_root("", worker_name) / ".mail.lock"
        return self._project_lock(project_id)

    def _mail_ignored_root(self, project_id: str, worker_name: str) -> Path:
        if not str(project_id or "").strip():
            return self._mail_root("", worker_name) / "ignored"
        return self._project_dir(project_id) / "mail" / "ignored"

    def _mail_idempotency_path(self, project_id: str, sender: str, key: str) -> Path:
        token = content_hash({"sender": sender, "key": key})
        if not str(project_id or "").strip():
            return (
                self.control
                / "workers"
                / component(sender, field="sender").lower()
                / "idempotency"
                / "mail"
                / f"{token}.json"
            )
        return self._project_dir(project_id) / "idempotency" / "mail" / f"{token}.json"

    def _mail_record_unlocked(
        self, project_id: str, worker_name: str, message_ref: str
    ) -> dict[str, Any]:
        parsed = parse_ref(message_ref)
        if parsed.kind != "mail":
            return {}
        root = self._mail_root(project_id, worker_name)
        name = f"{component(parsed.object_id)}.json"
        for state in ("inbox", "leased", "processed", "quarantine"):
            row = read_json(root / state / name, required=False)
            if row:
                return row
        return read_json(
            self._mail_ignored_root(project_id, worker_name) / name,
            required=False,
        )

    def _operator_response_path(self, worker_name: str, message_ref: str) -> Path:
        token = content_hash({"message_ref": str(message_ref or "")})
        return (
            self.control
            / "operator-responses"
            / component(worker_name, field="worker_name").lower()
            / f"{token}.json"
        )

    def _team_alias(self, project_id: str, worker_name: str) -> str:
        """The display alias this machine last heard for a teammate, or "".

        Peer mail is addressed by the stable worker name, which is a runtime
        kind plus a session UUID and is unreadable to a person. The relay
        already mirrors the project roster into the local field on every
        heartbeat, so the alias is available here without a control-plane
        round trip. When the roster has not arrived yet the label simply
        stays the stable name, which is correct but ugly, never wrong.
        """
        clean = str(worker_name or "").strip().lower()
        if not clean or not str(project_id or "").strip():
            return ""
        try:
            members = self.read_project_team(project_id)
        except Exception:
            return ""
        for member in members:
            if str(member.get("worker_name") or "").strip().lower() == clean:
                return str(member.get("worker_alias") or "").strip()
        return ""

    def resolve_mail_recipient(self, project_id: str, recipient: str) -> dict[str, Any]:
        """Resolve a stable address before selecting a transport."""

        address = str(recipient or "").strip().lower()
        if address in {"operator", "owner"}:
            return {
                "worker_name": address,
                "route": "remote",
                "pool_status": "active",
            }

        local_workers = {
            str(row.get("worker_name") or "").strip().lower(): row
            for row in json_records(self.control / "workers")
            if str(row.get("worker_name") or "").strip()
        }
        directory_record = (
            self.read_project_mail_recipients(project_id)
            if str(project_id or "").strip()
            else {"recipients": [], "updated_at": ""}
        )
        directory = {
            str(row.get("worker_name") or "").strip().lower(): dict(row)
            for row in directory_record.get("recipients") or []
            if isinstance(row, Mapping)
            and str(row.get("worker_name") or "").strip()
        }
        # The local record is the authority for a session hosted here. A
        # retirement or attendance change must beat an older heartbeat
        # snapshot that still called the same worker addressable.
        project_ref = make_ref("project", project_id) if project_id else ""
        for worker_name, row in local_workers.items():
            attends = project_ref in {
                str(value) for value in row.get("attended_project_refs") or []
            }
            attendance_observed = bool(
                str(row.get("attendances_observed_at") or "").strip()
            )
            if (
                project_ref
                and attendance_observed
                and not attends
                and str(row.get("pool_status") or "active").lower() != "retired"
            ):
                directory[worker_name] = {**row, "pool_status": "not_linked"}
            else:
                directory[worker_name] = row
        resolved = resolve_mail_recipient(
            address,
            list(directory.values()),
            error_namespace="field",
            directory_updated_at=str(directory_record.get("updated_at") or ""),
        )
        route = "local" if resolved.worker_name in local_workers else "remote"
        return {**resolved.as_mapping(), "route": route}

    @staticmethod
    def _require_work_ref_shape(work_ref: str) -> None:
        """Reject malformed work URIs before any local record is written."""
        try:
            parse_plan_node_ref(work_ref)
        except DomainError as exc:
            raise DomainError(
                "field_work_ref_invalid",
                f"{work_ref} is not a canonical plan-node reference.",
                status=400,
                details={"work_ref": work_ref, "reason": exc.code},
            ) from exc

    @staticmethod
    def _consumed_substitution_signature(text: str) -> list[str]:
        """Spot a body whose identifiers the shell ate before we ever saw them.

        A body passed as a double-quoted shell argument is expanded before the
        command runs. Backticks execute as command substitution and are
        replaced by empty output, and a dollar sign followed by a name is
        replaced by that variable's value. The damage is done before the body
        reaches us, so nothing here can recover it.

        What is left behind is a shape. Removing `Name` from "imports `Name`
        from" leaves the spaces that surrounded it, so the text reads "imports
        from" with a gap where the evidence was. The result still parses as
        English, which is exactly why this went unnoticed: a message missing the
        module it is about looks like careless writing, not data loss.

        So the sender is told, and only told. Refusing the mail would be worse
        than the defect, because a false positive would stop a legitimate
        message, and mail that cannot be sent is the one failure this whole
        surface exists to avoid.
        """

        import re

        # Fenced blocks and indented code align things on purpose.
        scannable: list[str] = []
        fenced = False
        for line in str(text or "").splitlines():
            if line.lstrip().startswith("```"):
                fenced = not fenced
                continue
            if fenced or line.startswith("    ") or "|" in line:
                continue
            scannable.append(line)

        found: list[str] = []
        gap = re.compile(r"[A-Za-z0-9]  +[A-Za-z0-9]")
        orphan = re.compile(r"[A-Za-z0-9] [;,.](?:\s|$)")
        for line in scannable:
            for pattern in (gap, orphan):
                match = pattern.search(line)
                if match:
                    start = max(0, match.start() - 30)
                    found.append(line[start : match.end() + 30].strip())
                    break
            if len(found) >= 3:
                break
        return found

    @classmethod
    def _body_advisory(cls, body: str) -> dict[str, Any] | None:
        """The warning attached to a send, or nothing when the body looks whole."""

        excerpts = cls._consumed_substitution_signature(body)
        if not excerpts:
            return None
        return {
            "code": "body_may_have_lost_shell_substitutions",
            "message": (
                "This body has gaps where a shell substitution would have "
                "removed text. If it was passed with --body, backticked "
                "identifiers and $variables were deleted before sending. "
                "Compose with a quoted heredoc and --body-file instead."
            ),
            "excerpts": excerpts,
        }

    def send_mail(
        self,
        project_id: str,
        *,
        sender: str,
        recipient: str,
        kind: str,
        subject: str,
        body: str,
        payload: Mapping[str, Any] | None = None,
        work_ref: str = "",
        correlation_id: str = "",
        reply_to: str = "",
        idempotency_key: str,
        sender_identity: Mapping[str, Any] | None = None,
        idempotency_identity: Mapping[str, Any] | None = None,
        idempotency_alias_keys: Sequence[str] = (),
        idempotency_identity_aliases: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        clean_project = (
            component(project_id, field="project_id")
            if str(project_id or "").strip()
            else ""
        )
        clean_sender = component(sender, field="sender").lower()
        resolution = self.resolve_mail_recipient(clean_project, recipient)
        if resolution["route"] != "local":
            raise DomainError(
                "field_mail_route_mismatch",
                "The addressed worker belongs to another host; use the remote route.",
                status=409,
                details={
                    "recipient": resolution["worker_name"],
                    "required_route": "remote",
                },
            )
        clean_recipient = str(resolution["worker_name"])
        key = bounded_text(idempotency_key, field="idempotency_key", maximum=512, required=True)
        clean_kind = bounded_text(kind, field="kind", maximum=128, required=True)
        identity = _sender_identity(sender_identity, fallback=clean_sender)
        if identity.get("kind") == "worker" and identity.get("label") == clean_sender:
            alias = self._team_alias(clean_project, clean_sender)
            if alias:
                identity["label"] = bounded_text(
                    alias, field="sender_identity.label", maximum=512
                )
        clean_subject = bounded_text(subject, field="subject", maximum=2000, required=True)
        clean_body = bounded_text(body, field="body", maximum=MAX_MAIL_BYTES)
        clean_payload = dict(payload or {})
        clean_attachments = stored_attachment_manifest({"payload": clean_payload})
        clean_work_ref = bounded_text(
            work_ref, field="work_ref", maximum=MAX_WORK_ITEM_REF_BYTES
        )
        if clean_work_ref and clean_project:
            self._require_work_ref_shape(clean_work_ref)
        # Delivery is not reading. Mail to a worker that is not listening lands
        # in a mailbox nobody opens, and the sender has no way to know: three
        # times in one evening a coordinator answered an idle worker by sending
        # it mail, which is the one thing that cannot reach a worker who is not
        # looking. So the send still succeeds, because the mail should be there
        # when it returns, and the sender is told.
        reach = self.worker_reachability(clean_recipient)

        if not clean_project:
            if clean_sender != "control-plane":
                raise DomainError(
                    "field_project_context_required",
                    "Worker-to-worker mail requires a shared project.",
                    status=409,
                )
            if clean_kind not in DIRECT_MAIL_KINDS:
                raise DomainError(
                    "field_direct_mail_kind_invalid",
                    "A direct worker mailbox accepts delivery_failed, "
                    "discard.notice, ping, reply, or request.",
                    details={"allowed": sorted(DIRECT_MAIL_KINDS)},
                )
            if clean_work_ref:
                raise DomainError(
                    "field_project_context_required",
                    "Direct worker mail cannot name project work.",
                    status=409,
                )
        clean_correlation_id = bounded_text(
            correlation_id, field="correlation_id", maximum=512
        )
        clean_reply_to = bounded_text(reply_to, field="reply_to", maximum=512)
        request_hash = content_hash(
            {
                "route": "local",
                "recipient": clean_recipient,
                "kind": clean_kind,
                "subject": clean_subject,
                "body": clean_body,
                "payload": clean_payload,
                "work_ref": clean_work_ref,
                "correlation_id": clean_correlation_id,
                "reply_to": clean_reply_to,
            }
        )
        replay_identity = dict(idempotency_identity or {})
        replay_identity_aliases = [
            dict(value)
            for value in idempotency_identity_aliases
            if isinstance(value, Mapping)
        ]
        replay_identity_hash = (
            content_hash(replay_identity) if replay_identity else ""
        )
        alias_keys: list[str] = []
        for value in idempotency_alias_keys:
            alias = bounded_text(
                value,
                field="idempotency_alias_key",
                maximum=512,
                required=True,
            )
            if alias != key and alias not in alias_keys:
                alias_keys.append(alias)
        with exclusive_lock(self._mail_lock(clean_project, clean_recipient)):
            if clean_project:
                self.read_project(clean_project)
            else:
                self.read_worker(clean_recipient)
            receipt_path = self._mail_idempotency_path(clean_project, clean_sender, key)
            receipt = read_json(receipt_path, required=False)
            receipt_source_path = receipt_path
            if not receipt:
                for alias in alias_keys:
                    candidate_path = self._mail_idempotency_path(
                        clean_project, clean_sender, alias
                    )
                    candidate = read_json(candidate_path, required=False)
                    if candidate:
                        receipt = candidate
                        receipt_source_path = candidate_path
                        break
            if receipt:
                stored_identity_hash = str(
                    receipt.get("idempotency_identity_hash") or ""
                )
                identity_matches = bool(
                    replay_identity_hash
                    and stored_identity_hash == replay_identity_hash
                )
                if replay_identity_hash and not identity_matches:
                    existing = self._mail_record_unlocked(
                        clean_project,
                        clean_recipient,
                        str(receipt.get("message_ref") or ""),
                    )
                    identity_matches = bool(
                        _contains_identity(existing, replay_identity)
                        or any(
                            _contains_identity(existing, alias)
                            for alias in replay_identity_aliases
                        )
                    )
                if stored_identity_hash and not identity_matches:
                    raise DomainError(
                        "field_mail_idempotency_conflict",
                        "The mail idempotency key belongs to another message identity.",
                        status=409,
                    )
                if (
                    receipt.get("request_hash") != request_hash
                    and not identity_matches
                ):
                    raise DomainError(
                        "field_mail_idempotency_conflict",
                        "The mail idempotency key was already used for different content.",
                        status=409,
                    )
                if replay_identity_hash and (
                    stored_identity_hash != replay_identity_hash
                    or receipt_source_path != receipt_path
                ):
                    receipt = dict(receipt)
                    receipt["idempotency_identity_hash"] = replay_identity_hash
                    atomic_write_json(receipt_path, receipt)
                return {**receipt, "replayed": True}
            sender_row = read_json(self._worker_path(clean_sender), required=False)
            recipient_row = read_json(self._worker_path(clean_recipient), required=False)
            status = "pending"
            if sender_row and sender_row.get("pool_status") == "limbo":
                status = "ignored_sender_limbo"
            elif recipient_row and recipient_row.get("pool_status") == "limbo":
                status = "ignored_recipient_limbo"
            message_id = new_id("mail")
            now = utc_now()
            envelope = {
                "schema": MAIL_SCHEMA,
                "message_id": message_id,
                "project_ref": (
                    make_ref("project", clean_project) if clean_project else ""
                ),
                "work_ref": clean_work_ref,
                "sender": clean_sender,
                "sender_identity": identity,
                "recipient": clean_recipient,
                "kind": clean_kind,
                "subject": clean_subject,
                "body": clean_body,
                "payload": clean_payload,
                "attachment_count": len(clean_attachments),
                "attachments": clean_attachments,
                "correlation_id": clean_correlation_id or message_id,
                "reply_to": clean_reply_to,
                "delivery_status": status,
                "state": status,
                "idempotency_key": key,
                "created_at": now,
                "updated_at": now,
            }
            envelope["message_ref"] = reference_for_record("mail", envelope)
            envelope["content_hash"] = content_hash(envelope)
            if status == "pending":
                destination = self._mail_root(clean_project, clean_recipient) / "inbox" / f"{message_id}.json"
            else:
                destination = self._mail_ignored_root(
                    clean_project, clean_recipient
                ) / f"{message_id}.json"
            atomic_write_json(destination, envelope)
            result = {
                "message_id": message_id,
                "message_ref": envelope["message_ref"],
                "delivery_status": status,
                "content_hash": envelope["content_hash"],
                "request_hash": request_hash,
            }
            if replay_identity_hash:
                result["idempotency_identity_hash"] = replay_identity_hash
            atomic_write_json(receipt_path, result)
            if clean_project:
                self._record_event_unlocked(
                    clean_project,
                    kind="mail.sent" if status == "pending" else "mail.ignored",
                    summary=(
                        f"{clean_kind} from {clean_sender} to "
                        f"{clean_recipient} is {status}."
                    ),
                    actor=clean_sender,
                    work_ref=clean_work_ref,
                    metadata={
                        "message_ref": envelope["message_ref"],
                        "delivery_status": status,
                    },
                )
            advisory = self._body_advisory(clean_body)
            return {
                **result,
                "replayed": False,
                # What the sender needs and could not otherwise learn: whether
                # anyone is listening at the other end.
                "recipient_reachable": bool(reach.get("reachable")),
                "recipient_state": reach.get("state"),
                "recipient_pending_messages": reach.get("pending_messages"),
                **({"body_advisory": advisory} if advisory else {}),
            }

    def enqueue_remote_mail(
        self,
        project_id: str,
        *,
        sender: str,
        recipient: str,
        kind: str,
        subject: str,
        body: str,
        payload: Mapping[str, Any] | None = None,
        work_ref: str = "",
        correlation_id: str = "",
        reply_to: str = "",
        idempotency_key: str,
        attachments: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Queue mail for a worker whose inbox belongs to another host relay.

        Files named in ``attachments`` (``path``, optional ``filename``) are
        copied next to the outbox row now, so the relay uploads a stable copy
        later even if the agent moves on; the envelope itself carries no bytes."""

        clean_project = (
            component(project_id, field="project_id")
            if str(project_id or "").strip()
            else ""
        )
        clean_sender = component(sender, field="sender").lower()
        requested_recipient = component(recipient, field="recipient").lower()
        clean_kind = bounded_text(
            kind, field="kind", maximum=128, required=True
        )
        if requested_recipient in OPERATOR_RECIPIENTS:
            require_operator_mail_kind(clean_kind)
        if not clean_project and requested_recipient not in {"operator", "owner"}:
            raise DomainError(
                "field_project_context_required",
                "Worker-to-worker mail requires a shared project.",
                status=409,
            )
        key = bounded_text(
            idempotency_key, field="idempotency_key", maximum=512, required=True
        )
        attachment_sources: list[tuple[Path, str]] = []
        for item in attachments or []:
            source = Path(str((item or {}).get("path") or "")).expanduser()
            if not source.is_file():
                raise DomainError(
                    "field_attachment_missing",
                    "An attachment path does not name a readable file.",
                    details={"path": str(source)},
                )
            filename = bounded_text(
                (item or {}).get("filename") or source.name, field="attachment.filename", maximum=512, required=True
            )
            attachment_sources.append((source, Path(filename).name))
        resolution = self.resolve_mail_recipient(clean_project, requested_recipient)
        if resolution["route"] != "remote":
            raise DomainError(
                "field_mail_route_mismatch",
                "The addressed worker belongs to this host; use the local route.",
                status=409,
                details={
                    "recipient": resolution["worker_name"],
                    "required_route": "local",
                },
            )
        clean_recipient = str(resolution["worker_name"])
        if attachment_sources and clean_recipient not in {"operator", "owner"}:
            raise DomainError(
                "field_attachments_operator_only",
                "Attachments travel to the operator inbox; worker-to-worker mail carries refs.",
            )
        mail = {
            "kind": clean_kind,
            "subject": bounded_text(
                subject, field="subject", maximum=2000, required=True
            ),
            "body": bounded_text(body, field="body", maximum=MAX_REMOTE_MAIL_BYTES),
            "payload": dict(payload or {}),
            "work_ref": bounded_text(
                work_ref, field="work_ref", maximum=MAX_WORK_ITEM_REF_BYTES
            ),
            "correlation_id": bounded_text(
                correlation_id, field="correlation_id", maximum=512
            ),
            "reply_to": bounded_text(reply_to, field="reply_to", maximum=512),
        }
        if not clean_project:
            if mail["work_ref"]:
                raise DomainError(
                    "field_project_context_required",
                    "Direct mail to the operator cannot name project work.",
                    status=409,
                )
        elif mail["work_ref"]:
            self._require_work_ref_shape(str(mail["work_ref"]))
        encoded = json.dumps(
            mail, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > MAX_REMOTE_MAIL_BYTES:
            raise DomainError(
                "field_remote_mail_too_large",
                "Remote worker mail exceeds the control-plane envelope limit.",
                details={
                    "maximum_bytes": MAX_REMOTE_MAIL_BYTES,
                    "payload_bytes": len(encoded),
                },
            )
        request_hash = content_hash(
            {"route": "remote", "recipient": clean_recipient, **mail}
        )
        with exclusive_lock(self._mail_lock(clean_project, clean_sender)):
            if clean_project:
                self.read_project(clean_project)
            self.read_worker(clean_sender)
            receipt_path = self._mail_idempotency_path(
                clean_project, clean_sender, key
            )
            receipt = read_json(receipt_path, required=False)
            if receipt:
                if receipt.get("request_hash") != request_hash:
                    raise DomainError(
                        "field_mail_idempotency_conflict",
                        "The mail idempotency key was already used for different content.",
                        status=409,
                    )
                return {**receipt, "replayed": True}
            message_id = new_id("mail")
            message_created_at = utc_now()
            message_ref = reference_for_record(
                "mail",
                {
                    "message_id": message_id,
                    "created_at": message_created_at,
                    "subject": mail["subject"],
                    "kind": mail["kind"],
                },
            )
            outbox_id = new_id("outbox")
            routed_mail = {
                **mail,
                "source_message_ref": message_ref,
                "sender": clean_sender,
                "recipient": clean_recipient,
                "idempotency_key": key,
            }
            attachment_files: list[dict[str, Any]] = []
            if attachment_sources:
                folder = self.control / "outbox" / "attachments" / outbox_id
                folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                for source, filename in attachment_sources:
                    target = folder / filename
                    target.write_bytes(source.read_bytes())
                    attachment_files.append(
                        {
                            "filename": filename,
                            "path": str(target),
                            "mime": mimetypes.guess_type(filename)[0] or "application/octet-stream",
                            "size": target.stat().st_size,
                        }
                    )
                routed_mail["attachment_files"] = attachment_files
            row = {
                "schema": OUTBOX_SCHEMA,
                "outbox_id": outbox_id,
                "kind": "mail.route",
                "worker_name": clean_sender,
                "project_ref": (
                    make_ref("project", clean_project) if clean_project else ""
                ),
                "content_hash": content_hash(routed_mail),
                "payload": routed_mail,
                "state": "pending",
                "created_at": message_created_at,
            }
            atomic_write_json(
                self.control / "outbox" / "pending" / f"{outbox_id}.json", row
            )
            if (
                clean_recipient in {"operator", "owner"}
                and mail["kind"] == "reply"
                and mail["reply_to"]
            ):
                atomic_write_json(
                    self._operator_response_path(clean_sender, mail["reply_to"]),
                    {
                        "worker_name": clean_sender,
                        "message_ref": mail["reply_to"],
                        "correlation_id": mail["correlation_id"],
                        "outbox_id": outbox_id,
                        "state": "queued",
                        "created_at": row["created_at"],
                    },
                )
            result = {
                "message_id": message_id,
                "message_ref": message_ref,
                "outbox_id": outbox_id,
                "delivery_route": "remote",
                "delivery_status": "queued",
                "content_hash": row["content_hash"],
                "request_hash": request_hash,
            }
            atomic_write_json(receipt_path, result)
            if clean_project:
                self._record_event_unlocked(
                    clean_project,
                    kind="mail.queued_remote",
                    summary=(
                        f"{mail['kind']} from {clean_sender} to {clean_recipient} "
                        "was queued for its host relay."
                    ),
                    actor=clean_sender,
                    work_ref=mail["work_ref"],
                    metadata={
                        "message_ref": message_ref,
                        "recipient": clean_recipient,
                    },
                )
            advisory = self._body_advisory(str(mail.get("body") or ""))
            return {
                **result,
                "replayed": False,
                **({"body_advisory": advisory} if advisory else {}),
            }

    def renew_mail_lease(
        self,
        project_id: str,
        *,
        worker_name: str,
        message_ref: str,
        lease_id: str,
        lease_owner: str,
        lease_seconds: int = 300,
        note: str = "",
    ) -> dict[str, Any]:
        """Say "still working" and keep the claim.

        A lease lapses on a timer, not on evidence that anyone abandoned it, so
        a worker still thinking about a message loses it and the message comes
        back as new. Coordinating makes that worse rather than better, because
        coordinating means holding several messages at once while deciding
        between them. On 2026-09-12 the operator's own questions redelivered
        three times while the answer was being written, and no acknowledgement
        could reach her until the lease was refreshed by chance.

        Three limits keep this from becoming a way to sit on a message:

        Only the holder may renew, and only while the lease is still alive. A
        lapsed claim is gone; take a fresh one by receiving the message again,
        which is what the redelivery is for.

        Every renewal is counted and stamped, so a message held across many
        renewals is visible rather than quietly parked.

        And renewal is bounded in total. Past that, the message returns for
        redelivery whatever the holder says, because a worker that has needed
        an hour on one message is not "still working", it is stuck, and
        somebody else should see it.
        """

        clean_project = (
            component(project_id, field="project_id")
            if str(project_id or "").strip()
            else ""
        )
        clean_worker = component(worker_name, field="worker_name").lower()
        parsed = parse_ref(message_ref)
        if parsed.kind != "mail":
            raise DomainError("field_mail_ref_invalid", "Expected a work:mail reference.")
        name = f"{component(parsed.object_id)}.json"
        root = self._mail_root(clean_project, clean_worker)
        if clean_project and not (root / "leased" / name).exists():
            worker_root = self._mail_root("", clean_worker)
            if (worker_root / "leased" / name).exists():
                root = worker_root
        source = root / "leased" / name
        with exclusive_lock(root / ".mail.lock"):
            row = read_json(source, required=False)
            if not row:
                raise DomainError(
                    "field_mail_lease_expired",
                    "The lease is no longer held, so there is nothing to renew. "
                    "Receive the message again to take a fresh one.",
                    status=409,
                    details={"message_ref": message_ref},
                )
            lease = row.get("lease") if isinstance(row.get("lease"), Mapping) else {}
            if str(lease.get("lease_id") or "") != str(lease_id):
                raise DomainError(
                    "field_mail_lease_mismatch",
                    "Only the current lease holder may renew this claim.",
                    status=409,
                )
            if str(lease.get("owner") or "") != str(lease_owner):
                raise DomainError(
                    "field_mail_lease_owner_mismatch",
                    "Only the session holding this lease may renew it.",
                    status=403,
                )
            expires_at = str(lease.get("expires_at") or "")
            if not expires_at or parse_utc(expires_at) <= datetime.now(timezone.utc):
                raise DomainError(
                    "field_mail_lease_expired",
                    "This lease already lapsed and the message was returned for "
                    "redelivery. Receive it again to take a fresh lease.",
                    status=409,
                    details={"message_ref": message_ref},
                )
            first_held = str(lease.get("first_leased_at") or lease.get("leased_at") or utc_now())
            held_for = (
                datetime.now(timezone.utc) - parse_utc(first_held)
            ).total_seconds()
            if held_for >= MAX_MAIL_HOLD_SECONDS:
                row.update(state="pending", delivery_status="redelivered", updated_at=utc_now())
                row.pop("lease", None)
                atomic_write_json(source, row)
                os.replace(source, root / "inbox" / name)
                raise DomainError(
                    "field_mail_held_too_long",
                    "This message has been held for the maximum time and was "
                    "returned for redelivery. A worker that cannot finish with "
                    "it should refuse it so someone else can see it.",
                    status=409,
                    details={
                        "message_ref": message_ref,
                        "held_seconds": int(held_for),
                        "maximum_seconds": MAX_MAIL_HOLD_SECONDS,
                    },
                )
            renewals = int(lease.get("renewals") or 0) + 1
            lease = {
                **lease,
                "expires_at": _future(min(max(int(lease_seconds), 5), 86_400)),
                "renewed_at": utc_now(),
                "renewals": renewals,
                "first_leased_at": first_held,
            }
            if str(note or "").strip():
                lease["note"] = bounded_text(note, field="note", maximum=1000)
            row["lease"] = lease
            row["updated_at"] = utc_now()
            atomic_write_json(source, row)
            return dict(row)

    def _recover_expired_mail(self, project_id: str, worker_name: str) -> None:
        root = self._mail_root(project_id, worker_name)
        for path in sorted((root / "leased").glob("*.json")):
            row = read_json(path)
            lease = row.get("lease") if isinstance(row.get("lease"), Mapping) else {}
            expires_at = str(lease.get("expires_at") or "")
            if not expires_at or parse_utc(expires_at) <= datetime.now(timezone.utc):
                row.update(state="pending", delivery_status="redelivered", updated_at=utc_now())
                row.pop("lease", None)
                atomic_write_json(path, row)
                os.replace(path, root / "inbox" / path.name)

    def pull_mail(
        self,
        project_id: str,
        *,
        worker_name: str,
        lease_owner: str,
        limit: int = 10,
        lease_seconds: int = 300,
        byte_budget: MailPullBudget | None = None,
        measure_response: Callable[[Sequence[Mapping[str, Any]]], int] | None = None,
        measure_message: Callable[[Mapping[str, Any]], int] | None = None,
    ) -> list[dict[str, Any]]:
        """Lease one bounded mailbox batch.

        A response-wide byte budget is decided here, before a source file moves
        from ``inbox`` to ``leased``. The caller supplies the exact serialized
        response size for the prospective local batch because only that layer
        knows the response envelope. Mail that would cross the budget remains
        pending and immediately readable by the next receive.
        """

        if (byte_budget is None) != (measure_response is None):
            raise DomainError(
                "field_mail_receive_budget_invalid",
                "A bounded mail pull requires both its byte budget and response measurer.",
            )
        clean_project = (
            component(project_id, field="project_id")
            if str(project_id or "").strip()
            else ""
        )
        clean_worker = component(worker_name, field="worker_name").lower()
        worker = self.read_worker(clean_worker)
        if worker.get("pool_status") != "active":
            raise DomainError("field_worker_limbo", "A worker in limbo cannot pull mail.", status=403)
        root = self._mail_root(clean_project, clean_worker)
        (root / "inbox").mkdir(parents=True, exist_ok=True, mode=0o700)
        (root / "leased").mkdir(parents=True, exist_ok=True, mode=0o700)
        (root / "processed").mkdir(parents=True, exist_ok=True, mode=0o700)
        claimed: list[dict[str, Any]] = []
        with exclusive_lock(root / ".mail.lock"):
            self._recover_expired_mail(clean_project, clean_worker)
            sources = sorted((root / "inbox").glob("*.json"))
            take = max(0, min(int(limit), 100))
            claimed_paths: list[Path] = []
            limited_by = ""
            try:
                for source in sources[:take]:
                    row = read_json(source)
                    lease_id = new_id("lease")
                    now = utc_now()
                    row.update(
                        state="leased",
                        lease={
                            "lease_id": lease_id,
                            "lease_ref": make_ref("lease", lease_id),
                            # When this message was first claimed, kept across
                            # renewals so "held too long" measures the message
                            # rather than the latest extension.
                            "first_leased_at": now,
                            "renewals": 0,
                            "owner": bounded_text(
                                lease_owner,
                                field="lease_owner",
                                maximum=512,
                                required=True,
                            ),
                            "leased_at": now,
                            "expires_at": _future(
                                min(max(int(lease_seconds), 5), 86_400)
                            ),
                        },
                        updated_at=now,
                    )
                    handled = self.prior_handling(
                        worker_name=clean_worker,
                        message_id=str(row.get("message_id") or ""),
                    )
                    if handled.get("entries"):
                        # Redelivered after the lease lapsed. Say so loudly rather
                        # than handing the worker something that looks new, so it
                        # can settle what it already answered instead of doing the
                        # work twice.
                        row["prior_handling"] = {
                            "already_handled": True,
                            "entries": handled["entries"],
                            "guidance": (
                                "This worker already acted on this message; the "
                                "lease lapsed before settlement. Do not redo the "
                                "work. Settle it, and only act again if the "
                                "earlier action is recorded as incomplete."
                            ),
                        }
                    delivered = row
                    if byte_budget is not None and measure_response is not None:
                        prospective_bytes = int(measure_response([*claimed, row]))
                        if not byte_budget.can_claim(prospective_bytes):
                            message_bytes = (
                                int(measure_message(row))
                                if measure_message is not None
                                else prospective_bytes
                            )
                            skip = _undeliverable_reason(
                                message_bytes=message_bytes,
                                response_bytes=prospective_bytes,
                                nothing_claimed=not claimed and byte_budget.is_empty(),
                            )
                            if skip is not None:
                                # Deferring a message no receive can carry
                                # blocked every message behind it (2026-09-21,
                                # a 71 KB mail stopped a worker's inbox). It is
                                # claimed as an ordinary lease instead, and the
                                # receive carries a bounded stub of it. The
                                # original stays whole on disk.
                                code, explanation = skip
                                marker = _receive_stub_marker(
                                    row,
                                    code=code,
                                    explanation=explanation,
                                    message_bytes=message_bytes,
                                )
                                stub = _stub_message(row, marker)
                                stub_bytes = int(measure_response([*claimed, stub]))
                                if byte_budget.can_claim(stub_bytes):
                                    row["skipped_by_receive"] = marker
                                    delivered = stub
                                    prospective_bytes = stub_bytes
                                    _LOGGER.warning(
                                        "[problem-board.receive] undeliverable mail claimed as a "
                                        "stub worker=%s ref=%s sender=%s reason=%s "
                                        "message_bytes=%s maximum_message_bytes=%s",
                                        clean_worker,
                                        row.get("message_ref"),
                                        row.get("sender"),
                                        code,
                                        message_bytes,
                                        MAX_RECEIVABLE_MESSAGE_BYTES,
                                    )
                            if delivered is row:
                                limited_by = "response_byte_limit"
                                byte_budget.defer(
                                    message_ref=str(row.get("message_ref") or ""),
                                    response_bytes=prospective_bytes,
                                )
                                break
                    destination = root / "leased" / source.name
                    try:
                        os.replace(source, destination)
                    except FileNotFoundError:
                        continue
                    claimed_paths.append(destination)
                    atomic_write_json(destination, row)

                    # The lease handed to the caller must be the lease written
                    # to disk for this exact message.
                    written = read_json(destination)
                    written_lease = (
                        written.get("lease") if isinstance(written.get("lease"), Mapping) else {}
                    )
                    if (
                        str(written.get("message_id") or "") != str(row.get("message_id") or "")
                        or str(written_lease.get("lease_id") or "") != lease_id
                    ):
                        raise DomainError(
                            "field_mail_lease_inconsistent",
                            "The lease written for this message does not match the "
                            "lease being returned for it.",
                            status=500,
                            details={
                                "expected_message_id": str(row.get("message_id") or ""),
                                "written_message_id": str(written.get("message_id") or ""),
                                "expected_lease_id": lease_id,
                                "written_lease_id": str(written_lease.get("lease_id") or ""),
                            },
                        )
                    claimed.append(delivered)
                    if byte_budget is not None and measure_response is not None:
                        byte_budget.record_claim(prospective_bytes)
            except Exception:
                # pull_mail itself must never strand a partial batch. The
                # session layer performs the same rollback across mailboxes.
                for path in claimed_paths:
                    row = read_json(path, required=False)
                    if not row:
                        continue
                    row.pop("lease", None)
                    row.update(
                        state="pending",
                        delivery_status="receive_rolled_back",
                        updated_at=utc_now(),
                    )
                    atomic_write_json(path, row)
                    os.replace(path, root / "inbox" / path.name)
                raise
            if byte_budget is not None:
                remaining_count = max(
                    0,
                    len(sources) - len(claimed_paths),
                )
                if not limited_by and remaining_count and take < len(sources):
                    limited_by = "item_limit"
                byte_budget.record_mailbox(
                    remaining=remaining_count,
                    limited_by=limited_by,
                )
        return claimed

    def record_stub_notice(
        self,
        project_id: str,
        *,
        worker_name: str,
        message_ref: str,
        outcome: str,
    ) -> None:
        """Record on the leased row whether the sender of a stubbed message was told."""

        clean_project = (
            component(project_id, field="project_id") if str(project_id or "").strip() else ""
        )
        root = self._mail_root(clean_project, component(worker_name, field="worker_name").lower())
        path = root / "leased" / f"{component(parse_ref(message_ref).object_id)}.json"
        with exclusive_lock(root / ".mail.lock"):
            row = read_json(path, required=False)
            marker = row.get("skipped_by_receive") if isinstance(row, Mapping) else None
            if not isinstance(marker, Mapping):
                return
            row["skipped_by_receive"] = {
                **marker,
                "sender_notice": bounded_text(outcome, field="sender_notice", maximum=64),
            }
            atomic_write_json(path, row)

    def rollback_mail_lease(
        self,
        project_id: str,
        *,
        worker_name: str,
        message_ref: str,
        lease_id: str,
        lease_owner: str,
        reason: str = "",
    ) -> dict[str, Any]:
        """Return one unexposed receive claim to the inbox immediately."""

        parsed = parse_ref(message_ref)
        if parsed.kind != "mail":
            raise DomainError("field_mail_ref_invalid", "Expected a work:mail reference.")
        clean_project = (
            component(project_id, field="project_id")
            if str(project_id or "").strip()
            else ""
        )
        clean_worker = component(worker_name, field="worker_name").lower()
        root = self._mail_root(clean_project, clean_worker)
        source = root / "leased" / f"{component(parsed.object_id)}.json"
        with exclusive_lock(root / ".mail.lock"):
            row = read_json(source)
            lease = row.get("lease") if isinstance(row.get("lease"), Mapping) else {}
            if str(lease.get("lease_id") or "") != str(lease_id) or str(
                lease.get("owner") or ""
            ) != str(lease_owner):
                raise DomainError(
                    "field_mail_lease_mismatch",
                    "Only the current lease owner may roll back this mail.",
                    status=409,
                )
            row.pop("lease", None)
            row.update(
                state="pending",
                delivery_status="receive_rolled_back",
                receive_rollback={
                    "at": utc_now(),
                    "reason": bounded_text(reason, field="reason", maximum=1000),
                },
                updated_at=utc_now(),
            )
            atomic_write_json(source, row)
            os.replace(source, root / "inbox" / source.name)
            return dict(row)

    def record_mail_receive_failure(
        self,
        project_id: str,
        *,
        worker_name: str,
        message_ref: str,
        lease_id: str,
        lease_owner: str,
        error_code: str,
        error_message: str,
        error_details: Mapping[str, Any] | None = None,
        maximum_attempts: int = MAX_MAIL_RECEIVE_FAILURES,
    ) -> dict[str, Any]:
        """Retry one bad envelope independently, then quarantine it visibly."""

        parsed = parse_ref(message_ref)
        if parsed.kind != "mail":
            raise DomainError("field_mail_ref_invalid", "Expected a work:mail reference.")
        clean_project = (
            component(project_id, field="project_id")
            if str(project_id or "").strip()
            else ""
        )
        clean_worker = component(worker_name, field="worker_name").lower()
        root = self._mail_root(clean_project, clean_worker)
        source = root / "leased" / f"{component(parsed.object_id)}.json"
        with exclusive_lock(root / ".mail.lock"):
            row = read_json(source)
            lease = row.get("lease") if isinstance(row.get("lease"), Mapping) else {}
            if str(lease.get("lease_id") or "") != str(lease_id) or str(
                lease.get("owner") or ""
            ) != str(lease_owner):
                raise DomainError(
                    "field_mail_lease_mismatch",
                    "Only the current lease owner may record a receive failure.",
                    status=409,
                )
            previous = (
                dict(row.get("receive_failure") or {})
                if isinstance(row.get("receive_failure"), Mapping)
                else {}
            )
            attempts = int(previous.get("attempts") or 0) + 1
            now = utc_now()
            failure = {
                "attempts": attempts,
                "first_failed_at": str(previous.get("first_failed_at") or now),
                "last_failed_at": now,
                "error": {
                    "code": bounded_text(
                        error_code, field="error_code", maximum=256, required=True
                    ),
                    "message": bounded_text(
                        error_message, field="error_message", maximum=2000
                    ),
                    "details": json.loads(
                        json.dumps(
                            dict(error_details or {}),
                            ensure_ascii=True,
                            default=str,
                        )
                    ),
                },
            }
            if isinstance(previous.get("report"), Mapping):
                failure["report"] = dict(previous["report"])
            quarantined = attempts >= max(1, int(maximum_attempts))
            state = "quarantined" if quarantined else "retry_pending"
            row.pop("lease", None)
            row.update(
                state=state,
                delivery_status=state,
                receive_failure=failure,
                updated_at=now,
            )
            if quarantined:
                row["quarantined_at"] = now
                row["quarantine_reason"] = {
                    **failure["error"],
                    "attempts": attempts,
                }
            destination = root / ("quarantine" if quarantined else "inbox") / source.name
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            atomic_write_json(source, row)
            os.replace(source, destination)
            issue = {
                "message_ref": str(row.get("message_ref") or ""),
                "project_ref": str(row.get("project_ref") or ""),
                "sender": str(row.get("sender") or ""),
                "subject": str(row.get("subject") or ""),
                "state": state,
                "attempts": attempts,
                "error": dict(failure["error"]),
            }
            if isinstance(failure.get("report"), Mapping):
                issue["report"] = dict(failure["report"])
            return issue

    def report_mail_delivery_failure(
        self,
        project_id: str,
        *,
        receiver_worker_name: str,
        message: Mapping[str, Any],
        error_code: str,
        error_message: str,
        error_details: Mapping[str, Any] | None = None,
        field_name: str = "",
        field_value: Any = _UNREPORTED_DELIVERY_VALUE,
        failed_recipient: str = "",
        guidance: str = "The worker remains listening. Send a corrected message.",
    ) -> dict[str, Any]:
        """Queue one idempotent failure notice back to the original sender."""

        clean_project = (
            component(project_id, field="project_id")
            if str(project_id or "").strip()
            else ""
        )
        clean_receiver = component(
            receiver_worker_name, field="receiver_worker_name"
        ).lower()
        clean_failed_recipient = bounded_text(
            failed_recipient or clean_receiver,
            field="failed_recipient",
            maximum=512,
            required=True,
        )
        source_ref = bounded_text(
            message.get("message_ref"),
            field="message_ref",
            maximum=512,
            required=True,
        )
        original_subject = bounded_text(
            message.get("subject"),
            field="delivery_failure.original_subject",
            maximum=2000,
        )
        outbox_id = bounded_text(
            message.get("outbox_id"),
            field="delivery_failure.outbox_id",
            maximum=128,
        )
        details = json.loads(
            json.dumps(dict(error_details or {}), ensure_ascii=True, default=str)
        )
        failure_target_input: dict[str, Any] = {
            "code": error_code,
            "details": details,
        }
        if str(field_name or "").strip():
            failure_target_input["field"] = field_name
        if field_value is not _UNREPORTED_DELIVERY_VALUE:
            failure_target_input["value"] = field_value
            failure_target_input["value_source"] = "failure.value"
        target = resolve_delivery_failure_target(failure_target_input)
        bad_field = bounded_text(
            target.field,
            field="delivery_failure.field",
            maximum=512,
        )
        bad_value = bounded_text(
            target.value, field="delivery_failure.value", maximum=4000
        )
        clean_target = DeliveryFailureTarget(
            field=bad_field,
            value=bad_value,
            field_source=bounded_text(
                target.field_source,
                field="delivery_failure.field_source",
                maximum=128,
                required=True,
            ),
            value_source=bounded_text(
                target.value_source,
                field="delivery_failure.value_source",
                maximum=128,
                required=True,
            ),
        )
        failure = {
            "message_ref": source_ref,
            "recipient": clean_failed_recipient,
            "code": bounded_text(
                error_code,
                field="delivery_failure.code",
                maximum=256,
                required=True,
            ),
            **clean_target.as_payload(),
            "reason": bounded_text(
                error_message, field="delivery_failure.reason", maximum=2000
            ),
        }
        if original_subject:
            failure["original_subject"] = original_subject
        if outbox_id:
            failure["outbox_id"] = outbox_id
        field_line, value_line = delivery_failure_target_lines(clean_target)
        subject = "Problem Board did not deliver a message"
        body_lines = [
            f"Problem Board did not deliver {source_ref} to {clean_failed_recipient}.",
            f"Disposition: {failure['code']}",
        ]
        if original_subject:
            body_lines.append(f"Original subject: {original_subject}")
        if outbox_id:
            body_lines.append(f"Outbox ID: {outbox_id}")
        body_lines.extend(
            [
                field_line,
                value_line,
                f"Reason: {failure['reason']}",
                bounded_text(
                    guidance,
                    field="delivery_failure.guidance",
                    maximum=2000,
                ),
            ]
        )
        body = "\n".join(body_lines)
        sender_identity = (
            dict(message.get("sender_identity") or {})
            if isinstance(message.get("sender_identity"), Mapping)
            else {}
        )
        sender_kind = str(sender_identity.get("kind") or "").strip().lower()
        sender_worker = str(
            sender_identity.get("worker_name") or message.get("sender") or ""
        ).strip().lower()
        notice_recipient = sender_worker if sender_kind == "worker" else "operator"
        key = f"delivery-failed:{content_hash(failure)}"
        common = {
            "sender": clean_receiver,
            "kind": "delivery_failed",
            "subject": subject,
            "body": body,
            "payload": {"delivery_failure": failure},
            # Never copy the rejected work_ref onto its own failure notice.
            "work_ref": "",
            "correlation_id": str(message.get("correlation_id") or ""),
            "reply_to": source_ref,
            "idempotency_key": key,
        }
        if sender_kind == "worker" and sender_worker:
            try:
                resolution = self.resolve_mail_recipient(
                    clean_project, sender_worker
                )
                if resolution["route"] == "local":
                    local_common = common
                    if not clean_project:
                        local_common = {
                            **common,
                            "sender": "control-plane",
                            "sender_identity": {
                                "kind": "system",
                                "label": "Problem Board",
                                "worker_name": "",
                            },
                        }
                    result = self.send_mail(
                        clean_project,
                        recipient=sender_worker,
                        **local_common,
                    )
                else:
                    result = self.enqueue_remote_mail(
                        clean_project,
                        recipient=sender_worker,
                        **common,
                    )
                route = str(resolution["route"])
            except DomainError as exc:
                if exc.code not in {
                    "field_worker_address_invalid",
                    "field_worker_alias_not_addressable",
                    "field_worker_not_found",
                    "field_worker_retired",
                }:
                    raise
                result = self.enqueue_remote_mail(
                    clean_project,
                    recipient="operator",
                    **common,
                )
                route = "remote"
                notice_recipient = "operator"
        else:
            result = self.enqueue_remote_mail(
                clean_project,
                recipient="operator",
                **common,
            )
            route = "remote"
        return {
            "delivery_route": route,
            "delivery_status": str(result.get("delivery_status") or ""),
            "message_ref": str(result.get("message_ref") or ""),
            "recipient": notice_recipient,
            "replayed": bool(result.get("replayed")),
        }

    def record_mail_receive_failure_report(
        self,
        project_id: str,
        *,
        worker_name: str,
        message_ref: str,
        report: Mapping[str, Any],
        report_field: str = "report",
    ) -> None:
        """Attach report evidence to the retrying or quarantined envelope."""

        parsed = parse_ref(message_ref)
        if parsed.kind != "mail":
            raise DomainError("field_mail_ref_invalid", "Expected a work:mail reference.")
        clean_project = (
            component(project_id, field="project_id")
            if str(project_id or "").strip()
            else ""
        )
        clean_worker = component(worker_name, field="worker_name").lower()
        root = self._mail_root(clean_project, clean_worker)
        name = f"{component(parsed.object_id)}.json"
        with exclusive_lock(root / ".mail.lock"):
            source = next(
                (
                    root / state / name
                    for state in ("inbox", "quarantine")
                    if (root / state / name).is_file()
                ),
                None,
            )
            if source is None:
                return
            row = read_json(source)
            failure = (
                dict(row.get("receive_failure") or {})
                if isinstance(row.get("receive_failure"), Mapping)
                else {}
            )
            if report_field not in {"report", "quarantine_report"}:
                raise DomainError("field_mail_report_invalid", "Unknown mail failure report kind.")
            failure[report_field] = json.loads(
                json.dumps(dict(report), ensure_ascii=True, default=str)
            )
            row["receive_failure"] = failure
            row["updated_at"] = utc_now()
            atomic_write_json(source, row)

    def list_worker_mail_quarantine(self, worker_name: str) -> list[dict[str, Any]]:
        """Return bounded diagnostics for mail removed from active delivery."""

        clean_worker = component(worker_name, field="worker_name").lower()
        roots = [self._mail_root("", clean_worker)]
        projects_root = self.control / "projects"
        if projects_root.is_dir():
            roots.extend(
                self._mail_root(path.name, clean_worker)
                for path in sorted(projects_root.iterdir())
                if path.is_dir()
            )
        rows: list[dict[str, Any]] = []
        for root in roots:
            for path in sorted((root / "quarantine").glob("*.json")):
                row = read_json(path)
                rows.append(
                    {
                        "message_ref": str(row.get("message_ref") or ""),
                        "project_ref": str(row.get("project_ref") or ""),
                        "sender": str(row.get("sender") or ""),
                        "sender_identity": dict(row.get("sender_identity") or {}),
                        "subject": str(row.get("subject") or ""),
                        "quarantined_at": str(row.get("quarantined_at") or ""),
                        "quarantine_reason": dict(row.get("quarantine_reason") or {}),
                        "receive_failure": dict(row.get("receive_failure") or {}),
                    }
                )
        return sorted(rows, key=lambda row: row["quarantined_at"], reverse=True)

    def inspect_mail_delivery(
        self,
        project_id: str,
        *,
        worker_name: str,
        message_ref: str,
    ) -> dict[str, Any]:
        """Read one mailbox record without leasing or changing its state."""

        parsed = parse_ref(message_ref)
        if parsed.kind != "mail":
            raise DomainError("field_mail_ref_invalid", "Expected a work:mail reference.")
        clean_project = (
            component(project_id, field="project_id")
            if str(project_id or "").strip()
            else ""
        )
        clean_worker = component(worker_name, field="worker_name").lower()
        root = self._mail_root(clean_project, clean_worker)
        name = f"{component(parsed.object_id)}.json"
        with exclusive_lock(root / ".mail.lock"):
            for state in ("inbox", "leased", "processed", "quarantine"):
                path = root / state / name
                if path.is_file():
                    return {"mailbox_state": state, **read_json(path)}
            ignored = self._mail_ignored_root(clean_project, clean_worker) / name
            if ignored.is_file():
                return {"mailbox_state": "ignored", **read_json(ignored)}
        raise DomainError(
            "field_mail_not_found",
            "The addressed mailbox does not contain this message.",
            status=404,
            details={"message_ref": message_ref, "worker_name": clean_worker},
        )

    def _handled_path(self, worker_name: str, message_id: str) -> Path:
        return (
            self.control
            / "workers"
            / component(worker_name, field="worker_name").lower()
            / "handled"
            / f"{component(message_id)}.json"
        )

    def record_handling(
        self,
        *,
        worker_name: str,
        message_ref: str,
        action: str,
        detail: str = "",
    ) -> None:
        """Remember that this worker already dealt with this message.

        A lease can lapse while a worker is still working, and the message is
        then returned for redelivery. Without a memory of what was already
        done, the worker receives it as new and redoes the whole thing:
        re-reads it, re-reasons about it, and writes the same answer again.
        For a model-backed worker that is the most expensive failure mode in
        the system, because the cost is paid in thinking rather than in
        retries, and nothing looks wrong while it happens.

        This is kept locally on purpose. It needs no network, works while the
        worker is cut off from the control plane, and survives the lease it
        outlives.
        """
        try:
            parsed = parse_ref(message_ref)
        except DomainError:
            return
        if parsed.kind != "mail":
            return
        path = self._handled_path(worker_name, parsed.object_id)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        previous = read_json(path, required=False) or {}
        entries = list(previous.get("entries") or [])
        entries.append(
            {
                "action": str(action or ""),
                "detail": bounded_text(detail, field="detail", maximum=2000),
                "at": utc_now(),
            }
        )
        atomic_write_json(
            path,
            {
                "schema": FIELD_SCHEMA,
                "message_ref": message_ref,
                "worker_name": component(worker_name, field="worker_name").lower(),
                "entries": entries[-20:],
                "updated_at": utc_now(),
            },
        )

    def prior_handling(self, *, worker_name: str, message_id: str) -> dict[str, Any]:
        """What this worker already did with this message, or an empty record."""
        return read_json(self._handled_path(worker_name, message_id), required=False) or {}

    def leased_correlation(self, project_id: str, *, worker_name: str, message_ref: str) -> str:
        """The correlation id of a message this worker currently holds, or "".

        Answering a person's message must be correlated to the control that
        carried it, not to the message itself, because settlement looks for
        the response against that control. The value is already on the leased
        message, so an agent should never have to copy it by hand: doing so is
        the difference between a reply a person can see and a reply the board
        will accept, and getting it wrong produces a correct visible answer
        that is still refused.
        """
        try:
            parsed = parse_ref(message_ref)
        except DomainError:
            return ""
        if parsed.kind != "mail":
            return ""
        clean_project = (
            component(project_id, field="project_id")
            if str(project_id or "").strip()
            else ""
        )
        clean_worker = component(worker_name, field="worker_name").lower()
        name = f"{component(parsed.object_id)}.json"

        # Look where the message actually is, not where this call implies.
        # A reply is often sent without a project even though the message it
        # answers arrived in a project mailbox, and the first version of this
        # helper searched only the mailbox named by the caller. It therefore
        # failed on exactly the case it exists for: answering a person's
        # message that arrived with a project, from a reply that carries none.
        roots = [self._mail_root(clean_project, clean_worker)]
        if clean_project:
            roots.append(self._mail_root("", clean_worker))
        else:
            projects_root = self.control / "projects"
            if projects_root.is_dir():
                for entry in sorted(projects_root.iterdir()):
                    if entry.is_dir():
                        roots.append(self._mail_root(entry.name, clean_worker))

        for root in roots:
            row = read_json(root / "leased" / name, required=False)
            if row:
                return str(row.get("correlation_id") or "")
        return ""

    def settle_mail(
        self,
        project_id: str,
        *,
        worker_name: str,
        message_ref: str,
        lease_id: str,
        lease_owner: str,
        outcome: str,
        summary: str = "",
    ) -> dict[str, Any]:
        if outcome not in {"acknowledged", "refused"}:
            raise DomainError("field_mail_outcome_invalid", "Mail outcome must be acknowledged or refused.")
        parsed = parse_ref(message_ref)
        if parsed.kind != "mail":
            raise DomainError("field_mail_ref_invalid", "Expected a work:mail reference.")
        clean_project = (
            component(project_id, field="project_id")
            if str(project_id or "").strip()
            else ""
        )
        clean_worker = component(worker_name, field="worker_name").lower()
        if self.read_worker(clean_worker).get("pool_status") != "active":
            raise DomainError(
                "field_worker_limbo",
                "A worker in limbo cannot settle mail.",
                status=403,
            )
        # A message is settled where it actually lives, not where the caller's
        # project argument says to look. Control-plane mail, which is how the
        # operator reaches an agent, is delivered with no project and lands in
        # the worker's own mailbox; a worker settling it names its project,
        # which pointed at the project mailbox instead. The lease was therefore
        # never found, the settle failed, and the message redelivered forever:
        # the operator's messages to this session came back three times before
        # anyone noticed they could not be acknowledged at all.
        name = f"{component(parsed.object_id)}.json"
        root = self._mail_root(clean_project, clean_worker)
        if clean_project and not (root / "leased" / name).exists():
            worker_root = self._mail_root("", clean_worker)
            if (worker_root / "leased" / name).exists():
                root = worker_root
        source = root / "leased" / name
        with exclusive_lock(root / ".mail.lock"):
            row = read_json(source, required=False)
            if not row:
                # The leased file is gone, which almost always means the lease
                # expired and the message was recovered back to the inbox for
                # redelivery. Saying "record does not exist" and printing a
                # path sends the reader hunting for a missing file, when the
                # message is fine and their claim on it is what lapsed. A
                # worker that spends longer on a reply than the lease allows
                # hits this repeatedly, and the honest answer tells it to
                # receive the message again rather than to look for a file.
                returned = read_json(root / "inbox" / source.name, required=False)
                if returned:
                    raise DomainError(
                        "field_mail_lease_expired",
                        "The mail lease expired and this message was returned "
                        "for redelivery. Receive it again to take a fresh "
                        "lease, then settle with that lease id.",
                        status=409,
                        details={
                            "message_ref": message_ref,
                            "lease_id": str(lease_id),
                            "state": str(returned.get("delivery_status") or ""),
                        },
                    )
                settled = read_json(root / "processed" / source.name, required=False)
                if settled:
                    raise DomainError(
                        "field_mail_already_settled",
                        "This message was already settled.",
                        status=409,
                        details={
                            "message_ref": message_ref,
                            "outcome": str(settled.get("state") or ""),
                        },
                    )
                # Genuinely unknown here: fall through to the original error.
                row = read_json(source)
            lease = row.get("lease") if isinstance(row.get("lease"), Mapping) else {}
            if str(lease.get("lease_id") or "") != str(lease_id) or str(lease.get("owner") or "") != str(lease_owner):
                raise DomainError("field_mail_lease_mismatch", "Only the current lease owner may settle this mail.", status=409)
            if parse_utc(str(lease.get("expires_at") or "")) <= datetime.now(timezone.utc):
                raise DomainError("field_mail_lease_expired", "The mail lease expired before settlement.", status=409)
            sender_identity = (
                dict(row.get("sender_identity") or {})
                if isinstance(row.get("sender_identity"), Mapping)
                else {}
            )
            if (
                str(sender_identity.get("kind") or "") == "user"
                and str(row.get("kind") or "") in {"request", "reply"}
            ):
                response = read_json(
                    self._operator_response_path(
                        clean_worker, str(row.get("message_ref") or "")
                    ),
                    required=False,
                )
                expected_correlation = str(row.get("correlation_id") or "")
                if not response or str(response.get("correlation_id") or "") != expected_correlation:
                    raise DomainError(
                        "field_operator_response_required",
                        "Send the operator a visible correlated reply before settling this message.",
                        status=409,
                        details={
                            "recipient": "operator",
                            "kind": "reply",
                            "correlation_id": expected_correlation,
                            "reply_to": str(row.get("message_ref") or ""),
                        },
                    )
                row["operator_response_outbox_id"] = str(
                    response.get("outbox_id") or ""
                )
            row.update(
                state=outcome,
                delivery_status=outcome,
                settlement_summary=bounded_text(summary, field="summary", maximum=8000),
                settled_at=utc_now(),
                updated_at=utc_now(),
            )
            destination = root / "processed" / source.name
            atomic_write_json(source, row)
            os.replace(source, destination)
        self.record_handling(
            worker_name=clean_worker,
            message_ref=message_ref,
            action=f"settled:{outcome}",
            detail=summary,
        )
        payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
        command_ref = str(payload.get("command_ref") or "")
        if command_ref:
            settlement = self.enqueue_control_settlement(
                clean_project,
                worker_name=clean_worker,
                command_ref=command_ref,
                message_ref=str(row.get("message_ref") or ""),
                session_id=lease_owner,
                outcome=outcome,
                summary=str(row.get("settlement_summary") or ""),
            )
            row["remote_settlement_outbox_id"] = settlement["outbox_id"]
        return row

    def discard_control_messages(
        self,
        *,
        worker_name: str,
        discard_ref: str,
        sender_label: str,
        reason: str,
        targets: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Discard pending local controls and notify the model about crossed receives."""

        clean_worker = component(worker_name, field="worker_name").lower()
        self.read_worker(clean_worker)
        clean_discard_ref = bounded_text(
            discard_ref, field="discard_ref", maximum=256, required=True
        )
        clean_sender = bounded_text(
            sender_label or "operator", field="sender_label", maximum=512
        )
        clean_reason = bounded_text(reason, field="reason", maximum=2000)
        rows = list(targets)
        if not rows or len(rows) > 100:
            raise DomainError(
                "field_discard_targets_invalid",
                "A discard request must name between 1 and 100 controls.",
            )

        outcomes: list[dict[str, Any]] = []
        received: list[dict[str, str]] = []
        for target in rows:
            command_ref = bounded_text(
                target.get("command_ref"),
                field="command_ref",
                maximum=256,
                required=True,
            )
            if parse_ref(command_ref).kind != "control":
                raise DomainError(
                    "field_control_ref_invalid",
                    "A discard target must be a work:control reference.",
                )
            project_ref = bounded_text(
                target.get("project_ref"), field="project_ref", maximum=256
            )
            parsed_project = parse_ref(project_ref) if project_ref else None
            if parsed_project is not None and parsed_project.kind != "project":
                raise DomainError(
                    "field_project_ref_invalid",
                    "A discard target project must be a work:project reference.",
                )
            project_id = parsed_project.object_id if parsed_project is not None else ""
            message_ref = bounded_text(
                target.get("message_ref"), field="message_ref", maximum=256
            )
            message_id = ""
            if message_ref:
                parsed_message = parse_ref(message_ref)
                if parsed_message.kind != "mail":
                    raise DomainError(
                        "field_mail_ref_invalid",
                        "A discard target message must be a work:mail reference.",
                    )
                message_id = parsed_message.object_id
            root = self._mail_root(project_id, clean_worker)
            found: tuple[str, Path, dict[str, Any]] | None = None
            with exclusive_lock(self._mail_lock(project_id, clean_worker)):
                self._recover_expired_mail(project_id, clean_worker)
                for state in ("inbox", "leased", "processed", "ignored"):
                    folder = (
                        self._mail_ignored_root(project_id, clean_worker)
                        if state == "ignored"
                        else root / state
                    )
                    candidates = (
                        [folder / f"{component(message_id)}.json"]
                        if message_id
                        else sorted(folder.glob("*.json"))
                    )
                    for path in candidates:
                        row = read_json(path, required=False)
                        if not row or str(row.get("recipient") or "").lower() != clean_worker:
                            continue
                        payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
                        if str(payload.get("command_ref") or "") != command_ref:
                            continue
                        found = (state, path, row)
                        break
                    if found is not None:
                        break

                if found is None:
                    if bool(target.get("received_hint")):
                        outcome = "already_received"
                        received.append(
                            {
                                "command_ref": command_ref,
                                "message_ref": message_ref,
                                "kind": bounded_text(
                                    target.get("kind"), field="kind", maximum=64
                                ),
                                "subject": bounded_text(
                                    target.get("subject") or command_ref,
                                    field="subject",
                                    maximum=2000,
                                ),
                                "project_ref": project_ref,
                            }
                        )
                    else:
                        outcome = "not_present"
                elif found[0] == "inbox":
                    _, source, row = found
                    now = utc_now()
                    row.update(
                        state="discarded_by_sender",
                        delivery_status="discarded_by_sender",
                        discard_request_ref=clean_discard_ref,
                        discard_reason=clean_reason,
                        discarded_at=now,
                        updated_at=now,
                    )
                    destination = self._mail_ignored_root(
                        project_id, clean_worker
                    ) / source.name
                    atomic_write_json(source, row)
                    os.replace(source, destination)
                    outcome = "discarded_before_receipt"
                elif found[0] == "ignored":
                    outcome = "already_discarded"
                else:
                    outcome = "already_received"
                    received.append(
                        {
                            "command_ref": command_ref,
                            "message_ref": str(found[2].get("message_ref") or message_ref),
                            "kind": bounded_text(
                                target.get("kind") or found[2].get("kind"),
                                field="kind",
                                maximum=64,
                            ),
                            "subject": bounded_text(
                                target.get("subject") or found[2].get("subject") or command_ref,
                                field="subject",
                                maximum=2000,
                            ),
                            "project_ref": project_ref,
                        }
                    )
            outcomes.append(
                {
                    "command_ref": command_ref,
                    "message_ref": message_ref,
                    "project_ref": project_ref,
                    "outcome": outcome,
                }
            )

        notice: dict[str, Any] | None = None
        if received:
            target_lines = "\n".join(
                f"- {item['kind'] or 'message'}: {item['subject']} ({item['command_ref']})"
                for item in received
            )
            reason_line = f"\nReason from {clean_sender}: {clean_reason}" if clean_reason else ""
            body = (
                f"{clean_sender} discarded {len(received)} earlier message"
                f"{'s' if len(received) != 1 else ''} after they reached this session. "
                "Do not start them. If work already began, stop at a safe boundary when "
                "possible. If effects are already complete, report that fact; history is "
                f"not erased.{reason_line}\n{target_lines}"
            )
            notice = self.send_mail(
                "",
                sender="control-plane",
                recipient=clean_worker,
                kind="discard.notice",
                subject=(
                    f"{len(received)} earlier message"
                    f"{'s were' if len(received) != 1 else ' was'} discarded"
                ),
                body=body,
                payload={
                    "discard_request_ref": clean_discard_ref,
                    "intent": "sender_no_longer_wants_these_messages_acted_on",
                    "reason": clean_reason,
                    "requested_handling": {
                        "not_started": "do_not_start",
                        "in_progress": "stop_at_a_safe_boundary_when_possible",
                        "already_completed": "report_completed_effects",
                    },
                    "targets": received,
                },
                correlation_id=clean_discard_ref,
                idempotency_key=f"discard-notice:{clean_discard_ref}",
                sender_identity={"kind": "user", "label": clean_sender},
            )
        return {
            "discard_ref": clean_discard_ref,
            "worker_name": clean_worker,
            "outcomes": outcomes,
            "soft_notice": notice,
        }

    def materialize_control(self, control: Mapping[str, Any]) -> dict[str, Any]:
        """Durably turn one leased KDCube control into local addressed mail.

        The remote command is acknowledged only after this method returns. The
        command ref is the idempotency key, so a relay crash between local write
        and remote acknowledgement cannot create a second local message.
        """
        command_ref = bounded_text(
            control.get("ref") or control.get("command_ref"),
            field="command_ref",
            maximum=256,
            required=True,
        )
        parsed_command = parse_ref(command_ref)
        if parsed_command.kind != "control":
            raise DomainError("field_control_ref_invalid", "Expected a work:control reference.")
        project_ref = bounded_text(
            control.get("project_ref"), field="project_ref", maximum=256
        )
        parsed_project = parse_ref(project_ref) if project_ref else None
        if parsed_project is not None and parsed_project.kind != "project":
            raise DomainError(
                "field_project_ref_invalid", "Expected a work:project reference."
            )
        recipient = bounded_text(
            control.get("recipient"), field="recipient", maximum=128, required=True
        ).lower()
        raw_payload = control.get("payload")
        if not isinstance(raw_payload, Mapping):
            raise DomainError(
                "field_control_payload_invalid",
                "A leased control payload must be a JSON object.",
                details={
                    "field": "payload",
                    "value_type": type(raw_payload).__name__,
                },
            )
        payload = dict(raw_payload)
        expected_hash = bounded_text(
            control.get("payload_hash"), field="payload_hash", maximum=128, required=True
        )
        actual_hash = content_hash(payload)
        if actual_hash != expected_hash:
            raise DomainError(
                "field_control_hash_invalid",
                "The leased control payload does not match its declared hash.",
                status=409,
                details={"command_ref": command_ref},
            )
        control_kind = bounded_text(
            control.get("kind"), field="kind", maximum=128, required=True
        )
        if not project_ref:
            if control_kind not in DIRECT_CONTROL_KINDS:
                raise DomainError(
                    "field_project_context_required",
                    "This control requires a project mailbox.",
                    status=409,
                    details={"action": control_kind},
                )
            if str(control.get("work_ref") or "").strip():
                raise DomainError(
                    "field_project_context_required",
                    "A direct worker control cannot name project work.",
                    status=409,
                )
        project_path = (
            self._project_path(parsed_project.object_id)
            if parsed_project is not None
            else None
        )
        if project_path is not None and not project_path.exists():
            project = payload.get("project") if isinstance(payload.get("project"), Mapping) else {}
            if control_kind not in {"materialize", "assign"} or not project:
                raise DomainError(
                    "field_project_not_materialized",
                    "The local project must be materialized before it can receive controls.",
                    status=409,
                    details={"project_ref": project_ref},
                )
            supplied_id = str(project.get("project_id") or parsed_project.object_id)
            if supplied_id != parsed_project.object_id:
                raise DomainError(
                    "field_project_ref_mismatch",
                    "The materialized project id does not match the control project ref.",
                    status=409,
                )
            self.create_project(
                project_id=parsed_project.object_id,
                title=project.get("title"),
                goal=project.get("goal"),
                owner=project.get("owner") or "control-plane",
            )
        assignment_receipt: dict[str, Any] | None = None
        if control_kind == "assign":
            if parsed_project is None:
                raise DomainError(
                    "field_project_context_required",
                    "An assignment requires a project mailbox.",
                    status=409,
                )
            assignment = (
                payload.get("assignment")
                if isinstance(payload.get("assignment"), Mapping)
                else None
            )
            if assignment is None:
                raise DomainError(
                    "field_assignment_required",
                    "An assign control requires an assignment envelope.",
                )
            if str(assignment.get("worker_name") or "").lower() != recipient:
                raise DomainError(
                    "field_assignment_recipient_mismatch",
                    "The assignment owner does not match the addressed worker.",
                    status=409,
                )
            assignment_receipt = self.materialize_assignment(
                parsed_project.object_id, assignment
            )
        routed_mail = payload.get("mail") if isinstance(payload.get("mail"), Mapping) else None
        sender = "control-plane"
        kind = control_kind
        subject = bounded_text(
            control.get("subject"), field="subject", maximum=2000, required=True
        )
        body = payload.get("body") or payload.get("instructions") or subject
        message_payload: dict[str, Any] = {
            "command_ref": command_ref,
            "command": payload,
            "payload_hash": expected_hash,
        }
        correlation_id = command_ref
        reply_to = ""
        sender_identity = (
            dict(control.get("sender_identity"))
            if isinstance(control.get("sender_identity"), Mapping)
            else None
        )
        # Files the operator sent ride the control as descriptors, hashed with
        # the payload. The relay fetched them into this field and reports the
        # local paths beside the payload, so the hash above still holds.
        local_paths = (
            dict(control.get("attachment_local_paths"))
            if isinstance(control.get("attachment_local_paths"), Mapping)
            else {}
        )
        local_files = (
            dict(control.get("attachment_local_files"))
            if isinstance(control.get("attachment_local_files"), Mapping)
            else {}
        )
        raw_attachments = payload.get("attachments") or []
        if not isinstance(raw_attachments, list):
            raise DomainError(
                "field_control_attachments_invalid",
                "Control attachments must be a JSON array.",
                details={
                    "field": "payload.attachments",
                    "value_type": type(raw_attachments).__name__,
                },
            )
        attachment_root = self._mail_root(
            parsed_project.object_id if parsed_project is not None else "",
            recipient,
        ) / "attachments"
        attachments = materialized_attachment_manifest(
            raw_attachments,
            local_paths=local_paths,
            local_files=local_files,
            attachment_root=attachment_root,
        )
        if control_kind == "reply":
            # The operator's answer belongs to the thread the worker opened:
            # the envelope carries the thread at the top level, not only
            # inside the command, so a reply reads like any other mail.
            correlation_id = (
                str(payload.get("correlation_id") or "")
                or str(payload.get("reply_to") or "")
                or command_ref
            )
            reply_to = str(payload.get("reply_to") or "")
        if control_kind == "mail" and routed_mail is None:
            raise DomainError(
                "field_routed_mail_invalid",
                "A routed mail control requires a mail object.",
                details={"field": "payload.mail"},
            )
        if control_kind == "mail" and routed_mail is not None:
            sender = bounded_text(
                control.get("sender"), field="sender", maximum=128, required=True
            ).lower()
            kind = bounded_text(
                routed_mail.get("kind"), field="mail.kind", maximum=128, required=True
            )
            subject = bounded_text(
                routed_mail.get("subject") or subject,
                field="mail.subject",
                maximum=2000,
                required=True,
            )
            body = bounded_text(
                routed_mail.get("body"),
                field="mail.body",
                maximum=MAX_REMOTE_MAIL_BYTES,
            )
            message_payload = {
                "command_ref": command_ref,
                "source_message_ref": str(routed_mail.get("source_message_ref") or ""),
                "payload": dict(routed_mail.get("payload") or {}),
                "payload_hash": expected_hash,
                "work_ref": str(routed_mail.get("work_ref") or ""),
                "identity_ref": str(routed_mail.get("identity_ref") or ""),
            }
            correlation_id = str(routed_mail.get("correlation_id") or command_ref)
            reply_to = str(routed_mail.get("reply_to") or "")
        if attachments:
            message_payload["attachments"] = attachments
        delivered_work_ref = str(
            payload.get("versioned_work_ref")
            or control.get("versioned_work_ref")
            or payload.get("work_ref")
            or control.get("work_ref")
            or ""
        )
        delivered_identity_ref = str(
            payload.get("identity_ref") or control.get("identity_ref") or ""
        )
        if control_kind == "assign" and assignment is not None:
            delivered_work_ref = str(
                assignment.get("versioned_work_ref")
                or assignment.get("work_version_ref")
                or assignment.get("work_ref")
                or delivered_work_ref
            )
            delivered_identity_ref = str(
                assignment.get("identity_ref") or delivered_identity_ref
            )
        elif control_kind == "mail" and routed_mail is not None:
            delivered_work_ref = str(
                routed_mail.get("versioned_work_ref")
                or routed_mail.get("work_version_ref")
                or routed_mail.get("work_ref")
                or delivered_work_ref
            )
            delivered_identity_ref = str(
                routed_mail.get("identity_ref") or delivered_identity_ref
            )
        if delivered_work_ref:
            message_payload["work_ref"] = delivered_work_ref
            message_payload["identity_ref"] = (
                delivered_identity_ref or plan_node_identity_ref(delivered_work_ref)
            )
        if assignment_receipt is not None:
            return self.send_assignment_notice(
                parsed_project.object_id,
                assignment=assignment_receipt,
                recipient=recipient,
                sender_identity=sender_identity,
            )
        return self.send_mail(
            parsed_project.object_id if parsed_project is not None else "",
            sender=sender,
            recipient=recipient,
            kind=kind,
            subject=subject,
            body=bounded_text(body, field="body", maximum=512_000),
            payload=message_payload,
            work_ref=delivered_work_ref,
            correlation_id=correlation_id,
            reply_to=reply_to,
            idempotency_key=f"control:{command_ref}",
            sender_identity=sender_identity,
        )

    def sync_project_team(self, project_id: str, team: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Store the project's linked workers as the relay last heard them.

        The packet then answers "who is on this project" locally, so a worker
        can write to a teammate without a control-plane round trip."""
        clean_id = component(project_id, field="project_id")
        path = self._project_dir(clean_id) / "team.json"
        with exclusive_lock(self._project_lock(clean_id)):
            self.read_project(clean_id)
            rows = []
            for member in team:
                if not isinstance(member, Mapping) or not str(member.get("worker_name") or "").strip():
                    continue
                rows.append(
                    {
                        "worker_name": str(member.get("worker_name") or "").lower(),
                        "worker_alias": str(member.get("worker_alias") or ""),
                        "role": str(member.get("role") or "worker"),
                        "runtime_kind": str(member.get("runtime_kind") or ""),
                        "capabilities": [str(item) for item in (member.get("capabilities") or [])],
                        "host_id": str(member.get("host_id") or ""),
                        "host_label": str(member.get("host_label") or ""),
                        "host_kind": str(member.get("host_kind") or ""),
                        "pool_status": str(member.get("pool_status") or ""),
                        "presence": str(member.get("presence") or ""),
                    }
                )
            record = {"schema": FIELD_SCHEMA, "project_id": clean_id, "members": rows, "updated_at": utc_now()}
            atomic_write_json(path, record)
            return record

    def read_project_team(self, project_id: str) -> list[dict[str, Any]]:
        path = self._project_dir(component(project_id, field="project_id")) / "team.json"
        record = read_json(path, required=False)
        return list((record or {}).get("members") or [])

    def sync_project_mail_recipients(
        self, project_id: str, recipients: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        """Store the control plane's stable-address directory for one project."""

        clean_id = component(project_id, field="project_id")
        path = self._project_dir(clean_id) / "mail-recipients.json"
        with exclusive_lock(self._project_lock(clean_id)):
            self.read_project(clean_id)
            rows: dict[str, dict[str, Any]] = {}
            for raw in recipients:
                if not isinstance(raw, Mapping):
                    continue
                worker_name = str(raw.get("worker_name") or "").strip().lower()
                if not worker_name:
                    continue
                rows[worker_name] = {
                    "worker_name": worker_name,
                    "worker_alias": str(raw.get("worker_alias") or ""),
                    "pool_status": str(raw.get("pool_status") or "active"),
                    "retired_at": str(raw.get("retired_at") or ""),
                    "retirement_reason": str(raw.get("retirement_reason") or ""),
                }
            record = {
                "schema": "problem-board.mail-recipient-directory.v1",
                "project_id": clean_id,
                "recipients": [rows[name] for name in sorted(rows)],
                "updated_at": utc_now(),
            }
            atomic_write_json(path, record)
            return record

    def read_project_mail_recipients(self, project_id: str) -> dict[str, Any]:
        clean_id = component(project_id, field="project_id")
        record = read_json(
            self._project_dir(clean_id) / "mail-recipients.json",
            required=False,
        )
        if record:
            return record
        return {
            "schema": "problem-board.mail-recipient-directory.v1",
            "project_id": clean_id,
            "recipients": self.read_project_team(clean_id),
            "updated_at": "",
        }

    def reconcile_project_mailboxes(
        self,
        project_id: str,
        *,
        reporter_worker_name: str,
    ) -> dict[str, Any]:
        """Archive mailboxes with no reader and notify each original sender."""

        started_at = utc_now()
        clean_project = component(project_id, field="project_id")
        reporter_record = self.read_worker(reporter_worker_name)
        reporter = str(reporter_record.get("worker_name") or "")
        recover_unpublished_receipts(
            self,
            clean_project,
            worker_name=reporter,
        )
        directory_record = self.read_project_mail_recipients(clean_project)
        recipients = {
            str(row.get("worker_name") or "").strip().lower(): dict(row)
            for row in directory_record.get("recipients") or []
            if isinstance(row, Mapping)
            and str(row.get("worker_name") or "").strip()
        }
        project_ref = make_ref("project", clean_project)
        for row in json_records(self.control / "workers"):
            worker_name = str(row.get("worker_name") or "").strip().lower()
            if worker_name:
                attends = project_ref in {
                    str(value)
                    for value in row.get("attended_project_refs") or []
                }
                attendance_observed = bool(
                    str(row.get("attendances_observed_at") or "").strip()
                )
                if (
                    attendance_observed
                    and not attends
                    and str(row.get("pool_status") or "active").lower()
                    != "retired"
                ):
                    recipients[worker_name] = {
                        **row,
                        "pool_status": "not_linked",
                    }
                else:
                    recipients[worker_name] = row
        aliases: dict[str, list[str]] = {}
        for worker_name, row in recipients.items():
            alias = str(row.get("worker_alias") or "").strip().casefold()
            if alias:
                aliases.setdefault(alias, []).append(worker_name)

        mail_root = self._project_dir(clean_project) / "mail"
        archive_root = mail_root / "undeliverable"
        moved: list[tuple[Path, dict[str, Any]]] = []
        examined_mailboxes: list[dict[str, Any]] = []
        undeliverable_records_examined = 0
        archived_mailboxes: list[dict[str, Any]] = []
        with exclusive_lock(self._project_lock(clean_project)):
            for source_root in sorted(mail_root.glob("*")):
                if not source_root.is_dir() or source_root.name in {
                    "ignored",
                    "reconciliation-receipts",
                    "undeliverable",
                }:
                    continue
                address = source_root.name.lower()
                recipient = recipients.get(address)
                recipient_status = str(
                    (recipient or {}).get("pool_status") or "active"
                ).lower()
                state_counts = {
                    state: len(list((source_root / state).glob("*.json")))
                    for state in ALL_MAILBOX_STATES
                }
                if recipient is not None and recipient_status not in {
                    "retired",
                    "not_linked",
                }:
                    examined_mailboxes.append(
                        {
                            "recipient": address,
                            "disposition": "active_recipient",
                            "eligible_states": [],
                            "eligible_records": 0,
                            "state_counts": state_counts,
                        }
                    )
                    continue
                if recipient is not None and recipient_status == "retired":
                    disposition = "retired_recipient"
                    reason = (
                        str(recipient.get("retirement_reason") or "")
                        or "The addressed worker was retired."
                    )
                    details = {
                        "recipient": address,
                        "worker_name": address,
                        "worker_alias": str(recipient.get("worker_alias") or ""),
                        "retired_at": str(recipient.get("retired_at") or ""),
                        "retirement_reason": str(
                            recipient.get("retirement_reason") or ""
                        ),
                    }
                    states = ACTIVE_MAILBOX_STATES
                elif recipient is not None:
                    disposition = "recipient_not_linked"
                    reason = (
                        f"The worker address {address} is not linked to this "
                        "project and cannot read its mailbox."
                    )
                    details = {
                        "recipient": address,
                        "worker_name": address,
                        "worker_alias": str(recipient.get("worker_alias") or ""),
                    }
                    states = ACTIVE_MAILBOX_STATES
                elif address.casefold() in aliases:
                    disposition = "recipient_alias_not_addressable"
                    stable_names = sorted(aliases[address.casefold()])
                    reason = (
                        f"{address} is a display alias, not a worker address. "
                        f"Use a stable address: {', '.join(stable_names)}."
                    )
                    details = {
                        "recipient": address,
                        "stable_worker_names": stable_names,
                    }
                    states = ALL_MAILBOX_STATES
                else:
                    disposition = "recipient_not_found"
                    reason = (
                        f"The worker address {address} does not exist in the "
                        "project recipient directory."
                    )
                    details = {"recipient": address}
                    states = ALL_MAILBOX_STATES
                examined_mailboxes.append(
                    {
                        "recipient": address,
                        "disposition": disposition,
                        "eligible_states": list(states),
                        "eligible_records": sum(
                            state_counts.get(state, 0) for state in states
                        ),
                        "state_counts": state_counts,
                    }
                )
                destination = archive_root / address
                rows = archive_mailbox_messages(
                    source_root,
                    destination,
                    states=states,
                    disposition=disposition,
                    reason=reason,
                    details=details,
                )
                if rows:
                    archived_mailboxes.append(
                        {
                            "recipient": address,
                            "disposition": disposition,
                            "count": len(rows),
                        }
                    )
                if states == ALL_MAILBOX_STATES:
                    for folder_name in ALL_MAILBOX_STATES:
                        folder = source_root / folder_name
                        try:
                            folder.rmdir()
                        except OSError:
                            pass
                    (source_root / ".mail.lock").unlink(missing_ok=True)
                    try:
                        source_root.rmdir()
                    except OSError:
                        pass

            for address_root in sorted(archive_root.glob("*")):
                if not address_root.is_dir():
                    continue
                for path in sorted(address_root.glob("*.json")):
                    undeliverable_records_examined += 1
                    row = read_json(path, required=False)
                    if not row or isinstance(row.get("failure_notice"), Mapping):
                        continue
                    failure = (
                        dict(row.get("recipient_failure") or {})
                        if isinstance(row.get("recipient_failure"), Mapping)
                        else {}
                    )
                    if not bool(failure.get("notify_sender", True)):
                        row["failure_notice"] = {
                            "delivery_status": "not_required",
                            "reason": "The archived record had already left the active mailbox.",
                        }
                        row["failure_notice_state"] = "not_required"
                        row["updated_at"] = utc_now()
                        atomic_write_json(path, row)
                        continue
                    if str(row.get("failure_notice_state") or "") == "reporting":
                        try:
                            report_age = (
                                datetime.now(timezone.utc)
                                - parse_utc(str(row.get("updated_at") or ""))
                            ).total_seconds()
                        except (DomainError, TypeError, ValueError):
                            report_age = 301
                        if report_age <= 300:
                            continue
                    row["failure_notice_state"] = "reporting"
                    row["failure_notice_reporter"] = reporter
                    row["updated_at"] = utc_now()
                    atomic_write_json(path, row)
                    moved.append((path, row))

        failure_notices: list[dict[str, Any]] = []
        report_failures: list[dict[str, Any]] = []
        seen_paths: set[Path] = set()
        for archive_path, message in moved:
            if archive_path in seen_paths:
                continue
            seen_paths.add(archive_path)
            failure = (
                dict(message.get("recipient_failure") or {})
                if isinstance(message.get("recipient_failure"), Mapping)
                else {}
            )
            details = (
                dict(failure.get("details") or {})
                if isinstance(failure.get("details"), Mapping)
                else {}
            )
            failed_recipient = str(
                details.get("recipient") or message.get("recipient") or ""
            )
            sender_identity = (
                dict(message.get("sender_identity") or {})
                if isinstance(message.get("sender_identity"), Mapping)
                else {}
            )
            sender_kind = str(sender_identity.get("kind") or "").lower()
            sender_worker = str(
                sender_identity.get("worker_name") or message.get("sender") or ""
            ).lower()
            intended_notice_recipient = (
                sender_worker if sender_kind == "worker" and sender_worker else "operator"
            )
            try:
                report = self.report_mail_delivery_failure(
                    clean_project,
                    receiver_worker_name=reporter,
                    failed_recipient=failed_recipient,
                    message=message,
                    error_code=str(failure.get("code") or "recipient_not_found"),
                    error_message=str(
                        failure.get("reason") or "The recipient cannot read this mail."
                    ),
                    error_details=details,
                    field_name="recipient",
                    field_value=failed_recipient,
                    guidance=(
                        "The original message was archived as undeliverable. "
                        "Choose an active stable worker address and send it again."
                    ),
                )
            except DomainError as exc:
                with exclusive_lock(self._project_lock(clean_project)):
                    current = read_json(archive_path, required=False)
                    if current:
                        current["failure_notice_state"] = "pending"
                        current["failure_notice_error"] = exc.to_dict()
                        current["updated_at"] = utc_now()
                        atomic_write_json(archive_path, current)
                report_failures.append(
                    {
                        "source_message_ref": str(
                            message.get("message_ref") or ""
                        ),
                        "failed_recipient": failed_recipient,
                        "notice_recipient": intended_notice_recipient,
                        "code": exc.code,
                        "reason": str(exc),
                    }
                )
                continue
            with exclusive_lock(self._project_lock(clean_project)):
                current = read_json(archive_path, required=False)
                if current:
                    current["failure_notice"] = report
                    current["failure_notice_state"] = "delivered"
                    current.pop("failure_notice_error", None)
                    current["updated_at"] = utc_now()
                    atomic_write_json(archive_path, current)
            failure_notices.append(
                {
                    "source_message_ref": str(
                        message.get("message_ref") or ""
                    ),
                    "failed_recipient": failed_recipient,
                    "notice_message_ref": str(report.get("message_ref") or ""),
                    "notice_recipient": str(report.get("recipient") or ""),
                    "delivery_route": str(report.get("delivery_route") or ""),
                    "delivery_status": str(report.get("delivery_status") or ""),
                    "replayed": bool(report.get("replayed")),
                }
            )

        completed_at = utc_now()
        receipt_id = new_timed_id("mailbox-reconciliation")
        receipt: dict[str, Any] = {
            "schema": MAILBOX_RECONCILIATION_RECEIPT_SCHEMA,
            "receipt_id": receipt_id,
            "project_ref": make_ref("project", clean_project),
            "reporter_worker_name": reporter,
            "host_id": str(reporter_record.get("host_id") or ""),
            "relay_id": str(reporter_record.get("relay_id") or ""),
            "started_at": started_at,
            "completed_at": completed_at,
            "examined": {
                "recipient_directory_entries": len(recipients),
                "undeliverable_records": undeliverable_records_examined,
                "mailboxes": examined_mailboxes,
            },
            "archived_count": sum(
                int(row.get("count") or 0) for row in archived_mailboxes
            ),
            "archived_mailboxes": archived_mailboxes,
            "failure_notices": failure_notices,
            "report_failures": report_failures,
        }
        receipt["receipt_ref"] = reference_for_record(
            "mail_reconciliation", receipt
        )
        stored_receipt = record_mailbox_reconciliation_receipt(
            self,
            clean_project,
            worker_name=reporter,
            receipt=receipt,
        )
        normalized_receipt = dict(stored_receipt.get("receipt") or {})
        return {
            "schema": "problem-board.mailbox-reconciliation.v1",
            "project_ref": make_ref("project", clean_project),
            # A run that changed nothing keeps no receipt (W287, LS1), so it
            # names none.
            "receipt_ref": (
                str(normalized_receipt.get("receipt_ref") or "")
                if stored_receipt.get("stored")
                else ""
            ),
            "receipt_stored": bool(stored_receipt.get("stored")),
            "archived_count": int(normalized_receipt.get("archived_count") or 0),
            "archived_mailboxes": archived_mailboxes,
            "failure_notices": len(failure_notices),
            "failure_notice_deliveries": failure_notices,
            "report_failures": report_failures,
        }

    def record_journal_receipt(
        self,
        project_id: str,
        *,
        worker_name: str,
        entry: Mapping[str, Any],
    ) -> dict[str, Any]:
        clean_project = (
            component(project_id, field="project_id")
            if str(project_id or "").strip()
            else ""
        )
        clean_worker = component(worker_name, field="worker_name").lower()
        self.read_project(clean_project)
        worker = self.read_worker(clean_worker)
        if worker.get("pool_status") != "active":
            raise DomainError(
                "field_worker_limbo",
                "A worker in limbo cannot index a journal entry.",
                status=403,
            )
        parsed_entry = parse_ref(str(entry.get("entry_ref") or ""))
        if parsed_entry.kind != "journal":
            raise DomainError(
                "field_journal_ref_invalid", "Expected a work:journal reference."
            )
        project_ref = str(entry.get("project_ref") or "")
        if project_ref != make_ref("project", clean_project):
            raise DomainError(
                "field_journal_project_mismatch",
                "The canonical journal entry belongs to another project.",
                status=409,
            )
        work_ref = str(entry.get("work_ref") or "")
        if work_ref:
            self._require_work_ref_shape(work_ref)
        repository_journal_ref = normalize_repository_ref(
            str(entry.get("repository_journal_ref") or ""),
            field="repository_journal_ref",
        )
        row = {
            "schema": JOURNAL_SCHEMA,
            "entry_id": parsed_entry.object_id,
            "entry_ref": str(parsed_entry),
            "project_ref": project_ref,
            "work_ref": work_ref,
            "worker_name": clean_worker,
            "title": bounded_text(
                entry.get("title") or str(parsed_entry),
                field="title",
                maximum=1000,
                required=True,
            ),
            "summary": bounded_text(
                entry.get("summary"), field="summary", maximum=8000
            ),
            "status": bounded_text(
                entry.get("status") or "recorded",
                field="status",
                maximum=64,
                required=True,
            ),
            "next_action": bounded_text(
                entry.get("next_action"),
                field="next_action",
                maximum=8000,
            ),
            "repository_journal_ref": repository_journal_ref,
            "supersedes_ref": str(
                entry.get("supersedes") or entry.get("supersedes_ref") or ""
            ),
            "content_hash": bounded_text(
                entry.get("content_hash"),
                field="content_hash",
                maximum=128,
                required=True,
            ),
            "created_at": str(entry.get("recorded_at") or utc_now()),
        }
        receipt_path = (
            self._project_dir(clean_project)
            / "journals"
            / f"{parsed_entry.object_id}.json"
        )
        with exclusive_lock(self._project_lock(clean_project)):
            existing = read_json(receipt_path, required=False)
            if existing:
                identity = {
                    "entry_ref": row["entry_ref"],
                    "project_ref": row["project_ref"],
                    "repository_journal_ref": row["repository_journal_ref"],
                    "content_hash": row["content_hash"],
                }
                if not _contains_identity(existing, identity):
                    raise DomainError(
                        "field_journal_receipt_conflict",
                        "This journal identity already records different content.",
                        status=409,
                        details={
                            "entry_ref": row["entry_ref"],
                            "repository_journal_ref": row["repository_journal_ref"],
                        },
                    )
                return dict(existing)
            event_id = (
                "journal-indexed-"
                + hashlib.sha256(row["entry_ref"].encode("utf-8")).hexdigest()[:24]
            )
            event = self._record_event_unlocked(
                clean_project,
                event_id=event_id,
                kind="journal.indexed",
                summary=f"{clean_worker} indexed a work journal entry.",
                actor=clean_worker,
                work_ref=row["work_ref"],
                metadata={
                    "entry_ref": row["entry_ref"],
                    "content_hash": row["content_hash"],
                    "repository_journal_ref": row["repository_journal_ref"],
                },
            )
            row["event_ref"] = str(event.get("event_ref") or "")
            atomic_write_json(receipt_path, row)
            return row

    def read_journal_receipt(
        self,
        project_id: str,
        *,
        entry_ref: str,
    ) -> dict[str, Any] | None:
        """Read one local journal receipt without creating or repairing state."""

        clean_project = component(project_id, field="project_id")
        parsed_entry = parse_ref(str(entry_ref or ""))
        if parsed_entry.kind != "journal":
            raise DomainError(
                "field_journal_ref_invalid", "Expected a work:journal reference."
            )
        row = read_json(
            self._project_dir(clean_project)
            / "journals"
            / f"{parsed_entry.object_id}.json",
            required=False,
        )
        return dict(row) if row else None

    def list_journals(self, project_id: str, *, work_ref: str = "") -> list[dict[str, Any]]:
        rows = json_records(self._project_dir(component(project_id, field="project_id")) / "journals")
        return [row for row in rows if not work_ref or row.get("work_ref") == work_ref]

    def _record_event_unlocked(
        self,
        project_id: str,
        *,
        event_id: str = "",
        kind: str,
        summary: str,
        actor: str,
        work_ref: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        clean_event_id = (
            component(event_id, field="event_id") if event_id else new_id("event")
        )
        row = {
            "schema": FIELD_SCHEMA,
            "event_id": clean_event_id,
            "project_ref": make_ref("project", project_id),
            "work_ref": str(work_ref or ""),
            "kind": bounded_text(kind, field="kind", maximum=128, required=True),
            "summary": bounded_text(summary, field="summary", maximum=8000, required=True),
            "actor": bounded_text(actor, field="actor", maximum=512, required=True),
            "metadata": dict(metadata or {}),
            "created_at": utc_now(),
        }
        row["event_ref"] = reference_for_record("event", row)
        path = self._project_dir(project_id) / "events" / f"{clean_event_id}.json"
        existing = read_json(path, required=False)
        if existing:
            identity = {
                "kind": row["kind"],
                "actor": row["actor"],
                "work_ref": row["work_ref"],
                "metadata": row["metadata"],
            }
            if not _contains_identity(existing, identity):
                raise DomainError(
                    "field_event_identity_conflict",
                    "This deterministic event identity already describes another event.",
                    status=409,
                    details={"event_id": clean_event_id},
                )
            return dict(existing)
        atomic_write_json(path, row)
        return row

    def record_event(self, project_id: str, **kwargs: Any) -> dict[str, Any]:
        clean_project = component(project_id, field="project_id")
        self.read_project(clean_project)
        return self._record_event_unlocked(clean_project, **kwargs)

    def list_events(self, project_id: str) -> list[dict[str, Any]]:
        return json_records(self._project_dir(component(project_id, field="project_id")) / "events")

    def acquire_scope_lease(
        self,
        project_id: str,
        *,
        worker_name: str,
        scopes: Sequence[str],
        ttl_seconds: int = 1800,
        base_revision: str = "",
    ) -> dict[str, Any]:
        clean_project = component(project_id, field="project_id")
        clean_worker = component(worker_name, field="worker_name").lower()
        if self.read_worker(clean_worker).get("pool_status") != "active":
            raise DomainError("field_worker_limbo", "A worker in limbo cannot lease source scopes.", status=403)
        normalized = sorted({_relative_scope(value) for value in scopes})
        if not normalized:
            raise DomainError("field_scope_required", "At least one source scope is required.")
        root = self._project_dir(clean_project) / "scope-leases"
        with exclusive_lock(root / ".scope-leases.lock"):
            now_dt = datetime.now(timezone.utc)
            active: list[dict[str, Any]] = []
            for path in sorted((root / "active").glob("*.json")):
                row = read_json(path)
                if parse_utc(str(row.get("expires_at") or "")) <= now_dt:
                    row.update(state="expired", settled_at=utc_now())
                    atomic_write_json(path, row)
                    os.replace(path, root / "settled" / path.name)
                else:
                    active.append(row)
            conflicts = [
                {"lease_ref": row.get("lease_ref"), "worker_name": row.get("worker_name"), "scopes": row.get("scopes")}
                for row in active
                if any(
                    _scopes_overlap(left, right)
                    for left in normalized
                    for right in (row.get("scopes") or [])
                )
            ]
            if conflicts:
                raise DomainError(
                    "field_scope_conflict",
                    "A requested source scope is already leased by another worker.",
                    status=409,
                    details={"conflicts": conflicts},
                )
            lease_id = new_id("lease")
            row = {
                "schema": FIELD_SCHEMA,
                "lease_id": lease_id,
                "lease_ref": make_ref("lease", lease_id),
                "project_ref": make_ref("project", clean_project),
                "worker_name": clean_worker,
                "scopes": normalized,
                "base_revision": str(base_revision or ""),
                "state": "active",
                "leased_at": utc_now(),
                "expires_at": _future(min(max(int(ttl_seconds), 30), 86_400)),
            }
            atomic_write_json(root / "active" / f"{lease_id}.json", row)
            return row

    def release_scope_lease(
        self,
        project_id: str,
        *,
        lease_ref: str,
        worker_name: str,
        outcome: str = "released",
    ) -> dict[str, Any]:
        parsed = parse_ref(lease_ref)
        if parsed.kind != "lease":
            raise DomainError("field_lease_ref_invalid", "Expected a work:lease reference.")
        clean_project = component(project_id, field="project_id")
        root = self._project_dir(clean_project) / "scope-leases"
        source = root / "active" / f"{component(parsed.object_id)}.json"
        with exclusive_lock(root / ".scope-leases.lock"):
            row = read_json(source)
            if row.get("worker_name") != component(worker_name, field="worker_name").lower():
                raise DomainError("field_lease_owner_mismatch", "Only the lease owner may release this scope.", status=403)
            row.update(state=str(outcome or "released"), settled_at=utc_now())
            atomic_write_json(source, row)
            os.replace(source, root / "settled" / source.name)
            return row

    # How long an assignee may be silent before a working item stops counting
    # as moving. Generous on purpose: an agent deep in one long turn is working,
    # and calling that stalled would be the same false confidence in the other
    # direction. Twenty minutes is longer than any single turn we have seen and
    # far shorter than the ninety minutes a worker once sat deaf unnoticed.
    STALLED_AFTER_SECONDS = 1200

    def _assignee_last_activity(self, project_id: str) -> dict[str, str]:
        """When each worker last did something, from every signal we already keep.

        Three sources, because no single one means active. An inbox check says
        the session is alive. A service event says it did work. Outbox activity
        covers work waiting to reach the board. Journal writes already record a
        journal.appended service event, so rereading every journal receipt here
        would count the same activity twice and slow every project packet.
        """

        latest: dict[str, str] = {}

        def note(worker: str, when: str) -> None:
            name = str(worker or "").strip().lower()
            stamp = str(when or "")
            if not name or not stamp:
                return
            if stamp > latest.get(name, ""):
                latest[name] = stamp

        for row in json_records(self.control / "workers"):
            listener = listener_without_legacy_fields(row.get("listener"))
            note(str(row.get("worker_name") or ""), str(listener.get("last_inbox_check_at") or ""))
        for row in self.list_events(project_id):
            # An event names its worker as "actor". Reading worker_name here
            # found nothing and every assignee looked silent, which is the same
            # false confidence this item exists to remove.
            note(
                str(row.get("actor") or row.get("worker_name") or ""),
                str(row.get("created_at") or ""),
            )
        # Reports and service events reach the board through the outbox, so a
        # worker that queued one has demonstrably done something even before it
        # is delivered. Leaving these out made a worker look silent between
        # doing the work and the relay sending it. Only rows still in flight
        # count: a delivered row is already a service event, and reading the
        # delivered history made every plan index pay for 54,843 rows on
        # dev-main (W287, rule LS3 in storage-and-retention.md).
        for state in OUTBOX_IN_FLIGHT_FOLDERS:
            for row in json_records(self.control / "outbox" / state):
                note(str(row.get("worker_name") or ""), str(row.get("created_at") or ""))
        return latest

    def _mark_stalled_work(
        self, project_id: str, items: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """Say which working items are actually moving.

        An item goes to working when somebody says so and nothing ever
        contradicts it. An item can remain at working while its assignee is idle,
        which an operator may read as progress. The board already held the evidence and
        ignored it.

        Derived rather than stored, which is the whole point: a worker that
        starts doing things again stops being stalled without anyone editing a
        status by hand, and no write is needed to keep the mark honest.
        """

        latest = self._assignee_last_activity(project_id)
        decorated: list[dict[str, Any]] = []
        for item in items:
            row = dict(item)
            assignee = str(row.get("assignee") or "").strip().lower()
            if row.get("status") == "working" and assignee:
                seen = latest.get(assignee, "")
                silent = _seconds_since(seen) if seen else None
                row["assignee_last_activity_at"] = seen
                row["assignee_silent_seconds"] = silent
                row["stalled"] = bool(
                    silent is None or silent > self.STALLED_AFTER_SECONDS
                )
                # Say why, so the reader does not have to guess whether this is
                # a worker that went quiet or one that never reported at all.
                row["stalled_reason"] = (
                    ""
                    if not row["stalled"]
                    else ("no recorded activity" if seen == "" else "assignee silent")
                )
            decorated.append(row)
        return decorated

    def plan_authority(self, project_id: str) -> dict[str, Any]:
        """What this machine's plan actually is, said out loud.

        Every machine holds a shard. A worker on another host can author an
        item this field has never seen, so the local plan is a partial view
        that has always been presented as though it were the plan. Nothing in
        the packet distinguished "this item does not exist" from "this machine
        cannot see the whole plan", which is the same absence-with-two-meanings
        that has cost us a worker outage and a silently empty index already.

        No mirrored server snapshot exists yet, so the answer is always
        local_shard today. The shape is the point: when a snapshot is mirrored
        it carries the generation it came from, and a reader can tell the two
        apart instead of inferring.
        """

        plan = self.current_plan(project_id)
        return {
            "authority": PLAN_AUTHORITY_LOCAL_SHARD,
            "complete": False,
            "plan_revision": int(plan.get("revision") or 0),
            "item_count": len(plan.get("items") or []),
            # Deliberately not a timestamp. Age cannot establish that no newer
            # generation exists, so a freshness number here would invite
            # exactly the wrong inference: that a young view is a current one.
            "reason": "no_server_snapshot_mirrored",
        }

    def require_complete_plan(self, project_id: str) -> dict[str, Any]:
        """The whole plan, or an honest refusal, never a shard wearing its name.

        For callers that need every item to be present for their answer to
        mean anything: deciding a reference names nothing, or reporting the
        plan as a whole. A shard cannot answer those, and answering anyway is
        how a partial view becomes a confident wrong one.

        Retryable on purpose. The complete index is a thing that arrives, so
        the caller should come back rather than treat this as a verdict.
        """

        authority = self.plan_authority(project_id)
        if not authority.get("complete"):
            raise DomainError(
                "work_plan_index_not_ready",
                (
                    "This machine holds a shard of the plan, not the whole "
                    "plan, so it cannot answer for every item. Try again once "
                    "the shared index is readable here."
                ),
                status=409,
                details={"retryable": True, **authority},
            )
        return authority

    def project_delivery_context(
        self, project_id: str, *, work_ref: str = ""
    ) -> dict[str, Any]:
        """Small local delivery context with no mirrored plan state."""

        clean_project = component(project_id, field="project_id")
        project = self.read_project(clean_project)
        journal_receipts = newest_json_records(
            self._project_dir(clean_project) / "journals",
            limit=20,
            predicate=(
                (lambda row: row.get("work_ref") == work_ref)
                if work_ref
                else None
            ),
        )
        return {
            "schema": FIELD_SCHEMA,
            "project": project,
            "plan": {
                "authority": "postgresql",
                "included": False,
                "index_operation": "project.plan.index",
                "item_operation": "project.plan.item",
                "git_export": "plan/",
            },
            "assignments": self.list_assignments(
                clean_project, work_ref=work_ref
            ),
            "team": self.read_project_team(clean_project),
            "journals": journal_receipts,
            "events": [
                row
                for row in self.list_events(clean_project)
                if not work_ref or row.get("work_ref") == work_ref
            ][-50:],
        }

    def projection(self, project_id: str) -> dict[str, Any]:
        project = self.read_project(project_id)
        plan = self.current_plan(project_id)
        # The projection is what the board draws, so the stalled mark has to
        # travel here too. Decorating only the local packet left the one
        # surface an operator actually looks at still showing a claim nothing
        # contradicted.
        plan = {**plan, "items": self._mark_stalled_work(project_id, plan.get("items") or [])}
        return build_projection(
            project=project,
            graph=plan,
            workers=self.list_workers(),
            events=self.list_events(project_id),
            contested=self.list_contested_calls(project_id),
        )

    def plan_nodes_document(
        self,
        project_id: str,
        *,
        item_refs: Sequence[str] | None = None,
        expected_revisions: Mapping[str, int] | None = None,
    ) -> dict[str, Any]:
        """Build one integrity-checked batch of selected plan nodes."""

        clean_project = component(project_id, field="project_id")
        project = self.read_project(clean_project)
        plan = self.current_plan(clean_project)
        items = [row for row in (plan.get("items") or []) if isinstance(row, Mapping)]
        selected_refs = (
            {
                parse_plan_node_ref(value).ref
                for value in item_refs
            }
            if item_refs is not None
            else None
        )
        dependencies: dict[str, list[str]] = {}
        for edge in plan.get("dependencies") or []:
            if not isinstance(edge, Mapping):
                continue
            dependencies.setdefault(
                str(edge.get("item_ref") or ""), []
            ).append(
                str(
                    edge.get("depends_on_ref") or ""
                )
            )

        rows: list[dict[str, Any]] = []
        for position, row in enumerate(items):
            item_ref = str(row.get("item_ref") or "")
            if selected_refs is not None and item_ref not in selected_refs:
                continue
            indexed = indexed_plan_item(
                row,
                ordinal=position,
                depends_on=dependencies.get(item_ref, []),
            )
            if expected_revisions is not None and item_ref in expected_revisions:
                indexed["expected_revision"] = int(expected_revisions[item_ref])
            rows.append(indexed)

        if selected_refs is not None:
            carried = {str(row.get("item_ref") or "") for row in rows}
            missing = sorted(selected_refs - carried)
            if missing:
                raise DomainError(
                    "field_item_not_found",
                    "A requested plan node does not exist in the local authoring batch.",
                    status=404,
                    details={"missing": missing},
                )

        document = {
            "schema": PLAN_NODES_SCHEMA,
            "project_ref": str(project.get("project_ref") or ""),
            "node_count": len(rows),
            "nodes": rows,
        }
        document["content_hash"] = content_hash(document)
        return document

    def enqueue_plan_nodes(
        self,
        project_id: str,
        *,
        item_refs: Sequence[str] | None = None,
        expected_revisions: Mapping[str, int] | None = None,
        worker_name: str = "",
    ) -> dict[str, Any]:
        """Queue one node batch; unrelated batches are never coalesced."""

        clean_project = component(project_id, field="project_id")
        clean_worker = ""
        if worker_name:
            worker = self.read_worker(worker_name)
            if worker.get("pool_status") != "active":
                raise DomainError(
                    "field_worker_limbo",
                    "A worker in limbo cannot publish plan nodes.",
                    status=403,
                )
            clean_worker = component(worker_name, field="worker_name").lower()
        document = self.plan_nodes_document(
            clean_project,
            item_refs=item_refs,
            expected_revisions=expected_revisions,
        )
        if not document["nodes"]:
            raise DomainError(
                "field_plan_nodes_empty",
                "Publish at least one plan node.",
            )
        project_ref = str(document["project_ref"])
        batch_hash = str(document["content_hash"])
        root = self.control / "outbox"
        now = utc_now()

        with exclusive_lock(root / ".outbox.lock"):
            for state in OUTBOX_FOLDERS:
                for path in (root / state).glob("*.json"):
                    existing = read_json(path)
                    if (
                        existing.get("kind") == "plan.nodes.publish"
                        and existing.get("project_ref") == project_ref
                        and existing.get("content_hash") == batch_hash
                    ):
                        return {**existing, "replayed": True, "coalesced": False}

            outbox_id = new_id("outbox")
            row = {
                "schema": OUTBOX_SCHEMA,
                "outbox_id": outbox_id,
                "kind": "plan.nodes.publish",
                "worker_name": clean_worker,
                "project_ref": project_ref,
                "content_hash": batch_hash,
                "payload": document,
                "state": "pending",
                "created_at": now,
                "coalesced_count": 0,
                "retry_count": 0,
                "next_attempt_at": "",
            }
            atomic_write_json(root / "pending" / f"{outbox_id}.json", row)
            return {**row, "replayed": False, "coalesced": False}

    def _publish_plan_nodes_unlocked(
        self,
        project_id: str,
        *,
        item_refs: Sequence[str] | None = None,
        expected_revisions: Mapping[str, int] | None = None,
    ) -> None:
        self.enqueue_plan_nodes(
            project_id,
            item_refs=item_refs,
            expected_revisions=expected_revisions,
        )

    def enqueue_service_event(
        self,
        project_id: str,
        *,
        worker_name: str,
        kind: str,
        summary: str,
        source_event_ref: str,
        work_ref: str = "",
        metadata: Mapping[str, Any] | None = None,
        content_hash_value: str = "",
    ) -> dict[str, Any]:
        clean_project = component(project_id, field="project_id")
        self.read_project(clean_project)
        normalized_work_ref = str(work_ref or "")
        if normalized_work_ref:
            self._require_work_ref_shape(normalized_work_ref)
        outbox_id = new_id("outbox")
        payload = {
            "project_ref": (
                make_ref("project", clean_project) if clean_project else ""
            ),
            "kind": bounded_text(kind, field="kind", maximum=128, required=True),
            "summary": bounded_text(summary, field="summary", maximum=8000, required=True),
            "source_event_ref": bounded_text(
                source_event_ref, field="source_event_ref", maximum=1000, required=True
            ),
            "work_ref": normalized_work_ref,
            "metadata": dict(metadata or {}),
            "content_hash": str(content_hash_value or ""),
        }
        row = {
            "schema": OUTBOX_SCHEMA,
            "outbox_id": outbox_id,
            "kind": "event.publish",
            "worker_name": component(worker_name, field="worker_name").lower(),
            "project_ref": payload["project_ref"],
            "content_hash": content_hash(payload),
            "payload": payload,
            "state": "pending",
            "created_at": utc_now(),
        }
        atomic_write_json(self.control / "outbox" / "pending" / f"{outbox_id}.json", row)
        return row

    def enqueue_control_settlement(
        self,
        project_id: str,
        *,
        worker_name: str,
        command_ref: str,
        message_ref: str,
        session_id: str,
        outcome: str,
        summary: str,
    ) -> dict[str, Any]:
        parsed = parse_ref(command_ref)
        if parsed.kind != "control":
            raise DomainError(
                "field_control_ref_invalid", "Expected a work:control reference."
            )
        clean_project = (
            component(project_id, field="project_id")
            if str(project_id or "").strip()
            else ""
        )
        payload = {
            "command_ref": command_ref,
            "message_ref": bounded_text(
                message_ref, field="message_ref", maximum=256, required=True
            ),
            "session_id": bounded_text(
                session_id, field="session_id", maximum=128, required=True
            ),
            "outcome": bounded_text(
                outcome, field="outcome", maximum=32, required=True
            ),
            "summary": bounded_text(summary, field="summary", maximum=8000),
        }
        outbox_id = new_id("outbox")
        row = {
            "schema": OUTBOX_SCHEMA,
            "outbox_id": outbox_id,
            "kind": "control.worker_settle",
            "worker_name": component(
                worker_name, field="worker_name"
            ).lower(),
            "project_ref": (
                make_ref("project", clean_project) if clean_project else ""
            ),
            "content_hash": content_hash(payload),
            "payload": payload,
            "state": "pending",
            "created_at": utc_now(),
        }
        atomic_write_json(
            self.control / "outbox" / "pending" / f"{outbox_id}.json", row
        )
        return row

    def _assignment_report_receipt_path(
        self,
        project_id: str,
        assignment_id: str,
        ownership_version: int,
        report_key: str = "",
    ) -> Path:
        identity = {
            "assignment_id": assignment_id,
            "ownership_version": int(ownership_version),
        }
        if report_key:
            identity["report_key"] = str(report_key)
        token = content_hash(identity)
        return (
            self._project_dir(project_id)
            / "idempotency"
            / "assignment-report"
            / f"{token}.json"
        )

    def _assignment_report_receipt_paths(
        self,
        project_id: str,
        *,
        assignment_ref: str,
        ownership_version: int,
        report_key: str = "",
    ) -> list[Path]:
        parsed = parse_ref(assignment_ref)
        stable_path = self._assignment_report_receipt_path(
            project_id,
            parsed.object_id,
            ownership_version,
            report_key,
        )
        root = stable_path.parent
        paths = [stable_path]
        for path in sorted(root.glob("*.json")):
            if path == stable_path:
                continue
            row = read_json(path, required=False)
            if not row:
                continue
            try:
                row_ref = parse_ref(str(row.get("assignment_ref") or ""))
            except DomainError:
                continue
            if (
                row_ref.kind == "assignment"
                and row_ref.object_id == parsed.object_id
                and int(row.get("ownership_version") or 0)
                == int(ownership_version)
            ):
                paths.append(path)
        return paths

    def enqueue_assignment_report(
        self,
        project_id: str,
        *,
        worker_name: str,
        assignment_ref: str,
        ownership_version: int,
        state: str,
        summary: str,
        result_ref: str,
        source_event_ref: str,
        review_look_at: str | None = None,
        review_could_not_verify: str | None = None,
        scope: str = "",
    ) -> dict[str, Any]:
        clean_project = component(project_id, field="project_id")
        clean_worker = component(worker_name, field="worker_name").lower()
        worker = self.read_worker(clean_worker)
        if worker.get("pool_status") != "active":
            raise DomainError(
                "field_worker_limbo",
                "A worker in limbo cannot report assignment effects.",
                status=403,
            )
        parsed = parse_ref(assignment_ref)
        if parsed.kind != "assignment":
            raise DomainError(
                "field_assignment_ref_invalid",
                "Expected a work:assignment reference.",
            )
        path = (
            self._project_dir(clean_project)
            / "assignments"
            / f"{component(parsed.object_id)}.json"
        )
        try:
            version = int(ownership_version)
        except (TypeError, ValueError) as exc:
            raise DomainError(
                "field_assignment_version_invalid",
                "ownership_version must be an integer.",
            ) from exc
        normalized_state = bounded_text(
            state, field="state", maximum=32, required=True
        ).lower()
        if normalized_state not in {"working", "blocked", "completed", "refused"}:
            raise DomainError(
                "field_assignment_state_invalid",
                "Assignment state must be working, blocked, completed, or refused.",
            )
        review_supplied = review_look_at is not None or review_could_not_verify is not None
        if review_supplied and normalized_state != "completed":
            raise DomainError(
                "field_review_report_state_invalid",
                "Review statements belong to a completed assignment report.",
            )
        requested_assignment_ref = assignment_ref
        payload = {
            "assignment_ref": requested_assignment_ref,
            "ownership_version": version,
            "state": normalized_state,
            "summary": bounded_text(
                summary, field="summary", maximum=8000, required=True
            ),
            "result_ref": bounded_text(
                result_ref, field="result_ref", maximum=1000
            ),
            "source_event_ref": bounded_text(
                source_event_ref,
                field="source_event_ref",
                maximum=1000,
                required=True,
            ),
        }
        # P4b (W278): the worker's one line about where it will edit rides the
        # report and stays on the assignment. Optional, so an older service that
        # does not know it sees nothing new.
        clean_scope = bounded_text(scope, field="scope", maximum=512)
        if clean_scope:
            payload["scope"] = clean_scope
        if review_supplied:
            payload["review"] = {
                "look_at": bounded_text(
                    review_look_at,
                    field="review.look_at",
                    maximum=8000,
                    required=True,
                ),
                "could_not_verify": bounded_text(
                    review_could_not_verify,
                    field="review.could_not_verify",
                    maximum=8000,
                    required=True,
                ),
            }
        request = {
                "assignment_id": parsed.object_id,
                "ownership_version": version,
                "state": normalized_state,
                "summary": payload["summary"],
                "result_ref": payload["result_ref"],
                "source_event_ref": payload["source_event_ref"],
        }
        if review_supplied:
            request["review"] = payload["review"]
        request_hash = content_hash(request)
        report_key = content_hash(
            {
                "state": normalized_state,
                "source_event_ref": payload["source_event_ref"],
            }
        )
        receipt_path = self._assignment_report_receipt_path(
            clean_project,
            parsed.object_id,
            version,
            report_key,
        )
        with exclusive_lock(self._project_lock(clean_project)):
            assignment = read_json(path, required=False)
            receipt_paths = self._assignment_report_receipt_paths(
                clean_project,
                assignment_ref=requested_assignment_ref,
                ownership_version=version,
                report_key=report_key,
            )
            receipts = [
                (candidate_path, receipt)
                for candidate_path in receipt_paths
                if (receipt := read_json(candidate_path, required=False))
            ]
            canonical_assignment_ref = requested_assignment_ref
            candidate_refs = [
                str(assignment.get("assignment_ref") or ""),
                *(
                    str(receipt.get("assignment_ref") or "")
                    for _candidate_path, receipt in receipts
                ),
                requested_assignment_ref,
            ]
            for candidate_ref in candidate_refs:
                if not candidate_ref:
                    continue
                try:
                    candidate = parse_ref(candidate_ref)
                except DomainError:
                    continue
                if (
                    candidate.kind == "assignment"
                    and candidate.object_id == parsed.object_id
                    and candidate.is_canonical
                ):
                    canonical_assignment_ref = str(candidate)
                    break
            payload["assignment_ref"] = canonical_assignment_ref
            compatible_hashes = {request_hash}
            compatible_assignment_refs = {
                requested_assignment_ref,
                canonical_assignment_ref,
                f"work:assignment:{parsed.object_id}",
            }
            compatible_assignment_refs.update(
                str(receipt.get("assignment_ref") or "")
                for _candidate_path, receipt in receipts
            )
            for candidate_ref in compatible_assignment_refs:
                if candidate_ref:
                    compatible_hashes.add(
                        content_hash({**payload, "assignment_ref": candidate_ref})
                    )
            for _candidate_path, receipt in receipts:
                if str(receipt.get("request_hash") or "") not in compatible_hashes:
                    continue
                canonical_receipt = {
                    **receipt,
                    "assignment_id": parsed.object_id,
                    "assignment_ref": canonical_assignment_ref,
                    "ownership_version": version,
                    "report_key": report_key,
                    "state": normalized_state,
                    "source_event_ref": payload["source_event_ref"],
                    "request_hash": request_hash,
                }
                atomic_write_json(receipt_path, canonical_receipt)
                return {**canonical_receipt, "replayed": True}
            prior_receipt = next(
                (
                    receipt
                    for _candidate_path, receipt in receipts
                    if str(receipt.get("report_key") or "") == report_key
                    or (
                        str(receipt.get("state") or "") == normalized_state
                        and str(receipt.get("source_event_ref") or "")
                        == payload["source_event_ref"]
                    )
                ),
                None,
            )
            if prior_receipt is not None:
                prior_state = str(prior_receipt.get("state") or "")
                prior_time = str(prior_receipt.get("created_at") or "")
                prior_ref = str(
                    prior_receipt.get("remote_ref")
                    or prior_receipt.get("outbox_id")
                    or ""
                )
                raise DomainError(
                    "field_assignment_report_idempotency_conflict",
                    (
                        "This report identity already has different content."
                        f" Prior report: {prior_ref or 'unknown'}"
                        f"{f', state {prior_state}' if prior_state else ''}"
                        f"{f', queued {prior_time}' if prior_time else ''}."
                        " Retry the prior report unchanged, or use a distinct "
                        "source_event_ref for a later report."
                    ),
                    status=409,
                    details={
                        "prior_report_ref": prior_ref,
                        "prior_report_state": prior_state,
                        "prior_report_time": prior_time,
                        "prior_source_event_ref": str(
                            prior_receipt.get("source_event_ref") or ""
                        ),
                        "required_action": (
                            "Retry the prior report unchanged, or use a distinct "
                            "source_event_ref for a later report."
                        ),
                    },
                )
            local_assignment_current = bool(
                assignment.get("worker_name") == clean_worker
                and int(assignment.get("ownership_version") or 0) == version
            )
            outbox_id = new_id("outbox")
            created_at = utc_now()
            row = {
                "schema": OUTBOX_SCHEMA,
                "outbox_id": outbox_id,
                "kind": "assignment.report",
                "worker_name": clean_worker,
                "project_ref": make_ref("project", clean_project),
                "content_hash": content_hash(payload),
                "payload": payload,
                "state": "pending",
                "created_at": created_at,
            }
            atomic_write_json(
                self.control / "outbox" / "pending" / f"{outbox_id}.json", row
            )
            result = {
                "outbox_id": outbox_id,
                "assignment_id": parsed.object_id,
                "assignment_ref": canonical_assignment_ref,
                "ownership_version": version,
                "state": normalized_state,
                "report_key": report_key,
                "source_event_ref": payload["source_event_ref"],
                "request_hash": request_hash,
                "local_assignment_current": local_assignment_current,
                "created_at": created_at,
            }
            atomic_write_json(receipt_path, result)
        return {**result, "replayed": False}

    def _leased_project_report_message(
        self,
        project_id: str,
        *,
        worker_name: str,
        message_ref: str,
        lease_id: str,
        lease_owner: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return the exact live report request held by this worker session.

        The caller holds this mailbox's lock. Report publication is an effect
        of addressed work, so a report file alone is never enough authority to
        queue it.
        """

        parsed_message = parse_ref(message_ref)
        if parsed_message.kind != "mail":
            raise DomainError(
                "field_mail_ref_invalid", "Expected a work:mail reference."
            )
        clean_project = component(project_id, field="project_id")
        clean_worker = component(worker_name, field="worker_name").lower()
        root = self._mail_root(clean_project, clean_worker)
        self._recover_expired_mail(clean_project, clean_worker)
        filename = f"{component(parsed_message.object_id)}.json"
        row = read_json(root / "leased" / filename, required=False)
        if not row:
            if read_json(root / "inbox" / filename, required=False):
                raise DomainError(
                    "field_mail_lease_expired",
                    "The project report request returned for redelivery. Receive it again before publishing.",
                    status=409,
                    details={"message_ref": message_ref},
                )
            if read_json(root / "processed" / filename, required=False):
                raise DomainError(
                    "field_mail_already_settled",
                    "The project report request was already settled.",
                    status=409,
                    details={"message_ref": message_ref},
                )
            raise DomainError(
                "field_project_report_request_not_leased",
                "This worker does not hold the named project report request.",
                status=409,
                details={"message_ref": message_ref},
            )
        lease = row.get("lease") if isinstance(row.get("lease"), Mapping) else {}
        if (
            str(lease.get("lease_id") or "") != str(lease_id or "")
            or str(lease.get("owner") or "") != str(lease_owner or "")
        ):
            raise DomainError(
                "field_mail_lease_mismatch",
                "Only the current lease owner may answer this project report request.",
                status=409,
            )
        if parse_utc(str(lease.get("expires_at") or "")) <= datetime.now(timezone.utc):
            raise DomainError(
                "field_mail_lease_expired",
                "The project report lease expired before publication was queued.",
                status=409,
            )
        if (
            str(row.get("recipient") or "").lower() != clean_worker
            or str(row.get("project_ref") or "") != make_ref("project", clean_project)
            or str(row.get("kind") or "") != "project.report"
        ):
            raise DomainError(
                "field_project_report_request_required",
                "The leased message is not this worker's project report request.",
                status=409,
            )
        payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
        command_ref = bounded_text(
            payload.get("command_ref"),
            field="command_ref",
            maximum=256,
            required=True,
        )
        if parse_ref(command_ref).kind != "control":
            raise DomainError(
                "field_control_ref_invalid", "Expected a work:control reference."
            )
        command = (
            dict(payload.get("command"))
            if isinstance(payload.get("command"), Mapping)
            else {}
        )
        report_ref = bounded_text(
            command.get("report_ref"),
            field="report_ref",
            maximum=256,
            required=True,
        )
        if parse_ref(report_ref).kind != "report":
            raise DomainError(
                "field_project_report_ref_invalid",
                "Expected a work:report reference.",
            )
        if str(command.get("project_ref") or "") != make_ref("project", clean_project):
            raise DomainError(
                "field_project_report_project_mismatch",
                "The report request and its project mailbox do not match.",
                status=409,
            )
        return row, command

    def _project_report_receipt_path(
        self, project_id: str, *, report_ref: str
    ) -> Path:
        report = parse_ref(report_ref)
        if report.kind != "report":
            raise DomainError(
                "field_project_report_ref_invalid",
                "Expected a work:report reference.",
            )
        return (
            self._project_dir(project_id)
            / "idempotency"
            / "project-report"
            / f"{component(report.object_id)}.json"
        )

    def enqueue_project_report_publish(
        self,
        project_id: str,
        *,
        worker_name: str,
        message_ref: str,
        lease_id: str,
        lease_owner: str,
        summary: str,
        work_refs: Sequence[str] | None = None,
        attachments: Sequence[Mapping[str, Any]] | None = None,
        not_seen: Sequence[Any] = (),
    ) -> dict[str, Any]:
        """Snapshot one leased report submission and its files into the outbox.

        What is queued is a submission: the author's summary, the work it
        points at, its files, and what the author could not see. The service
        composes the rest from rows it owns. The submission is validated here by
        the same module the service validates it with, so a document the service
        would refuse is refused before anything is queued.
        """

        clean_project = component(project_id, field="project_id")
        clean_worker = component(worker_name, field="worker_name").lower()
        self.read_project(clean_project)
        worker = self.read_worker(clean_worker)
        if worker.get("pool_status") != "active":
            raise DomainError(
                "field_worker_limbo",
                "A worker in limbo cannot publish a project report.",
                status=403,
            )
        from ..contract.project_report_contract import build_submission

        # Validate the author's fields before any file is read or any lock taken.
        build_submission(summary=summary, work_refs=work_refs, not_seen=not_seen)

        snapshots: list[dict[str, Any]] = []
        filenames: set[str] = set()
        for item in attachments or []:
            source = Path(str((item or {}).get("path") or "")).expanduser()
            if not source.is_file():
                raise DomainError(
                    "field_attachment_missing",
                    "An attachment path does not name a readable file.",
                    details={"path": str(source)},
                )
            filename = Path(
                bounded_text(
                    (item or {}).get("filename") or source.name,
                    field="attachment.filename",
                    maximum=512,
                    required=True,
                )
            ).name
            if filename in filenames:
                raise DomainError(
                    "field_attachment_filename_duplicate",
                    "Project report attachment filenames must be unique.",
                    details={"filename": filename},
                )
            filenames.add(filename)
            data = source.read_bytes()
            snapshots.append(
                {
                    "filename": filename,
                    "mime": mimetypes.guess_type(filename)[0]
                    or "application/octet-stream",
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "data": data,
                }
            )

        root = self._mail_root(clean_project, clean_worker)
        with exclusive_lock(root / ".mail.lock"):
            _message, command = self._leased_project_report_message(
                clean_project,
                worker_name=clean_worker,
                message_ref=message_ref,
                lease_id=lease_id,
                lease_owner=lease_owner,
            )
            report_ref = str(command["report_ref"])
            receipt_path = self._project_report_receipt_path(
                clean_project, report_ref=report_ref
            )
            receipt = read_json(receipt_path, required=False)
            created_at = str((receipt or {}).get("created_at") or utc_now())
            manifest = [
                {
                    "filename": item["filename"],
                    "mime": item["mime"],
                    "size": item["size"],
                    "sha256": item["sha256"],
                }
                for item in snapshots
            ]
            document = build_submission(
                summary=summary,
                work_refs=work_refs,
                attachments=manifest,
                not_seen=not_seen,
            )
            request_hash = content_hash(
                {
                    "action": "project.report.publish",
                    "report_ref": report_ref,
                    "document": document,
                    "attachments": manifest,
                }
            )
            if receipt:
                if receipt.get("request_hash") != request_hash:
                    raise DomainError(
                        "field_project_report_idempotency_conflict",
                        "This report request was already answered with different content.",
                        status=409,
                    )
                return {**receipt, "replayed": True}

            outbox_id = new_id("outbox")
            attachment_files: list[dict[str, Any]] = []
            if snapshots:
                folder = self.control / "outbox" / "attachments" / outbox_id
                folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                for snapshot in snapshots:
                    target = folder / str(snapshot["filename"])
                    target.write_bytes(snapshot["data"])
                    attachment_files.append(
                        {
                            key: snapshot[key]
                            for key in ("filename", "mime", "size", "sha256")
                        }
                        | {"path": str(target)}
                    )
            payload: dict[str, Any] = {"document": document}
            if attachment_files:
                payload["attachment_files"] = attachment_files
            row = {
                "schema": OUTBOX_SCHEMA,
                "outbox_id": outbox_id,
                "kind": "project.report.publish",
                "worker_name": clean_worker,
                "project_ref": make_ref("project", clean_project),
                "object_ref": report_ref,
                "source_message_ref": message_ref,
                "content_hash": request_hash,
                "payload": payload,
                "state": "pending",
                "created_at": utc_now(),
            }
            atomic_write_json(
                self.control / "outbox" / "pending" / f"{outbox_id}.json", row
            )
            result = {
                "outbox_id": outbox_id,
                "report_ref": report_ref,
                "message_ref": message_ref,
                "request_hash": request_hash,
                "created_at": created_at,
                "attachment_count": len(attachment_files),
                "delivery_status": "queued",
            }
            atomic_write_json(receipt_path, result)
        self.record_handling(
            worker_name=clean_worker,
            message_ref=message_ref,
            action="project-report:publish-queued",
            detail=report_ref,
        )
        return {**result, "replayed": False}

    def leased_project_report(
        self,
        project_id: str,
        *,
        worker_name: str,
        message_ref: str,
        lease_id: str,
        lease_owner: str,
    ) -> dict[str, Any]:
        """The report request behind one live lease: its ref and its predecessor."""

        clean_project = component(project_id, field="project_id")
        clean_worker = component(worker_name, field="worker_name").lower()
        root = self._mail_root(clean_project, clean_worker)
        with exclusive_lock(root / ".mail.lock"):
            _message, command = self._leased_project_report_message(
                clean_project,
                worker_name=clean_worker,
                message_ref=message_ref,
                lease_id=lease_id,
                lease_owner=lease_owner,
            )
        return {
            "report_ref": str(command.get("report_ref") or ""),
            "since_report_ref": str(command.get("since_report_ref") or ""),
            "ask": str(command.get("ask") or ""),
        }

    def read_outbox_record(self, outbox_id: str) -> dict[str, Any] | None:
        """One outbox row wherever it currently is, or None when it is unknown.

        A queued row is intent. Its terminal state (`sent`, `refused`,
        `ignored`) is written by the relay after the service answered, and that
        is the only thing a caller may read as an outcome.
        """

        clean_id = component(outbox_id, field="outbox_id")
        root = self.control / "outbox"
        for folder in ("sent", "refused", "leased", "pending"):
            row = read_json(root / folder / f"{clean_id}.json", required=False)
            if row:
                return dict(row)
        return None

    def enqueue_project_report_failure(
        self,
        project_id: str,
        *,
        worker_name: str,
        message_ref: str,
        lease_id: str,
        lease_owner: str,
        error_code: str,
        error_summary: str,
    ) -> dict[str, Any]:
        """Queue a terminal report failure for one exact leased request."""

        clean_project = component(project_id, field="project_id")
        clean_worker = component(worker_name, field="worker_name").lower()
        self.read_project(clean_project)
        worker = self.read_worker(clean_worker)
        if worker.get("pool_status") != "active":
            raise DomainError(
                "field_worker_limbo",
                "A worker in limbo cannot fail a project report.",
                status=403,
            )
        failure = {
            "error_code": bounded_text(
                error_code, field="error_code", maximum=128, required=True
            ),
            "error_summary": bounded_text(
                error_summary,
                field="error_summary",
                maximum=8000,
                required=True,
            ),
        }
        root = self._mail_root(clean_project, clean_worker)
        with exclusive_lock(root / ".mail.lock"):
            _message, command = self._leased_project_report_message(
                clean_project,
                worker_name=clean_worker,
                message_ref=message_ref,
                lease_id=lease_id,
                lease_owner=lease_owner,
            )
            report_ref = str(command["report_ref"])
            request_hash = content_hash(
                {
                    "action": "project.report.fail",
                    "report_ref": report_ref,
                    **failure,
                }
            )
            receipt_path = self._project_report_receipt_path(
                clean_project, report_ref=report_ref
            )
            receipt = read_json(receipt_path, required=False)
            if receipt:
                if receipt.get("request_hash") != request_hash:
                    raise DomainError(
                        "field_project_report_idempotency_conflict",
                        "This report request was already answered with a different result.",
                        status=409,
                    )
                return {**receipt, "replayed": True}
            outbox_id = new_id("outbox")
            row = {
                "schema": OUTBOX_SCHEMA,
                "outbox_id": outbox_id,
                "kind": "project.report.fail",
                "worker_name": clean_worker,
                "project_ref": make_ref("project", clean_project),
                "object_ref": report_ref,
                "source_message_ref": message_ref,
                "content_hash": request_hash,
                "payload": failure,
                "state": "pending",
                "created_at": utc_now(),
            }
            atomic_write_json(
                self.control / "outbox" / "pending" / f"{outbox_id}.json", row
            )
            result = {
                "outbox_id": outbox_id,
                "report_ref": report_ref,
                "message_ref": message_ref,
                "request_hash": request_hash,
                "delivery_status": "queued",
            }
            atomic_write_json(receipt_path, result)
        self.record_handling(
            worker_name=clean_worker,
            message_ref=message_ref,
            action="project-report:failure-queued",
            detail=report_ref,
        )
        return {**result, "replayed": False}

    def enqueue_plan_ref_resolution(
        self,
        project_id: str,
        *,
        worker_name: str,
        item_ref: str,
        outbox_id: str = "",
    ) -> dict[str, Any]:
        """Ask PostgreSQL plan authority to resolve one URI.

        The outbox retains only the request and a bounded yes/no proof. It does
        not mirror a plan node body or become a plan read fallback.
        """

        clean_project = component(project_id, field="project_id")
        project = self.read_project(clean_project)
        worker = self.read_worker(worker_name)
        if worker.get("pool_status") != "active":
            raise DomainError(
                "field_worker_limbo",
                "A worker in limbo cannot resolve project work.",
                status=403,
            )
        normalized_ref = parse_plan_node_ref(item_ref).ref
        payload = {"item_ref": normalized_ref, "limit": 1}
        clean_outbox_id = (
            component(outbox_id, field="outbox_id")
            if str(outbox_id or "").strip()
            else new_id("outbox")
        )
        row = {
            "schema": OUTBOX_SCHEMA,
            "outbox_id": clean_outbox_id,
            # The service operation ID is unchanged across every transport.
            "kind": "project.plan.index",
            "worker_name": str(worker.get("worker_name") or ""),
            "project_ref": str(project.get("project_ref") or ""),
            "content_hash": content_hash(payload),
            "payload": payload,
            "state": "pending",
            "created_at": utc_now(),
            "retry_count": 0,
            "next_attempt_at": "",
        }
        root = self.control / "outbox"
        with exclusive_lock(root / ".outbox.lock"):
            for state in ("sent", "refused", "leased", "pending"):
                existing = read_json(
                    root / state / f"{clean_outbox_id}.json", required=False
                )
                if not existing:
                    continue
                identity = {
                    "kind": row["kind"],
                    "worker_name": row["worker_name"],
                    "project_ref": row["project_ref"],
                    "content_hash": row["content_hash"],
                    "payload": row["payload"],
                }
                if not _contains_identity(existing, identity):
                    raise DomainError(
                        "field_outbox_identity_conflict",
                        "This outbox identity already describes another request.",
                        status=409,
                        details={"outbox_id": clean_outbox_id},
                    )
                return {**existing, "replayed": True}
            atomic_write_json(root / "pending" / f"{clean_outbox_id}.json", row)
        return {**row, "replayed": False}

    def outbox_record(self, outbox_id: str) -> dict[str, Any] | None:
        clean_id = component(outbox_id, field="outbox_id")
        root = self.control / "outbox"
        with exclusive_lock(root / ".outbox.lock"):
            for state in OUTBOX_FOLDERS:
                path = root / state / f"{clean_id}.json"
                if path.exists():
                    return read_json(path)
        return None

    def worker_outbox_status(self, *, worker_name: str, outbox_id: str) -> dict[str, Any]:
        """Return one worker-owned delivery state without its retained payload."""

        clean_worker = str(self.read_worker(worker_name).get("worker_name") or "")
        row = self.outbox_record(outbox_id)
        if row is None or str(row.get("worker_name") or "") != clean_worker:
            raise DomainError(
                "field_outbox_record_not_found",
                "No outbox record has that id for this worker.",
                status=404,
            )
        result = {
            "schema": "problem-board.worker-outbox-status.v1",
            "outbox_id": str(row.get("outbox_id") or ""),
            "kind": str(row.get("kind") or ""),
            "project_ref": str(row.get("project_ref") or ""),
            "state": str(row.get("state") or ""),
            "created_at": str(row.get("created_at") or ""),
            "updated_at": str(row.get("updated_at") or ""),
            "settled_at": str(row.get("settled_at") or ""),
            "retry_count": int(row.get("retry_count") or 0),
            "next_attempt_at": str(row.get("next_attempt_at") or ""),
            "last_error_code": str(row.get("last_error_code") or ""),
            "remote_disposition": str(row.get("remote_disposition") or ""),
        }
        if str(row.get("kind") or "") == "project.plan.index":
            proof = row.get("remote_result")
            if isinstance(proof, Mapping):
                result["resolution"] = {
                    "item_ref": str(proof.get("item_ref") or ""),
                    "current_work_ref": str(proof.get("current_work_ref") or ""),
                    "found": bool(proof.get("found")),
                    "error_code": str(proof.get("error_code") or ""),
                    "generation_present": bool(proof.get("generation_present")),
                    "plan_revision": int(proof.get("plan_revision") or 0),
                }
        return result

    def list_mail_deliveries(
        self,
        *,
        worker_name: str,
        project_ref: str = "",
        states: Sequence[str] = ("refused",),
        cursor: str = "",
        limit: int = 20,
    ) -> dict[str, Any]:
        """Page one worker's durable outbound mail delivery outcomes."""

        clean_worker = str(self.read_worker(worker_name).get("worker_name") or "")
        clean_project_ref = bounded_text(
            project_ref, field="project_ref", maximum=512
        )
        if clean_project_ref and parse_ref(clean_project_ref).kind != "project":
            raise DomainError(
                "field_project_ref_invalid", "Expected a work:project reference."
            )
        allowed_states = {"pending", "leased", "sent", "ignored", "refused"}
        selected_states = tuple(
            sorted({str(value or "").strip().lower() for value in states})
        )
        if not selected_states or set(selected_states) - allowed_states:
            raise DomainError(
                "field_mail_delivery_state_invalid",
                "Mail delivery state must be pending, leased, sent, ignored, or refused.",
                details={"states": list(selected_states)},
            )

        root = self.control / "outbox"
        rows: list[dict[str, Any]] = []
        with exclusive_lock(root / ".outbox.lock"):
            for folder in OUTBOX_FOLDERS:
                for row in json_records(root / folder):
                    if str(row.get("kind") or "") != "mail.route":
                        continue
                    if str(row.get("worker_name") or "") != clean_worker:
                        continue
                    if clean_project_ref and str(row.get("project_ref") or "") != clean_project_ref:
                        continue
                    if str(row.get("state") or "") not in selected_states:
                        continue
                    rows.append(row)

        rows.sort(
            key=lambda row: (
                str(row.get("created_at") or ""),
                str(row.get("outbox_id") or ""),
            ),
            reverse=True,
        )
        codec = ScopedKeysetCursor(
            scope={"worker_name": clean_worker},
            query={
                "collection": "mail_deliveries",
                "project_ref": clean_project_ref,
                "states": list(selected_states),
            },
            key_fields=("created_at", "outbox_id"),
        )
        boundary: tuple[Any, ...] | None = None
        if str(cursor or "").strip():
            try:
                boundary = codec.decode(
                    bounded_text(cursor, field="cursor", maximum=4000)
                )
            except CollectionError as exc:
                raise DomainError(exc.code, exc.message) from exc
        available = [
            row
            for row in rows
            if boundary is None
            or (
                str(row.get("created_at") or ""),
                str(row.get("outbox_id") or ""),
            )
            < (str(boundary[0]), str(boundary[1]))
        ]
        page_limit = max(1, min(int(limit), 200))
        page = available[:page_limit]
        next_cursor = ""
        if len(available) > page_limit and page:
            next_cursor = codec.encode(
                (
                    str(page[-1].get("created_at") or ""),
                    str(page[-1].get("outbox_id") or ""),
                )
            )
        return {
            "schema": MAIL_DELIVERY_PAGE_SCHEMA,
            "worker_name": clean_worker,
            "project_ref": clean_project_ref,
            "states": list(selected_states),
            "total": len(rows),
            "count": len(page),
            "items": [delivery_summary(row) for row in page],
            "next_cursor": next_cursor,
        }

    def replay_mail_delivery(
        self,
        *,
        outbox_id: str,
        worker_name: str,
        recipient: str,
        kind: str = "",
    ) -> dict[str, Any]:
        """Replay retained mail to an explicit address and optional corrected kind."""

        clean_id = component(outbox_id, field="outbox_id")
        clean_worker = str(self.read_worker(worker_name).get("worker_name") or "")
        root = self.control / "outbox"
        with exclusive_lock(root / ".outbox.lock"):
            source = next(
                (
                    root / folder / f"{clean_id}.json"
                    for folder in OUTBOX_FOLDERS
                    if (root / folder / f"{clean_id}.json").is_file()
                ),
                None,
            )
            if source is None:
                raise DomainError(
                    "field_outbox_not_found",
                    "The requested outbound delivery does not exist.",
                    status=404,
                    details={"outbox_id": clean_id},
                )
            original = read_json(source)
        if str(original.get("kind") or "") != "mail.route":
            raise DomainError(
                "field_mail_delivery_invalid",
                "Only an outbound mail delivery can be replayed.",
                status=409,
            )
        if str(original.get("worker_name") or "") != clean_worker:
            raise DomainError(
                "field_mail_delivery_forbidden",
                "Only the worker that sent this mail may replay it.",
                status=403,
            )
        if str(original.get("state") or "") not in {"refused", "ignored"}:
            raise DomainError(
                "field_mail_delivery_not_replayable",
                "Only refused or ignored mail can be replayed.",
                status=409,
                details={"state": str(original.get("state") or "")},
            )
        payload = (
            dict(original.get("payload") or {})
            if isinstance(original.get("payload"), Mapping)
            else {}
        )
        if not payload:
            raise DomainError(
                "field_mail_delivery_payload_missing",
                "This delivery no longer retains a replayable mail payload.",
                status=409,
            )
        project_ref_value = str(original.get("project_ref") or "")
        project_id = (
            parse_ref(project_ref_value).object_id if project_ref_value else ""
        )
        resolution = self.resolve_mail_recipient(project_id, recipient)
        original_kind = bounded_text(
            payload.get("kind"), field="kind", maximum=128, required=True
        )
        replay_kind = bounded_text(kind, field="kind", maximum=128) or original_kind
        replay_key = f"replay:{clean_id}:{resolution['worker_name']}"
        if replay_kind != original_kind:
            replay_key = f"{replay_key}:{replay_kind}"
        attachments = [
            {
                "path": str(item.get("path") or ""),
                "filename": str(item.get("filename") or ""),
            }
            for item in payload.get("attachment_files") or []
            if isinstance(item, Mapping)
        ]
        common = {
            "project_id": project_id,
            "sender": clean_worker,
            "recipient": str(resolution["worker_name"]),
            "kind": replay_kind,
            "subject": str(payload.get("subject") or ""),
            "body": str(payload.get("body") or ""),
            "payload": (
                dict(payload.get("payload") or {})
                if isinstance(payload.get("payload"), Mapping)
                else {}
            ),
            "work_ref": str(payload.get("work_ref") or ""),
            "correlation_id": str(payload.get("correlation_id") or ""),
            "reply_to": str(payload.get("reply_to") or ""),
            "idempotency_key": replay_key,
        }
        if resolution["route"] == "local":
            if attachments:
                raise DomainError(
                    "field_attachments_operator_only",
                    "A retained attachment can only be replayed to the operator inbox.",
                    status=409,
                )
            replay = self.send_mail(**common)
        else:
            replay = self.enqueue_remote_mail(**common, attachments=attachments)

        with exclusive_lock(root / ".outbox.lock"):
            source = next(
                (
                    root / folder / f"{clean_id}.json"
                    for folder in OUTBOX_FOLDERS
                    if (root / folder / f"{clean_id}.json").is_file()
                ),
                None,
            )
            if source is not None:
                row = read_json(source)
                row.update(
                    replayed_at=row.get("replayed_at") or utc_now(),
                    replayed_to=str(resolution["worker_name"]),
                    replayed_kind=replay_kind,
                    replay_message_ref=str(replay.get("message_ref") or ""),
                    replay_outbox_id=str(replay.get("outbox_id") or ""),
                )
                atomic_write_json(source, row)
        return {
            "schema": "problem-board.local-mail-replay.v1",
            "original_outbox_id": clean_id,
            "original_recipient": str(payload.get("recipient") or ""),
            "recipient": str(resolution["worker_name"]),
            "kind": replay_kind,
            "route": str(resolution["route"]),
            "delivery": replay,
        }

    def pull_outbox(
        self,
        *,
        relay_id: str,
        worker_name: str = "",
        project_ref: str = "",
        limit: int = 20,
        lease_seconds: int = 300,
        kinds: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        root = self.control / "outbox"
        claimed: list[dict[str, Any]] = []
        with exclusive_lock(root / ".outbox.lock"):
            now_dt = datetime.now(timezone.utc)
            for path in sorted((root / "leased").glob("*.json")):
                row = read_json(path)
                lease = row.get("lease") if isinstance(row.get("lease"), Mapping) else {}
                expires_at = str(lease.get("expires_at") or "")
                if not expires_at or parse_utc(expires_at) <= now_dt:
                    row.update(state="pending", updated_at=utc_now())
                    row.pop("lease", None)
                    atomic_write_json(path, row)
                    os.replace(path, root / "pending" / path.name)
            maximum = max(1, min(int(limit), 100))
            pending = [
                (source, read_json(source))
                for source in (root / "pending").glob("*.json")
            ]
            pending.sort(
                key=lambda item: (
                    str(item[1].get("created_at") or ""),
                    str(item[1].get("outbox_id") or item[0].stem),
                )
            )
            for source, candidate in pending:
                if len(claimed) >= maximum:
                    break
                candidate_worker = str(candidate.get("worker_name") or "")
                if worker_name:
                    if candidate_worker and candidate_worker != str(worker_name).lower():
                        continue
                    if (
                        not candidate_worker
                        and (
                            not project_ref
                            or str(candidate.get("project_ref") or "") != project_ref
                        )
                    ):
                        continue
                if kinds is not None and str(candidate.get("kind") or "") not in kinds:
                    continue
                next_attempt_at = str(candidate.get("next_attempt_at") or "")
                if next_attempt_at and parse_utc(next_attempt_at) > now_dt:
                    continue
                destination = root / "leased" / source.name
                try:
                    os.replace(source, destination)
                except FileNotFoundError:
                    continue
                row = candidate
                row.update(
                    state="leased",
                    lease={"relay_id": str(relay_id), "expires_at": _future(lease_seconds)},
                    updated_at=utc_now(),
                )
                atomic_write_json(destination, row)
                claimed.append(row)
        return claimed

    def retry_outbox(
        self,
        outbox_id: str,
        *,
        relay_id: str,
        error_code: str,
        error_summary: str,
    ) -> dict[str, Any]:
        """Return a transiently failed delivery to pending with bounded backoff."""

        clean_id = component(outbox_id, field="outbox_id")
        root = self.control / "outbox"
        source = root / "leased" / f"{clean_id}.json"
        with exclusive_lock(root / ".outbox.lock"):
            row = read_json(source)
            lease = row.get("lease") if isinstance(row.get("lease"), Mapping) else {}
            if str(lease.get("relay_id") or "") != str(relay_id):
                raise DomainError(
                    "field_outbox_lease_mismatch",
                    "Only the current relay may retry this outbox record.",
                    status=409,
                )
            retry_count = int(row.get("retry_count") or 0) + 1
            delay = min(
                OUTBOX_RETRY_BASE_SECONDS * (2 ** min(retry_count - 1, 8)),
                OUTBOX_RETRY_MAX_SECONDS,
            )
            row.update(
                state="pending",
                retry_count=retry_count,
                next_attempt_at=_future(delay),
                last_error_code=bounded_text(
                    error_code, field="error_code", maximum=128
                ),
                last_error_summary=bounded_text(
                    error_summary, field="error_summary", maximum=2000
                ),
                updated_at=utc_now(),
            )
            row.pop("lease", None)
            atomic_write_json(source, row)
            os.replace(source, root / "pending" / source.name)
            return row

    def settle_outbox(
        self,
        outbox_id: str,
        *,
        relay_id: str,
        outcome: str,
        remote_ref: str = "",
        remote_disposition: str = "",
        remote_result: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        clean_id = component(outbox_id, field="outbox_id")
        root = self.control / "outbox"
        source = root / "leased" / f"{clean_id}.json"
        with exclusive_lock(root / ".outbox.lock"):
            row = read_json(source)
            lease = row.get("lease") if isinstance(row.get("lease"), Mapping) else {}
            if str(lease.get("relay_id") or "") != str(relay_id):
                raise DomainError("field_outbox_lease_mismatch", "Only the current relay may settle this outbox record.", status=409)
            row.update(
                state=str(outcome),
                remote_ref=str(remote_ref or ""),
                remote_disposition=str(remote_disposition or outcome),
                settled_at=utc_now(),
            )
            if remote_result is not None:
                row["remote_result"] = dict(remote_result)
            if outcome == "sent":
                row.pop("payload", None)
            atomic_write_json(source, row)
            destination = root / terminal_folder(outcome)
            destination.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.replace(source, destination / source.name)
            if outcome != "sent":
                self._release_outbox_idempotency(row)
            return row

    def _release_outbox_idempotency(self, row: Mapping[str, Any]) -> None:
        """A refused delivery must not look answered to the next attempt.

        The idempotency record is written when the work is queued and says
        delivery_status queued. The outbox later learns the control plane
        refused it, and nothing carried that back, so the record kept claiming
        the request was answered and every retry was rejected as already
        answered with different content.

        That made one report request permanently unanswerable on 2026-09-12:
        the board showed Waiting for the coordinator, the outbox said refused,
        and the author could not publish again however correct the new attempt
        was. Releasing the record on a non-sent outcome is what lets a fixed
        cause actually be retried.
        """

        payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
        project_ref = str(row.get("project_ref") or "")
        if str(row.get("kind") or "") == "mail.route":
            key = str(payload.get("idempotency_key") or "")
            worker_name = str(row.get("worker_name") or payload.get("sender") or "")
            if key and worker_name:
                try:
                    project_id = parse_ref(project_ref).object_id if project_ref else ""
                    self._mail_idempotency_path(
                        project_id, worker_name, key
                    ).unlink(missing_ok=True)
                except DomainError:
                    pass
            return
        if str(row.get("kind") or "") == "assignment.report":
            assignment_ref = str(payload.get("assignment_ref") or "")
            try:
                ownership_version = int(payload.get("ownership_version"))
                project_id = parse_ref(project_ref).object_id
                report_key = content_hash(
                    {
                        "state": str(payload.get("state") or ""),
                        "source_event_ref": str(
                            payload.get("source_event_ref") or ""
                        ),
                    }
                )
                receipt_paths = self._assignment_report_receipt_paths(
                    project_id,
                    assignment_ref=assignment_ref,
                    ownership_version=ownership_version,
                    report_key=report_key,
                )
            except (DomainError, TypeError, ValueError):
                return
            for path in receipt_paths:
                receipt = read_json(path, required=False)
                if (
                    str(receipt.get("outbox_id") or "")
                    == str(row.get("outbox_id") or "")
                ):
                    path.unlink(missing_ok=True)
            return
        for key in ("report_ref", "idempotency_key", "object_ref"):
            value = str(payload.get(key) or row.get(key) or "")
            if not value:
                continue
            try:
                parsed = parse_ref(value)
            except DomainError:
                continue
            for family in ("project-report", "assignment-report"):
                path = (
                    self._project_dir(component(parse_ref(project_ref).object_id))
                    / "idempotency"
                    / family
                    / f"{component(parsed.object_id)}.json"
                )
                if path.exists():
                    path.unlink()
                    return


__all__ = [
    "FIELD_SCHEMA",
    "JOURNAL_SCHEMA",
    "MAIL_SCHEMA",
    "OUTBOX_SCHEMA",
    "SESSION_SCHEMA",
    "SESSION_STATES",
    "SharedFieldStore",
]

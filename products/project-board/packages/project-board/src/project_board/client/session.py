from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from ..contract.errors import DomainError
from ..contract.refs import parse_ref
from .mail_attachments import worker_message_with_attachments
from .mail_budget import MAX_WORKER_INPUT_BYTES, MailPullBudget
from .quarantine import quarantine_summary
from .store import SharedFieldStore


WORKER_INPUT_SCHEMA = "problem-board.worker-input.v2"
WORKER_WATCH_SCHEMA = "problem-board.worker-watch.v1"
LOGGER = logging.getLogger(__name__)
ACTIVE_ASSIGNMENT_STATES = frozenset({"assigned", "working", "blocked"})
# check_in records bounded message/control refs and may append one bounded wake
# acknowledgement after the pre-lease size decision. Reserve that final state
# growth so the complete pretty-printed CLI response remains under the cap.
WORKER_INPUT_FINALIZATION_RESERVE_BYTES = 16 * 1024


def _worker_input_wire_bytes(value: Mapping[str, Any]) -> int:
    """Bytes emitted by ``pb worker receive``, including its CLI envelope."""

    return len(
        (
            json.dumps(
                {"ok": True, "result": dict(value)},
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    )


# Every receive echoes the session view. Its message-ref lists grow to 100
# refs each (MAX_WAKE_MESSAGE_REFS), and on 2026-09-21 they made up 27 KB of a
# 37 KB empty receive, so any mail over about 25 KB could never be delivered.
# The echo keeps a count and the newest few; the full lists stay in inspect.
SESSION_ECHO_REFS = 5
_SESSION_REF_LISTS = frozenset(
    {
        "last_message_refs",
        "last_control_refs",
        "wake_queued_submission_ids",
        "last_wake_message_refs",
        "wake_observed_message_refs",
        "last_acknowledged_wake_message_refs",
    }
)


def _echo_ref_list(target: dict[str, Any], name: str, value: Any) -> None:
    if name in _SESSION_REF_LISTS and isinstance(value, (list, tuple)):
        target[name] = list(value)[-SESSION_ECHO_REFS:]
        target[f"{name}_count"] = len(value)
    else:
        target[name] = value


def _worker_input_session_view(session: Mapping[str, Any]) -> dict[str, Any]:
    """Keep receive evidence while leaving full listener diagnostics to inspect."""

    fields = (
        "session_id",
        "state",
        "presence",
        "check_interval_seconds",
        "heartbeat_at",
        "heartbeat_age_seconds",
        "stale_after_seconds",
        "attached_at",
        "detached_at",
        "last_inbox_check_at",
        "inbox_check_state",
        "inbox_check_age_seconds",
        "inbox_check_interval_seconds",
        "inbox_overdue_by_seconds",
        "last_inbox_result_at",
        "last_mail_settled_at",
        "last_settled_message_ref",
        "last_message_refs",
        "last_control_refs",
        "observed_control_plane_state",
        "revision",
    )
    view: dict[str, Any] = {}
    for field in fields:
        if field in session:
            _echo_ref_list(view, field, session[field])
    raw_subscription = (
        session.get("subscription")
        if isinstance(session.get("subscription"), Mapping)
        else {}
    )
    subscription_fields = (
        "adapter",
        "state",
        "outstanding_wake_id",
        "wake_delivery_state",
        "wake_attempts",
        "wake_first_attempt_at",
        "wake_last_attempt_at",
        "wake_provenance",
        "wake_queued_submission_ids",
        "last_wake_message_refs",
        "wake_observed_message_refs",
        "last_acknowledged_wake_id",
        "last_acknowledged_wake_message_refs",
        "last_wake_acknowledged_at",
        "last_wake_receipt",
        "queue_reconciliation_required",
        "last_queue_reconciliation",
    )
    subscription: dict[str, Any] = {}
    for field in subscription_fields:
        if field in raw_subscription:
            _echo_ref_list(subscription, field, raw_subscription[field])
    history = [
        dict(item)
        for item in raw_subscription.get("wake_acknowledgements") or []
        if isinstance(item, Mapping)
    ]
    if history:
        subscription["wake_acknowledgements"] = history[-1:]
    view["subscription"] = subscription
    return view


# The receive envelope must stay inside RECEIVE_ENVELOPE_RESERVE_BYTES whatever
# the worker attends. Projects with leased mail are always listed, since their
# counts describe this receive, then others up to the cap. Counts are exact.
RECEIVE_ECHO_PROJECTS = 20
RECEIVE_ECHO_ASSIGNMENTS = 20


def _bounded_projects(projects: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    active = [dict(row) for row in projects if int(row.get("leased_messages") or 0)]
    idle = [dict(row) for row in projects if not int(row.get("leased_messages") or 0)]
    return (active + idle)[: max(RECEIVE_ECHO_PROJECTS, len(active))]


ISSUE_VIEW_MAX_BYTES = 2048


def _issue_view(issue: Mapping[str, Any]) -> dict[str, Any]:
    """A bounded view of one delivery issue for the receive response.

    Error details are arbitrary, so they are kept only while small. The full
    issue, reports included, stays on the message's retry or quarantine row.
    """

    error = issue.get("error") if isinstance(issue.get("error"), Mapping) else {}
    details = error.get("details") if isinstance(error.get("details"), Mapping) else {}
    encoded = json.dumps(details, ensure_ascii=True, sort_keys=True, default=str)
    view = {
        "message_ref": str(issue.get("message_ref") or "")[:256],
        "project_ref": str(issue.get("project_ref") or "")[:256],
        "sender": str(issue.get("sender") or "")[:160],
        "subject": str(issue.get("subject") or "")[:200],
        "state": str(issue.get("state") or "")[:64],
        "attempts": int(issue.get("attempts") or 0),
        "error": {
            "code": str(error.get("code") or "")[:128],
            "message": str(error.get("message") or "")[:400],
            "details": (
                json.loads(encoded)
                if len(encoded) <= 400
                else {"truncated": True, "bytes": len(encoded)}
            ),
        },
    }
    for name in ("report", "quarantine_report"):
        report = issue.get(name)
        if isinstance(report, Mapping):
            view[name] = {
                "delivery_status": str(report.get("delivery_status") or "")[:64],
                "outbox_id": str(report.get("outbox_id") or "")[:128],
            }
    # Characters are not bytes: escaped non-ASCII text can grow twelvefold.
    # Shorten the free-text fields until the encoded view fits its reserve.
    while _issue_view_bytes(view) > ISSUE_VIEW_MAX_BYTES:
        view["subject"] = view["subject"][: len(view["subject"]) // 2]
        view["sender"] = view["sender"][: len(view["sender"]) // 2]
        view["error"]["message"] = view["error"]["message"][: len(view["error"]["message"]) // 2]
        if not (view["subject"] or view["sender"] or view["error"]["message"]):
            break
    if _issue_view_bytes(view) > ISSUE_VIEW_MAX_BYTES:
        # The fixed fields alone are over the reserve: keep only what names the
        # message and the failure. The full issue stays on its row.
        view = {
            "message_ref": view["message_ref"][:160],
            "state": view["state"][:32],
            "error": {"code": view["error"]["code"][:64]},
            "truncated": True,
        }
    return view


def _issue_view_bytes(view: Mapping[str, Any]) -> int:
    return len(json.dumps(view, ensure_ascii=True, indent=2, sort_keys=True).encode("utf-8"))


def _undeliverable_notice_recipient(entry: Mapping[str, Any]) -> str:
    """Operator-originated mail is answered to ``operator``, as replies are."""

    sender = str(entry.get("sender") or "").strip()
    if sender in {"", "control-plane", "operator"} or str(entry.get("sender_kind") or "") == "user":
        return "operator"
    return sender


def _notify_stub_sender(
    field: Any, *, receiver: str, project_id: str, message: dict[str, Any]
) -> None:
    """Tell the sender of a message this receive carried only as a stub.

    One idempotent local write per stubbed message: ``send_mail`` for a local
    sender, ``enqueue_remote_mail`` for a remote one or the operator. A crash
    before it leaves the lease to expire, the message is stubbed again, and
    the notice is written again under the same idempotency key. The outcome is
    shown on the item and kept on the leased row. Never raises: the receive
    itself goes on.
    """

    marker = message.get("skipped_by_receive")
    if not isinstance(marker, dict):
        return
    entry = {
        "message_ref": str(message.get("message_ref") or ""),
        "sender": str(message.get("sender") or ""),
        "sender_kind": str(
            (message.get("sender_identity") or {}).get("kind") or ""
            if isinstance(message.get("sender_identity"), Mapping)
            else ""
        ),
        "subject": str(message.get("subject") or ""),
        "correlation_id": str(message.get("correlation_id") or ""),
        "created_at": str(message.get("created_at") or ""),
        **marker,
    }
    try:
        _send_undeliverable_notice(field, receiver=receiver, project_id=project_id, entry=entry)
        outcome = "sent"
    except Exception as exc:  # noqa: BLE001 - the receive already has the stub
        outcome = f"failed: {str(getattr(exc, 'code', '') or type(exc).__name__)[:48]}"
        LOGGER.warning(
            "[problem-board.receive] could not notify sender of an undeliverable mail "
            "ref=%s sender=%s error=%s",
            entry["message_ref"],
            entry["sender"],
            outcome,
        )
    marker["sender_notice"] = outcome
    try:
        field.record_stub_notice(
            project_id,
            worker_name=receiver,
            message_ref=entry["message_ref"],
            outcome=outcome,
        )
    except Exception:  # noqa: BLE001
        LOGGER.warning(
            "[problem-board.receive] could not record a stub notice ref=%s",
            entry["message_ref"],
            exc_info=True,
        )


def _send_undeliverable_notice(
    field: Any, *, receiver: str, project_id: str, entry: Mapping[str, Any]
) -> None:
    """Reply to the sender of a message no receive could carry, over the same
    local or remote route ``pb worker send`` uses. Idempotent per message."""

    message_ref = str(entry.get("message_ref") or "")
    lines = [
        f"Your message {message_ref} was not delivered to {receiver} and was not processed.",
        "",
        f"Subject: {entry.get('subject') or ''}",
        f"Sent at: {entry.get('created_at') or ''}",
        f"Reason: {entry.get('reason') or ''}. {entry.get('explanation') or ''}",
        f"Message size: {entry.get('message_bytes')} bytes as a receive item, "
        f"of which the body is {entry.get('body_bytes')} bytes. "
        f"The most one message can carry is {entry.get('maximum_message_bytes')} bytes.",
        "",
        "The receiver was given a short notice of it with its first bytes, not the "
        "message itself, so the full message has not been processed. Please rework "
        "it and send it again.",
    ]
    resolution = field.resolve_mail_recipient(
        project_id, _undeliverable_notice_recipient(entry)
    )
    mail = {
        "sender": receiver,
        "recipient": str(resolution["worker_name"]),
        "kind": "reply",
        "subject": f"Not delivered: {entry.get('subject') or message_ref}"[:200],
        "body": "\n".join(lines),
        "correlation_id": str(entry.get("correlation_id") or ""),
        "reply_to": message_ref,
        "idempotency_key": f"undeliverable-{parse_ref(message_ref).object_id}",
    }
    if str(resolution["route"]) == "local":
        field.send_mail(project_id, **mail)
    else:
        field.enqueue_remote_mail(project_id, **mail)

def _operator_response_contract(message: Mapping[str, Any]) -> dict[str, Any] | None:
    sender = (
        dict(message.get("sender_identity") or {})
        if isinstance(message.get("sender_identity"), Mapping)
        else {}
    )
    if str(sender.get("kind") or "") != "user" or str(message.get("kind") or "") not in {
        "request",
        "reply",
    }:
        return None
    payload = (
        dict(message.get("payload") or {})
        if isinstance(message.get("payload"), Mapping)
        else {}
    )
    return {
        "required_before_settlement": True,
        "recipient": "operator",
        "kind": "reply",
        "correlation_id": str(
            message.get("correlation_id") or payload.get("command_ref") or ""
        ),
        "reply_to": str(message.get("message_ref") or ""),
        "instruction": (
            "Send the operator a visible correlated reply before settling this "
            "conversation message. Settlement records handling; it does not create "
            "a chat turn."
        ),
    }


def _worker_view(worker: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "worker_name": worker.get("worker_name"),
        "worker_alias": worker.get("worker_alias"),
        "worker_identity": worker.get("worker_identity"),
        "runtime_kind": worker.get("runtime_kind"),
        "runtime_session_id": worker.get("runtime_session_id"),
        "capabilities": list(worker.get("capabilities") or []),
        "host_id": worker.get("host_id"),
        "host_label": worker.get("host_label"),
        "host_kind": worker.get("host_kind"),
        "relay_id": worker.get("relay_id"),
        "pool_status": worker.get("pool_status"),
        # Derived from evidence rather than asserted at enrollment, so no surface
        # shows a deaf worker as available.
        "reachability": worker.get("reachability"),
        "control_plane": {
            "state": worker.get("control_plane_state") or "local_only",
            "connected_at": worker.get("control_plane_connected_at") or "",
        },
        "authorization": dict(worker.get("authorization") or {}),
    }


def listen_worker_input(
    field: SharedFieldStore,
    *,
    worker_name: str,
    check_interval_seconds: int = 30,
) -> dict[str, Any]:
    worker = field.read_worker(worker_name)
    session = field.listen_worker(
        str(worker.get("worker_name") or ""),
        check_interval_seconds=check_interval_seconds,
    )
    subscription = dict(session.get("subscription") or {})
    adapter = str(subscription.get("adapter") or "session-owned-watch")
    return {
        "schema": WORKER_INPUT_SCHEMA,
        "worker": _worker_view(worker),
        "session": session,
        "attended_project_refs": list(worker.get("attended_project_refs") or []),
        "monitor": {
            "owner": "login-relay" if adapter == "codex-queue" else "this-agent-session",
            "adapter": adapter,
            "command": (
                "codex queue -> worker receive"
                if adapter == "codex-queue"
                else "one session-owned background attachment -> pb worker watch"
            ),
            "interval_seconds": session["check_interval_seconds"],
            "heartbeat_evidence": "last_inbox_check_at",
            "rule": (
                "The host login relay queues a standard inbox-check instruction into this "
                "exact Codex session. Only that native queue can create a Codex model "
                "turn; pb worker watch in a background terminal is diagnostic only. The "
                "model pulls and settles local mail with pb worker receive."
                if adapter == "codex-queue"
                else "Use this runtime's session-owned background event facility to run "
                "exactly one pb worker watch process. It emits availability only and "
                "never leases or reveals mail. On a wake, this model calls pb "
                "worker receive, handles and settles every returned lease, then leaves "
                "the watch running. Terminate it before worker detach."
            ),
        },
    }


def probe_worker_input(
    field: SharedFieldStore,
    *,
    worker_name: str,
) -> dict[str, Any]:
    """Check for local availability without leasing mail or exposing its body."""

    worker = field.read_worker(worker_name)
    stable_name = str(worker.get("worker_name") or "")
    listener = field.worker_listener_session(stable_name)
    if listener is None or listener.get("state") == "detached":
        raise DomainError(
            "field_worker_not_listening",
            "This coding-agent session must run worker listen before watching its inbox.",
            status=409,
        )
    control_plane_state = str(worker.get("control_plane_state") or "local_only")
    observed_state = str(
        listener.get("observed_control_plane_state") or "local_only"
    )
    signals = []
    if control_plane_state != observed_state:
        signals.append(
            {
                "kind": (
                    "control_plane.connected"
                    if control_plane_state == "published"
                    else "control_plane.state_changed"
                ),
                "previous_state": observed_state,
                "state": control_plane_state,
            }
        )
    pending_refs = field.pending_worker_mail_refs(stable_name)
    lease_owner = str(worker.get("runtime_session_id") or stable_name)
    active_leases = field.list_worker_mail_leases(
        stable_name,
        lease_owner=lease_owner,
        limit=20,
    )
    held_refs = [
        str(item.get("message_ref") or "")
        for item in active_leases.get("items") or []
        if item.get("message_ref")
    ]
    listener = field.record_worker_inbox_probe(
        stable_name,
        observed_control_plane_state=control_plane_state,
    )
    return {
        "schema": WORKER_WATCH_SCHEMA,
        "worker": _worker_view(field.read_worker(stable_name)),
        "session": listener,
        "pending_count": len(pending_refs),
        "pending_refs": pending_refs,
        "held_lease_count": int(active_leases.get("total") or 0),
        "held_lease_refs": held_refs,
        "held_lease_next_cursor": str(active_leases.get("next_cursor") or ""),
        "work_count": len(pending_refs) + int(active_leases.get("total") or 0),
        "signals": signals,
        "instruction": (
            "Run pb worker receive in this exact session, then handle and settle "
            "every returned lease. Run pb worker leases for work this session "
            "already holds."
            if pending_refs
            else (
                "Run pb worker leases in this exact session to enumerate and "
                "recover the work it already holds."
                if active_leases.get("total")
                else ""
            )
        ),
    }


def pull_worker_input(
    field: SharedFieldStore,
    *,
    worker_name: str,
    limit: int = 5,
    lease_seconds: int = 1800,
    wake_id: str = "",
) -> dict[str, Any]:
    """Read the worker's direct mailbox, then every attended project shard."""

    worker = field.read_worker(worker_name)
    stable_name = str(worker.get("worker_name") or "")
    listener = field.worker_listener_session(stable_name)
    if listener is None or listener.get("state") == "detached":
        raise DomainError(
            "field_worker_not_listening",
            "This coding-agent session must run worker listen before receiving mail.",
            status=409,
        )
    lease_owner = str(worker.get("runtime_session_id") or stable_name)
    control_plane_state = str(
        worker.get("control_plane_state") or "local_only"
    )
    observed_control_plane_state = str(
        listener.get("observed_control_plane_state") or "local_only"
    )
    signals = []
    if control_plane_state != observed_control_plane_state:
        signals.append(
            {
                "kind": "control_plane.connected"
                if control_plane_state == "published"
                else "control_plane.state_changed",
                "previous_state": observed_control_plane_state,
                "state": control_plane_state,
            }
        )
    project_refs = list(worker.get("attended_project_refs") or [])
    observed_projects = listener.get("observed_project_refs")
    if isinstance(observed_projects, list):
        for dropped in sorted(set(observed_projects) - set(project_refs)):
            # Unlinked (or removed) since the last receive: the agent is told,
            # not left to notice a missing project line (rehearsal gap 7).
            signals.append(
                {
                    "kind": "project.attendance_ended",
                    "project_ref": dropped,
                    "message": (
                        f"This agent no longer attends {dropped}: its mail and work are "
                        "no longer yours. Stop work there and ask your owner or the "
                        "coordinator before doing anything more for it."
                    ),
                }
            )
    remaining = max(1, min(int(limit), 100))
    items: list[dict[str, Any]] = []
    projects: list[dict[str, Any]] = []
    project_scopes: list[tuple[str, str]] = []
    assignment_refs: list[str] = []
    claimed: list[dict[str, str]] = []
    delivery_issues: list[dict[str, Any]] = []
    worker_view = _worker_view(worker)
    budget = MailPullBudget()
    quarantine = quarantine_summary(field, stable_name)
    active_before = field.list_worker_mail_leases(
        stable_name,
        lease_owner=lease_owner,
        limit=1,
    )
    already_held_count = int(active_before.get("total") or 0)

    projects_not_on_host: list[dict[str, Any]] = []
    for project_ref in project_refs:
        parsed = parse_ref(str(project_ref))
        if parsed.kind != "project":
            continue
        if not field._project_path(parsed.object_id).exists():
            # Attended on the board, not yet written on this host (W304
            # finding 39): the relay writes the record on its next poll of
            # the project, and the project's mail waits on the board until
            # then. One such project never stops the rest of the receive. It
            # is listed after the scoped projects, whose positions the lease
            # loop below uses as indexes.
            projects_not_on_host.append(
                {
                    "project_ref": str(project_ref),
                    "project_id": parsed.object_id,
                    "revision": 0,
                    "leased_messages": 0,
                    "state": "not_on_this_host",
                    "note": (
                        "The relay writes this project's record on its next poll; "
                        "its mail waits on the board until then."
                    ),
                }
            )
            continue
        project_scopes.append((str(project_ref), parsed.object_id))
        projects.append(
            {
                "project_ref": str(project_ref),
                "project_id": parsed.object_id,
                "revision": int(
                    field.read_project(parsed.object_id).get("revision") or 0
                ),
                "leased_messages": 0,
            }
        )
        assignment_refs.extend(
            str(row.get("assignment_ref") or "")
            for row in field.list_assignments(parsed.object_id)
            if str(row.get("worker_name") or "").lower() == stable_name.lower()
            and str(row.get("state") or "") in ACTIVE_ASSIGNMENT_STATES
            and str(row.get("assignment_ref") or "")
        )
    assignment_refs = list(dict.fromkeys(assignment_refs))
    projects.extend(projects_not_on_host)

    def report_delivery_failure(
        project_id: str,
        message: Mapping[str, Any],
        *,
        error_code: str,
        error_message: str,
        error_details: Mapping[str, Any] | None = None,
        field_name: str = "",
        field_value: Any = "",
    ) -> dict[str, Any]:
        try:
            return field.report_mail_delivery_failure(
                project_id,
                receiver_worker_name=stable_name,
                message=message,
                error_code=error_code,
                error_message=error_message,
                error_details=error_details,
                field_name=field_name,
                field_value=field_value,
            )
        except Exception as exc:
            # Reporting is a second transport path. Its failure must be visible,
            # but it cannot turn the original receiver off.
            LOGGER.exception(
                "Problem Board could not report malformed mail message=%s sender=%s",
                message.get("message_ref"),
                message.get("sender"),
            )
            return {
                "delivery_route": "",
                "delivery_status": "failed",
                "message_ref": "",
                "recipient": str(message.get("sender") or ""),
                "replayed": False,
                "error": f"{type(exc).__name__}: {str(exc)[:800]}",
            }

    def remember_claim(project_id: str, message: Mapping[str, Any]) -> dict[str, str]:
        lease = (
            dict(message.get("lease") or {})
            if isinstance(message.get("lease"), Mapping)
            else {}
        )
        claim = {
            "project_id": project_id,
            "message_ref": str(message.get("message_ref") or ""),
            "lease_id": str(lease.get("lease_id") or ""),
        }
        claimed.append(claim)
        return claim

    def isolate_message_failure(
        project_id: str,
        message: Mapping[str, Any],
        claim: dict[str, str],
        exc: DomainError,
    ) -> None:
        issue = field.record_mail_receive_failure(
            project_id,
            worker_name=stable_name,
            message_ref=claim["message_ref"],
            lease_id=claim["lease_id"],
            lease_owner=lease_owner,
            error_code=exc.code,
            error_message=str(exc),
            error_details=exc.details,
        )
        if not issue.get("report") and str(message.get("kind") or "") != "delivery_failed":
            report = report_delivery_failure(
                project_id,
                message,
                error_code=exc.code,
                error_message=str(exc),
                error_details=exc.details,
            )
            issue["report"] = report
            if report.get("delivery_status") != "failed":
                try:
                    field.record_mail_receive_failure_report(
                        project_id,
                        worker_name=stable_name,
                        message_ref=claim["message_ref"],
                        report=report,
                    )
                except Exception:
                    LOGGER.exception(
                        "Problem Board could not retain failure-report evidence "
                        "message=%s",
                        claim["message_ref"],
                    )
        if issue["state"] == "quarantined":
            quarantine["count"] += 1
            if str(message.get("kind") or "") != "delivery_failed":
                report = report_delivery_failure(
                    project_id,
                    message,
                    error_code="field_mail_quarantined",
                    error_message=(
                        f"Message was held after {issue['attempts']} failed receive "
                        f"attempts: {str(exc)[:1500]}"
                    ),
                    error_details={
                        "field": "message_ref",
                        "value": claim["message_ref"],
                        "receive_error_code": exc.code,
                    },
                    field_name="message_ref",
                    field_value=claim["message_ref"],
                )
                issue["quarantine_report"] = report
                if report.get("delivery_status") != "failed":
                    try:
                        field.record_mail_receive_failure_report(
                            project_id,
                            worker_name=stable_name,
                            message_ref=claim["message_ref"],
                            report=report,
                            report_field="quarantine_report",
                        )
                    except Exception:
                        LOGGER.exception(
                            "Problem Board could not retain quarantine-report evidence "
                            "message=%s",
                            claim["message_ref"],
                        )
        claimed.remove(claim)
        # The full issue stays durable on the retry or quarantine row. The
        # response carries a bounded view, whose room the claim-time budget
        # already reserved, so an isolation cannot push it over its limit.
        delivery_issues.append(_issue_view(issue))

    def received_item(
        *, project_ref: str, project_id: str, message: Mapping[str, Any]
    ) -> dict[str, Any]:
        projected_message = worker_message_with_attachments(
            message,
            project_ref=project_ref,
            attachment_root=(
                field._mail_root(project_id, stable_name) / "attachments"
            ),
            runtime_kind=str(worker.get("runtime_kind") or ""),
            runtime_session_id=str(worker.get("runtime_session_id") or ""),
        )
        item: dict[str, Any] = {
            "scope": "project" if project_ref else "direct",
            "project_ref": project_ref,
            "project_id": project_id,
            "message": projected_message,
        }
        response = _operator_response_contract(projected_message)
        if response is not None:
            item["operator_response"] = response
        return item

    def acquired_lease_manifest(
        response_items: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        manifest: list[dict[str, Any]] = []
        for item in response_items:
            message = (
                item.get("message")
                if isinstance(item.get("message"), Mapping)
                else {}
            )
            lease = (
                message.get("lease")
                if isinstance(message.get("lease"), Mapping)
                else {}
            )
            message_ref = str(message.get("message_ref") or "")
            lease_id = str(lease.get("lease_id") or "")
            if not message_ref or not lease_id:
                continue
            manifest.append(
                {
                    "project_ref": str(item.get("project_ref") or ""),
                    "project_id": str(item.get("project_id") or ""),
                    "message_ref": message_ref,
                    "lease_id": lease_id,
                    "lease_ref": str(lease.get("lease_ref") or ""),
                    "expires_at": str(lease.get("expires_at") or ""),
                }
            )
        return manifest

    def delivery_view(
        *,
        item_count: int,
        remaining_count: int,
        limited_by: str,
        held_lease_count: int,
    ) -> dict[str, Any]:
        return {
            "item_count": int(item_count),
            "held_lease_count": int(held_lease_count),
            "remaining_count": int(remaining_count),
            "has_more": bool(remaining_count),
            "limited_by": str(limited_by),
            "maximum_bytes": MAX_WORKER_INPUT_BYTES,
            "deferred_message_ref": budget.deferred_message_ref,
            "required_response_bytes": budget.required_response_bytes,
        }

    def response_document(
        *,
        response_items: list[dict[str, Any]],
        response_projects: list[dict[str, Any]],
        response_session: Mapping[str, Any],
        remaining_count: int = 0,
        limited_by: str = "",
    ) -> dict[str, Any]:
        acquired = acquired_lease_manifest(response_items)
        total_held_count = already_held_count + len(acquired)
        delivery = delivery_view(
            item_count=len(response_items),
            remaining_count=remaining_count,
            limited_by=limited_by,
            held_lease_count=total_held_count,
        )
        if wake_id:
            subscription = response_session.get("subscription") or {}
            receipt = (
                subscription.get("last_wake_receipt") or {}
                if isinstance(subscription, Mapping)
                else {}
            )
            state = (
                str(receipt.get("state") or "unrecorded")
                if isinstance(receipt, Mapping)
                and str(receipt.get("wake_id") or "") == wake_id
                else "unrecorded"
            )
            delivery["wake"] = {
                "id": wake_id,
                "state": state,
                "expected_id": str(receipt.get("expected_wake_id") or "")
                if isinstance(receipt, Mapping) and state == "stale"
                else "",
                "stale_or_duplicate": state in {"stale", "already_acknowledged"},
            }
        return {
            "acquired_leases": acquired,
            "active_leases": {
                "already_held_count": already_held_count,
                "acquired_now_count": len(acquired),
                "total_held_count": total_held_count,
                "enumerate_command": ["pb", "worker", "leases"],
                "read_command": [
                    "pb",
                    "worker",
                    "lease-read",
                    "--message-ref",
                    "<message-ref>",
                    "--lease-id",
                    "<lease-id>",
                ],
            },
            "schema": WORKER_INPUT_SCHEMA,
            "worker": worker_view,
            "session": _worker_input_session_view(response_session),
            "projects": _bounded_projects(response_projects),
            "projects_count": len(response_projects),
            "assignments": assignment_refs[:RECEIVE_ECHO_ASSIGNMENTS],
            "assignments_count": len(assignment_refs),
            "items": response_items,
            "delivery_issues": delivery_issues,
            "quarantine": dict(quarantine),
            "signals": signals,
            "delivery": delivery,
            "settlement": {
                "required": True,
                "lease_owner": lease_owner,
                "outcomes": ["acknowledged", "refused"],
            },
        }

    def measured_item(
        *, project_ref: str, project_id: str, message: Mapping[str, Any]
    ) -> dict[str, Any]:
        try:
            return received_item(
                project_ref=project_ref,
                project_id=project_id,
                message=message,
            )
        except DomainError:
            # Message validation belongs to the established isolation path
            # after leasing. Size the full raw envelope here so a malformed
            # message cannot abort or strand the whole batch.
            return {
                "scope": "project" if project_ref else "direct",
                "project_ref": project_ref,
                "project_id": project_id,
                "message": dict(message),
            }

    def mailbox_message_size(*, project_ref: str, project_id: str):
        """Bytes one message adds as a receive item, independent of the envelope."""

        def measure(message: Mapping[str, Any]) -> int:
            return len(
                json.dumps(
                    measured_item(
                        project_ref=project_ref, project_id=project_id, message=message
                    ),
                    ensure_ascii=True,
                    indent=2,
                    sort_keys=True,
                ).encode("utf-8")
            )

        return measure

    def mailbox_response_size(
        *, project_ref: str, project_id: str, project_index: int | None
    ):
        def measure(messages: Sequence[Mapping[str, Any]]) -> int:
            measured_items = [
                measured_item(
                    project_ref=project_ref, project_id=project_id, message=message
                )
                for message in messages
            ]
            prospective_items = [
                *items,
                *measured_items,
            ]
            # Room for each claimed message to become a bounded delivery issue
            # instead, should it fail isolation after being claimed.
            issue_reserve = sum(
                max(
                    0,
                    ISSUE_VIEW_MAX_BYTES
                    - len(json.dumps(item, ensure_ascii=True, indent=2, sort_keys=True)),
                )
                for item in measured_items
            )
            prospective_projects = [dict(row) for row in projects]
            if project_index is not None:
                prospective_projects[project_index]["leased_messages"] = len(messages)
            return (
                _worker_input_wire_bytes(
                    response_document(
                        response_items=prospective_items,
                        response_projects=prospective_projects,
                        response_session=listener,
                    )
                )
                + WORKER_INPUT_FINALIZATION_RESERVE_BYTES
                + issue_reserve
            )

        return measure

    try:
        direct_messages = field.pull_mail(
            "",
            worker_name=stable_name,
            lease_owner=lease_owner,
            limit=remaining,
            lease_seconds=lease_seconds,
            byte_budget=budget,
            measure_response=mailbox_response_size(
                project_ref="",
                project_id="",
                project_index=None,
            ),
            measure_message=mailbox_message_size(project_ref="", project_id=""),
        )
        for message in direct_messages:
            claim = remember_claim("", message)
            _notify_stub_sender(field, receiver=stable_name, project_id="", message=message)
            try:
                items.append(
                    received_item(project_ref="", project_id="", message=message)
                )
            except DomainError as exc:
                isolate_message_failure("", message, claim, exc)
        remaining -= len(direct_messages)
        for project_index, (project_ref, project_id) in enumerate(project_scopes):
            before_items = len(items)
            messages = field.pull_mail(
                project_id,
                worker_name=stable_name,
                lease_owner=lease_owner,
                limit=remaining,
                lease_seconds=lease_seconds,
                byte_budget=budget,
                measure_response=mailbox_response_size(
                    project_ref=project_ref,
                    project_id=project_id,
                    project_index=project_index,
                ),
                measure_message=mailbox_message_size(
                    project_ref=project_ref, project_id=project_id
                ),
            )
            for message in messages:
                claim = remember_claim(project_id, message)
                _notify_stub_sender(
                    field, receiver=stable_name, project_id=project_id, message=message
                )
                try:
                    items.append(
                        received_item(
                            project_ref=project_ref,
                            project_id=project_id,
                            message=message,
                        )
                    )
                except DomainError as exc:
                    isolate_message_failure(project_id, message, claim, exc)
            projects[project_index]["leased_messages"] = len(items) - before_items
            remaining -= len(messages)
        message_refs = [
            str(item["message"].get("message_ref") or "") for item in items
        ]
        message_refs.extend(
            str(issue.get("message_ref") or "")
            for issue in delivery_issues
            if issue.get("message_ref")
        )
        control_refs = [
            str(payload.get("command_ref") or "")
            for item in items
            for payload in [
                item["message"].get("payload")
                if isinstance(item["message"].get("payload"), Mapping)
                else {}
            ]
            if payload.get("command_ref")
        ]
        # Complete every read and transformation before acknowledging the
        # wake. The response contains messages and revision markers only; all
        # larger project state is read explicitly when the task needs it.
        # Inbox freshness and work state answer different questions. An empty
        # receive proves that the session checked its inbox; it does not mean
        # an active edit stopped or a reported blocker disappeared.
        received_work = bool(items or delivery_issues)
        next_state = (
            "working"
            if received_work
            else str(listener.get("state") or "waiting")
        )
        listener = field.check_in_worker_listener(
            stable_name,
            state=next_state,
            inbox_checked=True,
            message_refs=message_refs,
            control_refs=control_refs,
            observed_control_plane_state=control_plane_state,
            wake_id=wake_id,
            observed_project_refs=project_refs,
        )
        result = response_document(
            response_items=items,
            response_projects=projects,
            response_session=listener,
            remaining_count=budget.remaining_messages,
            limited_by=budget.limited_by,
        )
        payload_bytes = _worker_input_wire_bytes(result)
        result["delivery"]["payload_bytes"] = payload_bytes
        payload_bytes = _worker_input_wire_bytes(result)
        result["delivery"]["payload_bytes"] = payload_bytes
        payload_bytes = _worker_input_wire_bytes(result)
        if payload_bytes > MAX_WORKER_INPUT_BYTES:
            raise DomainError(
                "field_worker_input_too_large",
                "The complete worker receive response exceeds its local protocol limit.",
                status=500,
                details={
                    "maximum_bytes": MAX_WORKER_INPUT_BYTES,
                    "payload_bytes": payload_bytes,
                    "leased_messages": len(items),
                },
            )
        return result
    except Exception as exc:
        rollback_errors: list[dict[str, str]] = []
        for claim in reversed(claimed):
            try:
                field.rollback_mail_lease(
                    claim["project_id"],
                    worker_name=stable_name,
                    message_ref=claim["message_ref"],
                    lease_id=claim["lease_id"],
                    lease_owner=lease_owner,
                    reason=f"{type(exc).__name__}: {str(exc)[:800]}",
                )
            except Exception as rollback_exc:
                rollback_errors.append(
                    {
                        "message_ref": claim["message_ref"],
                        "error": f"{type(rollback_exc).__name__}: {rollback_exc}",
                    }
                )
        if rollback_errors:
            raise DomainError(
                "field_mail_receive_rollback_failed",
                "The receive failed and one or more provisional mail leases could not be returned.",
                status=500,
                details={"rollback_errors": rollback_errors},
            ) from exc
        raise


__all__ = [
    "WORKER_INPUT_SCHEMA",
    "listen_worker_input",
    "probe_worker_input",
    "pull_worker_input",
]

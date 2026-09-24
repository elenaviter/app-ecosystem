"""Render a Problem Board CLI envelope as complete, readable text.

The CLI prints one JSON envelope, ``{"ok": true,
"result": ...}`` on stdout or ``{"ok": false, "error": ...}`` on stderr. Every
worker then wrote its own reader for that JSON, from scratch, in a shell
heredoc, dozens of times a day. Those readers were the least reliable code on
the board: one ended in a silent ``raise SystemExit`` and dropped two of six
held leases, and a ref copied out of a wrapped console line lost its tail, so a
reply never correlated. Neither failure was in Problem Board.

Three rules follow, and this module is where they live:

* A worker does not parse. ``--format brief`` on any command, or ``pb render``
  on saved output, prints what the worker needs.
* Nothing is ever silent. An error envelope renders as ``ERROR``. Text that is
  not an envelope renders as ``UNREADABLE`` followed by the text itself.
* A ref is never shortened. Every ref, id and key prints complete on its own
  line, and every follow-up command prints complete on one line with the refs
  already in it, so a worker copies a whole line or nothing.
"""

from __future__ import annotations

import json
import shlex
from typing import Any, Iterable, Mapping, Sequence

FORMAT_JSON = "json"
FORMAT_BRIEF = "brief"
FORMATS = (FORMAT_JSON, FORMAT_BRIEF)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_UNREADABLE = 2

_REF_SUFFIXES = ("_ref", "_refs", "_id", "_ids", "_key", "_hash")
_BODY_INDENT = "    "


class UnreadableOutput(Exception):
    """The text is not a Problem Board envelope. ``raw`` carries it complete."""

    def __init__(self, reason: str, raw: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.raw = raw


# --------------------------------------------------------------------------- input


def parse_envelope(text: str) -> dict[str, Any]:
    """Parse one CLI envelope. Anything else raises UnreadableOutput.

    The CLI writes an error envelope to stderr and nothing to stdout, so a
    reader fed only stdout sees an empty string. That is the most common
    unreadable input and it gets its own reason.
    """
    stripped = text.strip()
    if not stripped:
        raise UnreadableOutput(
            "empty output. The CLI writes an error envelope to stderr and nothing "
            "to stdout, so capture both (2>&1) or use --format brief.",
            text,
        )
    skipped: list[str] = []
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        # A warning line printed before the envelope (seen with `pb worker
        # inspect` on 2026-09-18) is not a reason to lose the envelope. Skip to
        # the first line that opens an object and keep the skipped lines visible.
        lines = stripped.splitlines()
        start = next((index for index, line in enumerate(lines) if line.lstrip().startswith("{")), None)
        if start is None or start == 0:
            raise UnreadableOutput(f"not JSON ({exc.msg} at line {exc.lineno}, column {exc.colno})", text) from exc
        try:
            parsed = json.loads("\n".join(lines[start:]))
        except json.JSONDecodeError as inner:
            raise UnreadableOutput(f"not JSON ({inner.msg} at line {inner.lineno + start}, column {inner.colno})", text) from inner
        skipped = lines[:start]
    if not isinstance(parsed, Mapping) or "ok" not in parsed:
        raise UnreadableOutput("JSON without an ok field is not a Problem Board envelope", text)
    envelope = dict(parsed)
    if skipped:
        envelope["_skipped_prefix"] = skipped
    return envelope


# --------------------------------------------------------------------------- output


def render_text(text: str, *, worker_flags: Sequence[str] = ()) -> tuple[str, int]:
    """Render raw CLI text. Returns the rendering and the exit code to use."""
    try:
        envelope = parse_envelope(text)
    except UnreadableOutput as exc:
        return render_unreadable(exc.reason, exc.raw), EXIT_UNREADABLE
    return render_envelope(envelope, worker_flags=worker_flags), (EXIT_OK if envelope.get("ok") else EXIT_ERROR)


def render_unreadable(reason: str, raw: str) -> str:
    lines = [f"UNREADABLE: {reason}", "raw output follows, complete:"]
    lines.extend(_BODY_INDENT + line for line in (raw.splitlines() or [""]))
    return "\n".join(lines) + "\n"


def render_envelope(envelope: Mapping[str, Any], *, worker_flags: Sequence[str] = ()) -> str:
    prefix: list[str] = []
    skipped = envelope.get("_skipped_prefix")
    if skipped:
        prefix.append(f"NOTE: {len(skipped)} line(s) printed before the envelope:")
        prefix.extend(_BODY_INDENT + line for line in skipped)
    if not envelope.get("ok"):
        return "\n".join(prefix + [_render_error(envelope.get("error")).rstrip("\n")]) + "\n"
    result = envelope.get("result")
    lines: list[str] = prefix + ["OK"]
    lines.extend(_render_result(result, list(worker_flags)))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- errors


def _render_error(error: Any) -> str:
    if not isinstance(error, Mapping):
        return f"ERROR: {error!r}\n"
    code = error.get("code") or "unknown"
    lines = [f"ERROR {code}"]
    message = error.get("message")
    if message:
        lines.append(f"message: {message}")
    details = error.get("details")
    if details:
        lines.append("details:")
        lines.extend(_flatten(details, prefix="  "))
    remaining = {k: v for k, v in error.items() if k not in ("code", "message", "details")}
    if remaining:
        lines.extend(_flatten(remaining, prefix=""))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- results


def _render_result(result: Any, flags: list[str]) -> list[str]:
    if not isinstance(result, Mapping):
        return _flatten(result, prefix="") or ["(empty result)"]
    schema = str(result.get("schema") or "")
    if schema.startswith("problem-board.worker-input."):
        return _render_receive(result, flags)
    if schema.startswith("problem-board.local-mail-leases."):
        return _render_leases(result, flags)
    if "message" in result and "settlement" in result and isinstance(result.get("message"), Mapping):
        return _render_lease_read(result, flags)
    if "operation" in result and "object" in result:
        return _render_coordinate(result)
    if "session" in result and "worker" in result and isinstance(result.get("session"), Mapping):
        return _render_inspect(result)
    if schema.startswith("problem-board.local-journal-receipt."):
        return ["journal receipt:"] + _flatten(result.get("receipt", result), prefix="  ")
    if "items" in result and isinstance(result.get("items"), list) and "states" in result:
        return _render_deliveries(result, flags)
    if isinstance(result.get("workers"), list):
        return _render_worker_list(result)
    if "settlement_summary" in result and "settled_at" in result:
        return _render_settled(result)
    if not result:
        return ["(empty result)"]
    return _flatten(result, prefix="")


def _render_receive(result: Mapping[str, Any], flags: list[str]) -> list[str]:
    lines: list[str] = []
    delivery = result.get("delivery") or {}
    items = result.get("items") or []
    acquired = result.get("acquired_leases") or []
    active = result.get("active_leases") or {}
    lease_by_message = {l.get("message_ref"): l for l in acquired if isinstance(l, Mapping)}

    lines.append(
        "delivery: items {} · remaining {} · has_more {} · limited_by {}".format(
            delivery.get("item_count", len(items)),
            delivery.get("remaining_count", "?"),
            delivery.get("has_more", "?"),
            delivery.get("limited_by") or "none",
        )
    )
    if delivery.get("deferred_message_ref"):
        lines.append(f"deferred_message_ref: {delivery['deferred_message_ref']}")
    lines.append(
        "leases: acquired now {} · already held {} · total held {}".format(
            active.get("acquired_now_count", len(acquired)),
            active.get("already_held_count", "?"),
            active.get("total_held_count", "?"),
        )
    )
    total_held = active.get("total_held_count")
    if isinstance(total_held, int) and total_held > len(items):
        lines.append(
            f"NOTE: {total_held - len(items)} held lease(s) are not in this batch. "
            + _cmd(["pb", "worker", "leases"], flags)
        )
    for issue in result.get("delivery_issues") or []:
        lines.append("DELIVERY ISSUE:")
        lines.extend(_flatten(issue, prefix="  "))
    for project in result.get("projects") or []:
        if isinstance(project, Mapping):
            lines.append(
                f"project: {project.get('project_ref')} · not on this host yet: {project.get('note')}"
                if project.get("state") == "not_on_this_host"
                else f"project: {project.get('project_ref')} · revision {project.get('revision')} · leased {project.get('leased_messages')}"
            )
    for ref in result.get("assignments") or []:
        lines.append(f"assignment: {ref}")

    if not items:
        lines.append("items: none")
    for index, item in enumerate(items, start=1):
        if not isinstance(item, Mapping):
            lines.append(f"--- item {index} of {len(items)} (unexpected shape)")
            lines.extend(_flatten(item, prefix="  "))
            continue
        message = item.get("message") if isinstance(item.get("message"), Mapping) else item
        lease = lease_by_message.get(message.get("message_ref")) or {}
        lines.append(f"--- item {index} of {len(items)}")
        lines.extend(_render_message(message, lease, item.get("project_ref") or message.get("project_ref"), flags))
    return lines


def _render_leases(result: Mapping[str, Any], flags: list[str]) -> list[str]:
    items = result.get("items") or []
    lines = [f"held leases: {result.get('count', len(items))} of {result.get('total', '?')}"]
    if result.get("next_cursor"):
        lines.append(f"next_cursor: {result['next_cursor']}")
        lines.append("more: " + _cmd(["pb", "worker", "leases", "--cursor", str(result["next_cursor"])], flags))
    for index, lease in enumerate(items, start=1):
        if not isinstance(lease, Mapping):
            lines.extend(_flatten(lease, prefix="  "))
            continue
        lines.append(f"--- lease {index} of {len(items)}")
        for key in ("kind", "sender", "subject", "project_ref", "message_ref", "lease_id", "leased_at", "expires_at", "renewals"):
            if key in lease:
                lines.append(f"{key}: {lease[key]}")
        lines.extend(_commands_for(lease.get("message_ref"), lease.get("lease_id"), lease.get("project_ref"), flags))
    return lines


def _render_lease_read(result: Mapping[str, Any], flags: list[str]) -> list[str]:
    message = result["message"]
    lease = message.get("lease") if isinstance(message.get("lease"), Mapping) else {}
    lines = _render_message(message, lease, result.get("project_ref") or message.get("project_ref"), flags)
    settlement = result.get("settlement") or {}
    if settlement:
        lines.append(
            "settlement: required {} · outcomes {}".format(
                settlement.get("required"), ", ".join(map(str, settlement.get("outcomes") or []))
            )
        )
    if result.get("replayed") is not None:
        lines.append(f"replayed: {result.get('replayed')}")
    return lines


def _render_message(message: Mapping[str, Any], lease: Mapping[str, Any], project_ref: Any, flags: list[str]) -> list[str]:
    lines: list[str] = []
    for key in ("kind", "sender", "recipient", "subject", "created_at", "state", "delivery_status"):
        if message.get(key) not in (None, ""):
            lines.append(f"{key}: {message[key]}")
    for key in ("message_ref", "correlation_id", "reply_to", "work_ref", "idempotency_key"):
        if message.get(key) not in (None, ""):
            lines.append(f"{key}: {message[key]}")
    if project_ref:
        lines.append(f"project_ref: {project_ref}")
    lease_id = lease.get("lease_id") if isinstance(lease, Mapping) else None
    if lease_id:
        lines.append(f"lease_id: {lease_id}")
        if lease.get("expires_at"):
            lines.append(f"lease_expires_at: {lease['expires_at']}")
    count = message.get("attachment_count")
    if count:
        lines.append(f"attachments: {count}")
        for attachment in message.get("attachments") or []:
            if isinstance(attachment, Mapping):
                lines.extend(_flatten(attachment, prefix="  "))
    body = message.get("body")
    if body not in (None, ""):
        lines.append("body:")
        lines.extend(_BODY_INDENT + line for line in str(body).splitlines())
    payload = message.get("payload")
    if payload:
        lines.append("payload:")
        lines.extend(_flatten(payload, prefix="  "))
    lines.extend(
        _commands_for(
            message.get("message_ref"),
            lease_id,
            project_ref,
            flags,
            sender=message.get("sender"),
            kind=message.get("kind"),
            correlation_id=message.get("correlation_id"),
            message_id=message.get("message_id"),
        )
    )
    return lines


def _render_inspect(result: Mapping[str, Any]) -> list[str]:
    session = result.get("session") or {}
    worker = result.get("worker") or {}
    channel = result.get("channel") or {}
    authorization = result.get("authorization") or {}
    subscription = session.get("subscription") or {}
    lines = [
        f"worker: {channel.get('worker_name') or worker.get('worker_name')} · alias {channel.get('alias') or '-'}",
        f"channel: {channel.get('state')} · authorization {authorization.get('state')} · worker authorization {(worker.get('authorization') or {}).get('state')}",
        f"session: {session.get('state')} · heartbeat age {session.get('heartbeat_age_seconds')} s",
        "inbox check: {} · age {} s · overdue by {} s · interval {} s".format(
            session.get("inbox_check_state"),
            session.get("inbox_check_age_seconds"),
            session.get("inbox_overdue_by_seconds"),
            session.get("inbox_check_interval_seconds"),
        ),
        f"last_inbox_check_at: {session.get('last_inbox_check_at')}",
        f"subscription: adapter {subscription.get('adapter')} · state {subscription.get('state')} · wake delivery {subscription.get('wake_delivery_state')}",
    ]
    if session.get("inbox_check_state") == "stale":
        lines.append("NOTE: inbox checks are stale. For a Claude Code worker this means its watch has stopped.")
    for ref in session.get("last_message_refs") or []:
        lines.append(f"last_message_ref: {ref}")
    for ref in session.get("last_control_refs") or []:
        lines.append(f"last_control_ref: {ref}")
    listener = worker.get("listener") or {}
    if listener.get("last_settled_message_ref"):
        lines.append(f"last_settled_message_ref: {listener['last_settled_message_ref']}")
    return lines


def _render_deliveries(result: Mapping[str, Any], flags: list[str]) -> list[str]:
    items = result.get("items") or []
    lines = [f"deliveries: {len(items)} shown · total {result.get('total', '?')} · states {', '.join(map(str, result.get('states') or []))}"]
    if result.get("next_cursor"):
        lines.append(f"next_cursor: {result['next_cursor']}")
    for index, item in enumerate(items, start=1):
        lines.append(f"--- delivery {index} of {len(items)}")
        if isinstance(item, Mapping):
            for key in ("state", "remote_disposition", "created_at", "settled_at", "outbox_id", "recipient", "kind", "subject", "source_message_ref", "replay_outbox_id", "replay_message_ref", "replayed_to", "replayed_at", "last_error"):
                if item.get(key) not in (None, ""):
                    lines.append(f"{key}: {item[key]}")
        else:
            lines.extend(_flatten(item, prefix="  "))
    return lines


def _render_settled(result: Mapping[str, Any]) -> list[str]:
    lines = [f"settled: {result.get('state')} at {result.get('settled_at')}"]
    for key in ("message_ref", "correlation_id", "sender", "subject", "settlement_summary"):
        if result.get(key) not in (None, ""):
            lines.append(f"{key}: {result[key]}")
    return lines


def _render_worker_list(result: Mapping[str, Any]) -> list[str]:
    """One line per worker, then the reachability facts that tell a dead path.

    `reachability.overdue_by_seconds` exposes a stopped Claude Code watch even
    when nothing is currently reading the queue.
    """
    workers = result.get("workers") or []
    lines = [f"workers: {len(workers)}"]
    if result.get("config"):
        lines.append(f"config: {result['config']}")
    for worker in workers:
        if not isinstance(worker, Mapping):
            lines.extend(_flatten(worker, prefix="  "))
            continue
        reach = worker.get("reachability") if isinstance(worker.get("reachability"), Mapping) else {}
        listener = worker.get("listener") if isinstance(worker.get("listener"), Mapping) else {}
        alias = worker.get("worker_alias") or "-"
        lines.append(f"--- {alias} ({worker.get('worker_name')})")
        lines.append(
            "runtime {} · pool {} · session {} · reachable {} · pending {} · overdue by {} s · last inbox check {}".format(
                worker.get("runtime_kind"),
                worker.get("pool_status"),
                reach.get("session_state") or listener.get("state"),
                reach.get("reachable"),
                reach.get("pending_messages"),
                reach.get("overdue_by_seconds"),
                reach.get("last_inbox_check_at") or listener.get("last_inbox_check_at") or "-",
            )
        )
        for ref in worker.get("attended_project_refs") or []:
            lines.append(f"attends: {ref}")
        if worker.get("worker_ref"):
            lines.append(f"worker_ref: {worker['worker_ref']}")
        overdue = reach.get("overdue_by_seconds")
        if worker.get("runtime_kind") == "claude-code" and isinstance(overdue, (int, float)) and overdue > 0 and worker.get("pool_status") != "retired":
            lines.append(
                f"NOTE: a Claude Code worker overdue by {int(overdue)} s has no running watch. "
                "Its session cannot be reached through the board until its guard or its operator restarts it."
            )
    return lines


_RECEIPT_OUTCOMES = ("applied", "refused")


def _render_coordinate(result: Mapping[str, Any]) -> list[str]:
    lines = [f"operation: {result.get('operation')}"]
    obj = result.get("object")
    if isinstance(obj, Mapping) and isinstance(obj.get("item"), Mapping):
        item = obj["item"]
        for key in ("item_key", "title", "status", "assignee", "revision", "work_ref"):
            if item.get(key) not in (None, ""):
                lines.append(f"{key}: {item[key]}")
        if item.get("description"):
            lines.append("description:")
            lines.extend(_BODY_INDENT + line for line in str(item["description"]).splitlines())
        for entry in item.get("acceptance") or []:
            lines.append(f"acceptance: {entry}")
        rest = {k: v for k, v in item.items() if k not in ("item_key", "title", "status", "assignee", "revision", "work_ref", "description", "acceptance")}
        lines.extend(_flatten(rest, prefix=""))
        other = {k: v for k, v in obj.items() if k != "item"}
        lines.extend(_flatten(other, prefix=""))
        return lines
    if isinstance(obj, Mapping) and obj.get("state") in _RECEIPT_OUTCOMES:
        # A governed-mutation receipt. The outcome is the first line, and an
        # empty error slot is not printed: a caller that reads `error = {}`
        # beside `observed_revision` can mistake an applied receipt for a
        # conflict and retry it under a fresh key, writing it again. `state`
        # is the field that says applied or refused; a refusal carries its
        # reason and details beside it. Other coordinate objects carry a
        # `state` too (a binding is `bound`, a channel `exempt`), and those
        # keep the flat form: the receipt treatment is keyed on the outcome
        # vocabulary, not on the key name.
        state = str(obj.get("state") or "")
        lines.append(f"state: {state}{' (replayed)' if obj.get('replayed') else ''}")
        rest = {
            k: v
            for k, v in obj.items()
            if k not in ("state", "replayed") and not (k == "error" and not v)
        }
        lines.extend(_flatten(rest, prefix=""))
        return lines
    lines.extend(_flatten(obj, prefix=""))
    return lines


# --------------------------------------------------------------------------- commands


def _commands_for(
    message_ref: Any,
    lease_id: Any,
    project_ref: Any,
    flags: list[str],
    *,
    sender: Any = None,
    kind: Any = None,
    correlation_id: Any = None,
    message_id: Any = None,
) -> list[str]:
    """Complete follow-up commands, refs filled in, one per line."""
    if not message_ref or not lease_id:
        return []
    scope = ["--project-ref", str(project_ref)] if project_ref else []
    lines = [
        "read: " + _cmd(["pb", "worker", "lease-read", *scope, "--message-ref", str(message_ref), "--lease-id", str(lease_id)], flags),
        "settle: "
        + _cmd(
            ["pb", "worker", "settle", *scope, "--message-ref", str(message_ref), "--lease-id", str(lease_id), "--outcome", "acknowledged", "--summary", "<what you did>"],
            flags,
        ),
    ]
    if kind in ("question", "request") and correlation_id:
        recipient = "operator" if sender in (None, "", "control-plane", "operator") else str(sender)
        reply_scope = [] if recipient == "operator" else scope
        key = f"reply-{message_id}" if message_id else "reply-<message_id>"
        lines.append(
            "reply: "
            + _cmd(
                [
                    "pb", "worker", "send", *reply_scope,
                    "--recipient", recipient, "--kind", "reply",
                    "--correlation-id", str(correlation_id), "--reply-to", str(message_ref),
                    "--subject", "<subject>", "--body-file", "<path>", "--idempotency-key", key,
                ],
                flags,
            )
        )
    return lines


def _cmd(parts: Iterable[str], flags: list[str]) -> str:
    parts = list(parts)
    head, tail = parts[:3], parts[3:]
    if flags and parts[:2] == ["pb", "worker"]:
        head = head + list(flags)
    quoted = []
    for part in head + tail:
        if part.startswith("<") and part.endswith(">"):
            quoted.append(part)
        else:
            quoted.append(shlex.quote(part))
    return " ".join(quoted)


# --------------------------------------------------------------------------- generic


def _flatten(value: Any, *, prefix: str, path: str = "") -> list[str]:
    """Every leaf as ``path = value``, complete. Nothing is skipped or cut."""
    lines: list[str] = []
    if isinstance(value, Mapping):
        if not value:
            lines.append(f"{prefix}{path or '(object)'} = {{}}")
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            lines.extend(_flatten(child, prefix=prefix, path=child_path))
    elif isinstance(value, list):
        if not value:
            lines.append(f"{prefix}{path or '(list)'} = []")
        for index, child in enumerate(value):
            lines.extend(_flatten(child, prefix=prefix, path=f"{path}[{index}]"))
    else:
        if isinstance(value, str) and "\n" in value:
            lines.append(f"{prefix}{path} =")
            lines.extend(f"{prefix}{_BODY_INDENT}{line}" for line in value.splitlines())
        else:
            lines.append(f"{prefix}{path} = {value}")
    return lines


def worker_flags_from_argv(argv: Sequence[str]) -> list[str]:
    """The runtime identity flags present on the command line, for rendered commands."""
    flags: list[str] = []
    args = list(argv)
    for name in ("--runtime-kind", "--runtime-session-id", "--session-id", "--config"):
        if name in args:
            index = args.index(name)
            if index + 1 < len(args):
                canonical = "--runtime-session-id" if name == "--session-id" else name
                flags.extend([canonical, args[index + 1]])
        else:
            for arg in args:
                if arg.startswith(name + "="):
                    canonical = "--runtime-session-id" if name == "--session-id" else name
                    flags.extend([canonical, arg.split("=", 1)[1]])
    return flags


def select_format(argv: Sequence[str], environ: Mapping[str, str]) -> tuple[list[str], str]:
    """Strip ``--format X`` / ``--format=X`` / ``--brief`` from argv, anywhere.

    argparse binds an option to one subparser, and this option belongs to all of
    them, so it is read before parsing. ``PB_FORMAT`` is the default.
    """
    remaining: list[str] = []
    chosen = environ.get("PB_FORMAT", FORMAT_JSON).strip().lower() or FORMAT_JSON
    args = list(argv)
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--format" and index + 1 < len(args):
            chosen = args[index + 1].strip().lower()
            index += 2
            continue
        if arg.startswith("--format="):
            chosen = arg.split("=", 1)[1].strip().lower()
            index += 1
            continue
        if arg == "--brief":
            chosen = FORMAT_BRIEF
            index += 1
            continue
        remaining.append(arg)
        index += 1
    if chosen not in FORMATS:
        raise ValueError(f"unknown output format {chosen!r}; expected one of {', '.join(FORMATS)}")
    return remaining, chosen


__all__ = [
    "EXIT_ERROR",
    "EXIT_OK",
    "EXIT_UNREADABLE",
    "FORMATS",
    "FORMAT_BRIEF",
    "FORMAT_JSON",
    "UnreadableOutput",
    "parse_envelope",
    "render_envelope",
    "render_text",
    "render_unreadable",
    "select_format",
    "worker_flags_from_argv",
]

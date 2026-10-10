"""Render a Problem Board CLI envelope as bounded, readable text.

The CLI prints one JSON envelope, ``{"ok": true,
"result": ...}`` on stdout or ``{"ok": false, "error": ...}`` on stderr. Every
worker then wrote its own reader for that JSON, from scratch, in a shell
heredoc, dozens of times a day. Those readers were the least reliable code on
the board: one ended in a silent ``raise SystemExit`` and dropped two of six
held leases, and a ref copied out of a wrapped console line lost its tail, so a
reply never correlated. Neither failure was in Problem Board.

Three rules follow, and this module is where they live:

* A worker does not parse. ``--format brief`` on any command, or ``pb render``
  on saved output, prints what the worker needs. Delivery output remains
  complete; read-heavy operations and oversized item/assignment receipts have
  explicit compact renderers. ``--format json`` remains the full-detail view.
* Nothing is ever silent. An error envelope renders as ``ERROR``. Text that is
  not an envelope renders as ``UNREADABLE`` followed by the text itself.
* A displayed locator is never shortened. Actionable refs print complete on
  their own lines, cursors, commits and paths stay whole, and every follow-up
  command prints complete on one line with the refs already in it.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import unquote

from project_board.client.limit_state import limit_state_line, usage_windows_line, window_length_label

FORMAT_JSON = "json"
FORMAT_BRIEF = "brief"
FORMATS = (FORMAT_JSON, FORMAT_BRIEF)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_UNREADABLE = 2

_REF_SUFFIXES = ("_ref", "_refs", "_id", "_ids", "_key", "_hash")
_BODY_INDENT = "    "
_PREVIEW_BYTES = 320
_LONG_PREVIEW_BYTES = 720
_BRIEF_SECTION_ITEMS = 8
_BRIEF_REFS = 12
_FULL_DETAIL_LINE = "detail: compact brief view; rerun the same command with --format json for every field"


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
        return _withhold_signed_urls(
            "\n".join(prefix + [_render_error(envelope.get("error")).rstrip("\n")]) + "\n"
        )
    result = envelope.get("result")
    lines: list[str] = prefix + ["OK"]
    lines.extend(_render_result(result, list(worker_flags)))
    return _withhold_signed_urls("\n".join(lines) + "\n")


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
    current = details.get("current_revision") if isinstance(details, Mapping) else None
    if code == "work_item_revision_conflict" and isinstance(current, int) and current > 0:
        # W563: an additive note (plan.note.append) is retried once with the
        # same text at the current revision; a replacement edit re-reads the
        # item and decides again, because its compare-and-set protects
        # another writer's change. Never retry an outcome-unknown write with
        # altered content.
        lines.append(
            f"retry: plan.note.append only: the same text, unchanged, with \"expected_revision\": {current}. "
            "A replacement edit (plan.item.update) reads the item again and decides first."
        )
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- results


def _preview(value: Any, *, maximum_bytes: int = _PREVIEW_BYTES) -> str:
    """One bounded prose line.

    Identifiers never pass through this helper. Callers print refs, ids, keys,
    hashes, cursors, commits and paths directly so they remain copyable whole.
    """

    text = " ".join(str(value or "").split())
    encoded = text.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return text
    prefix = encoded[: max(0, maximum_bytes - 3)].decode("utf-8", errors="ignore")
    return prefix.rstrip() + "..."


def _joined(values: Any) -> str:
    if isinstance(values, (list, tuple)):
        return ", ".join(str(value) for value in values)
    return str(values or "")


def _present(value: Any) -> bool:
    return value not in (None, "", [], {})


def _bounded(values: Any, *, maximum: int = _BRIEF_SECTION_ITEMS) -> tuple[list[Any], int]:
    items = list(values) if isinstance(values, (list, tuple)) else []
    return items[:maximum], len(items)


def _note_omitted(lines: list[str], label: str, *, shown: int, total: int) -> None:
    if total > shown:
        lines.append(
            f"{label}: {shown} of {total} shown in brief; narrow the read or use --format json"
        )


def _is_worker_context(result: Mapping[str, Any]) -> bool:
    return (
        "project_ref" in result
        and "workspace" in result
        and isinstance(result.get("team"), list)
        and isinstance(result.get("repositories"), list)
    )


def _is_journal_search(result: Mapping[str, Any]) -> bool:
    return isinstance(result.get("entries"), list) and isinstance(
        result.get("index"), Mapping
    )


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
    if schema == "problem-board.worker-inbox.v1":
        return _render_worker_inbox(result, flags)
    if schema == "problem-board.first-run-status.v1":
        return _render_first_run_status(result)
    if _is_procedure_verify(result) and not result.get("detail"):
        return _render_procedure_verify(result)
    if schema == "problem-board.note-read.v1":
        lines = [
            "note: {} · item {} · ordinal {} · by {} · at {}".format(
                result.get("note_ref") or result.get("note_id") or "-", result.get("item_key") or "-",
                result.get("ordinal", "?"), result.get("author_label") or result.get("author") or "-",
                result.get("created_at") or "-",
            ),
            "text:",
        ]
        return lines + [_BODY_INDENT + line for line in str(result.get("text") or "").splitlines() or [""]]
    if schema == "problem-board.item-read.v1":
        return _render_item_read(result)
    if _is_workspace_sweep(result):
        return _render_workspace_sweep(result)
    if _is_worker_context(result):
        return _render_worker_context(result)
    if _is_journal_search(result):
        return _render_journal_search(result)
    if str(result.get("schema") or "") == "project-board.client-source-status.v2":
        return _render_source_status(result)
    if schema == "problem-board.relay-service.v1":
        return _render_relay_service_status(result)
    if "session" in result and "worker" in result and isinstance(result.get("session"), Mapping):
        return _render_inspect(result)
    if schema.startswith("problem-board.local-journal-receipt."):
        return ["journal receipt:"] + _flatten(result.get("receipt", result), prefix="  ")
    if "items" in result and isinstance(result.get("items"), list) and "states" in result:
        return _render_deliveries(result, flags)
    if isinstance(result.get("workers"), list):
        return _render_worker_list(result)
    if isinstance(result.get("recovery"), Mapping) and isinstance(result.get("queue_result"), Mapping):
        return _render_wake_recovery(result)
    if "settlement_summary" in result and "settled_at" in result:
        return _render_settled(result)
    if not result:
        return ["(empty result)"]
    lines = _flatten(result, prefix="")
    if isinstance(result.get("team"), list) and result.get("team"):
        lines.extend(_team_usage_lines(result["team"]))
    return lines


def _is_procedure_verify(result: Mapping[str, Any]) -> bool:
    return isinstance(result.get("verified"), list) and isinstance(result.get("package"), Mapping)


def _render_procedure_verify(result: Mapping[str, Any]) -> list[str]:
    """A verdict per target; the manifest and changed files with --detail (W563).

    Root, 2026-10-06 05:17 UTC (W563 note_e42209a9): adopting .15, the brief
    verify printed the whole package inventory and the same 48 changed files
    for both runtimes. The adoption decision needs, per target, whether it is
    current and intact and whether its entrypoint changed.
    """

    package = result["package"]
    files = package.get("files") if isinstance(package.get("files"), Mapping) else {}
    lines = [
        "procedure: {} · revision {} · digest {} · {} files".format(
            package.get("package_id") or "-", package.get("revision") or "-",
            str(package.get("source_digest") or "")[:12] or "-", len(files) or "?",
        )
    ]
    entrypoint = str(package.get("entrypoint") or "SKILL.md")
    for row in result["verified"]:
        if not isinstance(row, Mapping):
            continue
        changed = [str(path) for path in row.get("changed_files") or []]
        errors = [str(error) for error in row.get("errors") or []]
        lines.append(
            "--- {} · {} · installed {} · digest {} · files verified {} · errors {}".format(
                row.get("target") or "-", row.get("state") or "-", row.get("installed_revision") or "-",
                "matches" if row.get("installed_digest") and row.get("installed_digest") == row.get("source_digest")
                else "DIFFERS",
                row.get("files_verified", "?"), len(errors) or "none",
            )
        )
        for error in errors[:3]:
            lines.append(f"  error: {_preview(error, maximum_bytes=200)}")
        if row.get("changed_since_revision"):
            lines.append(
                "  changed since {}: {} file(s) · {} {}".format(
                    row["changed_since_revision"], len(changed), entrypoint,
                    "changed: load it once" if entrypoint in changed else "unchanged",
                )
            )
    lines.append(
        "read: of the changed files, only the modules you had loaded or now need for your acts; "
        "a newly available module is read when its act comes up"
    )
    lines.append("detail: add --detail for the manifest and every changed file; --format json for every field")
    return lines


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _render_first_run_status(result: Mapping[str, Any]) -> list[str]:
    """The machine and session verdict a recovery decision needs (W563).

    Root, 2026-10-06 04:14-04:18 UTC (note_e2512d98): during a relay reconnect
    the brief status printed 138 lines, about 2,854 tokens, mostly source
    subtree hashes printed twice, for a decision that needs the channel state,
    the error, the next retry and the authorization. Those lead here; the
    source identity is one line per side with a same-or-different verdict,
    and the JSON keeps every field.
    """

    machine = _mapping(result.get("machine"))
    session = _mapping(result.get("session"))
    following = _mapping(result.get("next"))
    relay = _mapping(machine.get("relay"))
    relay_source = _mapping(relay.get("source"))
    client = _mapping(machine.get("client"))
    client_source = _mapping(client.get("source"))
    lines = [f"status: {result.get('state') or '-'} · next step {following.get('step') or 'none'}"]
    if session:
        lines.append(
            "session: {} ({}) · channel {} · authorization {} · control plane {} · projects {}".format(
                session.get("alias") or "-", session.get("worker_name") or "-",
                session.get("channel_state") or "-", session.get("authorization") or "-",
                session.get("control_plane_state") or "-", len(session.get("attended_project_refs") or []),
            )
        )
    connection = _mapping(session.get("connection"))
    if connection:
        lines.append(
            "connection: {} · error {} · attempts {} · schedule {} · next attempt {}{}".format(
                connection.get("state") or "-", connection.get("reason") or "-",
                connection.get("attempts", "?"), connection.get("schedule") or "-",
                connection.get("next_attempt_at") or "-",
                " · an attempt is running now" if connection.get("attempt_in_progress") else "",
            )
        )
    refusal = _mapping(session.get("refusal"))
    if refusal:
        lines.append(
            "refusal: {} · permanent {} · credential refused {}".format(
                refusal.get("code") or refusal.get("error_code") or "-",
                refusal.get("permanent", "?"), refusal.get("credential", "?"),
            )
        )
    relay_commit = str(relay_source.get("commit") or "")
    client_commit = str(client_source.get("commit") or "")
    lines.append(
        "relay: installed {} · running {} · commit {} · release {}".format(
            relay.get("installed", "?"), relay.get("running", "?"), relay_commit[:12] or "-",
            str(relay_source.get("release_id") or "")[:12] or "-",
        )
    )
    lines.append(
        "client: commit {} · pinned {} · {}".format(
            client_commit[:12] or "-", client.get("pinned", "?"),
            "same as the relay" if relay_commit and relay_commit == client_commit
            else "DIFFERS from the relay" if relay_commit and client_commit else "relay source unknown",
        )
    )
    prerequisites = _mapping(machine.get("prerequisites"))
    checked = [item for item in prerequisites.get("checked") or [] if isinstance(item, Mapping)]
    keyring = next((item for item in checked if item.get("name") == "keyring"), {})
    lines.append(
        "prerequisites: ok {} · missing {} · keyring {}".format(
            prerequisites.get("ok", "?"), ", ".join(map(str, prerequisites.get("missing") or [])) or "none",
            "usable" if keyring.get("found") else f"NOT usable, fix: {keyring.get('fix') or 'see --format json'}"
            if keyring else "not checked",
        )
    )
    target = _mapping(machine.get("default_target"))
    if target:
        lines.append(f"target: {target.get('target_id') or '-'} · host {target.get('host_id') or '-'}")
    # Who must approve the next step is the deciding fact in a recovery:
    # relay install, restart and source selection are the operator's (Ops,
    # review of 1ffd4a22).
    approval = str(following.get("approval") or "")
    if following.get("command"):
        lines.append(f"next command: {following['command']} · approval {approval or 'none'}")
    elif approval and approval != "none":
        lines.append(f"next approval: {approval}")
    if following.get("explain"):
        lines.append(f"why: {_preview(following['explain'], maximum_bytes=300)}")
    lines.append(_FULL_DETAIL_LINE)
    return lines


_INBOX_CLASS_LABELS = {0: "operator", 1: "action", 2: "information"}
_ITEM_KEY_RE = re.compile(r":(w\d+):", re.IGNORECASE)


def _render_worker_inbox(result: Mapping[str, Any], flags: list[str]) -> list[str]:
    """Pending mail as headers, two lines each, never a body (W563)."""

    classes = result.get("classes") if isinstance(result.get("classes"), Mapping) else {}
    by_kind = result.get("pending_by_kind") if isinstance(result.get("pending_by_kind"), Mapping) else {}
    lines = [
        "inbox: pending {} · matched {} · shown {} · operator {} · action {} · information {}".format(
            result.get("pending", "?"), result.get("matched", "?"), result.get("returned", "?"),
            classes.get("operator", 0), classes.get("action", 0), classes.get("information", 0),
        ),
        "pending by kind: " + (" · ".join(f"{kind} {count}" for kind, count in by_kind.items()) or "none"),
    ]
    if result.get("backlog"):
        lines.append(
            f"backlog: {result['backlog']} of the pending are set aside by a backlog mark "
            "(received only with pb worker receive --backlog)"
        )
    headers = [header for header in result.get("headers") or [] if isinstance(header, Mapping)]
    if not headers:
        lines.append("headers: none")
    for header in headers:
        if header.get("operator"):
            label = "operator"
        elif header.get("expected_reaction") == "acknowledge_only":
            label = "information"
        else:
            label = "action" if header.get("kind") in {
                "question", "decision", "request", "blocked", "delivery_failed", "assign", "reply",
            } else "information"
        match = _ITEM_KEY_RE.search(str(header.get("work_ref") or ""))
        lines.append(
            "--- {} · {} · {} · from {}{} · {}".format(
                label + (" · backlog" if header.get("backlog") else ""),
                header.get("created_at") or "-", header.get("kind") or "-", header.get("sender") or "-",
                f" · {match.group(1).upper()}" if match else "",
                _preview(header.get("subject"), maximum_bytes=120) or "(no subject)",
            )
        )
        lines.append(f"message_ref: {header.get('message_ref')}")
    if headers:
        lines.append(
            "receive one: " + _cmd(["pb", "worker", "receive", "--message-ref", "<message_ref>"], flags)
        )
    return lines


def _render_item_read(result: Mapping[str, Any]) -> list[str]:
    """An item's text fields printed whole, tied to the revision read (W563).

    This is the safe full read the brief item view points to when it clips
    prose: nothing here is previewed, and attachments are names and refs only.
    """

    lines = [
        "item: {} · {} · revision {} · updated {}".format(
            result.get("item_key") or "-", result.get("status") or "-",
            result.get("revision", "?"), result.get("updated_at") or "-",
        )
    ]
    for key in ("project_ref", "identity_ref", "item_ref"):
        if _present(result.get(key)):
            lines.append(f"{key}: {result[key]}")
    lines.append(
        "assignee {} · acting {} · reviewer {}".format(
            result.get("assignee") or "-", result.get("acting_assignee") or "-", result.get("reviewer") or "-"
        )
    )
    assignment = result.get("assignment")
    if isinstance(assignment, Mapping):
        lines.append(
            "assignment: state {} · ownership {} · worker {}".format(
                assignment.get("state") or "-", assignment.get("ownership_version", "?"),
                assignment.get("worker_name") or "-",
            )
        )
        if _present(assignment.get("assignment_ref")):
            lines.append(f"assignment.assignment_ref: {assignment['assignment_ref']}")
    fields = result.get("fields") if isinstance(result.get("fields"), Mapping) else {}
    for name, value in fields.items():
        if not _present(value):
            lines.append(f"{name}: (empty)")
        elif isinstance(value, list):
            lines.append(f"{name}: {len(value)}")
            for position, entry in enumerate(value, start=1):
                lines.append(f"  [{position}] {' '.join(str(entry).split())}")
        elif isinstance(value, Mapping):
            lines.append(f"{name}:")
            for key, entry in value.items():
                if _present(entry):
                    lines.append(f"  {key}:")
                    lines.extend(_BODY_INDENT + line for line in _joined(entry).splitlines() or [""])
        else:
            lines.append(f"{name}:")
            lines.extend(_BODY_INDENT + line for line in str(value).splitlines() or [""])
    attachments = [entry for entry in result.get("attachments") or [] if isinstance(entry, Mapping)]
    lines.append(f"attachments: {result.get('attachment_count', len(attachments))}")
    if not attachments and result.get("attachment_count"):
        lines.append(
            f"  list: pb worker item-attachment-list --project-ref {result.get('project_ref') or '<project-ref>'} "
            f"--item-key {result.get('item_key') or '<Wn>'}"
        )
    for entry in attachments:
        lines.append(f"  attachment: {entry.get('filename') or '-'} · {entry.get('file_ref') or '-'}")
    lines.append(f"notes: {result.get('note_count', 0)} (not read here; plan.notes.list)")
    return lines


def _is_workspace_sweep(result: Mapping[str, Any]) -> bool:
    return isinstance(result.get("trees"), list) and "would_remove" in result and "workspace" in result


# Trees, runs and loose entries shown in the sweep brief; the rest are counted.
_SWEEP_ROWS = 60


def _count(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def _render_workspace_sweep(result: Mapping[str, Any]) -> list[str]:
    """The sweep's verdict: counts, and only what --apply would remove (W563).

    Coordinator, 2026-10-06 00:35Z: a sweep with nothing eligible printed
    48 trees, 475 scratch runs and 228 loose entries, 8,210 tokens. The brief
    form is now the verdict; --detail prints a line per tree, run and loose
    entry, and the JSON keeps every path.
    """

    if result.get("detail"):
        return _render_workspace_sweep_detail(result)
    trees = [tree for tree in result.get("trees") or [] if isinstance(tree, Mapping)]
    runs = [run for run in result.get("scratch_runs") or [] if isinstance(run, Mapping)]
    loose = [entry for entry in result.get("loose") or [] if isinstance(entry, Mapping)]
    removable = [str(path) for path in result.get("would_remove") or []]
    removable_runs = [run for run in runs if run.get("action") == "remove"]
    lines = [
        "workspace sweep: {} · trees {} · would remove {} · size {}".format(
            result.get("workspace") or "-", len(trees), len(removable), result.get("total_bytes", "not measured")
        ),
        "verdict: {}".format(
            f"{len(removable)} tree(s) and {len(removable_runs)} scratch run(s) would be removed by --apply"
            if removable or removable_runs
            else "nothing to remove"
        ),
    ]
    for key in ("state", "reason", "apply_refused"):
        if _present(result.get(key)):
            lines.append(f"{key}: {_preview(result[key])}")
    kept = [tree for tree in trees if tree.get("action") != "remove"]
    lines.append(
        "trees: keep {} · remove {} · dirty {} · unpushed {} · ended {}".format(
            len(kept), len(trees) - len(kept),
            sum(1 for tree in trees if _count(tree.get("dirty"))),
            sum(1 for tree in trees if tree.get("unpushed_commits")),
            sum(1 for tree in trees if tree.get("ended")),
        )
    )
    if runs:
        lines.append(f"scratch runs: {len(runs)} · keep {len(runs) - len(removable_runs)} · remove {len(removable_runs)}")
    if loose:
        lines.append(f"loose entries: {len(loose)} (move each into a run with pb worker scratch --new)")
    shown, total = _bounded(removable, maximum=_SWEEP_ROWS)
    for path in shown:
        lines.append(f"would_remove: {path}")
    _note_omitted(lines, "removable trees", shown=len(shown), total=total)
    shown_runs, run_total = _bounded(removable_runs, maximum=_SWEEP_ROWS)
    for run in shown_runs:
        lines.append(f"would_remove run: item {run.get('item') or '-'} · {run.get('path') or '-'}")
    _note_omitted(lines, "removable scratch runs", shown=len(shown_runs), total=run_total)
    handled = {"worker", "workspace", "trees", "would_remove", "total_bytes", "scratch_runs", "loose",
               "state", "reason", "apply_refused", "detail"}
    rest = {key: value for key, value in result.items() if key not in handled}
    if rest:
        lines.extend(_flatten(rest, prefix=""))
    lines.append("detail: add --detail for a line per tree, run and loose entry")
    lines.append(_FULL_DETAIL_LINE)
    return lines


def _render_workspace_sweep_detail(result: Mapping[str, Any]) -> list[str]:
    """One line per tree and run, counts instead of path lists (W563).

    The flat form printed every dirty, untracked and ignored path of every
    tree: a workspace with many trees produced thousands of lines (W423,
    3,703 lines). What --apply would remove is printed whole; each kept tree
    shows its first reason and how many more there are. The JSON keeps every
    path.
    """

    trees = [tree for tree in result.get("trees") or [] if isinstance(tree, Mapping)]
    removable = [str(path) for path in result.get("would_remove") or []]
    lines = [
        "workspace sweep: {} · trees {} · would remove {} · size {}".format(
            result.get("workspace") or "-", len(trees), len(removable), result.get("total_bytes", "not measured")
        )
    ]
    for key in ("state", "reason", "apply_refused"):
        if _present(result.get(key)):
            lines.append(f"{key}: {_preview(result[key])}")
    for path in removable:
        lines.append(f"would_remove: {path}")
    shown, total = _bounded(trees, maximum=_SWEEP_ROWS)
    for tree in shown:
        where = tree.get("branch") or (f"detached {tree['head']}" if tree.get("head") else "-")
        facts = [
            str(tree.get("action") or "-"),
            str(tree.get("kind") or "-"),
            f"item {tree.get('item') or '-'}",
            where,
            f"size {tree.get('size_bytes', '-')}",
        ]
        for key, label in (("dirty", "dirty"), ("untracked", "untracked"), ("ignored", "ignored")):
            if _count(tree.get(key)):
                facts.append(f"{label} {_count(tree.get(key))}")
        if tree.get("unpushed_commits"):
            facts.append(f"unpushed {tree['unpushed_commits']}")
        if tree.get("ended"):
            facts.append(f"ended: {_preview(tree['ended'], maximum_bytes=80)}")
        lines.append(f"--- {' · '.join(facts)} · {tree.get('path') or '-'}")
        keep = [str(reason) for reason in tree.get("keep") or []]
        if keep:
            more = f" (+{len(keep) - 1} more)" if len(keep) > 1 else ""
            lines.append(f"  keep: {_preview(keep[0], maximum_bytes=200)}{more}")
    _note_omitted(lines, "trees", shown=len(shown), total=total)
    runs = [run for run in result.get("scratch_runs") or [] if isinstance(run, Mapping)]
    if runs:
        lines.append(f"scratch runs: {len(runs)}")
        shown_runs, run_total = _bounded(runs, maximum=_SWEEP_ROWS)
        for run in shown_runs:
            keep = [str(reason) for reason in run.get("keep") or []]
            lines.append(
                "--- run {} · item {} · {} · {}{}".format(
                    run.get("action") or "-",
                    run.get("item") or "-",
                    _preview(run.get("purpose"), maximum_bytes=80) or "-",
                    run.get("path") or "-",
                    f" · keep: {_preview(keep[0], maximum_bytes=120)}" if keep else "",
                )
            )
        _note_omitted(lines, "scratch runs", shown=len(shown_runs), total=run_total)
    loose = [entry for entry in result.get("loose") or [] if isinstance(entry, Mapping)]
    if loose:
        shown_loose, loose_total = _bounded(loose, maximum=_SWEEP_ROWS)
        for entry in shown_loose:
            lines.append(f"loose: {entry.get('kind') or '-'} · {entry.get('path') or '-'}")
        lines.append("loose entries: move each into a run with pb worker scratch --new")
        _note_omitted(lines, "loose entries", shown=len(shown_loose), total=loose_total)
    handled = {"worker", "workspace", "trees", "would_remove", "total_bytes", "scratch_runs", "loose",
               "state", "reason", "apply_refused", "detail"}
    rest = {key: value for key, value in result.items() if key not in handled}
    if rest:
        lines.extend(_flatten(rest, prefix=""))
    lines.append(_FULL_DETAIL_LINE)
    return lines


def _render_worker_context(result: Mapping[str, Any]) -> list[str]:
    """The current coordinates and decision facts, not every cached field.

    Keep the dotted repository lines stable: the public add-a-worker-host
    procedure consumes those lines when it audits deploy keys. Everything
    displayed here is current local evidence from the command invocation; the
    JSON form retains the complete context record.
    """

    lines = [f"context: {result.get('project_ref') or '-'}"]
    routing = result.get("context_view") == "routing"
    if routing:
        # W563: the routing view; the start-up coordinates are in the full read.
        lines.append("view: routing (start-up coordinates: the same command without --routing)")
    for key in (
        "project_ref",
        "project_on_this_host",
        "project_card",
        "project_facts_revision",
        "workspace",
        "workspace_source",
        "journal_state",
        "journal_home_ref",
        "project_artifact_ref",
        "revision",
        "local_journal_home",
        "local_journal_directory",
        "journal_home_commit",
        "journal_home_read_root",
        "project_setup_ref",
        "local_project_setup",
        "project_instructions_ref",
        "local_project_instructions",
        "project_instructions_state",
        "project_facts_ref",
        "local_project_facts",
        "project_facts_state",
        "project_environment_ref",
        "local_project_environment",
        "project_environment_state",
        "project_files_revision",
        "project_files_editable",
        "repositories_revision",
    ):
        if key in result:
            lines.append(f"{key} = {result[key]}")

    for key in (
        "project_goal",
        "workspace_note",
        "project_note",
        "journal_error_code",
        "journal_error",
    ):
        if _present(result.get(key)):
            lines.append(f"{key} = {_preview(result[key], maximum_bytes=_LONG_PREVIEW_BYTES)}")

    facts, fact_count = _bounded(result.get("project_facts") or [])
    if facts:
        lines.append(f"project facts: {fact_count}")
        for index, fact in enumerate(facts):
            if isinstance(fact, Mapping):
                label = _preview(fact.get("label"), maximum_bytes=120)
                value = _preview(fact.get("value"), maximum_bytes=_PREVIEW_BYTES)
                lines.append(f"project_facts[{index}] = {label}: {value}")
        _note_omitted(lines, "project facts", shown=len(facts), total=fact_count)

    roles = result.get("roles")
    if isinstance(roles, Mapping):
        # W517: the optional roles the board carries, beside the coordinator.
        for name, role in sorted(roles.items()):
            # A role the project never declared says nothing (review P3).
            if not isinstance(role, Mapping) or str(role.get("state") or "none") == "none":
                continue
            holder = role.get("holder") if isinstance(role.get("holder"), Mapping) else {}
            pending = role.get("pending_handovers")
            pending = pending if isinstance(pending, Mapping) else {}
            oldest = str(pending.get("oldest_at") or "")
            lines.append(
                "role {}: state {} · holder {} · revision {} · pending hand-overs {}{}{}".format(
                    name,
                    role.get("state") or "none",
                    holder.get("worker_name") or "-",
                    role.get("revision", 0),
                    pending.get("count", 0),
                    f" · oldest since {oldest}" if oldest and pending.get("count") else "",
                    " (overdue)" if pending.get("overdue") else "",
                )
            )
            if role.get("unavailable_reason"):
                lines.append(f"role {name}.unavailable_reason = {role['unavailable_reason']}")

    coordinator = result.get("coordinator")
    if isinstance(coordinator, Mapping):
        lines.append(
            "coordinator: state {} · acting {} · revision {}".format(
                coordinator.get("state") or "unknown",
                coordinator.get("acting"),
                coordinator.get("revision", 0),
            )
        )
        holder = coordinator.get("holder") if isinstance(coordinator.get("holder"), Mapping) else None
        home = coordinator.get("home") if isinstance(coordinator.get("home"), Mapping) else None
        for key in (
            "holder",
            "home",
            "since",
            "expected_until",
            "home_available",
            "home_unavailable_reason",
        ):
            if key in coordinator:
                value = coordinator[key]
                if isinstance(value, Mapping):
                    # W563: the home coordinator is usually the holder; it is
                    # named once, and in full only when it differs.
                    if key == "home" and holder is not None and home is not None and (
                        str(home.get("worker_name") or "") == str(holder.get("worker_name") or "")
                    ):
                        lines.append("coordinator.home = the holder")
                        continue
                    for field in (
                        "worker_ref",
                        "worker_name",
                        "worker_alias",
                        "host_id",
                        "host_label",
                        "attending",
                        "available",
                    ):
                        if _present(value.get(field)):
                            lines.append(
                                f"coordinator.{key}.{field} = {value[field]}"
                            )
                else:
                    rendered = (
                        _preview(value)
                        if key == "home_unavailable_reason"
                        else value
                    )
                    lines.append(f"coordinator.{key} = {rendered}")
    coordinators, coordinator_count = _bounded(result.get("coordinators") or [])
    for name in coordinators:
        lines.append(f"coordinator_label: {name}")
    _note_omitted(
        lines,
        "coordinator labels",
        shown=len(coordinators),
        total=coordinator_count,
    )

    # W393: every teammate is accounted for, one compact scheduling row each.
    # What a coordinator routes by (the info line, a blocked wake, model and
    # effort, usage) is on the default read; `--member` shows one in full.
    team = [member for member in result.get("team") or [] if isinstance(member, Mapping)]
    team_filter = result.get("team_filter")
    if isinstance(team_filter, Mapping):
        lines.append(
            "team: {} of {} match --member {}".format(
                team_filter.get("matched", len(team)),
                team_filter.get("team_total", "?"),
                team_filter.get("member") or "-",
            )
        )
    else:
        lines.append(f"team: {len(team)} · shown {len(team)}")
    full = isinstance(team_filter, Mapping)
    if full:
        for index, member in enumerate(team):
            lines.extend(
                _team_member_lines(
                    index, len(team), member, full=full,
                    project_ref=str(result.get("project_ref") or "<project-ref>"),
                )
            )
    else:
        # W563 (Q8, coordinator 2026-10-05): one bounded row per teammate;
        # provider account and provenance are in `--member <name>` and JSON.
        shared = _account_share_counts(team)
        for member in team:
            lines.extend(_team_row(member, shared, str(result.get("project_ref") or "<project-ref>")))
        if team:
            lines.append(
                "team detail (provider account, provenance): pb worker context --project-ref "
                f"{result.get('project_ref') or '<project-ref>'} --member <name>"
            )

    own = result.get("self")
    if isinstance(own, Mapping):
        lines.append("self:")
        for key in ("state", "worker_ref", "worker_name", "observed_at", "note"):
            if _present(own.get(key)):
                value = own[key]
                lines.append(
                    f"  {key} = {_preview(value) if key == 'note' else value}"
                )
        for group in ("owner", "runtime_account"):
            value = own.get(group)
            if isinstance(value, Mapping):
                for field in (
                    "kind",
                    "user_id",
                    "display_name",
                    "provider",
                    "account_id",
                    "account_label",
                    "email",
                    "organization",
                    "source",
                    "observed_at",
                ):
                    if _present(value.get(field)):
                        lines.append(f"  {group}.{field} = {value[field]}")

    repositories, repository_count = _bounded(result.get("repositories") or [])
    lines.append(f"repositories: {repository_count}")
    for index, repository in enumerate(repositories):
        if not isinstance(repository, Mapping):
            continue
        for key in (
            "alias",
            "url",
            "role",
            "branch",
            "path",
            "repository_ref",
        ):
            if key in repository:
                lines.append(f"repositories[{index}].{key} = {repository[key]}")
    _note_omitted(
        lines,
        "repositories",
        shown=len(repositories),
        total=repository_count,
    )

    identity = result.get("commit_identity")
    if isinstance(identity, Mapping) and identity:
        lines.append("commit identity:")
        for key in ("name", "email", "source"):
            if _present(identity.get(key)):
                lines.append(f"  {key} = {identity[key]}")
        if _present(identity.get("source_note")):
            lines.append(f"  source_note = {_preview(identity['source_note'])}")
        # One `git config` pair per clone repeated the same name and email for
        # every repository (W563); one command sets them all, and the JSON
        # keeps each.
        command_count = len(identity.get("commands") or [])
        if command_count:
            lines.append(
                f"  set in every clone ({command_count} git config commands): "
                f"pb worker workspace-report --project-ref {result.get('project_ref') or '<project-ref>'} --set-identity"
            )

    clone = result.get("journal_clone")
    if isinstance(clone, Mapping) and clone:
        lines.append("journal clone:")
        for key in (
            "state",
            "path",
            "branch",
            "head",
            "upstream",
            "upstream_head",
            "ahead",
            "behind",
            "action",
        ):
            if _present(clone.get(key)):
                value = clone[key]
                lines.append(
                    f"  {key} = {_preview(value) if key == 'action' else value}"
                )

    files, file_count = _bounded(result.get("project_files") or [])
    if "project_files" in result:
        lines.append(f"further project files: {file_count}")
    for index, item in enumerate(files):
        if not isinstance(item, Mapping):
            continue
        for key in ("ref", "local_path", "state", "alias", "path"):
            if key in item:
                lines.append(f"project_files[{index}].{key} = {item[key]}")
        if _present(item.get("description")):
            lines.append(
                f"project_files[{index}].description = {_preview(item['description'])}"
            )
    _note_omitted(lines, "further project files", shown=len(files), total=file_count)

    runtimes, runtime_count = _bounded(result.get("runtimes") or [])
    if "runtimes" in result:
        lines.append(f"runtimes: {runtime_count}")
    for runtime_index, runtime in enumerate(runtimes):
        if not isinstance(runtime, Mapping):
            continue
        for key in ("name", "host", "kind", "profile_ref", "local_profile"):
            if key in runtime:
                lines.append(f"runtimes[{runtime_index}].{key} = {runtime[key]}")
        actions, action_count = _bounded(runtime.get("actions") or [])
        for action_index, action in enumerate(actions):
            if not isinstance(action, Mapping):
                continue
            stem = f"runtimes[{runtime_index}].actions[{action_index}]"
            lines.append(f"{stem}.name = {action.get('name') or ''}")
            lines.append(f"{stem}.who = {_joined(action.get('who'))}")
            releases, release_count = _bounded(action.get("releases") or [])
            for release_index, release in enumerate(releases):
                if not isinstance(release, Mapping):
                    continue
                for key in ("repository", "ref"):
                    if key in release:
                        lines.append(
                            f"{stem}.releases[{release_index}].{key} = {release[key]}"
                        )
            _note_omitted(
                lines,
                f"{stem}.releases",
                shown=len(releases),
                total=release_count,
            )
        _note_omitted(
            lines,
            f"runtimes[{runtime_index}].actions",
            shown=len(actions),
            total=action_count,
        )
    _note_omitted(lines, "runtimes", shown=len(runtimes), total=runtime_count)

    for key in ("project_setup_issues", "journal_error_details"):
        values = result.get(key)
        if isinstance(values, list):
            shown_values, value_count = _bounded(values)
            for value in shown_values:
                lines.append(f"{key}: {_preview(value)}")
            _note_omitted(
                lines, key, shown=len(shown_values), total=value_count
            )
        elif isinstance(values, Mapping):
            lines.append(f"{key}: {_preview(json.dumps(values, sort_keys=True))}")

    lines.append(_FULL_DETAIL_LINE)
    # Kept last for compatibility with the established brief-output contract
    # and so quota evidence is one scan-friendly block.
    if team and full:
        lines.extend(_team_usage_lines(team))
    if routing:
        lines = _routing_lines(lines)
    return lines


# Lines of the context brief a routing read keeps, by their start (W563). The
# reader is in the team rows, so its `self:` block is not repeated.
_ROUTING_PREFIXES = ("context:", "view:", "attendance_note", "role ", "coordinator", "team", "--- ")


def _routing_lines(lines: list[str]) -> list[str]:
    """The routing view: who coordinates, the roles, self and the team rows.

    Indented lines are kept only inside the sections kept (self, team rows'
    wake lines), never the repository or runtime blocks.
    """

    kept: list[str] = []
    keep_indented = False
    for line in lines:
        if line.startswith("  "):
            if keep_indented:
                kept.append(line)
            continue
        keep_indented = line.startswith(("--- ", "coordinator"))
        if line.rstrip().endswith("=") or line.endswith("= None"):
            continue  # an empty field says nothing a route needs
        if line.startswith("team detail"):
            kept.append("team detail: --member <name> (account, provenance)")
        elif line.startswith(_ROUTING_PREFIXES):
            kept.append(line)
    return kept


def _render_journal_search(result: Mapping[str, Any]) -> list[str]:
    all_entries = result.get("entries") or []
    entries, entry_count = _bounded(all_entries)
    index = result.get("index") or {}
    lines = [
        "journal search: returned {} · brief {} · index {} · indexed {} · issues {} · excluded {} · recorded {}".format(
            entry_count,
            len(entries),
            index.get("state") or "unknown",
            index.get("indexed_entries", "?"),
            index.get("issue_count", 0),
            index.get("excluded_count", 0),
            index.get("recorded_at") or "-",
        )
    ]
    if not entries:
        lines.append("entries: none")
    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, Mapping):
            continue
        lines.append(
            "--- journal result {} of {}: {} · {} · {} · {}".format(
                position,
                entry_count,
                _preview(entry.get("title"), maximum_bytes=180) or "(untitled)",
                entry.get("status") or "-",
                entry.get("recorded_at") or "-",
                entry.get("worker_name") or "-",
            )
        )
        for key in ("entry_ref", "project_ref", "work_ref", "repository_journal_ref"):
            if _present(entry.get(key)):
                lines.append(f"{key}: {entry[key]}")
        if _present(entry.get("summary")):
            lines.append(
                f"summary: {_preview(entry['summary'])}"
            )
        snippet = _preview(entry.get("snippet"))
        if snippet and snippet != _preview(entry.get("summary")):
            lines.append(f"snippet: {snippet}")
        for key in ("tags", "keywords"):
            if _present(entry.get(key)):
                lines.append(f"{key}: {_preview(_joined(entry[key]))}")
        see_also, see_also_count = _bounded(
            entry.get("see_also") or [], maximum=_BRIEF_REFS
        )
        for ref in see_also:
            lines.append(f"see_also: {ref}")
        _note_omitted(
            lines,
            "see_also refs",
            shown=len(see_also),
            total=see_also_count,
        )
        if entry.get("index_issues"):
            lines.append(f"index issues: {len(entry['index_issues'])}")
    _note_omitted(lines, "journal results", shown=len(entries), total=entry_count)
    lines.append(_FULL_DETAIL_LINE)
    return lines


def _source_identity(value: Any) -> str:
    if not isinstance(value, Mapping):
        return "unknown"
    mode = str(value.get("mode") or "unknown")
    fields = [f"source={mode}"]
    if mode == "released":
        fields.append(f"version={value.get('version') or 'unknown'}")
        if value.get("release_id"):
            fields.append(f"release={value['release_id']}")
    elif mode == "checkout":
        fields.extend(
            (
                f"head={value.get('head') or 'unknown'}",
                f"dirty={value.get('dirty') if value.get('dirty') is not None else 'unknown'}",
            )
        )
    elif mode == "snapshot":
        if value.get("release_id"):
            fields.append(f"release={value['release_id']}")
        if value.get("commit"):
            fields.append(f"commit={value['commit']}")
        components, component_count = _bounded(value.get("components") or [])
        for component in components:
            if isinstance(component, Mapping):
                fields.append(
                    f"{component.get('name') or 'component'}={component.get('commit') or 'unknown'}"
                )
        if component_count > len(components):
            fields.append(f"components={len(components)}-of-{component_count}-shown")
    elif value.get("release_id"):
        fields.append(f"release={value['release_id']}")
    return " ".join(fields)


def _render_relay_source_status(
    relay: Mapping[str, Any], *, label: str
) -> list[str]:
    lines = [f"--- {label}"]
    for key in ("config", "service_id", "system", "installed", "running"):
        if key in relay:
            lines.append(f"{key}: {relay[key]}")
    source = relay.get("source")
    if isinstance(source, Mapping):
        lines.append(f"source: {_source_identity(source)}")
    bootstrap = relay.get("bootstrap_source")
    if isinstance(bootstrap, Mapping):
        lines.append(f"bootstrap: {_source_identity(bootstrap)}")
    startup = relay.get("startup_record")
    if isinstance(startup, Mapping) and startup:
        lines.append(
            f"startup: pid {startup.get('pid') or '-'} · at {startup.get('started_at') or '-'}"
        )
        if isinstance(startup.get("source"), Mapping):
            lines.append(f"startup source: {_source_identity(startup['source'])}")
    for key in ("source_selection_error", "manager_error"):
        if _present(relay.get(key)):
            lines.append(f"{key}: {_preview(relay[key], maximum_bytes=_LONG_PREVIEW_BYTES)}")
    log = relay.get("log")
    if isinstance(log, Mapping):
        state = log.get("state") or log.get("status")
        if _present(state):
            lines.append(f"log: {state}")
    return lines


def _render_source_status(result: Mapping[str, Any]) -> list[str]:
    lines = ["client source status:"]
    for key, label in (
        ("selected", "selected"),
        ("running_release", "running pb"),
        ("released_bootstrap", "released bootstrap"),
    ):
        if isinstance(result.get(key), Mapping):
            lines.append(f"{label}: {_source_identity(result[key])}")
    lines.append(
        f"selection_matches_bootstrap: {result.get('selection_matches_bootstrap')}"
    )
    active = result.get("active_release")
    if isinstance(active, Mapping):
        lines.append(f"active_release.release_id: {active.get('release_id') or '-'}")
        lines.append(f"active_release.path: {active.get('path') or '-'}")
        if isinstance(active.get("source"), Mapping) and active.get("source"):
            lines.append(f"active_release.source: {_source_identity(active['source'])}")
    environment = result.get("environment")
    if isinstance(environment, Mapping):
        for key in ("python", "pb"):
            if key in environment:
                lines.append(f"environment.{key}: {environment[key]}")
    launcher = result.get("launcher")
    if isinstance(launcher, Mapping):
        lines.append(
            "launcher: installed {} · current {} · version {} · expected {}".format(
                launcher.get("installed"),
                launcher.get("current"),
                launcher.get("version", "?"),
                launcher.get("expected_version", "?"),
            )
        )
        for key in ("path", "expected_pb"):
            if _present(launcher.get(key)):
                lines.append(f"launcher.{key}: {launcher[key]}")
    relay = result.get("relay")
    if isinstance(relay, Mapping):
        lines.extend(_render_relay_source_status(relay, label="primary relay"))
    host_relays, relay_count = _bounded(result.get("host_relays") or [])
    lines.append(f"host relays: {relay_count}")
    for index, host_relay in enumerate(host_relays, start=1):
        if isinstance(host_relay, Mapping):
            lines.extend(
                _render_relay_source_status(host_relay, label=f"host relay {index}")
            )
    _note_omitted(lines, "host relays", shown=len(host_relays), total=relay_count)
    lines.append(_FULL_DETAIL_LINE)
    return lines


def _diagnostic_summary(diagnostic: Mapping[str, Any]) -> str:
    fields = [f"state {diagnostic.get('state') or 'not reported'}"]
    if _present(diagnostic.get("code")):
        fields.append(f"code {diagnostic['code']}")
    if _present(diagnostic.get("started_at")):
        fields.append(f"since {diagnostic['started_at']}")
    if _present(diagnostic.get("next_attempt_at")):
        fields.append(f"next attempt {diagnostic['next_attempt_at']}")
    if _present(diagnostic.get("message")):
        fields.append(f"message {_preview(diagnostic['message'])}")
    if _present(diagnostic.get("last_error")):
        fields.append(f"last error {_preview(diagnostic['last_error'])}")
    recent = diagnostic.get("recent")
    if isinstance(recent, list):
        fields.append(f"recent {len(recent)}")
        latest = (
            next((entry for entry in reversed(recent) if isinstance(entry, Mapping)), None)
            if str(diagnostic.get("state") or "").lower() not in {"ready", "healthy"}
            else None
        )
        if latest is not None:
            if _present(latest.get("code")):
                fields.append(f"latest {latest['code']}")
            if _present(latest.get("ended_at") or latest.get("started_at")):
                fields.append(f"at {latest.get('ended_at') or latest['started_at']}")
            if _present(latest.get("message")):
                fields.append(f"message {_preview(latest['message'])}")
    return " · ".join(fields)


def _render_relay_service_status(result: Mapping[str, Any]) -> list[str]:
    lines = ["relay service status:"]
    for key in ("service_id", "system", "installed", "running", "definition", "config"):
        if key in result:
            lines.append(f"{key}: {result[key]}")
    for key in ("source", "bootstrap_source"):
        if isinstance(result.get(key), Mapping):
            lines.append(f"{key}: {_source_identity(result[key])}")
    startup = result.get("startup_record")
    if isinstance(startup, Mapping) and startup:
        lines.append(f"startup: pid {startup.get('pid') or '-'} · at {startup.get('started_at') or '-'}")
        if isinstance(startup.get("source"), Mapping):
            lines.append(f"startup source: {_source_identity(startup['source'])}")
    for key in ("source_selection_error", "manager_error"):
        if _present(result.get(key)):
            lines.append(f"{key}: {_preview(result[key], maximum_bytes=_LONG_PREVIEW_BYTES)}")
    log = result.get("log")
    if isinstance(log, Mapping):
        lines.append(
            "log: {} · size {} bytes · total {} bytes".format(
                log.get("state") or log.get("status") or "available",
                log.get("size_bytes", "?"), log.get("total_size_bytes", "?"),
            )
        )
        for key in ("path", "error"):
            if _present(log.get(key)):
                value = log[key] if key == "path" else _preview(log[key])
                lines.append(f"log.{key}: {value}")
    diagnostics = result.get("relay_diagnostics")
    if isinstance(diagnostics, Mapping):
        transport = diagnostics.get("transport")
        if isinstance(transport, Mapping):
            lines.append(
                "transport: degraded {} · code {} · since {} · recent {}".format(
                    transport.get("degraded"), transport.get("code") or "-",
                    transport.get("started_at") or "-",
                    len(transport.get("recent") or []) if isinstance(transport.get("recent"), list) else "?",
                )
            )
            if _present(transport.get("waking_on")):
                lines.append(f"transport.waking_on: {transport['waking_on']}")
        channels = diagnostics.get("channels")
        if isinstance(channels, list):
            attention = []
            healthy = []
            for channel in channels:
                if not isinstance(channel, Mapping):
                    continue
                diagnostic = channel.get("relay_diagnostic")
                diagnostic = diagnostic if isinstance(diagnostic, Mapping) else {}
                state = str(diagnostic.get("state") or "not_recorded").lower()
                channel_state = str(channel.get("channel_state") or "").lower()
                target = (
                    healthy
                    if state in {"ready", "healthy"}
                    and channel_state not in {"reconnecting", "pending_authorization", "error"}
                    else attention
                )
                target.append(channel)
            shown = (attention + healthy)[:_BRIEF_SECTION_ITEMS]
            lines.append(f"channels: {len(channels)} · attention {len(attention)}")
            for channel in shown:
                diagnostic = channel.get("relay_diagnostic")
                diagnostic = diagnostic if isinstance(diagnostic, Mapping) else {}
                lines.append(
                    "channel: {} · {} · {} · {}".format(
                        channel.get("worker_alias") or "-",
                        channel.get("worker_name") or "-",
                        channel.get("channel_state") or "-",
                        _diagnostic_summary(diagnostic),
                    )
                )
            _note_omitted(lines, "channels", shown=len(shown), total=len(channels))
    lines.append(_FULL_DETAIL_LINE)
    return lines


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
    backlog = result.get("backlog")
    if isinstance(backlog, Mapping) and backlog.get("counted") is False:
        # W563: a selected receive does not count the marked mail; say so
        # instead of printing a pending count it never took.
        lines.append(
            "backlog: not counted by this selected receive · marked {} at {}".format(
                backlog.get("marked_count", "?"), backlog.get("marked_at") or "-",
            )
        )
        lines.append(str(backlog.get("instruction") or ""))
    elif isinstance(backlog, Mapping):
        # W563: the marked mail stays visible on every receive.
        lines.append(
            "backlog: pending {} · requests, decisions and questions {} · oldest {} · marked {} at {}{}".format(
                backlog.get("pending_count", "?"),
                backlog.get("unresolved_count", "?"),
                backlog.get("oldest_at") or "-",
                backlog.get("marked_count", "?"),
                backlog.get("marked_at") or "-",
                " · this batch is backlog" if backlog.get("received_now") else "",
            )
        )
        lines.append(str(backlog.get("instruction") or ""))
    selection = result.get("selection")
    if isinstance(selection, Mapping):
        lines.append(
            "selection: {} · matched pending {} · claimed now {} · unselected pending {}".format(
                selection.get("state") or "unknown",
                selection.get("matched_pending_count", 0),
                selection.get("claimed_now_count", 0),
                selection.get("unselected_count", 0),
            )
        )
        if selection.get("oldest_unselected_at"):
            lines.append(f"oldest unselected pending: {selection['oldest_unselected_at']}")
        if selection.get("previous_state"):
            lines.append(f"previous message state: {selection['previous_state']}")
        if selection.get("held_count"):
            lines.append(f"matching held leases: {selection['held_count']}")
            for held in selection.get("held") or []:
                if isinstance(held, Mapping):
                    lines.append(
                        "  {} · lease {} · expires {}".format(
                            held.get("message_ref") or "?",
                            held.get("lease_id") or "?",
                            held.get("expires_at") or "?",
                        )
                    )
        lines.append(str(selection.get("instruction") or "Run ordinary pb worker receive."))
    # A signal is produced once (an unlink, a control-plane change); brief
    # output is the only place the agent sees it.
    for signal in result.get("signals") or []:
        if isinstance(signal, Mapping):
            lines.append(f"SIGNAL {signal.get('kind') or 'unknown'}:")
            lines.extend(_flatten({k: v for k, v in signal.items() if k != "kind"}, prefix="  "))
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
        lines.extend(
            _render_message(
                message, lease, item.get("project_ref") or message.get("project_ref"), flags,
                compact=True,
            )
        )
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


def _render_message(
    message: Mapping[str, Any],
    lease: Mapping[str, Any],
    project_ref: Any,
    flags: list[str],
    *,
    compact: bool = False,
) -> list[str]:
    """One message as brief lines.

    ``compact`` is the receive view (W472): it leaves out empty envelope
    defaults and a work_ref the header already shows, and prints each
    attachment as one summary plus its exact read command. The W393 handling
    ledger (refs, correlation, idempotency key, lease and every follow-up
    command) and the body stay. ``pb worker lease-read`` and ``--format json``
    keep the complete message.
    """

    lines: list[str] = []
    for key in ("kind", "sender", "recipient", "subject", "created_at", "state", "delivery_status"):
        if message.get(key) not in (None, ""):
            lines.append(f"{key}: {message[key]}")
    for key in ("message_ref", "correlation_id", "reply_to", "work_ref", "idempotency_key"):
        if message.get(key) not in (None, ""):
            lines.append(f"{key}: {message[key]}")
    origin = message.get("operator_origin")
    if isinstance(origin, Mapping):
        lines.append(f"operator_origin.channel: {origin.get('channel') or 'unknown'}")
        if origin.get("ref"):
            lines.append(f"operator_origin.ref: {origin['ref']}")
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
        for position, attachment in enumerate(message.get("attachments") or [], start=1):
            if isinstance(attachment, Mapping):
                lines.extend(
                    _compact_attachment(attachment, position)
                    if compact
                    else _flatten(attachment, prefix="  ")
                )
    body = message.get("body")
    if body not in (None, ""):
        lines.append("body:")
        lines.extend(_BODY_INDENT + line for line in str(body).splitlines())
    payload = message.get("payload")
    if compact:
        payload = _with_matching_retirement_command_folded(payload, message)
        payload = _without_envelope_defaults(payload, header_work_ref=message.get("work_ref"))
    if payload:
        lines.append("payload:")
        lines.extend(_flatten(_payload_without_body_copies(payload, body), prefix="  "))
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


_LOCAL_ATTACHMENT_SCHEMA = "problem-board.local-mail-attachment.v1"


def _compact_attachment(attachment: Mapping[str, Any], position: int) -> list[str]:
    """One attachment as a summary, its exact file_ref and one runnable read command.

    The command is the same argv the receive returned, quoted as a shell line,
    so a filename with spaces, quotes or non-ASCII text still runs as printed.
    Anything that is not a well-formed local attachment, or a field the summary
    does not name, is printed in full rather than dropped.
    """

    command = attachment.get("read_command")
    well_formed = (
        isinstance(command, list)
        and command
        and all(isinstance(part, str) for part in command)
        and isinstance(attachment.get("file_ref"), str)
        and attachment.get("file_ref")
    )
    if not well_formed:
        return _flatten(attachment, prefix="  ")
    summary = " · ".join(
        str(attachment.get(name)) if attachment.get(name) not in (None, "") else f"{name} unknown"
        for name in ("filename", "mime")
    )
    size = attachment.get("size")
    summary += f" · {size} bytes" if size not in (None, "") else " · size unknown"
    lines = [f"  [{position}] {summary}"]
    if attachment.get("sha256"):
        lines.append(f"      sha256 = {attachment['sha256']}")
    lines.append(f"      file_ref = {attachment['file_ref']}")
    lines.append("      read: " + " ".join(shlex.quote(part) for part in command))
    named = {"filename", "mime", "size", "sha256", "file_ref", "read_command", "schema"}
    rest = {name: value for name, value in attachment.items() if name not in named}
    if attachment.get("schema") not in (None, _LOCAL_ATTACHMENT_SCHEMA):
        rest["schema"] = attachment["schema"]
    if rest:
        lines.extend(_flatten(rest, prefix="      "))
    return lines


def _without_envelope_defaults(payload: Any, *, header_work_ref: Any) -> Any:
    """The payload without its empty top-level envelope defaults (W472).

    Only exact top-level values are left out: an empty ``identity_ref``,
    ``work_ref`` or ``payload`` object, an ``operator_origin`` that names no
    origin, and a ``work_ref`` equal to the one the header already prints.
    Nothing nested is touched, so a task, review or instruction field always
    stays, and every non-empty ref (``command_ref``, ``source_message_ref``,
    ``payload_hash``) stays.
    """

    if not isinstance(payload, Mapping):
        return payload
    kept: dict[str, Any] = {}
    for name, value in payload.items():
        if name in ("identity_ref", "work_ref") and value == "":
            continue
        if name == "work_ref" and value and value == header_work_ref:
            continue
        if name == "payload" and isinstance(value, Mapping) and not value:
            continue
        if (
            name == "operator_origin"
            and isinstance(value, Mapping)
            and set(value) <= {"channel", "ref"}
            and str(value.get("channel") or "unknown") == "unknown"
            and not value.get("ref")
        ):
            continue
        kept[name] = value
    return kept


# The fields of a routed mail's admitted command (W474) that repeat what the
# delivered message already shows. Any other field is printed in full.
_RETIREMENT_MAIL_FIELDS = (
    "kind",
    "subject",
    "body",
    "correlation_id",
    "reply_to",
    "source_message_ref",
    "work_ref",
    "identity_ref",
    "payload",
)


def _with_matching_retirement_command_folded(payload: Any, message: Mapping[str, Any]) -> Any:
    """The payload with a matching copy of the admitted mail command named, not repeated (W472).

    Since W474 a routed mail carries ``retirement_command``: the command the
    server admitted, kept as transport evidence. Its ``mail`` fields repeat
    the message's own kind, subject, body, correlation, reply-to, source and
    work refs and payload. When the copy has that shape, each of those fields
    is shown as one summary line naming what it matches, and a field that
    differs, or any field outside that set, is still printed in full under its
    own name. A copy of any other shape is printed whole. Nothing is dropped
    from the JSON output or ``pb worker lease-read``, and the summary carries
    no authority: it says the copy matches, nothing more.
    """

    if not isinstance(payload, Mapping):
        return payload
    command = payload.get("retirement_command")
    if not isinstance(command, Mapping) or "mail" not in command:
        return payload
    # `attachment_custody` names who holds the files (W563: the server added it,
    # and an unknown key printed the whole copy, download paths included).
    if not set(command) <= {"mail", "attachments", "attachment_request_hash", "attachment_custody"}:
        return payload
    mail = command.get("mail")
    if not isinstance(mail, Mapping):
        return payload
    extra: dict[str, Any] = {}
    if "attachments" in command:
        folded_attachments = _retirement_attachments_summary(command.get("attachments"), message)
        if folded_attachments is None:
            extra["attachments"] = command.get("attachments")
        else:
            extra["attachments"] = folded_attachments
    for key in ("attachment_request_hash", "attachment_custody"):
        if key in command:
            extra[key] = command.get(key)
    matched: list[str] = []
    differing: dict[str, Any] = {}
    for name, value in mail.items():
        if _retirement_field_matches(name, value, payload, message):
            matched.append(name)
        else:
            differing[name] = value
    if not matched:
        return payload
    folded = dict(payload)
    summary = "admitted copy of this message; matches its " + ", ".join(matched)
    if differing or extra:
        mail_view: Any = {"(matching fields)": summary, **differing} if differing else summary
        folded["retirement_command"] = {"mail": mail_view, **extra}
    else:
        folded["retirement_command"] = summary + " (full copy: pb worker lease-read or --format json)"
    return folded


# Fields of an admitted attachment entry that only locate or carry the stored
# file (they are encoded in its file_ref, or are the signed transport link).
_RETIREMENT_ATTACHMENT_LOCATORS = frozenset({
    "download_url", "download_path", "stored_name", "owner_id", "conversation_id", "turn_id",
})
# Fields that must equal the message's own attachment for the entry to fold.
_RETIREMENT_ATTACHMENT_IDENTITY = ("filename", "mime", "size", "sha256")


def _retirement_attachments_summary(entries: Any, message: Mapping[str, Any]) -> str | None:
    """One line for admitted attachment entries that repeat the message's attachments, else None.

    Each entry folds only when its file_ref names one of the message's own
    attachments, every identity field it carries equals that attachment's, and
    it carries nothing beyond those and the file's locators. Anything else
    returns None, and the entries are printed in full (signed links withheld).
    """

    if not isinstance(entries, list) or not entries:
        return None
    local = {
        str(item.get("file_ref")): item
        for item in message.get("attachments") or []
        if isinstance(item, Mapping) and isinstance(item.get("file_ref"), str) and item.get("file_ref")
    }
    for entry in entries:
        if not isinstance(entry, Mapping):
            return None
        ref = entry.get("file_ref")
        if not isinstance(ref, str) or ref not in local:
            return None
        allowed = {"file_ref", *_RETIREMENT_ATTACHMENT_IDENTITY, *_RETIREMENT_ATTACHMENT_LOCATORS}
        if not set(entry) <= allowed:
            return None
        # The summary claims every identity field matches, so every one must be
        # present on both sides and equal as JSON.
        for name in _RETIREMENT_ATTACHMENT_IDENTITY:
            if name not in entry or name not in local[ref] or not _same_json(entry[name], local[ref][name]):
                return None
        # A locator is only redundant when it is a plain string; anything else
        # may carry content the reader needs, so the entry is printed.
        for name in _RETIREMENT_ATTACHMENT_LOCATORS:
            if name in entry and entry[name] is not None and not isinstance(entry[name], str):
                return None
    return (
        f"{len(entries)} admitted attachment entr{'y' if len(entries) == 1 else 'ies'} match this "
        "message's attachments by file_ref, filename, mime, size and sha256; their locators and "
        "signed links are not printed (read each file with its attachment read command)"
    )


def _retirement_field_matches(
    name: str, value: Any, payload: Mapping[str, Any], message: Mapping[str, Any]
) -> bool:
    """Whether one field of the admitted mail command repeats what the message shows.

    Matching is strict: values compare as JSON, so ``0`` and ``false`` (or
    ``1`` and ``true``) never match each other at any depth, and a value of
    an unexpected type never matches, so it is printed in full.
    """

    if name not in _RETIREMENT_MAIL_FIELDS:
        return False
    if name == "body":
        return isinstance(value, str) and value.strip() == str(message.get("body") or "").strip()
    if name in ("work_ref", "identity_ref"):
        if value is None or value == "":
            return _empty_text(payload.get(name))
        if not isinstance(value, str):
            return False
        # The server may adapt a work locator for older clients, so the copy
        # matches when it names a locator this message already shows.
        shown = (message.get("work_ref"), payload.get("work_ref"), payload.get("identity_ref"),
                 payload.get("versioned_work_ref"))
        return value in {item for item in shown if isinstance(item, str) and item}
    if name == "correlation_id" and (value is None or value == ""):
        # An uncorrelated mail is delivered under its command's ref.
        command_ref = payload.get("command_ref")
        return isinstance(command_ref, str) and bool(command_ref) and message.get("correlation_id") == command_ref
    if name == "reply_to":
        if value is None or value == "":
            return _empty_text(message.get("reply_to"))
        return isinstance(value, str) and value == message.get("reply_to")
    if name == "source_message_ref":
        return isinstance(value, str) and _same_json(value, payload.get("source_message_ref"))
    if name == "payload":
        inner = payload.get("payload")
        return isinstance(value, Mapping) and _same_json(value, inner if isinstance(inner, Mapping) else {})
    return isinstance(value, str) and _same_json(value, message.get(name))


def _empty_text(value: Any) -> bool:
    """Only an absent value or the empty string counts as empty, never 0, false or []."""

    return value is None or (isinstance(value, str) and value == "")


def _same_json(left: Any, right: Any) -> bool:
    """Equal as JSON, so a boolean never equals a number; unserialisable values never match."""

    try:
        return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)
    except (TypeError, ValueError):
        return False


def _payload_without_body_copies(payload: Any, body: Any) -> Any:
    """The payload with each prose copy of the body named instead of repeated (W393).

    A review notice carries its whole task as the body and again as
    ``payload.command.instructions``, so brief output printed it twice. Only a
    prose field (one named in ``_PROSE_COPY_KEYS``) whose text equals the body,
    ignoring only leading and trailing whitespace, is printed as one line that
    says so, with its size and a hash prefix shared with the body. Every other
    value stays whole: a ref, id, path or list of paths is copied as printed,
    even when it equals the body. A near copy stays whole too. The JSON output
    is untouched.
    """

    text = str(body or "").strip()
    if not text:
        return payload
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    marker = f"(identical to the body above: {len(text.encode('utf-8'))} bytes, sha256 {digest})"

    def replace(value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        return {
            name: (
                marker
                if str(name) in _PROSE_COPY_KEYS
                and isinstance(child, str)
                and child.strip() == text
                else replace(child)
            )
            for name, child in value.items()
        }

    return replace(payload)


# Payload fields that carry a task's prose. Only these are ever named as a
# copy of the body: everything else, locators and lists of them included, is
# printed as it is.
_PROSE_COPY_KEYS = frozenset({"instructions", "body", "text", "description", "task"})


def _relay_turns_line(turns: Any) -> str:
    """W456: this channel's last attendance poll, last success and backoff."""

    if not isinstance(turns, Mapping) or not turns.get("recorded_at"):
        return "relay turns: not recorded yet"
    backoff = turns.get("backoff") if isinstance(turns.get("backoff"), Mapping) else None
    return "relay turns: last attendance poll {} · last success {} · last outcome {}{} · backoff {} · recorded {}".format(
        turns.get("last_attendance_poll_at") or "-",
        turns.get("last_success_at") or "-",
        turns.get("last_outcome") or "-",
        f" ({turns['last_code']})" if turns.get("last_code") else "",
        (
            "none"
            if backoff is None
            else "attempt {} · next {} · {}".format(
                backoff.get("attempts"), backoff.get("next_attempt_at") or "-", backoff.get("reason") or "-"
            )
        ),
        turns.get("recorded_at"),
    )


def _render_inspect(result: Mapping[str, Any]) -> list[str]:
    session = result.get("session") or {}
    worker = result.get("worker") or {}
    channel = result.get("channel") or {}
    # A detach answers ``worker`` (and older clients ``channel``) as plain strings; read
    # them as the worker name and the channel state instead of failing the whole view.
    if not isinstance(worker, Mapping):
        worker = {"worker_name": str(worker)}
    if not isinstance(channel, Mapping):
        channel = {"state": str(channel)}
    authorization = result.get("authorization") or {}
    subscription = session.get("subscription") or {}
    lines = [
        f"worker: {channel.get('worker_name') or worker.get('worker_name')} · alias {channel.get('alias') or '-'}",
        f"channel: {channel.get('state')} · authorization {authorization.get('state')} · worker authorization {(worker.get('authorization') or {}).get('state')}",
        _relay_turns_line(channel.get("relay_turns")),
        f"session: {session.get('state')} · heartbeat age {session.get('heartbeat_age_seconds')} s",
        "inbox check: {} · age {} s · overdue by {} s · interval {} s".format(
            session.get("inbox_check_state"),
            session.get("inbox_check_age_seconds"),
            session.get("inbox_overdue_by_seconds"),
            session.get("inbox_check_interval_seconds"),
        ),
        f"last_inbox_check_at: {session.get('last_inbox_check_at')}",
        f"subscription: adapter {subscription.get('adapter')} · state {subscription.get('state')} · wake delivery "
        + (
            "n/a (session-owned watch)"
            if subscription.get("adapter") == "session-owned-watch"
            else str(subscription.get("wake_delivery_state"))
        ),
    ]
    if session.get("inbox_check_state") == "stale":
        lines.append("NOTE: inbox checks are stale. For a Claude Code worker this means its watch has stopped.")
    channel_connection = channel.get("connection")
    for label, connection in (
        ("channel.connection", channel_connection),
        ("session.connection", session.get("connection")),
    ):
        if not isinstance(connection, Mapping) or not connection:
            continue
        if label == "session.connection" and connection == channel_connection:
            continue
        lines.append(
            "{}: state {} · attempts {} · schedule {} · {}".format(
                label,
                connection.get("state") or "not reported",
                connection.get("attempts", "?"),
                _preview(connection.get("schedule"), maximum_bytes=100) or "-",
                (
                    "attempt running since "
                    + str(connection.get("attempt_started_at") or "not reported")
                    if connection.get("attempt_in_progress") is True
                    else "next attempt "
                    + str(connection.get("next_attempt_at") or "not reported")
                ),
            )
        )
        for key in ("reason", "last_error", "last_error_code", "last_error_summary"):
            if _present(connection.get(key)):
                lines.append(f"{label}.{key}: {_preview(connection[key])}")
    diagnostic = worker.get("relay_diagnostic")
    if isinstance(diagnostic, Mapping) and diagnostic:
        lines.append(f"relay diagnostic: {_diagnostic_summary(diagnostic)}")
    wake_id = subscription.get("outstanding_wake_id")
    wake_state = subscription.get("wake_delivery_state")
    if wake_id or wake_state:
        lines.append(
            f"wake: {wake_state or 'not reported'} · attempts {subscription.get('wake_attempts', 0)}"
        )
        if wake_id:
            lines.append(f"outstanding_wake_id: {wake_id}")
        for key in ("wake_first_attempt_at", "wake_last_attempt_at"):
            if _present(subscription.get(key)):
                lines.append(f"{key}: {subscription[key]}")
    if subscription.get("queue_reconciliation_required"):
        lines.append("wake attention: queue reconciliation required")
    if _present(subscription.get("last_error")):
        lines.append(f"wake last_error: {_preview(subscription['last_error'])}")
    reachability = worker.get("reachability")
    if isinstance(reachability, Mapping):
        if _present(reachability.get("wake_state")):
            lines.append(f"wake attention state: {reachability['wake_state']}")
        for key in ("wake_overdue_since", "wake_retry_exhausted_since", "wake_last_error"):
            if _present(reachability.get(key)):
                value = reachability[key] if key != "wake_last_error" else _preview(reachability[key])
                lines.append(f"wake.{key}: {value}")
    if _present(subscription.get("last_acknowledged_wake_id")):
        lines.append(f"last_acknowledged_wake_id: {subscription['last_acknowledged_wake_id']}")
    mail = result.get("mail")
    if isinstance(mail, Mapping):
        leases = mail.get("active_leases")
        if isinstance(leases, Mapping):
            lines.append(f"active mail leases: {leases.get('total', leases.get('count', '?'))}")
        if "quarantine_count" in mail:
            lines.append(f"quarantine: {mail['quarantine_count']}")
    message_refs, message_count = _bounded(session.get("last_message_refs") or [], maximum=_BRIEF_REFS)
    for ref in message_refs:
        lines.append(f"last_message_ref: {ref}")
    _note_omitted(lines, "last message refs", shown=len(message_refs), total=message_count)
    control_refs, control_count = _bounded(session.get("last_control_refs") or [], maximum=_BRIEF_REFS)
    for ref in control_refs:
        lines.append(f"last_control_ref: {ref}")
    _note_omitted(lines, "last control refs", shown=len(control_refs), total=control_count)
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


def _worker_metadata_is_stale(worker: Mapping[str, Any]) -> bool:
    """Use reported worker/session state as the freshness signal; never guess by age."""

    if str(worker.get("presence") or "").strip().lower() in {
        "stale",
        "offline",
    }:
        return True
    reachability = worker.get("reachability")
    if not isinstance(reachability, Mapping):
        return False
    return str(reachability.get("state") or "").strip().lower() in {
        "detached",
        "never_listened",
        "not_listening",
        "retired",
        "stale",
    }


def _reported_evidence_state(
    record: Mapping[str, Any], *, present: bool, stale: bool
) -> str:
    if not present:
        return "missing"
    reported = str(record.get("state") or "").strip().lower()
    if stale or reported in {"expired", "offline", "stale"}:
        return "stale"
    return reported or "reported"


def _runtime_model_brief(worker: Mapping[str, Any]) -> str:
    record = worker.get("runtime_model")
    record = record if isinstance(record, Mapping) else {}
    model = str(record.get("model_display") or record.get("model") or "").strip()
    effort = str(
        record.get("reasoning_effort") or record.get("effort") or ""
    ).strip()
    state = _reported_evidence_state(
        record,
        present=bool(model or effort),
        stale=_worker_metadata_is_stale(worker),
    )
    source = str(record.get("source") or "").strip() or "not reported"
    observed = str(
        record.get("observed_at") or record.get("recorded_at") or ""
    ).strip() or "not reported"
    return (
        f"runtime: model {model or 'missing'} · reasoning effort {effort or 'missing'} "
        f"· state {state} · source {source} · observed {observed}"
    )


def _runtime_account_brief(worker: Mapping[str, Any]) -> str:
    carrier: Mapping[str, Any] = worker
    account = worker.get("runtime_account")
    if not isinstance(account, Mapping):
        account = worker.get("provider_account")
    board_record = worker.get("board_record")
    if not isinstance(account, Mapping) and isinstance(board_record, Mapping):
        carrier = board_record
        account = board_record.get("runtime_account")
        if not isinstance(account, Mapping):
            account = board_record.get("provider_account")
    account = account if isinstance(account, Mapping) else {}
    fields = []
    for key, label in (
        ("provider", "provider"),
        ("account_id", "account_id"),
        ("account_label", "label"),
        ("email", "email"),
        ("organization", "organization"),
    ):
        value = str(account.get(key) or "").strip()
        if value:
            fields.append(f"{label} {value}")
    state = _reported_evidence_state(
        account,
        present=bool(fields),
        stale=_worker_metadata_is_stale(worker),
    )
    source = str(
        account.get("source")
        or carrier.get("runtime_account_source")
        or carrier.get("provider_account_source")
        or worker.get("runtime_account_source")
        or worker.get("provider_account_source")
        or ""
    ).strip() or "not reported"
    observed = str(
        account.get("observed_at")
        or carrier.get("runtime_account_observed_at")
        or carrier.get("provider_account_observed_at")
        or carrier.get("observed_at")
        or worker.get("runtime_account_observed_at")
        or worker.get("provider_account_observed_at")
        or ""
    ).strip() or "not reported"
    identity = " · ".join(fields) if fields else "missing"
    return (
        f"provider account: {identity} · state {state} · source {source} "
        f"· observed {observed}"
    ) + _session_account_note(carrier if carrier is not worker else worker)


# W310: the session's account against the host's current login, in plain words.
_ACCOUNT_STATE_NOTES = {
    "bound": "the session's account, proven from the session",
    "inferred": "inferred from the host login when the board first saw the session, not proven for the session",
    "mismatch": "first read from the host login, and the host is now logged in to another account",
    "unknown": "not known: the host login is unread, or an earlier board replaced the first reading with a later login",
    "unreported": "not reported",
}


def _session_account_note(worker: Mapping[str, Any]) -> str:
    state = str(worker.get("account_state") or "").strip()
    note = _ACCOUNT_STATE_NOTES.get(state)
    return f" · {note}" if note else ""


# W310: whose usage a sample is. Only the session's own account is capacity.
_ATTRIBUTION_NOTES = {
    "inferred": "not confirmed capacity: read under the host login, inferred as this session's",
    "host_login": "not this session's capacity: read under the host's other login",
    "unverified": "not this session's capacity: the account it was read under is not known",
}


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
        lines.append(_worker_limits_line(worker.get("runtime_limit_state")))
        lines.append(_usage_line(worker.get("runtime_limit_state")))
        lines.append(_runtime_model_brief(worker))
        lines.append(_runtime_account_brief(worker))
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
        lines.extend(_native_delivery_lines(worker, reach))
    return lines


def _native_delivery_lines(worker: Mapping[str, Any], reach: Mapping[str, Any]) -> list[str]:
    """What a stranded Codex wake needs from a coordinator, in two lines (W405).

    Transport (relay, Card) says nothing about whether the model received its
    mail. A wake taken twice without a receive, with mail still pending, is
    the state no automatic path will change; it is named here with the one
    supported recovery. An idle Codex with nothing pending prints nothing.
    """

    if worker.get("runtime_kind") != "codex" or worker.get("pool_status") == "retired":
        return []
    lines: list[str] = []
    wake_id = str(reach.get("outstanding_wake_id") or "")
    exhausted = str(reach.get("wake_retry_exhausted_since") or "")
    recovery = reach.get("wake_recovery") if isinstance(reach.get("wake_recovery"), Mapping) else {}
    last = reach.get("last_wake_recovery") if isinstance(reach.get("last_wake_recovery"), Mapping) else {}
    try:
        pending = int(reach.get("pending_messages") or 0)
    except (TypeError, ValueError):
        pending = 0
    current_recovery = bool(recovery) and str(recovery.get("wake_id") or "") == wake_id
    if wake_id and exhausted and pending <= 0:
        # An exhausted marker with nothing waiting is not a stall. A recovery
        # still recorded for that wake stays visible for audit and keeps its
        # fence, without a stall claim or a new instruction.
        if current_recovery:
            return [
                "recovery: {} for wake {} at {} · no mail pending · unresolved until the worker's receive of this wake; do not submit again".format(
                    recovery.get("state"),
                    wake_id,
                    recovery.get("recorded_at") or recovery.get("requested_at"),
                )
            ]
        wake_id = ""
    if wake_id and exhausted:
        lines.append(
            "NOTE: native delivery stalled: wake {} taken without a receive and its one retry used since {}; "
            "pending {}; last inbox check {}.".format(
                wake_id,
                exhausted,
                reach.get("pending_messages"),
                reach.get("last_inbox_check_at") or "-",
            )
        )
        if current_recovery:
            lines.append(
                "recovery: {} at {}{} · resolved only by the worker's receive of this wake; do not submit again{}".format(
                    recovery.get("state"),
                    recovery.get("recorded_at") or recovery.get("requested_at"),
                    f" · submission {recovery['submission_id']}" if recovery.get("submission_id") else "",
                    " (the last call failed before queuing: one more recovery is allowed)"
                    if recovery.get("state") == "failed"
                    else "",
                )
            )
        else:
            lines.append(
                f"recover once: pb worker wake-recover --worker {worker.get('worker_name')} --wake-id {wake_id}"
            )
    elif last.get("resolved_at"):
        lines.append(
            "last recovery: wake {} resolved by the worker's receive at {}".format(
                last.get("wake_id"), last.get("resolved_at")
            )
        )
    return lines


def _render_wake_recovery(result: Mapping[str, Any]) -> list[str]:
    recovery = result.get("recovery") or {}
    queue = result.get("queue_result") or {}
    lines = [
        f"wake recovery: {recovery.get('state')} · worker {result.get('worker_name')} · wake {result.get('wake_id')}",
        "queue: adapter {} · delivered {} · submission {}{}".format(
            queue.get("adapter"),
            queue.get("delivered"),
            queue.get("queued_submission_id") or "-",
            f" · reason {queue['reason']}" if queue.get("reason") else "",
        ),
    ]
    if result.get("next"):
        lines.append(f"next: {result['next']}")
    return lines


def _worker_limits_line(state: Any) -> str:
    """What the runtime last said about its usage limits, and when (W26).

    The shared account stops at a set share of its limits, so agents read this
    before a large step. The JSON carried it all along; the brief list dropped
    it (2026-09-26). No state is "not reported", never "fine".
    """

    if not isinstance(state, Mapping) or not state:
        return "limits: not reported"
    observed = str(state.get("observed_at") or state.get("recorded_at") or "")
    when = f" · observed {observed[:10]} {observed[11:16]}Z" if len(observed) >= 16 else ""
    return f"limits: {limit_state_line(state)}{when}"


def _usage_line(state: Any) -> str:
    """Each usage window's share and reset, whatever the limit kind (W351)."""

    windows = usage_windows_line(state) if isinstance(state, Mapping) else ""
    return f"usage: {windows or 'no windows reported'}"


# An info line longer than this is cut on the compact row, and the row says
# so and names the command that shows it whole.
_TEAM_INFO_BYTES = 480


def _team_member_lines(
    index: int, total: int, member: Mapping[str, Any], *, full: bool, project_ref: str
) -> list[str]:
    """One teammate's scheduling row: who, where, runtime, info, a blocked wake."""

    alias = str(member.get("worker_alias") or "-")
    name = str(member.get("worker_name") or "-")
    lines = [
        "--- team member {} of {}: {} ({}) · role {} · runtime {} · host {} · pool {} · presence {}".format(
            index + 1,
            total,
            alias,
            name,
            member.get("role") or "worker",
            member.get("runtime_kind") or "-",
            member.get("host_label") or member.get("host_id") or "-",
            member.get("pool_status") or "-",
            member.get("presence") or "-",
        ),
        "  " + _runtime_model_brief(member),
    ]
    lines.append("  " + _runtime_account_brief(member))
    info = str(member.get("info_text") or "").strip()
    if info:
        set_at = str(member.get("info_set_at") or "").strip()
        suffix = f" · set {set_at}" if set_at else ""
        size = len(info.encode("utf-8"))
        if full or size <= _TEAM_INFO_BYTES:
            lines.append(f"  info: {' '.join(info.split())}{suffix}")
        else:
            lines.append(
                f"  info: {_preview(info, maximum_bytes=_TEAM_INFO_BYTES)}{suffix} · "
                f"cut from {size} bytes: pb worker context --project-ref {project_ref} --member {name}"
            )
    else:
        lines.append("  info: none")
    lines.extend(_team_wake_lines(member))
    if full:
        capabilities = member.get("capabilities") or []
        if capabilities:
            lines.append("  capabilities: " + _joined(capabilities))
    return lines


def _member_account_key(member: Mapping[str, Any]) -> str:
    for carrier in (member, member.get("board_record") if isinstance(member.get("board_record"), Mapping) else {}):
        for key in ("runtime_account", "provider_account"):
            account = carrier.get(key)
            if isinstance(account, Mapping):
                ident = str(account.get("account_id") or account.get("email") or "").strip()
                if ident:
                    return ident
    return ""


def _account_share_counts(team: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for member in team:
        key = _member_account_key(member)
        if key:
            counts[key] = counts.get(key, 0) + 1
    return counts


# The info line on a compact team row; longer ones say so and name --member.
_TEAM_ROW_INFO_BYTES = 160


def _team_row(member: Mapping[str, Any], shared: Mapping[str, int], project_ref: str, *,
              now: datetime | None = None) -> list[str]:
    """One teammate on one line: who, where, runtime, quota and restrictions (W563, Q8).

    Usage is the provider account's, so a figure shared by several teammates
    says how many: it is not one agent's consumption. A window whose reset has
    passed is named so its figure is not read as capacity now.
    """

    moment = now or datetime.now(timezone.utc)
    alias = str(member.get("worker_alias") or "-")
    name = str(member.get("worker_name") or "-")
    record = member.get("runtime_model") if isinstance(member.get("runtime_model"), Mapping) else {}
    model = str(record.get("model_display") or record.get("model") or "").strip()
    effort = str(record.get("reasoning_effort") or record.get("effort") or "").strip()
    stale = _worker_metadata_is_stale(member)
    if model or effort:
        runtime = f"model {model or '?'}{'/' + effort if effort else ''}{' (stale)' if stale else ''}"
    else:
        runtime = "model not reported"
    facts = [
        f"{alias} ({name})",
        str(member.get("role") or "worker"),
        str(member.get("runtime_kind") or "-"),
        str(member.get("host_label") or member.get("host_id") or "-"),
        str(member.get("presence") or "-"),
        runtime,
    ]
    state = member.get("limit_state")
    if isinstance(state, Mapping) and state:
        windows = usage_windows_line(state)
        head = "usage" if state.get("kind") == "ok" else limit_state_line(state)
        usage = f"{head} {windows}".strip() if windows else limit_state_line(state)
        observed = str(state.get("observed_at") or "").strip()
        if len(observed) >= 16:
            usage += f" · obs {observed[5:10]} {observed[11:16]}Z"
        passed = _passed_reset_windows(state, moment)
        if passed:
            usage += f" · {', '.join(passed)} reset passed, current use unknown"
        count = shared.get(_member_account_key(member), 0)
        if count > 1:
            usage += f" · account shared by {count}"
        facts.append(usage)
    else:
        facts.append("usage not reported")
    info = " ".join(str(member.get("info_text") or "").split())
    if info:
        size = len(info.encode("utf-8"))
        facts.append(
            "info: " + (info if size <= _TEAM_ROW_INFO_BYTES else _preview(info, maximum_bytes=_TEAM_ROW_INFO_BYTES)
                        + f" (cut from {size} B: --member {name})")
        )
    lines = ["--- " + " · ".join(facts)]
    lines.extend(_team_wake_lines(member))
    disk = _team_disk_line(member)
    if disk:
        lines.append(disk)
    return lines


def _gigabytes(value: Any) -> str:
    return f"{int(value) / 1_000_000_000:.1f} GB"


def _team_disk_line(member: Mapping[str, Any]) -> str:
    """One teammate's disk and workspace state (W547), for the coordinator's team status.

    The host's free disk, the workspace size and the latest sweep's counts,
    each with the time it was observed. Not a routing line: the routing view
    drops it, which keeps W563's routing budget unchanged.
    """

    usage = member.get("disk_usage")
    if not isinstance(usage, Mapping) or not usage.get("host_total_bytes"):
        return ""
    alias = str(member.get("worker_alias") or member.get("worker_name") or "-")
    free, total = int(usage.get("host_free_bytes") or 0), int(usage["host_total_bytes"])
    facts = [f"host free {100 * free / total:.0f}% ({_gigabytes(free)})"]
    if usage.get("workspace_bytes") is not None:
        facts.append(f"workspace {_gigabytes(usage['workspace_bytes'])}")
    reported = str(usage.get("reported_at") or "")
    if len(reported) >= 16:
        facts.append(f"reported {reported[5:10]} {reported[11:16]}Z")
    sweep = usage.get("sweep")
    if isinstance(sweep, Mapping) and sweep.get("observed_at"):
        observed = str(sweep["observed_at"])
        facts.append(
            f"unregistered {int(sweep.get('unregistered') or 0)} · orphan {int(sweep.get('orphan') or 0)}"
            f" · ended but kept {int(sweep.get('ended_but_kept') or 0)}"
            + (f" · swept {observed[5:10]} {observed[11:16]}Z" if len(observed) >= 16 else "")
        )
    else:
        # No sweep reported yet is unknown, never zero.
        facts.append("sweep not reported")
    return f"disk {alias}: " + " · ".join(facts)


def _team_wake_lines(member: Mapping[str, Any]) -> list[str]:
    """A teammate's held or recovered native wake, when the board reports one."""

    lines: list[str] = []
    hold = member.get("wake_hold")
    if isinstance(hold, Mapping) and hold.get("since"):
        lines.append(
            "  wake held since {} until {} · pending {}".format(
                hold.get("since"), hold.get("until") or "-", hold.get("pending", "?")
            )
        )
    recovery = member.get("wake_recovery")
    if isinstance(recovery, Mapping) and recovery.get("wake_id"):
        lines.append(
            "  wake recovery {} for {} · since {}".format(
                recovery.get("state") or "-",
                recovery.get("wake_id"),
                recovery.get("recorded_at") or recovery.get("requested_at") or "-",
            )
        )
    return lines


def _team_usage_lines(team: Sequence[Any], *, now: datetime | None = None) -> list[str]:
    """One line per teammate with its limit and usage windows (W351).

    `pb worker context` is the only cross-host view a worker's CLI has; the
    coordinator routes by these figures (the operator's per-pool caps). A
    window whose reset time has passed is named, so its figure is not read as
    capacity now (W393).
    """

    moment = now or datetime.now(timezone.utc)
    lines = ["team usage:"]
    for member in team:
        if not isinstance(member, Mapping):
            continue
        state = member.get("limit_state")
        name = str(member.get("worker_name") or "-")
        alias = str(member.get("worker_alias") or "")
        label = f"{alias} ({name})" if alias and alias != name else name
        host = member.get("host_label") or member.get("host_id") or ""
        where = f" on {host}" if host else ""
        if isinstance(state, Mapping) and state:
            windows = usage_windows_line(state)
            # "usage ok" already lists the windows without resets; say it once.
            head = "usage ok" if state.get("kind") == "ok" else limit_state_line(state)
            status = f"{head} · {windows}" if windows else limit_state_line(state)
            # A team row is the last sample relayed by that worker, not a claim
            # that the sample is fresh now. Keep its existing provenance so a
            # coordinator can judge it without inventing a freshness threshold.
            status = f"last reported {status}"
            source = str(state.get("source") or "").strip()
            observed = str(state.get("observed_at") or "").strip()
            status += f" · source {source or 'not reported'}"
            status += f" · observed {observed or 'not reported'}"
            attribution = _ATTRIBUTION_NOTES.get(str(state.get("attribution") or ""))
            if attribution:
                status += f" · {attribution}"
            passed = _passed_reset_windows(state, moment)
            if passed:
                status += (
                    f" · reset passed for {', '.join(passed)}: its figure is from before "
                    "the reset, current use not reported"
                )
        else:
            status = "not reported"
        lines.append(f"  {label}{where}: {status}")
    return lines


def _passed_reset_windows(state: Mapping[str, Any], now: datetime) -> list[str]:
    labels = []
    for window in state.get("windows") or []:
        if not isinstance(window, Mapping) or window.get("used_percent") is None:
            continue
        resets = str(window.get("resets_at") or "").strip()
        if not resets:
            continue
        try:
            moment = datetime.fromisoformat(resets.replace("Z", "+00:00"))
        except ValueError:
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        if moment <= now:
            labels.append(window_length_label(window))
    return labels


_RECEIPT_OUTCOMES = ("applied", "refused")


def _latest_actionable_review_return(
    item: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    if str(item.get("status") or "").strip().lower() in {"cancelled", "done"}:
        return None
    if "latest_review" in item:
        latest = item["latest_review"]
        if not isinstance(latest, Mapping):
            return None
    else:
        # Compatibility with pre-pagination servers only. New item reads
        # supply one bounded latest decision, including explicit null.
        history = [
            entry for entry in item.get("review_history") or []
            if isinstance(entry, Mapping)
        ]
        if not history:
            return None
        _index, latest = max(
            enumerate(history),
            key=lambda pair: (
                str(pair[1].get("timestamp") or pair[1].get("created_at") or ""), pair[0],
            ),
        )
    decision = str(latest.get("decision") or "").strip().lower()
    operation = str(latest.get("operation") or "").strip().lower()
    if decision not in {"return", "returned"} and operation != "review.return":
        return None
    return latest


def _render_plan_item(operation: str, item: Mapping[str, Any]) -> list[str]:
    title = _preview(item.get("title"), maximum_bytes=220) or "(untitled)"
    lines = [
        f"operation: {operation}",
        "item: {} · {} · {}".format(
            item.get("item_key") or "-", item.get("status") or "-", title
        ),
    ]
    for key in ("identity_ref", "item_ref", "work_ref"):
        if _present(item.get(key)):
            lines.append(f"{key}: {item[key]}")
    lines.append(
        "revision {} · assignee {} · reviewer {} · acting {} · updated {}".format(
            item.get("revision", item.get("item_revision", "?")),
            item.get("assignee") or "-",
            item.get("reviewer") or "-",
            item.get("acting_assignee") or "-",
            item.get("updated_at") or item.get("item_updated_at") or "-",
        )
    )
    summary = _without_title_prefix(item.get("summary"), item.get("title"))
    description = " ".join(str(item.get("description") or "").split())
    # A summary that only restates the start of the description is not
    # printed twice (W563: both previews carried the same text). Only the
    # whole summary counts: one that adds anything, a hold or a constraint,
    # is printed (review of e35c5800).
    if _present(summary) and not description.startswith(summary):
        lines.append(f"summary: {_preview(summary, maximum_bytes=_ITEM_PROSE_BYTES)}")
    if _present(description):
        lines.append(f"description preview: {_preview(description, maximum_bytes=_ITEM_PROSE_BYTES)}")
    if item.get("note_count"):
        lines.append(
            "notes: {} · read: pb coordinate plan.notes.list --object-ref <project-ref> "
            "--payload-json '{{\"item_key\":\"{}\",\"limit\":20}}'".format(
                item["note_count"], item.get("item_key") or "<Wn>"
            )
        )
    acceptance, acceptance_count = _bounded(
        item.get("acceptance") or [], maximum=5
    )
    for value in acceptance:
        lines.append(f"acceptance preview: {_preview(value, maximum_bytes=_ITEM_ACCEPTANCE_BYTES)}")
    _note_omitted(
        lines,
        "acceptance lines",
        shown=len(acceptance),
        total=acceptance_count,
    )
    for key in ("tags", "keywords"):
        if _present(item.get(key)):
            lines.append(f"{key}: {_preview(_joined(item[key]))}")
    dependencies, dependency_count = _bounded(
        item.get("depends_on") or [], maximum=_BRIEF_REFS
    )
    for dependency in dependencies:
        lines.append(f"depends_on: {dependency}")
    _note_omitted(
        lines,
        "dependencies",
        shown=len(dependencies),
        total=dependency_count,
    )
    dependency_facts, dependency_fact_count = _bounded(
        item.get("dependency_facts") or [], maximum=_BRIEF_REFS
    )
    for fact in dependency_facts:
        if isinstance(fact, Mapping) and _present(fact.get("item_ref")):
            lines.append(f"dependency_ref: {fact['item_ref']}")
            lines.append(
                f"dependency state: {fact.get('state') or 'unknown'} · on page {fact.get('on_page')}"
            )
    _note_omitted(
        lines,
        "dependency facts",
        shown=len(dependency_facts),
        total=dependency_fact_count,
    )
    for key in ("result_ref",):
        if _present(item.get(key)):
            lines.append(f"{key}: {item[key]}")
    for key in ("result", "blocked_reason", "cancel_reason"):
        if _present(item.get(key)):
            lines.append(f"{key}: {_preview(item[key], maximum_bytes=_ITEM_PROSE_BYTES)}")
    review = item.get("review")
    if isinstance(review, Mapping):
        for key in (
            "look_at",
            "could_not_verify",
            "reviewer",
            "submitted_at",
            "merged",
            "deploy",
        ):
            if _present(review.get(key)):
                value = review[key]
                lines.append(
                    f"review.{key}: {_preview(_joined(value), maximum_bytes=_ITEM_PROSE_BYTES)}"
                )
    latest_return = _latest_actionable_review_return(item)
    if latest_return is not None:
        actor = latest_return.get("actor")
        actor = actor if isinstance(actor, Mapping) else {}
        lines.append(
            "latest review return: decision {} · at {} · by {}".format(
                latest_return.get("decision") or "return",
                latest_return.get("timestamp")
                or latest_return.get("created_at")
                or "not reported",
                actor.get("label")
                or actor.get("ref")
                or latest_return.get("actor_principal_key")
                or "not reported",
            )
        )
        lines.append(
            "latest review return reason: {}".format(
                _preview(
                    latest_return.get("reason"),
                    maximum_bytes=_LONG_PREVIEW_BYTES,
                )
                or "not reported"
            )
        )
        evidence, evidence_count = _bounded(
            latest_return.get("evidence") or [], maximum=_BRIEF_REFS,
        )
        for ref in evidence:
            lines.append(f"latest review return evidence: {ref}")
        _note_omitted(
            lines, "latest review return evidence refs", shown=len(evidence), total=evidence_count,
        )
    assignment = item.get("assignment")
    if isinstance(assignment, Mapping):
        lines.append(
            "assignment: state {} · ownership {} · worker {} · updated {}".format(
                assignment.get("state") or "-",
                assignment.get("ownership_version", "?"),
                assignment.get("worker_name") or "-",
                assignment.get("updated_at") or "-",
            )
        )
        # A report names the assignment ref and ownership version; the item's
        # own refs are printed above, and the version the ownership was issued
        # at stays in the JSON (W563).
        for key in ("assignment_ref", "control_ref"):
            if _present(assignment.get(key)):
                lines.append(f"assignment.{key}: {assignment[key]}")
    lines.extend(_item_attachment_lines(item))
    if isinstance(item.get("reference_error"), Mapping):
        lines.extend(_flatten(item["reference_error"], prefix="reference_error."))
    clipped = _clipped_item_fields(item)
    if clipped:
        lines.append(
            "clipped above: {} · read whole: pb worker item-read --project-ref <project-ref> --item-key {}{}".format(
                ", ".join(clipped), item.get("item_key") or "<Wn>",
                "".join(f" --field {name}" for name in clipped),
            )
        )
    lines.append(_FULL_DETAIL_LINE)
    return lines


# The item brief previews prose this far; `pb worker item-read` prints it whole.
_ITEM_PROSE_BYTES = 320
_ITEM_ACCEPTANCE_BYTES = 200


def _clipped_item_fields(item: Mapping[str, Any]) -> list[str]:
    """The item text fields the brief view previewed rather than printed whole."""

    clipped = []
    for name in ("summary", "description", "result", "blocked_reason", "cancel_reason"):
        text = " ".join(str(item.get(name) or "").split())
        if len(text.encode("utf-8")) > _ITEM_PROSE_BYTES:
            clipped.append(name)
    acceptance = item.get("acceptance") or []
    if isinstance(acceptance, list) and (
        len(acceptance) > 5
        or any(len(" ".join(str(entry).split()).encode("utf-8")) > _ITEM_ACCEPTANCE_BYTES for entry in acceptance)
    ):
        clipped.append("acceptance")
    review = item.get("review")
    if isinstance(review, Mapping) and any(
        len(_joined(value).encode("utf-8")) > _ITEM_PROSE_BYTES for value in review.values()
    ):
        clipped.append("review")
    return clipped


def _without_title_prefix(summary: Any, title: Any) -> str:
    """The summary less a leading copy of the title, which the item line shows."""

    text = " ".join(str(summary or "").split())
    heading = " ".join(str(title or "").split())
    if heading and text.startswith(heading):
        text = text[len(heading):].lstrip(" .:-·")
    return text


def _item_attachment_lines(item: Mapping[str, Any]) -> list[str]:
    """A count, a few files and the commands that list and read the rest.

    Listing every file ref cost two lines each on items that carry dozens of
    test files and reports (W563). The listing command pages them by name and
    ref without download links; the JSON item read is not the way to them.
    """

    entries = item.get("attachments") or item.get("attachment_refs") or []
    entries = entries if isinstance(entries, list) else []
    total = item.get("attachment_count") or len(entries)
    if not total:
        return []
    key = item.get("item_key") or "<Wn>"
    # W563: the count and the two commands; the listing pages names and refs.
    return [
        f"attachments: {total} · list: pb worker item-attachment-list --project-ref <project-ref> --item-key {key}"
        f" · read one: pb worker item-attachment-read --project-ref <project-ref> --item-key {key}"
        " --file-ref <file_ref> --output <new path>"
    ]


def _render_plan_search(operation: str, page: Mapping[str, Any]) -> list[str]:
    all_items = page.get("items") or []
    items, returned_count = _bounded(all_items)
    lines = [
        f"operation: {operation}",
        "plan search: matched {} · returned {} · brief {} · page {} of {} · plan revision {} · spent {}".format(
            page.get("matched_count", "?"),
            returned_count,
            len(items),
            page.get("page", 1),
            page.get("page_count", 1),
            page.get("plan_revision", "?"),
            page.get("spend", False),
        ),
    ]
    for key in (
        "project_ref",
        "snapshot_id",
        "generation_token",
        "next_cursor",
    ):
        if _present(page.get(key)):
            lines.append(f"{key}: {page[key]}")
    for key in (
        "query",
        "status",
        "assignee",
        "lifecycle",
        "created_from",
        "created_to",
        "updated_from",
        "updated_to",
    ):
        if _present(page.get(key)):
            value = _joined(page[key])
            lines.append(
                f"criteria.{key}: {_preview(value) if key == 'query' else value}"
            )
    if not items:
        lines.append("items: none")
        ranked_refs, ranked_ref_count = _bounded(
            page.get("ranked_item_refs") or [], maximum=_BRIEF_REFS
        )
        for ref in ranked_refs:
            lines.append(f"ranked_item_ref: {ref}")
        _note_omitted(
            lines,
            "ranked item refs",
            shown=len(ranked_refs),
            total=ranked_ref_count,
        )
    for position, item in enumerate(items, start=1):
        if not isinstance(item, Mapping):
            continue
        lines.append(
            "--- plan result {} of {}: {} · {} · {}".format(
                position,
                returned_count,
                item.get("item_key") or "-",
                item.get("status") or "-",
                _preview(item.get("title"), maximum_bytes=220) or "(untitled)",
            )
        )
        for key in ("identity_ref", "item_ref", "work_ref"):
            if _present(item.get(key)):
                lines.append(f"{key}: {item[key]}")
        lines.append(
            "rank {} · revision {} · assignee {} · state {} · updated {}".format(
                item.get("search_rank", item.get("rank", position)),
                item.get("revision", item.get("item_revision", "?")),
                item.get("assignee") or "-",
                item.get("derived_state") or "-",
                item.get("updated_at") or item.get("item_updated_at") or "-",
            )
        )
        if _present(item.get("summary")):
            lines.append(f"summary: {_preview(item['summary'])}")
        dependencies, dependency_count = _bounded(
            item.get("depends_on") or [], maximum=_BRIEF_REFS
        )
        for dependency in dependencies:
            lines.append(f"depends_on: {dependency}")
        _note_omitted(
            lines,
            "dependencies for " + str(item.get("item_key") or "item"),
            shown=len(dependencies),
            total=dependency_count,
        )
        if isinstance(item.get("reference_error"), Mapping):
            lines.extend(_flatten(item["reference_error"], prefix="reference_error."))
    _note_omitted(
        lines, "plan results", shown=len(items), total=returned_count
    )
    lines.append(_FULL_DETAIL_LINE)
    return lines


# An index page is the caller's own bounded slice (its `limit`), and each item
# costs two lines here, so the page is shown whole up to this many items.
_INDEX_ITEMS = 100


# Dependencies listed per index row; the rest are counted.
_INDEX_DEPENDENCIES = 8


def _dependency_name(dependency: Any) -> str:
    """A dependency as the exact ref the plan stores, or its key and ref when given as a record."""

    if isinstance(dependency, Mapping):
        key = str(dependency.get("item_key") or "")
        ref = str(dependency.get("identity_ref") or dependency.get("work_ref") or dependency.get("item_ref") or "")
        return f"{key} {ref}".strip() or "-"
    return str(dependency)


def _render_plan_index(operation: str, page: Mapping[str, Any]) -> list[str]:
    """One plan-index page as a status table: two lines per item.

    The flat form printed every item's content hashes, embedding fields,
    transitions and keywords, so a seven-item page cost more bytes in brief
    than in JSON (W563). What a status or dispatch decision reads is kept:
    key, status, owner, reviewer, revision, freshness, the counts that say
    whether a full read is needed, and the identity ref to act on.
    """

    all_items = page.get("items") or []
    items, returned_count = _bounded(all_items, maximum=_INDEX_ITEMS)
    lines = [
        f"operation: {operation}",
        "plan index: matched {} · returned {} · page {} of {} · plan revision {} · items in plan {}".format(
            page.get("matched_count", page.get("count", "?")),
            returned_count,
            page.get("page", 1),
            page.get("page_count", 1),
            page.get("plan_revision", "?"),
            page.get("item_count", "?"),
        ),
    ]
    for key in ("project_ref", "generation_token", "next_cursor"):
        if _present(page.get(key)):
            lines.append(f"{key}: {page[key]}")
    counts = page.get("state_counts")
    if isinstance(counts, list) and counts:
        lines.append(
            "state counts: "
            + " · ".join(
                f"{entry.get('state')} {entry.get('count')}"
                for entry in counts
                if isinstance(entry, Mapping)
            )
        )
    if not items:
        lines.append("items: none")
    for item in items:
        if not isinstance(item, Mapping):
            continue
        status = str(item.get("status") or "-")
        derived = str(item.get("derived_state") or "")
        assignee = item.get("assignee") or "-"
        acting = item.get("acting_assignee") or ""
        owner = assignee if not acting or acting == assignee else f"{assignee} (acting {acting})"
        facts = [
            str(item.get("item_key") or "-"),
            status if not derived or derived == status else f"{status} (derived {derived})",
            f"assignee {owner}",
            f"reviewer {item.get('reviewer') or '-'}",
            f"rev {item.get('revision', item.get('item_revision', '?'))}",
            f"updated {item.get('updated_at') or item.get('item_updated_at') or '-'}",
        ]
        for key, label in (
            ("note_count", "notes"),
            ("attachment_count", "attachments"),
        ):
            if item.get(key):
                facts.append(f"{label} {item[key]}")
        dependencies = [dependency for dependency in item.get("depends_on") or [] if dependency]
        # W563 (Root, exchange observation 3): a coordinator could not tell an
        # item with no dependencies from one whose dependencies the brief
        # left out, and fell back to a JSON read. The count is always shown,
        # and the dependencies themselves on their own line.
        facts.append(f"depends on {len(dependencies) or 'none'}")
        lines.append(
            "--- "
            + " · ".join(facts)
            + " · "
            + (_preview(item.get("title"), maximum_bytes=160) or "(untitled)")
        )
        if dependencies:
            shown_dependencies, dependency_total = _bounded(dependencies, maximum=_INDEX_DEPENDENCIES)
            names = [_dependency_name(dependency) for dependency in shown_dependencies]
            more = f" (+{dependency_total - len(shown_dependencies)} more)" if dependency_total > len(shown_dependencies) else ""
            lines.append(f"  depends_on: {', '.join(names)}{more}")
        # W563: the key reads the item (`project.plan.item`), which prints the
        # refs a mutation copies; the index does not repeat them per row.
        if isinstance(item.get("reference_error"), Mapping):
            lines.extend(_flatten(item["reference_error"], prefix="reference_error."))
    if items:
        lines.append(
            "read one: pb coordinate project.plan.item --object-ref <project-ref> "
            "--payload-json '{\"item_key\":\"<key>\"}'"
        )
    _note_omitted(lines, "plan index items", shown=len(items), total=returned_count)
    lines.append(_FULL_DETAIL_LINE)
    return lines


def _render_plan_notes(operation: str, page: Mapping[str, Any]) -> list[str]:
    notes = page.get("items")
    notes = notes if isinstance(notes, list) else []
    lines = [
        f"operation: {operation}",
        f"notes: returned {len(notes)} · total {page.get('total', '?')} · state {page.get('state') or '-'}",
    ]
    for key in ("project_ref", "identity_ref", "work_ref", "view_ref", "content_hash", "next_cursor"):
        if _present(page.get(key)):
            lines.append(f"{key}: {page[key]}")
    item = page.get("item")
    if isinstance(item, Mapping):
        lines.append(
            "item: {} · {} · {} · revision {}".format(
                item.get("item_key") or "-", item.get("status") or "-",
                _preview(item.get("title"), maximum_bytes=220) or "(untitled)",
                item.get("revision", "?"),
            )
        )
        for key in ("identity_ref", "item_ref"):
            if _present(item.get(key)):
                lines.append(f"item.{key}: {item[key]}")
    clipped = 0
    for index, note in enumerate(notes, start=1):
        if not isinstance(note, Mapping):
            continue
        # A page cursor advances past the whole returned page. Every note ref
        # therefore remains visible even when its body preview is omitted.
        lines.append(
            "note {}: {} · ordinal {} · by {} · at {}{}".format(
                index,
                note.get("note_ref") or note.get("note_id") or "-",
                note.get("ordinal", "?"),
                _preview(note.get("author_label") or note.get("author"), maximum_bytes=100) or "-",
                note.get("created_at") or "-",
                " · unavailable" if note.get("available") is False else "",
            )
        )
        if index <= _BRIEF_SECTION_ITEMS and _present(note.get("text")):
            lines.append(f"  preview: {_preview(note['text'])}")
            if len(" ".join(str(note["text"]).split()).encode("utf-8")) > _PREVIEW_BYTES:
                clipped += 1
    _note_omitted(
        lines, "note previews", shown=min(len(notes), _BRIEF_SECTION_ITEMS), total=len(notes)
    )
    if clipped:
        # W563: the safe whole read of a note, once for the page.
        key = (item.get("item_key") if isinstance(item, Mapping) else "") or "<Wn>"
        lines.append(
            f"clipped previews: {clipped} · read one whole: pb worker note-read --project-ref "
            f"{page.get('project_ref') or '<project-ref>'} --item-key {key} --note-ref <note ref above>"
        )
    lines.append(_FULL_DETAIL_LINE)
    return lines


def _assignment_task_preview(task: Any) -> str:
    if not isinstance(task, Mapping):
        return _preview(task)
    for key in ("instructions", "summary", "description", "task"):
        if _present(task.get(key)):
            return _preview(task[key])
    return _preview(
        json.dumps(task, ensure_ascii=True, sort_keys=True),
    )


def _assignment_owner_lines(assignment: Mapping[str, Any]) -> list[str]:
    """Who owns the item now, and who held this implementation assignment (W393).

    The assignment's worker is who the implementation was assigned to. The
    item's direct assignee is who acts on it now: after a review is routed,
    that is the reviewer. A board that does not report the item's assignee is
    said to, never filled in with the implementer.
    """

    worker = str(assignment.get("worker_name") or "") or "-"
    lines = []
    if "item_assignee" in assignment:
        owner = str(assignment.get("item_assignee") or "")
        reviewer = str(assignment.get("item_reviewer") or "")
        lines.append(f"current owner: {owner or 'none'} · reviewer {reviewer or 'none'}")
    else:
        lines.append("current owner: not reported by this board")
        owner = ""
    lines.append(
        "implementation: {} · assigned {} · updated {}".format(
            worker,
            assignment.get("assigned_at") or "-",
            assignment.get("updated_at") or "-",
        )
    )
    if owner and worker != "-" and owner != worker:
        lines.append(
            f"note: {worker} held the implementation. The item's current owner is {owner}."
        )
    return lines


def _render_assignment_list(operation: str, page: Mapping[str, Any]) -> list[str]:
    all_items = page.get("items") or []
    items, returned_count = _bounded(all_items)
    lines = [
        f"operation: {operation}",
        "assignments: matched {} · returned {} · brief {} · total current {} · updated {}".format(
            page.get("matched_count", "?"),
            returned_count,
            len(items),
            page.get("item_count", "?"),
            page.get("updated_at") or "-",
        ),
    ]
    for key in (
        "project_ref",
        "worker_ref",
        "worker_name",
        "generation_token",
        "next_cursor",
    ):
        if _present(page.get(key)):
            lines.append(f"{key}: {page[key]}")
    criteria = page.get("criteria")
    if isinstance(criteria, Mapping):
        criteria_refs, criteria_ref_count = _bounded(
            criteria.get("refs") or [], maximum=_BRIEF_REFS
        )
        for ref in criteria_refs:
            lines.append(f"criteria.ref: {ref}")
        _note_omitted(
            lines,
            "criteria refs",
            shown=len(criteria_refs),
            total=criteria_ref_count,
        )
        for key in (
            "status",
            "item_status",
            "query",
            "created_from",
            "created_to",
            "updated_from",
            "updated_to",
            "match",
        ):
            if _present(criteria.get(key)):
                value = _joined(criteria[key])
                lines.append(
                    f"criteria.{key}: {_preview(value) if key == 'query' else value}"
                )
    if not items:
        lines.append("items: none")
    for position, assignment in enumerate(items, start=1):
        if not isinstance(assignment, Mapping):
            continue
        lines.append(
            "--- assignment {} of {}: {} · assignment {} · item {} · ownership {}".format(
                position,
                returned_count,
                assignment.get("item_key") or "-",
                assignment.get("state") or "-",
                assignment.get("item_status") or "-",
                assignment.get("ownership_version", "?"),
            )
        )
        if _present(assignment.get("item_title") or assignment.get("title")):
            lines.append(
                "title: "
                + _preview(
                    assignment.get("item_title") or assignment.get("title"),
                    maximum_bytes=220,
                )
            )
        for key in (
            "assignment_ref",
            "identity_ref",
            "work_ref",
            "versioned_work_ref",
            "work_version_ref",
            "control_ref",
        ):
            if _present(assignment.get(key)):
                lines.append(f"{key}: {assignment[key]}")
        lines.extend(_assignment_owner_lines(assignment))
        if _present(assignment.get("scope")):
            lines.append(f"scope: {_preview(assignment['scope'])}")
        task_preview = _assignment_task_preview(assignment.get("task"))
        if task_preview:
            lines.append(f"task preview: {task_preview}")
        for key in ("result_ref",):
            if _present(assignment.get(key)):
                lines.append(f"{key}: {assignment[key]}")
        for key in ("result", "blocked_reason", "settlement_summary"):
            if _present(assignment.get(key)):
                lines.append(f"{key}: {_preview(assignment[key])}")
        sources = assignment.get("sources") or []
        if not sources and isinstance(assignment.get("source"), Mapping):
            sources = [assignment["source"]]
        shown_sources, source_count = _bounded(
            sources, maximum=_BRIEF_REFS
        )
        for source_index, source in enumerate(shown_sources):
            if not isinstance(source, Mapping):
                continue
            for key in ("repository_ref", "base_commit", "branch"):
                if _present(source.get(key)):
                    lines.append(f"source[{source_index}].{key}: {source[key]}")
        _note_omitted(
            lines,
            "sources for " + str(assignment.get("item_key") or "assignment"),
            shown=len(shown_sources),
            total=source_count,
        )
        if isinstance(assignment.get("reference_error"), Mapping):
            lines.extend(
                _flatten(assignment["reference_error"], prefix="reference_error.")
            )
    _note_omitted(
        lines, "assignments", shown=len(items), total=returned_count
    )
    lines.append(_FULL_DETAIL_LINE)
    return lines


def _render_assignment_receipt(operation: str, assignment: Mapping[str, Any]) -> list[str]:
    lines = [f"operation: {operation}"]
    for key in ("applied", "replayed", "disposition"):
        if key in assignment:
            lines.append(f"{key}: {assignment[key]}")
    lines.append(
        "assignment: state {} · ownership {} · worker {} · updated {}".format(
            assignment.get("state") or "-",
            assignment.get("ownership_version", "?"),
            assignment.get("worker_name") or "-",
            assignment.get("updated_at") or "-",
        )
    )
    if _present(assignment.get("title")):
        lines.append(f"title: {_preview(assignment['title'], maximum_bytes=220)}")
    for key in (
        "ref", "assignment_ref", "project_ref", "identity_ref", "work_ref",
        "versioned_work_ref", "work_version_ref", "control_ref",
        "current_control_ref", "result_ref", "source_event_ref",
    ):
        if _present(assignment.get(key)):
            lines.append(f"{key}: {assignment[key]}")
    task_preview = _assignment_task_preview(assignment.get("task"))
    if task_preview:
        lines.append(f"task preview: {task_preview}")
    for key in ("result_summary", "settlement_summary", "assignee_limit_warning"):
        if _present(assignment.get(key)):
            lines.append(f"{key}: {_preview(assignment[key], maximum_bytes=_LONG_PREVIEW_BYTES)}")
    limit = assignment.get("assignee_limit")
    if isinstance(limit, Mapping):
        lines.append(f"assignee limit: {limit_state_line(limit)}")
        usage = usage_windows_line(limit)
        if usage:
            lines.append(f"assignee usage: {usage}")
    sources = assignment.get("sources") or assignment.get("source_repositories") or []
    if not sources and isinstance(assignment.get("source"), Mapping):
        sources = [assignment["source"]]
    if not sources and assignment.get("source_repository_ref"):
        sources = [{
            "repository_ref": assignment.get("source_repository_ref"),
            "base_commit": assignment.get("source_base_commit"),
            "branch": assignment.get("source_branch"),
        }]
    shown_sources, source_count = _bounded(sources, maximum=_BRIEF_REFS)
    for index, source in enumerate(shown_sources):
        if not isinstance(source, Mapping):
            continue
        for key in ("repository_ref", "base_commit", "branch"):
            if _present(source.get(key)):
                lines.append(f"source[{index}].{key}: {source[key]}")
    _note_omitted(lines, "sources", shown=len(shown_sources), total=source_count)
    report = assignment.get("report")
    if isinstance(report, Mapping):
        for key in ("state", "source_event_ref", "result_ref"):
            if _present(report.get(key)):
                lines.append(f"report.{key}: {report[key]}")
        if _present(report.get("summary")):
            lines.append(f"report.summary: {_preview(report['summary'])}")
    lines.append(_FULL_DETAIL_LINE)
    return lines


def _render_coordinate(result: Mapping[str, Any]) -> list[str]:
    operation = str(result.get("operation") or "")
    obj = result.get("object")
    if isinstance(obj, Mapping) and operation == "project.plan.item":
        item = obj.get("item") if isinstance(obj.get("item"), Mapping) else obj
        return _render_plan_item(operation, item)
    if isinstance(obj, Mapping) and operation == "project.plan.search":
        return _render_plan_search(operation, obj)
    if isinstance(obj, Mapping) and operation == "project.plan.index":
        return _render_plan_index(operation, obj)
    if isinstance(obj, Mapping) and operation == "plan.notes.list":
        return _render_plan_notes(operation, obj)
    if isinstance(obj, Mapping) and operation == "assignment.list":
        return _render_assignment_list(operation, obj)
    if isinstance(obj, Mapping) and operation in {"assignment.assign", "assignment.return", "assignment.report"}:
        return _render_assignment_receipt(operation, obj)
    lines = [f"operation: {operation}"]
    # The relay's record of this request follows the outcome: a receipt's
    # state stays the first line after the operation (brief-output.md).
    recovery = _recovery_lines(result.get("recovery"))
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
        lines.extend(recovery)
        rest = {
            k: v
            for k, v in obj.items()
            if k not in ("state", "replayed") and not (k == "error" and not v)
        }
        return lines + _mutation_fields(rest)
    lines.extend(recovery)
    if isinstance(obj, Mapping) and isinstance(obj.get("item"), Mapping):
        # An envelope that nests the item beside its own fields. The item is
        # shown by its coordinates, never by its body.
        lines.extend(_mutated_item_lines("item", obj["item"]))
        return lines + _mutation_fields({k: v for k, v in obj.items() if k != "item"})
    if isinstance(obj, Mapping) and _is_item_record(obj):
        # A mutation that answers with the updated item itself (plan.item.update
        # does, W393). The item carries no receipt state, so none is printed:
        # the outcome is the recovery line above, when the relay kept one.
        lines.extend(_mutated_item_lines("item", obj))
        for flag, label in (("_mutation_replayed", "replayed"), ("_mutation_noop", "unchanged")):
            if obj.get(flag):
                lines.append(f"{label}: True")
        lines.append(_FULL_DETAIL_LINE)
        return lines
    lines.extend(_flatten(obj, prefix=""))
    return lines


# Keys a mutation result carries as whole identifiers: printed complete, never
# previewed, however long.
_WHOLE_VALUE_SUFFIXES = ("_ref", "_refs", "_id", "_key", "_hash", "_commit")
# A nested value up to this many flattened lines is small enough to show
# whole; a larger one is named and left to --format json.
_NESTED_FIELD_LINES = 8


def _is_item_record(value: Mapping[str, Any]) -> bool:
    """A plan work item: its key, its identity and a revision.

    A materialized item names its revision ``revision``; the compact item a
    receipt nests names it ``item_revision``.
    """

    return (
        _present(value.get("item_key"))
        and _present(value.get("identity_ref") or value.get("item_ref"))
        and ("revision" in value or "item_revision" in value)
    )


def _mutated_item_lines(label: str, item: Mapping[str, Any]) -> list[str]:
    """A mutated item by its coordinates: key, status, title, refs, revision."""

    title = _preview(item.get("title"), maximum_bytes=220) or "(untitled)"
    lines = [
        "{}: {} · {} · {}".format(
            label, item.get("item_key") or "-", item.get("status") or "-", title
        )
    ]
    for key in ("identity_ref", "item_ref", "work_ref"):
        if _present(item.get(key)):
            lines.append(f"{label}.{key}: {item[key]}")
    lines.append(
        "{}: revision {} · assignee {} · reviewer {} · updated {}".format(
            label,
            item.get("revision", item.get("item_revision", "?")),
            item.get("assignee") or "-",
            item.get("reviewer") or "-",
            item.get("updated_at") or item.get("item_updated_at") or "-",
        )
    )
    return lines


def _mutated_assignment_lines(assignment: Mapping[str, Any]) -> list[str]:
    """An assignment a mutation touched, by its state and coordinates."""

    lines = [
        "assignment: state {} · ownership {} · worker {} · updated {}".format(
            assignment.get("state") or "-",
            assignment.get("ownership_version", "?"),
            assignment.get("worker_name") or "-",
            assignment.get("updated_at") or "-",
        )
    ]
    for key in ("assignment_ref", "ref", "current_control_ref"):
        if _present(assignment.get(key)):
            lines.append(f"assignment.{key}: {assignment[key]}")
    return lines


def _mutation_fields(fields: Mapping[str, Any]) -> list[str]:
    """A mutation result's own fields, bounded, as ``key = value``.

    Outcome fields (applied, changed, revisions, statuses) and every ref are
    printed whole. A nested item or assignment is shown by its coordinates.
    An error, and any small nested value, is shown whole. A large one (an
    item body, a task, observed files) is named, and --format json has it.
    """

    lines: list[str] = []
    omitted: list[str] = []
    for key, value in fields.items():
        name = str(key)
        if name.startswith("_mutation_"):
            continue
        if name in ("item", "work_item") and isinstance(value, Mapping) and value:
            if _is_item_record(value):
                lines.extend(_mutated_item_lines(name, value))
            else:
                omitted.append(name)
            continue
        if name == "assignment" and isinstance(value, Mapping):
            if value:
                lines.extend(_mutated_assignment_lines(value))
                omitted.append("assignment detail")
            continue
        if isinstance(value, (Mapping, list, tuple)):
            flattened = _flatten(value, prefix="", path=name)
            # An error is the reason for the outcome: always whole.
            if name == "error" or len(flattened) <= _NESTED_FIELD_LINES:
                lines.extend(flattened)
            else:
                omitted.append(name)
            continue
        if isinstance(value, str) and not name.endswith(_WHOLE_VALUE_SUFFIXES):
            lines.append(f"{name} = {_preview(value, maximum_bytes=_LONG_PREVIEW_BYTES)}")
            continue
        lines.append(f"{name} = {value}")
    if omitted:
        lines.append("not shown in brief: " + ", ".join(omitted))
        lines.append(_FULL_DETAIL_LINE)
    return lines


def _recovery_lines(recovery: Any) -> list[str]:
    """The relay's record of this request's delivery (W404), when it kept one."""

    if not isinstance(recovery, Mapping) or not recovery:
        return []
    line = "recovery: {} · source {}".format(
        recovery.get("state") or "-", recovery.get("source") or "-"
    )
    if _present(recovery.get("first_sent_at")):
        line += f" · first sent {recovery['first_sent_at']}"
    lines = [line]
    if _present(recovery.get("idempotency_key")):
        lines.append(f"recovery.idempotency_key: {recovery['idempotency_key']}")
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
        key = f"reply-{message_id}" if message_id else "reply-<message_id>"
        # The delivered project selects the writer's route, even for operator replies.
        lines.append(
            "reply: "
            + _cmd(
                [
                    "pb", "worker", "send", *scope,
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


def _flatten(value: Any, *, prefix: str, path: str = "", withheld: bool = False) -> list[str]:
    """Every leaf as ``path = value``, complete. Nothing is skipped or cut.

    The one exception is a signed link: every leaf under a ``download_url``,
    ``upload_url`` or ``signed_url`` key, whatever its shape, prints as withheld.
    """
    lines: list[str] = []
    if isinstance(value, Mapping):
        if not value:
            lines.append(f"{prefix}{path or '(object)'} = {{}}")
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            lines.extend(_flatten(child, prefix=prefix, path=child_path,
                                  withheld=withheld or str(key) in _SIGNED_LINK_KEYS))
    elif isinstance(value, list):
        if not value:
            lines.append(f"{prefix}{path or '(list)'} = []")
        for index, child in enumerate(value):
            lines.extend(_flatten(child, prefix=prefix, path=f"{path}[{index}]", withheld=withheld))
    else:
        if withheld and value not in (None, ""):
            value = _SIGNED_LINK_WITHHELD
        if isinstance(value, str) and "\n" in value:
            lines.append(f"{prefix}{path} =")
            lines.extend(f"{prefix}{_BODY_INDENT}{line}" for line in value.splitlines())
        else:
            lines.append(f"{prefix}{path} = {value}")
    return lines


# A signed link is a short-lived credential: whoever holds it can download the
# file. Brief output never prints one (W472, Infra 2026-10-03). The attachment
# read command and the file ref are the way to the file; --format json keeps
# the stored value for diagnosis.
_SIGNED_LINK_KEYS = frozenset({"download_url", "upload_url", "signed_url"})
_SIGNED_QUERY_NAME = re.compile(
    r"(sig|signature|token|credential|secret|key|auth|session|policy|hmac|expires|x-amz-|x-goog-)",
    re.IGNORECASE,
)
_SIGNED_LINK_WITHHELD = "(signed link withheld; use the attachment read command)"


_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)


def _withhold_signed_urls(text: str) -> str:
    """Rendered text with the query and fragment of every signed http(s) link withheld.

    Runs over everything a brief view prints (headers, subject, body, payload,
    attachment lines, errors), so a link in prose is caught as well as one in
    a field. It parses by hand, never with ``urlsplit``: a malformed link is
    still found and withheld, and can never stop the view from rendering.
    """

    def replace(match: re.Match[str]) -> str:
        url = match.group(0)
        cut = min((i for i in (url.find("?"), url.find("#")) if i >= 0), default=-1)
        if cut < 0:
            return url
        names = [
            unquote(part.split("=", 1)[0])
            for part in re.split(r"[?#&;]", url[cut:])
            if part
        ]
        if not any(_SIGNED_QUERY_NAME.search(name) for name in names):
            return url
        return f"{url[:cut]}?(signed query withheld: {len(names)} parameters)"

    return _URL_IN_TEXT.sub(replace, text)


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

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
import shlex
from typing import Any, Iterable, Mapping, Sequence

from project_board.client.limit_state import limit_state_line, usage_windows_line

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


def _render_worker_context(result: Mapping[str, Any]) -> list[str]:
    """The current coordinates and decision facts, not every cached field.

    Keep the dotted repository lines stable: the public add-a-worker-host
    procedure consumes those lines when it audits deploy keys. Everything
    displayed here is current local evidence from the command invocation; the
    JSON form retains the complete context record.
    """

    lines = [f"context: {result.get('project_ref') or '-'}"]
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

    coordinator = result.get("coordinator")
    if isinstance(coordinator, Mapping):
        lines.append(
            "coordinator: state {} · acting {} · revision {}".format(
                coordinator.get("state") or "unknown",
                coordinator.get("acting"),
                coordinator.get("revision", 0),
            )
        )
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

    team, team_count = _bounded(result.get("team") or [])
    lines.append(f"team: {team_count}")
    for index, member in enumerate(team):
        if not isinstance(member, Mapping):
            continue
        alias = str(member.get("worker_alias") or "-")
        name = str(member.get("worker_name") or "-")
        lines.append(
            "--- team member {}: {} ({}) · role {} · runtime {} · host {} · pool {} · presence {}".format(
                index + 1,
                alias,
                name,
                member.get("role") or "worker",
                member.get("runtime_kind") or "-",
                member.get("host_label") or member.get("host_id") or "-",
                member.get("pool_status") or "-",
                member.get("presence") or "-",
            )
        )
        lines[-1] += (
            f" · {_runtime_model_brief(member)} · {_runtime_account_brief(member)}"
        )
        # Stable worker names are addresses; keep them on their own copyable line.
        lines.append(f"team[{index}].worker_name = {name}")
        if _present(member.get("info_text")):
            lines.append(
                f"team[{index}].info_text = {_preview(member['info_text'])}"
            )
    _note_omitted(lines, "team members", shown=len(team), total=team_count)

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
        commands, command_count = _bounded(identity.get("commands") or [])
        for command in commands:
            lines.append(f"  command: {command}")
        _note_omitted(
            lines,
            "commit identity commands",
            shown=len(commands),
            total=command_count,
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
    if team:
        lines.extend(_team_usage_lines(team))
    return lines


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


def _payload_without_body_copies(payload: Any, body: Any) -> Any:
    """The payload with each prose copy of the body named instead of repeated (W393).

    A review notice carries its whole task as the body and again as
    ``payload.command.instructions``, so brief output printed it twice. A prose
    copy that equals the body, ignoring only leading and trailing whitespace,
    is printed as one line that says so, with its size and a hash prefix shared
    with the body. Any other text, a near copy included, stays whole.

    Identifiers are never replaced, even when the body is only a ref: a field
    named like a ref, id, key, hash, commit or path keeps its value whole, so
    it stays directly copyable. Prose means text with whitespace in it. The
    JSON output is untouched.
    """

    text = str(body or "").strip()
    if not text or not any(character.isspace() for character in text):
        return payload
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    marker = f"(identical to the body above: {len(text.encode('utf-8'))} bytes, sha256 {digest})"

    def replace(value: Any, key: str) -> Any:
        if isinstance(value, Mapping):
            return {name: replace(child, str(name)) for name, child in value.items()}
        if isinstance(value, list):
            return [replace(child, key) for child in value]
        if (
            isinstance(value, str)
            and not _is_locator_key(key)
            and value.strip() == text
        ):
            return marker
        return value

    return replace(payload, "")


# Payload keys whose values are identifiers a reader copies, never prose.
_LOCATOR_SUFFIXES = ("ref", "refs", "id", "ids", "key", "hash", "commit", "path", "url", "uri")


def _is_locator_key(key: str) -> bool:
    name = key.lower()
    return any(name == suffix or name.endswith(f"_{suffix}") for suffix in _LOCATOR_SUFFIXES)


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
            "{}: state {} · attempts {} · schedule {} · next attempt {}".format(
                label,
                connection.get("state") or "not reported",
                connection.get("attempts", "?"),
                _preview(connection.get("schedule"), maximum_bytes=100) or "-",
                connection.get("next_attempt_at") or "not reported",
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
    )


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


def _team_usage_lines(team: Sequence[Any]) -> list[str]:
    """One line per teammate with its limit and usage windows (W351).

    `pb worker context` is the only cross-host view a worker's CLI has; the
    coordinator routes by these figures (the operator's per-pool caps).
    """

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
        else:
            status = "not reported"
        lines.append(f"  {label}{where}: {status}")
    return lines


_RECEIPT_OUTCOMES = ("applied", "refused")


def _latest_actionable_review_return(
    item: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    if str(item.get("status") or "").strip().lower() in {"cancelled", "done"}:
        return None
    history = [
        entry
        for entry in item.get("review_history") or []
        if isinstance(entry, Mapping)
    ]
    if not history:
        return None
    _index, latest = max(
        enumerate(history),
        key=lambda pair: (
            str(pair[1].get("timestamp") or pair[1].get("created_at") or ""),
            pair[0],
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
    if _present(item.get("summary")):
        lines.append(
            f"summary: {_preview(item['summary'], maximum_bytes=_LONG_PREVIEW_BYTES)}"
        )
    if _present(item.get("description")):
        lines.append(
            f"description preview: {_preview(item['description'], maximum_bytes=_LONG_PREVIEW_BYTES)}"
        )
    acceptance, acceptance_count = _bounded(
        item.get("acceptance") or [], maximum=5
    )
    for value in acceptance:
        lines.append(f"acceptance preview: {_preview(value)}")
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
            lines.append(f"{key}: {_preview(item[key], maximum_bytes=_LONG_PREVIEW_BYTES)}")
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
                    f"review.{key}: {_preview(_joined(value), maximum_bytes=_LONG_PREVIEW_BYTES)}"
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
        for key in (
            "assignment_ref",
            "identity_ref",
            "work_ref",
            "versioned_work_ref",
            "control_ref",
        ):
            if _present(assignment.get(key)):
                lines.append(f"assignment.{key}: {assignment[key]}")
    attachments, attachment_count = _bounded(
        item.get("attachments") or item.get("attachment_refs") or [],
        maximum=_BRIEF_REFS,
    )
    for attachment in attachments:
        if isinstance(attachment, str):
            lines.append(f"attachment_ref: {attachment}")
            continue
        if not isinstance(attachment, Mapping):
            continue
        if _present(attachment.get("file_ref")):
            lines.append(f"attachment.file_ref: {attachment['file_ref']}")
        if _present(attachment.get("filename")):
            lines.append(f"attachment.filename: {attachment['filename']}")
    _note_omitted(
        lines,
        "attachments",
        shown=len(attachments),
        total=attachment_count,
    )
    if isinstance(item.get("reference_error"), Mapping):
        lines.extend(_flatten(item["reference_error"], prefix="reference_error."))
    lines.append(_FULL_DETAIL_LINE)
    return lines


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
    _note_omitted(
        lines, "note previews", shown=min(len(notes), _BRIEF_SECTION_ITEMS), total=len(notes)
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
        lines.append(
            "worker {} · assigned {} · updated {}".format(
                assignment.get("worker_name") or "-",
                assignment.get("assigned_at") or "-",
                assignment.get("updated_at") or "-",
            )
        )
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

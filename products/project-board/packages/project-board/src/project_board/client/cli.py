from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import signal
import sys
import time
from contextlib import asynccontextmanager
from importlib import metadata
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from ..contract.errors import DomainError
from ..contract.operation_outcomes import require_successful_operation_envelope
from ..contract.plan_nodes import plan_node_identity_ref
from ..contract.reference_artifacts import (
    REFERENCE_MAPPING_ARTIFACT_SCHEMA,
    decode_reference_mapping_document,
    encode_reference_mapping_document,
)
from ..contract.refs import normalize_reference_mapping, parse_ref
from .agent_session import resolve_agent_session
from .authorization import (
    PROFILE_METADATA_ABSENT,
    authorization_command,
    authorize_worker_profile,
    inspect_profile_metadata,
)
from .host_config import (
    HOST_CONFIG_SCHEMA,
    HostRelayConfig,
    enroll_worker_channel,
    initialize_host_config,
    resolve_host_config_path,
    update_host_config,
)
from .diagnostics import host_relay_diagnostics
from .journals import JournalWorkspace, RepositoryMap, parse_source_repositories
from .journal_operations import JournalIndexWorkflow
from .plan_authority import require_plan_item
from .prose_arguments import (
    guard_inline_payload_prose,
    guard_inline_prose,
    refuse_unresolved_payload_slots,
    refuse_unresolved_slots,
)
from .commands import load_json
from .relay_pacing import channel_reconnect_state
from .coordinate_queue import (
    DEFAULT_COORDINATE_TIMEOUT_SECONDS,
    CoordinateQueue,
)
from .io import atomic_write_json, content_hash, new_id, read_json, utc_now
from .mail_attachments import worker_message_with_attachments
from .outbox_outcomes import (
    DEFAULT_OUTBOX_WAIT_SECONDS as DEFAULT_REPORT_WAIT_SECONDS,
    await_outbox_outcome as _await_outbox_outcome,
    submit_assignment_report,
)
from .quarantine import list_quarantine, read_quarantine, settle_quarantine
from ..contract.worker_identity import WorkerSessionIdentity
from .card_refusal import with_actionable_refusal
from .stop_guard import stop_guard_decision
from .worker_watch import worker_watch_events
from .limit_state import (
    limit_state_from_claude_statusline,
    limit_state_from_claude_stop_failure,
    limit_state_line,
)
from .render import (
    FORMATS,
    FORMAT_BRIEF,
    render_envelope,
    render_text,
    select_format,
    worker_flags_from_argv,
)
from .relay_faults import (
    DEFAULT_RELAY_FAULT_TTL_SECONDS,
    INJECTABLE_RELAY_FAULT_CODES,
    clear_relay_fault,
    inject_relay_fault,
    pending_relay_faults,
)
from .session import (
    listen_worker_input,
    probe_worker_input,
    pull_worker_input,
)
from .store import SharedFieldStore
from .commands import execute


def _tcp_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "callback port must be an integer between 1 and 65535"
        ) from exc
    if not 1 <= port <= 65_535:
        raise argparse.ArgumentTypeError(
            "callback port must be between 1 and 65535"
        )
    return port


def _field(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--field", required=True, help="Root of the worker-shared project field.")


def _project(parser: argparse.ArgumentParser) -> None:
    _field(parser)
    parser.add_argument("--project-id", required=True)


def _actor(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--actor", required=True)


def _worker(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--worker", required=True)


def _journal_workspace(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--journal-workspace",
        required=True,
        help="LOCAL user-wide journal aggregate; links and index here are rebuildable.",
    )
    parser.add_argument(
        "--source-repo",
        action="append",
        required=True,
        help="Private LOCAL repository map NAME=/absolute/checkout/path; repeat as needed.",
    )
    parser.add_argument(
        "--source-repo-url",
        action="append",
        default=[],
        help="ALIAS=https://... remote URL for a mapped repository alias; the board links repo:ALIAS refs to it.",
    )


def _agent_identity(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--runtime-kind",
        choices=("codex", "claude-code"),
        help="Detected for Codex; Claude Code supplies claude-code explicitly.",
    )
    parser.add_argument(
        "--runtime-session-id",
        "--session-id",
        dest="runtime_session_id",
        help="Native resumable session id; Codex detects CODEX_SESSION_ID.",
    )


def _host_config(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        help="Host relay config; defaults to this user's selected Problem Board target.",
    )


def _project_board_version() -> str:
    try:
        return metadata.version("project-board")
    except metadata.PackageNotFoundError:
        return "source"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="problem-board", description="Operate a Problem Board shared field.")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {_project_board_version()}",
    )
    parser.add_argument(
        "--format",
        choices=FORMATS,
        default=None,
        help=(
            "Output form for any command, accepted anywhere on the command line: "
            "json (default, one envelope) or brief (complete readable text, refs "
            "whole, errors on stdout). PB_FORMAT sets the default."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser(
        "status",
        help=(
            "Show which first-run step this machine and session are at, and the "
            "next one. Read-only, and works before anything is configured."
        ),
    )
    _host_config(status)
    status.add_argument(
        "--runtime-kind",
        choices=("codex", "claude-code"),
        default="",
        help="This agent session's runtime, to include its own enrollment and projects.",
    )
    status.add_argument(
        "--runtime-session-id",
        "--session-id",
        dest="runtime_session_id",
        default="",
        help="This agent session's native resumable id.",
    )

    setup = sub.add_parser(
        "setup", help="Create one machine-scoped Problem Board relay and local field."
    )
    setup.add_argument("--target-id", required=True)
    setup.add_argument("--endpoint", required=True)
    setup.add_argument("--tenant", required=True)
    setup.add_argument("--platform-project", required=True)
    setup.add_argument("--host-id", default="")
    setup.add_argument("--host-label", default="Local machine")
    setup.add_argument(
        "--allow-root",
        action="append",
        required=True,
        help="Filesystem root coding sessions may use; repeat for each approved root.",
    )
    setup.add_argument(
        "--source-repo",
        action="append",
        default=[],
        help="Portable repository alias NAME=/absolute/checkout; repeat as needed.",
    )
    setup.add_argument(
        "--source-repo-url",
        action="append",
        default=[],
        help="ALIAS=https://... remote URL for a mapped repository alias; the board links repo:ALIAS refs to it.",
    )
    setup.add_argument("--config")
    setup.add_argument("--state-root")
    setup.add_argument("--connection-hub-state-root")
    setup.add_argument(
        "--idle-reconcile-ceiling",
        "--idle-poll-interval",
        dest="idle_poll_interval",
        type=int,
        default=120,
        help=(
            "Longest a relay cycle may be delayed while a worker attends no "
            "project. A ceiling on waiting, not a polling schedule."
        ),
    )

    host = sub.add_parser(
        "host", help="Inspect or revise this machine's non-secret relay configuration."
    )
    host_commands = host.add_subparsers(dest="host_command", required=True)
    command = host_commands.add_parser("inspect", help="Show the selected host configuration.")
    _host_config(command)
    command = host_commands.add_parser(
        "probe-worker",
        help="Prove one worker's inbox, wake, receive, and settlement path.",
    )
    _host_config(command)
    command.add_argument("--worker", required=True)
    command.add_argument("--timeout-seconds", type=float, default=180.0)
    command.add_argument("--poll-seconds", type=float, default=0.5)
    command = host_commands.add_parser(
        "configure", help="Apply a reviewed local-root or receiver-policy revision."
    )
    _host_config(command)
    command.add_argument(
        "--endpoint",
        default="",
        help=(
            "Replace the governed Problem Board service endpoint; rotates every "
            "resource-scoped worker profile and requires authorization again."
        ),
    )
    command.add_argument(
        "--connection-hub-state-root",
        help=(
            "Select the one host-scoped Connection Hub profile metadata store; "
            "credentials remain in native custody."
        ),
    )
    command.add_argument("--host-label", default="")
    command.add_argument("--add-allow-root", action="append", default=[])
    command.add_argument("--set-source-repo", action="append", default=[])
    command.add_argument(
        "--set-source-repo-url",
        action="append",
        default=[],
        help="ALIAS=https://... remote URL the board links for repo:ALIAS refs.",
    )
    command.add_argument("--allow-control-kind", action="append")
    peers = command.add_mutually_exclusive_group()
    peers.add_argument("--allow-peer-worker", action="append")
    peers.add_argument("--deny-all-peers", action="store_true")
    command.add_argument("--max-control-bytes", type=int)
    command.add_argument(
        "--allow-session-resume-view",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Allow an owner to request an expiring host-generated resume command.",
    )
    command.add_argument(
        "--reconcile-ceiling",
        "--poll-interval",
        dest="poll_interval",
        type=int,
        help=(
            "Longest the relay may go without a reconciliation cycle when no "
            "push arrives. A ceiling on waiting, not a polling schedule."
        ),
    )
    command.add_argument(
        "--idle-reconcile-ceiling",
        "--idle-poll-interval",
        dest="idle_poll_interval",
        type=int,
        help=(
            "Longest a relay cycle may be delayed while a worker attends no "
            "project."
        ),
    )
    command.add_argument(
        "--create-missing-journal-home",
        action=argparse.BooleanOptionalAction,
        default=None,
    )

    relay_fault = host_commands.add_parser(
        "relay-fault",
        help="Exercise one retryable worker-channel failure during a coordinated live test.",
    )
    relay_fault_commands = relay_fault.add_subparsers(
        dest="relay_fault_command", required=True
    )
    command = relay_fault_commands.add_parser(
        "inject",
        help="Arm one expiring, one-shot failure for an active worker channel.",
    )
    _host_config(command)
    command.add_argument("--worker", required=True)
    command.add_argument(
        "--code",
        required=True,
        choices=sorted(INJECTABLE_RELAY_FAULT_CODES),
    )
    command.add_argument(
        "--ttl-seconds",
        type=int,
        default=DEFAULT_RELAY_FAULT_TTL_SECONDS,
    )
    command.add_argument(
        "--confirm-live-interruption",
        action="store_true",
        help="Confirm that the named worker channel may be interrupted once.",
    )
    command = relay_fault_commands.add_parser(
        "status",
        help="Show unconsumed host-local relay faults.",
    )
    _host_config(command)
    command.add_argument("--worker", default="")
    command = relay_fault_commands.add_parser(
        "clear",
        help="Disarm one fault before the relay consumes it.",
    )
    _host_config(command)
    command.add_argument("--worker", required=True)

    worker = sub.add_parser(
        "worker", help="Operate the coding-agent session invoking this command."
    )
    worker_commands = worker.add_subparsers(dest="worker_command", required=True)

    coordinate = sub.add_parser(
        "coordinate",
        help="Call one Problem Board operation with this session's own card.",
        description=(
            "Invoke a canonical Problem Board operation against the governed "
            "endpoint using the card this session already holds. The operation "
            "ids are the ones the catalog publishes, so there is no second "
            "namespace to learn: worker.retire is worker.retire on every surface."
        ),
    )
    coordinate.add_argument(
        "action",
        help="Canonical operation id, for example worker.retire or project.register.",
    )
    coordinate.add_argument(
        "--object-ref",
        default="",
        help="The object the operation acts on, when it takes one.",
    )
    coordinate.add_argument("--payload-json", default="", help="Inline JSON object payload.")
    coordinate.add_argument("--payload-file", default="", help="File holding a JSON object payload, or - for stdin.")
    coordinate.add_argument(
        "--route",
        choices=("relay", "direct"),
        default="relay",
        help=(
            "Use this worker's persistent relay channel by default; direct keeps "
            "the credential-bearing in-process path for an unrestricted terminal."
        ),
    )
    coordinate.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_COORDINATE_TIMEOUT_SECONDS,
        help="Maximum time to wait for the worker relay response (1..600 seconds).",
    )
    _agent_identity(coordinate)
    _host_config(coordinate)

    plan = sub.add_parser(
        "plan",
        help="Migrate, compare, or sync the canonical project plan.",
    )
    plan_commands = plan.add_subparsers(dest="plan_command", required=True)
    command = plan_commands.add_parser(
        "migrate",
        help="Normalize plan storage while preserving minted canonical node URIs.",
    )
    _host_config(command)
    command.add_argument("--project-ref", required=True)
    command.add_argument(
        "--migration-id",
        default="",
        help="Stable retry ID; required with --restore-ref.",
    )
    command.add_argument(
        "--restore-ref",
        action="append",
        default=[],
        metavar="CURRENT=ESTABLISHED",
        help=(
            "Restore a title-reminted canonical URI while retaining its creation "
            "timestamp and key; repeat for each node."
        ),
    )
    command.add_argument(
        "--expected-project-revision",
        type=int,
        help="Required project revision fence when --restore-ref is used.",
    )
    command.add_argument(
        "--confirm-cutover",
        action="store_true",
        help="Confirm that plan writes are paused and the PostgreSQL publisher is ready.",
    )
    command = plan_commands.add_parser(
        "cutover-local",
        help="Preview or commit the guarded local-plan recovery into PostgreSQL.",
    )
    _host_config(command)
    _agent_identity(command)
    command.add_argument("--project-ref", required=True)
    cutover_mode = command.add_mutually_exclusive_group()
    cutover_mode.add_argument(
        "--preview",
        action="store_true",
        help="Show every URI rewrite and recovered assignment outcome without writing.",
    )
    cutover_mode.add_argument(
        "--confirm-cutover",
        action="store_true",
        help="Commit the exact operator-reviewed preview into PostgreSQL.",
    )
    command.add_argument(
        "--expected-cutover-hash",
        default="",
        help="Exact cutover_content_hash returned by --preview.",
    )
    command.add_argument(
        "--reviewed-preview",
        default="",
        metavar="FILE",
        help="JSON output from the exact --preview invocation the operator reviewed.",
    )
    command.add_argument(
        "--expected-plan-revision",
        type=int,
        help="Current PostgreSQL plan revision that the recovered plan may replace.",
    )
    command.add_argument(
        "--idempotency-key",
        default="",
        help="Stable retry key; reuse it unchanged after an uncertain response.",
    )

    references = sub.add_parser(
        "references",
        help="Preview or apply one project-wide durable-reference migration.",
    )
    reference_commands = references.add_subparsers(
        dest="references_command",
        required=True,
    )
    command = reference_commands.add_parser(
        "preview",
        help="Show affected PostgreSQL rows and exact local and tracked-journal rewrites without writing.",
    )
    _host_config(command)
    _agent_identity(command)
    command.add_argument("--project-ref", required=True)
    command = reference_commands.add_parser(
        "migrate",
        help="Apply one reviewed preview across PostgreSQL, the local field, and Git.",
    )
    _host_config(command)
    _agent_identity(command)
    command.add_argument("--project-ref", required=True)
    command.add_argument(
        "--reviewed-preview",
        required=True,
        metavar="FILE",
        help="JSON output from the exact references preview invocation reviewed by the operator.",
    )
    command.add_argument(
        "--expected-preview-hash",
        required=True,
        help="The preview_hash printed by the reviewed preview.",
    )
    command.add_argument(
        "--idempotency-key",
        required=True,
        help="Stable retry key; reuse it unchanged after an interrupted migration.",
    )
    for name, help_text in (
        ("drift", "Compare the Git export with the complete PostgreSQL plan index."),
        ("sync", "Write and commit the complete PostgreSQL plan index to Git."),
    ):
        command = plan_commands.add_parser(name, help=help_text)
        _host_config(command)
        _agent_identity(command)
        command.add_argument("--project-ref", required=True)
        if name == "sync":
            command.add_argument(
                "--author",
                default="",
                help="Optional Git author in 'Name <email>' form.",
            )
    command = plan_commands.add_parser(
        "import",
        help="Validate a Git plan package and atomically write it to PostgreSQL.",
    )
    _host_config(command)
    _agent_identity(command)
    command.add_argument("--project-ref", required=True, help="The PostgreSQL target project.")
    command.add_argument(
        "--from",
        dest="source",
        default="",
        help="Journal home containing plan/index.json; defaults to this project's bound journal home.",
    )
    command.add_argument(
        "--rebind-project",
        action="store_true",
        help="Allow an export from another project to be imported into --project-ref.",
    )
    command.add_argument(
        "--expected-plan-revision",
        required=True,
        type=int,
        help="Current PostgreSQL plan revision that this import may replace.",
    )
    command.add_argument(
        "--idempotency-key",
        required=True,
        help="Stable retry key; reuse it unchanged after an uncertain response.",
    )

    procedure = sub.add_parser(
        "procedure",
        help="Inspect, install, or verify the Problem Board worker skill package.",
    )
    procedure_commands = procedure.add_subparsers(
        dest="procedure_command", required=True
    )
    command = procedure_commands.add_parser(
        "show", help="Show the canonical package and installed version state."
    )
    command.add_argument(
        "--target",
        action="append",
        choices=("codex", "claude-code"),
        default=[],
    )
    command.add_argument("--home", help="Testing or alternate user home.")
    command = procedure_commands.add_parser(
        "verify", help="Verify every file in an installed procedure package."
    )
    command.add_argument(
        "--target",
        action="append",
        choices=("codex", "claude-code"),
        default=[],
    )
    command.add_argument("--home", help="Testing or alternate user home.")
    command = procedure_commands.add_parser(
        "install", help="Install the complete package as a local coding-agent skill."
    )
    command.add_argument(
        "--target",
        action="append",
        choices=("codex", "claude-code"),
        required=True,
    )
    command.add_argument("--home", help="Testing or alternate user home.")
    command.add_argument("--force", action="store_true")
    command.add_argument(
        "--allow-downgrade",
        action="store_true",
        help="Install a package older than the installed revision on purpose.",
    )

    command = worker_commands.add_parser(
        "whoami", help="Show this runtime's stable Problem Board identity."
    )
    _agent_identity(command)

    command = worker_commands.add_parser(
        "list", help="List this host's workers with local relay diagnostics."
    )
    _host_config(command)

    command = worker_commands.add_parser(
        "listen", help="Enroll this exact session and begin its inbox lifecycle."
    )
    _host_config(command)
    _agent_identity(command)
    command.add_argument("--alias", default="")
    command.add_argument("--capability", action="append", default=[])
    command.add_argument("--check-interval", type=int, default=30)

    command = worker_commands.add_parser(
        "authorize",
        help="Authorize one enrolled worker profile from the user's interactive terminal.",
    )
    _host_config(command)
    command.add_argument("profile")
    command.add_argument("--wait-seconds", type=float, default=600.0)
    command.add_argument("--no-open", action="store_true")
    command.add_argument(
        "--device",
        action="store_true",
        help="Authorize in a browser on another device without a callback listener.",
    )
    command.add_argument(
        "--callback-port",
        type=_tcp_port,
        help="Fixed loopback callback port, including for an SSH-forwarded browser.",
    )
    command.add_argument(
        "--replace-card",
        action="store_true",
        help="Revoke the recorded Card and authorize a new one.",
    )
    command.add_argument(
        "--coordinator",
        action="store_true",
        help=(
            "Request the descriptor's coordinator profile for a first or "
            "replacement Card."
        ),
    )

    command = worker_commands.add_parser(
        "receive", help="Receive and lease this worker's available direct and project mail."
    )
    _host_config(command)
    _agent_identity(command)
    command.add_argument("--limit", type=int, default=5)
    command.add_argument("--lease-seconds", type=int, default=1800)
    command.add_argument(
        "--wake-id",
        default="",
        help="Acknowledge the exact automatic Codex wake that requested this receive.",
    )

    command = worker_commands.add_parser(
        "quarantine", help="Inspect or resolve this worker's held mail."
    )
    _host_config(command)
    _agent_identity(command)
    quarantine_commands = command.add_subparsers(dest="quarantine_command", required=True)
    command = quarantine_commands.add_parser("list", help="Page held messages and reasons.")
    command.add_argument("--cursor", default="")
    command.add_argument("--limit", type=int, default=20)
    for action in ("read", "release", "discard"):
        command = quarantine_commands.add_parser(action, help=f"{action.title()} one held message.")
        command.add_argument("--project-ref", default="")
        command.add_argument("--message-ref", required=True)
        if action in {"release", "discard"}:
            command.add_argument("--expected-quarantined-at", required=True)
        if action == "discard":
            command.add_argument("--reason", required=True)

    command = worker_commands.add_parser(
        "leases",
        help="Page the active mail leases held by this exact worker session.",
    )
    _host_config(command)
    _agent_identity(command)
    command.add_argument("--cursor", default="")
    command.add_argument("--limit", type=int, default=20)

    command = worker_commands.add_parser(
        "lease-read",
        help="Re-read one active mail lease held by this exact worker session.",
    )
    _host_config(command)
    _agent_identity(command)
    command.add_argument(
        "--project-ref",
        default="",
        help="The project ref from worker leases; omit for direct mail.",
    )
    command.add_argument("--message-ref", required=True)
    command.add_argument("--lease-id", required=True)

    command = worker_commands.add_parser(
        "attachment-read",
        help="Resolve one attached file from a lease held by this exact worker session.",
    )
    _host_config(command)
    _agent_identity(command)
    command.add_argument(
        "--project-ref",
        default="",
        help="The project ref from the attachment read command; omit for direct mail.",
    )
    command.add_argument("--message-ref", required=True)
    command.add_argument("--lease-id", required=True)
    command.add_argument("--file-ref", required=True)

    command = worker_commands.add_parser(
        "item-attach", help="Attach a local file to one current work item."
    )
    _host_config(command)
    _agent_identity(command)
    command.add_argument("--project-ref", required=True)
    command.add_argument("--item-key", required=True)
    command.add_argument("--file", required=True)

    command = worker_commands.add_parser(
        "item-attachment-read", help="Download one file listed on a work item."
    )
    _host_config(command)
    _agent_identity(command)
    command.add_argument("--project-ref", required=True)
    command.add_argument("--item-key", required=True)
    command.add_argument("--file-ref", required=True)
    command.add_argument("--output", required=True)

    command = worker_commands.add_parser(
        "renew",
        help="Extend one active mail lease held by this exact worker session.",
    )
    _host_config(command)
    _agent_identity(command)
    command.add_argument(
        "--project-ref",
        default="",
        help="The project ref from worker leases; omit for direct mail.",
    )
    command.add_argument("--message-ref", required=True)
    command.add_argument("--lease-id", required=True)
    command.add_argument("--lease-seconds", type=int, default=1800)
    command.add_argument("--note", default="")

    command = worker_commands.add_parser(
        "watch", help="Emit notification-only inbox availability without leasing mail."
    )
    _host_config(command)
    _agent_identity(command)
    command.add_argument("--check-interval", type=int, default=30)
    command.add_argument("--coalesce-seconds", type=float, default=1.0)
    command.add_argument(
        "--once", action="store_true", help="Probe once instead of staying attached."
    )

    command = worker_commands.add_parser(
        "detach", help="Mark this user-started session as no longer listening."
    )
    _host_config(command)
    _agent_identity(command)

    command = worker_commands.add_parser(
        "busy-until",
        help=(
            "State until when (UTC) this worker expects to finish what it is on, "
            "in one line, so the board can show it and mark it overdue. Set it "
            "after planning, set it again with the reason when it slips, clear "
            "it with --clear when the work is done."
        ),
    )
    _host_config(command)
    _agent_identity(command)
    command.add_argument(
        "until",
        nargs="?",
        default="",
        help="ISO-8601 UTC instant, for example 2026-09-23T21:30Z.",
    )
    command.add_argument("--note", default="", help="One line naming the work, required with a time.")
    command.add_argument("--clear", action="store_true", help="Clear the estimate: the work is done.")

    command = worker_commands.add_parser(
        "limit-state",
        help=(
            "Record what the coding-agent runtime itself says about its usage "
            "limit (W26). Claude Code runs this as its statusLine command and as "
            "its StopFailure hook for rate_limit: the JSON arrives on stdin, the "
            "state is recorded for the relay, and one status line is printed."
        ),
    )
    _host_config(command)
    _agent_identity(command)
    command.add_argument(
        "--source",
        default="statusline",
        choices=("statusline", "stop-failure"),
        help="Which Claude Code surface is calling: the status line (default) or the StopFailure hook.",
    )
    command.add_argument(
        "--payload-file",
        default="-",
        help="The JSON Claude Code passed, a file or - for stdin (default).",
    )

    command = worker_commands.add_parser(
        "stop-guard",
        help=(
            "Claude Code's Stop hook (W182): when an attending worker's turn ends with "
            "no pb worker watch running, block the stop once with the commands that "
            "re-arm it. Reads the hook JSON on stdin; never blocks anything else."
        ),
    )
    _host_config(command)

    command = worker_commands.add_parser(
        "workspace",
        help=(
            "Declare where on this host you edit one repository for one assignment, so the "
            "relay can publish the tracked files you have in flight (W278). Local only. "
            "--list shows the declarations, --clear forgets them."
        ),
    )
    _host_config(command)
    _agent_identity(command)
    command.add_argument("--assignment-ref", default="", help="The assignment (work:assignment:...) the worktree serves.")
    command.add_argument("--repository", default="", help="The repository ref the assignment binds, for example repo:app-ecosystem/products.")
    command.add_argument("--path", default="", help="The worktree directory on this host.")
    command.add_argument("--clear", action="store_true", help="Forget the declaration(s) for --assignment-ref (and --repository when given).")
    command.add_argument("--list", action="store_true", help="Show this session's declared worktrees.")

    command = worker_commands.add_parser(
        "idle",
        help=(
            "Report that this session has run out of work, naming why. Silence "
            "already means unreachable, so this is said rather than inferred."
        ),
    )
    _host_config(command)
    _agent_identity(command)
    command.add_argument("--project-ref", required=True)
    command.add_argument(
        "--reason",
        required=True,
        choices=("finished", "blocked", "nothing_assigned"),
    )
    command.add_argument(
        "--summary",
        default="",
        help="What you were doing and where it got to, so the thread can be picked up.",
    )
    command.add_argument(
        "--work-ref",
        default="",
        help="The item this session last worked on.",
    )

    command = worker_commands.add_parser(
        "inspect", help="Inspect this session's local enrollment and presence."
    )
    _host_config(command)
    _agent_identity(command)

    command = worker_commands.add_parser(
        "context", help="Resolve one attended project's local source and journal paths."
    )
    _host_config(command)
    _agent_identity(command)
    command.add_argument("--project-ref", required=True)

    command = worker_commands.add_parser(
        "send", help="Send mail as this session-bound worker."
    )
    _host_config(command)
    _agent_identity(command)
    command.add_argument(
        "--project-ref",
        default="",
        help="Shared project context. Omit when writing directly to the operator.",
    )
    command.add_argument(
        "--recipient",
        required=True,
        help="A project teammate, operator for this worker's owner, or coordinator for whoever holds the project's coordinator role now (needs --project-ref).",
    )
    command.add_argument("--kind", required=True)
    command.add_argument("--subject", required=True)
    body = command.add_mutually_exclusive_group()
    body.add_argument("--body")
    body.add_argument("--body-file")
    command.add_argument("--payload-file")
    command.add_argument("--work-ref", default="")
    command.add_argument("--correlation-id", default="")
    command.add_argument("--reply-to", default="")
    command.add_argument("--attach", action="append", default=[], help="File to send with the message (repeatable); only to the operator inbox.")
    command.add_argument("--idempotency-key", required=True)
    command.add_argument("--route", choices=("auto", "local", "remote"), default="auto")

    command = worker_commands.add_parser(
        "deliveries",
        help="Page this worker's durable outbound mail delivery outcomes.",
    )
    _host_config(command)
    _agent_identity(command)
    command.add_argument("--project-ref", default="")
    command.add_argument(
        "--state",
        action="append",
        choices=("pending", "leased", "sent", "ignored", "refused"),
        default=[],
    )
    command.add_argument("--cursor", default="")
    command.add_argument("--limit", type=int, default=20)

    command = worker_commands.add_parser(
        "outbox-status",
        help="Read one worker-owned outbox result by exact id, including plan validation.",
    )
    _host_config(command)
    _agent_identity(command)
    command.add_argument("--outbox-id", required=True)

    command = worker_commands.add_parser(
        "replay-delivery",
        help="Replay one retained refused mail payload to a stable recipient.",
    )
    _host_config(command)
    _agent_identity(command)
    command.add_argument("--outbox-id", required=True)
    command.add_argument("--recipient", required=True)
    command.add_argument(
        "--kind",
        default="",
        help="Correct the retained mail kind without changing the original record.",
    )

    command = worker_commands.add_parser(
        "settle", help="Acknowledge or refuse one message leased by this session."
    )
    _host_config(command)
    _agent_identity(command)
    command.add_argument(
        "--project-ref",
        default="",
        help="The project ref returned with the message; omit for direct mail.",
    )
    command.add_argument("--message-ref", required=True)
    command.add_argument("--lease-id", required=True)
    command.add_argument("--outcome", choices=("acknowledged", "refused"), required=True)
    command.add_argument("--summary", default="")

    command = worker_commands.add_parser(
        "report", help="Report work with the assignment's exact ownership version."
    )
    _host_config(command)
    _agent_identity(command)
    command.add_argument("--project-ref", required=True)
    command.add_argument("--assignment-ref", required=True)
    command.add_argument("--ownership-version", required=True, type=int)
    command.add_argument(
        "--state", choices=("working", "blocked", "completed", "refused"), required=True
    )
    summary_source = command.add_mutually_exclusive_group(required=True)
    summary_source.add_argument("--summary", help="One line. Longer text goes through --summary-file.")
    summary_source.add_argument("--summary-file", help="UTF-8 summary file, or - for stdin.")
    command.add_argument("--result-ref", default="")
    command.add_argument("--source-event-ref", required=True)
    command.add_argument("--review-look-at", help="How to check a completed result.")
    command.add_argument("--review-could-not-verify", help="Remaining gaps, or None explicitly.")
    command.add_argument(
        "--scope",
        default="",
        help=(
            "One line naming the module, path prefixes or runtime surface this work will change. "
            "Set it with the first working report, again only when the boundary grows (W278)."
        ),
    )
    command.add_argument(
        "--wait-seconds",
        type=float,
        default=DEFAULT_REPORT_WAIT_SECONDS,
        help=(
            "How long to wait for the service's terminal answer. A deadline "
            "returns an outcome-unknown error because the report may have applied."
        ),
    )

    project_report = worker_commands.add_parser(
        "project-report",
        help="Publish or fail the exact project report request leased by this session.",
    )
    project_report_commands = project_report.add_subparsers(
        dest="project_report_command", required=True
    )
    command = project_report_commands.add_parser(
        "publish", help="Queue one immutable report document and its attachments."
    )
    _host_config(command)
    _agent_identity(command)
    command.add_argument("--project-ref", required=True)
    command.add_argument("--message-ref", required=True)
    command.add_argument("--lease-id", required=True)
    report_summary = command.add_mutually_exclusive_group(required=True)
    report_summary.add_argument(
        "--summary", help="Human-readable account of where the project stands."
    )
    report_summary.add_argument(
        "--summary-file", help="UTF-8 report summary file, or - for stdin."
    )
    command.add_argument(
        "--attach",
        action="append",
        default=[],
        help="File to preserve with the report; repeat for each file.",
    )
    command.add_argument(
        "--not-seen",
        action="append",
        default=[],
        help="Something you could not see or reach while answering; repeat for each.",
    )
    command.add_argument(
        "--wait-seconds",
        type=float,
        default=DEFAULT_REPORT_WAIT_SECONDS,
        help=(
            "How long to wait for the service's answer before returning. A queued "
            "report is intent, not a receipt. 0 returns at once and says so."
        ),
    )
    command = project_report_commands.add_parser(
        "preview",
        help=(
            "Compose the report on the service and store nothing: the delta since "
            "the predecessor, capped, with the reason each item is in it."
        ),
    )
    _host_config(command)
    _agent_identity(command)
    command.add_argument("--project-ref", required=True)
    command.add_argument("--message-ref", required=True)
    command.add_argument("--lease-id", required=True)
    preview_summary = command.add_mutually_exclusive_group()
    preview_summary.add_argument("--summary")
    preview_summary.add_argument("--summary-file")
    command.add_argument("--not-seen", action="append", default=[])
    command = project_report_commands.add_parser(
        "status", help="Read the terminal outcome of one queued report publication."
    )
    _host_config(command)
    _agent_identity(command)
    command.add_argument("--project-ref", required=True)
    command.add_argument("--message-ref", required=True)
    command.add_argument("--lease-id", required=True)
    command.add_argument("--outbox-id", required=True)
    command = project_report_commands.add_parser(
        "fail", help="Queue the reason this report request could not be completed."
    )
    _host_config(command)
    _agent_identity(command)
    command.add_argument("--project-ref", required=True)
    command.add_argument("--message-ref", required=True)
    command.add_argument("--lease-id", required=True)
    command.add_argument("--error-code", required=True)
    command.add_argument("--error-summary", required=True)

    command = worker_commands.add_parser(
        "journal-index",
        help="Index one Git journal file already authored by this worker.",
    )
    _host_config(command)
    _agent_identity(command)
    command.add_argument("--project-ref", required=True)
    command.add_argument("--repository-journal-ref", required=True)

    command = worker_commands.add_parser(
        "journal-index-resume",
        help="Resume the first incomplete step of a journal-index operation.",
    )
    _host_config(command)
    _agent_identity(command)
    command.add_argument("--operation-id", required=True)

    command = worker_commands.add_parser(
        "journal-index-status",
        help="Read journal-index step outcomes without changing any authority.",
    )
    _host_config(command)
    _agent_identity(command)
    command.add_argument("--project-ref", required=True)
    selector = command.add_mutually_exclusive_group(required=True)
    selector.add_argument("--operation-id")
    selector.add_argument("--outbox-id")
    command.add_argument(
        "--repository-journal-ref",
        default="",
        help="Required only for an outbox created before the operation ledger existed.",
    )

    command = worker_commands.add_parser(
        "journal-search", help="Search the current project's Git journal."
    )
    _host_config(command)
    _agent_identity(command)
    command.add_argument(
        "--project-ref",
        default="",
        help="Defaults to the one project this worker attends.",
    )
    command.add_argument("--query", default="")
    command.add_argument("--work-ref", default="")
    command.add_argument("--author", default="")
    command.add_argument("--status", default="")
    command.add_argument("--limit", type=int, default=20)

    command = sub.add_parser("field-init", help="Initialize a shared field.")
    _field(command)
    command.add_argument("--field-id")
    command.add_argument("--title", default="Shared work field")

    command = sub.add_parser("project-create", help="Create the canonical local project record.")
    _field(command)
    command.add_argument("--project-id")
    command.add_argument("--title", required=True)
    command.add_argument("--goal", required=True)
    command.add_argument("--owner", required=True)

    command = sub.add_parser("project-list", help="List local projects.")
    _field(command)

    command = sub.add_parser(
        "contested-add",
        help="Record one resolved disagreement: who claimed what, and what settled it.",
    )
    _project(command)
    _actor(command)
    command.add_argument("--call-file", required=True, help="JSON file or - for stdin.")

    command = sub.add_parser(
        "contested-list",
        help="Show the workers matrix: counts, and every call behind them.",
    )
    _project(command)
    command.add_argument(
        "--who",
        default="",
        help=(
            "Show only this participant's record: their counts, their calls, "
            "and what the other side claimed in each."
        ),
    )

    command = sub.add_parser(
        "assignment-report",
        help="Queue progress or a result carrying the exact assignment ownership version.",
    )
    _project(command)
    _worker(command)
    command.add_argument("--assignment-ref", required=True)
    command.add_argument("--ownership-version", required=True, type=int)
    command.add_argument(
        "--state", choices=("working", "blocked", "completed", "refused"), required=True
    )
    command.add_argument("--summary", required=True)
    command.add_argument("--result-ref", default="")
    command.add_argument("--source-event-ref", required=True)
    command.add_argument("--review-look-at", help="How to check a completed result.")
    command.add_argument("--review-could-not-verify", help="Remaining gaps, or None explicitly.")
    command.add_argument(
        "--scope",
        default="",
        help=(
            "One line naming the module, path prefixes or runtime surface this work will change. "
            "Set it with the first working report, again only when the boundary grows (W278)."
        ),
    )
    command.add_argument(
        "--wait-seconds",
        type=float,
        default=DEFAULT_REPORT_WAIT_SECONDS,
        help=(
            "How long to wait for the service's terminal answer. A deadline "
            "returns an outcome-unknown error because the report may have applied."
        ),
    )

    command = sub.add_parser("worker-register", help="Register a local worker and logical host.")
    _field(command)
    _worker(command)
    command.add_argument(
        "--runtime-kind",
        choices=("codex", "claude-code", "resident", "relay"),
        required=True,
    )
    command.add_argument("--capability", action="append", default=[])
    command.add_argument("--authority-label", default="local-only")
    command.add_argument("--host-id", required=True)
    command.add_argument("--host-label", required=True)
    command.add_argument("--host-kind", choices=("local", "hosted", "remote"), default="local")
    command.add_argument("--relay-id", default="")
    command.add_argument(
        "--reconcile-ceiling",
        "--poll-interval",
        dest="poll_interval",
        type=int,
        default=60,
        help=(
            "Longest the relay may go without a reconciliation cycle when no "
            "push arrives."
        ),
    )

    command = sub.add_parser("worker-list", help="List registered local workers.")
    _field(command)

    command = sub.add_parser("worker-status", help="Move a local worker to active or limbo.")
    _field(command)
    _worker(command)
    _actor(command)
    command.add_argument("--status", choices=("active", "limbo"), required=True)
    command.add_argument("--reason", default="")

    command = sub.add_parser(
        "journal-workspace-init",
        help="Initialize the LOCAL all-project journal aggregate.",
    )
    _journal_workspace(command)

    command = sub.add_parser(
        "journal-bind",
        help="Resolve one portable project journal home, link it locally, and rebuild the index.",
    )
    _journal_workspace(command)
    command.add_argument("--project-ref", required=True)
    command.add_argument("--journal-home-ref", required=True)
    command.add_argument("--project-artifact-ref", default="")
    command.add_argument("--revision", type=int, default=0)
    command.add_argument("--create-home", action="store_true")

    command = sub.add_parser(
        "journal-refresh",
        help="Rebuild the disposable LOCAL journal index from every bound Git home.",
    )
    _journal_workspace(command)

    command = sub.add_parser(
        "project-context",
        help="Resolve a project's portable LOCAL journal and working-directory context.",
    )
    _journal_workspace(command)
    command.add_argument("--project-ref", required=True)

    command = sub.add_parser(
        "journal-search",
        help="Search canonical LOCAL journal files with lexical SQLite FTS.",
    )
    _journal_workspace(command)
    command.add_argument("--query", default="")
    command.add_argument("--project-ref", default="")
    command.add_argument("--work-ref", default="")
    command.add_argument("--worker", default="")
    command.add_argument("--status", default="")
    command.add_argument("--limit", type=int, default=20)

    command = sub.add_parser(
        "journal-read", help="Read one canonical journal entry by portable repo ref."
    )
    _journal_workspace(command)
    command.add_argument("--repository-journal-ref", required=True)

    command = sub.add_parser(
        "mail-send",
        help="Send addressed worker mail locally or through the configured relay.",
    )
    _project(command)
    command.add_argument("--sender", required=True)
    command.add_argument("--recipient", required=True, help="A worker name, coordinator for the project's acting coordinator, or operator for the project owner's board inbox (kinds question, blocked, decision, progress, reply, update, result).")
    command.add_argument("--kind", required=True)
    command.add_argument("--subject", required=True)
    body = command.add_mutually_exclusive_group(required=True)
    body.add_argument("--body")
    body.add_argument("--body-file")
    command.add_argument("--payload-file")
    command.add_argument("--work-ref", default="")
    command.add_argument("--correlation-id", default="")
    command.add_argument("--reply-to", default="")
    command.add_argument("--attach", action="append", default=[], help="File to send with the message (repeatable); only to the operator inbox.")
    command.add_argument("--idempotency-key", required=True)
    command.add_argument(
        "--route",
        choices=("auto", "local", "remote"),
        default="auto",
        help="auto writes to a locally registered recipient or queues the remote relay.",
    )

    command = sub.add_parser("mail-pull", help="Lease mail from one worker shard.")
    _project(command)
    _worker(command)
    command.add_argument("--lease-owner", required=True)
    command.add_argument("--limit", type=int, default=10)
    command.add_argument("--lease-seconds", type=int, default=300)

    command = sub.add_parser(
        "mail-renew",
        help="Say this worker is still working, and keep its claim on a message.",
    )
    _project(command)
    _worker(command)
    command.add_argument("--message-ref", required=True)
    command.add_argument("--lease-id", required=True)
    command.add_argument("--lease-owner", required=True)
    command.add_argument("--lease-seconds", type=int, default=300)
    command.add_argument("--note", default="")

    command = sub.add_parser("mail-settle", help="Acknowledge or refuse leased local mail.")
    _project(command)
    _worker(command)
    command.add_argument("--message-ref", required=True)
    command.add_argument("--lease-id", required=True)
    command.add_argument("--lease-owner", required=True)
    command.add_argument("--outcome", choices=("acknowledged", "refused"), required=True)
    command.add_argument("--summary", default="")

    command = sub.add_parser(
        "journal-index",
        help="Index an agent-authored Git journal file and record a local receipt.",
    )
    _project(command)
    _worker(command)
    _journal_workspace(command)
    command.add_argument("--repository-journal-ref", required=True)

    command = sub.add_parser(
        "journal-index-resume",
        help="Resume the first incomplete step of a journal-index operation.",
    )
    _project(command)
    _worker(command)
    _journal_workspace(command)
    command.add_argument("--operation-id", required=True)

    command = sub.add_parser(
        "journal-index-status",
        help="Read journal-index step outcomes without changing any authority.",
    )
    _project(command)
    _journal_workspace(command)
    selector = command.add_mutually_exclusive_group(required=True)
    selector.add_argument("--operation-id")
    selector.add_argument("--outbox-id")
    command.add_argument("--repository-journal-ref", default="")

    command = sub.add_parser("scope-acquire", help="Lease relative source paths before editing.")
    _project(command)
    _worker(command)
    command.add_argument("--scope", action="append", required=True)
    command.add_argument("--ttl-seconds", type=int, default=1800)
    command.add_argument("--base-revision", default="")

    command = sub.add_parser("scope-release", help="Settle a source-path lease.")
    _project(command)
    _worker(command)
    command.add_argument("--lease-ref", required=True)
    command.add_argument("--outcome", default="released")

    command = sub.add_parser("event-queue", help="Queue a short service event for the relay.")
    _project(command)
    _worker(command)
    command.add_argument("--kind", required=True)
    command.add_argument("--summary", required=True)
    command.add_argument("--source-event-ref", required=True)
    command.add_argument("--work-ref", default="")
    command.add_argument("--metadata-file")
    command.add_argument("--content-hash", default="")

    command = sub.add_parser(
        "relay",
        help="Run the Card-authorized Data Bus relay and reconcile the local field.",
    )
    _host_config(command)
    command.add_argument("--once", action="store_true")

    relay_service = sub.add_parser(
        "relay-service", help="Manage the persistent per-machine relay user service."
    )
    relay_service_commands = relay_service.add_subparsers(
        dest="relay_service_command", required=True
    )
    for name in ("install", "status", "start", "stop", "restart", "uninstall"):
        command = relay_service_commands.add_parser(
            name, help=f"{name.capitalize()} the selected relay user service."
        )
        _host_config(command)

    source = sub.add_parser(
        "source",
        help="Inspect or explicitly select the immutable source used by pb and its relay.",
    )
    source_commands = source.add_subparsers(dest="source_command", required=True)
    command = source_commands.add_parser(
        "status", help="Show the active release environment, target receipt, launcher, and running relay source."
    )
    _host_config(command)
    command = source_commands.add_parser(
        "use-code",
        help=(
            "Export the approved App Ecosystem and KDCube commits as one "
            "client source, select it for pb and the relay, and restart the relay."
        ),
    )
    _host_config(command)
    command.add_argument(
        "--repository",
        "--app-ecosystem-repository",
        dest="repository",
        required=True,
    )
    command.add_argument(
        "--ref", "--app-ecosystem-ref", dest="ref", required=True
    )
    command.add_argument(
        "--expect",
        "--expect-app-ecosystem",
        dest="expect",
        required=True,
        help=(
            "The full approved App Ecosystem commit; selection refuses when "
            "its ref resolves elsewhere."
        ),
    )
    command.add_argument("--kdcube-repository", required=True)
    command.add_argument("--kdcube-ref", required=True)
    command.add_argument(
        "--expect-kdcube",
        required=True,
        help="The full approved KDCube commit; selection refuses when its ref resolves elsewhere.",
    )
    command.add_argument("--wait-seconds", type=float, default=None)
    command = source_commands.add_parser(
        "use-release",
        help="Install and select an exact project-board release for pb and the relay, and restart the relay.",
    )
    _host_config(command)
    command.add_argument(
        "--expect-version",
        required=True,
        help="The exact installed version approved for this target.",
    )
    command.add_argument("--wait-seconds", type=float, default=None)

    command = sub.add_parser(
        "render",
        help=(
            "Render saved pb output as complete text: refs whole, errors and "
            "unreadable input visible. Reads stdin unless --file is given."
        ),
    )
    command.add_argument("--file", default="", help="Saved pb output; stdin when omitted.")
    command.add_argument(
        "--runtime-kind",
        choices=("codex", "claude-code"),
        default="",
        help="Put this runtime identity into the rendered follow-up commands.",
    )
    command.add_argument(
        "--runtime-session-id",
        "--session-id",
        dest="runtime_session_id",
        default="",
        help="Put this session id into the rendered follow-up commands.",
    )
    return parser


def _identity(args: Any):
    return resolve_agent_session(
        runtime_kind=getattr(args, "runtime_kind", "") or "",
        runtime_session_id=getattr(args, "runtime_session_id", "") or "",
    )


def _reference_migration_lease_owner(args: Any) -> str:
    """Return the invoking agent session when this is an agent-run migration."""

    explicit_identity = bool(
        str(getattr(args, "runtime_kind", "") or "").strip()
        or str(getattr(args, "runtime_session_id", "") or "").strip()
    )
    try:
        return _identity(args).runtime_session_id
    except DomainError as exc:
        if explicit_identity or exc.code != "field_agent_session_identity_required":
            raise
        return ""


def _prose_from(inline: str | None, path: str | None) -> str:
    """The text of a prose argument: the file when one was named, else the inline value."""

    if path:
        return sys.stdin.read() if path == "-" else Path(path).expanduser().read_text(encoding="utf-8")
    return inline or ""


def _json_object(path: str, *, field: str) -> dict[str, Any]:
    try:
        raw = sys.stdin.read() if path == "-" else Path(path).expanduser().read_text(encoding="utf-8")
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise DomainError(
            "problem_board_json_invalid",
            f"{field} must name a readable JSON object.",
            details={"field": field, "path": path},
        ) from exc
    if not isinstance(value, Mapping):
        raise DomainError(
            "problem_board_json_invalid",
            f"{field} must be a JSON object.",
            details={"field": field, "path": path},
        )
    return dict(value)


def _worker_tokens(identity: Any, action: str) -> list[str]:
    command = ["pb", "worker", action]
    if identity.runtime_kind == "claude-code":
        command.extend(
            [
                "--runtime-kind",
                identity.runtime_kind,
                "--runtime-session-id",
                identity.runtime_session_id,
            ]
        )
    return command


def _status_command(args: Any) -> dict[str, Any]:
    """First-run state of this machine and, when named, this session."""

    from .first_run import first_run_status

    identity = None
    if str(getattr(args, "runtime_kind", "") or "").strip() or str(
        getattr(args, "runtime_session_id", "") or ""
    ).strip():
        identity = _identity(args)
    else:
        try:
            identity = _identity(args)
        except (DomainError, ValueError):
            # No session named and none detectable (Claude Code supplies its
            # id explicitly): report the machine, and say how to name one.
            identity = None
    return first_run_status(config=getattr(args, "config", None), identity=identity)


def _setup(args: Any) -> dict[str, Any]:
    repositories = parse_source_repositories(args.source_repo)
    # Validate mappings before writing the selected default configuration.
    repository_map = RepositoryMap.from_mapping(repositories)
    for raw in args.allow_root:
        root = Path(raw).expanduser().resolve()
        if not root.is_dir():
            raise DomainError(
                "work_allowed_root_missing",
                "Every approved coding root must already exist.",
                details={"root": str(root)},
            )
    config = initialize_host_config(
        target_id=args.target_id,
        endpoint=args.endpoint,
        tenant=args.tenant,
        platform_project=args.platform_project,
        host_id=args.host_id,
        host_label=args.host_label,
        allowed_roots=args.allow_root,
        source_repositories=repositories,
        source_repository_urls=parse_source_repositories(getattr(args, "source_repo_url", []) or []),
        config_path=args.config,
        state_root=args.state_root,
        connection_hub_state_root=args.connection_hub_state_root,
        idle_reconcile_ceiling_seconds=args.idle_poll_interval,
    )
    field = SharedFieldStore(config.field_root)
    manifest = field.initialize(title=f"Problem Board field on {config.host_label}")
    journals = JournalWorkspace(config.journal_workspace_root, repository_map).initialize()
    return {
        "config": str(config.path),
        "field_root": str(config.field_root),
        "journal_workspace": str(config.journal_workspace_root),
        "field": manifest,
        "journals": journals,
        "next": {
            "relay_debug": ["pb", "relay", "--once"],
            "relay_install": ["pb", "relay-service", "install"],
            "selected_agent_session": ["pb", "worker", "listen"],
            "session_authority": {
                "allowed_roots": list(config.allowed_roots),
                "rule": (
                    "Before enrollment, the selected coding-agent session verifies that "
                    "its own harness can read and write every required approved root. "
                    "Problem Board receiver policy does not grant filesystem access."
                ),
            },
            "authorization": (
                "Each selected session receives its own Connection Hub profile. "
                "worker listen returns the exact authorization command when needed."
            ),
        },
    }


def _host_view(config: HostRelayConfig) -> dict[str, Any]:
    diagnostics = host_relay_diagnostics(config)
    return {
        "schema": HOST_CONFIG_SCHEMA,
        "config": str(config.path or ""),
        "target": {
            "id": config.target_id,
            "endpoint": config.endpoint,
            "tenant": config.tenant,
            "project": config.platform_project,
            "bundle_id": config.bundle_id,
        },
        "field_root": str(config.field_root),
        "allowed_roots": list(config.allowed_roots),
        "host": {
            "id": config.host_id,
            "label": config.host_label,
            "kind": config.host_kind,
        },
        "relay": {
            "id": config.relay_id,
            "reconcile_ceiling_seconds": config.reconcile_ceiling_seconds,
            "idle_reconcile_ceiling_seconds": config.idle_reconcile_ceiling_seconds,
            # Pre-rename aliases, kept for one transition so an operator script
            # or widget reading the old key still sees the real value.
            "poll_interval_seconds": config.reconcile_ceiling_seconds,
            "idle_poll_interval_seconds": config.idle_reconcile_ceiling_seconds,
            "diagnostics": diagnostics,
        },
        "connection_hub": {
            "state_root": str(config.connection_hub_state_root or ""),
        },
        "receiver_policy": {
            "allowed_control_kinds": list(config.allowed_control_kinds),
            "allowed_peer_workers": list(config.allowed_peer_workers),
            "max_control_bytes": config.max_control_bytes,
            "allow_session_resume_view": config.allow_session_resume_view,
        },
        "journal_workspace": {
            "root": str(config.journal_workspace_root or ""),
            "source_repositories": dict(config.source_repositories),
            "source_repository_urls": dict(config.source_repository_urls),
            "create_missing_home": config.create_missing_journal_home,
        },
        "workers": [worker.to_mapping() for worker in config.workers],
    }


def _host_command(args: Any) -> dict[str, Any]:
    path = resolve_host_config_path(args.config)
    if args.host_command == "inspect":
        return _host_view(HostRelayConfig.load(path))
    if args.host_command == "probe-worker":
        config = HostRelayConfig.load(path)
        field = SharedFieldStore(config.field_root)
        worker_name = str(args.worker or "").strip().lower()
        worker = field.read_worker(worker_name)
        listener = field.worker_listener_session(worker_name)
        if listener is None or listener.get("state") == "detached":
            raise DomainError(
                "field_delivery_probe_worker_not_listening",
                "The delivery probe requires an attached worker listener.",
                status=409,
                details={"worker_name": worker_name},
            )
        probe_id = new_id("probe")
        sent = field.send_mail(
            "",
            sender="control-plane",
            recipient=worker_name,
            kind="ping",
            subject="Problem Board end-to-end delivery probe",
            body=(
                f"Delivery probe {probe_id}. Acknowledge and settle this message; "
                "no other action is required."
            ),
            payload={"delivery_probe_id": probe_id},
            correlation_id=probe_id,
            idempotency_key=f"delivery-probe:{probe_id}",
            sender_identity={
                "kind": "system",
                "label": "Problem Board delivery probe",
            },
        )
        message_ref = str(sent.get("message_ref") or "")
        deadline = time.monotonic() + max(1.0, float(args.timeout_seconds))
        poll_seconds = max(0.05, min(float(args.poll_seconds), 10.0))
        record: dict[str, Any] = {}
        observed_listener = listener
        while time.monotonic() < deadline:
            record = field.inspect_mail_delivery(
                "", worker_name=worker_name, message_ref=message_ref
            )
            observed_listener = field.worker_listener_session(worker_name) or {}
            if record.get("mailbox_state") == "processed":
                break
            if record.get("mailbox_state") in {"quarantine", "ignored"}:
                break
            time.sleep(poll_seconds)
        subscription = dict(observed_listener.get("subscription") or {})
        acknowledgements = [
            dict(value)
            for value in list(subscription.get("wake_acknowledgements") or [])
            if isinstance(value, Mapping)
        ]
        matching_acknowledgement = next(
            (
                value
                for value in reversed(acknowledgements)
                if message_ref in list(value.get("message_refs") or [])
            ),
            None,
        )
        if matching_acknowledgement is None and message_ref in list(
            subscription.get("last_acknowledged_wake_message_refs") or []
        ):
            matching_acknowledgement = {
                "wake_id": str(
                    subscription.get("last_acknowledged_wake_id") or ""
                ),
                "acknowledged_at": str(
                    subscription.get("last_wake_acknowledged_at") or ""
                ),
            }
        phases = {
            "local_inbox": bool(record),
            "wake": {
                "acknowledged": matching_acknowledgement is not None,
                "wake_id": str(
                    (matching_acknowledgement or {}).get("wake_id") or ""
                ),
                "acknowledged_at": str(
                    (matching_acknowledgement or {}).get("acknowledged_at") or ""
                ),
            },
            "receive": {
                "observed_at": str(
                    observed_listener.get("last_inbox_result_at") or ""
                ),
            },
            "settlement": {
                "state": str(record.get("state") or ""),
                "settled_at": str(record.get("settled_at") or ""),
                "summary": str(record.get("settlement_summary") or ""),
            },
        }
        passed = (
            record.get("mailbox_state") == "processed"
            and record.get("state") == "acknowledged"
            and phases["wake"]["acknowledged"]
            and bool(phases["receive"]["observed_at"])
            and bool(phases["settlement"]["settled_at"])
        )
        result = {
            "schema": "problem-board.delivery-probe.v1",
            "probe_id": probe_id,
            "message_ref": message_ref,
            "worker_name": str(worker.get("worker_name") or worker_name),
            "passed": passed,
            "mailbox_state": str(record.get("mailbox_state") or "missing"),
            "phases": phases,
        }
        if not passed:
            raise DomainError(
                "field_delivery_probe_failed",
                "The worker delivery probe did not complete inbox, wake, receive, "
                "and acknowledged settlement before its deadline.",
                status=504,
                details=result,
            )
        return result
    if args.host_command == "configure":
        repositories = parse_source_repositories(args.set_source_repo)
        peer_workers = [] if args.deny_all_peers else args.allow_peer_worker
        updated = update_host_config(
            path,
            endpoint=args.endpoint,
            connection_hub_state_root=args.connection_hub_state_root,
            host_label=args.host_label,
            add_allowed_roots=args.add_allow_root,
            source_repositories=repositories,
            source_repository_urls=parse_source_repositories(args.set_source_repo_url),
            allowed_control_kinds=args.allow_control_kind,
            allowed_peer_workers=peer_workers,
            max_control_bytes=args.max_control_bytes,
            allow_session_resume_view=args.allow_session_resume_view,
            reconcile_ceiling_seconds=args.poll_interval,
            idle_reconcile_ceiling_seconds=args.idle_poll_interval,
            create_missing_journal_home=args.create_missing_journal_home,
        )
        return _host_view(updated)
    if args.host_command == "relay-fault":
        config = HostRelayConfig.load(path)
        selected_worker = str(getattr(args, "worker", "") or "").strip().lower()
        if args.relay_fault_command == "status":
            selected = [selected_worker] if selected_worker else None
            return {
                "schema": "problem-board.relay-fault-status.v1",
                "faults": pending_relay_faults(path, worker_names=selected),
            }
        channel = next(
            (
                worker
                for worker in config.workers
                if worker.worker_name == selected_worker
            ),
            None,
        )
        if channel is None:
            raise DomainError(
                "work_relay_fault_worker_not_found",
                "Relay fault injection requires one exact enrolled worker name.",
                status=404,
                details={"worker_name": selected_worker},
            )
        if args.relay_fault_command == "clear":
            return {
                "schema": "problem-board.relay-fault-status.v1",
                "worker_name": selected_worker,
                "cleared": clear_relay_fault(path, worker_name=selected_worker),
            }
        if not args.confirm_live_interruption:
            raise DomainError(
                "work_relay_fault_confirmation_required",
                "Confirm the coordinated live interruption with --confirm-live-interruption.",
                details={"worker_name": selected_worker},
            )
        if channel.state != "active":
            raise DomainError(
                "work_relay_fault_worker_not_active",
                "Relay fault injection requires an active worker channel.",
                status=409,
                details={
                    "worker_name": selected_worker,
                    "channel_state": channel.state,
                },
            )
        return {
            "schema": "problem-board.relay-fault-status.v1",
            "fault": inject_relay_fault(
                path,
                worker_name=selected_worker,
                code=args.code,
                ttl_seconds=args.ttl_seconds,
            ),
            "inspect": [
                ["pb", "host", "inspect"],
                ["pb", "relay-service", "status"],
                ["pb", "worker", "list"],
            ],
        }
    raise ValueError(f"unsupported host command: {args.host_command}")


async def _coordinate_direct(
    *,
    config: HostRelayConfig,
    channel: Any,
    action: str,
    object_ref: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Use the original in-process path where native credentials are readable."""

    from connection_hub_cli.cli import build_services
    from connection_hub_cli.paths import StatePaths
    from connection_hub_cli.profile_connection import connect_profile_tools

    from .mcp_client import ProblemBoardMcpClient, governed_endpoint_identity

    paths = (
        StatePaths(config.connection_hub_state_root)
        if config.connection_hub_state_root is not None
        else StatePaths.default()
    )
    services = build_services(paths=paths)
    profile = services.profiles.require(channel.profile)
    async with connect_profile_tools(
        profile_name=channel.profile,
        profiles=services.profiles,
        credentials=services.credentials,
        oauth_sessions=services.oauth_profile_sessions,
    ) as (remote, _client):
        endpoint = str(getattr(profile, "endpoint", "") or "")
        governed_endpoint_identity(endpoint)
        client = ProblemBoardMcpClient(remote)
        return await client.action(
            object_ref=object_ref,
            action=action,
            payload=payload,
        )


def _coordinate_response(response: Mapping[str, Any]) -> dict[str, Any]:
    if bool(response.get("ok")):
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise DomainError(
                "work_coordinate_response_invalid",
                "The worker relay returned no governed operation result.",
                status=502,
            )
        require_successful_operation_envelope(
            str(result.get("operation") or ""),
            result,
        )
        return dict(result)
    error = response.get("error")
    error = dict(error) if isinstance(error, Mapping) else {}
    raise DomainError(
        str(error.get("code") or "work_coordinate_relay_failed"),
        str(error.get("message") or "The worker relay could not run the operation."),
        status=int(error.get("status") or 502),
        details=(
            dict(error.get("details"))
            if isinstance(error.get("details"), Mapping)
            else {}
        ),
    )


def _channel_reconnecting_error(
    worker_name: str, reconnect: Mapping[str, Any]
) -> DomainError:
    return DomainError(
        "work_coordinate_channel_reconnecting",
        (
            "This worker's channel is not open: the relay is reconnecting it "
            f"after {reconnect.get('reason') or 'a failure'} "
            f"(attempt {reconnect.get('attempts') or 0}, next attempt "
            f"{reconnect.get('next_attempt_at') or 'unknown'}). Retry after that time."
        ),
        status=503,
        details={
            "worker_name": worker_name,
            "channel_state": "reconnecting",
            "last_error": str(reconnect.get("reason") or ""),
            "attempts": int(reconnect.get("attempts") or 0),
            "retry_schedule": str(reconnect.get("schedule") or ""),
            "next_attempt_at": str(reconnect.get("next_attempt_at") or ""),
            "inspect": ["pb", "status"],
        },
    )


def _raise_if_channel_reconnecting(config_path: Any, worker_name: str) -> None:
    reconnect = channel_reconnect_state(config_path, worker_name)
    if reconnect is not None:
        raise _channel_reconnecting_error(worker_name, reconnect)


def _send_channel_reconnecting_error(
    worker_name: str, reconnect: Mapping[str, Any], *, idempotency_key: str
) -> DomainError:
    return DomainError(
        "work_send_channel_reconnecting",
        (
            "This worker's channel is not open: the relay is reconnecting it "
            f"after {reconnect.get('reason') or 'a failure'} "
            f"(attempt {reconnect.get('attempts') or 0}, next attempt "
            f"{reconnect.get('next_attempt_at') or 'unknown'}). The message was "
            "not delivered. Retry after that time with the same idempotency key: "
            "a delivered message replays, a lost one goes through."
        ),
        status=503,
        details={
            "worker_name": worker_name,
            "channel_state": "reconnecting",
            "last_error": str(reconnect.get("reason") or ""),
            "attempts": int(reconnect.get("attempts") or 0),
            "retry_schedule": str(reconnect.get("schedule") or ""),
            "next_attempt_at": str(reconnect.get("next_attempt_at") or ""),
            "delivered": False,
            "idempotency_key": str(idempotency_key or ""),
        },
    )


def _raise_if_send_channel_reconnecting(
    config_path: Any, worker_name: str, *, idempotency_key: str
) -> None:
    """Remote mail rides the worker's channel: a channel the relay is
    reconnecting cannot carry it, and the sender hears so at once instead of
    reading a queued message as a delivered one."""

    reconnect = channel_reconnect_state(config_path, worker_name)
    if reconnect is not None:
        raise _send_channel_reconnecting_error(
            worker_name, reconnect, idempotency_key=idempotency_key
        )


def _coordinate_command(args: Any) -> dict[str, Any]:
    """Call any canonical operation through this exact worker's Card channel."""

    identity = _identity(args)
    path = resolve_host_config_path(getattr(args, "config", None))
    config = HostRelayConfig.load(path)
    channel = config.worker(identity)
    if channel is None:
        raise DomainError(
            "work_worker_channel_missing",
            "This session has no worker relay channel; run pb worker listen first.",
            status=409,
            details={"worker_name": identity.worker_name},
        )
    if channel.state != "active":
        raise DomainError(
            "work_worker_channel_not_active",
            "This session's worker relay channel is not active.",
            status=409,
            details={
                "worker_name": identity.worker_name,
                "channel_state": channel.state,
                "required_action": f"pb worker authorize {channel.profile}",
            },
        )
    payload: dict[str, Any] = {}
    if args.payload_file and args.payload_json:
        raise ValueError("pass only one of --payload-file and --payload-json")
    if args.payload_file:
        payload = load_json(args.payload_file)
        refuse_unresolved_payload_slots(payload, argument="--payload-file")
    elif args.payload_json:
        value = json.loads(args.payload_json)
        if isinstance(value, Mapping):
            guard_inline_payload_prose(value, argument="--payload-json", file_argument="--payload-file")
            refuse_unresolved_payload_slots(value, argument="--payload-json")
        if not isinstance(value, dict):
            raise ValueError("--payload-json must be a JSON object")
        payload = value
    action = str(args.action or "")
    object_ref = str(args.object_ref or "")
    if str(getattr(args, "route", "relay") or "relay") == "direct":
        return asyncio.run(
            _coordinate_direct(
                config=config,
                channel=channel,
                action=action,
                object_ref=object_ref,
                payload=payload,
            )
        )

    try:
        timeout_seconds = float(
            getattr(args, "timeout_seconds", DEFAULT_COORDINATE_TIMEOUT_SECONDS)
        )
    except (TypeError, ValueError) as exc:
        raise DomainError(
            "work_coordinate_timeout_invalid",
            "timeout_seconds must be a number between 1 and 600.",
        ) from exc
    if not 1.0 <= timeout_seconds <= 600.0:
        raise DomainError(
            "work_coordinate_timeout_invalid",
            "timeout_seconds must be between 1 and 600.",
        )
    # A channel the relay is reconnecting cannot carry this request. Say so at
    # once, with what the relay knows, instead of waiting out the deadline
    # behind "the relay did not claim the operation".
    _raise_if_channel_reconnecting(path, channel.worker_name)
    queue = CoordinateQueue(config.field_root)
    request = queue.submit(
        worker_name=channel.worker_name,
        worker_identity=channel.worker_identity,
        runtime_kind=channel.runtime_kind,
        runtime_session_id=channel.runtime_session_id,
        action=action,
        object_ref=object_ref,
        payload=payload,
        timeout_seconds=timeout_seconds,
    )
    request_id = str(request["request_id"])
    deadline = time.monotonic() + timeout_seconds
    next_channel_check = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        response = queue.take_response(
            worker_name=channel.worker_name,
            request_id=request_id,
        )
        if response is not None:
            return _coordinate_response(response)
        if time.monotonic() >= next_channel_check:
            next_channel_check = time.monotonic() + 1.0
            reconnect = channel_reconnect_state(path, channel.worker_name)
            if reconnect is not None:
                # Only a request no relay ever claimed is withdrawn; a claimed
                # one stays for the relay to finish or reconcile.
                withdrawn = queue.cancel_pending_if_unclaimed(
                    worker_name=channel.worker_name,
                    request_id=request_id,
                )
                if withdrawn is not None:
                    raise _channel_reconnecting_error(channel.worker_name, reconnect)
        time.sleep(0.05)
    cancelled = queue.cancel_pending(
        worker_name=channel.worker_name,
        request_id=request_id,
    )
    if cancelled is not None and not bool(cancelled.get("claimed_once")):
        raise DomainError(
            "work_coordinate_relay_unavailable",
            "The worker relay did not claim the governed operation before its deadline.",
            status=504,
            details={
                "worker_name": channel.worker_name,
                "request_id": request_id,
                "timeout_seconds": timeout_seconds,
            },
        )
    if cancelled is not None:
        raise DomainError(
            "work_coordinate_outcome_unknown",
            "The relay claimed the governed operation but no result arrived before the deadline.",
            status=504,
            details={
                "worker_name": channel.worker_name,
                "request_id": request_id,
                "transport_attempts": int(
                    cancelled.get("transport_attempts") or 0
                ),
            },
        )
    response = queue.take_response(
        worker_name=channel.worker_name,
        request_id=request_id,
    )
    if response is not None:
        return _coordinate_response(response)
    raise DomainError(
        "work_coordinate_outcome_unknown",
        "The relay claimed the governed operation but no result arrived before the deadline.",
        status=504,
        details={"worker_name": channel.worker_name, "request_id": request_id},
    )


def _project_id(project_ref: str) -> str:
    address = parse_ref(project_ref)
    if address.selector != "project":
        raise DomainError(
            "work_project_ref_invalid",
            "project_ref must be a canonical work:project URI.",
            details={"project_ref": project_ref},
        )
    return address.object_id


def _plan_index_page(args: Any, *, cursor: str) -> dict[str, Any]:
    request = argparse.Namespace(
        action="project.plan.index",
        object_ref=args.project_ref,
        payload_json=json.dumps({"limit": 200, "cursor": cursor}),
        payload_file="",
        runtime_kind=getattr(args, "runtime_kind", ""),
        runtime_session_id=getattr(args, "runtime_session_id", ""),
        config=getattr(args, "config", None),
    )
    response = _coordinate_command(request)
    page = response.get("object")
    if not isinstance(page, Mapping):
        raise DomainError(
            "plan_sync_index_response_invalid",
            "The project plan-index operation returned no index object.",
            status=502,
            details={"response_keys": sorted(str(key) for key in response)},
        )
    return dict(page)


def _plan_item(args: Any, *, work_ref: str) -> dict[str, Any]:
    request = argparse.Namespace(
        action="project.plan.item",
        object_ref=args.project_ref,
        payload_json=json.dumps({"work_ref": work_ref}),
        payload_file="",
        runtime_kind=getattr(args, "runtime_kind", ""),
        runtime_session_id=getattr(args, "runtime_session_id", ""),
        config=getattr(args, "config", None),
    )
    response = _coordinate_command(request)
    item = response.get("object")
    if not isinstance(item, Mapping):
        raise DomainError(
            "plan_sync_item_response_invalid",
            "The project plan-item operation returned no item object.",
            status=502,
            details={"work_ref": work_ref},
        )
    return dict(item)


def _plan_notes_page(args: Any, *, work_ref: str, cursor: str) -> dict[str, Any]:
    request = argparse.Namespace(
        action="plan.notes.list",
        object_ref=args.project_ref,
        payload_json=json.dumps(
            {
                "work_ref": work_ref,
                "limit": 100,
                "cursor": cursor,
                "idempotency_key": (
                    "plan-cutover-notes:"
                    + content_hash({"work_ref": work_ref, "cursor": cursor})
                ),
            }
        ),
        payload_file="",
        runtime_kind=getattr(args, "runtime_kind", ""),
        runtime_session_id=getattr(args, "runtime_session_id", ""),
        config=getattr(args, "config", None),
    )
    response = _coordinate_command(request)
    page = response.get("object")
    if not isinstance(page, Mapping):
        raise DomainError(
            "plan_cutover_notes_response_invalid",
            "The plan-notes operation returned no note page.",
            status=502,
            details={"work_ref": work_ref},
        )
    return dict(page)


def _complete_plan_index(args: Any) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    cursor = ""
    result: dict[str, Any] = {}
    for _ in range(10_000):
        page = _plan_index_page(args, cursor=cursor)
        if str(page.get("schema") or "") != "problem-board.plan-index.v2":
            raise DomainError(
                "plan_sync_index_schema_invalid",
                "The remote project must publish the canonical v2 plan index before Git sync.",
                status=409,
                details={"schema": str(page.get("schema") or "")},
            )
        if not result:
            result = dict(page)
        elif (
            int(page.get("plan_revision") or 0)
            != int(result.get("plan_revision") or 0)
            or int(page.get("item_count") or 0)
            != int(result.get("item_count") or 0)
            or str(page.get("generation_token") or "")
            != str(result.get("generation_token") or "")
        ):
            raise DomainError(
                "plan_sync_index_changed",
                "The plan changed while its pages were read; retry the sync from the first page.",
                status=409,
            )
        items.extend(
            dict(row)
            for row in page.get("items") or []
            if isinstance(row, Mapping)
        )
        cursor = str(page.get("next_cursor") or "")
        if not cursor:
            break
    else:
        raise DomainError(
            "plan_sync_index_too_large",
            "The complete plan index exceeded the bounded page count.",
            status=409,
        )
    expected = int(result.get("item_count") or 0)
    if len(items) != expected:
        raise DomainError(
            "plan_sync_index_incomplete",
            "The complete plan index did not return every published node.",
            status=409,
            details={"expected": expected, "received": len(items)},
        )
    result["items"] = items
    result["count"] = len(items)
    result["next_cursor"] = ""
    return result



def _report_publication_outcome(
    *,
    queued: Mapping[str, Any],
    row: Mapping[str, Any] | None,
    settle_command: Sequence[str],
    status_command: Sequence[str],
) -> dict[str, Any]:
    """Say what happened to one report publication, and only offer what is safe.

    The settle command is printed for a stored report and for nothing else.
    Settling closes the lease, and the lease is the only way to publish again.
    """

    state = str((row or {}).get("state") or "")
    base = {
        "outbox_id": str(queued.get("outbox_id") or ""),
        "report_ref": str(queued.get("report_ref") or ""),
        "message_ref": str(queued.get("message_ref") or ""),
        "attachment_count": int(queued.get("attachment_count") or 0),
        "replayed": bool(queued.get("replayed")),
    }
    if state == "sent":
        return {
            **base,
            "published": True,
            "delivery_status": "published",
            "settled_at": str((row or {}).get("settled_at") or ""),
            "next": {
                "settle": list(settle_command),
                "rule": (
                    "The service stored this report and the board shows it. "
                    "Settle this exact mail lease once."
                ),
            },
        }
    if state in {"refused", "ignored"}:
        remote = (row or {}).get("remote_result")
        error = dict(remote.get("error")) if isinstance(remote, Mapping) and isinstance(remote.get("error"), Mapping) else {}
        raise DomainError(
            "field_project_report_refused",
            "The service refused this report. Nothing was published.",
            status=409,
            details={
                **base,
                "published": False,
                "delivery_status": state,
                "remote_disposition": str((row or {}).get("remote_disposition") or ""),
                "error": error,
                "rule": (
                    "Do not settle. The lease is still yours and is the only way to "
                    "publish again: fix the cause and run publish once more."
                ),
            },
        )
    return {
        **base,
        "published": False,
        "delivery_status": "queued",
        "intent_not_receipt": True,
        "outbox_state": state or "unknown",
        "next": {
            "status": list(status_command),
            "rule": (
                "This is a queued intent, not a receipt. The service has not "
                "answered yet, and it can still refuse. Do not report the report "
                "as published and do not settle until status says published."
            ),
        },
    }


def _complete_remote_plan(args: Any) -> dict[str, Any]:
    """Read every authoritative item and prove one PostgreSQL generation."""

    index = _complete_plan_index(args)
    generation_token = str(index.get("generation_token") or "")
    if not generation_token:
        raise DomainError(
            "plan_sync_generation_missing",
            "The PostgreSQL plan index did not identify its generation.",
            status=409,
        )
    hydrated: list[dict[str, Any]] = []
    for compact in index.get("items") or []:
        work_ref = str(compact.get("item_ref") or "")
        item = _plan_item(args, work_ref=work_ref)
        for field in ("item_ref", "revision", "source_content_hash"):
            if item.get(field) != compact.get(field):
                raise DomainError(
                    "plan_sync_index_changed",
                    "A plan item changed while the Git export was being read; retry from the first page.",
                    status=409,
                    details={"work_ref": work_ref, "field": field},
                )
        hydrated.append(
            {
                **compact,
                **item,
                "ordinal": int(compact.get("ordinal") or 0),
                "depends_on": list(compact.get("depends_on") or []),
            }
        )

    final_page = _plan_index_page(args, cursor="")
    if (
        int(final_page.get("plan_revision") or 0)
        != int(index.get("plan_revision") or 0)
        or str(final_page.get("generation_token") or "") != generation_token
    ):
        raise DomainError(
            "plan_sync_index_changed",
            "The plan changed while the Git export was being read; retry from the first page.",
            status=409,
        )
    return {**index, "items": hydrated, "count": len(hydrated)}


def _complete_remote_plan_with_notes(
    args: Any, index: Mapping[str, Any]
) -> dict[str, Any]:
    generation_token = str(index.get("generation_token") or "")
    notes: list[dict[str, Any]] = []
    seen_note_refs: set[str] = set()
    for item in index.get("items") or []:
        work_ref = str(item.get("item_ref") or "")
        expected = int(item.get("note_count") or 0)
        if expected == 0:
            continue
        cursor = ""
        received = 0
        declared_total: int | None = None
        for _ in range(10_000):
            page = _plan_notes_page(args, work_ref=work_ref, cursor=cursor)
            if str(page.get("work_ref") or "") != work_ref:
                raise DomainError(
                    "plan_cutover_notes_response_invalid",
                    "A note page names another work item.",
                    status=502,
                    details={"work_ref": work_ref},
                )
            detail = page.get("item")
            if (
                not isinstance(detail, Mapping)
                or int(detail.get("revision") or 0) != int(item.get("revision") or 0)
            ):
                raise DomainError(
                    "plan_sync_index_changed",
                    "A plan item changed while its notes were read; retry the cutover.",
                    status=409,
                    details={"work_ref": work_ref},
                )
            total = int(page.get("total") or 0)
            if declared_total is None:
                declared_total = total
            elif declared_total != total:
                raise DomainError(
                    "plan_sync_index_changed",
                    "A note collection changed while it was read; retry the cutover.",
                    status=409,
                    details={"work_ref": work_ref},
                )
            for raw in page.get("items") or []:
                if not isinstance(raw, Mapping):
                    raise DomainError(
                        "plan_cutover_notes_response_invalid",
                        "A note page contains a non-object row.",
                        status=502,
                        details={"work_ref": work_ref},
                    )
                note = dict(raw)
                note_ref = str(note.get("note_ref") or "")
                note_identity_ref = str(
                    note.get("identity_ref") or note.get("item_ref") or ""
                )
                if (
                    plan_node_identity_ref(note_identity_ref)
                    != plan_node_identity_ref(work_ref)
                    or note_ref in seen_note_refs
                ):
                    raise DomainError(
                        "plan_cutover_notes_response_invalid",
                        "A note page contains a duplicate or mismatched note.",
                        status=502,
                        details={"work_ref": work_ref, "note_ref": note_ref},
                    )
                seen_note_refs.add(note_ref)
                notes.append(note)
                received += 1
            cursor = str(page.get("next_cursor") or "")
            if not cursor:
                break
        else:
            raise DomainError(
                "plan_cutover_notes_too_large",
                "The note collection exceeded the bounded page count.",
                status=409,
                details={"work_ref": work_ref},
            )
        if received != expected or declared_total != expected:
            raise DomainError(
                "plan_sync_index_changed",
                "A plan item's note count changed while the cutover was verified.",
                status=409,
                details={
                    "work_ref": work_ref,
                    "expected": expected,
                    "received": received,
                    "declared_total": declared_total,
                },
            )

    final_page = _plan_index_page(args, cursor="")
    if (
        int(final_page.get("plan_revision") or 0)
        != int(index.get("plan_revision") or 0)
        or str(final_page.get("generation_token") or "") != generation_token
    ):
        raise DomainError(
            "plan_sync_index_changed",
            "The plan changed while its notes were read; retry the cutover.",
            status=409,
        )
    return {**dict(index), "notes": notes}


REFERENCE_MIGRATION_PREVIEW_SCHEMA = "problem-board.reference-migration-preview.v1"
REFERENCE_MIGRATION_RECEIPT_SCHEMA = "problem-board.reference-migration-receipt.v1"


def _reference_mapping_request(
    args: Any,
    *,
    action: str,
    object_ref: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    request = argparse.Namespace(
        action=action,
        object_ref=object_ref,
        payload_json=json.dumps(dict(payload)),
        payload_file="",
        runtime_kind=getattr(args, "runtime_kind", ""),
        runtime_session_id=getattr(args, "runtime_session_id", ""),
        config=getattr(args, "config", None),
    )
    return _coordinate_command(request)


def _worker_item(args: Any) -> dict[str, Any]:
    result = _reference_mapping_request(
        args,
        action="project.plan.item",
        object_ref=args.project_ref,
        payload={"item_key": args.item_key},
    )
    item = result.get("object")
    if not isinstance(item, Mapping):
        raise DomainError(
            "work_item_response_invalid",
            "The work-item read returned no item.",
            status=502,
        )
    return dict(item)


def _worker_item_attach(args: Any) -> dict[str, Any]:
    from .relay import _http_upload

    source = Path(args.file).expanduser().resolve(strict=True)
    if not source.is_file():
        raise DomainError("work_attachment_file_invalid", "The attachment path is not a file.")
    item = _worker_item(args)
    data = source.read_bytes()
    # A text attachment is read by people too: a template slot left in it is
    # the same defect as one in a mail body, and it is refused the same way.
    try:
        refuse_unresolved_slots(data.decode("utf-8"), argument="--file")
    except UnicodeDecodeError:
        pass
    slot_result = _reference_mapping_request(
        args,
        action="attachment.request_upload",
        object_ref="work:worker:self",
        payload={"filename": source.name},
    )
    slot = slot_result.get("object")
    if not isinstance(slot, Mapping):
        raise DomainError("work_attachment_upload_response_invalid", "No upload slot was returned.", status=502)
    maximum = int(slot.get("max_bytes") or 0)
    if maximum and len(data) > maximum:
        raise DomainError(
            "work_attachment_too_large",
            "The file exceeds the upload slot limit.",
            details={"maximum_bytes": maximum, "content_bytes": len(data)},
        )
    upload_url = str(slot.get("upload_url") or "")
    staged_ref = str(slot.get("staged_ref") or "")
    if not upload_url or not staged_ref:
        raise DomainError("work_attachment_upload_response_invalid", "The upload slot is incomplete.", status=502)
    import mimetypes

    asyncio.run(_http_upload(upload_url, data, mimetypes.guess_type(source.name)[0] or "application/octet-stream"))
    refs = [str(ref) for ref in item.get("attachment_refs") or []]
    updated = _reference_mapping_request(
        args,
        action="plan.item.update",
        object_ref=args.project_ref,
        payload={
            "work_ref": item["item_ref"],
            "expected_revision": item["revision"],
            "changes": {"attachment_refs": [*refs, staged_ref]},
            "idempotency_key": new_id("item-attach"),
        },
    )
    changed = updated.get("object")
    if not isinstance(changed, Mapping):
        raise DomainError("work_item_response_invalid", "The work-item update returned no item.", status=502)
    added = [str(ref) for ref in changed.get("attachment_refs") or [] if str(ref) not in refs]
    return {
        "project_ref": args.project_ref,
        "item_key": args.item_key,
        "item_ref": changed.get("item_ref"),
        "attachment_count": changed.get("attachment_count"),
        "file_ref": added[0] if len(added) == 1 else "",
        "content_hash": hashlib.sha256(data).hexdigest(),
        "content_bytes": len(data),
    }


def _worker_item_attachment_read(args: Any) -> dict[str, Any]:
    from .relay import _http_download

    item = _worker_item(args)
    descriptor = next(
        (
            entry for entry in item.get("attachments") or []
            if isinstance(entry, Mapping) and entry.get("file_ref") == args.file_ref
        ),
        None,
    )
    if descriptor is None:
        raise DomainError(
            "work_item_attachment_not_found",
            "The file is not attached to this item.",
            status=404,
        )
    download_url = str(descriptor.get("download_url") or "")
    if not download_url:
        raise DomainError(
            "work_item_attachment_unavailable",
            "This item file has no available governed download link.",
            status=409,
        )
    destination = Path(args.output).expanduser().absolute()
    if destination.exists():
        raise DomainError(
            "work_attachment_output_exists",
            "The output path already exists; choose a new path.",
            status=409,
        )
    data = asyncio.run(_http_download(download_url))
    with destination.open("xb") as handle:
        handle.write(data)
    return {
        "project_ref": args.project_ref,
        "item_key": args.item_key,
        "file_ref": args.file_ref,
        "local_path": str(destination),
        "content_hash": hashlib.sha256(data).hexdigest(),
        "content_bytes": len(data),
    }


def _upload_reference_mapping(
    args: Any,
    *,
    project_ref: str,
    reference_mapping: Mapping[str, Any],
) -> dict[str, Any]:
    from .relay import _http_upload

    mapping = normalize_reference_mapping(reference_mapping)
    encoded = encode_reference_mapping_document(
        project_ref=project_ref,
        reference_mapping=mapping,
    )
    response = _reference_mapping_request(
        args,
        action="attachment.request_upload",
        object_ref="work:worker:self",
        payload={"filename": "reference-mapping.json"},
    )
    slot = response.get("object")
    if not isinstance(slot, Mapping):
        raise DomainError(
            "work_reference_mapping_upload_response_invalid",
            "The reference mapping upload request returned no upload slot.",
            status=502,
        )
    try:
        maximum = int(slot.get("max_bytes") or 0)
    except (TypeError, ValueError) as exc:
        raise DomainError(
            "work_reference_mapping_upload_response_invalid",
            "The reference mapping upload slot has an invalid byte limit.",
            status=502,
        ) from exc
    if maximum and len(encoded) > maximum:
        raise DomainError(
            "work_reference_mapping_document_too_large",
            "The reference mapping document exceeds the upload slot limit.",
            status=409,
            details={"document_bytes": len(encoded), "maximum_bytes": maximum},
        )
    upload_url = str(slot.get("upload_url") or "").strip()
    staged_ref = str(slot.get("staged_ref") or "").strip()
    if not upload_url or not staged_ref:
        raise DomainError(
            "work_reference_mapping_upload_response_invalid",
            "The reference mapping upload slot is incomplete.",
            status=502,
        )
    asyncio.run(_http_upload(upload_url, encoded, "application/json"))
    return {
        "staged_ref": staged_ref,
        "content_hash": hashlib.sha256(encoded).hexdigest(),
        "content_bytes": len(encoded),
        "reference_mapping_hash": content_hash(mapping),
        "reference_mapping_count": len(mapping),
    }


def _download_reference_mapping(
    remote: Mapping[str, Any],
    *,
    project_ref: str,
) -> tuple[dict[str, str], dict[str, Any]]:
    from .relay import _http_download

    artifact = remote.get("reference_mapping_artifact")
    if not isinstance(artifact, Mapping):
        mapping = normalize_reference_mapping(remote.get("reference_mapping"))
        return mapping, dict(remote)
    descriptor = dict(artifact)
    if descriptor.get("schema") != REFERENCE_MAPPING_ARTIFACT_SCHEMA:
        raise DomainError(
            "work_reference_mapping_artifact_invalid",
            "The reference preview returned an unsupported mapping artifact.",
            status=502,
        )
    if str(descriptor.get("project_ref") or "") != project_ref:
        raise DomainError(
            "work_reference_mapping_artifact_identity_mismatch",
            "The reference preview returned a mapping artifact for another project.",
            status=502,
        )
    for field in (
        "file_ref",
        "content_hash",
        "content_bytes",
        "reference_mapping_hash",
        "reference_mapping_count",
    ):
        field_value = descriptor.get(field)
        if field not in descriptor or field_value is None or field_value == "":
            raise DomainError(
                "work_reference_mapping_artifact_invalid",
                f"The reference preview mapping artifact requires {field}.",
                status=502,
                details={"field": field},
            )
    download_url = str(descriptor.pop("download_url", "") or "").strip()
    if not download_url:
        raise DomainError(
            "work_reference_mapping_artifact_invalid",
            "The reference preview returned no mapping artifact download link.",
            status=502,
        )
    encoded = asyncio.run(_http_download(download_url))
    mapping = decode_reference_mapping_document(
        encoded,
        project_ref=project_ref,
        expected_content_hash=str(descriptor.get("content_hash") or ""),
        expected_content_bytes=descriptor.get("content_bytes"),
        expected_mapping_hash=str(
            descriptor.get("reference_mapping_hash") or ""
        ),
        expected_mapping_count=descriptor.get("reference_mapping_count"),
    )
    sanitized_remote = dict(remote)
    sanitized_remote["reference_mapping_artifact"] = descriptor
    sanitized_remote.pop("reference_mapping", None)
    return mapping, sanitized_remote


def _reference_migration_preview_hash(value: Mapping[str, Any]) -> str:
    remote = value.get("postgresql") if isinstance(value.get("postgresql"), Mapping) else {}
    local = value.get("local_field") if isinstance(value.get("local_field"), Mapping) else {}
    git = value.get("git") if isinstance(value.get("git"), Mapping) else {}
    return content_hash(
        {
            "schema": REFERENCE_MIGRATION_PREVIEW_SCHEMA,
            "project_ref": str(value.get("project_ref") or ""),
            "reference_mapping_hash": str(
                value.get("reference_mapping_hash") or ""
            ),
            "postgresql_migration_hash": str(remote.get("migration_hash") or ""),
            "postgresql_plan_revision": int(remote.get("plan_revision") or 0),
            "local_migration_hash": str(local.get("local_migration_hash") or ""),
            "git_files": list(git.get("files") or []),
        }
    )


def _reference_migration_preview(args: Any) -> dict[str, Any]:
    from .plan_sync import preview_tracked_reference_rewrites
    from .reference_migration import (
        discover_local_reference_mapping,
        preview_local_reference_migration,
    )

    config = HostRelayConfig.load(resolve_host_config_path(getattr(args, "config", None)))
    lease_owner = _reference_migration_lease_owner(args)
    project_id = _project_id(args.project_ref)
    field = SharedFieldStore(config.field_root)
    local_mapping = discover_local_reference_mapping(field, project_id)
    uploaded_mapping = _upload_reference_mapping(
        args,
        project_ref=args.project_ref,
        reference_mapping=local_mapping,
    )
    response = _reference_mapping_request(
        args,
        action="project.references.preview",
        object_ref=args.project_ref,
        payload={"reference_mapping_upload": uploaded_mapping},
    )
    remote = response.get("object")
    if not isinstance(remote, Mapping):
        raise DomainError(
            "work_reference_migration_preview_response_invalid",
            "The reference preview operation returned no PostgreSQL inventory.",
            status=502,
        )
    reference_mapping, remote = _download_reference_mapping(
        remote,
        project_ref=args.project_ref,
    )
    local = preview_local_reference_migration(
        field,
        project_id,
        reference_mapping=reference_mapping,
        lease_owner=lease_owner,
    )
    workspace = JournalWorkspace(
        config.journal_workspace_root,
        RepositoryMap.from_mapping(dict(config.source_repositories)),
    )
    context = workspace.context(args.project_ref)
    journal_home = Path(str(context["local_journal_home"]))
    git_files = list(
        preview_tracked_reference_rewrites(
            journal_home,
            reference_mapping,
        )
    )
    result = {
        "schema": REFERENCE_MIGRATION_PREVIEW_SCHEMA,
        "project_ref": args.project_ref,
        "reference_mapping": reference_mapping,
        "reference_mapping_hash": content_hash(reference_mapping),
        "reference_mapping_count": len(reference_mapping),
        "postgresql": dict(remote),
        "local_field": local,
        "git": {
            "journal_home_ref": str(context.get("journal_home_ref") or ""),
            "rewritten_file_count": len(git_files),
            "files": git_files,
        },
        "unresolved_reference_count": 0,
        "unresolved_references": [],
    }
    result["preview_hash"] = _reference_migration_preview_hash(result)
    return result


def _references_command(args: Any) -> dict[str, Any]:
    from .plan_sync import sync_plan
    from .reference_migration import (
        REFERENCE_MIGRATION_RECEIPT_SCHEMA as LOCAL_RECEIPT_SCHEMA,
        apply_local_reference_migration,
        reference_migration_receipt_path,
    )

    if args.references_command == "preview":
        return _reference_migration_preview(args)

    config = HostRelayConfig.load(resolve_host_config_path(getattr(args, "config", None)))
    lease_owner = _reference_migration_lease_owner(args)
    project_id = _project_id(args.project_ref)
    field = SharedFieldStore(config.field_root)
    reviewed = load_json(args.reviewed_preview)
    if reviewed.get("ok") is True and isinstance(reviewed.get("result"), Mapping):
        reviewed = dict(reviewed["result"])
    if (
        reviewed.get("schema") != REFERENCE_MIGRATION_PREVIEW_SCHEMA
        or reviewed.get("project_ref") != args.project_ref
    ):
        raise DomainError(
            "work_reference_migration_preview_invalid",
            "The reviewed preview belongs to another project or schema.",
            status=409,
        )
    expected_hash = str(args.expected_preview_hash or "").strip()
    if (
        not expected_hash
        or str(reviewed.get("preview_hash") or "") != expected_hash
        or _reference_migration_preview_hash(reviewed) != expected_hash
    ):
        raise DomainError(
            "work_reference_migration_preview_invalid",
            "The supplied preview hash does not match the reviewed preview file.",
            status=409,
        )
    migration_id = str(args.idempotency_key or "").strip()
    receipt_path = reference_migration_receipt_path(
        field,
        project_id,
        migration_id,
    )
    receipt = read_json(receipt_path, required=False)
    if receipt:
        if (
            receipt.get("schema") != REFERENCE_MIGRATION_RECEIPT_SCHEMA
            or receipt.get("project_ref") != args.project_ref
            or receipt.get("preview_hash") != expected_hash
            or receipt.get("migration_id") != migration_id
        ):
            raise DomainError(
                "work_reference_migration_replay_conflict",
                "The migration ID already belongs to another reviewed preview.",
                status=409,
            )
        if receipt.get("state") == "completed":
            return {**receipt, "replayed": True}
    else:
        current = _reference_migration_preview(args)
        if str(current.get("preview_hash") or "") != expected_hash:
            raise DomainError(
                "work_reference_migration_preview_changed",
                "Project references changed after this migration was reviewed.",
                status=409,
                details={
                    "expected_preview_hash": expected_hash,
                    "current_preview_hash": str(current.get("preview_hash") or ""),
                },
            )
        busy_refs = list(
            (current.get("local_field") or {}).get("busy_refs") or []
        )
        if busy_refs:
            raise DomainError(
                "field_reference_migration_busy",
                "A local record that must be rewritten is currently leased.",
                status=409,
                details={"retryable": True, "leased_refs": busy_refs},
            )
        receipt = {
            "schema": REFERENCE_MIGRATION_RECEIPT_SCHEMA,
            "migration_id": migration_id,
            "project_ref": args.project_ref,
            "preview_hash": expected_hash,
            "state": "prepared",
            "reviewed_preview": reviewed,
            "prepared_at": utc_now(),
        }
        atomic_write_json(receipt_path, receipt)

    reference_mapping = normalize_reference_mapping(
        reviewed.get("reference_mapping")
    )
    remote_preview = dict(reviewed.get("postgresql") or {})
    if not isinstance(receipt.get("postgresql"), Mapping):
        payload = {
            "expected_plan_revision": int(
                remote_preview.get("plan_revision") or 0
            ),
            "expected_migration_hash": str(
                remote_preview.get("migration_hash") or ""
            ),
            "idempotency_key": migration_id,
        }
        mapping_artifact = remote_preview.get("reference_mapping_artifact")
        if isinstance(mapping_artifact, Mapping):
            payload["reference_mapping_artifact"] = dict(mapping_artifact)
        else:
            payload["reference_mapping"] = reference_mapping
        response = _reference_mapping_request(
            args,
            action="project.references.migrate",
            object_ref=args.project_ref,
            payload=payload,
        )
        remote_result = response.get("object")
        if not isinstance(remote_result, Mapping):
            raise DomainError(
                "work_reference_migration_response_invalid",
                "The reference migration operation returned no PostgreSQL receipt.",
                status=502,
            )
        receipt["postgresql"] = dict(remote_result)
        receipt["state"] = "postgresql_applied"
        atomic_write_json(receipt_path, receipt)

    if not isinstance(receipt.get("local_field"), Mapping):
        local_result = apply_local_reference_migration(
            field,
            project_id,
            reviewed_preview=dict(reviewed.get("local_field") or {}),
            lease_owner=lease_owner,
            allow_reference_catch_up=True,
        )
        if local_result.get("schema") != LOCAL_RECEIPT_SCHEMA:
            raise DomainError(
                "field_reference_migration_receipt_invalid",
                "The local reference migration returned an invalid receipt.",
                status=500,
            )
        receipt["local_field"] = local_result
        receipt["state"] = "local_field_applied"
        atomic_write_json(receipt_path, receipt)

    if not isinstance(receipt.get("git"), Mapping):
        index = _complete_remote_plan(args)
        workspace = JournalWorkspace(
            config.journal_workspace_root,
            RepositoryMap.from_mapping(dict(config.source_repositories)),
        )
        context = workspace.context(args.project_ref)
        git_result = sync_plan(
            Path(str(context["local_journal_home"])),
            index,
            reference_mapping=reference_mapping,
            reviewed_reference_files=list(
                (reviewed.get("git") or {}).get("files") or []
            ),
        ).as_dict()
        receipt["git"] = git_result
        receipt["state"] = "completed"
        receipt["completed_at"] = utc_now()
        atomic_write_json(receipt_path, receipt)
    return receipt


def _plan_command(args: Any) -> dict[str, Any]:
    from .plan_cutover import (
        local_plan_cutover_preview,
        prepared_local_plan_from_preview,
        prepare_local_plan_cutover,
        require_local_plan_cutover_ready,
        resume_local_plan_retirement,
        retire_local_plan,
        verify_remote_plan,
    )
    from .plan_migration import migrate_project_plan_storage
    from .plan_storage import BucketedPlanStore
    from .plan_sync import plan_drift, read_plan_export, sync_plan

    config = HostRelayConfig.load(resolve_host_config_path(getattr(args, "config", None)))
    project_id = _project_id(args.project_ref)
    if args.plan_command == "migrate":
        field = SharedFieldStore(config.field_root)
        if not args.confirm_cutover:
            raise DomainError(
                "field_plan_migration_confirmation_required",
                "Pause plan writes, coordinate the PostgreSQL publisher, then pass --confirm-cutover.",
                status=409,
            )
        restorations: dict[str, str] = {}
        for raw in args.restore_ref:
            source, separator, target = str(raw or "").partition("=")
            source = source.strip()
            target = target.strip()
            if not separator or not source or not target:
                raise DomainError(
                    "field_plan_migration_restore_argument_invalid",
                    "Use --restore-ref CURRENT=ESTABLISHED with two complete URIs.",
                )
            previous = restorations.get(source)
            if previous is not None and previous != target:
                raise DomainError(
                    "field_plan_migration_restore_argument_conflict",
                    "One current URI cannot be restored to two different URIs.",
                    status=409,
                    details={"source_ref": source},
                )
            restorations[source] = target
        return migrate_project_plan_storage(
            field,
            project_id,
            migration_id=args.migration_id,
            reference_restorations=restorations,
            expected_project_revision=args.expected_project_revision,
        ).as_dict()

    if args.plan_command == "cutover-local":
        field = SharedFieldStore(config.field_root)
        if args.preview:
            prepared = prepare_local_plan_cutover(field, project_id)
            readiness = require_local_plan_cutover_ready(field, prepared)
            return local_plan_cutover_preview(prepared, readiness=readiness)
        if not args.confirm_cutover:
            raise DomainError(
                "field_plan_cutover_confirmation_required",
                "Run with --preview, inspect every rewrite and recovered outcome, then pass --confirm-cutover.",
                status=409,
            )
        expected_cutover_hash = str(args.expected_cutover_hash or "").strip()
        if not expected_cutover_hash:
            raise DomainError(
                "field_plan_cutover_preview_required",
                "Pass the cutover_content_hash returned by --preview as --expected-cutover-hash.",
                status=409,
            )
        if args.expected_plan_revision is None:
            raise DomainError(
                "field_plan_cutover_revision_required",
                "Pass the current PostgreSQL plan revision as --expected-plan-revision.",
                status=409,
            )
        idempotency_key = str(args.idempotency_key or "").strip()
        if not idempotency_key:
            raise DomainError(
                "field_plan_cutover_idempotency_key_required",
                "Pass one stable --idempotency-key and reuse it for every retry.",
                status=409,
            )
        reviewed_preview_path = str(args.reviewed_preview or "").strip()
        if not reviewed_preview_path:
            raise DomainError(
                "field_plan_cutover_reviewed_artifact_required",
                "Pass the JSON file produced by the exact reviewed --preview invocation.",
                status=409,
            )
        resumed = resume_local_plan_retirement(
            field,
            project_id,
            expected_cutover_content_hash=expected_cutover_hash,
            expected_idempotency_key=idempotency_key,
        )
        if resumed is not None:
            return resumed
        preview_document = load_json(reviewed_preview_path)
        if preview_document.get("ok") is True and isinstance(
            preview_document.get("result"), Mapping
        ):
            preview_document = dict(preview_document["result"])
        reviewed = prepared_local_plan_from_preview(
            preview_document,
            project_id=project_id,
        )
        if reviewed.project_ref != args.project_ref:
            raise DomainError(
                "field_plan_cutover_preview_project_mismatch",
                "The reviewed preview belongs to a different project.",
                status=409,
                details={
                    "expected_project_ref": args.project_ref,
                    "reviewed_project_ref": reviewed.project_ref,
                },
            )
        if reviewed.cutover_content_hash != expected_cutover_hash:
            raise DomainError(
                "field_plan_cutover_preview_changed",
                "The supplied cutover hash differs from the reviewed preview file.",
                status=409,
                details={
                    "expected_cutover_hash": expected_cutover_hash,
                    "observed_cutover_hash": reviewed.cutover_content_hash,
                },
            )
        current = prepare_local_plan_cutover(field, project_id)
        require_local_plan_cutover_ready(field, current)
        if current.cutover_content_hash != reviewed.cutover_content_hash:
            raise DomainError(
                "field_plan_cutover_preview_changed",
                "The local cutover source differs from the operator-reviewed preview.",
                status=409,
                details={
                    "expected_cutover_hash": reviewed.cutover_content_hash,
                    "observed_cutover_hash": current.cutover_content_hash,
                },
            )
        request = argparse.Namespace(
            action="project.plan.import",
            object_ref=args.project_ref,
            payload_json=json.dumps(
                {
                    "package": reviewed.package,
                    "reference_mapping": reviewed.reference_mapping,
                    "assignment_outcomes": reviewed.assignment_outcomes,
                    "source_project_ref": reviewed.project_ref,
                    "expected_plan_revision": args.expected_plan_revision,
                    "idempotency_key": idempotency_key,
                }
            ),
            payload_file="",
            runtime_kind=getattr(args, "runtime_kind", ""),
            runtime_session_id=getattr(args, "runtime_session_id", ""),
            config=getattr(args, "config", None),
        )
        response = _coordinate_command(request)
        imported = response.get("object")
        if not isinstance(imported, Mapping):
            raise DomainError(
                "plan_cutover_import_response_invalid",
                "The plan import operation returned no PostgreSQL generation.",
                status=502,
            )
        remote = _complete_remote_plan(args)
        remote = _complete_remote_plan_with_notes(args, remote)
        verification = verify_remote_plan(reviewed, imported, remote)
        result = retire_local_plan(
            field,
            reviewed,
            import_result=imported,
            remote_verification=verification,
            idempotency_key=idempotency_key,
        )
        return result

    if args.plan_command == "import":
        if args.source:
            journal_home = Path(str(args.source))
        else:
            workspace = JournalWorkspace(
                config.journal_workspace_root,
                RepositoryMap.from_mapping(dict(config.source_repositories)),
            )
            context = workspace.context(args.project_ref)
            journal_home = Path(str(context["local_journal_home"]))
        package = read_plan_export(
            journal_home,
            target_project_ref=args.project_ref,
        )
        source_project_ref = str(package["source_project_ref"])
        if source_project_ref != args.project_ref and not args.rebind_project:
            raise DomainError(
                "plan_import_project_mismatch",
                "The export belongs to another project; pass --rebind-project to choose this target explicitly.",
                status=409,
                details={
                    "source_project_ref": source_project_ref,
                    "target_project_ref": args.project_ref,
                },
            )
        publication = dict(package["publication"])
        request = argparse.Namespace(
            action="project.plan.import",
            object_ref=args.project_ref,
            payload_json=json.dumps(
                {
                    "package": publication,
                    "source_project_ref": source_project_ref,
                    "expected_plan_revision": args.expected_plan_revision,
                    "idempotency_key": args.idempotency_key,
                }
            ),
            payload_file="",
            runtime_kind=getattr(args, "runtime_kind", ""),
            runtime_session_id=getattr(args, "runtime_session_id", ""),
            config=getattr(args, "config", None),
        )
        response = _coordinate_command(request)
        imported = response.get("object")
        if not isinstance(imported, Mapping):
            raise DomainError(
                "plan_import_response_invalid",
                "The plan import operation returned no PostgreSQL generation.",
                status=502,
            )
        return {
            "schema": "problem-board.plan-import.v1",
            "source_project_ref": source_project_ref,
            "target_project_ref": args.project_ref,
            "plan_revision": int(publication.get("plan_revision") or 0),
            "item_count": int(publication.get("item_count") or 0),
            "journal_home": str(journal_home.resolve()),
            "generation": dict(imported),
        }

    index = _complete_remote_plan(args)
    workspace = JournalWorkspace(
        config.journal_workspace_root,
        RepositoryMap.from_mapping(dict(config.source_repositories)),
    )
    context = workspace.context(args.project_ref)
    journal_home = Path(str(context["local_journal_home"]))
    if args.plan_command == "drift":
        return {
            **plan_drift(journal_home, index),
            "project_ref": args.project_ref,
            "journal_home_ref": str(context.get("journal_home_ref") or ""),
        }
    if args.plan_command == "sync":
        field = SharedFieldStore(config.field_root)
        storage = BucketedPlanStore(field._project_dir(project_id) / "plans")
        return {
            **sync_plan(
                journal_home,
                index,
                author=args.author,
                reference_mapping=storage.completed_reference_mapping(),
            ).as_dict(),
            "project_ref": args.project_ref,
            "journal_home_ref": str(context.get("journal_home_ref") or ""),
        }
    raise ValueError(f"unsupported plan command: {args.plan_command}")


def _busy_until_payload(args: Any) -> dict[str, Any]:
    """The worker.estimate payload for pb worker busy-until, or the refusal, before any request."""

    if args.clear and (args.until or args.note):
        raise DomainError(
            "work_worker_estimate_arguments",
            "--clear takes no time and no note.",
        )
    if not args.clear and not args.until:
        raise DomainError(
            "work_worker_estimate_arguments",
            "Give the UTC time you expect to finish by, with --note, or --clear.",
        )
    if args.clear:
        return {"busy_until": ""}
    if not str(args.note or "").strip():
        raise DomainError(
            "work_worker_estimate_note_required",
            "An estimate names the work in one line: pass --note.",
        )
    return {"busy_until": args.until, "note": _prose_from(args.note, None)}


def _worker_command(args: Any) -> dict[str, Any]:
    if args.worker_command == "authorize":
        path = resolve_host_config_path(getattr(args, "config", None))
        return asyncio.run(
            authorize_worker_profile(
                path,
                profile_name=args.profile,
                wait_seconds=args.wait_seconds,
                no_open=args.no_open,
                callback_port=args.callback_port,
                device=args.device,
                replace_card=args.replace_card,
                coordinator=args.coordinator,
            )
        )
    if args.worker_command == "list":
        path = resolve_host_config_path(getattr(args, "config", None))
        config = HostRelayConfig.load(path)
        return {
            "config": str(path),
            "workers": SharedFieldStore(config.field_root).list_workers(),
        }
    identity = _identity(args)
    if args.worker_command == "whoami":
        return {
            "runtime_kind": identity.runtime_kind,
            "runtime_session_id": identity.runtime_session_id,
            "worker_identity": identity.worker_identity,
            "worker_name": identity.worker_name,
            "identity_rule": "runtime_kind + native resumable session id",
            "channel": _whoami_channel(args, identity),
        }
    path = resolve_host_config_path(getattr(args, "config", None))
    config = HostRelayConfig.load(path)
    channel = config.worker(identity)
    field = SharedFieldStore(config.field_root)
    if args.worker_command == "listen":
        from .agent_session import default_profile_name

        profile_name = (
            channel.profile
            if channel is not None
            else default_profile_name(identity, config.profile_scope)
        )
        profile = inspect_profile_metadata(config, profile_name)
        same_active_channel = bool(
            channel is not None
            and channel.profile == profile_name
            and channel.state == "active"
        )
        channel = enroll_worker_channel(
            path,
            identity=identity,
            profile=profile_name,
            worker_alias=args.alias,
            capabilities=args.capability,
            authorized=same_active_channel,
            working_directory=str(Path.cwd().resolve()),
        )
        try:
            previous_worker = field.read_worker(identity.worker_name)
        except DomainError as exc:
            if exc.code != "field_record_not_found":
                raise
            previous_worker = {}
        previous_authorization = (
            dict(previous_worker.get("authorization") or {})
            if isinstance(previous_worker.get("authorization"), dict)
            else {}
        )
        authorization_state = (
            "active" if channel.state == "active" else str(profile["state"])
        )
        if (
            channel.state != "active"
            and profile["state"] != PROFILE_METADATA_ABSENT
            and previous_authorization.get("state")
            not in {None, "", "active", PROFILE_METADATA_ABSENT}
        ):
            authorization_state = str(previous_authorization["state"])
        field.register_worker(
            worker_name=identity.worker_name,
            worker_alias=channel.worker_alias,
            worker_identity=identity.worker_identity,
            runtime_kind=identity.runtime_kind,
            runtime_session_id=identity.runtime_session_id,
            capabilities=channel.capabilities,
            authority_label=f"connection-hub:{channel.profile}",
            host_id=config.host_id,
            host_label=config.host_label,
            host_kind=config.host_kind,
            relay_id=f"{config.relay_id}-{identity.worker_name}",
            reconcile_ceiling_seconds=config.reconcile_ceiling_seconds,
            control_plane_state=(
                "authorized_waiting_for_relay"
                if channel.state == "active"
                else "pending_authorization"
            ),
            authorization_state=authorization_state,
            authorization_reason=(
                "relay_proved_problem_board_card"
                if channel.state == "active"
                else "profile_metadata_not_authority"
            ),
            authorization_action=(
                ""
                if channel.state == "active"
                else str(previous_authorization.get("action") or "authorize")
            ),
        )
        listening = listen_worker_input(
            field,
            worker_name=identity.worker_name,
            check_interval_seconds=args.check_interval,
        )
        result = {
            "config": str(path),
            "channel": channel.to_mapping(),
            "authorization": profile,
            "listening": listening,
        }
        if channel.state != "active":
            authorize = authorization_command(
                channel.profile,
                config_path=path if getattr(args, "config", None) else None,
            )
            result["next"] = {
                "requires_user_action": True,
                "requires_user_consent": (
                    profile["state"] == PROFILE_METADATA_ABSENT
                    or authorization_state == "credential_expired_or_invalid"
                ),
                "authorize": authorize,
                "after_authorization": "No second enrollment is required.",
                "relay_status": ["pb", "relay-service", "status"],
                "rule": (
                    "This session requested authorization. The user grants it in "
                    "an interactive terminal; the login relay keeps credential "
                    "custody and proves the stream. It publishes this worker and "
                    "notifies the exact session when that runtime supports external "
                    "input; otherwise the state waits for the session's next inbox check."
                ),
            }
        else:
            result["next"] = {
                "receive": _worker_tokens(identity, "receive"),
                "watch": (
                    None
                    if identity.runtime_kind == "codex"
                    else _worker_tokens(identity, "watch")
                ),
                "rule": (
                    "The host login relay queues a standard inbox-check instruction into "
                    "this exact Codex session. Only that native queue can create a Codex "
                    "model turn; pb worker watch in a background terminal is diagnostic "
                    "only. This model then calls pb worker receive and settles every "
                    "returned lease."
                    if identity.runtime_kind == "codex"
                    else "Start exactly one pb worker watch process through this "
                    "runtime's session-owned background facility. The process emits only "
                    "availability. On a wake, this model calls pb worker receive, handles "
                    "and settles every returned lease, and leaves the watch running."
                ),
            }
        return result
    if channel is None:
        raise DomainError(
            "work_relay_worker_not_enrolled",
            "This coding-agent session is not enrolled. Run worker listen first.",
            status=404,
        )
    if args.worker_command == "quarantine":
        if args.quarantine_command == "list":
            return list_quarantine(
                field, identity.worker_name, cursor=args.cursor, limit=args.limit
            )
        if args.quarantine_command == "read":
            return read_quarantine(
                field,
                identity.worker_name,
                project_ref=args.project_ref,
                message_ref=args.message_ref,
            )
        return settle_quarantine(
            field,
            identity.worker_name,
            project_ref=args.project_ref,
            message_ref=args.message_ref,
            expected_quarantined_at=args.expected_quarantined_at,
            action=args.quarantine_command,
            reason=str(getattr(args, "reason", "") or ""),
        )
    if args.worker_command == "receive":
        return pull_worker_input(
            field,
            worker_name=identity.worker_name,
            limit=args.limit,
            lease_seconds=args.lease_seconds,
            wake_id=args.wake_id,
        )
    if args.worker_command == "leases":
        worker = field.read_worker(identity.worker_name)
        lease_owner = str(
            worker.get("runtime_session_id") or identity.runtime_session_id
        )
        return field.list_worker_mail_leases(
            identity.worker_name,
            lease_owner=lease_owner,
            cursor=args.cursor,
            limit=args.limit,
        )
    if args.worker_command == "watch":
        interval = max(5, min(int(args.check_interval), 300))
        coalesce = max(0.0, min(float(args.coalesce_seconds), 5.0))
        if args.once:
            return probe_worker_input(field, worker_name=identity.worker_name)
        # A watch replaced by a newer one is stopped with SIGTERM (the Claude
        # Code guard does it every cycle). That is its normal end: exit 0 and
        # print nothing, since every stdout line is an event, so the harness
        # does not report each replaced watch as failed (W182, operator
        # 2026-09-25: "script failed (exit 144)" on every guard). The handler
        # is in place before the attachment says this watch runs, so no
        # SIGTERM sent on that record can find the default handler
        # (codex-ui review of ae#137).
        previous = signal.signal(signal.SIGTERM, _end_replaced_watch)
        try:
            # The Stop hook reads this to tell a running watch from none (W182).
            field.record_watch_attachment(
                str(field.read_worker(identity.worker_name).get("worker_name") or identity.worker_name),
                pid=os.getpid(),
                runtime_session_id=identity.runtime_session_id,
            )
            for event in worker_watch_events(
                field,
                worker_name=identity.worker_name,
                check_interval_seconds=interval,
                coalesce_seconds=coalesce,
            ):
                print(json.dumps(event, ensure_ascii=True, sort_keys=True), flush=True)
        finally:
            signal.signal(signal.SIGTERM, previous)
    if args.worker_command == "detach":
        return {
            "worker": identity.worker_name,
            "session": field.detach_worker_listener(identity.worker_name),
            "relay_channel": "close_then_disable_on_reconciliation",
        }
    if args.worker_command == "busy-until":
        response = _reference_mapping_request(
            args,
            action="worker.estimate",
            object_ref="work:worker:self",
            payload=_busy_until_payload(args),
        )
        worker = response.get("object") if isinstance(response.get("object"), Mapping) else {}
        return {
            "worker": identity.worker_name,
            "busy_until": str(worker.get("busy_until") or ""),
            "busy_note": str(worker.get("busy_note") or ""),
            "busy_set_at": str(worker.get("busy_set_at") or ""),
            "rule": (
                "Cleared: the board shows no estimate for this worker."
                if args.clear
                else "The board shows this until it passes or you set or clear it again."
            ),
        }
    if args.worker_command == "workspace":
        if args.list:
            return {"worker": identity.worker_name, "workspaces": field.workspaces(identity.worker_name)}
        if args.clear:
            if not str(args.assignment_ref or "").strip():
                raise ValueError("--clear needs --assignment-ref")
            cleared = field.clear_workspace(
                identity.worker_name,
                assignment_ref=args.assignment_ref,
                repository_ref=args.repository,
            )
            return {"worker": identity.worker_name, "cleared": cleared, "workspaces": field.workspaces(identity.worker_name)}
        if not (str(args.assignment_ref or "").strip() and str(args.repository or "").strip() and str(args.path or "").strip()):
            raise ValueError("declare a workspace with --assignment-ref, --repository and --path, or pass --list or --clear")
        declared = field.declare_workspace(
            identity.worker_name,
            assignment_ref=args.assignment_ref,
            repository_ref=args.repository,
            path=args.path,
        )
        return {"worker": identity.worker_name, "declared": declared, "workspaces": field.workspaces(identity.worker_name)}
    if args.worker_command == "idle":
        idle_project = parse_ref(args.project_ref).object_id
        require_plan_item(
            field,
            project_id=idle_project,
            worker_name=identity.worker_name,
            work_ref=args.work_ref,
        )
        declared = field.declare_worker_idle(
            identity.worker_name,
            reason=args.reason,
            summary=args.summary,
            last_work_ref=args.work_ref,
        )
        # Tell the coordinator on the channel it already listens on, rather than
        # leaving it to notice an absence. The report is the point: an agent
        # that stops appearing is indistinguishable from one that cannot be
        # heard, and the coordinator usually finds out neither way.
        #
        # It is a project event, not mail (operator ruling, 2026-09-23, W182):
        # a notice a `pb` state command writes is a fact about the worker, not
        # a message from it, and it carries its facts so the board can show it.
        reason = args.reason.replace("_", " ")
        last = args.work_ref or "nothing recorded"
        notice = field.enqueue_service_event(
            parse_ref(args.project_ref).object_id,
            worker_name=identity.worker_name,
            kind="worker.idle",
            summary=f"Out of work: {reason}. {args.summary} Last worked on: {last}.",
            source_event_ref=f"idle:{identity.worker_name}:{declared['since']}",
            work_ref=args.work_ref,
            metadata={
                "notice": "idle",
                "idle_reason": args.reason,
                "since": declared["since"],
                "summary": args.summary,
                "last_work_ref": args.work_ref or "",
                "reported_by": "pb worker idle",
            },
            idempotency_key=f"idle:{identity.worker_name}:{declared['since']}",
        )
        return {"worker": identity.worker_name, "idle": declared, "notice": notice}
    if args.worker_command == "inspect":
        quarantined = list_quarantine(field, identity.worker_name, limit=20)
        worker = field.read_worker(identity.worker_name)
        active_leases = field.list_worker_mail_leases(
            identity.worker_name,
            lease_owner=str(
                worker.get("runtime_session_id") or identity.runtime_session_id
            ),
            limit=20,
        )
        channel_row = channel.to_mapping()
        reconnect = channel_reconnect_state(path, identity.worker_name)
        if reconnect is not None and channel.state == "active":
            # Configured active, but the relay is reconnecting it.
            channel_row["state"] = "reconnecting"
            channel_row["connection"] = reconnect
        return {
            "config": str(path),
            "channel": channel_row,
            "authorization": inspect_profile_metadata(config, channel.profile),
            "worker": worker,
            "session": field.worker_listener_session(identity.worker_name),
            "mail": {
                "quarantine_count": quarantined["total"],
                "quarantine": quarantined["items"],
                "quarantine_next_cursor": quarantined["next_cursor"],
                "active_leases": active_leases,
            },
        }
    if args.worker_command == "context":
        return _worker_project_context(
            config, field, str(args.project_ref), channel=config.worker(identity)
        )
    project_ref = str(getattr(args, "project_ref", "") or "").strip()
    parsed_project = parse_ref(project_ref) if project_ref else None
    if parsed_project is not None and parsed_project.kind != "project":
        raise DomainError(
            "field_project_ref_invalid", "Expected a work:project reference."
        )
    project_id = parsed_project.object_id if parsed_project is not None else ""
    if args.worker_command == "deliveries":
        return field.list_mail_deliveries(
            worker_name=identity.worker_name,
            project_ref=project_ref,
            states=args.state or ("refused",),
            cursor=args.cursor,
            limit=args.limit,
        )
    if args.worker_command == "outbox-status":
        return field.worker_outbox_status(
            worker_name=identity.worker_name,
            outbox_id=args.outbox_id,
        )
    if args.worker_command == "replay-delivery":
        return field.replay_mail_delivery(
            outbox_id=args.outbox_id,
            worker_name=identity.worker_name,
            recipient=args.recipient,
            kind=args.kind,
        )
    if args.worker_command == "lease-read":
        message = field.read_worker_mail_lease(
            project_id,
            worker_name=identity.worker_name,
            message_ref=args.message_ref,
            lease_id=args.lease_id,
            lease_owner=identity.runtime_session_id,
        )
        projected_message = worker_message_with_attachments(
            message,
            project_ref=project_ref,
            attachment_root=(
                field._mail_root(project_id, identity.worker_name) / "attachments"
            ),
            runtime_kind=identity.runtime_kind,
            runtime_session_id=identity.runtime_session_id,
        )
        return {
            "schema": "problem-board.worker-lease-read.v1",
            "project_ref": project_ref,
            "message": projected_message,
            "replayed": True,
            "settlement": {
                "required": True,
                "outcomes": ["acknowledged", "refused"],
            },
        }
    if args.worker_command == "attachment-read":
        return field.read_worker_mail_attachment(
            project_id,
            worker_name=identity.worker_name,
            message_ref=args.message_ref,
            lease_id=args.lease_id,
            lease_owner=identity.runtime_session_id,
            file_ref=args.file_ref,
        )
    if args.worker_command == "item-attach":
        return _worker_item_attach(args)
    if args.worker_command == "item-attachment-read":
        return _worker_item_attachment_read(args)
    if args.worker_command == "renew":
        return field.renew_mail_lease(
            project_id,
            worker_name=identity.worker_name,
            message_ref=args.message_ref,
            lease_id=args.lease_id,
            lease_owner=identity.runtime_session_id,
            lease_seconds=args.lease_seconds,
            note=args.note,
        )
    if args.worker_command == "send":
        payload = (
            _json_object(args.payload_file, field="payload-file")
            if args.payload_file
            else {}
        )
        body = (
            Path(args.body_file).expanduser().read_text(encoding="utf-8")
            if args.body_file
            else args.body
        )
        # A template slot left in a subject or body is a defect the reader
        # would otherwise report back (2026-09-23, twenty of them).
        refuse_unresolved_slots(args.subject, argument="--subject")
        refuse_unresolved_slots(body, argument="--body-file" if args.body_file else "--body")
        resolution = field.resolve_mail_recipient(project_id, args.recipient)
        recipient = str(resolution["worker_name"])
        route = str(resolution["route"])
        if args.route != "auto" and args.route != route:
            raise DomainError(
                "field_mail_route_mismatch",
                f"The stable recipient requires the {route} route.",
                status=409,
                details={"recipient": recipient, "required_route": route},
            )
        correlation_id = args.correlation_id
        if not str(correlation_id or "").strip() and str(args.reply_to or "").strip():
            # Answering a leased message correlates to the control that carried
            # it, never to the message ref. The value is on the leased message,
            # so resolve it here rather than making every agent copy it by hand.
            correlation_id = field.leased_correlation(
                project_id,
                worker_name=identity.worker_name,
                message_ref=args.reply_to,
            ) or correlation_id
        attachments = [{"path": str(Path(item).expanduser().resolve())} for item in (getattr(args, "attach", None) or [])]
        if not str(body or "").strip() and not attachments:
            raise DomainError(
                "field_mail_content_required",
                "Mail requires text or at least one attachment.",
            )
        require_plan_item(
            field,
            project_id=project_id,
            worker_name=identity.worker_name,
            work_ref=args.work_ref,
        )
        if route == "local":
            if attachments:
                raise DomainError(
                    "field_attachments_operator_only",
                    "Attachments travel to the operator inbox; local worker mail carries paths in its body.",
                )
            return field.send_mail(
                project_id,
                sender=identity.worker_name,
                recipient=recipient,
                kind=args.kind,
                subject=args.subject,
                body=body,
                payload=payload,
                work_ref=args.work_ref,
                correlation_id=correlation_id,
                reply_to=args.reply_to,
                idempotency_key=args.idempotency_key,
            )
        _raise_if_send_channel_reconnecting(
            path, identity.worker_name, idempotency_key=args.idempotency_key
        )
        return field.enqueue_remote_mail(
            project_id,
            sender=identity.worker_name,
            recipient=recipient,
            kind=args.kind,
            subject=args.subject,
            body=body,
            payload=payload,
            work_ref=args.work_ref,
            correlation_id=correlation_id,
            reply_to=args.reply_to,
            idempotency_key=args.idempotency_key,
            attachments=attachments,
        )
    if args.worker_command == "settle":
        settled = field.settle_mail(
            project_id,
            worker_name=identity.worker_name,
            message_ref=args.message_ref,
            lease_id=args.lease_id,
            lease_owner=identity.runtime_session_id,
            outcome=args.outcome,
            summary=args.summary,
        )
        field.record_worker_mail_settlement(
            identity.worker_name, message_ref=args.message_ref
        )
        return settled
    if args.worker_command == "report":
        if parsed_project is None:
            raise DomainError(
                "field_project_ref_required",
                "Reporting assigned work requires a project ref.",
            )
        return submit_assignment_report(
            field,
            project_id,
            worker_name=identity.worker_name,
            assignment_ref=args.assignment_ref,
            ownership_version=args.ownership_version,
            state=args.state,
            summary=_prose_from(args.summary, getattr(args, "summary_file", None)),
            result_ref=args.result_ref,
            source_event_ref=args.source_event_ref,
            review_look_at=args.review_look_at,
            review_could_not_verify=args.review_could_not_verify,
            scope=str(getattr(args, "scope", "") or ""),
            wait_seconds=args.wait_seconds,
            status_command_prefix=("pb", "worker", "outbox-status"),
        )
    if args.worker_command == "project-report":
        if parsed_project is None:
            raise DomainError(
                "field_project_ref_required",
                "A project report request requires its project ref.",
            )
        settle_command = [
            "pb", "worker", "settle",
            "--project-ref", project_ref,
            "--message-ref", args.message_ref,
            "--lease-id", args.lease_id,
            "--outcome", "acknowledged",
        ]

        def status_command(outbox_id: str) -> list[str]:
            return [
                "pb", "worker", "project-report", "status",
                "--project-ref", project_ref,
                "--message-ref", args.message_ref,
                "--lease-id", args.lease_id,
                "--outbox-id", outbox_id,
            ]

        if args.project_report_command == "status":
            row = field.read_outbox_record(args.outbox_id)
            if row is None:
                raise DomainError(
                    "field_outbox_record_not_found",
                    "No outbox record has that id on this machine.",
                    status=404,
                    details={"outbox_id": args.outbox_id},
                )
            return _report_publication_outcome(
                queued={
                    "outbox_id": args.outbox_id,
                    "report_ref": str(row.get("object_ref") or ""),
                    "message_ref": str(row.get("source_message_ref") or ""),
                },
                row=row,
                settle_command=settle_command,
                status_command=status_command(args.outbox_id),
            )
        if args.project_report_command in {"publish", "preview"}:
            summary_file = getattr(args, "summary_file", None)
            summary = (
                sys.stdin.read()
                if summary_file == "-"
                else Path(summary_file).expanduser().read_text(encoding="utf-8")
                if summary_file
                else (args.summary or "")
            )
        if args.project_report_command == "preview":
            from ..contract.project_report_contract import build_submission

            request = field.leased_project_report(
                project_id,
                worker_name=identity.worker_name,
                message_ref=args.message_ref,
                lease_id=args.lease_id,
                lease_owner=identity.runtime_session_id,
            )
            # A preview may come before the summary exists: the author reads the
            # delta first and writes from it.
            submission = build_submission(
                summary=summary.strip() or "(preview, summary not written yet)",
                not_seen=list(args.not_seen or []),
            )
            response = _coordinate_command(
                argparse.Namespace(
                    **{
                        **vars(args),
                        "action": "project.report.publish",
                        "object_ref": request["report_ref"],
                        "payload_json": json.dumps(
                            {"document": submission, "dry_run": True}
                        ),
                        "payload_file": None,
                        "route": "relay",
                        "timeout_seconds": DEFAULT_COORDINATE_TIMEOUT_SECONDS,
                    }
                )
            )
            composed = response.get("object") if isinstance(response.get("object"), Mapping) else response
            return {
                "published": False,
                "dry_run": True,
                "report_ref": request["report_ref"],
                "ask": request["ask"],
                "document": composed.get("document"),
                "rule": (
                    "Nothing was stored. This is the document the service would "
                    "compose now: write the summary from it, then run publish."
                ),
            }
        if args.project_report_command == "publish":
            # The author sends the summary, the work it names, its files, and
            # what the author could not see. The service composes the rest from
            # rows it owns, so no plan is read here or anywhere else.
            queued = field.enqueue_project_report_publish(
                project_id,
                worker_name=identity.worker_name,
                message_ref=args.message_ref,
                lease_id=args.lease_id,
                lease_owner=identity.runtime_session_id,
                summary=summary,
                attachments=[
                    {"path": str(Path(item).expanduser().resolve())}
                    for item in args.attach
                ],
                not_seen=list(args.not_seen or []),
            )
            row = _await_outbox_outcome(
                field,
                str(queued.get("outbox_id") or ""),
                wait_seconds=args.wait_seconds,
            )
            return _report_publication_outcome(
                queued=queued,
                row=row,
                settle_command=settle_command,
                status_command=status_command(str(queued.get("outbox_id") or "")),
            )
        result = field.enqueue_project_report_failure(
            project_id,
            worker_name=identity.worker_name,
            message_ref=args.message_ref,
            lease_id=args.lease_id,
            lease_owner=identity.runtime_session_id,
            error_code=args.error_code,
            error_summary=args.error_summary,
        )
        return {
            **result,
            "next": {
                "settle": settle_command,
                "rule": (
                    "The failure is queued for the service. Settle this exact mail "
                    "lease once; the relay delivers the queued result independently."
                ),
            },
        }
    if args.worker_command == "journal-search":
        if parsed_project is None:
            project_ref = _attended_project_ref(field, identity.worker_name)
        workspace = JournalWorkspace(
            config.journal_workspace_root,
            RepositoryMap.from_mapping(dict(config.source_repositories)),
        )
        entries = workspace.search(
            args.query,
            project_ref=project_ref,
            work_ref=args.work_ref,
            worker_name=args.author,
            status=args.status,
            limit=args.limit,
        )
        return {"entries": entries, "index": workspace.index_status()}
    if args.worker_command in {
        "journal-index",
        "journal-index-resume",
        "journal-index-status",
    }:
        workspace = JournalWorkspace(
            config.journal_workspace_root,
            RepositoryMap.from_mapping(dict(config.source_repositories)),
        )
        workflow = JournalIndexWorkflow(
            field=field,
            workspace=workspace,
            operation_root=field.control / "operations" / "journal-index",
        )
    if args.worker_command == "journal-index":
        if parsed_project is None:
            raise DomainError(
                "field_project_ref_required",
                "Indexing a project journal entry requires a project ref.",
            )
        return workflow.start(
            project_id=project_id,
            worker_name=identity.worker_name,
            repository_journal_ref=args.repository_journal_ref,
        )
    if args.worker_command == "journal-index-resume":
        return workflow.resume(
            args.operation_id,
            worker_name=identity.worker_name,
        )
    if args.worker_command == "journal-index-status":
        if parsed_project is None:
            raise DomainError(
                "field_project_ref_required",
                "Journal operation status requires a project ref.",
            )
        if args.operation_id:
            return workflow.status(
                args.operation_id,
                project_id=project_id,
            )
        return workflow.status_by_outbox(
            args.outbox_id,
            project_id=project_id,
            repository_journal_ref=args.repository_journal_ref,
        )
    raise ValueError(f"unsupported worker command: {args.worker_command}")


def _end_replaced_watch(signum: int, frame: Any) -> None:
    """SIGTERM ends a watch cleanly: status 0, no output (W182)."""

    del signum, frame
    raise SystemExit(0)


def _whoami_channel(args: Any, identity: WorkerSessionIdentity) -> dict[str, Any]:
    """This session's channel on the host: alias, profile and state (W304 C11).

    Read from the local host configuration only, so it answers from a plain
    shell as well as inside the session. A host without configuration, or a
    session never enrolled, says so instead of failing.
    """

    try:
        config = HostRelayConfig.load(
            resolve_host_config_path(getattr(args, "config", None))
        )
    except (DomainError, OSError, ValueError):
        return {"enrolled": False, "reason": "host_not_configured"}
    channel = config.worker(identity)
    if channel is None:
        return {"enrolled": False, "reason": "session_not_enrolled"}
    try:
        board_record = SharedFieldStore(config.field_root).worker_board_record(identity.worker_name)
    except DomainError:
        board_record = {}
    return {
        "enrolled": True,
        "worker_alias": channel.worker_alias,
        "profile": channel.profile,
        "state": channel.state,
        # Who owns this agent and which provider account it runs under, as
        # the board records them (W304 finding 47).
        "board_record": board_record
        or {"state": "not_received", "note": "The relay stores it from its next heartbeat."},
    }


def _attended_project_ref(field: SharedFieldStore, worker_name: str) -> str:
    """The one project this worker attends, for a command that names none (W305).

    An agent about to act on a subject searches its project's journal with
    only a query. A worker attending no project, or several, is asked to name
    one, with the choices.
    """

    try:
        worker = field.read_worker(worker_name)
    except DomainError:
        worker = {}
    attended = sorted(
        str(ref) for ref in worker.get("attended_project_refs") or [] if str(ref or "").strip()
    )
    if len(attended) == 1:
        return attended[0]
    raise DomainError(
        "field_project_ref_required",
        "This worker attends several projects: name one with --project-ref."
        if attended
        else "This worker attends no project, so it has no project journal: name one with --project-ref.",
        details={"attended_project_refs": attended},
    )


def _own_board_record(field: SharedFieldStore, channel: Any) -> dict[str, Any]:
    name = str(getattr(channel, "worker_name", "") or "")
    try:
        record = field.worker_board_record(name) if name else {}
    except DomainError:
        record = {}
    return record or {"state": "not_received", "note": "The relay stores it from its next heartbeat."}


def _worker_project_context(
    config: HostRelayConfig,
    field: SharedFieldStore,
    project_ref: str,
    *,
    channel: Any = None,
) -> dict[str, Any]:
    """Where this worker's project stands on this host: its team first, then its journal (W304 finding 39).

    A just-linked agent could not see its coordinator or teammates: the
    command returned only the journal, and failed when the journal was not
    bound yet. The team comes from the record the relay writes on
    attendance, and a journal that is not available is a named state.
    """

    parsed = parse_ref(project_ref)
    if parsed.kind != "project":
        raise DomainError("field_project_ref_invalid", "Expected a work:project reference.")
    on_host = field._project_path(parsed.object_id).exists()
    team = field.read_project_team(parsed.object_id) if on_host else []
    repositories = (
        field.read_project_repositories(parsed.object_id)
        if on_host
        else {"revision": 0, "repositories": []}
    )
    try:
        journal = JournalWorkspace(
            config.journal_workspace_root,
            RepositoryMap.from_mapping(dict(config.source_repositories), require_existing=False),
        ).context(project_ref)
        journal_state = {"journal_state": "available"}
    except DomainError as exc:
        journal = {}
        journal_state = {
            "journal_state": "unavailable",
            "journal_error_code": exc.code,
            "journal_error": str(exc),
        }
    # The folder this session enrolled from (`pb worker listen`), which the
    # host procedure makes the agent's own workspace. Repositories are set up
    # inside it, one folder per alias (W304 finding 39).
    workspace = str(getattr(channel, "working_directory", "") or "")
    return {
        "project_ref": project_ref,
        "project_on_this_host": on_host,
        "workspace": workspace,
        **(
            {}
            if workspace
            else {
                "workspace_note": (
                    "This session enrolled before its folder was recorded: run "
                    "`pb worker listen` from your workspace folder to record it."
                )
            }
        ),
        **(
            {}
            if on_host
            else {"project_note": "The relay writes this project's record on its next poll."}
        ),
        "coordinators": [
            str(member.get("worker_name") or "")
            for member in team
            if str(member.get("role") or "") == "coordinator"
        ],
        "team": team,
        # This worker's own owner and provider account (W304 finding 47).
        "self": _own_board_record(field, channel),
        # The repositories to set the workspace up from (W304 finding 39).
        "repositories": repositories["repositories"],
        "repositories_revision": repositories["revision"],
        **journal_state,
        **journal,
    }


PROCEDURE_TARGETS = ("codex", "claude-code")


def _procedure_targets(args: Any) -> tuple[list[str], str]:
    """The agent kinds whose installed procedure a command checks, and where they came from.

    Named targets are checked as named. Otherwise the kinds this host's relay
    channels run: a host that runs only Claude Code may still hold an old Codex
    skill, and that copy is nobody's procedure (W304 C9, spark1, 2026-09-24).
    A host with no configuration or no channel yet checks both kinds.
    """

    named = [str(item) for item in (getattr(args, "target", None) or [])]
    if named:
        return named, "named"
    try:
        config = HostRelayConfig.load(
            resolve_host_config_path(getattr(args, "config", None))
        )
    except (DomainError, OSError, ValueError):
        return list(PROCEDURE_TARGETS), "default"
    kinds = {
        channel.runtime_kind
        for channel in config.workers
        if channel.state != "disabled" and channel.runtime_kind in PROCEDURE_TARGETS
    }
    if not kinds:
        return list(PROCEDURE_TARGETS), "default"
    return [kind for kind in PROCEDURE_TARGETS if kind in kinds], "host_channels"


def _procedure_command(args: Any) -> dict[str, Any]:
    from .procedures import (
        install_agent_procedure,
        source_package,
        source_path,
        verify_agent_procedure,
    )

    targets, targets_from = _procedure_targets(args)
    package = source_package()

    if args.procedure_command == "show":
        return {
            "procedure": str(source_path()),
            "package": package,
            "installed": verify_agent_procedure(
                targets, home=getattr(args, "home", None)
            ),
            "instruction": (
                "Install the complete package for future sessions. A running session "
                "must explicitly re-read its installed SKILL.md."
            ),
        }
    if args.procedure_command == "verify":
        verified = verify_agent_procedure(targets, home=args.home)
        if any(row["state"] != "current" for row in verified):
            raise DomainError(
                "work_agent_procedure_verification_failed",
                "One or more installed worker procedure packages are not current and intact.",
                status=409,
                details={
                    "package": package,
                    "targets": targets,
                    "targets_from": targets_from,
                    "installed": verified,
                },
            )
        return {
            "procedure": str(source_path()),
            "package": package,
            "targets": targets,
            "targets_from": targets_from,
            "verified": verified,
        }
    if args.procedure_command == "install":
        claude_code = "claude-code" in (args.target or [])
        if claude_code:
            # The hooks name this install's own pb; one that cannot be named is refused before anything is written.
            from .claude_settings import pb_command

            hook_pb = pb_command()
        installed = install_agent_procedure(
            args.target,
            home=args.home,
            force=args.force,
            allow_downgrade=bool(getattr(args, "allow_downgrade", False)),
        )
        result = {
            "procedure": str(source_path()),
            "package": package,
            "installed": installed,
        }
        if claude_code:
            # The status line and hooks a Claude Code worker needs (W304 finding 45).
            from .claude_settings import merge_claude_code_settings
            from .procedures import _home_path

            result["claude_code_settings"] = merge_claude_code_settings(_home_path(args.home), pb=hook_pb)
        return result
    raise ValueError(f"unsupported procedure command: {args.procedure_command}")


def _relay_service_command(args: Any) -> dict[str, Any]:
    from .relay_service import RelayService

    service = RelayService.create(resolve_host_config_path(args.config))
    name = args.relay_service_command
    if name == "install":
        return service.install()
    operation = getattr(service, name)
    return operation()


def _source_command(args: Any) -> dict[str, Any]:
    from .relay_service import STARTUP_WAIT_SECONDS
    from .source_control import ClientSourceController

    controller = ClientSourceController(resolve_host_config_path(args.config))
    if args.source_command == "status":
        return controller.status()
    wait = getattr(args, "wait_seconds", None)
    wait_seconds = STARTUP_WAIT_SECONDS if wait is None else float(wait)
    if args.source_command == "use-code":
        return controller.use_code(
            repository=args.repository,
            ref=args.ref,
            expect=args.expect,
            kdcube_repository=args.kdcube_repository,
            kdcube_ref=args.kdcube_ref,
            expect_kdcube=args.expect_kdcube,
            wait_seconds=wait_seconds,
        )
    if args.source_command == "use-release":
        return controller.use_release(
            expect_version=args.expect_version,
            wait_seconds=wait_seconds,
        )
    raise ValueError(f"unsupported source command: {args.source_command}")


async def _relay(args: Any) -> Any:
    from connection_hub_cli.cli import build_services
    from connection_hub_cli.paths import StatePaths
    from connection_hub_cli.profile_connection import (
        connect_profile_tools,
        resolve_profile_bearer,
    )
    from service_foundation.host_relay import HostRelayPolicy, HostRelayRuntime

    from app_foundation.data_bus import (
        DelegatedCardCredential,
        FederatedDataBusClient,
    )

    from .mcp_client import (
        ProblemBoardDataBusClient,
        ProblemBoardMcpClient,
        governed_endpoint_identity,
    )
    from .relay import (
        CONFIG_SCHEMA,
        ProblemBoardHostRelayAdapter,
        ProblemBoardRelaySupervisor,
        RelayConfig,
        channel_lifecycle_labels,
        transient_failure,
    )
    from .relay_admission import open_with_one_refresh, reconnect_credential_source
    from .relay_failures import descriptor_limit_label, staged_failure
    from .relay_service import CLIENT_SOURCE_PATHS, STARTUP_RECORD_NAME
    from .relay_source import describe_source, source_line, write_startup_record
    from ..contract.worker_stream import worker_stream_partition

    config_path = resolve_host_config_path(args.config)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    if raw.get("schema") == HOST_CONFIG_SCHEMA:
        host = HostRelayConfig.load(config_path)

        @asynccontextmanager
        async def connector(host_config, channel, *, replacement_epoch):
            paths = (
                StatePaths(host_config.connection_hub_state_root)
                if host_config.connection_hub_state_root is not None
                else StatePaths.default()
            )
            services = build_services(paths=paths)
            profile = services.profiles.require(channel.profile)
            if profile.endpoint.rstrip("/") != host_config.endpoint.rstrip("/"):
                raise DomainError(
                    "work_relay_profile_endpoint_mismatch",
                    "The worker profile points at a different governed MCP endpoint.",
                    status=409,
                )
            # The card opens the session itself. There is no exchange, so
            # nothing here mints a credential that outlives the card it came
            # from: revoking the card ends this session rather than leaving a
            # token valid until it lapses.
            started = time.monotonic()
            try:
                bearer = await resolve_profile_bearer(
                    profile_name=channel.profile,
                    profiles=services.profiles,
                    credentials=services.credentials,
                    oauth_sessions=services.oauth_profile_sessions,
                )
            except Exception as exc:
                failure = staged_failure(
                    exc, operation="card.resolve_credential", target=channel.profile,
                    elapsed_seconds=time.monotonic() - started,
                    retryable=retryable(exc),
                )
                if failure is exc:
                    raise
                raise failure from exc
            if not profile.access_id:
                raise DomainError(
                    "work_relay_card_address_missing",
                    "The worker profile does not record its delegated Card address.",
                    status=409,
                )
            endpoint = urlsplit(host_config.endpoint)
            bundle_id, card_resource = governed_endpoint_identity(
                host_config.endpoint
            )
            platform_url = urlunsplit((endpoint.scheme, endpoint.netloc, "", "", ""))

            def card_credential(current_bearer: str) -> DelegatedCardCredential:
                return DelegatedCardCredential(
                    tenant=host_config.tenant,
                    project=host_config.platform_project,
                    bundle_id=bundle_id,
                    resource=card_resource,
                    bearer_token=current_bearer,
                )

            async def current_bearer() -> str:
                return await resolve_profile_bearer(
                    profile_name=channel.profile,
                    profiles=services.profiles,
                    credentials=services.credentials,
                    oauth_sessions=services.oauth_profile_sessions,
                )

            # A refused admission with an OAuth-backed profile re-mints the
            # session once through the card (relay_admission). A static bearer
            # has nothing to refresh with and keeps the refusal as it came.
            refresh_bearer = None
            if (
                getattr(profile, "auth_type", "static_bearer") == "oauth"
                and services.oauth_profile_sessions is not None
            ):
                sessions = services.oauth_profile_sessions

                async def refresh_bearer() -> str:
                    return await sessions.refresh_access_token(channel.profile)

            async def open_bus(bearer_now: str) -> FederatedDataBusClient:
                bus = FederatedDataBusClient(
                    platform_url=platform_url,
                    credential=card_credential(bearer_now),
                    # The socket reconnects on its own after a transport drop,
                    # and each reconnect handshake presents the bearer valid at
                    # that moment, not the one captured here (relay_admission).
                    credential_source=reconnect_credential_source(
                        resolve_bearer=current_bearer,
                        refresh_bearer=refresh_bearer,
                        credential=card_credential,
                        profile=channel.profile,
                    ),
                    lifecycle_labels=channel_lifecycle_labels(
                        channel, replacement_epoch
                    ),
                )
                try:
                    await bus.connect()
                except BaseException:
                    await bus.close()
                    raise
                return bus

            started = time.monotonic()
            try:
                data_bus, admission = await open_with_one_refresh(
                    open_bus=open_bus,
                    bearer=bearer,
                    refresh_bearer=refresh_bearer,
                    profile=channel.profile,
                    target=platform_url,
                )
            except Exception as exc:
                failure = staged_failure(
                    exc, operation="data_bus.connect", target=platform_url,
                    elapsed_seconds=time.monotonic() - started,
                    retryable=retryable(exc),
                )
                if failure is exc:
                    raise
                raise failure from exc
            if admission.get("refreshed"):
                logging.getLogger(__name__).info(
                    "Data Bus admitted profile=%s after one Card session refresh (refusal code=%s)",
                    channel.profile,
                    (admission.get("first_refusal") or {}).get("code") or "unnamed",
                )
            try:
                yield ProblemBoardDataBusClient(
                    data_bus,
                    partition_ref=worker_stream_partition(
                        f"card:{profile.access_id}"
                    ),
                )
            finally:
                await data_bus.close()

        from connection_hub_cli.errors import UpstreamError

        try:
            from app_foundation.mcp.client import RemoteMcpConnectionError
        except Exception:  # pragma: no cover - optional transport dependency
            RemoteMcpConnectionError = ()  # type: ignore[assignment]
        try:
            from mcp.shared.exceptions import MCPError
        except Exception:  # pragma: no cover - optional transport dependency
            MCPError = ()  # type: ignore[assignment]

        def retryable(error: BaseException) -> bool:
            # The governed endpoint answering 429 or refusing a connection is a
            # transport condition of this cycle, not a configuration fault.
            if isinstance(error, (UpstreamError, RemoteMcpConnectionError, MCPError)):
                return True
            return transient_failure(error)

        adapter = ProblemBoardRelaySupervisor(
            config_path=config_path,
            connector=connector,
            retryable=retryable,
        )
        # The effective descriptor ceiling, once per start. A relay begun
        # before the service definition carried a limit runs under the
        # session default until it is reinstalled, and this line is how a
        # log reader knows which.
        # And where the code came from: one exported commit, or the checkout
        # with its head and dirty flag as evidence. The same facts go into a
        # startup record the activation command waits for, so "activated"
        # is said by the process that started and not by the one that asked
        # for it.
        source = describe_source(Path(__file__), scope_paths=CLIENT_SOURCE_PATHS)
        logging.getLogger(__name__).info(
            "Problem Board relay starting pid=%s file_descriptor_limit=%s %s",
            os.getpid(),
            descriptor_limit_label(),
            source_line(source),
        )
        if not args.once:
            # A foreground `pb relay --once` is a check, not the service. Its
            # record would stand in for the service's and mislead an
            # activation waiting on it.
            write_startup_record(
                config_path.parent / "logs" / STARTUP_RECORD_NAME,
                pid=os.getpid(),
                source=source,
                file_descriptor_limit=descriptor_limit_label(),
                config=str(config_path),
            )
        runtime = HostRelayRuntime(
            adapter=adapter,
            policy=HostRelayPolicy(poll_interval_seconds=host.reconcile_ceiling_seconds),
        )
        try:
            if args.once:
                return await runtime.run_once()
            await runtime.run()
        finally:
            await adapter.aclose()
        return {"stopped": True}

    config = RelayConfig.load(config_path)
    field = SharedFieldStore(config.field_root)
    paths = (
        StatePaths(config.connection_hub_state_root)
        if config.connection_hub_state_root is not None
        else StatePaths.default()
    )
    services = build_services(paths=paths)
    profile = services.profiles.require(config.connection_hub_profile)
    governed_endpoint_identity(str(getattr(profile, "endpoint", "") or ""))
    async with connect_profile_tools(
        profile_name=config.connection_hub_profile,
        profiles=services.profiles,
        credentials=services.credentials,
        oauth_sessions=services.oauth_profile_sessions,
    ) as (remote, _client):
        adapter = ProblemBoardHostRelayAdapter(
            config=config,
            field=field,
            client=ProblemBoardMcpClient(remote),
        )
        runtime = HostRelayRuntime(
            adapter=adapter,
            policy=HostRelayPolicy(
                poll_interval_seconds=config.reconcile_ceiling_seconds,
            ),
        )
        if args.once:
            return await runtime.run_once()
        await runtime.run()
    return {"stopped": True}


def _render_command(args: argparse.Namespace) -> int:
    """`pb render`: the shipped reader for saved pb output.

    Exit 0 for an ok envelope, 1 for an error envelope, 2 for text that is not
    an envelope. Every case prints something, so a reader that produced nothing
    cannot be mistaken for an empty result.
    """
    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    else:
        text = sys.stdin.read()
    flags: list[str] = []
    if args.runtime_kind:
        flags.extend(["--runtime-kind", args.runtime_kind])
    if args.runtime_session_id:
        flags.extend(["--runtime-session-id", args.runtime_session_id])
    rendered, code = render_text(text, worker_flags=flags)
    print(rendered, end="")
    return code


def _limit_state_command(args: argparse.Namespace, *, stdin: Any = None) -> int:
    """Record the runtime's own limit state and print one status line (W26).

    Claude Code shows this command's stdout as the status line, so the output
    is one short line and never an envelope, and a failure to record never
    breaks the status line: it is said on stderr and the exit code stays 0.
    """

    raw = ""
    try:
        source = sys.stdin if stdin is None else stdin
        raw = source.read() if str(args.payload_file or "-") == "-" else Path(args.payload_file).read_text(encoding="utf-8")
        payload = json.loads(raw) if raw.strip() else {}
    except (OSError, ValueError) as exc:
        print(f"limit state not recorded: payload unreadable ({exc})", file=sys.stderr)
        print("limit unknown")
        return 0
    observed_at = utc_now()
    if args.source == "stop-failure":
        state = limit_state_from_claude_stop_failure(payload, observed_at=observed_at)
    else:
        state = limit_state_from_claude_statusline(payload, observed_at=observed_at)
    try:
        identity = _limit_state_identity(args, payload)
        if identity is not None:
            config = HostRelayConfig.load(resolve_host_config_path(getattr(args, "config", None)))
            field = SharedFieldStore(config.field_root)
            try:
                field.read_worker(identity.worker_name)
            except DomainError as exc:
                if exc.code != "field_record_not_found":
                    raise
                # The settings are user-level, so every Claude Code session on
                # the host runs this. A session that is not a worker gets its
                # status line and nothing is recorded or said.
                identity = None
            if identity is not None:
                field.record_runtime_limit_state(identity.worker_name, state)
    except DomainError as exc:
        print(f"limit state not recorded: {exc.code}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - the status line must never break on this
        print(f"limit state not recorded: {exc}", file=sys.stderr)
    print(limit_state_line(state))
    return 0


def _stop_guard_command(args: argparse.Namespace, *, stdin: Any = None) -> int:
    """Claude Code's Stop hook: block a worker's stop once when its watch is not running (W182).

    The settings are user-level, so every Claude Code session on the host runs
    this. Anything that is not an attending worker's session, and any failure
    here, lets the stop through: the exit code is always 0, and only a block
    prints anything on stdout.
    """

    try:
        source = sys.stdin if stdin is None else stdin
        raw = source.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, Mapping) or payload.get("stop_hook_active"):
            return 0
        session = str(payload.get("session_id") or "").strip()
        if not session:
            return 0
        identity = WorkerSessionIdentity.create("claude-code", session)
        config = HostRelayConfig.load(resolve_host_config_path(getattr(args, "config", None)))
        decision = stop_guard_decision(
            payload, field=SharedFieldStore(config.field_root), worker_name=identity.worker_name
        )
    except Exception as exc:  # noqa: BLE001 - a guard must never trap the session it guards
        print(f"stop guard let the stop through: {exc}", file=sys.stderr)
        return 0
    if decision is not None:
        print(json.dumps(decision, ensure_ascii=True))
    return 0


def _limit_state_identity(
    args: argparse.Namespace, payload: Mapping[str, Any]
) -> WorkerSessionIdentity | None:
    """Which session the limit state belongs to.

    The settings line runs a bare ``pb worker limit-state``, so the identity
    comes from the JSON Claude Code passes (``session_id``, runtime
    ``claude-code``) unless the flags name one. No session id anywhere is not
    an error: the status line still prints, nothing is recorded.
    """

    kind = str(getattr(args, "runtime_kind", "") or "").strip()
    session = str(getattr(args, "runtime_session_id", "") or "").strip()
    if kind or session:
        return _identity(args)
    payload_session = str(payload.get("session_id") or "").strip() if isinstance(payload, Mapping) else ""
    if not payload_session:
        return None
    return WorkerSessionIdentity.create("claude-code", payload_session)


def _channel_profile(args: argparse.Namespace) -> str:
    """This session's worker channel profile, or empty when no channel is known.

    Read only to name the fix in a refusal, so any failure here is nothing: the
    refusal still prints, with a placeholder where the profile would be.
    """

    try:
        identity = _identity(args)
        config = HostRelayConfig.load(resolve_host_config_path(getattr(args, "config", None)))
        channel = config.worker(identity)
    except Exception:  # noqa: BLE001 - a missing channel never hides the refusal
        return ""
    return str(getattr(channel, "profile", "") or "")


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        cleaned_argv, output_format = select_format(raw_argv, os.environ)
    except ValueError as exc:
        print(
            json.dumps({"ok": False, "error": {"code": "problem_board_format_invalid", "message": str(exc)}}, sort_keys=True),
            file=sys.stderr,
        )
        return 1
    args = build_parser().parse_args(cleaned_argv)
    if args.command == "render":
        return _render_command(args)
    if args.command == "worker" and getattr(args, "worker_command", "") == "limit-state":
        # One plain line, because Claude Code shows it as the status line.
        return _limit_state_command(args)
    if args.command == "worker" and getattr(args, "worker_command", "") == "stop-guard":
        # Claude Code reads the hook's decision as JSON on stdout.
        return _stop_guard_command(args)
    worker_flags = worker_flags_from_argv(cleaned_argv)
    try:
        # Inline prose is one line or a file, before any command runs, so the
        # sender learns at the moment of typing and not from a reader.
        guard_inline_prose(args)
        if args.command == "relay":
            result = asyncio.run(_relay(args))
        elif args.command == "setup":
            result = _setup(args)
        elif args.command == "status":
            result = _status_command(args)
        elif args.command == "host":
            result = _host_command(args)
        elif args.command == "worker":
            result = _worker_command(args)
        elif args.command == "coordinate":
            result = _coordinate_command(args)
        elif args.command == "plan":
            result = _plan_command(args)
        elif args.command == "references":
            result = _references_command(args)
        elif args.command == "procedure":
            result = _procedure_command(args)
        elif args.command == "relay-service":
            result = _relay_service_command(args)
        elif args.command == "source":
            result = _source_command(args)
        else:
            result = execute(args)
    except (DomainError, ValueError, OSError) as exc:
        payload = exc.to_dict() if isinstance(exc, DomainError) else {
            "code": "problem_board_command_failed",
            "message": str(exc),
        }
        # A Card refusal names the operation, its permission group and the
        # replace-card command with this session's profile (W262): the reader
        # of this output is the one who has to act on it.
        payload = with_actionable_refusal(payload, profile=_channel_profile(args))
        envelope = {"ok": False, "error": payload}
        if output_format == FORMAT_BRIEF:
            # Brief mode puts the error on stdout: a reader of stdout must see it.
            print(render_envelope(envelope, worker_flags=worker_flags), end="")
        else:
            print(json.dumps(envelope, sort_keys=True), file=sys.stderr)
        return 1
    envelope = {"ok": True, "result": result}
    if output_format == FORMAT_BRIEF:
        print(render_envelope(envelope, worker_flags=worker_flags), end="")
    else:
        print(json.dumps(envelope, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "main"]

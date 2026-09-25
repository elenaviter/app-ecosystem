from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import time
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence

try:
    from service_foundation.host_relay import HostRelayRetryableError
except ImportError:  # pragma: no cover - the relay runtime is a host-side dependency

    class HostRelayRetryableError(RuntimeError):  # type: ignore[no-redef]
        """Fallback so this module imports where no relay runtime is installed."""

        def __init__(self, code: str, message: str) -> None:
            super().__init__(message)
            self.code = str(code or "host_relay_retryable_error")
            self.message = str(message or "The host relay cycle can be retried.")


from .card_refusal import actionable_card_refusal
from .limit_state import session_with_limit_state, wake_deferred_until
from .runtime_model import session_with_runtime_model
from .worktree_files import (
    MAX_OBSERVED_PATHS as MAX_OBSERVED_PATHS_DEFAULT,
    WorktreeObserverCache,
    observations_signature,
    observe_assignments,
)
from ..contract.errors import DomainError
from ..contract.delivery_failures import resolve_delivery_failure_target
from ..contract.plan_nodes import parse_plan_node_ref
from ..contract.refs import parse_ref
from ..contract.worker_identity import WorkerSessionIdentity, normalize_worker_alias
from ..contract.runtime_account import normalize_runtime_account
from .io import content_hash, new_id, parse_utc, read_json, utc_now
from .journals import JournalWorkspace, RepositoryMap
from .mail_attachments import normalize_attachment_manifest
from .plan_authority import PLAN_REF_RESOLUTION_SCHEMA
from ..contract.plan_host import NOTE_VIEW_KIND, PLAN_HOST_CONTROL_KINDS
from .host_config import HostRelayConfig, WorkerChannelConfig, set_worker_channel_state
from .authorization import PROFILE_METADATA_ABSENT, authorization_observation
from .runtime_account import read_runtime_account
from .coordinate_queue import COORDINATE_LEASE_LOST, CoordinateQueue
from .credential_refusal import credential_refused
from .relay_pacing import HANDSHAKE_TIMEOUT_REASON, PACING_FILENAME, RelayPacing
from .relay_trace import RelayActivityTrace
from .relay_admission import is_namespace_handshake_timeout, is_runtime_unavailable
from .session_delivery import (
    notify_agent_session,
    reconcile_agent_session_queue,
)
from .resume_command import build_session_resume_command
from .relay_faults import consume_relay_fault, pending_relay_faults
from .relay_failures import (
    RelayStageError,
    failure_message,
    failure_type,
    is_descriptor_exhaustion,
    staged_failure,
)
from .local_state_maintenance import run_local_state_maintenance
from .local_store import last_read_summaries
from .outbox_drain import RelayOutboxDrainServer
from .outbox_store import OutboxStore
from .store import WAKE_OVERDUE_GRACE_SECONDS, SharedFieldStore, _seconds_since


logger = logging.getLogger(__name__)


CONFIG_SCHEMA = "problem-board.host-relay-config.v1"
DEFAULT_CONTROL_KINDS = (
    "assign",
    "journal.catalog",
    "journal.read",
    "materialize",
    "ping",
    "project.report",
    "replan",
    "reply",
    "request",
    "resume",
    "stop",
)
# A project's journal ref can name a repository alias this machine never
# mapped. That is LOCAL host policy, decided by the operator, so it must not
# read as a channel failure: raised out of the project cycle it starved every
# control addressed to the worker (a ping waited on a journal alias) and made
# the supervisor drop the Data Bus session each cycle, so the board saw the
# relay flap between connected and stale. The cycle reports the gap and goes
# on to pull controls; journal views refuse with the same code.
LOCAL_JOURNAL_MAPPING_CODES = frozenset(
    {
        "journal_repository_unmapped",
        "journal_repository_root_missing",
        "journal_home_missing",
        "journal_repository_ref_escape",
        "journal_repository_root_invalid",
        "journal_repository_alias_invalid",
        "journal_workspace_link_conflict",
    }
)
# Attendance adapters are rebuilt every cycle, so the once-per-gap log line
# is remembered at module scope, keyed by relay, project, and code.
_reported_journal_gaps: set[tuple[str, str, str]] = set()
# W304 D13: one event when a project's journal becomes unavailable on this
# machine, one when it is back. The incident lives in a durable record in the
# field store, so a restart never repeats or loses either event.
JOURNAL_NOTICE_KIND = "worker.journal"

# The remote worker-session row is a presence projection, not a replica of the
# host's delivery ledger. Twenty control refs are enough to reconcile recently
# read controls; wake histories remain in the local shared field and never ride
# every heartbeat through the Data Bus stream.
HEARTBEAT_CONTROL_REF_LIMIT = 20
HEARTBEAT_SESSION_FIELDS = (
    "session_id",
    "state",
    "presence",
    "check_interval_seconds",
    "attached_at",
    "last_inbox_check_at",
    "heartbeat_at",
    "detached_at",
    "last_inbox_result_at",
    "last_mail_settled_at",
    "last_settled_message_ref",
    # W26: the runtime's own usage-limit state, read on this host, so the
    # board says "out of tokens, resets at" instead of reading silence.
    "limit_state",
    # W327: the model and reasoning effort the runtime says it runs with.
    "runtime_model",
)
HEARTBEAT_SUBSCRIPTION_FIELDS = (
    "adapter",
    "state",
    "last_event_kind",
    "last_attempt_at",
    "last_delivered_at",
    "last_error",
    "revision",
)


def _heartbeat_session_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    """Project local session state onto the remote presence contract."""

    projected = {
        field: value[field]
        for field in HEARTBEAT_SESSION_FIELDS
        if field in value
    }
    projected["last_control_refs"] = list(
        value.get("last_control_refs") or []
    )[-HEARTBEAT_CONTROL_REF_LIMIT:]
    subscription = (
        value.get("subscription")
        if isinstance(value.get("subscription"), Mapping)
        else {}
    )
    projected["subscription"] = {
        field: subscription[field]
        for field in HEARTBEAT_SUBSCRIPTION_FIELDS
        if field in subscription
    }
    return projected


async def _http_upload(url: str, data: bytes, mime: str) -> None:
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=data, headers={"Content-Type": mime}) as response:
            if response.status >= 300:
                raise DomainError(
                    "work_attachment_upload_failed",
                    f"Attachment upload answered {response.status}.",
                    status=502,
                )


async def _http_download(url: str) -> bytes:
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status >= 300:
                raise DomainError(
                    "work_attachment_download_failed",
                    f"Attachment download answered {response.status}.",
                    status=502,
                )
            return await response.read()


def _deadline_passed(value: Any) -> bool:
    """A missing or unreadable wake deadline counts as passed."""

    deadline = str(value or "")
    if not deadline:
        return True
    try:
        return parse_utc(deadline) <= datetime.now(timezone.utc)
    except DomainError:
        return True


class ControlClient(Protocol):
    async def action(
        self,
        *,
        object_ref: str,
        action: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    async def action_with_transport_identity(
        self,
        *,
        object_ref: str,
        action: str,
        payload: Mapping[str, Any] | None = None,
        transport_request_id: str,
    ) -> dict[str, Any]: ...


class ChannelConnector(Protocol):
    def __call__(
        self,
        host: HostRelayConfig,
        channel: WorkerChannelConfig,
        *,
        replacement_epoch: int,
    ) -> AbstractAsyncContextManager[ControlClient]: ...


def channel_lifecycle_labels(
    channel: WorkerChannelConfig, replacement_epoch: int
) -> dict[str, str | int]:
    """Stable relay coordinates for one replaceable Data Bus client."""

    return {
        "worker_name": channel.worker_name,
        "channel_identity": channel.worker_identity,
        "replacement_epoch": replacement_epoch,
    }


def _required(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise DomainError("work_relay_config_invalid", f"Relay config requires {field}.")
    return text


def _object_result(response: Mapping[str, Any]) -> dict[str, Any]:
    value = response.get("object")
    if isinstance(value, Mapping):
        return dict(value)
    output = response.get("output")
    if isinstance(output, Mapping) and isinstance(output.get("object"), Mapping):
        return dict(output["object"])
    return dict(response)


def _coordinate_queue_window(
    request: Mapping[str, Any],
) -> tuple[float, float] | None:
    last_error = request.get("last_transport_error")
    last_error = last_error if isinstance(last_error, Mapping) else {}
    queued_at = str(last_error.get("observed_at") or request.get("created_at") or "")
    claimed_at = str(request.get("leased_at") or request.get("first_claimed_at") or "")
    try:
        return parse_utc(queued_at).timestamp(), parse_utc(claimed_at).timestamp()
    except DomainError:
        return None


def _coordinate_queue_wait_seconds(request: Mapping[str, Any]) -> float | None:
    window = _coordinate_queue_window(request)
    return None if window is None else max(0.0, window[1] - window[0])


def _secret_fields(value: Any, path: tuple[str, ...] = ()) -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).lower()
            current = (*path, str(key))
            if normalized in {"bearer", "token", "credential", "secret"} or normalized.endswith(
                ("_token", "_credential", "_secret")
            ):
                found.append(".".join(current))
            found.extend(_secret_fields(child, current))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            found.extend(_secret_fields(child, (*path, str(index))))
    return found


def _declared_ceiling(value: Mapping[str, Any]) -> int:
    """The reconciliation ceiling, reading the pre-rename key as well.

    A relay.json written before the rename carries poll_interval_seconds. Not
    reading it would drop a configured ceiling back to the default without
    saying so, which is the same invisible slowdown the rename exists to stop.
    """

    for key in ("reconcile_ceiling_seconds", "poll_interval_seconds"):
        declared = value.get(key)
        if declared:
            return max(5, min(int(declared), 86_400))
    return 60


def _declared_idle_ceiling(value: Mapping[str, Any]) -> int:
    active = _declared_ceiling(value)
    for key in ("idle_reconcile_ceiling_seconds", "idle_poll_interval_seconds"):
        declared = value.get(key)
        if declared:
            return max(active, min(int(declared), 86_400))
    return max(active, 120)


@dataclass(frozen=True)
class RelayConfig:
    field_root: Path
    project_id: str
    connection_hub_profile: str
    connection_hub_state_root: Path | None
    worker_name: str
    worker_alias: str
    worker_identity: str
    runtime_kind: str
    runtime_session_id: str
    working_directory: str
    capabilities: tuple[str, ...]
    host_id: str
    host_label: str
    host_kind: str
    relay_id: str
    # Named for a polling design this relay does not have. The relay waits on
    # Data Bus push, see wait_for_wakeup, and this is the CEILING on that wait:
    # the longest it will go without a reconciliation cycle when no push
    # arrives. It is a safety net, not a schedule, and removing it would let a
    # single missed push leave the relay asleep forever.
    #
    # The name has already cost real time. It was read as proof that the board
    # polls, which produced a confident and wrong explanation of why opening a
    # plan node is slow. The real cause is losing the push transport, after
    # which every wake takes the full ceiling. Renaming these two fields is
    # tracked separately because they appear in 64 places plus the config, CLI
    # flags and the procedure.
    reconcile_ceiling_seconds: int
    idle_reconcile_ceiling_seconds: int
    journal_workspace_root: Path | None
    source_repositories: tuple[tuple[str, str], ...]
    allowed_roots: tuple[str, ...]
    create_missing_journal_home: bool
    allowed_control_kinds: tuple[str, ...]
    allowed_peer_workers: tuple[str, ...]
    max_control_bytes: int
    allow_session_resume_view: bool
    # Remote URL per repository alias, published with the worker so the board
    # can render links for portable repo: refs. Paths never cross the network.
    source_repository_urls: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RelayConfig":
        if value.get("schema") != CONFIG_SCHEMA:
            raise DomainError("work_relay_config_invalid", "The relay config schema is unsupported.")
        worker = value.get("worker") if isinstance(value.get("worker"), Mapping) else {}
        host = worker.get("host") if isinstance(worker.get("host"), Mapping) else {}
        host_kind = str(host.get("kind") or "local").strip().lower()
        if host_kind not in {"local", "hosted", "remote"}:
            raise DomainError(
                "work_relay_config_invalid", "worker.host.kind must be local, hosted, or remote."
            )
        capabilities: Sequence[Any] = (
            worker.get("capabilities")
            if isinstance(worker.get("capabilities"), (list, tuple))
            else []
        )
        journal = (
            value.get("journal_workspace")
            if isinstance(value.get("journal_workspace"), Mapping)
            else {}
        )
        raw_repositories = (
            journal.get("source_repositories")
            if isinstance(journal.get("source_repositories"), Mapping)
            else {}
        )
        raw_repository_urls = (
            journal.get("source_repository_urls")
            if isinstance(journal.get("source_repository_urls"), Mapping)
            else {}
        )
        journal_root = str(journal.get("root") or "").strip()
        if journal and (not journal_root or not raw_repositories):
            raise DomainError(
                "work_relay_config_invalid",
                "journal_workspace requires root and source_repositories.",
            )
        journal_root_path = Path(journal_root).expanduser() if journal_root else None
        if journal_root_path is not None and not journal_root_path.is_absolute():
            raise DomainError(
                "work_relay_config_invalid",
                "journal_workspace.root must be an absolute LOCAL path.",
            )
        connection_hub = (
            value.get("connection_hub")
            if isinstance(value.get("connection_hub"), Mapping)
            else {}
        )
        connection_hub_state_root = str(connection_hub.get("state_root") or "").strip()
        connection_hub_state_path = (
            Path(connection_hub_state_root).expanduser()
            if connection_hub_state_root
            else None
        )
        if connection_hub_state_path is not None and not connection_hub_state_path.is_absolute():
            raise DomainError(
                "work_relay_config_invalid",
                "connection_hub.state_root must be an absolute LOCAL path.",
            )
        receiver_policy = (
            value.get("receiver_policy")
            if isinstance(value.get("receiver_policy"), Mapping)
            else {}
        )
        raw_kinds = receiver_policy.get("allowed_control_kinds")
        if raw_kinds is None:
            raw_kinds = DEFAULT_CONTROL_KINDS
        if isinstance(raw_kinds, (str, bytes, bytearray)) or not isinstance(
            raw_kinds, Sequence
        ):
            raise DomainError(
                "work_relay_config_invalid",
                "receiver_policy.allowed_control_kinds must be an array.",
            )
        raw_peers = receiver_policy.get("allowed_peer_workers") or []
        if isinstance(raw_peers, (str, bytes, bytearray)) or not isinstance(
            raw_peers, Sequence
        ):
            raise DomainError(
                "work_relay_config_invalid",
                "receiver_policy.allowed_peer_workers must be an array.",
            )
        try:
            max_control_bytes = int(
                receiver_policy.get("max_control_bytes") or 64 * 1024
            )
        except (TypeError, ValueError) as exc:
            raise DomainError(
                "work_relay_config_invalid",
                "receiver_policy.max_control_bytes must be an integer.",
            ) from exc
        return cls(
            field_root=Path(_required(value.get("field_root"), "field_root")).expanduser().resolve(),
            project_id=_required(value.get("project_id"), "project_id"),
            connection_hub_profile=_required(
                connection_hub.get("profile"), "connection_hub.profile"
            ),
            connection_hub_state_root=(
                connection_hub_state_path.resolve() if connection_hub_state_path else None
            ),
            worker_name=_required(worker.get("name"), "worker.name").lower(),
            worker_alias=normalize_worker_alias(worker.get("alias")),
            worker_identity=str(worker.get("identity") or "").strip(),
            runtime_kind=_required(worker.get("runtime_kind"), "worker.runtime_kind"),
            runtime_session_id=str(worker.get("runtime_session_id") or "").strip(),
            working_directory=str(worker.get("working_directory") or "").strip(),
            capabilities=tuple(sorted({str(item).strip() for item in capabilities if str(item).strip()})),
            host_id=_required(host.get("id"), "worker.host.id"),
            host_label=_required(host.get("label"), "worker.host.label"),
            host_kind=host_kind,
            relay_id=_required(worker.get("relay_id"), "worker.relay_id"),
            reconcile_ceiling_seconds=_declared_ceiling(value),
            idle_reconcile_ceiling_seconds=_declared_idle_ceiling(value),
            journal_workspace_root=(
                journal_root_path.resolve() if journal_root_path else None
            ),
            source_repositories=tuple(
                sorted((str(alias), str(root)) for alias, root in raw_repositories.items())
            ),
            allowed_roots=tuple(
                sorted(
                    str(Path(root).expanduser().resolve())
                    for root in (value.get("allowed_roots") or [])
                    if str(root or "").strip()
                )
            ),
            create_missing_journal_home=bool(journal.get("create_missing_home", False)),
            allowed_control_kinds=tuple(
                sorted({str(item).strip() for item in raw_kinds if str(item).strip()})
            ),
            allowed_peer_workers=tuple(
                sorted({str(item).strip().lower() for item in raw_peers if str(item).strip()})
            ),
            max_control_bytes=max(1024, min(max_control_bytes, 512 * 1024)),
            allow_session_resume_view=bool(
                receiver_policy.get("allow_session_resume_view", True)
            ),
            source_repository_urls=tuple(
                sorted((str(alias), str(url)) for alias, url in raw_repository_urls.items() if str(url).strip())
            ),
        )

    @classmethod
    def from_host_channel(
        cls,
        host: HostRelayConfig,
        channel: WorkerChannelConfig,
        *,
        project_id: str,
    ) -> "RelayConfig":
        return cls(
            field_root=host.field_root,
            project_id=project_id,
            connection_hub_profile=channel.profile,
            connection_hub_state_root=host.connection_hub_state_root,
            worker_name=channel.worker_name,
            worker_alias=channel.worker_alias,
            worker_identity=channel.worker_identity,
            runtime_kind=channel.runtime_kind,
            runtime_session_id=channel.runtime_session_id,
            working_directory=channel.working_directory,
            capabilities=channel.capabilities,
            host_id=host.host_id,
            host_label=host.host_label,
            host_kind=host.host_kind,
            relay_id=f"{host.relay_id}-{channel.worker_name}",
            reconcile_ceiling_seconds=host.reconcile_ceiling_seconds,
            idle_reconcile_ceiling_seconds=host.idle_reconcile_ceiling_seconds,
            journal_workspace_root=host.journal_workspace_root,
            source_repositories=host.source_repositories,
            allowed_roots=host.allowed_roots,
            create_missing_journal_home=host.create_missing_journal_home,
            allowed_control_kinds=host.allowed_control_kinds,
            allowed_peer_workers=host.allowed_peer_workers,
            max_control_bytes=host.max_control_bytes,
            allow_session_resume_view=host.allow_session_resume_view,
            source_repository_urls=getattr(host, "source_repository_urls", ()),
        )

    @classmethod
    def load(cls, path: str | Path) -> "RelayConfig":
        config_path = Path(path).expanduser().resolve()
        try:
            value = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DomainError(
                "work_relay_config_unreadable", "The relay config could not be read."
            ) from exc
        if not isinstance(value, Mapping):
            raise DomainError("work_relay_config_invalid", "The relay config must be a JSON object.")
        forbidden = _secret_fields(value)
        if forbidden:
            raise DomainError(
                "work_relay_secret_in_config",
                "Relay config identifies a Connection Hub profile and never stores credentials.",
                details={"fields": sorted(forbidden)},
            )
        return cls.from_mapping(value)

    def journal_workspace(self) -> JournalWorkspace | None:
        if self.journal_workspace_root is None:
            return None
        return JournalWorkspace(
            self.journal_workspace_root,
            # A checkout that is not on this machine is reported per project
            # when a journal needs it, and never keeps a channel closed (W304 D13).
            RepositoryMap.from_mapping(dict(self.source_repositories), require_existing=False),
        )


class ProblemBoardHostRelayAdapter:
    adapter_id = "problem-board"

    def __init__(
        self,
        *,
        config: RelayConfig,
        field: SharedFieldStore,
        client: ControlClient,
        uploader: Callable[..., Any] | None = None,
        downloader: Callable[..., Any] | None = None,
        session_report_signatures: dict[str, str] | None = None,
        attendance_cache: dict[str, Any] | None = None,
        heartbeat_sent_at: dict[str, float] | None = None,
        monotonic: Callable[[], float] | None = None,
        trace: RelayActivityTrace | None = None,
        runtime_account_reader: Callable[[], Awaitable[Mapping[str, Any]]] | None = None,
        runtime_account_error_state: dict[str, str] | None = None,
        outbox_drain_lock: asyncio.Lock | None = None,
    ) -> None:
        self.config = config
        self.field = field
        self.client = client
        # Files cross the network by signed HTTP links, never inside a
        # control: outbound through an upload slot the control plane mints,
        # inbound by fetching the operator's signed download link. Both are
        # injectable so tests never open a socket.
        self.uploader = uploader or _http_upload
        self.downloader = downloader or _http_download
        self.journal_workspace = config.journal_workspace()
        # Publishing registers the worker once per connection. Afterwards the
        # heartbeat alone proves relay presence; the control plane answers
        # ``work_worker_unavailable`` when the registration is gone or in limbo,
        # and only then is the worker published again. This keeps one governed
        # call per idle cycle instead of two.
        self._published_interval: int | None = None
        # W278 part B: per project, the signature of the files in flight last
        # published, so an unchanged set rides no heartbeat.
        self._assignment_files_signatures: dict[str, str] = {}
        self._store_reads_signatures: dict[str, str] = {}
        self._worktree_observer = WorktreeObserverCache()
        # Child adapters are rebuilt for attended projects every cycle. Share
        # this map with them so each discovery/project scope sends a full
        # session projection once, then omits it until that projection changes.
        self._session_report_signatures = (
            session_report_signatures
            if session_report_signatures is not None
            else {}
        )
        # A discovery heartbeat is needed to bootstrap attendance. Every
        # project heartbeat returns the same authoritative attendance snapshot,
        # so a linked worker can reuse it instead of sending a second heartbeat
        # on every cycle. Child project adapters share this cache.
        self._attendance_cache = (
            attendance_cache
            if attendance_cache is not None
            else {
                "initialized": False,
                "items": [],
                "attendance_revision": 0,
                "attendance_revision_observed": False,
                "worker_alias": "",
                "host_retirements": [],
            }
        )
        # A Data Bus push starts a reconciliation cycle immediately. It does
        # not make every linked project's presence heartbeat due. Child
        # adapters are rebuilt each cycle, so they share this monotonic clock
        # state with the persistent channel adapter.
        self._heartbeat_sent_at = (
            heartbeat_sent_at if heartbeat_sent_at is not None else {}
        )
        self._monotonic = monotonic or time.monotonic
        self._trace = trace or RelayActivityTrace(log=logger)
        self._runtime_account_reader = runtime_account_reader
        self._runtime_account_error_state = (
            runtime_account_error_state
            if runtime_account_error_state is not None
            else {"code": ""}
        )
        # Child attendance adapters and the beside-cycle drain share this
        # lock, so one worker never sends two outbox rows concurrently through
        # the same Card channel.
        self._outbox_drain_lock = outbox_drain_lock
        # The LOCAL journal mapping gap this cycle found, if any, so a journal
        # view in the same cycle refuses with the cause instead of "unbound".
        self._journal_mapping_gap: dict[str, Any] | None = None

    async def _runtime_account(self) -> dict[str, str]:
        """Read identification metadata for an actual publish or heartbeat."""

        if self._runtime_account_reader is None:
            return {}
        try:
            account = normalize_runtime_account(await self._runtime_account_reader())
        except DomainError as exc:
            if self._runtime_account_error_state.get("code") != exc.code:
                logger.warning(
                    "Problem Board could not read runtime account identity "
                    "worker_name=%s error_code=%s",
                    self.config.worker_name,
                    exc.code,
                )
                self._runtime_account_error_state["code"] = exc.code
            return {}
        self._runtime_account_error_state["code"] = ""
        return account

    async def _add_runtime_account(self, payload: dict[str, Any]) -> None:
        account = await self._runtime_account()
        if account:
            payload["runtime_account"] = account

    def _trace_stage(self, stage: str, *, operation: str):
        return self._trace.stage(
            stage,
            channel=self.config.worker_name,
            operation=operation,
        )

    def _session_report_delta(
        self,
        *,
        project_ref: str,
        sessions: Sequence[Mapping[str, Any]],
    ) -> tuple[list[dict[str, Any]] | None, str]:
        projected = [_heartbeat_session_projection(value) for value in sessions]
        signature = content_hash(projected)
        if self._session_report_signatures.get(project_ref) == signature:
            return None, signature
        return projected, signature

    def _store_reads_delta(self, *, project_ref: str) -> tuple[dict[str, Any] | None, str]:
        """The last read of each local store by this worker, when it changed (W287).

        What range of which store the relay last read, per agent, so the board
        can show it. It never forces a heartbeat: it rides on the next one.
        """

        summaries = last_read_summaries(self.config.worker_name)
        stable = {
            store: {key: value for key, value in summary.items() if key not in {"ms", "at"}}
            for store, summary in summaries.items()
        }
        signature = content_hash(stable)
        if not summaries or self._store_reads_signatures.get(project_ref) == signature:
            return None, signature
        return summaries, signature

    def _assignment_files_delta(
        self, *, project_ref: str, fresh: bool = False
    ) -> tuple[list[dict[str, Any]] | None, str]:
        """The tracked files in flight per active assignment and repository (W278 part B).

        Read from the worktrees this worker declared on this host (``pb worker
        workspace``), against the base commit the assignment binds for that
        repository. Published only when the set changed: a derived signal, not
        the handoff. No declaration means nothing here, and the board says so.
        """

        workspaces = self.field.workspaces(self.config.worker_name)
        observations = (
            observe_assignments(
                workspaces,
                self.field.list_assignments(self.config.project_id),
                worker_name=self.config.worker_name,
                observe=lambda path, *, base_commit="", limit=MAX_OBSERVED_PATHS_DEFAULT: self._worktree_observer(
                    path, base_commit=base_commit, limit=limit, fresh=fresh
                ),
            )
            if workspaces
            else []
        )
        signature = observations_signature(observations)
        if self._assignment_files_signatures.get(project_ref) == signature:
            return None, signature
        return observations, signature

    def _record_session_report(self, *, project_ref: str, signature: str) -> None:
        self._session_report_signatures[project_ref] = signature

    def _defer_until_attendance_read(self, command_ref: str) -> bool:
        """Whether a "not linked" refusal waits for a fresher attendance read (W304 join race).

        The local record is the only evidence at hand, and it can predate a
        link the board committed seconds ago (the relay re-stamps an unchanged
        cached snapshot, so its time says nothing about the link). The first
        refusal of a control forces a read from the board and defers; the
        refusal stands only once a read that completed after that first
        refusal still says "not linked". Both moments are this relay's own
        monotonic clock, so no two hosts' clocks are ever compared.
        """

        deferrals = self._attendance_cache.setdefault("not_linked_deferrals", {})
        first = deferrals.get(command_ref)
        if first is None:
            deferrals[command_ref] = self._monotonic()
            self._attendance_cache["initialized"] = False
            return True
        read_at = self._attendance_cache.get("board_read_monotonic")
        if read_at is not None and read_at > first:
            deferrals.pop(command_ref, None)
            return False
        self._attendance_cache["initialized"] = False
        return True

    def _materialize_after_board_read(self, item: Mapping[str, Any]) -> dict[str, Any] | None:
        """Deliver on the board's own attendance when it lists the control's project, else None."""

        project_ref = str(item.get("project_ref") or "")
        board_refs = [
            str(entry.get("project_ref") or "")
            for entry in self._attendance_cache.get("items") or []
            if isinstance(entry, Mapping) and entry.get("project_ref")
        ]
        if not project_ref or project_ref not in board_refs:
            return None
        try:
            self.field.sync_worker_attendances(self.config.worker_name, board_refs)
            return self.field.materialize_control(item)
        except DomainError:
            return None

    async def _read_attendance_for_deferred_control(self) -> str:
        """Read attendance immediately after a cached not-linked result.

        The control is already leased, so waiting for the next supervisor poll
        adds the idle interval to a just-linked worker's welcome. Reuse the
        discovery heartbeat in this cycle. A failed optimization leaves the
        control deferred and cannot abort the rest of its leased batch.
        """

        # This is a best-effort latency read; no failure may strand its batch.
        try:
            discovery, republished = await self._heartbeat_with_republish(
                {"availability": "available"}
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Problem Board immediate attendance read failed; controls remain "
                "deferred worker=%s error=%s",
                self.config.worker_name,
                str(getattr(exc, "code", "") or type(exc).__name__),
                exc_info=True,
            )
            return "unavailable"
        if (
            republished is not None
            and republished["remote"].get("pool_status") == "limbo"
        ):
            return "limbo"
        self._record_project_heartbeat("")
        self._record_attendance_observation(discovery)
        refreshed = (
            "attendances" in discovery
            and bool(self._attendance_cache.get("initialized"))
        )
        if not refreshed:
            logger.warning(
                "Problem Board immediate attendance read returned no current "
                "snapshot; controls remain deferred worker=%s",
                self.config.worker_name,
            )
        return "refreshed" if refreshed else "unavailable"

    def _record_attendance_observation(self, value: Mapping[str, Any]) -> None:
        own = value.get("self")
        if isinstance(own, Mapping):
            # The worker's own owner and account, for whoami and context
            # (W304 finding 47). A record the host cannot keep never fails
            # the heartbeat that carried it.
            try:
                self.field.record_worker_board_record(self.config.worker_name, own)
            except DomainError:
                logger.debug("Could not keep the board's own record for %s.", self.config.worker_name, exc_info=True)
        if "attendances" in value:
            snapshot_is_current = True
            if "attendance_revision" in value:
                try:
                    revision = max(
                        0, int(value.get("attendance_revision") or 0)
                    )
                except (TypeError, ValueError):
                    revision = 0
                observed_revision = int(
                    self._attendance_cache.get("attendance_revision") or 0
                )
                snapshot_is_current = (
                    not self._attendance_cache.get(
                        "attendance_revision_observed"
                    )
                    or revision >= observed_revision
                )
                if snapshot_is_current:
                    self._attendance_cache["attendance_revision"] = revision
                    self._attendance_cache[
                        "attendance_revision_observed"
                    ] = True
            elif self._attendance_cache.get("attendance_revision_observed"):
                snapshot_is_current = False
            if snapshot_is_current:
                # When the board last answered with the attendance, on this
                # relay's clock (W304 join race).
                self._attendance_cache["board_read_monotonic"] = self._monotonic()
                self._attendance_cache["initialized"] = True
                self._attendance_cache["items"] = [
                    dict(item)
                    for item in value.get("attendances") or []
                    if isinstance(item, Mapping) and item.get("project_ref")
                ]
        if value.get("worker_alias"):
            self._attendance_cache["worker_alias"] = str(value["worker_alias"])
        if "host_retirements" in value:
            self._attendance_cache["host_retirements"] = [
                dict(item)
                for item in value.get("host_retirements") or []
                if isinstance(item, Mapping)
            ]

    def _invalidate_cached_project(self, project_ref: str) -> None:
        self._attendance_cache["initialized"] = False
        self._attendance_cache["items"] = [
            dict(item)
            for item in self._attendance_cache.get("items") or []
            if isinstance(item, Mapping)
            and str(item.get("project_ref") or "") != project_ref
        ]
        self._session_report_signatures.pop(project_ref, None)
        self._heartbeat_sent_at.pop(project_ref, None)

    def _active_poll_interval(
        self, sessions: Sequence[Mapping[str, Any]]
    ) -> int:
        intervals = [
            max(5, int(session.get("check_interval_seconds") or 30))
            for session in sessions
        ]
        return min(
            [self.config.reconcile_ceiling_seconds, *intervals]
            if intervals
            else [self.config.reconcile_ceiling_seconds]
        )

    def _project_heartbeat_wait(
        self,
        *,
        project_ref: str,
        sessions: Sequence[Mapping[str, Any]],
        interval_seconds: int | None = None,
    ) -> float:
        last_sent = self._heartbeat_sent_at.get(project_ref)
        if last_sent is None:
            return 0.0
        elapsed = max(0.0, self._monotonic() - last_sent)
        interval = (
            max(1, int(interval_seconds))
            if interval_seconds is not None
            else self._active_poll_interval(sessions)
        )
        return max(0.0, float(interval) - elapsed)

    def _discovery_heartbeat_wait(
        self, sessions: Sequence[Mapping[str, Any]]
    ) -> float:
        interval = (
            self.config.idle_reconcile_ceiling_seconds
            if self._attendance_cache.get("initialized")
            and not self._attendance_cache.get("items")
            else self._active_poll_interval(sessions)
        )
        return self._project_heartbeat_wait(
            project_ref="",
            sessions=sessions,
            interval_seconds=interval,
        )

    def _record_project_heartbeat(self, project_ref: str) -> None:
        self._heartbeat_sent_at[project_ref] = self._monotonic()

    def request_attendance_refresh(
        self,
        *,
        kind: str,
        refs: Mapping[str, Any] | None = None,
    ) -> bool:
        """Invalidate for one attendance revision newer than the snapshot."""

        clean_kind = str(kind or "")
        clean_refs = dict(refs or {})
        if clean_kind not in {
            "project.linked",
            "project.unlinked",
            "worker.retired",
        }:
            return False

        try:
            hint_revision = max(
                0, int(clean_refs.get("attendance_revision") or 0)
            )
        except (TypeError, ValueError):
            hint_revision = 0
        observed_revision = int(
            self._attendance_cache.get("attendance_revision") or 0
        )
        if hint_revision:
            if hint_revision <= observed_revision:
                return False
            if not self._attendance_cache.get("initialized"):
                return False
            self._attendance_cache["initialized"] = False
            return True

        # Once this relay has observed the revisioned snapshot contract, a
        # legacy unversioned hint cannot be ordered and therefore cannot force
        # presence traffic. The idle deadline remains the compatibility path.
        if self._attendance_cache.get("attendance_revision_observed"):
            return False

        if clean_kind == "worker.retired":
            if self._attendance_cache.get("retirement_hint_seen"):
                return False
            self._attendance_cache["retirement_hint_seen"] = True
            if not self._attendance_cache.get("initialized"):
                return False
            self._attendance_cache["initialized"] = False
            return True

        if not self._attendance_cache.get("initialized"):
            return False

        project_ref = str(clean_refs.get("project_ref") or "")
        if not project_ref:
            return False
        attendance = next(
            (
                item
                for item in self._attendance_cache.get("items") or []
                if isinstance(item, Mapping)
                and str(item.get("project_ref") or "") == project_ref
            ),
            None,
        )
        if clean_kind == "project.linked":
            hinted_role = str(clean_refs.get("role") or "")
            if attendance is not None and (
                not hinted_role
                or str(attendance.get("role") or "") == hinted_role
            ):
                return False
        elif attendance is None:
            return False

        self._attendance_cache["initialized"] = False
        return True

    def _next_project_heartbeat_seconds(
        self,
        *,
        project_ref: str,
        sessions: Sequence[Mapping[str, Any]],
    ) -> int:
        remaining = self._project_heartbeat_wait(
            project_ref=project_ref,
            sessions=sessions,
        )
        return max(1, math.ceil(remaining))

    def _receiver_refusal(self, control: Mapping[str, Any]) -> str:
        kind = str(control.get("kind") or "")
        if kind == "session.resume":
            return (
                ""
                if self.config.allow_session_resume_view
                else "receiver_policy_session_resume_view_denied"
            )
        # A reply answers a message this worker sent to the operator. The
        # receiver policy gates new authority arriving at the host, not the
        # operator's answer to the worker's own question, so it is never
        # refused for its kind.
        if kind != "reply" and kind not in self.config.allowed_control_kinds:
            return "receiver_policy_control_kind_denied"
        payload = control.get("payload") if isinstance(control.get("payload"), Mapping) else {}
        payload_bytes = len(
            json.dumps(
                payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        )
        if payload_bytes > self.config.max_control_bytes:
            return "receiver_policy_control_too_large"
        if kind == "mail":
            sender = str(control.get("sender") or "").strip().lower()
            peers = set(self.config.allowed_peer_workers)
            if not sender or ("*" not in peers and sender not in peers):
                return "receiver_policy_peer_denied"
        return ""

    # What each receiver-policy refusal names, so the sender and the host
    # owner see the setting that decided it (W304 finding 38).
    RECEIVER_POLICY_SETTINGS = {
        "receiver_policy_peer_denied": "receiver_policy.allowed_peer_workers",
        "receiver_policy_control_kind_denied": "receiver_policy.allowed_control_kinds",
        "receiver_policy_control_too_large": "receiver_policy.max_control_bytes",
        "receiver_policy_session_resume_view_denied": "receiver_policy.allow_session_resume_view",
    }

    def _receiver_policy_failure(self, control: Mapping[str, Any], code: str) -> dict[str, Any]:
        setting = self.RECEIVER_POLICY_SETTINGS.get(code, "receiver_policy")
        sender = str(control.get("sender") or "")
        reason = (
            f"The receiving host accepts mail only from the peers in {setting}, and {sender or 'this sender'} is not one. "
            "The host owner changes it with `pb host configure --allow-peer-worker`."
            if code == "receiver_policy_peer_denied"
            else f"The receiving host's {setting} refused this {control.get('kind') or 'control'}."
        )
        payload = control.get("payload") if isinstance(control.get("payload"), Mapping) else {}
        mail = payload.get("mail") if isinstance(payload.get("mail"), Mapping) else {}
        return {
            "message_ref": str(control.get("ref") or control.get("command_ref") or ""),
            # The sender's own ref for the mail, so its delivery row is marked refused.
            "source_message_ref": str(mail.get("source_message_ref") or ""),
            "code": code,
            "field": setting,
            "field_source": "receiver_policy",
            "value": sender if code == "receiver_policy_peer_denied" else str(control.get("kind") or ""),
            "reason": reason,
        }

    async def _settle_leased_control(
        self,
        command_ref: str,
        *,
        action: str,
        payload: Mapping[str, Any],
        kind: str = "",
        sender: str = "",
    ) -> bool:
        """Settle one leased control, log a refusal with its reason, and never close the channel.

        W304 finding 38: a refusal logged nothing, and the board's receipt for
        an applied refusal (the control, state "refused") was read as a
        refused operation, which closed the worker channel. A board that
        predates the applied marker is recognized by that state. Any other
        failure is logged, and the lease expires so the control plane offers
        the control again: one control never stops the rest.
        """

        target_state = "refused" if action == "control.refuse" else "acknowledged"
        if action == "control.refuse":
            logger.warning(
                "Problem Board relay refused control control=%s kind=%s sender=%s worker=%s summary=%s",
                command_ref,
                kind or "unknown",
                sender or "unknown",
                self.config.worker_name,
                str(payload.get("result_summary") or ""),
            )
        try:
            # Literal actions, so the worker profile's coverage stays provable
            # from this file (applications test_bundle_contract).
            if action == "control.refuse":
                await self.client.action(object_ref=command_ref, action="control.refuse", payload=dict(payload))
            else:
                await self.client.action(object_ref=command_ref, action="control.acknowledge", payload=dict(payload))
            return True
        except DomainError as exc:
            details = dict(exc.details or {})
            if details.get("outcome_state") == target_state and details.get("applied") is not False:
                # The board applied it and returned the control, whose own
                # state is the settled one (a board before the applied marker).
                return True
            logger.warning(
                "Problem Board relay could not settle control control=%s action=%s kind=%s code=%s status=%s reason=%s; "
                "the lease expires and the control is offered again",
                command_ref,
                action,
                kind or "unknown",
                exc.code,
                exc.status,
                str(exc),
            )
            return False

    async def _refuse_malformed_control(
        self,
        *,
        command_ref: str,
        kind: str,
        lease_id: str,
        error: Exception,
    ) -> dict[str, Any]:
        if isinstance(error, DomainError):
            code = error.code
            reason = str(error)
            details = dict(error.details)
        else:
            code = "field_control_payload_invalid"
            reason = str(error) or type(error).__name__
            details = {"field": "payload", "value_type": type(error).__name__}
        target = resolve_delivery_failure_target(
            {"code": code, "details": details}
        )
        failure = {
            "message_ref": command_ref,
            "code": code,
            **target.as_payload(),
            "reason": reason,
        }
        logger.warning(
            "Problem Board rejected malformed control control=%s kind=%s "
            "code=%s field=%s; continuing with the batch",
            command_ref,
            kind,
            code,
            failure["field"] or "unreported",
        )
        await self._settle_leased_control(
            command_ref,
            action="control.refuse",
            kind=kind,
            payload={
                "lease_id": lease_id,
                "lease_owner": self.config.relay_id,
                "result_summary": f"Local materialization refused: {code}",
                "result": {"delivery_failure": failure},
            },
        )
        return failure

    def _journal_notice_event(self, *, key: str, summary: str, metadata: Mapping[str, Any]) -> bool:
        try:
            self.field.enqueue_service_event(
                str(self.config.project_id),
                worker_name=self.config.worker_name,
                kind=JOURNAL_NOTICE_KIND,
                summary=summary,
                source_event_ref=f"relay:{key}",
                metadata=dict(metadata),
                idempotency_key=key,
            )
            return True
        except DomainError:
            # The project is not materialized here yet: the next cycle tries again.
            return False

    def _journal_record(self, project_ref: str) -> dict[str, Any]:
        try:
            return dict(self.field.journal_incident_record(self.config.worker_name, project_ref) or {})
        except DomainError:
            return {}

    def _write_journal_record(self, project_ref: str, record: Mapping[str, Any] | None) -> bool:
        try:
            self.field.write_journal_incident_record(self.config.worker_name, project_ref, record)
            return True
        except DomainError:
            logger.debug("Could not write the journal incident record.", exc_info=True)
            return False

    def _journal_open_key(self, record: Mapping[str, Any]) -> str:
        return f"journal:{self.config.worker_name}:{record.get('project_ref')}:{record.get('since')}"

    def _journal_close_key(self, record: Mapping[str, Any]) -> str:
        return f"{self._journal_open_key(record)}:restored"

    def _queue_journal_open(self, record: Mapping[str, Any]) -> bool:
        metadata = {
            "notice": "journal",
            "state": "unavailable",
            "project_ref": str(record.get("project_ref") or ""),
            "repository": str(record.get("repository") or ""),
            "path": str(record.get("path") or ""),
            "error_code": str(record.get("error_code") or ""),
            "since": str(record.get("since") or ""),
            "runtime_kind": self.config.runtime_kind,
            "runtime_session_id": self.config.runtime_session_id,
            "worker_alias": self.config.worker_alias or "",
            "reported_by": "relay",
        }
        return self._journal_notice_event(
            key=self._journal_open_key(record), summary=str(record.get("message") or ""), metadata=metadata
        )

    def _queue_journal_close(self, record: Mapping[str, Any]) -> bool:
        metadata = {
            "notice": "journal",
            "state": "available",
            "project_ref": str(record.get("project_ref") or ""),
            "since": str(record.get("since") or ""),
            "restored_at": str(record.get("closed_at") or ""),
            "reported_by": "relay",
        }
        return self._journal_notice_event(
            key=self._journal_close_key(record),
            summary=f"journal available again for {record.get('project_ref')}",
            metadata=metadata,
        )

    def _report_journal_incident(self, project_ref: str, gap: Mapping[str, Any] | None) -> None:
        """Report a project's journal being unavailable once, and its return once (W304 D13).

        The same record and phases as the notification-path incident: the
        record is written before the first event (the intent is the gate), each
        event's phase moves from pending to enqueued, a phase lost after an
        enqueue is repaired from the outbox receipt, and a relay rebuilt or
        restarted in the middle resumes from the record it finds. So the card
        is told once that the journal is unavailable and once that it is back.
        """

        record = self._journal_record(project_ref)
        if record and record.get("close_note") == "pending":
            if self._note_exists(self._journal_close_key(record)) or self._queue_journal_close(record):
                self._write_journal_record(project_ref, None)
                record = {}
            else:
                return
        if gap is not None:
            same = (
                record
                and record.get("error_code") == gap["error_code"]
                and record.get("path", "") == gap.get("path", "")
            )
            if same:
                if record.get("open_note") == "enqueued":
                    return
                if self._note_exists(self._journal_open_key(record)) or self._queue_journal_open(record):
                    record["open_note"] = "enqueued"
                    self._write_journal_record(project_ref, record)
                return
            if record:
                # A different gap replaced the one on record: close the old one if it was ever told.
                if record.get("open_note") == "enqueued" or self._note_exists(self._journal_open_key(record)):
                    record["closed_at"] = utc_now()
                    if not self._queue_journal_close(record):
                        record["close_note"] = "pending"
                        self._write_journal_record(project_ref, record)
                        return
                self._write_journal_record(project_ref, None)
            record = {
                "project_ref": project_ref,
                "since": utc_now(),
                "repository": str(gap.get("repository") or ""),
                "path": str(gap.get("path") or ""),
                "error_code": str(gap["error_code"]),
                "message": str(gap["message"]),
                "open_note": "pending",
            }
            if not self._write_journal_record(project_ref, record):
                return
            if self._queue_journal_open(record):
                record["open_note"] = "enqueued"
                self._write_journal_record(project_ref, record)
            return
        if not record:
            return
        told = record.get("open_note") == "enqueued" or self._note_exists(self._journal_open_key(record))
        if not told:
            # Back before anyone was told: nothing to close.
            self._write_journal_record(project_ref, None)
            return
        record["open_note"] = "enqueued"
        record["closed_at"] = record.get("closed_at") or utc_now()
        record["close_note"] = "pending"
        if not self._write_journal_record(project_ref, record):
            return
        if self._queue_journal_close(record):
            self._write_journal_record(project_ref, None)

    def _reconcile_journal_binding(self, heartbeat: Mapping[str, Any]) -> dict[str, Any]:
        if self.journal_workspace is None:
            return {"state": "disabled"}
        binding = heartbeat.get("journal_binding")
        if not isinstance(binding, Mapping) or not binding.get("journal_home_ref"):
            self.journal_workspace.initialize()
            return {"state": "unbound", "project_ref": f"work:project:{self.config.project_id}"}
        project_ref = f"work:project:{self.config.project_id}"
        try:
            result = self.journal_workspace.reconcile(
                binding,
                create_home=self.config.create_missing_journal_home,
            )
        except DomainError as exc:
            if exc.code not in LOCAL_JOURNAL_MAPPING_CODES:
                raise
            details = dict(exc.details or {})
            repository = str(details.get("repository") or details.get("alias") or "")
            path = str(details.get("path") or "")
            gap = {
                "state": "unmapped",
                "project_ref": project_ref,
                "journal_home_ref": str(binding.get("journal_home_ref") or ""),
                "error_code": exc.code,
                "error_summary": str(exc),
                "repository": repository,
                "path": path,
                # One sentence for the worker and the operator (W304 D13).
                "message": (
                    f"journal unavailable for {project_ref}: {repository} not found at {path}"
                    if path
                    else f"journal unavailable for {project_ref}: {exc}"
                ),
                "mapped_repositories": sorted(self.journal_workspace.repositories.roots),
            }
            self._journal_mapping_gap = gap
            key = (self.config.relay_id, project_ref, exc.code)
            if key not in _reported_journal_gaps:
                _reported_journal_gaps.add(key)
                logger.warning(
                    "Problem Board %s (code=%s ref=%s mapped=%s); controls still flow, "
                    "journal views refuse until the checkout exists or "
                    "`pb host configure --set-source-repo` maps it",
                    gap["message"],
                    exc.code,
                    gap["journal_home_ref"],
                    ",".join(gap["mapped_repositories"]) or "-",
                )
            self._report_journal_incident(project_ref, gap)
            return gap
        self._report_journal_incident(project_ref, None)
        self._journal_mapping_gap = None
        _reported_journal_gaps.difference_update(
            {key for key in _reported_journal_gaps if key[:2] == (self.config.relay_id, project_ref)}
        )
        return {
            "state": "bound",
            "journal_home_ref": result["journal_home_ref"],
            "project_artifact_ref": result["project_artifact_ref"],
            "local_project_artifact": result["local_project_artifact"],
            "revision": result["revision"],
            "changed": result["changed"],
            "indexed_entries": result["indexed_entries"],
        }

    async def _publish_worker(
        self,
        *,
        reconcile_ceiling_seconds: int | None = None,
        relay_degraded_intervals: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        capabilities = set(self.config.capabilities)
        if self.journal_workspace is not None:
            capabilities.add("journal-view")
        published_capabilities = sorted(capabilities)
        # The advertised interval sizes the board's relay window (three
        # intervals), so an idling worker publishes the interval it will
        # actually keep rather than reporting stale between slow cycles.
        interval = int(reconcile_ceiling_seconds or self.config.reconcile_ceiling_seconds)
        payload: dict[str, Any] = {
            "worker_name": self.config.worker_name,
            "worker_alias": self.config.worker_alias,
            "worker_identity": self.config.worker_identity,
            "runtime_kind": self.config.runtime_kind,
            "runtime_session_id": self.config.runtime_session_id,
            "capabilities": published_capabilities,
            "host_id": self.config.host_id,
            "host_label": self.config.host_label,
            "host_kind": self.config.host_kind,
            "relay_id": self.config.relay_id,
            "reconcile_ceiling_seconds": interval,
            # The pre-rename key stays on the wire for one transition so a
            # board or widget still reading it does not lose the value and
            # quietly fall back to a default ceiling.
            "poll_interval_seconds": interval,
            "repository_urls": dict(self.config.source_repository_urls),
        }
        await self._add_runtime_account(payload)
        completed = [
            {
                "code": str(item.get("code") or ""),
                "started_at": str(item.get("started_at") or ""),
                "ended_at": str(item.get("ended_at") or ""),
            }
            for item in relay_degraded_intervals
            if isinstance(item, Mapping)
            and item.get("code")
            and item.get("started_at")
            and item.get("ended_at")
        ][-20:]
        if completed:
            payload["relay_degraded_intervals"] = completed
        response = await self.client.action(
            object_ref="work:worker:self",
            action="worker.publish",
            payload=payload,
        )
        self._published_interval = interval
        remote = _object_result(response)
        self._record_attendance_observation(remote)
        local = self.field.register_worker(
            worker_name=self.config.worker_name,
            worker_alias=str(remote.get("worker_alias") or self.config.worker_alias),
            worker_identity=str(
                remote.get("worker_identity") or self.config.worker_identity
            ),
            runtime_kind=self.config.runtime_kind,
            runtime_session_id=str(
                remote.get("runtime_session_id") or self.config.runtime_session_id
            ),
            capabilities=published_capabilities,
            authority_label=str(remote.get("authority_label") or "remote-authority"),
            host_id=self.config.host_id,
            host_label=self.config.host_label,
            host_kind=self.config.host_kind,
            relay_id=self.config.relay_id,
            reconcile_ceiling_seconds=interval,
            attended_project_refs=[
                str(item.get("project_ref") or "")
                for item in remote.get("attendances") or []
                if isinstance(item, Mapping) and item.get("project_ref")
            ] if "attendances" in remote else None,
            worker_ref=str(remote.get("worker_ref") or ""),
            worker_id=str(remote.get("worker_id") or ""),
            control_plane_state="published",
        )
        remote_status = str(remote.get("pool_status") or "active")
        if local.get("pool_status") != remote_status and remote_status in {"active", "limbo"}:
            local = self.field.set_worker_status(
                self.config.worker_name,
                remote_status,
                actor="control-plane",
                reason=str(remote.get("eviction_reason") or "remote pool state"),
            )
        return {"remote": remote, "local": local}

    async def publish_relay_degraded_intervals(
        self, intervals: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        """Offer completed local outages once the Card-authorized route recovers."""

        return await self._publish_worker(
            reconcile_ceiling_seconds=self._published_interval,
            relay_degraded_intervals=intervals,
        )

    async def _pull_controls(self, *, project_ref: str | None = None) -> dict[str, int]:
        requested_project_ref = (
            f"work:project:{self.config.project_id}"
            if project_ref is None
            else str(project_ref)
        )
        response = await self.client.action(
            object_ref="work:worker:self",
            action="control.pull",
            payload={
                "project_ref": requested_project_ref,
                "lease_owner": self.config.relay_id,
                "lease_seconds": max(60, self.config.reconcile_ceiling_seconds * 3),
                "limit": 20,
            },
        )
        result = _object_result(response)
        lease_id = str(result.get("lease_id") or "")
        items = result.get("items") if isinstance(result.get("items"), list) else []
        # Materialize and assign controls create the local state every other
        # control needs. The control plane leases in creation order, but this
        # host must not depend on it: live, a ping handled before the
        # project's own materialize control in the same pull was refused as
        # "project not materialized". Creation order breaks ties.
        items = sorted(
            (item for item in items if isinstance(item, Mapping)),
            key=lambda item: (
                {"discard": 0, "materialize": 1, "assign": 2}.get(
                    str(item.get("kind") or ""), 3
                ),
                str(item.get("created_at") or ""),
            ),
        )
        counts = {
            "controls_materialized": 0,
            "controls_refused": 0,
            "controls_deferred": 0,
            "controls_discarded": 0,
            "discard_notices": 0,
            "journal_views_published": 0,
            "note_views_published": 0,
            "plan_commands_applied": 0,
            "plan_commands_refused": 0,
        }
        attendance_refresh_state: str | None = None
        for item in items:
            if not isinstance(item, Mapping):
                continue
            command_ref = str(item.get("ref") or item.get("command_ref") or "")
            kind = str(item.get("kind") or "")
            if not isinstance(item.get("payload"), Mapping):
                await self._refuse_malformed_control(
                    command_ref=command_ref,
                    kind=kind,
                    lease_id=lease_id,
                    error=DomainError(
                        "field_control_payload_invalid",
                        "A leased control payload must be a JSON object.",
                        details={
                            "field": "payload",
                            "value_type": type(item.get("payload")).__name__,
                        },
                    ),
                )
                counts["controls_refused"] += 1
                continue
            if kind == "discard":
                payload = (
                    dict(item.get("payload") or {})
                    if isinstance(item.get("payload"), Mapping)
                    else {}
                )
                if content_hash(payload) != str(item.get("payload_hash") or ""):
                    await self._refuse_malformed_control(
                        command_ref=command_ref,
                        kind=kind,
                        lease_id=lease_id,
                        error=DomainError(
                            "field_control_hash_invalid",
                            "The discard payload does not match its declared hash.",
                            status=409,
                            details={"field": "payload_hash"},
                        ),
                    )
                    counts["controls_refused"] += 1
                    continue
                discarded = self.field.discard_control_messages(
                    worker_name=self.config.worker_name,
                    discard_ref=command_ref,
                    sender_label=str(payload.get("sender_label") or "operator"),
                    reason=str(payload.get("reason") or ""),
                    targets=[
                        dict(value)
                        for value in payload.get("targets") or []
                        if isinstance(value, Mapping)
                    ],
                )
                notice = (
                    discarded.get("soft_notice")
                    if isinstance(discarded.get("soft_notice"), Mapping)
                    else {}
                )
                await self.client.action(
                    object_ref=command_ref,
                    action="control.discard_complete",
                    payload={
                        "lease_id": lease_id,
                        "lease_owner": self.config.relay_id,
                        "outcomes": list(discarded.get("outcomes") or []),
                        "notice_ref": str(notice.get("message_ref") or ""),
                    },
                )
                counts["controls_discarded"] += sum(
                    1
                    for value in discarded.get("outcomes") or []
                    if value.get("outcome")
                    in {"discarded_before_receipt", "already_discarded", "not_present"}
                )
                counts["discard_notices"] += 1 if notice else 0
                continue
            if kind in PLAN_HOST_CONTROL_KINDS:
                # Plan mutations and note reads now execute against PostgreSQL
                # at the operation boundary. A queued command can only be a
                # pre-cutover artifact; applying it to the local field would
                # recreate a second plan authority.
                payload = (
                    dict(item.get("payload") or {})
                    if isinstance(item.get("payload"), Mapping)
                    else {}
                )
                error_code = "work_plan_host_protocol_retired"
                error_message = (
                    "This pre-cutover plan-host command cannot run locally. "
                    "Retry the operation so PostgreSQL applies it directly."
                )
                if kind == NOTE_VIEW_KIND and payload.get("view_ref"):
                    try:
                        await self.client.action(
                            object_ref=str(payload.get("view_ref") or ""),
                            action="note.view.fail",
                            payload={
                                "error_code": error_code,
                                "error_summary": error_message,
                            },
                        )
                    except DomainError:
                        pass
                result_payload = {
                    "operation": kind,
                    "work_ref": str(item.get("work_ref") or ""),
                    "expected_revision": payload.get("expected_revision"),
                    "observed_revision": None,
                    "error": {
                        "code": error_code,
                        "message": error_message,
                        "details": {"retryable": True},
                    },
                }
                await self._settle_leased_control(
                    command_ref,
                    action="control.refuse",
                    kind=kind,
                    payload={
                        "lease_id": lease_id,
                        "lease_owner": self.config.relay_id,
                        "result_summary": f"Plan host refused: {error_code}",
                        "result": result_payload,
                    },
                )
                counts["plan_commands_refused"] += 1
                continue
            refusal = self._receiver_refusal(item)
            if refusal:
                await self._settle_leased_control(
                    command_ref,
                    action="control.refuse",
                    kind=kind,
                    sender=str(item.get("sender") or ""),
                    payload={
                        "lease_id": lease_id,
                        "lease_owner": self.config.relay_id,
                        "result_summary": f"Receiving host refused control: {refusal}",
                        "result": {"delivery_failure": self._receiver_policy_failure(item, refusal)},
                    },
                )
                counts["controls_refused"] += 1
                continue
            if kind in {"journal.catalog", "journal.read"}:
                try:
                    view_ref = await self._serve_journal_view(item)
                except DomainError as exc:
                    payload = dict(item.get("payload") or {})
                    view_ref = str(payload.get("view_ref") or "")
                    if view_ref:
                        try:
                            await self.client.action(
                                object_ref=view_ref,
                                action="journal.view.fail",
                                payload={
                                    "error_code": exc.code,
                                    "error_summary": str(exc),
                                },
                            )
                        except DomainError:
                            pass
                    await self._settle_leased_control(
                        command_ref,
                        action="control.refuse",
                        kind=kind,
                        payload={
                            "lease_id": lease_id,
                            "lease_owner": self.config.relay_id,
                            "result_summary": f"Journal view refused: {exc.code}",
                        },
                    )
                    counts["controls_refused"] += 1
                    continue
                await self._settle_leased_control(
                    command_ref,
                    action="control.acknowledge",
                    kind=kind,
                    payload={
                        "lease_id": lease_id,
                        "lease_owner": self.config.relay_id,
                        "result_summary": "Journal view published for the requesting user.",
                        "result_ref": view_ref,
                    },
                )
                counts["journal_views_published"] += 1
                continue
            if kind == "session.resume":
                try:
                    view_ref = await self._serve_session_resume_view(item)
                except DomainError as exc:
                    payload = dict(item.get("payload") or {})
                    view_ref = str(payload.get("view_ref") or "")
                    if view_ref:
                        try:
                            await self.client.action(
                                object_ref=view_ref,
                                action="session.resume.fail",
                                payload={
                                    "error_code": exc.code,
                                    "error_summary": str(exc),
                                },
                            )
                        except DomainError:
                            pass
                    await self._settle_leased_control(
                        command_ref,
                        action="control.refuse",
                        kind=kind,
                        payload={
                            "lease_id": lease_id,
                            "lease_owner": self.config.relay_id,
                            "result_summary": f"Session resume view refused: {exc.code}",
                        },
                    )
                    counts["controls_refused"] += 1
                    continue
                await self._settle_leased_control(
                    command_ref,
                    action="control.acknowledge",
                    kind=kind,
                    payload={
                        "lease_id": lease_id,
                        "lease_owner": self.config.relay_id,
                        "result_summary": "Resume command prepared for the requesting user.",
                        "result_ref": view_ref,
                    },
                )
                counts["session_resume_views_published"] = 1
                continue
            try:
                await self._fetch_attachments(item)
                receipt = self.field.materialize_control(item)
                self._attendance_cache.get("not_linked_deferrals", {}).pop(command_ref, None)
            except DomainError as exc:
                if exc.code == "field_project_not_materialized":
                    # Not a refusal: the project's materialize control has not
                    # reached this host yet. The lease is left to expire so the
                    # control plane offers the control again next cycle, after
                    # the project exists locally. Refusing here was terminal.
                    logger.warning(
                        "Problem Board control deferred until the project is "
                        "materialized locally project=%s control=%s kind=%s",
                        requested_project_ref or "direct",
                        command_ref,
                        kind,
                    )
                    counts["controls_deferred"] += 1
                    continue
                if exc.code == "field_worker_not_linked":
                    should_defer = self._defer_until_attendance_read(command_ref)
                    if should_defer:
                        if attendance_refresh_state is None:
                            attendance_refresh_state = (
                                await self._read_attendance_for_deferred_control()
                            )
                        if attendance_refresh_state == "limbo":
                            logger.warning(
                                "Problem Board stopped the leased control batch because "
                                "the worker entered limbo worker=%s",
                                self.config.worker_name,
                            )
                            counts["controls_deferred"] += 1
                            break
                        if attendance_refresh_state == "refreshed":
                            # Every item was leased before this one batch read,
                            # so the same authoritative snapshot resolves every
                            # cached not-linked result in the pull.
                            self._attendance_cache.get(
                                "not_linked_deferrals", {}
                            ).pop(command_ref, None)
                            should_defer = False
                    if not should_defer:
                        # The board lists the control's project when the local
                        # host record is what lags. Synchronize that attendance
                        # and deliver the already-leased control.
                        receipt = self._materialize_after_board_read(item)
                        if receipt is not None:
                            await self._settle_leased_control(
                                command_ref,
                                action="control.acknowledge",
                                kind=kind,
                                payload={
                                    "lease_id": lease_id,
                                    "lease_owner": self.config.relay_id,
                                    "result_summary": str(
                                        receipt.get("delivery_status")
                                        or "materialized"
                                    ),
                                    "result_ref": str(
                                        receipt.get("message_ref") or ""
                                    ),
                                },
                            )
                            counts["controls_materialized"] += 1
                            continue
                    if should_defer:
                        # No authoritative board snapshot arrived. The one
                        # batch read already logged why; keep this lease
                        # eligible for a later cycle and continue with peers.
                        logger.debug(
                            "Problem Board control remains deferred after attendance "
                            "read control=%s kind=%s worker=%s",
                            command_ref,
                            kind,
                            self.config.worker_name,
                        )
                        counts["controls_deferred"] += 1
                        continue
                await self._refuse_malformed_control(
                    command_ref=command_ref,
                    kind=kind,
                    lease_id=lease_id,
                    error=exc,
                )
                counts["controls_refused"] += 1
                continue
            await self._settle_leased_control(
                command_ref,
                action="control.acknowledge",
                kind=kind,
                payload={
                    "lease_id": lease_id,
                    "lease_owner": self.config.relay_id,
                    "result_summary": str(receipt.get("delivery_status") or "materialized"),
                    "result_ref": str(receipt.get("message_ref") or ""),
                },
            )
            counts["controls_materialized"] += 1
        return counts

    async def _upload_attachments(self, files: Sequence[Mapping[str, Any]]) -> list[str]:
        """Each outbound file goes into a slot the control plane mints; the mail names the staged refs."""
        refs: list[str] = []
        for item in files:
            path = Path(str(item.get("path") or ""))
            filename = str(item.get("filename") or path.name)
            data = path.read_bytes()
            slot = _object_result(
                await self.client.action(
                    object_ref="work:worker:self",
                    action="attachment.request_upload",
                    payload={"filename": filename},
                )
            )
            await self.uploader(
                str(slot.get("upload_url") or ""),
                data,
                str(item.get("mime") or "application/octet-stream"),
            )
            refs.append(str(slot.get("staged_ref") or ""))
        return refs

    async def _fetch_attachments(self, item: Mapping[str, Any]) -> None:
        """Inbound files named by signed links land in this field before the envelope is written."""
        payload = item.get("payload") if isinstance(item.get("payload"), Mapping) else {}
        descriptors = payload.get("attachments") if isinstance(payload.get("attachments"), list) else []
        if not descriptors:
            return
        command_id = parse_ref(str(item.get("ref") or item.get("command_ref") or "")).object_id
        project_ref = str(item.get("project_ref") or "")
        project_id = parse_ref(project_ref).object_id if project_ref else ""
        recipient = str(item.get("recipient") or self.config.worker_name).lower()
        folder = self.field._mail_root(project_id, recipient) / "attachments" / command_id
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        # The payload is hashed by the control plane; the local paths sit
        # beside it so the envelope's hash check still passes.
        normalized_descriptors = normalize_attachment_manifest(
            descriptors,
            require_local_path=False,
        )
        local_paths: dict[str, str] = {}
        local_files: dict[str, dict[str, Any]] = {}
        for descriptor, normalized in zip(
            descriptors,
            normalized_descriptors,
            strict=True,
        ):
            url = str(descriptor.get("download_url") or "")
            filename = str(normalized["filename"])
            file_ref = str(normalized["file_ref"])
            if not url:
                raise DomainError(
                    "field_control_attachment_url_missing",
                    "Every control attachment must carry a signed download URL.",
                    details={"file_ref": file_ref, "filename": filename},
                )
            data = await self.downloader(url)
            declared_size = int(normalized["size"])
            if declared_size != len(data):
                raise DomainError(
                    "field_control_attachment_size_mismatch",
                    "The downloaded attachment size does not match its descriptor.",
                    status=409,
                    details={
                        "file_ref": file_ref,
                        "declared_size": declared_size,
                        "downloaded_size": len(data),
                    },
                )
            attachment_id = hashlib.sha256(file_ref.encode("utf-8")).hexdigest()[:16]
            target = folder / attachment_id / filename
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target.write_bytes(data)
            local_paths[file_ref] = str(target)
            local_files[file_ref] = {
                "local_path": str(target),
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        if local_paths and isinstance(item, dict):
            item["attachment_local_paths"] = local_paths
            item["attachment_local_files"] = local_files

    async def _serve_journal_view(self, control: Mapping[str, Any]) -> str:
        if self.journal_workspace is None:
            raise DomainError(
                "work_journal_workspace_disabled",
                "This relay has no LOCAL journal workspace mapping.",
            )
        gap = self._journal_mapping_gap
        if gap is not None:
            raise DomainError(
                str(gap["error_code"]),
                str(gap["error_summary"]),
                details={
                    "repository": gap["repository"],
                    "journal_home_ref": gap["journal_home_ref"],
                    "mapped_repositories": list(gap["mapped_repositories"]),
                },
            )
        payload = dict(control.get("payload") or {})
        if content_hash(payload) != str(control.get("payload_hash") or ""):
            raise DomainError(
                "field_control_hash_invalid",
                "The journal-view control payload does not match its declared hash.",
                status=409,
            )
        project_ref = str(control.get("project_ref") or "")
        if project_ref != f"work:project:{self.config.project_id}":
            raise DomainError(
                "work_journal_view_project_mismatch",
                "The journal-view request targets another configured project.",
                status=409,
            )
        view_ref = str(payload.get("view_ref") or "")
        if parse_ref(view_ref).kind != "journal_view":
            raise DomainError(
                "work_journal_view_ref_invalid",
                "Expected a Problem Board journal-view reference.",
            )
        mode = str(payload.get("mode") or "")
        kind = str(control.get("kind") or "")
        if mode == "catalog" and kind == "journal.catalog":
            page = self.journal_workspace.view_catalog_page(
                project_ref=project_ref,
                query=str(payload.get("query") or ""),
                cursor=str(payload.get("cursor") or ""),
                limit=max(1, min(int(payload.get("limit") or 100), 100)),
                worker_name=str(payload.get("author") or ""),
                date_from=str(payload.get("date_from") or ""),
                date_to=str(payload.get("date_to") or ""),
                statuses=[str(value) for value in payload.get("statuses") or []],
                weights=(payload.get("weights") if isinstance(payload.get("weights"), Mapping) else None),
            )
            entries = page["entries"]
            next_cursor = str(page.get("next_cursor") or "")
            action_payload: dict[str, Any] = {
                "entries": entries,
                "title": "",
                "content": "",
                "content_hash": content_hash(
                    {"entries": entries, "next_cursor": next_cursor}
                ),
                "next_cursor": next_cursor,
            }
        elif mode == "document" and kind == "journal.read":
            result = self.journal_workspace.read_for_project(
                project_ref=project_ref,
                repository_journal_ref=str(payload.get("repository_journal_ref") or ""),
            )
            metadata = (
                result.get("metadata")
                if isinstance(result.get("metadata"), Mapping)
                else {}
            )
            action_payload = {
                "entries": [],
                "title": str(metadata.get("title") or "Journal entry"),
                "content": str(result.get("content") or ""),
                "content_hash": str(result.get("content_hash") or ""),
                "repository_journal_ref": str(result["repository_journal_ref"]),
                "next_cursor": "",
            }
        else:
            raise DomainError(
                "work_journal_view_mode_mismatch",
                "The journal-view mode does not match its control kind.",
                status=409,
            )
        await self.client.action(
            object_ref=view_ref,
            action="journal.view.publish",
            payload=action_payload,
        )
        return view_ref

    async def _serve_session_resume_view(self, control: Mapping[str, Any]) -> str:
        payload = dict(control.get("payload") or {})
        if content_hash(payload) != str(control.get("payload_hash") or ""):
            raise DomainError(
                "field_control_hash_invalid",
                "The session-resume payload does not match its declared hash.",
                status=409,
            )
        project_ref = str(control.get("project_ref") or "")
        if project_ref and project_ref != f"work:project:{self.config.project_id}":
            raise DomainError(
                "work_session_resume_project_mismatch",
                "The resume request targets another configured project.",
                status=409,
            )
        view_ref = str(payload.get("view_ref") or "")
        if parse_ref(view_ref).kind != "session_resume":
            raise DomainError(
                "work_session_resume_ref_invalid",
                "Expected a Problem Board session-resume reference.",
            )
        generated = build_session_resume_command(
            runtime_kind=self.config.runtime_kind,
            runtime_session_id=self.config.runtime_session_id,
            working_directory=self.config.working_directory,
            allowed_roots=self.config.allowed_roots,
            field_root=self.config.field_root,
        )
        command = str(generated["command"])
        await self.client.action(
            object_ref=view_ref,
            action="session.resume.publish",
            payload={
                "runtime_kind": self.config.runtime_kind,
                "runtime_session_id": self.config.runtime_session_id,
                "command": command,
                "command_hash": hashlib.sha256(command.encode("utf-8")).hexdigest(),
            },
        )
        return view_ref

    # A Claude Code worker whose inbox checks have lapsed has no watch process
    # and cannot be reached through the board. A Codex session's inbox checks
    # are expected to lapse while it idles, so for it the reportable
    # conditions are about its wake: a retry that never reached the queue or
    # a wake taken twice without acknowledgement are dead paths, and a wake
    # the native queue still holds past the report threshold is a factual
    # queue notice, not a verdict. Retired and detached sessions are not
    # dead paths.
    DEAD_PATH_RUNTIMES = ("claude-code", "codex")

    def _report_dead_notification_path(self) -> dict[str, Any]:
        """Act on a dead notification path instead of only exposing it.

        On 2026-09-18 two Claude Code workers sat unreachable for hours while
        every listing carried `reachability.overdue_by_seconds` for them and
        nothing read it. The worker that has the dead path cannot report it,
        because reporting needs the path. This relay can: it serves that worker
        and keeps running when the session's watch has stopped. Once per cycle
        it reads the local reachability record and, on the transition into a
        reportable condition, queues one update to the operator through the
        worker's own outbox, and one more when the condition ends.

        Conditions come in kinds, and the kind decides everything the operator
        sees. A Claude Code session whose inbox checks have lapsed, a Codex
        wake consumed twice without acknowledgement, and a Codex retry that
        never reached the queue are dead paths: they open as "notification
        path dead" and close as "restored". A Codex wake still queued past the
        report threshold is a fact about the queue, not a verdict on the
        session: it opens as "wake still queued" and closes as "wake no longer
        queued", and its state is never "dead"; reporting is state-specific.

        The relay builds a fresh adapter per worker per cycle, so the incident
        being reported lives in a durable record in the field store: kind,
        wake id, start, and a phase for each of the two notes, pending once
        the intent is written and enqueued once the outbox holds it. Every
        cycle resumes from the phase it finds. The initial intent gates the
        first enqueue, a lost phase after an enqueue is repaired from the
        outbox receipt, and a condition that ends or changes kind before the
        next cycle still gets each transition reported exactly once.
        """
        if self.config.runtime_kind not in self.DEAD_PATH_RUNTIMES:
            return {"state": "exempt"}
        try:
            reach = self.field.worker_reachability(self.config.worker_name)
        except DomainError:
            return {"state": "unknown"}
        current = self._current_incident(reach)
        try:
            record = dict(self.field.dead_path_record(self.config.worker_name) or {})
        except DomainError:
            record = {}
        if record and not record.get("kind"):
            # A v1 row from before kinds existed: everything it reported was
            # a dead path.
            record["kind"] = "dead_path"
        open_state = current["state"] if current else "alive"

        # An incident the record still holds whose close note is pending is
        # finished before anything else is looked at: the enqueue was refused,
        # or the process died after the enqueue and before the phase moved.
        # The outbox receipt tells which.
        if record and str(record.get("close_note") or "") == "pending":
            already_out = self._note_exists(self._close_key(record))
            if already_out or self._queue_close_note(record, reach):
                self._write_record(None)
                if not current:
                    self._log_close(record)
                    return {
                        "state": self._close_state(record),
                        "since": record["since"],
                        "reported": not already_out,
                    }
                record = {}
            else:
                return {
                    "state": self._close_state(record),
                    "since": record["since"],
                    "reported": False,
                    "retry": True,
                }

        if current:
            same = record and self._same_incident(record, current)
            if same:
                if str(record.get("open_note") or "") == "enqueued":
                    return {"state": open_state, "since": record["since"], "reported": False}
                # Intent was written and the note is not known to be out.
                if self._note_exists(self._open_key(record)) or self._queue_open_note(
                    record, reach
                ):
                    record["open_note"] = "enqueued"
                    self._write_record(record)
                    self._log_open(record)
                    return {"state": open_state, "since": record["since"], "reported": True}
                self._log_open_refused(record)
                return {"state": open_state, "since": record["since"], "reported": False, "retry": True}
            if record:
                # A different incident is on record and the session was never
                # seen clear in between: the old one ended and this one began
                # inside one cycle, or the kind changed on the same wake. Close
                # the old one first, if its open note ever went out.
                if str(record.get("open_note") or "") == "enqueued" or self._note_exists(
                    self._open_key(record)
                ):
                    if not self._queue_close_note(record, reach):
                        record["close_note"] = "pending"
                        self._write_record(record)
                        return {
                            "state": self._close_state(record),
                            "since": record["since"],
                            "reported": False,
                            "retry": True,
                        }
                self._write_record(None)
            # A new incident: the intent is a gate. With no record, a note that
            # went out before the process died could never be matched to its
            # close, so nothing is enqueued until the intent is durable.
            record = {
                "kind": current["kind"],
                "wake_id": current["wake_id"],
                "since": current["since"],
                "open_note": "pending",
                # Frozen at the open, so a retried event carries the same
                # facts and replays instead of conflicting.
                "pending": reach.get("pending_messages"),
            }
            if not self._write_record(record):
                logger.warning(
                    "Problem Board worker %s: %s since %s; the incident record could "
                    "not be written and the report is retried next cycle.",
                    self.config.worker_name,
                    self._open_phrase(record),
                    record["since"],
                )
                return {"state": open_state, "since": record["since"], "reported": False, "retry": True}
            if self._queue_open_note(record, reach):
                record["open_note"] = "enqueued"
                self._write_record(record)
                self._log_open(record)
                return {"state": open_state, "since": record["since"], "reported": True}
            self._log_open_refused(record)
            return {"state": open_state, "since": record["since"], "reported": False, "retry": True}

        if not record:
            return {"state": "alive", "since": "", "reported": False}
        # Clear, with an incident on record. Was its open note ever out? The
        # phase says so, or the receipt does when the phase never advanced.
        told = str(record.get("open_note") or "") == "enqueued" or self._note_exists(
            self._open_key(record)
        )
        if not told:
            # The condition ended before the operator heard of it. Nothing to
            # close; drop the record.
            self._write_record(None)
            return {"state": "alive", "since": "", "reported": False}
        record["open_note"] = "enqueued"
        record.setdefault("closed_at", str(reach.get("last_inbox_check_at") or "") or utc_now())
        record["close_note"] = "pending"
        if not self._write_record(record):
            # The record still says the incident is open, which is the truth
            # until the close intent is durable. Retry next cycle.
            return {
                "state": self._close_state(record),
                "since": record["since"],
                "reported": False,
                "retry": True,
            }
        if self._queue_close_note(record, reach):
            self._write_record(None)
            self._log_close(record)
            return {"state": self._close_state(record), "since": record["since"], "reported": True}
        logger.info(
            "Problem Board worker %s: %s since %s has ended; the closing note was "
            "refused and is retried next cycle.",
            self.config.worker_name,
            self._open_phrase(record),
            record["since"],
        )
        return {
            "state": self._close_state(record),
            "since": record["since"],
            "reported": False,
            "retry": True,
        }

    # Incident kinds. dead_path: a Claude Code session whose inbox checks have
    # lapsed. consumed_overdue and failed_overdue: a Codex session that took a
    # wake and either took its one retry too or could not be offered it, and
    # acknowledged nothing. queued: a Codex wake the native queue still holds
    # past the report threshold, a fact about the queue and not a verdict.
    DEAD_PATH_KINDS = ("dead_path", "consumed_overdue", "failed_overdue")

    def _current_incident(self, reach: Mapping[str, Any]) -> dict[str, str] | None:
        session_state = str(reach.get("session_state") or "")
        if session_state not in {"working", "waiting"}:
            return None
        if self.config.runtime_kind == "codex":
            wake_state = str(reach.get("wake_state") or "")
            since = str(reach.get("wake_overdue_since") or "") or "unknown"
            wake_id = str(reach.get("outstanding_wake_id") or "")
            if wake_state in {"consumed_overdue", "failed_overdue"}:
                return {"kind": wake_state, "since": since, "wake_id": wake_id, "state": "dead"}
            if wake_state == "queued_overdue":
                return {"kind": "queued", "since": since, "wake_id": wake_id, "state": "queued"}
            return None
        if reach.get("state") == "not_listening":
            return {
                "kind": "dead_path",
                "since": str(reach.get("last_inbox_check_at") or "") or "unknown",
                "wake_id": "",
                "state": "dead",
            }
        return None

    @staticmethod
    def _same_incident(record: Mapping[str, Any], current: Mapping[str, str]) -> bool:
        return (
            str(record.get("kind") or "") == current["kind"]
            and str(record.get("since") or "") == current["since"]
            and str(record.get("wake_id") or "") == current["wake_id"]
        )

    def _identity(self, record: Mapping[str, Any]) -> str:
        """The incident's identity, the same one _same_incident compares.

        Kind, wake id and start together. Two incidents that differ in kind
        on the same wake and stamp (a failed retry read as consumed a cycle
        later) are two incidents, and keys that carried only the start let
        the second one's open note replay the first's.
        """
        return (
            f"{self.config.worker_name}:{record.get('kind') or 'dead_path'}:"
            f"{record.get('wake_id') or '-'}:{record.get('since')}"
        )

    def _open_key(self, record: Mapping[str, Any]) -> str:
        prefix = "wake-queued" if record.get("kind") == "queued" else "dead-path"
        return f"{prefix}:{self._identity(record)}"

    def _close_key(self, record: Mapping[str, Any]) -> str:
        prefix = "wake-queued-cleared" if record.get("kind") == "queued" else "dead-path-restored"
        return f"{prefix}:{self._identity(record)}"

    @staticmethod
    def _close_state(record: Mapping[str, Any]) -> str:
        return "queue_cleared" if record.get("kind") == "queued" else "restored"

    @staticmethod
    def _open_phrase(record: Mapping[str, Any]) -> str:
        return (
            "wake still queued"
            if record.get("kind") == "queued"
            else "dead notification path"
        )

    def _log_open(self, record: Mapping[str, Any]) -> None:
        logger.warning(
            "Problem Board worker %s has a %s since %s; the operator has been told "
            "through the outbox.",
            self.config.worker_name,
            self._open_phrase(record),
            record.get("since"),
        )

    def _log_open_refused(self, record: Mapping[str, Any]) -> None:
        logger.warning(
            "Problem Board worker %s has a %s since %s; the operator note was refused "
            "and is retried next cycle.",
            self.config.worker_name,
            self._open_phrase(record),
            record.get("since"),
        )

    def _log_close(self, record: Mapping[str, Any]) -> None:
        if record.get("kind") == "queued":
            logger.info(
                "Problem Board worker %s: wake no longer queued (queued since %s).",
                self.config.worker_name,
                record.get("since"),
            )
            return
        logger.info(
            "Problem Board worker %s notification path restored (dead since %s).",
            self.config.worker_name,
            record.get("since"),
        )

    def _write_record(self, record: Mapping[str, Any] | None) -> bool:
        """Persist the incident record.

        A failed phase advance after an enqueue is repaired next cycle from
        the outbox receipt. A failed write of the initial intent is not
        repairable that way, because without the record nothing later knows
        which incident to look for, so the caller treats it as a gate.
        """
        try:
            self.field.write_dead_path_record(self.config.worker_name, record)
            return True
        except DomainError:
            logger.debug("Could not write the dead-path record.", exc_info=True)
            return False

    def _note_exists(self, key: str) -> bool:
        try:
            return bool(
                self.field.service_event_receipt_exists(
                    worker_name=self.config.worker_name, idempotency_key=key
                )
            )
        except DomainError:
            return False

    def _queue_open_note(self, record: Mapping[str, Any], reach: Mapping[str, Any]) -> bool:
        alias = self.config.worker_alias or self.config.worker_name
        pending = record.get("pending", reach.get("pending_messages"))
        kind = str(record.get("kind") or "")
        since = str(record.get("since") or "")
        wake_id = str(record.get("wake_id") or "")
        if kind == "consumed_overdue":
            subject = f"{alias}: notification path dead since {since}, {pending} message(s) waiting"
            body = (
                f"The relay for {alias} ({self.config.worker_name}) enqueued a wake "
                f"({wake_id}) into the session's native queue. The session took it, "
                "and took the one retry, without acknowledging either; overdue again "
                f"at {since}. The model ran turns that did not run pb worker receive "
                "with the wake id, so nothing on the board can tell whether it read "
                "its mail. Mail and assignments queue and are not lost. Pending now: "
                f"{pending}. The session is Codex {self.config.runtime_session_id}. "
                f"Running `pb worker receive --wake-id {wake_id}` in it clears the "
                "wake. The relay will not enqueue another for the same input. This "
                "notice is published by the relay, not by the model, which is why the "
                "model has not answered."
            )
        elif kind == "failed_overdue":
            subject = f"{alias}: notification path dead since {since}, {pending} message(s) waiting"
            body = (
                f"The relay for {alias} ({self.config.worker_name}) enqueued a wake "
                f"({wake_id}) into the session's native queue. The session took it "
                "without acknowledging it, and the one retry the relay is allowed "
                "could not be queued: the queue command answered "
                f"{reach.get('wake_last_error') or 'an error'}. Overdue again at "
                f"{since}. Nothing on the board can tell whether the model read its "
                "mail, and the relay cannot reach its queue. Mail and assignments "
                f"queue and are not lost. Pending now: {pending}. The session is Codex "
                f"{self.config.runtime_session_id}. Running `pb worker receive "
                f"--wake-id {wake_id}` in it clears the wake; if the queue command "
                "keeps failing, the Codex app-server for that session is the place to "
                "look. The relay will not enqueue another for the same input. This "
                "notice is published by the relay, not by the model, which is why the "
                "model has not answered."
            )
        elif kind == "queued":
            subject = f"{alias}: wake still queued since {since}, {pending} message(s) waiting"
            body = (
                f"Wake still queued. The relay for {alias} ({self.config.worker_name}) "
                f"enqueued a wake ({wake_id}) into the session's native queue. Its "
                f"first acknowledgement deadline passed at {since}, about a minute "
                "after it was enqueued, and a listing taken "
                f"{reach.get('wake_overdue_grace_seconds') or 300} seconds or more "
                f"after that (at {reach.get('wake_queued_confirmed_at') or 'unknown'}) "
                "still found it in the queue. This says the session has not started a turn "
                "on it, and nothing more: a Codex turn already running takes the wake "
                "when it ends, and the session's own presence reads "
                f"{reach.get('session_state') or 'unknown'} from its inbox checks. "
                f"Mail and assignments queue and are not lost. Pending now: {pending}. "
                f"The session is Codex {self.config.runtime_session_id}. If it is not "
                "mid-turn, typing anything in it makes it drain its queue and receive. "
                "The relay will not enqueue a second wake for the same input. This "
                "notice is published by the relay, not by the model."
            )
        else:
            subject = f"{alias}: notification path dead since {since}, {pending} message(s) waiting"
            body = (
                f"The relay for {alias} ({self.config.worker_name}) reports that the "
                f"session's inbox checks stopped at {since}. For a Claude Code worker "
                "that means its watch process has ended and nothing on the board can "
                "reach the model. Mail and assignments queue and are not lost. "
                f"Pending now: {pending}. The session is Claude Code "
                f"{self.config.runtime_session_id}. Typing anything in it, or its "
                "scheduled guard, restores the path. This notice is published by the "
                "relay, not by the model, which is why the model has not answered."
            )
        state = "wake_queued" if kind == "queued" else "dead"
        return self._queue_operator_note(
            key=self._open_key(record), subject=subject, body=body, record=record, state=state
        )

    def _queue_close_note(self, record: Mapping[str, Any], reach: Mapping[str, Any]) -> bool:
        alias = self.config.worker_alias or self.config.worker_name
        kind = str(record.get("kind") or "")
        since = str(record.get("since") or "")
        wake_id = str(record.get("wake_id") or "")
        seen = reach.get("last_inbox_check_at")
        if kind == "queued":
            subject = f"{alias}: wake no longer queued (queued since {since})"
            body = (
                f"The wake ({wake_id}) the relay for {alias} ({self.config.worker_name}) "
                f"reported as still queued since {since} is no longer in the session's "
                f"queue as of {seen}: the session took it or acknowledged it. Queued mail "
                "reaches the session on its next receive. No action is needed."
            )
        elif kind in {"consumed_overdue", "failed_overdue"}:
            subject = f"{alias}: notification path restored (dead since {since})"
            body = (
                f"The relay for {alias} ({self.config.worker_name}) no longer sees an "
                f"overdue wake for the session as of {seen}. The wake ({wake_id}) it "
                f"reported at {since} has been acknowledged or replaced, and queued mail "
                "reaches the session on its next receive."
            )
        else:
            subject = f"{alias}: notification path restored (dead since {since})"
            body = (
                f"The relay for {alias} ({self.config.worker_name}) sees inbox checks "
                f"again as of {seen}. The path that died at {since} is back, and queued "
                "mail reaches the session on its next receive."
            )
        state = "wake_dequeued" if kind == "queued" else "restored"
        return self._queue_operator_note(
            key=self._close_key(record), subject=subject, body=body, record=record, state=state
        )

    NOTICE_EVENT_KIND = "worker.notification_path"

    def _queue_operator_note(
        self,
        *,
        key: str,
        subject: str,
        body: str,
        record: Mapping[str, Any] | None = None,
        state: str = "",
    ) -> bool:
        """Publish one notification-path notice as a project event, never as mail.

        Operator ruling, 2026-09-23: notices the tooling writes on a worker's
        behalf are events, not messages (W182). The event carries its facts,
        is attributed to the relay's reporting of this worker rather than to
        the worker as a message sender, and is never indexed as mail. The
        board shows the path state on the worker's card from its inbox
        checks, so a worker that attends no project has no event to publish
        and loses nothing: its card already says stale or unreachable.

        True when the event under this key is queued, now or on an earlier
        attempt (its receipt). False on a refusal, so the caller keeps its
        record and tries again next cycle.
        """
        project_id = str(self.config.project_id or "").strip()
        if not project_id:
            logger.info(
                "Problem Board worker %s: %s. No project attended, so no event; "
                "the worker card shows the path state.",
                self.config.worker_name,
                subject,
            )
            return True
        facts = dict(record or {})
        metadata = {
            "notice": "notification_path",
            "state": state,
            "incident_kind": str(facts.get("kind") or "dead_path"),
            "since": str(facts.get("since") or ""),
            "pending_messages": facts.get("pending"),
            "wake_id": str(facts.get("wake_id") or ""),
            "runtime_kind": self.config.runtime_kind,
            "runtime_session_id": self.config.runtime_session_id,
            "worker_alias": self.config.worker_alias or "",
            "reported_by": "relay",
        }
        try:
            self.field.enqueue_service_event(
                project_id,
                worker_name=self.config.worker_name,
                kind=self.NOTICE_EVENT_KIND,
                summary=f"{subject}. {body}",
                source_event_ref=f"relay:{key}",
                metadata=metadata,
                idempotency_key=key,
            )
            return True
        except DomainError:
            # Telling the operator must not become a second failure on top of
            # the first. The caller logs and retries.
            logger.debug("Could not queue the notice event %s.", key, exc_info=True)
            return False

    async def _flush_outbox(
        self,
        *,
        kinds: set[str] | None = None,
        project_ref: str | None = None,
    ) -> dict[str, int]:
        with self._trace_stage(
            "attendance.outbox",
            operation="outbox.flush",
        ):
            if self._outbox_drain_lock is None:
                return await self._flush_outbox_unlocked(
                    kinds=kinds,
                    project_ref=project_ref,
                )
            async with self._outbox_drain_lock:
                return await self._flush_outbox_unlocked(
                    kinds=kinds,
                    project_ref=project_ref,
                )

    async def _flush_outbox_unlocked(
        self,
        *,
        kinds: set[str] | None = None,
        project_ref: str | None = None,
    ) -> dict[str, int]:
        counts = {
            "outbox_sent": 0,
            "outbox_ignored": 0,
            "outbox_refused": 0,
            "outbox_retried": 0,
        }
        for row in self.field.pull_outbox(
            relay_id=self.config.relay_id,
            worker_name=self.config.worker_name,
            project_ref=(
                project_ref
                if project_ref is not None
                else (
                    f"work:project:{self.config.project_id}"
                    if self.config.project_id
                    else ""
                )
            ),
            limit=20,
            kinds=kinds,
        ):
            payload = dict(row.get("payload") or {})
            kind = str(row.get("kind") or "")
            try:
                if kind == "plan.nodes.publish":
                    response = await self.client.action(
                        object_ref=str(row.get("project_ref") or ""),
                        action=kind,
                        payload={"batch": payload},
                    )
                elif kind == "project.plan.index":
                    response = await self.client.action(
                        object_ref=str(row.get("project_ref") or ""),
                        action=kind,
                        payload=payload,
                    )
                elif kind == "event.publish":
                    project_ref = str(
                        payload.pop("project_ref", "")
                        or row.get("project_ref")
                        or ""
                    )
                    response = await self.client.action(
                        object_ref=project_ref,
                        action=kind,
                        payload=payload,
                    )
                elif kind == "mail.route":
                    files = payload.pop("attachment_files", None) or []
                    if files:
                        payload["attachments"] = await self._upload_attachments(files)
                    response = await self.client.action(
                        object_ref=str(row.get("project_ref") or ""),
                        action=kind,
                        payload=payload,
                    )
                elif kind == "mail.reconciliation.publish":
                    response = await self.client.action(
                        object_ref=str(row.get("project_ref") or ""),
                        action=kind,
                        payload=payload,
                    )
                elif kind == "control.worker_settle":
                    command_ref = str(payload.pop("command_ref", "") or "")
                    response = await self.client.action(
                        object_ref=command_ref,
                        action=kind,
                        payload={
                            "session_id": payload.get("session_id") or "",
                            "outcome": payload.get("outcome") or "",
                            "result_summary": payload.get("summary") or "",
                            "result_ref": payload.get("message_ref") or "",
                        },
                    )
                elif kind == "assignment.report":
                    assignment_ref = str(payload.pop("assignment_ref", "") or "")
                    response = await self.client.action(
                        object_ref=assignment_ref,
                        action=kind,
                        payload=payload,
                    )
                elif kind == "project.report.publish":
                    files = payload.pop("attachment_files", None) or []
                    if files:
                        payload["staged_refs"] = await self._upload_attachments(files)
                    response = await self.client.action(
                        object_ref=str(row.get("object_ref") or ""),
                        action=kind,
                        payload=payload,
                    )
                elif kind == "project.report.fail":
                    response = await self.client.action(
                        object_ref=str(row.get("object_ref") or ""),
                        action=kind,
                        payload=payload,
                    )
                else:
                    self.field.settle_outbox(
                        str(row.get("outbox_id") or ""),
                        relay_id=self.config.relay_id,
                        outcome="refused",
                        remote_disposition="unsupported_outbox_kind",
                    )
                    counts["outbox_refused"] += 1
                    continue
            except DomainError as exc:
                if exc.status >= 500:
                    self.field.retry_outbox(
                        str(row.get("outbox_id") or ""),
                        relay_id=self.config.relay_id,
                        error_code=exc.code,
                        error_summary=str(exc),
                    )
                    counts["outbox_retried"] += 1
                    continue
                failure_report: dict[str, Any] | None = None
                if kind == "mail.route":
                    original = (
                        dict(row.get("payload") or {})
                        if isinstance(row.get("payload"), Mapping)
                        else {}
                    )
                    failed_recipient = str(original.get("recipient") or "")
                    source_message_ref = str(
                        original.get("source_message_ref") or ""
                    )
                    try:
                        project_ref = str(row.get("project_ref") or "")
                        project_id = (
                            parse_ref(project_ref).object_id if project_ref else ""
                        )
                        failure_report = self.field.report_mail_delivery_failure(
                            project_id,
                            receiver_worker_name=str(
                                row.get("worker_name") or self.config.worker_name
                            ),
                            failed_recipient=failed_recipient,
                            message={
                                "message_ref": source_message_ref,
                                "subject": str(original.get("subject") or ""),
                                "outbox_id": str(row.get("outbox_id") or ""),
                                "sender": str(
                                    row.get("worker_name")
                                    or self.config.worker_name
                                ),
                                "sender_identity": {
                                    "kind": "worker",
                                    "worker_name": str(
                                        row.get("worker_name")
                                        or self.config.worker_name
                                    ),
                                },
                                "correlation_id": str(
                                    original.get("correlation_id") or ""
                                ),
                            },
                            error_code=exc.code,
                            error_message=str(exc),
                            error_details=exc.details,
                            guidance=(
                                "The message was never delivered. Correct the "
                                "rejected field and replay the retained outbox "
                                "delivery."
                            ),
                        )
                    except DomainError as report_error:
                        # Do not turn a sender-notification failure into silent
                        # terminal loss. The original payload stays leased only
                        # until this bounded retry returns it to pending.
                        self.field.retry_outbox(
                            str(row.get("outbox_id") or ""),
                            relay_id=self.config.relay_id,
                            error_code="field_delivery_failure_notice_failed",
                            error_summary=(
                                f"{report_error.code}: {report_error}"
                            ),
                        )
                        counts["outbox_retried"] += 1
                        continue
                # The code alone sends the reader hunting. work_value_required
                # says a value was missing and not which one, and the author of
                # the refused payload is the only person who could fix it.
                details = exc.details if isinstance(exc.details, Mapping) else {}
                named = " ".join(
                    f"{key}={details[key]}"
                    for key in (
                        "field",
                        "recipient",
                        "operation",
                        "resource",
                        "required_roles",
                    )
                    if details.get(key)
                )
                self.field.settle_outbox(
                    str(row.get("outbox_id") or ""),
                    relay_id=self.config.relay_id,
                    outcome="refused",
                    remote_disposition=f"{exc.code} {named}".strip() if named else exc.code,
                    remote_result={
                        "error": exc.to_dict(),
                        **(
                            {"delivery_failure_report": failure_report}
                            if failure_report is not None
                            else {}
                        ),
                    },
                )
                counts["outbox_refused"] += 1
                continue
            remote = _object_result(response)
            disposition = str(remote.get("disposition") or "accepted")
            outcome = "ignored" if disposition.startswith("ignored_") else "sent"
            remote_result = None
            if kind == "project.plan.index":
                requested_ref = str(payload.get("item_ref") or "")
                requested = parse_plan_node_ref(requested_ref)
                resolved_items = [
                    dict(item)
                    for item in (remote.get("items") or [])
                    if isinstance(item, Mapping)
                ]
                current = None
                for item in resolved_items:
                    candidate_ref = str(
                        item.get("identity_ref") or item.get("item_ref") or ""
                    )
                    try:
                        candidate_identity = parse_plan_node_ref(
                            candidate_ref
                        ).identity_ref
                    except DomainError:
                        continue
                    if candidate_identity == requested.identity_ref:
                        current = item
                        break
                current_ref = str((current or {}).get("item_ref") or "")
                found = bool(
                    current
                    and (
                        requested.version is None
                        or current_ref == requested.ref
                    )
                )
                remote_result = {
                    "schema": PLAN_REF_RESOLUTION_SCHEMA,
                    "project_ref": str(remote.get("project_ref") or ""),
                    "item_ref": requested_ref,
                    "identity_ref": requested.identity_ref,
                    "current_work_ref": current_ref,
                    "found": found,
                    "error_code": (
                        "work_plan_item_version_stale"
                        if current and requested.version is not None and not found
                        else ""
                    ),
                    "generation_present": bool(remote.get("generation_present")),
                    "plan_revision": int(remote.get("plan_revision") or 0),
                    "content_hash": str(remote.get("content_hash") or ""),
                }
            self.field.settle_outbox(
                str(row.get("outbox_id") or ""),
                relay_id=self.config.relay_id,
                outcome=outcome,
                remote_ref=str(
                    remote.get("ref")
                    or remote.get("command_ref")
                    or remote.get("event_ref")
                    or remote.get("receipt_ref")
                    or row.get("object_ref")
                    or ""
                ),
                remote_disposition=disposition,
                remote_result=remote_result,
            )
            counts["outbox_ignored" if outcome == "ignored" else "outbox_sent"] += 1
        return counts

    async def _limbo_result(self) -> dict[str, Any]:
        discards = await self._pull_controls(project_ref="")
        outbox = await self._flush_outbox(
            kinds={
                "assignment.report",
                "mail.route",
                "control.worker_settle",
                "event.publish",
            }
        )
        return {
            "worker_name": self.config.worker_name,
            "pool_status": "limbo",
            **discards,
            **outbox,
            "agent_sessions_reported": 0,
            "journal_workspace": {"state": "not_reconciled"},
            "next_poll_seconds": self.config.idle_reconcile_ceiling_seconds,
        }

    def _materialize_attended_project(self, heartbeat: Mapping[str, Any]) -> bool:
        """Write the local record of a project this worker attends, from the board's heartbeat (W304 finding 39).

        The board answers every heartbeat of an attended project with the
        project row. The relay wrote the local record from it only when the
        worker also held an assignment, so an agent linked to a project with
        no work yet (claude-ops on spark1, 2026-09-24) had no record, and every
        receive failed. Mail for the project waits on the board until this
        record exists, then arrives on the next pull.
        """

        project_id = self.config.project_id
        project = (
            dict(heartbeat.get("assignment_project"))
            if isinstance(heartbeat.get("assignment_project"), Mapping)
            else {}
        )
        if self.field._project_path(project_id).exists():
            self._sync_project_repositories(project)
            return False
        if not project:
            return False
        title = str(project.get("title") or project_id)
        try:
            self.field.create_project(
                project_id=project_id,
                title=title,
                goal=str(project.get("goal") or title),
                owner="control-plane",
            )
        except DomainError as exc:
            if exc.code != "field_project_exists":
                raise
            return False
        logger.info(
            "Problem Board project written on this host project=%s worker=%s",
            f"work:project:{project_id}",
            self.config.worker_name,
        )
        self._sync_project_repositories(project)
        return True

    def _sync_project_repositories(self, project: Mapping[str, Any]) -> None:
        """Keep the project's declared repositories on this host, by the board's revision."""

        repositories = project.get("repositories")
        if not isinstance(repositories, list):
            return
        self.field.sync_project_repositories(
            self.config.project_id,
            repositories,
            revision=int(project.get("repositories_revision") or 0),
        )

    def _reconcile_assignments(
        self, heartbeat: Mapping[str, Any]
    ) -> tuple[int, list[dict[str, Any]]]:
        """Recover durable assignments whose notification never materialized."""

        assignments = heartbeat.get("assignments")
        if not isinstance(assignments, list):
            return 0, []
        project_id = self.config.project_id
        if not self.field._project_path(project_id).exists():
            if not assignments:
                return 0, []
            self._materialize_attended_project(heartbeat)
        created = 0
        active_refs: list[str] = []
        issues: list[dict[str, Any]] = []
        for raw in assignments:
            if not isinstance(raw, Mapping):
                continue
            assignment_ref = str(raw.get("assignment_ref") or "")
            try:
                parsed_assignment = parse_ref(assignment_ref)
            except DomainError:
                parsed_assignment = None
            if (
                parsed_assignment is not None
                and parsed_assignment.kind == "assignment"
                and assignment_ref not in active_refs
            ):
                active_refs.append(assignment_ref)

            source_hash = self.field.assignment_reconciliation_source_hash(raw)
            reconciliation = self.field.assignment_reconciliation_record(
                project_id, raw
            )
            same_source = bool(
                reconciliation
                and reconciliation.get("source_hash") == source_hash
            )
            refreshes_assignment_identity = bool(
                same_source
                and self.field.assignment_reconciliation_can_refresh_identity(
                    reconciliation, raw
                )
            )
            if same_source and reconciliation.get("state") == "notified":
                continue
            if (
                same_source
                and reconciliation.get("state") == "stopped"
                and reconciliation.get("notice_message_ref")
                and not refreshes_assignment_identity
            ):
                issues.append(reconciliation)
                continue
            if (
                same_source
                and not refreshes_assignment_identity
                and reconciliation.get("state") in {
                    "parked",
                    "report_pending",
                    "stopped",
                }
            ):
                try:
                    notice = self.field.send_assignment_reconciliation_failure_notice(
                        project_id,
                        raw,
                        recipient=self.config.worker_name,
                        error=dict(reconciliation.get("error") or {}),
                        sender_identity={
                            "kind": "control-plane",
                            "label": "Problem Board",
                        },
                    )
                except Exception as report_exc:  # noqa: BLE001
                    report_error = (
                        report_exc.to_dict()
                        if isinstance(report_exc, DomainError)
                        else {
                            "code": type(report_exc).__name__,
                            "message": str(report_exc),
                        }
                    )
                    issue = self.field.park_assignment_reconciliation_issue(
                        project_id,
                        raw,
                        error=dict(reconciliation.get("error") or {}),
                        report_error=report_error,
                    )
                else:
                    issue = self.field.park_assignment_reconciliation_issue(
                        project_id,
                        raw,
                        error=dict(reconciliation.get("error") or {}),
                        notice=notice,
                    )
                issues.append(issue)
                continue
            try:
                materialized = self.field.materialize_assignment(project_id, raw)
                notice = self.field.send_assignment_notice(
                    project_id,
                    assignment=materialized,
                    recipient=self.config.worker_name,
                    sender_identity={
                        "kind": "control-plane",
                        "label": "Problem Board",
                    },
                )
            except DomainError as exc:
                if exc.status >= 500:
                    raise
                try:
                    failure_notice = (
                        self.field.send_assignment_reconciliation_failure_notice(
                            project_id,
                            raw,
                            recipient=self.config.worker_name,
                            error=exc.to_dict(),
                            sender_identity={
                                "kind": "control-plane",
                                "label": "Problem Board",
                            },
                        )
                    )
                except Exception as report_exc:  # noqa: BLE001
                    report_error = (
                        report_exc.to_dict()
                        if isinstance(report_exc, DomainError)
                        else {
                            "code": type(report_exc).__name__,
                            "message": str(report_exc),
                        }
                    )
                    issue = self.field.park_assignment_reconciliation_issue(
                        project_id,
                        raw,
                        error=exc,
                        report_error=report_error,
                    )
                else:
                    issue = self.field.park_assignment_reconciliation_issue(
                        project_id,
                        raw,
                        error=exc,
                        notice=failure_notice,
                    )
                issues.append(issue)
                logger.warning(
                    "Problem Board stopped one assignment reconciliation "
                    "failure after its worker report assignment=%s "
                    "ownership_version=%s error_code=%s state=%s",
                    assignment_ref,
                    str(raw.get("ownership_version") or ""),
                    exc.code,
                    issue.get("state"),
                )
                continue
            self.field.record_assignment_reconciliation_success(
                project_id, raw, notice=notice
            )
            if not notice.get("replayed"):
                created += 1
        self.field.sync_active_assignments(
            project_id,
            worker_name=self.config.worker_name,
            assignment_refs=active_refs,
        )
        return created, issues

    async def _poll_project_once(
        self,
        *,
        agent_sessions: Sequence[Mapping[str, Any]],
        force_heartbeat: bool = False,
    ) -> dict[str, Any]:
        project_ref = f"work:project:{self.config.project_id}"
        session_delta, session_signature = self._session_report_delta(
            project_ref=project_ref,
            sessions=agent_sessions,
        )
        assignment_files_delta, files_signature = self._assignment_files_delta(
            project_ref=project_ref, fresh=force_heartbeat
        )
        store_reads_delta, store_reads_signature = self._store_reads_delta(
            project_ref=project_ref
        )
        # A change in files in flight is worth a heartbeat of its own. An empty
        # set that this process never published is not a change: without this,
        # every rebuilt adapter of a worker with no declared worktree forced one
        # extra heartbeat (30 in one push burst), a regression from W278 part B.
        # The empty set still rides on the next heartbeat that goes anyway.
        files_changed = assignment_files_delta is not None and (
            bool(assignment_files_delta)
            or project_ref in self._assignment_files_signatures
        )
        worker_info, info_pending = self._worker_info()
        heartbeat_sent = force_heartbeat or session_delta is not None or files_changed or info_pending or (
            self._project_heartbeat_wait(
                project_ref=project_ref,
                sessions=agent_sessions,
            )
            <= 0
        )
        heartbeat_result: dict[str, Any] = {}
        journal_workspace: dict[str, Any] = {"state": "unchanged"}
        mailbox_reconciliation: dict[str, Any] = {"state": "unchanged"}
        assignment_notices_reconciled = 0
        assignment_reconciliation_issues: list[dict[str, Any]] = []
        if heartbeat_sent:
            heartbeat_payload: dict[str, Any] = {
                "availability": "available",
                "project_ref": project_ref,
            }
            if session_delta is not None:
                heartbeat_payload["agent_sessions"] = session_delta
            if assignment_files_delta is not None:
                heartbeat_payload["assignment_files"] = assignment_files_delta
            if store_reads_delta is not None:
                heartbeat_payload["store_reads"] = store_reads_delta
            self._add_worker_info(heartbeat_payload, worker_info)
            await self._add_runtime_account(heartbeat_payload)
            try:
                with self._trace_stage(
                    "attendance.heartbeat",
                    operation="worker.heartbeat",
                ):
                    heartbeat_response = await self.client.action(
                        object_ref="work:worker:self",
                        action="worker.heartbeat",
                        payload=heartbeat_payload,
                    )
            except DomainError as exc:
                if exc.code != "work_worker_not_linked":
                    raise
                self._invalidate_cached_project(project_ref)
                self._report_dead_notification_path()
                outbox = await self._flush_outbox(
                    kinds={
                        "assignment.report",
                        "mail.route",
                        "control.worker_settle",
                        "event.publish",
                    }
                )
                return {
                    "worker_name": self.config.worker_name,
                    "pool_status": "active",
                    "attendance": "waiting_for_link",
                    "project_ref": project_ref,
                    "controls_materialized": 0,
                    "controls_refused": 0,
                    "controls_deferred": 0,
                    **outbox,
                    "heartbeat_sent": False,
                    "agent_sessions_reported": 0,
                    "journal_workspace": {"state": "waiting_for_link"},
                }
            self._record_project_heartbeat(project_ref)
            self._record_session_report(
                project_ref=project_ref,
                signature=session_signature,
            )
            self._assignment_files_signatures[project_ref] = files_signature
            if store_reads_delta is not None:
                self._store_reads_signatures[project_ref] = store_reads_signature
            heartbeat_result = _object_result(heartbeat_response)
            self._acknowledge_worker_info(worker_info, heartbeat_result)
            self._record_attendance_observation(heartbeat_result)
            self._materialize_attended_project(heartbeat_result)
            journal_workspace = self._reconcile_journal_binding(heartbeat_result)
            # The team travels with every project heartbeat so a worker can
            # address a teammate from its packet without asking the control plane.
            if isinstance(heartbeat_result.get("team"), list):
                try:
                    self.field.sync_project_team(
                        self.config.project_id, heartbeat_result["team"]
                    )
                except DomainError:
                    # The project is not materialized here yet; the next cycle
                    # after its materialize control lands stores the team.
                    pass
            recipients = heartbeat_result.get("mail_recipients")
            if not isinstance(recipients, list):
                recipients = heartbeat_result.get("team")
            mailbox_reconciliation = {"state": "directory_unavailable"}
            if isinstance(recipients, list):
                try:
                    mailbox_reconciliation = await self._run_startup_recovery(
                        recipients
                    )
                except DomainError:
                    # The same project-materialization race applies to this compact
                    # address directory. The next heartbeat replaces it completely.
                    mailbox_reconciliation = {"state": "waiting_for_materialization"}
            (
                assignment_notices_reconciled,
                assignment_reconciliation_issues,
            ) = self._reconcile_assignments(heartbeat_result)
        with self._trace_stage(
            "attendance.controls",
            operation="control.pull",
        ):
            controls = await self._pull_controls()
        self._report_dead_notification_path()
        outbox = await self._flush_outbox()
        return {
            "worker_name": self.config.worker_name,
            "pool_status": "active",
            "attendance": "linked",
            "project_ref": project_ref,
            **controls,
            "assignment_notices_reconciled": assignment_notices_reconciled,
            "assignment_reconciliation_issues": assignment_reconciliation_issues,
            "mailbox_reconciliation": mailbox_reconciliation,
            **outbox,
            "heartbeat_sent": heartbeat_sent,
            "next_heartbeat_seconds": self._next_project_heartbeat_seconds(
                project_ref=project_ref,
                sessions=agent_sessions,
            ),
            "agent_sessions_reported": (
                len(agent_sessions) if heartbeat_sent and session_delta is not None else 0
            ),
            "journal_workspace": journal_workspace,
        }

    async def _run_startup_recovery(
        self,
        recipients: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        with self._trace_stage(
            "startup_recovery",
            operation="mailbox.reconciliation",
        ):
            return await asyncio.to_thread(
                self._reconcile_project_mailboxes,
                recipients,
            )

    def _reconcile_project_mailboxes(
        self, recipients: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        self.field.sync_project_mail_recipients(self.config.project_id, recipients)
        return self.field.reconcile_project_mailboxes(
            self.config.project_id,
            reporter_worker_name=self.config.worker_name,
        )

    async def _ensure_registration(
        self, *, reconcile_ceiling_seconds: int | None = None
    ) -> dict[str, Any] | None:
        """Publish once per connection, or again when the advertised interval changes."""

        interval = int(reconcile_ceiling_seconds or self.config.reconcile_ceiling_seconds)
        if self._published_interval == interval:
            return None
        return await self._publish_worker(reconcile_ceiling_seconds=interval)

    def _worker_info(self) -> tuple[dict[str, Any], bool]:
        """The worker's local info line (W330) and whether the board has yet to store it."""

        info = self.field.worker_info(self.config.worker_name)
        pending = bool(info) and info.get("published_text") != str(info.get("text") or "")
        return info, pending

    @staticmethod
    def _add_worker_info(payload: dict[str, Any], info: Mapping[str, Any]) -> None:
        # W330: the worker's own line rides every heartbeat once it was ever
        # set; an empty text clears it on the board, and a worker that never
        # set one sends no key, so the board keeps what it has.
        if info:
            payload["worker_info"] = {"text": str(info.get("text") or "")}

    def _acknowledge_worker_info(self, info: Mapping[str, Any], result: Mapping[str, Any]) -> None:
        """Stop forcing heartbeats once the board answers with the same line."""

        if not info or "info_text" not in result:
            return
        stored = str(result.get("info_text") or "")
        if stored == str(info.get("text") or ""):
            self.field.mark_worker_info_published(self.config.worker_name, stored)

    async def _heartbeat_with_republish(
        self, payload: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Heartbeat first; republish only when the control plane no longer knows us."""

        heartbeat_payload = dict(payload)
        await self._add_runtime_account(heartbeat_payload)
        try:
            return _object_result(
                await self.client.action(
                    object_ref="work:worker:self", action="worker.heartbeat", payload=heartbeat_payload
                )
            ), None
        except DomainError as exc:
            if exc.code != "work_worker_unavailable":
                raise
        # Re-register with the interval already advertised so the recovery does
        # not cost a second publish for the interval alone.
        interval = self._published_interval
        self._published_interval = None
        registration = await self._publish_worker(reconcile_ceiling_seconds=interval)
        if registration["remote"].get("pool_status") == "limbo":
            return {}, registration
        return _object_result(
            await self.client.action(
                object_ref="work:worker:self", action="worker.heartbeat", payload=heartbeat_payload
            )
        ), registration

    def _listener_sessions(self) -> list[dict[str, Any]]:
        """This session's listener row, carrying the runtime's own limit state (W26)
        and the model and effort it runs with (W327)."""

        listener = self.field.worker_listener_session(self.config.worker_name)
        if listener is None:
            return []
        recorded_model = self.field.runtime_model(self.config.worker_name)
        row = session_with_runtime_model(
            session_with_limit_state(
                listener,
                runtime_kind=self.config.runtime_kind,
                runtime_session_id=self.config.runtime_session_id,
                now=utc_now(),
                recorded=self.field.runtime_limit_state(self.config.worker_name),
            ),
            runtime_kind=self.config.runtime_kind,
            runtime_session_id=self.config.runtime_session_id,
            recorded=recorded_model,
        )
        # Codex: what the rollout said is the last known value, so a later
        # read that misses keeps it. Written only when it changes.
        model = row.get("runtime_model")
        if model and str(self.config.runtime_kind or "").lower() == "codex":
            try:
                self.field.record_runtime_model(self.config.worker_name, model)
            except DomainError:
                pass
        return [row]

    async def poll_once(self) -> dict[str, Any]:
        cycle = self._trace.start_cycle()
        outcome = "succeeded"
        try:
            return await self._poll_once_body()
        except BaseException as exc:
            outcome = (
                "cancelled"
                if isinstance(exc, asyncio.CancelledError)
                else f"failed:{type(exc).__name__}"
            )
            raise
        finally:
            self._trace.finish_cycle(cycle, outcome=outcome)

    async def _poll_once_body(self) -> dict[str, Any]:
        with self._trace_stage(
            "channel.registration",
            operation="worker.publish",
        ):
            registration = await self._ensure_registration()
        if registration is not None and registration["remote"].get("pool_status") == "limbo":
            return await self._limbo_result()
        agent_sessions = self._listener_sessions()
        with self._trace_stage(
            "project.poll",
            operation="project.reconcile",
        ):
            return await self._poll_project_once(
                agent_sessions=agent_sessions,
                force_heartbeat=True,
            )

    async def poll_attendances_once(self) -> dict[str, Any]:
        """Discover and poll the current project of this session-bound worker."""

        registration = await self._ensure_registration(
            reconcile_ceiling_seconds=self._published_interval
        )
        if registration is not None and registration["remote"].get("pool_status") == "limbo":
            return await self._limbo_result()
        sessions = self._listener_sessions()
        discovery: dict[str, Any] = {}
        discovery_heartbeat_sent = False
        discovery_session_delta: list[dict[str, Any]] | None = None
        if (
            not self._attendance_cache.get("initialized")
            or not self._attendance_cache.get("items")
        ):
            # Discovery is the bootstrap and relink path. Once a project is
            # known, its heartbeat refreshes the same authoritative snapshot.
            # An unrelated Data Bus push still starts a control reconciliation,
            # but it does not make unchanged discovery presence due.
            discovery_session_delta, session_signature = self._session_report_delta(
                project_ref="",
                sessions=sessions,
            )
            worker_info, info_pending = self._worker_info()
            discovery_heartbeat_sent = (
                not self._attendance_cache.get("initialized")
                or discovery_session_delta is not None
                or info_pending
                or self._discovery_heartbeat_wait(sessions) <= 0
            )
            if discovery_heartbeat_sent:
                heartbeat_payload: dict[str, Any] = {"availability": "available"}
                if discovery_session_delta is not None:
                    heartbeat_payload["agent_sessions"] = discovery_session_delta
                self._add_worker_info(heartbeat_payload, worker_info)
                with self._trace_stage(
                    "attendance.heartbeat",
                    operation="worker.heartbeat.discovery",
                ):
                    discovery, republished = await self._heartbeat_with_republish(
                        heartbeat_payload
                    )
                if (
                    republished is not None
                    and republished["remote"].get("pool_status") == "limbo"
                ):
                    return await self._limbo_result()
                self._record_project_heartbeat("")
                self._record_session_report(
                    project_ref="",
                    signature=session_signature,
                )
                self._acknowledge_worker_info(worker_info, discovery)
                self._record_attendance_observation(discovery)
        with self._trace_stage(
            "attendance.controls",
            operation="control.pull.discovery",
        ):
            direct_controls = await self._pull_controls(project_ref="")
        attendances = [
            dict(item)
            for item in self._attendance_cache.get("items") or []
            if isinstance(item, Mapping) and item.get("project_ref")
        ]
        projects: list[dict[str, Any]] = []
        for attendance in attendances:
            parsed = parse_ref(str(attendance["project_ref"]))
            if parsed.kind != "project":
                continue
            adapter = ProblemBoardHostRelayAdapter(
                config=RelayConfig(
                    **{
                        **self.config.__dict__,
                        "project_id": parsed.object_id,
                    }
                ),
                field=self.field,
                client=self.client,
                session_report_signatures=self._session_report_signatures,
                attendance_cache=self._attendance_cache,
                heartbeat_sent_at=self._heartbeat_sent_at,
                monotonic=self._monotonic,
                trace=self._trace,
                runtime_account_reader=self._runtime_account_reader,
                runtime_account_error_state=self._runtime_account_error_state,
                outbox_drain_lock=self._outbox_drain_lock,
            )
            project = await adapter._poll_project_once(agent_sessions=sessions)
            if project.get("attendance") == "linked":
                projects.append(project)
        observed_attendances = [
            dict(item)
            for item in self._attendance_cache.get("items") or []
            if isinstance(item, Mapping) and item.get("project_ref")
        ]
        self.field.sync_worker_attendances(
            self.config.worker_name,
            [str(item["project_ref"]) for item in observed_attendances],
        )
        worker_alias = str(
            self._attendance_cache.get("worker_alias")
            or discovery.get("worker_alias")
            or self.config.worker_alias
        )
        host_retirements = list(
            self._attendance_cache.get("host_retirements") or []
        )
        if not projects:
            outbox = await self._flush_outbox()
            # An authoritative empty snapshot is idle. A stale cached project
            # that was refused is rediscovered at the active cadence.
            advertised_interval = (
                self.config.idle_reconcile_ceiling_seconds
                if self._attendance_cache.get("initialized")
                else self._active_poll_interval(sessions)
            )
            next_poll_seconds = min(
                advertised_interval,
                max(1, math.ceil(self._discovery_heartbeat_wait(sessions))),
            )
            await self._ensure_registration(
                reconcile_ceiling_seconds=advertised_interval
            )
            return {
                "worker_name": self.config.worker_name,
                "worker_alias": worker_alias,
                "pool_status": "active",
                "host_retirements": host_retirements,
                "attendance": "waiting_for_link",
                "projects": [],
                "direct": direct_controls,
                **outbox,
                "agent_sessions_reported": (
                    len(sessions)
                    if discovery_heartbeat_sent
                    and discovery_session_delta is not None
                    else 0
                ),
                "next_poll_seconds": next_poll_seconds,
            }
        await self._ensure_registration(reconcile_ceiling_seconds=self.config.reconcile_ceiling_seconds)
        heartbeat_delays = [
            int(project["next_heartbeat_seconds"])
            for project in projects
            if isinstance(project.get("next_heartbeat_seconds"), (int, float))
            and not isinstance(project.get("next_heartbeat_seconds"), bool)
            and project["next_heartbeat_seconds"] > 0
        ]
        return {
            "worker_name": self.config.worker_name,
            "worker_alias": worker_alias,
            "pool_status": "active",
            "host_retirements": host_retirements,
            "attendance": "linked",
            "projects": projects,
            "direct": direct_controls,
            "agent_sessions_reported": sum(
                int(project.get("agent_sessions_reported") or 0)
                for project in projects
            ),
            "next_poll_seconds": (
                min(heartbeat_delays)
                if heartbeat_delays
                else self._active_poll_interval(sessions)
            ),
        }


# A wake whose outcome the relay does not yet know. Only while one of these
# stands can a second submission hand the session two turns.
WAKE_IN_DOUBT_STATES = frozenset({"attempting", "queued", "consumed"})


def wake_withheld_by_reconciliation(
    queue_reconciliation: Mapping[str, Any] | None,
    subscription: Mapping[str, Any],
) -> str:
    """Why a fresh wake waits on a failed queue listing, or '' when it does not.

    The listing exists to tell a new submission from a duplicate of an earlier
    one whose acceptance timed out. That risk is real only while an earlier
    wake is in doubt (attempting, queued, consumed, or any outstanding wake
    id). With no such wake a failed listing says nothing about duplicates, and
    waiting on it left Codex sessions without a wake for minutes on 2026-09-23
    while nothing was logged. The wake goes out, and the failed listing is
    logged beside it.
    """

    if queue_reconciliation is None or queue_reconciliation.get("reconciled"):
        return ""
    if str(subscription.get("outstanding_wake_id") or ""):
        return "queue_reconciliation_failed_with_outstanding_wake"
    if str(subscription.get("wake_delivery_state") or "") in WAKE_IN_DOUBT_STATES:
        return "queue_reconciliation_failed_with_wake_in_doubt"
    return ""


def _reconciliation_detail(queue_reconciliation: Mapping[str, Any] | None) -> str:
    if not queue_reconciliation:
        return ""
    return str(
        queue_reconciliation.get("reason")
        or queue_reconciliation.get("detail")
        or queue_reconciliation.get("state")
        or ""
    )[:200]


TRANSIENT_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

# Refusals about the card itself, which no amount of retrying can fix. A card
# that has been revoked will not come back by being asked again, so treating
# these as transient keeps a dead channel looking healthy instead of surfacing
# it for re-consent.
#
# Scope matters here and it is easy to get wrong. These are failures of the
# credential, met when the relay proves its card and opens its stream. A
# refusal of one operation inside a working stream is a different thing: the
# card is fine, that single call is not permitted, and the channel must keep
# running. `work_worker_stream_grant_required` is exactly that per-operation
# case and deliberately does not appear below, because a worker missing the
# journal capability must not be treated as a worker with no card.
PERMANENT_ERROR_CODES = frozenset(
    {
        "delegated_capability_not_granted",
        "delegated_card_revoked",
        "delegated_card_not_found",
        # The Data Bus refused the bearer and the token endpoint refused to
        # mint another: the card's own answer, after one refresh
        # (relay_admission). A refused bearer alone is not here, because a
        # session lost server-side comes back with that refresh.
        "delegated_card_refresh_refused",
        "work_worker_card_required",
    }
)
TRANSIENT_ERROR_CODES = frozenset(
    {
        "data_bus_outcome_unknown",
        "oauth_metadata_request_failed",
        "oauth_profile_lock_timeout",
        "oauth_server_metadata_unavailable",
        "oauth_session_lock_timeout",
        "state_lock_timeout",
        "work_relay_stream_expired",
        "work_mcp_tool_failed",
        "work_relay_transport_unavailable",
    }
)


def transient_failure(error: BaseException) -> bool:
    """Default classification of a channel failure the next cycle may recover from.

    Rate limiting, gateway pressure, and dropped connections are the failures a
    relay meets between two perfectly healthy cycles. Treating them as fatal
    made the whole machine relay exit on one 429 and left every enrolled worker
    without a heartbeat until a supervisor restarted the process.
    """

    # A refusal may arrive as either a local DomainError or a Connection Hub
    # management error. Classify its structured fields before its Python class:
    # the metadata outage that motivated this guard carried the right code but
    # did not inherit any of the relay's previously recognized error classes.
    if isinstance(error, RelayStageError):
        return error.retryable
    code_text = str(getattr(error, "code", "") or "")
    detail_text = str(getattr(error, "details", "") or "")
    if any(name in code_text for name in PERMANENT_ERROR_CODES):
        return False
    if any(name in detail_text for name in PERMANENT_ERROR_CODES):
        return False
    if (
        "'retryable': False" in code_text
        or '"retryable": false' in code_text.lower()
    ):
        return False
    if (
        "'retryable': False" in detail_text
        or '"retryable": false' in detail_text.lower()
    ):
        return False
    if code_text in TRANSIENT_ERROR_CODES:
        return True
    try:
        status = int(getattr(error, "status", 0) or 0)
    except (TypeError, ValueError):
        status = 0
    if status in TRANSIENT_STATUSES:
        return True
    if isinstance(error, (ConnectionError, TimeoutError, OSError, asyncio.TimeoutError)):
        return True
    # python-socketio raises its own ConnectionError/TimeoutError classes that
    # do not subclass the builtins; a dropped Data Bus socket is as transient
    # as a dropped TCP connection and must not end the relay process.
    if str(type(error).__module__ or "").startswith("socketio"):
        return True
    return False


@dataclass
class _ChannelSession:
    """One worker's Data Bus connection kept open across relay cycles."""

    profile: str
    worker_name: str
    channel_identity: str
    replacement_epoch: int
    stack: AsyncExitStack
    adapter: ProblemBoardHostRelayAdapter
    card_fingerprint: str = ""
    closing: bool = False
    close_failure: Exception | None = None

    async def aclose(self) -> None:
        await self.stack.aclose()


class ProblemBoardRelaySupervisor:
    """One machine process multiplexing independently authorized workers."""

    adapter_id = "problem-board-host"
    def __init__(
        self,
        *,
        config_path: str | Path,
        connector: ChannelConnector,
        retryable: Callable[[BaseException], bool] | None = None,
        session_notifier: Callable[..., Mapping[str, Any]] | None = None,
        session_queue_reconciler: Callable[..., Mapping[str, Any]] | None = None,
        pacing: RelayPacing | None = None,
        trace: RelayActivityTrace | None = None,
    ) -> None:
        self.config_path = Path(config_path).expanduser().resolve()
        self.connector = connector
        # How often the gateway may be called, per host and per channel
        # (local/relay_pacing.py). Kept beside the host config.
        self._pacing = pacing or RelayPacing(
            self.config_path.parent / PACING_FILENAME, forget_permanent=True
        )
        self.retryable = retryable or transient_failure
        self.session_notifier = session_notifier or notify_agent_session
        self.session_queue_reconciler = (
            session_queue_reconciler or reconcile_agent_session_queue
        )
        self._trace = trace or RelayActivityTrace(log=logger)
        # Keyed by worker name. A session carries one live, Card-scoped Data
        # Bus connection and the adapter's registration state. The Card itself
        # opens the connection; terminal authority failure drops the session
        # and moves that exact worker back to pending authorization.
        self._sessions: dict[str, _ChannelSession] = {}
        # Data Bus generations belong to one client object. This monotonic
        # channel epoch preserves replacement order across those object-local
        # generation resets for the lifetime of the relay process.
        self._replacement_epochs: dict[str, int] = {}
        self._queue_reconciled_workers: set[str] = set()
        # W26: per worker, the reset time a deferred wake was last logged for,
        # so a limit is said once per reset and not once per cycle.
        self._limit_wake_deferrals: dict[str, str] = {}
        self._listener_signature_cache: dict[
            str, tuple[tuple[int, int, int] | None, tuple]
        ] = {}
        # A ready outbox signature wakes the ordinary cycle once. If pacing or
        # authorization leaves the same row pending, the next wait observes it
        # as the baseline instead of spinning until the channel is due.
        self._local_outbox_ready_signatures: dict[str, tuple] = {}
        self._expected_open_failure_signatures: dict[
            str, tuple[str, str, str]
        ] = {}
        # Coordinate requests are served beside the channel cycle, so a slow
        # channel or a reload cannot hold every worker's pb coordinate.
        self._coordinate_task: asyncio.Task | None = None
        self._coordinate_draining: dict[str, asyncio.Task] = {}
        # Local-state housekeeping (legacy cleanup, retention) costs time in
        # proportion to history, so it runs in a thread beside the cycle, never
        # inside it (W287, rule LS5 in storage-and-retention.md).
        self._maintenance_task: asyncio.Task | None = None
        # One drain per worker at a time, whichever path starts it. The
        # queue's claim is exclusive per request and released before the
        # request runs, so without this a side drain executing an earlier
        # request and a cycle drain claiming a later one overlap, and the
        # later operation can run first.
        self._coordinate_drain_locks: dict[str, asyncio.Lock] = {}
        # The cycle and the outbox side server share one lock per worker. The
        # filesystem claim is also exclusive, but this keeps one Card channel
        # from carrying concurrent sends in either restart order.
        self._outbox_drain_locks: dict[str, asyncio.Lock] = {}
        self._outbox_server = RelayOutboxDrainServer(
            config_path=self.config_path,
            pacing=self._pacing,
            session_for=lambda worker_name: self._sessions.get(worker_name),
            session_matches=lambda host, channel, session: self._session_matches(
                host,
                channel,
                session,
                require_card=True,
            ),
            drain_lock=self._outbox_drain_lock,
            log=logger,
        )
        # Kept as one shared view for channel shutdown and focused relay tests.
        self._outbox_draining = self._outbox_server.draining

    def _is_retryable(self, error: BaseException) -> bool:
        if (
            isinstance(error, DomainError)
            and error.code == "work_relay_channel_stop_failed"
        ):
            # The context-manager exit has already been consumed, so this
            # process cannot prove the superseded client's tasks are gone.
            # Exit and let the service supervisor replace the whole process.
            return False
        return (
            error.retryable
            if isinstance(error, RelayStageError)
            else self.retryable(error)
        )

    async def _notify_session(
        self,
        host: HostRelayConfig,
        channel: WorkerChannelConfig,
        *,
        event_kind: str,
        message_refs: Sequence[str] = (),
        wake_id: str = "",
        retried: bool = False,
    ) -> dict[str, Any]:
        if event_kind != "input.available":
            return {
                "adapter": "none",
                "state": "status_only",
                "event_kind": event_kind,
                "delivered": False,
                "reason": "status_event_does_not_wake_model",
            }
        field = SharedFieldStore(host.field_root)
        try:
            listener = field.worker_listener_session(channel.worker_name)
        except DomainError as exc:
            if exc.code != "field_record_not_found":
                raise
            listener = None
        if not listener or listener.get("state") == "detached":
            return {
                "adapter": "none",
                "state": "session_not_listening",
                "event_kind": event_kind,
                "delivered": False,
                "reason": "field_worker_not_listening",
            }
        delivery_wake_id = wake_id or new_id("wake")
        try:
            prepared_session = field.prepare_worker_session_wake(
                channel.worker_name,
                message_refs=message_refs,
                wake_id=delivery_wake_id,
                retry=retried,
                wake_origin={
                    "host_id": host.host_id,
                    "relay_id": host.relay_id,
                    "process_id": str(os.getpid()),
                },
            )
        except DomainError as exc:
            if exc.code == "field_worker_not_listening":
                return {
                    "adapter": "none",
                    "state": "session_not_listening",
                    "event_kind": event_kind,
                    "delivered": False,
                    "reason": exc.code,
                    "wake_id": delivery_wake_id,
                }
            if exc.code in {
                "field_worker_wake_already_acknowledged",
                "field_worker_wake_conflict",
            }:
                listener_subscription = (
                    dict(listener.get("subscription") or {})
                    if isinstance(listener.get("subscription"), Mapping)
                    else {}
                )
                return {
                    "adapter": str(listener_subscription.get("adapter") or "unknown"),
                    "state": "wake_not_queued",
                    "event_kind": event_kind,
                    "delivered": False,
                    "deduplicated": True,
                    "reason": exc.code,
                    "wake_id": delivery_wake_id,
                }
            raise
        prepared_subscription = dict(prepared_session.get("subscription") or {})
        wake_provenance = dict(
            prepared_subscription.get("wake_provenance") or {}
        )
        result = dict(
            await asyncio.to_thread(
                self.session_notifier,
                channel,
                event_kind=event_kind,
                wake_id=delivery_wake_id,
                wake_provenance=wake_provenance,
            )
        )
        result.setdefault("event_kind", "queue.reconcile")
        result.setdefault("delivered", False)
        # A pushed wake leaves a line. A Claude Code channel records an attempt
        # every cycle by design (its session-owned watch is the path), so that
        # case stays at debug and a Codex push or any delivery is information.
        pushed_log = (
            logger.info
            if str(result.get("adapter") or "") == "codex-queue" or bool(result.get("delivered"))
            else logger.debug
        )
        pushed_log(
            "Problem Board wake pushed worker=%s wake_id=%s adapter=%s state=%s "
            "delivered=%s reason=%s submission=%s retried=%s pending=%d",
            channel.worker_name,
            delivery_wake_id,
            str(result.get("adapter") or "unknown"),
            str(result.get("state") or "unknown"),
            bool(result.get("delivered")),
            str(result.get("reason") or ""),
            str(result.get("queued_submission_id") or ""),
            retried,
            len(message_refs),
        )
        try:
            field.record_worker_session_delivery(
                channel.worker_name,
                adapter=str(result.get("adapter") or "unknown"),
                state=str(result.get("state") or "unknown"),
                event_kind=event_kind,
                delivered=bool(result.get("delivered")),
                reason=str(result.get("reason") or ""),
                message_refs=message_refs,
                wake_id=delivery_wake_id,
                prepared=True,
                queued_submission_id=str(
                    result.get("queued_submission_id") or ""
                ),
            )
        except DomainError as exc:
            if exc.code != "field_worker_not_listening":
                raise
            result["state"] = "session_not_listening"
            result["delivered"] = False
            result["reason"] = exc.code
        result["wake_id"] = delivery_wake_id
        result["wake_provenance"] = wake_provenance
        if retried:
            result["retried"] = True
        return result

    async def _reconcile_session_queue(
        self,
        host: HostRelayConfig,
        channel: WorkerChannelConfig,
        listener: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        subscription = (
            dict(listener.get("subscription") or {})
            if isinstance(listener.get("subscription"), Mapping)
            else {}
        )
        if str(subscription.get("adapter") or "") != "codex-queue":
            return None
        required = bool(subscription.get("queue_reconciliation_required"))
        state = str(subscription.get("wake_delivery_state") or "")
        # A queued or consumed wake is re-read once its deadline has passed,
        # not every cycle: the listing is a subprocess against the native
        # queue, and the deadline is the bounded cadence the store renews.
        # Without this a queued wake could never change state by itself.
        uncertain = state in {"attempting", "failed"} or (
            state in {"queued", "consumed"}
            and _deadline_passed(subscription.get("wake_ack_deadline_at"))
        )
        if state == "queued" and not uncertain:
            # A queued report falls due a fixed time after the first expired
            # deadline, and the backoff may have left the last listing well
            # before that. The notice claims the queue still holds the wake,
            # so it is derived only from a listing taken after it fell due:
            # force that listing here when the last one predates the due
            # point.
            overdue_since = str(subscription.get("wake_queued_overdue_since") or "")
            confirmed_at = str(subscription.get("wake_queued_confirmed_at") or "")
            if overdue_since and _seconds_since(overdue_since) >= WAKE_OVERDUE_GRACE_SECONDS:
                due_age = _seconds_since(overdue_since) - WAKE_OVERDUE_GRACE_SECONDS
                if not confirmed_at or _seconds_since(confirmed_at) > due_age:
                    uncertain = True
        if (
            channel.worker_name in self._queue_reconciled_workers
            and not required
            and not uncertain
        ):
            return None
        expected_wake_id = str(subscription.get("outstanding_wake_id") or "")
        result = dict(
            await asyncio.to_thread(
                self.session_queue_reconciler,
                channel,
                expected_wake_id=expected_wake_id,
                expected_submission_ids=list(
                    subscription.get("wake_queued_submission_ids") or []
                ),
            )
        )
        try:
            field = SharedFieldStore(host.field_root)
            listener = field.record_worker_session_queue_reconciliation(
                channel.worker_name,
                expected_wake_id=expected_wake_id,
                result=result,
            )
        except DomainError as exc:
            if exc.code != "field_worker_not_listening":
                raise
            return result
        current_wake_id = str(
            (listener.get("subscription") or {}).get("outstanding_wake_id") or ""
        )
        if result.get("reconciled") and current_wake_id == expected_wake_id:
            self._queue_reconciled_workers.add(channel.worker_name)
        else:
            self._queue_reconciled_workers.discard(channel.worker_name)
        return result

    async def _reconcile_queues_before_channels(
        self, host: HostRelayConfig, channels: Sequence[WorkerChannelConfig]
    ) -> None:
        field = SharedFieldStore(host.field_root)

        async def reconcile(channel: WorkerChannelConfig) -> None:
            try:
                listener = field.worker_listener_session(channel.worker_name)
                if not listener or listener.get("state") == "detached":
                    return
                result = await self._reconcile_session_queue(host, channel, listener)
                if result is not None and not result.get("reconciled"):
                    logger.warning(
                        "Problem Board native queue preflight incomplete worker=%s "
                        "state=%s reason=%s",
                        channel.worker_name,
                        result.get("state"),
                        result.get("reason"),
                    )
            except DomainError as exc:
                if exc.code != "field_record_not_found":
                    logger.warning(
                        "Problem Board native queue preflight failed worker=%s "
                        "error_code=%s message=%s",
                        channel.worker_name,
                        exc.code,
                        failure_message(exc),
                    )
            except Exception as exc:  # noqa: BLE001 - retry after channel work
                logger.warning(
                    "Problem Board native queue preflight failed worker=%s "
                    "error_type=%s message=%s",
                    channel.worker_name,
                    type(exc).__name__,
                    failure_message(exc),
                )

        await asyncio.gather(*(reconcile(channel) for channel in channels))

    async def _notify_available_input(
        self, host: HostRelayConfig, channel: WorkerChannelConfig
    ) -> dict[str, Any] | None:
        field = SharedFieldStore(host.field_root)
        try:
            listener = field.worker_listener_session(channel.worker_name)
        except DomainError as exc:
            if exc.code != "field_record_not_found":
                raise
            return None
        if not listener or listener.get("state") == "detached":
            return None
        queue_reconciliation = await self._reconcile_session_queue(
            host, channel, listener
        )
        try:
            pending_refs = field.pending_worker_mail_refs(channel.worker_name)
            listener = field.worker_listener_session(channel.worker_name)
        except DomainError as exc:
            if exc.code != "field_record_not_found":
                raise
            return queue_reconciliation
        if not pending_refs or not listener or listener.get("state") == "detached":
            return queue_reconciliation
        # W26: a wake to an agent the runtime says is out of tokens or rate
        # limited only piles up turns it cannot take. It waits for the reset
        # the runtime named, said once per reset in the log, and the mail
        # stays pending for the wake that follows the reset.
        now = utc_now()
        deferred_until = wake_deferred_until(
            session_with_limit_state(
                listener,
                runtime_kind=channel.runtime_kind,
                runtime_session_id=channel.runtime_session_id,
                now=now,
                recorded=field.runtime_limit_state(channel.worker_name),
            ).get("limit_state"),
            now=now,
        )
        if deferred_until:
            if self._limit_wake_deferrals.get(channel.worker_name) != deferred_until:
                self._limit_wake_deferrals[channel.worker_name] = deferred_until
                logger.warning(
                    "Problem Board wake deferred worker=%s reason=agent_rate_limited "
                    "until=%s pending=%d",
                    channel.worker_name,
                    deferred_until,
                    len(pending_refs),
                )
            return {
                **(queue_reconciliation or {}),
                "wake_deferred": True,
                "wake_deferred_until": deferred_until,
                "reason": "agent_rate_limited",
            }
        self._limit_wake_deferrals.pop(channel.worker_name, None)
        subscription = (
            dict(listener.get("subscription") or {})
            if isinstance(listener.get("subscription"), Mapping)
            else {}
        )
        withheld = wake_withheld_by_reconciliation(queue_reconciliation, subscription)
        if withheld:
            # Every withheld wake is said out loud. On 2026-09-23 Codex
            # sessions sat without a wake for minutes and the log had no line
            # about it, so the cause could only be read from the queue database.
            logger.warning(
                "Problem Board wake withheld worker=%s reason=%s detail=%s "
                "prior_wake=%s prior_state=%s pending=%d",
                channel.worker_name,
                withheld,
                _reconciliation_detail(queue_reconciliation),
                str(subscription.get("outstanding_wake_id") or ""),
                str(subscription.get("wake_delivery_state") or ""),
                len(pending_refs),
            )
            return queue_reconciliation
        if queue_reconciliation is not None and not queue_reconciliation.get("reconciled"):
            logger.warning(
                "Problem Board wake proceeds although the queue listing failed "
                "worker=%s detail=%s: no earlier wake is in doubt, so a duplicate "
                "is not possible",
                channel.worker_name,
                _reconciliation_detail(queue_reconciliation),
            )
        outstanding_wake_id = str(
            subscription.get("outstanding_wake_id") or ""
        )
        if outstanding_wake_id:
            coalesced = field.coalesce_worker_session_wake(
                channel.worker_name,
                message_refs=pending_refs,
                wake_id=outstanding_wake_id,
            )
            if coalesced is None:
                listener = field.worker_listener_session(channel.worker_name) or {}
                subscription = (
                    dict(listener.get("subscription") or {})
                    if isinstance(listener.get("subscription"), Mapping)
                    else {}
                )
                outstanding_wake_id = str(
                    subscription.get("outstanding_wake_id") or ""
                )
                if not outstanding_wake_id:
                    return await self._notify_session(
                        host,
                        channel,
                        event_kind="input.available",
                        message_refs=pending_refs,
                    )
                coalesced = field.coalesce_worker_session_wake(
                    channel.worker_name,
                    message_refs=pending_refs,
                    wake_id=outstanding_wake_id,
                )
            if coalesced is not None:
                subscription = dict(coalesced.get("subscription") or {})
            # An outstanding wake in any state waits for its deadline. Past it,
            # the relay asks the store to prepare a retry, and the store decides
            # by state: a failed submission is retried, a consumed one once, a
            # queued one never, since the native queue still holds the first
            # and a second would hand the session two turns. A refusal
            # comes back as wake_not_queued with reconciliation requested, so
            # the queue is re-read instead. Until 2026-09-19 a "delivered"
            # state returned here for every later message and the record
            # never moved again.
            deadline = str(subscription.get("wake_ack_deadline_at") or "")
            if _deadline_passed(deadline):
                return await self._notify_session(
                    host,
                    channel,
                    event_kind="input.available",
                    message_refs=pending_refs,
                    wake_id=outstanding_wake_id,
                    retried=True,
                )
            wake_state = str(subscription.get("wake_delivery_state") or "")
            logger.info(
                "Problem Board wake deduplicated worker=%s wake_id=%s state=%s "
                "retry_at=%s pending=%d",
                channel.worker_name,
                outstanding_wake_id,
                wake_state,
                deadline,
                len(pending_refs),
            )
            return {
                "adapter": str(subscription.get("adapter") or "unknown"),
                "state": str(subscription.get("state") or "unknown"),
                "event_kind": "input.available",
                "delivered": wake_state == "queued",
                "wake_state": wake_state,
                "deduplicated": True,
                "wake_id": outstanding_wake_id,
                "retry_at": deadline,
            }
        return await self._notify_session(
            host,
            channel,
            event_kind="input.available",
            message_refs=pending_refs,
        )

    @staticmethod
    def _record_authorization(
        host: HostRelayConfig,
        channel: WorkerChannelConfig,
        observation: Mapping[str, Any],
    ) -> None:
        try:
            SharedFieldStore(host.field_root).record_worker_authorization(
                channel.worker_name,
                state=str(observation.get("state") or "connection_unavailable"),
                reason=str(observation.get("error_code") or ""),
                action=str(observation.get("action") or "retry"),
                control_plane_state="pending_authorization",
            )
        except DomainError as exc:
            if exc.code != "field_record_not_found":
                raise

    @staticmethod
    def _profile_entry(
        host: HostRelayConfig, channel: WorkerChannelConfig
    ) -> Mapping[str, Any] | None:
        """The channel's local profile record, read without any credential."""

        root = host.connection_hub_state_root
        if root is None:
            return None
        try:
            data = json.loads((root / "profiles.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        profiles = data.get("profiles") if isinstance(data, Mapping) else None
        entries = profiles.values() if isinstance(profiles, Mapping) else (profiles or [])
        for entry in entries:
            if isinstance(entry, Mapping) and str(entry.get("name") or "") == channel.profile:
                return entry
        return None

    @classmethod
    def _profile_fingerprint(
        cls, host: HostRelayConfig, channel: WorkerChannelConfig
    ) -> str:
        """Non-secret identity of the channel's local profile record.

        ``pb worker authorize`` rewrites the record (a new ``updated_at``, or a
        new profile), so a change here is the signal that a pending channel is
        worth one more gateway call.
        """

        entry = cls._profile_entry(host, channel)
        if entry is None:
            return ""
        return "|".join(
            str(entry.get(key) or "")
            for key in ("access_id", "credential_ref", "updated_at", "record_version")
        )

    @classmethod
    def _card_fingerprint(
        cls, host: HostRelayConfig, channel: WorkerChannelConfig
    ) -> str:
        """Non-secret identity of the Card the channel's profile is bound to.

        A token refresh rewrites ``updated_at`` for the same Card, so this
        leaves it out: only a different Card, credential slot, endpoint or
        authentication kind means an open session belongs to another Card.
        """

        entry = cls._profile_entry(host, channel)
        if entry is None:
            return ""
        return "|".join(
            str(entry.get(key) or "")
            for key in ("access_id", "credential_ref", "endpoint", "auth_type")
        )

    def _session_matches(
        self,
        host: HostRelayConfig,
        channel: WorkerChannelConfig,
        session: _ChannelSession,
        *,
        require_card: bool,
    ) -> bool:
        """Whether ``session`` is the one opened for this channel and Card.

        Both paths that drain coordinate requests ask this: the
        cycle before it reuses a cached session, the side server before it
        starts a drain. A different profile, native channel identity or bound
        Card means the session belongs to what was replaced.

        ``require_card`` fails closed on an unknown Card: the side server
        carries nothing unless both the Card at open and the Card now were
        read and agree, so an unreadable profile leaves the request to the
        cycle. The cycle reopens on any change, including a Card that became
        unreadable, and keeps a session only when neither read found a Card,
        which is a host without a local profile store, where the open had
        nothing to bind to either.
        """

        if (
            session.closing
            or session.profile != channel.profile
            or session.channel_identity != channel.worker_identity
        ):
            return False
        current = self._card_fingerprint(host, channel)
        if require_card:
            return bool(session.card_fingerprint) and session.card_fingerprint == current
        return session.card_fingerprint == current

    def _record_channel_failure(
        self, pacing: RelayPacing, worker_name: str, error: BaseException
    ) -> float:
        """Back a failed channel off: quickly when only the handshake timed out
        or the runtime is not there, with the doubling otherwise."""

        handshake = is_namespace_handshake_timeout(error)
        return pacing.record_failure(
            worker_name,
            HANDSHAKE_TIMEOUT_REASON if handshake else self._failure_code(error),
            handshake_timeout=handshake,
            runtime_unavailable=not handshake and is_runtime_unavailable(error),
            credential=credential_refused(error),
        )

    @staticmethod
    def _cycle_next_poll(
        next_poll_seconds: Sequence[int],
        failures: Sequence[BaseException],
        pacing: RelayPacing,
    ) -> int | None:
        """The seconds until the next cycle, or None for the base interval.

        The fastest attending channel sets the machine cycle. A failed channel
        keeps the base interval so its reconnect is not delayed. A channel
        waiting on the runtime pulls the cycle to its retry, whatever else
        failed, so a runtime that came back is seen within seconds.
        """

        candidates: list[int] = []
        if next_poll_seconds and not failures:
            candidates.append(min(next_poll_seconds))
        soonest = pacing.soonest_runtime_retry_seconds()
        if soonest is not None:
            candidates.append(max(1, math.ceil(soonest)))
        return min(candidates) if candidates else None

    @staticmethod
    def _failure_code(error: BaseException) -> str:
        code = str(getattr(error, "code", "") or "")
        if code:
            return code
        if is_descriptor_exhaustion(error):
            # A raw Errno 24 from a field-file read reaches this line
            # without passing through staged_failure.
            return "work_relay_descriptor_exhausted"
        return type(error).__name__

    @staticmethod
    def _failure_evidence(error: BaseException) -> dict[str, Any]:
        raw = getattr(error, "details", None)
        details = dict(raw) if isinstance(raw, Mapping) else {}
        evidence: dict[str, Any] = {}
        for key in (
            "method", "url", "server_reason", "failure_kind", "operation", "target",
            "transport_message_id", "transport_phase", "request_scope", "socket_id",
        ):
            value = str(details.get(key) or "").strip()
            if value:
                evidence[key] = value
        for key in ("ingress_accepted", "transport_replayed", "connection_active"):
            if isinstance(details.get(key), bool):
                evidence[key] = details[key]
        try:
            if details.get("connection_generation") is not None:
                evidence["connection_generation"] = max(
                    0, int(details["connection_generation"])
                )
        except (TypeError, ValueError):
            pass
        status = details.get("status") if isinstance(error, RelayStageError) else details.get(
            "status", getattr(error, "status", None)
        )
        try:
            if status is not None and str(status).strip():
                evidence["status"] = int(status)
        except (TypeError, ValueError):
            pass
        for key in ("elapsed_seconds", "timeout_seconds"):
            try:
                if details.get(key) is not None:
                    evidence[key] = round(max(0.0, float(details[key])), 3)
            except (TypeError, ValueError):
                pass
        return evidence

    @classmethod
    def _authorization_failure_observation(cls, error: BaseException) -> dict[str, Any]:
        observation = authorization_observation(error)
        observation["error_type"] = failure_type(error)
        observation["message"] = failure_message(error)
        request = cls._failure_evidence(error)
        if request:
            observation["request"] = request
        return observation

    @classmethod
    def _record_relay_channel_degraded(
        cls,
        host: HostRelayConfig,
        channel: WorkerChannelConfig,
        error: BaseException,
    ) -> None:
        try:
            SharedFieldStore(host.field_root).record_relay_channel_degraded(
                channel.worker_name,
                code=cls._failure_code(error),
                message=failure_message(error),
                request=cls._failure_evidence(error),
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "Could not persist the degraded relay channel worker=%s error_code=%s",
                channel.worker_name,
                cls._failure_code(error),
                exc_info=True,
            )

    async def _record_relay_channel_recovered(
        self,
        host: HostRelayConfig,
        channel: WorkerChannelConfig,
    ) -> None:
        field = SharedFieldStore(host.field_root)
        try:
            field.record_relay_channel_recovered(channel.worker_name)
            pending = field.pending_relay_degraded_intervals(channel.worker_name)
        except Exception:  # noqa: BLE001
            logger.warning(
                "Could not close the degraded relay channel worker=%s",
                channel.worker_name,
                exc_info=True,
            )
            return
        if not pending:
            return
        session = self._sessions.get(channel.worker_name)
        if session is None:
            return
        try:
            published = await session.adapter.publish_relay_degraded_intervals(pending)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Recovered relay channel could not publish its prior degraded interval "
                "worker=%s error_code=%s",
                channel.worker_name,
                self._failure_code(exc),
            )
            return
        accepted = published.get("remote", {}).get("relay_degraded_intervals")
        if not isinstance(accepted, list):
            logger.info(
                "Recovered relay channel offered prior degraded intervals, but the "
                "control plane has not acknowledged that field worker=%s",
                channel.worker_name,
            )
            return
        field.mark_relay_degraded_intervals_published(
            channel.worker_name,
            [item for item in accepted if isinstance(item, Mapping)],
        )

    async def _open_session(
        self, host: HostRelayConfig, channel: WorkerChannelConfig
    ) -> _ChannelSession:
        replacement_epoch = self._replacement_epochs.get(channel.worker_name, 0) + 1
        self._replacement_epochs[channel.worker_name] = replacement_epoch
        logger.info(
            "Problem Board relay channel lifecycle event=opening worker_name=%s "
            "channel_identity=%s replacement_epoch=%d",
            channel.worker_name,
            channel.worker_identity,
            replacement_epoch,
        )
        # Read before connecting: a Card replaced while the connection opens
        # leaves this session with the old identity, so it is refused beside
        # the cycle rather than trusted.
        card_fingerprint = self._card_fingerprint(host, channel)
        stack = AsyncExitStack()
        try:
            client = await stack.enter_async_context(
                self.connector(
                    host,
                    channel,
                    replacement_epoch=replacement_epoch,
                )
            )
            adapter = ProblemBoardHostRelayAdapter(
                config=RelayConfig.from_host_channel(
                    host, channel, project_id="attendance"
                ),
                field=SharedFieldStore(host.field_root),
                client=client,
                trace=self._trace,
                runtime_account_reader=lambda: read_runtime_account(
                    channel.runtime_kind
                ),
                outbox_drain_lock=self._outbox_drain_lock(
                    channel.worker_name
                ),
            )
        except BaseException as exc:
            await stack.aclose()
            if isinstance(exc, Exception):
                error_type = failure_type(exc)
                error_code = self._failure_code(exc)
                message = failure_message(exc)
                expected_profile_absence = (
                    self._authorization_failure_observation(exc).get("state")
                    == PROFILE_METADATA_ABSENT
                )
                signature = (error_type, error_code, message)
                if (
                    not expected_profile_absence
                    or self._expected_open_failure_signatures.get(
                        channel.worker_name
                    )
                    != signature
                ):
                    logger.warning(
                        "Problem Board relay channel lifecycle event=open_failed "
                        "worker_name=%s channel_identity=%s replacement_epoch=%d "
                        "error_type=%s error_code=%s message=%s",
                        channel.worker_name,
                        channel.worker_identity,
                        replacement_epoch,
                        error_type,
                        error_code,
                        message,
                        exc_info=not expected_profile_absence,
                    )
                if expected_profile_absence:
                    self._expected_open_failure_signatures[
                        channel.worker_name
                    ] = signature
            else:
                logger.warning(
                    "Problem Board relay channel lifecycle event=open_failed "
                    "worker_name=%s channel_identity=%s replacement_epoch=%d",
                    channel.worker_name,
                    channel.worker_identity,
                    replacement_epoch,
                    exc_info=True,
                )
            raise
        self._expected_open_failure_signatures.pop(channel.worker_name, None)
        session = _ChannelSession(
            profile=channel.profile,
            worker_name=channel.worker_name,
            channel_identity=channel.worker_identity,
            replacement_epoch=replacement_epoch,
            stack=stack,
            adapter=adapter,
            card_fingerprint=card_fingerprint,
        )
        logger.info(
            "Problem Board relay channel lifecycle event=opened worker_name=%s "
            "channel_identity=%s replacement_epoch=%d",
            session.worker_name,
            session.channel_identity,
            session.replacement_epoch,
        )
        # An open channel has no failure on record. The pacing record is
        # otherwise removed only by the cycle's record_success after a
        # completed poll, and until then pb status and the pb coordinate
        # admission read it as "not open now" and refuse a channel that is
        # open (W274: three minutes seven seconds after a relay restart on
        # 2026-09-22, while the first cycle came round).
        self._pacing.record_success(channel.worker_name)
        return session

    async def _drop_session(self, worker_name: str) -> None:
        session = self._sessions.get(worker_name)
        if session is None:
            return
        if session.close_failure is not None:
            logger.error(
                "Problem Board relay channel lifecycle event=stop_blocked "
                "worker_name=%s channel_identity=%s replacement_epoch=%d "
                "error_code=%s",
                session.worker_name,
                session.channel_identity,
                session.replacement_epoch,
                self._failure_code(session.close_failure),
            )
            raise session.close_failure
        session.closing = True
        # A drain beside the cycle may be using this session's client. Closing
        # marks the session so no new drain starts; the running one finishes
        # its request and writes the response before the client closes.
        side_drains = [
            task
            for task in (
                self._coordinate_draining.get(worker_name),
                self._outbox_draining.get(worker_name),
            )
            if task is not None
            and not task.done()
            and task is not asyncio.current_task()
        ]
        if side_drains:
            await asyncio.gather(*side_drains, return_exceptions=True)
        logger.info(
            "Problem Board relay channel lifecycle event=stopping worker_name=%s "
            "channel_identity=%s replacement_epoch=%d",
            session.worker_name,
            session.channel_identity,
            session.replacement_epoch,
        )
        try:
            await session.aclose()
        except Exception as exc:
            logger.error(
                "Problem Board relay channel lifecycle event=stop_failed "
                "worker_name=%s channel_identity=%s replacement_epoch=%d",
                session.worker_name,
                session.channel_identity,
                session.replacement_epoch,
                exc_info=True,
            )
            session.close_failure = DomainError(
                "work_relay_channel_stop_failed",
                "The superseded worker channel did not stop; this relay process "
                "cannot open a replacement and must be replaced by its service "
                "supervisor.",
                status=500,
                details={
                    "worker_name": session.worker_name,
                    "channel_identity": session.channel_identity,
                    "replacement_epoch": session.replacement_epoch,
                    "error_type": failure_type(exc),
                },
            )
            raise session.close_failure from exc
        if self._sessions.get(worker_name) is session:
            self._sessions.pop(worker_name)
        logger.info(
            "Problem Board relay channel lifecycle event=stopped worker_name=%s "
            "channel_identity=%s replacement_epoch=%d",
            session.worker_name,
            session.channel_identity,
            session.replacement_epoch,
        )

    def _coordinate_drain_lock(self, worker_name: str) -> asyncio.Lock:
        lock = self._coordinate_drain_locks.get(worker_name)
        if lock is None:
            lock = self._coordinate_drain_locks[worker_name] = asyncio.Lock()
        return lock

    def _outbox_drain_lock(self, worker_name: str) -> asyncio.Lock:
        lock = self._outbox_drain_locks.get(worker_name)
        if lock is None:
            lock = self._outbox_drain_locks[worker_name] = asyncio.Lock()
        return lock

    async def _drain_coordinate_for_worker(
        self,
        host: HostRelayConfig,
        channel: WorkerChannelConfig,
        session: _ChannelSession,
    ) -> dict[str, int]:
        """Drain this worker's requests, one drain at a time across both paths.

        The channel cycle and the side server both end here. The cycle waits
        for a side drain in progress, so a later request is claimed only after
        the earlier one has run and answered, and the side server does not
        start while the cycle holds the lock (serve_coordinate_once checks it).
        """

        async with self._coordinate_drain_lock(channel.worker_name):
            return await self._drain_coordinate_requests(host, channel, session)

    async def _drain_coordinate_requests(
        self,
        host: HostRelayConfig,
        channel: WorkerChannelConfig,
        session: _ChannelSession,
    ) -> dict[str, int]:
        """Run bounded local requests through this exact worker Card channel."""

        queue = CoordinateQueue(host.field_root)
        requests = queue.claim(worker_name=channel.worker_name)
        counts = {
            "claimed": len(requests),
            "completed": 0,
            "deferred": 0,
            "refused": 0,
        }
        expected_identity = {
            "worker_name": channel.worker_name,
            "worker_identity": channel.worker_identity,
            "runtime_kind": channel.runtime_kind,
            "runtime_session_id": channel.runtime_session_id,
        }

        def log_stages(
            request: Mapping[str, Any],
            *,
            outcome: str,
            relay_started: float,
            governed_action_seconds: float,
            wait_context: Sequence[Mapping[str, Any]],
        ) -> None:
            queue_wait = _coordinate_queue_wait_seconds(request)
            queue_wait_text = (
                "unavailable" if queue_wait is None else f"{queue_wait:.3f}"
            )
            relay_total_seconds = time.monotonic() - relay_started
            if queue_wait is not None and queue_wait >= self._trace.slow_seconds:
                logger.warning(
                    "Problem Board coordinate slow wait worker=%s request_id=%s "
                    "operation=%s queue_wait_seconds=%.3f threshold_seconds=%.3f "
                    "waited_on=%s",
                    channel.worker_name,
                    str(request.get("request_id") or ""),
                    str(request.get("action") or ""),
                    queue_wait,
                    self._trace.slow_seconds,
                    json.dumps(
                        list(wait_context),
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                )
            logger.info(
                "Problem Board coordinate stages worker=%s request_id=%s "
                "operation=%s outcome=%s queue_wait_seconds=%s "
                "relay_local_seconds=%.3f governed_action_seconds=%.3f "
                "relay_total_seconds=%.3f "
                "transport_attempts=%d",
                channel.worker_name,
                str(request.get("request_id") or ""),
                str(request.get("action") or ""),
                outcome,
                queue_wait_text,
                max(0.0, relay_total_seconds - governed_action_seconds),
                governed_action_seconds,
                relay_total_seconds,
                int(request.get("transport_attempts") or 0),
            )

        for request in requests:
            relay_started = time.monotonic()
            governed_action_seconds = 0.0
            outcome = "refused"
            queue_window = _coordinate_queue_window(request)
            wait_context = (
                self._trace.wait_context(*queue_window)
                if queue_window is not None
                and queue_window[1] - queue_window[0]
                >= self._trace.slow_seconds
                else []
            )
            observed_identity = {
                key: str(request.get(key) or "") for key in expected_identity
            }
            if observed_identity != expected_identity:
                try:
                    queue.complete(
                        request,
                        error={
                            "code": "work_coordinate_worker_mismatch",
                            "message": (
                                "The relay request does not belong to this worker channel."
                            ),
                            "status": 409,
                            "details": {
                                "request_id": str(request.get("request_id") or ""),
                                "worker_name": channel.worker_name,
                            },
                        },
                    )
                except DomainError as exc:
                    if exc.code != COORDINATE_LEASE_LOST:
                        raise
                    continue
                counts["refused"] += 1
                outcome = "worker_mismatch"
                log_stages(
                    request,
                    outcome=outcome,
                    relay_started=relay_started,
                    governed_action_seconds=governed_action_seconds,
                    wait_context=wait_context,
                )
                continue
            try:
                request = queue.mark_attempt(request)
                stable_action = getattr(
                    session.adapter.client,
                    "action_with_transport_identity",
                    None,
                )
                if not callable(stable_action):
                    raise DomainError(
                        "work_coordinate_transport_identity_unavailable",
                        "The worker relay client cannot preserve retry identity.",
                        status=503,
                    )
                arguments = {
                    "object_ref": str(request.get("object_ref") or ""),
                    "action": str(request.get("action") or ""),
                    "payload": dict(request.get("payload") or {}),
                }
                action_started = time.monotonic()
                try:
                    result = await stable_action(
                        **arguments,
                        transport_request_id=str(request.get("request_id") or ""),
                    )
                finally:
                    governed_action_seconds = time.monotonic() - action_started
                if not isinstance(result, Mapping):
                    raise DomainError(
                        "work_coordinate_response_invalid",
                        "The governed operation returned no response object.",
                        status=502,
                    )
            except Exception as exc:  # noqa: BLE001 - becomes a bounded response
                if (
                    isinstance(exc, DomainError)
                    and exc.code == COORDINATE_LEASE_LOST
                ):
                    continue
                if (
                    isinstance(exc, DomainError)
                    and exc.code == "data_bus_outcome_unknown"
                    and not queue.expired(request)
                ):
                    try:
                        queue.defer_after_unknown(request, error=exc)
                    except DomainError as queue_exc:
                        if queue_exc.code == COORDINATE_LEASE_LOST:
                            continue
                        raise
                    counts["deferred"] += 1
                    outcome = "outcome_unknown_deferred"
                else:
                    try:
                        queue.fail(request, exc)
                    except DomainError as queue_exc:
                        if queue_exc.code == COORDINATE_LEASE_LOST:
                            continue
                        raise
                    counts["refused"] += 1
                    outcome = "refused"
            else:
                try:
                    queue.complete(request, result=dict(result))
                except DomainError as exc:
                    if exc.code == COORDINATE_LEASE_LOST:
                        continue
                    raise
                counts["completed"] += 1
                outcome = "completed"
            log_stages(
                request,
                outcome=outcome,
                relay_started=relay_started,
                governed_action_seconds=governed_action_seconds,
                wait_context=wait_context,
            )
        return counts

    async def _retire_channel(
        self,
        host: HostRelayConfig,
        channel: WorkerChannelConfig,
        *,
        reason: str,
    ) -> dict[str, Any]:
        """Apply the server tombstone to this host's exact session channel."""

        # Retirement first removes the live capability. Only after the Data
        # Bus client has stopped do local state and relay configuration exclude
        # this worker. If close cannot be proved, _drop_session fails closed
        # and this channel remains visible for process-level recovery.
        await self._drop_session(channel.worker_name)
        identity = WorkerSessionIdentity.create(
            channel.runtime_kind, channel.runtime_session_id
        )
        set_worker_channel_state(
            self.config_path,
            identity=identity,
            state="disabled",
        )
        field = SharedFieldStore(host.field_root)
        try:
            local = field.retire_worker(
                channel.worker_name,
                actor="control-plane",
                reason=reason or "Retired by the Problem Board operator.",
            )
        except DomainError as exc:
            if exc.code != "field_record_not_found":
                raise
            local = {}
        return {
            "worker_name": channel.worker_name,
            "worker_identity": channel.worker_identity,
            "state": "retired",
            "reason": reason,
            "local_record": bool(local),
        }

    @staticmethod
    def _terminal_listener_reason(listener: Mapping[str, Any] | None) -> str:
        if not listener:
            return ""
        if str(listener.get("state") or "") == "detached":
            return "field_listener_detached"
        subscription = (
            listener.get("subscription")
            if isinstance(listener.get("subscription"), Mapping)
            else {}
        )
        if str(subscription.get("state") or "") == "retired":
            return "field_listener_subscription_retired"
        return ""

    async def _disable_locally_terminal_channels(
        self, host: HostRelayConfig
    ) -> list[dict[str, Any]]:
        """Close and disable channels whose session listener has ended."""

        field = SharedFieldStore(host.field_root)
        retired: list[dict[str, Any]] = []
        for channel in host.workers:
            if channel.state not in {"active", "pending_authorization"}:
                continue
            try:
                listener = field.worker_listener_session(channel.worker_name)
            except DomainError as exc:
                if exc.code != "field_record_not_found":
                    raise
                continue
            reason = self._terminal_listener_reason(listener)
            if not reason:
                continue

            await self._drop_session(channel.worker_name)

            # A user can explicitly resume while an old cycle is closing. Do
            # not disable that new listener: its next cycle must be allowed to
            # re-enter pending authorization.
            try:
                current_listener = field.worker_listener_session(
                    channel.worker_name
                )
            except DomainError as exc:
                if exc.code != "field_record_not_found":
                    raise
                current_listener = None
            current_reason = self._terminal_listener_reason(current_listener)
            if not current_reason:
                continue

            identity = WorkerSessionIdentity.create(
                channel.runtime_kind, channel.runtime_session_id
            )
            try:
                set_worker_channel_state(
                    self.config_path,
                    identity=identity,
                    state="disabled",
                    expected_state=channel.state,
                )
            except DomainError as exc:
                if exc.code != "work_relay_worker_state_changed":
                    raise
                continue
            logger.info(
                "Problem Board relay channel lifecycle event=disabled "
                "worker_name=%s channel_identity=%s reason=%s",
                channel.worker_name,
                channel.worker_identity,
                current_reason,
            )
            retired.append(
                {
                    "worker_name": channel.worker_name,
                    "worker_identity": channel.worker_identity,
                    "state": "retired",
                    "reason": current_reason,
                    "local_record": True,
                }
            )
        return retired

    async def _apply_host_retirements(
        self,
        host: HostRelayConfig,
        results: Sequence[object],
    ) -> list[dict[str, Any]]:
        directives: dict[str, Mapping[str, Any]] = {}
        for result in results:
            if isinstance(result, BaseException) or not isinstance(result, Mapping):
                continue
            for raw in result.get("host_retirements") or []:
                if not isinstance(raw, Mapping):
                    continue
                name = str(raw.get("worker_name") or "")
                identity = str(raw.get("worker_identity") or "")
                if name and identity:
                    directives[name] = raw
        retired: list[dict[str, Any]] = []
        for worker_name, directive in directives.items():
            channel = next(
                (
                    candidate
                    for candidate in host.workers
                    if candidate.worker_name == worker_name
                    and candidate.worker_identity
                    == str(directive.get("worker_identity") or "")
                ),
                None,
            )
            if channel is None or channel.state == "disabled":
                continue
            retired.append(
                await self._retire_channel(
                    host,
                    channel,
                    reason=str(directive.get("retirement_reason") or ""),
                )
            )
        return retired

    # How long a cycle waits for a dropped socket's own reconnect before it
    # replaces the session. The Data Bus client reconnects on its own and each
    # reconnect handshake presents the bearer valid at that moment, so a
    # channel whose runtime is up is back within seconds. A replacement costs
    # a fresh credential and the pacing backoff, minutes rather than seconds.
    reconnect_grace_seconds = 10.0

    async def _await_own_reconnect(
        self, channel: WorkerChannelConfig, session: _ChannelSession
    ) -> bool:
        """True when the session's socket is connected, waiting for its own reconnect first."""

        client = session.adapter.client
        if getattr(client, "connected", True):
            return True
        wait = getattr(client, "wait_until_connected", None)
        if not callable(wait):
            return False
        logger.info(
            "Problem Board relay channel lifecycle event=awaiting_reconnect "
            "worker_name=%s channel_identity=%s replacement_epoch=%d grace_seconds=%.0f",
            channel.worker_name,
            session.channel_identity,
            session.replacement_epoch,
            self.reconnect_grace_seconds,
        )
        connected = bool(await wait(self.reconnect_grace_seconds))
        logger.info(
            "Problem Board relay channel lifecycle event=%s worker_name=%s "
            "channel_identity=%s replacement_epoch=%d",
            "reconnect_observed" if connected else "reconnect_grace_expired",
            channel.worker_name,
            session.channel_identity,
            session.replacement_epoch,
        )
        return connected

    async def _poll_channel(
        self, host: HostRelayConfig, channel: WorkerChannelConfig
    ) -> dict[str, Any]:
        injected = consume_relay_fault(
            self.config_path,
            worker_name=channel.worker_name,
        )
        if injected is not None:
            await self._drop_session(channel.worker_name)
            raise DomainError(
                str(injected["code"]),
                "A host operator injected one relay channel-open failure for live acceptance.",
                status=503,
                details={
                    "fault_id": str(injected["fault_id"]),
                    "worker_name": channel.worker_name,
                    "source": "host_operator_live_acceptance",
                },
            )
        session = self._sessions.get(channel.worker_name)
        if session is not None and not self._session_matches(
            host, channel, session, require_card=False
        ):
            await self._drop_session(channel.worker_name)
            session = None
        if session is None:
            # The Card can be replaced while the connector opens. The new
            # session then carries the Card read before the open, so it is
            # checked again before anything is drained through it, and
            # reopened once against the Card the profile holds now.
            for _attempt in range(2):
                started = time.monotonic()
                try:
                    with self._trace.stage(
                        "channel.open",
                        channel=channel.worker_name,
                        operation="data_bus.connect",
                    ):
                        session = await self._open_session(host, channel)
                except Exception as exc:
                    failure = staged_failure(
                        exc, operation="channel.open", target=host.endpoint,
                        elapsed_seconds=time.monotonic() - started,
                        retryable=self._is_retryable(exc),
                    )
                    if failure is exc:
                        raise
                    raise failure from exc
                self._sessions[channel.worker_name] = session
                if self._session_matches(host, channel, session, require_card=False):
                    break
                logger.info(
                    "Problem Board relay channel lifecycle event=card_replaced_during_open "
                    "worker_name=%s channel_identity=%s replacement_epoch=%d",
                    session.worker_name,
                    session.channel_identity,
                    session.replacement_epoch,
                )
                await self._drop_session(channel.worker_name)
                session = None
            if session is None:
                raise DomainError(
                    "work_relay_channel_card_replaced_during_open",
                    "The worker's Card changed twice while its channel opened; "
                    "the next cycle opens it again.",
                    status=503,
                    details={"worker_name": channel.worker_name},
                )
        # A socket the client is reconnecting on its own is given its grace
        # before anything is asked of it. When the grace expires the drain and
        # the poll below fail as they always did, and the cycle records the
        # failure and replaces the session.
        with self._trace.stage(
            "channel.reconnect",
            channel=channel.worker_name,
            operation="data_bus.wait_until_connected",
        ):
            await self._await_own_reconnect(channel, session)
        started = time.monotonic()
        try:
            with self._trace.stage(
                "coordinate.drain",
                channel=channel.worker_name,
                operation="coordinate.claim_and_execute",
            ):
                coordinate = await self._drain_coordinate_for_worker(
                    host,
                    channel,
                    session,
                )
        except Exception as exc:
            failure = staged_failure(
                exc, operation="coordinate.drain", target=channel.worker_name,
                elapsed_seconds=time.monotonic() - started,
                retryable=self._is_retryable(exc),
            )
            if failure is exc:
                raise
            raise failure from exc
        started = time.monotonic()
        try:
            with self._trace.stage(
                "attendance.poll",
                channel=channel.worker_name,
                operation="attendance.reconcile",
            ):
                result = await session.adapter.poll_attendances_once()
            if coordinate["claimed"]:
                result = {**dict(result), "coordinate_requests": coordinate}
            return result
        except BaseException as exc:
            keep_uncertain_session = bool(
                isinstance(exc, DomainError)
                and exc.code == "data_bus_outcome_unknown"
                and getattr(session.adapter.client, "connected", False)
            )
            if not keep_uncertain_session:
                await self._drop_session(channel.worker_name)
            if isinstance(exc, Exception):
                failure = staged_failure(
                    exc, operation="attendance.poll", target=channel.worker_name,
                    elapsed_seconds=time.monotonic() - started,
                    retryable=self._is_retryable(exc),
                )
                if failure is not exc:
                    raise failure from exc
            raise

    async def aclose(self) -> None:
        # Stop side servers before any session closes, so no drain starts on a
        # client that is being torn down.
        await self.stop_coordinate_server()
        await self.stop_outbox_server()
        await self.stop_local_state_maintenance()
        for worker_name in list(self._sessions):
            await self._drop_session(worker_name)

    def _listener_reconciliation_signature(
        self, worker_path: Path
    ) -> tuple:
        try:
            stat = worker_path.stat()
        except OSError:
            stat_signature = None
        else:
            stat_signature = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        cache_key = str(worker_path)
        cached = self._listener_signature_cache.get(cache_key)
        if cached is not None and cached[0] == stat_signature:
            return cached[1]
        if stat_signature is None:
            semantic = ("missing",)
        else:
            try:
                worker = read_json(worker_path)
            except DomainError as exc:
                semantic = ("unreadable", exc.code)
            else:
                listener = (
                    worker.get("listener")
                    if isinstance(worker.get("listener"), Mapping)
                    else {}
                )
                subscription = (
                    listener.get("subscription")
                    if isinstance(listener.get("subscription"), Mapping)
                    else {}
                )
                terminal_reason = self._terminal_listener_reason(listener)
                if terminal_reason:
                    semantic = ("terminal", terminal_reason)
                elif subscription.get("queue_reconciliation_required"):
                    semantic = (
                        "required",
                        str(subscription.get("outstanding_wake_id") or ""),
                        tuple(subscription.get("wake_queued_submission_ids") or []),
                    )
                else:
                    semantic = (
                        "settled",
                        str(listener.get("state") or ""),
                        str(subscription.get("state") or ""),
                    )
        self._listener_signature_cache[cache_key] = (stat_signature, semantic)
        return semantic

    def _local_work_signature(
        self,
        field_root: Path,
        *,
        worker_names: Sequence[str] = (),
    ) -> tuple:
        """What locally raised work looks like right now, cheaply.

        Push covers work that arrives from the board. It does not cover work
        raised on this machine: an operator action lands in the local field as
        an outbox row or an operator response, and nothing on the Data Bus
        announces it. Without this the relay sleeps to the ceiling before
        noticing, so a click waits thirty seconds for a relay that was idle the
        whole time.
        """

        control = field_root / ".problem-board"
        signature: list[tuple] = [
            (
                "outbox-ready",
                OutboxStore(control).ready_signature(worker_names=worker_names),
            )
        ]
        response_directory = control / "operator-responses"
        try:
            response_entries = sorted(response_directory.iterdir())
        except OSError:
            signature.append(("operator-responses", None))
        else:
            newest = 0
            for entry in response_entries:
                try:
                    newest = max(newest, entry.stat().st_mtime_ns)
                except OSError:
                    continue
            signature.append(
                ("operator-responses", len(response_entries), newest)
            )
        signature.append(
            (
                "coordinate",
                *CoordinateQueue(field_root).work_signature(
                    worker_names=worker_names
                ),
            )
        )
        for worker_name in sorted(set(worker_names)):
            worker_path = control / "workers" / f"{worker_name}.json"
            signature.append(
                (
                    "worker-listener",
                    worker_name,
                    *self._listener_reconciliation_signature(worker_path),
                )
            )
        fault_root = self.config_path.parent / "relay-faults"
        try:
            fault_entries = sorted(fault_root.glob("*.json"))
        except OSError:
            signature.append(("relay-faults", None))
        else:
            newest = 0
            for entry in fault_entries:
                try:
                    newest = max(newest, entry.stat().st_mtime_ns)
                except OSError:
                    continue
            signature.append(("relay-faults", len(fault_entries), newest))
        return tuple(signature)

    async def _wait_for_local_work(
        self,
        field_root: Path,
        timeout_seconds: float,
        *,
        worker_names: Sequence[str] = (),
    ) -> bool:
        """Wake on work raised on this machine, the way push wakes on the board."""

        coordinate_queue = CoordinateQueue(field_root)
        outbox = OutboxStore(field_root / ".problem-board")
        if coordinate_queue.has_ready_work(worker_names=worker_names):
            return True
        outbox_key = str(field_root.expanduser().resolve())
        ready_signature = outbox.ready_signature(worker_names=worker_names)
        if (
            ready_signature
            and self._local_outbox_ready_signatures.get(outbox_key, ())
            != ready_signature
        ):
            self._local_outbox_ready_signatures[outbox_key] = ready_signature
            return True
        initial = self._local_work_signature(
            field_root,
            worker_names=worker_names,
        )
        # Close the check-to-baseline race: a row that became the baseline is
        # already work and must not wait for a second change.
        ready_signature = outbox.ready_signature(worker_names=worker_names)
        if (
            ready_signature
            and self._local_outbox_ready_signatures.get(outbox_key, ())
            != ready_signature
        ):
            self._local_outbox_ready_signatures[outbox_key] = ready_signature
            return True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, float(timeout_seconds))
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.25, remaining))
            if self._local_work_signature(
                field_root,
                worker_names=worker_names,
            ) != initial:
                self._local_outbox_ready_signatures[outbox_key] = (
                    outbox.ready_signature(worker_names=worker_names)
                )
                return True

    @staticmethod
    def _profile_store_signature(path: Path) -> tuple[int, int, int] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return stat.st_ino, stat.st_mtime_ns, stat.st_size

    async def _wait_for_profile_store_change(
        self, path: Path, timeout_seconds: float
    ) -> bool:
        """Use local profile persistence as a wake hint, never as authorization."""

        initial = self._profile_store_signature(path)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, float(timeout_seconds))
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.5, remaining))
            if self._profile_store_signature(path) != initial:
                return True

    def _record_push_state(
        self, *, push_sessions: int, sessions: int, ceiling_seconds: float
    ) -> None:
        """Report the transition between push and ceiling, not every cycle.

        Degradation here is invisible by construction: the relay keeps working,
        just slowly, so nothing fails and nobody is told. The log already
        carried the evidence, work_relay_transport_unavailable over five hundred
        times, while the only thing that reached a person was that opening a
        plan node took half a minute.
        """

        degraded = sessions > 0 and push_sessions == 0
        if degraded == getattr(self, "_push_degraded", None):
            return
        self._push_degraded = degraded
        # Write it where an operator can read it. A log line is evidence only
        # for whoever already suspects the relay; the point of this item is
        # that the slowdown reaches the board without anyone suspecting first.
        try:
            host = HostRelayConfig.load(self.config_path)
            SharedFieldStore(host.field_root).record_relay_transport_state(
                degraded=degraded,
                reason="relay_push_transport_unavailable" if degraded else "",
                ceiling_seconds=ceiling_seconds,
            )
        except Exception:  # noqa: BLE001
            # Reporting the degradation must never become a second failure on
            # top of it. The log below still runs.
            logger.debug("Could not record relay transport state.", exc_info=True)
        if degraded:
            logger.warning(
                "Problem Board relay lost its push transport; every wake now "
                "waits the full reconciliation ceiling of %.0fs. Operator "
                "actions will feel slow until it returns. sessions=%d",
                ceiling_seconds,
                sessions,
            )
        else:
            logger.info(
                "Problem Board relay is waking on push again: %d of %d sessions.",
                push_sessions,
                sessions,
            )

    async def wait_for_wakeup(
        self, timeout_seconds: float, stop_event: asyncio.Event | None = None
    ) -> bool:
        """Wake a reconciliation cycle on PB push without treating push as state."""

        waiters = []
        push_waiters: dict[asyncio.Task, ProblemBoardHostRelayAdapter] = {}
        push_sessions = 0
        for session in self._sessions.values():
            wait_for_event = getattr(session.adapter.client, "wait_for_event", None)
            if callable(wait_for_event):
                push_sessions += 1
                waiter = asyncio.create_task(
                    wait_for_event(float(timeout_seconds))
                )
                waiters.append(waiter)
                push_waiters[waiter] = session.adapter
        # A relay with no push waiter is not idle, it is deaf, and the only
        # symptom anyone ever sees is that the board feels slow. Losing the Data
        # Bus turns every wake into the full ceiling, so say so once per
        # transition rather than letting it read as normal latency.
        self._record_push_state(
            push_sessions=push_sessions,
            sessions=len(self._sessions),
            ceiling_seconds=float(timeout_seconds),
        )
        host = HostRelayConfig.load(self.config_path)
        active_worker_names = [
            worker.worker_name for worker in host.workers if worker.state == "active"
        ]
        relay_worker_names = [
            worker.worker_name
            for worker in host.workers
            if worker.state in {"active", "pending_authorization"}
        ]
        if pending_relay_faults(
            self.config_path,
            worker_names=active_worker_names,
        ):
            return False
        # An operator action is not a board event, so push never carries it.
        # This waiter is what makes a click cost its own latency instead of the
        # reconciliation ceiling.
        waiters.append(
            asyncio.create_task(
                self._wait_for_local_work(
                    host.field_root,
                    float(timeout_seconds),
                    worker_names=relay_worker_names,
                )
            )
        )
        if (
            host.connection_hub_state_root is not None
            and any(worker.state == "pending_authorization" for worker in host.workers)
        ):
            waiters.append(
                asyncio.create_task(
                    self._wait_for_profile_store_change(
                        host.connection_hub_state_root / "profiles.json",
                        float(timeout_seconds),
                    )
                )
            )
        stop_task = (
            asyncio.create_task(stop_event.wait()) if stop_event is not None else None
        )
        if stop_task is not None:
            waiters.append(stop_task)
        if not waiters:
            if stop_event is None:
                await asyncio.sleep(timeout_seconds)
                return False
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=timeout_seconds)
            except asyncio.TimeoutError:
                return False
            return True
        done, pending = await asyncio.wait(
            waiters,
            timeout=max(0.1, float(timeout_seconds)),
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            adapter = push_waiters.get(task)
            if adapter is None or task.cancelled():
                continue
            try:
                event = task.result()
            except Exception:  # noqa: BLE001 - the next poll owns transport recovery
                continue
            if not isinstance(event, Mapping):
                continue
            data = event.get("data")
            if not isinstance(data, Mapping):
                data = event
            kind = str(data.get("kind") or "")
            if kind in {
                "project.linked",
                "project.unlinked",
                "worker.retired",
            }:
                # Attendance is server-owned. A targeted transition event is
                # the explicit reason to bypass the discovery heartbeat
                # deadline for this channel; unrelated pushes only reconcile
                # controls and cannot amplify its presence traffic.
                refs = data.get("refs")
                adapter.request_attendance_refresh(
                    kind=kind,
                    refs=refs if isinstance(refs, Mapping) else {},
                )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        stopping = bool(stop_task is not None and stop_task in done and stop_task.result())
        if stopping:
            await self.stop_coordinate_server()
            await self.stop_outbox_server()
        return stopping

    # -- local-state housekeeping, beside the channel cycle -------------------

    LOCAL_STATE_MAINTENANCE_FIRST_DELAY_SECONDS = 30.0
    LOCAL_STATE_MAINTENANCE_INTERVAL_SECONDS = 300.0

    def _ensure_local_state_maintenance(self, field_root: Path) -> None:
        task = self._maintenance_task
        if task is None or task.done():
            self._maintenance_task = asyncio.create_task(
                self._maintain_local_state(field_root),
                name="problem-board-local-state-maintenance",
            )

    async def stop_local_state_maintenance(self) -> None:
        task = self._maintenance_task
        self._maintenance_task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _maintain_local_state(self, field_root: Path) -> None:
        """Run housekeeping in a thread, first shortly after start, then on an interval.

        Whether retention is due is recorded in the field (LS4), so this
        interval only decides how often the check runs.
        """

        await asyncio.sleep(self.LOCAL_STATE_MAINTENANCE_FIRST_DELAY_SECONDS)
        while True:
            try:
                await asyncio.to_thread(
                    run_local_state_maintenance, SharedFieldStore(field_root)
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - housekeeping never stops the relay
                logger.warning(
                    "Problem Board local-state maintenance failed field=%s",
                    field_root,
                    exc_info=True,
                )
            await asyncio.sleep(self.LOCAL_STATE_MAINTENANCE_INTERVAL_SECONDS)

    # -- outbox rows, served beside the channel cycle -----------------------

    def _ensure_outbox_server(self) -> None:
        self._outbox_server.ensure_started()

    async def stop_outbox_server(self) -> None:
        await self._outbox_server.stop()

    def serve_outbox_once(self) -> list[str]:
        return self._outbox_server.serve_once()

    # -- coordinate requests, served beside the channel cycle -----------------

    COORDINATE_SERVE_INTERVAL_SECONDS = 0.25

    def _ensure_coordinate_server(self) -> None:
        task = self._coordinate_task
        if task is None or task.done():
            self._coordinate_task = asyncio.create_task(
                self._serve_coordinate_requests(),
                name="problem-board-coordinate-server",
            )

    async def stop_coordinate_server(self) -> None:
        tasks = [
            task
            for task in (self._coordinate_task, *self._coordinate_draining.values())
            if task is not None and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._coordinate_task = None
        self._coordinate_draining.clear()

    async def _serve_coordinate_requests(self) -> None:
        """Serve local coordinate requests as they arrive, not once per cycle.

        Why: the channel cycle runs every channel's network work together and
        waits for the slowest, so a request that arrived mid-cycle can wait out
        a reload or another channel's Data Bus timeout. Claims stay exclusive per
        request, so the cycle's own drain remains a safe fallback.
        """

        while True:
            try:
                self.serve_coordinate_once()
            except Exception:  # noqa: BLE001 - the next pass retries
                logger.warning("Problem Board coordinate server pass failed", exc_info=True)
            await asyncio.sleep(self.COORDINATE_SERVE_INTERVAL_SECONDS)

    def serve_coordinate_once(self) -> list[str]:
        """Start a drain for each worker with ready requests and an open channel.

        Returns the worker names a drain was started for. A worker without an
        open session, or whose channel is backing off, is left to the cycle
        (pb coordinate already refuses at once while it is reconnecting).
        """

        host = HostRelayConfig.load(self.config_path)
        queue = CoordinateQueue(host.field_root)
        started: list[str] = []
        for channel in host.workers:
            name = channel.worker_name
            if channel.state != "active" or name in self._coordinate_draining:
                continue
            if self._coordinate_drain_lock(name).locked():
                continue  # the cycle is draining this worker now
            session = self._sessions.get(name)
            if session is None or session.closing:
                continue
            if not self._pacing.channel_due(name):
                continue
            if not queue.has_ready_work(worker_names=[name]):
                continue
            # Only the session opened for this exact channel and Card may
            # carry its requests. During a replacement the cached session can
            # still belong to the old one; the cycle drops and reopens it
            # before its own drain, and until then the requests wait. The
            # check reads the profile record, so it runs only when there is
            # work to carry.
            if not self._session_matches(host, channel, session, require_card=True):
                continue
            task = asyncio.create_task(
                self._drain_coordinate_beside_cycle(host, channel, session),
                name=f"problem-board-coordinate-{name}",
            )
            self._coordinate_draining[name] = task
            started.append(name)
        return started

    async def _drain_coordinate_beside_cycle(
        self,
        host: HostRelayConfig,
        channel: WorkerChannelConfig,
        session: _ChannelSession,
    ) -> None:
        try:
            await self._drain_coordinate_for_worker(host, channel, session)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - per-request errors are already answered
            logger.warning(
                "Problem Board coordinate drain beside the cycle failed worker=%s",
                channel.worker_name,
                exc_info=True,
            )
        finally:
            self._coordinate_draining.pop(channel.worker_name, None)

    async def poll_once(self) -> dict[str, Any]:
        cycle = self._trace.start_cycle()
        outcome = "succeeded"
        try:
            return await self._poll_once_body()
        except BaseException as exc:
            outcome = (
                "cancelled"
                if isinstance(exc, asyncio.CancelledError)
                else f"failed:{self._failure_code(exc)}"
            )
            raise
        finally:
            self._trace.finish_cycle(cycle, outcome=outcome)

    async def _poll_once_body(self) -> dict[str, Any]:
        self._ensure_coordinate_server()
        self._ensure_outbox_server()
        with self._trace.stage("host.load", operation="relay.config"):
            host = HostRelayConfig.load(self.config_path)
        self._ensure_local_state_maintenance(host.field_root)
        with self._trace.stage(
            "channels.retire_local",
            operation="terminal_listener.reconcile",
        ):
            locally_retired = await self._disable_locally_terminal_channels(host)
        if locally_retired:
            host = HostRelayConfig.load(self.config_path)
        channels = [worker for worker in host.workers if worker.state == "active"]
        pending_channels = [
            worker for worker in host.workers if worker.state == "pending_authorization"
        ]
        active_names = {
            worker.worker_name for worker in (*channels, *pending_channels)
        }
        for worker_name in list(self._sessions):
            if worker_name not in active_names:
                with self._trace.stage(
                    "channel.close",
                    channel=worker_name,
                    operation="inactive_session.close",
                ):
                    await self._drop_session(worker_name)
        with self._trace.stage(
            "native_queue.reconcile",
            operation="session_queue.preflight",
        ):
            await self._reconcile_queues_before_channels(
                host, [*channels, *pending_channels]
            )
        # Only channels the pacing allows call the gateway this cycle. A
        # deferred active channel still gets its local session wake below.
        pacing = self._pacing
        host_quiet = pacing.host_quiet_seconds() > 0
        due_channels = [
            worker for worker in channels
            if not host_quiet and pacing.channel_due(worker.worker_name)
        ]
        due_names = {worker.worker_name for worker in due_channels}
        deferred_channels = [
            worker for worker in channels if worker.worker_name not in due_names
        ]
        fingerprints = {
            worker.worker_name: self._profile_fingerprint(host, worker)
            for worker in pending_channels
        }
        due_pending = [
            worker for worker in pending_channels
            if not host_quiet
            and pacing.pending_due(worker.worker_name, fingerprints[worker.worker_name])
        ]
        due_pending_names = {worker.worker_name for worker in due_pending}
        waiting_pending = [
            worker for worker in pending_channels
            if worker.worker_name not in due_pending_names
        ]
        with self._trace.stage(
            "channels.active",
            operation="channel.poll_due",
        ):
            results = await asyncio.gather(
                *(self._poll_channel(host, worker) for worker in due_channels),
                return_exceptions=True,
            )
        with self._trace.stage(
            "channels.pending",
            operation="authorization.retry_due",
        ):
            pending_results = await asyncio.gather(
                *(self._poll_channel(host, worker) for worker in due_pending),
                return_exceptions=True,
            )
        with self._trace.stage(
            "channels.retire_remote",
            operation="host_retirements.apply",
        ):
            remote_retired = await self._apply_host_retirements(
                host, [*results, *pending_results]
            )
        retired = [*locally_retired, *remote_retired]
        retired_names = {row["worker_name"] for row in retired}
        workers: list[dict[str, Any]] = []
        failures: list[BaseException] = []
        next_poll_seconds: list[int] = []
        pending: list[dict[str, Any]] = []
        demoted = 0
        for channel, result in zip(due_channels, results):
            if channel.worker_name in retired_names:
                continue
            if isinstance(result, BaseException):
                pacing.observe(result)
            if (
                isinstance(result, DomainError)
                and result.code == "work_worker_retired"
            ):
                retired_row = await self._retire_channel(
                    host,
                    channel,
                    reason=str(result.details.get("retirement_reason") or ""),
                )
                retired.append(retired_row)
                retired_names.add(channel.worker_name)
                continue

            # Local mail and the native session wake do not depend on a healthy
            # Data Bus reconciliation. A poisoned outbox or remote operation
            # refusal must not make an already-running coding session deaf.
            try:
                notification_started = time.monotonic()
                with self._trace.stage(
                    "session.notify",
                    channel=channel.worker_name,
                    operation="input.available",
                ):
                    delivery = await self._notify_available_input(host, channel)
            except Exception as exc:  # noqa: BLE001
                failure = staged_failure(
                    exc, operation="session.notify", target=channel.worker_name,
                    elapsed_seconds=time.monotonic() - notification_started,
                    retryable=self._is_retryable(exc),
                )
                logger.warning(
                    "Problem Board session wake failed worker=%s "
                    "error_type=%s error_code=%s message=%s",
                    channel.worker_name,
                    failure_type(failure),
                    self._failure_code(failure),
                    failure_message(failure),
                    exc_info=True,
                )
                delivery = {
                    "adapter": "unknown",
                    "state": "error",
                    "event_kind": "input.available",
                    "delivered": False,
                    "reason": self._failure_code(failure),
                    "message": failure_message(failure),
                    "request": self._failure_evidence(failure),
                }

            if isinstance(result, BaseException):
                observation = self._authorization_failure_observation(result)
                if observation["terminal_channel"]:
                    pacing.record_pending_refusal(
                        channel.worker_name,
                        fingerprint=self._profile_fingerprint(host, channel),
                        permanent=True,
                        reason=self._failure_code(result),
                        credential=credential_refused(result),
                    )
                    identity = WorkerSessionIdentity.create(
                        channel.runtime_kind, channel.runtime_session_id
                    )
                    set_worker_channel_state(
                        self.config_path,
                        identity=identity,
                        state="pending_authorization",
                        expected_state="active",
                    )
                    self._record_authorization(host, channel, observation)
                    pending_row = {
                        **channel.to_mapping(),
                        "observation": observation,
                    }
                    if delivery is not None:
                        pending_row["session_delivery"] = delivery
                    pending.append(pending_row)
                    demoted += 1
                    continue
                retryable = self._is_retryable(result)
                if retryable:
                    self._record_relay_channel_degraded(host, channel, result)
                # Every failed channel backs off. The classification only
                # decides the log, the degraded record and the operator hint.
                # The one exception is a relay process that must be replaced:
                # that failure is about this process, not the gateway, and it
                # has to keep surfacing until the supervisor replaces it.
                if self._failure_code(result) != "work_relay_channel_stop_failed":
                    self._record_channel_failure(pacing, channel.worker_name, result)
                failures.append(result)
                # An authorization refusal that does not name what it wanted is
                # a dead end for whoever reads it. work_worker_operation_not_granted
                # appeared 1131 times in this log without once saying which
                # operation or which claim, so nobody could act on any of them.
                details = getattr(result, "details", None)
                details = dict(details) if isinstance(details, Mapping) else {}
                request = self._failure_evidence(result)
                wanted = " ".join(
                    f"{key}={details[key]}"
                    for key in ("operation", "required_grants", "resource", "held_grants")
                    if details.get(key) and key not in request
                )
                request_log = "".join(
                    f" {key}={json.dumps(value, ensure_ascii=True)}"
                    for key, value in request.items()
                )
                logger.warning(
                    "Problem Board worker channel failed worker=%s profile=%s "
                    "error_type=%s error_code=%s retryable=%s%s%s%s",
                    channel.worker_name,
                    channel.profile,
                    failure_type(result),
                    self._failure_code(result),
                    retryable,
                    request_log,
                    f" {wanted}" if wanted else "",
                    f" message={failure_message(result)}",
                )
                # The log says what it wanted; the surfaced row did not, and the
                # row is what a person actually sees. Carrying only error_type
                # and retryable is why 1131 refusals produced no action: the
                # reader could tell something failed and never what to grant.
                error_row = {
                    "worker_name": channel.worker_name,
                    "worker_alias": channel.worker_alias,
                    "state": "error",
                    "error_type": failure_type(result),
                    "error_code": self._failure_code(result),
                    "retryable": retryable,
                    "message": failure_message(result),
                    "needed": {
                        key: details[key]
                        for key in (
                            "operation",
                            "required_grants",
                            "resource",
                            "held_grants",
                        )
                        if details.get(key)
                    },
                }
                # A Card whose operation list predates the operation is fixed
                # by one command, and the row names it with this channel's
                # profile (W262, operator 2026-09-23: re-approval, no fallback).
                actionable = actionable_card_refusal(
                    self._failure_code(result), details, profile=channel.profile
                )
                if actionable:
                    error_row["needed"] = {**error_row["needed"], **actionable}
                if request:
                    error_row["request"] = request
                if delivery is not None:
                    error_row["session_delivery"] = delivery
                workers.append(error_row)
            else:
                row = dict(result)
                pacing.record_success(channel.worker_name)
                await self._record_relay_channel_recovered(host, channel)
                if delivery is not None:
                    row["session_delivery"] = delivery
                hint = row.get("next_poll_seconds")
                if isinstance(hint, (int, float)) and hint > 0:
                    next_poll_seconds.append(int(hint))
                workers.append(row)
        for channel in deferred_channels:
            if channel.worker_name in retired_names:
                continue
            try:
                with self._trace.stage(
                    "session.notify",
                    channel=channel.worker_name,
                    operation="input.available.deferred_channel",
                ):
                    delivery = await self._notify_available_input(host, channel)
            except Exception:  # noqa: BLE001 - a local wake never blocks the cycle
                logger.warning(
                    "Problem Board session wake failed worker=%s", channel.worker_name,
                    exc_info=True,
                )
                delivery = None
            deferred_row = {
                "worker_name": channel.worker_name,
                "worker_alias": channel.worker_alias,
                "state": "deferred",
                "reason": "host_rate_limited" if host_quiet else "backing_off",
            }
            if delivery is not None:
                deferred_row["session_delivery"] = delivery
            workers.append(deferred_row)
        for channel in waiting_pending:
            if channel.worker_name not in retired_names:
                pending.append({**channel.to_mapping(), "deferred": True})
        promoted = 0
        for channel, result in zip(due_pending, pending_results):
            if channel.worker_name in retired_names:
                continue
            if isinstance(result, BaseException):
                pacing.observe(result)
                # A runtime that is not there refused nothing: never permanent.
                permanent = not self._is_retryable(result) and not is_runtime_unavailable(result)
                pacing.record_pending_refusal(
                    channel.worker_name,
                    fingerprint=fingerprints[channel.worker_name],
                    permanent=permanent,
                    reason=self._failure_code(result),
                    credential=permanent and credential_refused(result),
                )
                if not permanent:
                    self._record_channel_failure(pacing, channel.worker_name, result)
                if (
                    isinstance(result, DomainError)
                    and result.code == "work_worker_retired"
                ):
                    retired_row = await self._retire_channel(
                        host,
                        channel,
                        reason=str(result.details.get("retirement_reason") or ""),
                    )
                    retired.append(retired_row)
                    retired_names.add(channel.worker_name)
                    continue
                observation = self._authorization_failure_observation(result)
                self._record_authorization(host, channel, observation)
                if self._is_retryable(result):
                    self._record_relay_channel_degraded(host, channel, result)
                pending.append(
                    {
                        **channel.to_mapping(),
                        "observation": observation,
                    }
                )
                continue
            identity = WorkerSessionIdentity.create(
                channel.runtime_kind, channel.runtime_session_id
            )
            try:
                set_worker_channel_state(
                    self.config_path,
                    identity=identity,
                    state="active",
                    expected_state="pending_authorization",
                )
            except DomainError:
                await self._drop_session(channel.worker_name)
                raise
            row = dict(result)
            row["authorization_transition"] = "activated"
            pacing.record_success(channel.worker_name)
            await self._record_relay_channel_recovered(host, channel)
            with self._trace.stage(
                "session.notify",
                channel=channel.worker_name,
                operation="control_plane.connected",
            ):
                row["session_delivery"] = await self._notify_session(
                    host,
                    channel,
                    event_kind="control_plane.connected",
                )
            hint = row.get("next_poll_seconds")
            if isinstance(hint, (int, float)) and hint > 0:
                next_poll_seconds.append(int(hint))
            workers.append(row)
            promoted += 1
        if (
            failures
            and len(failures) == len(due_channels)
            and not deferred_channels
            and not pending_channels
        ):
            first = failures[0]
            if all(self._is_retryable(error) for error in failures):
                raise HostRelayRetryableError(
                    "work_relay_transport_unavailable",
                    f"Every worker channel failed this cycle: {first}",
                ) from first
            raise first
        active_channel_names = {item.worker_name for item in channels}
        retired_active_count = sum(
            1 for row in retired if row["worker_name"] in active_channel_names
        )
        result: dict[str, Any] = {
            "host_id": host.host_id,
            "relay_id": host.relay_id,
            "configured_workers": len(host.workers),
            "active_workers": max(
                0,
                len(channels) - demoted + promoted - retired_active_count,
            ),
            "pending_authorization": pending,
            "retired_workers": retired,
            "workers": workers,
            "pacing": pacing.snapshot(),
        }
        hint = self._cycle_next_poll(next_poll_seconds, failures, pacing)
        if hint is not None:
            result["next_poll_seconds"] = hint
        return result

__all__ = [
    "CONFIG_SCHEMA",
    "ChannelConnector",
    "ProblemBoardHostRelayAdapter",
    "ProblemBoardRelaySupervisor",
    "RelayConfig",
    "channel_lifecycle_labels",
]

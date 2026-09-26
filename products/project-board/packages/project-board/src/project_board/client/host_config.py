from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from ..contract.errors import DomainError
from ..contract.worker_identity import (
    WorkerSessionIdentity,
    normalize_worker_alias,
)
from .agent_session import default_profile_name
from .io import atomic_write_json, component, exclusive_lock, read_json, utc_now
from .journals import repository_entries, repository_entry


HOST_CONFIG_SCHEMA = "problem-board.host-relay-config.v2"
HOST_CONFIG_POINTER_SCHEMA = "problem-board.host-relay-default.v1"
HOST_CONFIG_BUNDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$")
DEFAULT_CONTROL_KINDS = (
    "assign",
    "journal.catalog",
    "journal.read",
    "mail",
    "materialize",
    "ping",
    "project.report",
    "replan",
    "request",
    "resume",
    "stop",
)

# Which teammates may mail the agents on a new host. The board already lets
# only a worker that shares a project address another, so this is the host
# owner's second gate. Empty refuses every peer until the owner opts in
# (`pb host configure --allow-peer-worker <name>` or "*"), which on
# 2026-09-24 left a new agent unable to receive its coordinator's mail
# (W304 finding 38). The default for new hosts is the operator's decision.
# Operator ruling, 2026-09-26 (W304 decision 3): a new host accepts mail from
# its project teammates. The board lets only agents that share a project
# address each other, so "*" grants nothing beyond that; `pb host configure
# --deny-all-peers` or named workers narrow it.
DEFAULT_ALLOWED_PEER_WORKERS: tuple[str, ...] = ("*",)


def _required(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise DomainError("work_relay_config_invalid", f"Relay config requires {field}.")
    return text


def _agent_root(value: str, allowed_roots: Sequence[Any]) -> str:
    """The configured agent workspace root, which must lie inside an approved work root."""

    if not value:
        return ""
    root = _absolute(value, "agent_workspace.root")
    if not any(is_inside(root, str(item)) for item in allowed_roots or []):
        raise DomainError(
            "work_relay_config_invalid",
            "agent_workspace.root must lie inside an approved work root.",
            details={"agent_workspace_root": str(root)},
        )
    return str(root)


def _absolute(value: Any, field: str) -> Path:
    path = Path(_required(value, field)).expanduser()
    if not path.is_absolute():
        raise DomainError(
            "work_relay_config_invalid", f"{field} must be an absolute LOCAL path."
        )
    return path.resolve()


def _endpoint(value: Any) -> str:
    endpoint = _required(value, "target.endpoint")
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise DomainError(
            "work_relay_config_invalid",
            "target.endpoint must be an absolute HTTP or HTTPS MCP endpoint.",
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise DomainError(
            "work_relay_config_invalid",
            "target.endpoint cannot contain credentials, query parameters, or a fragment.",
        )
    return endpoint.rstrip("/")


def _bundle_id(value: Any) -> str:
    bundle_id = str(value or "problem-board@1-0").strip()
    if not HOST_CONFIG_BUNDLE_RE.fullmatch(bundle_id):
        raise DomainError(
            "work_relay_config_invalid",
            "target.bundle_id contains unsupported characters.",
        )
    return bundle_id


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


# The old names describe a polling design this relay does not have. A relay.json
# written before the rename still carries them, and a host that stops reading
# them would silently fall back to the default ceiling, which is the exact
# invisible slowdown this rename exists to prevent. So both are read and only
# the new one is written.
_CEILING_KEYS = ("reconcile_ceiling_seconds", "poll_interval_seconds")
_IDLE_CEILING_KEYS = ("idle_reconcile_ceiling_seconds", "idle_poll_interval_seconds")


def _declared(relay: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = relay.get(key)
        if value:
            return value
    return None


def _reconcile_ceiling(relay: Mapping[str, Any]) -> int:
    return max(5, min(int(_declared(relay, _CEILING_KEYS) or 60), 86_400))


def _idle_reconcile_ceiling(relay: Mapping[str, Any]) -> int:
    # Push wakes the relay immediately. This ceiling is the reconciliation
    # fallback when no project currently addresses the worker.
    active = _reconcile_ceiling(relay)
    declared = int(_declared(relay, _IDLE_CEILING_KEYS) or max(active, 120))
    return max(active, min(declared, 86_400))


@dataclass(frozen=True)
class WorkerChannelConfig:
    runtime_kind: str
    runtime_session_id: str
    worker_identity: str
    worker_name: str
    profile: str
    worker_alias: str
    capabilities: tuple[str, ...]
    state: str
    # Captured when this exact session enrolls. It stays in the LOCAL host
    # config and lets the relay rebuild a useful resume command later.
    working_directory: str = ""

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "WorkerChannelConfig":
        identity = WorkerSessionIdentity.create(
            value.get("runtime_kind"), value.get("runtime_session_id")
        )
        declared_identity = str(value.get("worker_identity") or identity.worker_identity)
        declared_name = str(value.get("worker_name") or identity.worker_name)
        if declared_identity != identity.worker_identity or declared_name != identity.worker_name:
            raise DomainError(
                "work_relay_worker_identity_invalid",
                "A worker channel's stable identity must be derived from runtime_kind and runtime_session_id.",
            )
        capabilities = value.get("capabilities") or []
        if isinstance(capabilities, (str, bytes, bytearray)) or not isinstance(
            capabilities, Sequence
        ):
            raise DomainError(
                "work_relay_config_invalid", "worker.capabilities must be an array."
            )
        state = str(value.get("state") or "active").strip().lower()
        if state not in {"active", "pending_authorization", "disabled"}:
            raise DomainError(
                "work_relay_config_invalid",
                "worker.state must be active, pending_authorization, or disabled.",
            )
        working_directory = str(value.get("working_directory") or "").strip()
        if working_directory:
            working_path = Path(working_directory).expanduser()
            if not working_path.is_absolute():
                raise DomainError(
                    "work_relay_config_invalid",
                    "worker.working_directory must be an absolute LOCAL path.",
                )
            working_directory = str(working_path.resolve())
        result = cls(
            runtime_kind=identity.runtime_kind,
            runtime_session_id=identity.runtime_session_id,
            worker_identity=identity.worker_identity,
            worker_name=identity.worker_name,
            profile=component(_required(value.get("profile"), "worker.profile"), field="profile"),
            worker_alias=normalize_worker_alias(value.get("alias")),
            capabilities=tuple(
                sorted({str(item).strip() for item in capabilities if str(item).strip()})
            ),
            state=state,
            working_directory=working_directory,
        )
        return result

    def to_mapping(self) -> dict[str, Any]:
        return {
            "runtime_kind": self.runtime_kind,
            "runtime_session_id": self.runtime_session_id,
            "worker_identity": self.worker_identity,
            "worker_name": self.worker_name,
            "profile": self.profile,
            "alias": self.worker_alias,
            "capabilities": list(self.capabilities),
            "state": self.state,
            **(
                {"working_directory": self.working_directory}
                if self.working_directory
                else {}
            ),
        }


@dataclass(frozen=True)
class HostRelayConfig:
    path: Path | None
    target_id: str
    endpoint: str
    tenant: str
    platform_project: str
    bundle_id: str
    field_root: Path
    connection_hub_state_root: Path | None
    host_id: str
    host_label: str
    host_kind: str
    relay_id: str
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
    workers: tuple[WorkerChannelConfig, ...]
    # Remote URL per alias, operator-declared, published so the board can link
    # portable repo: refs. Never a local path.
    source_repository_urls: tuple[tuple[str, str], ...] = ()
    # W262: (alias, read_root, read_ref) for each alias whose map entry names
    # the never-edited worktree the project's setup is read from.
    source_read_roots: tuple[tuple[str, str, str], ...] = ()
    # W262: where each agent's own workspace lives, one folder per agent
    # (``pb host configure --agent-workspace-root``); unset, the first approved
    # work root. See ``agent_workspace_root``.
    agent_workspace_root: str = ""

    @property
    def effective_agent_workspace_root(self) -> str:
        """The configured agent workspace root, else the first approved work root."""

        return self.agent_workspace_root or (self.allowed_roots[0] if self.allowed_roots else "")

    def repository_mapping(self) -> dict[str, Any]:
        """The repository map entries, read roots included, for ``RepositoryMap``."""

        return repository_entries(self.source_repositories, self.source_read_roots)

    @property
    def profile_scope(self) -> str:
        return "|".join(
            (
                self.endpoint,
                self.tenant,
                self.platform_project,
                self.bundle_id,
            )
        )

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, Any], *, path: Path | None = None
    ) -> "HostRelayConfig":
        if value.get("schema") != HOST_CONFIG_SCHEMA:
            raise DomainError(
                "work_relay_config_invalid", "The host relay config schema is unsupported."
            )
        forbidden = _secret_fields(value)
        if forbidden:
            raise DomainError(
                "work_relay_secret_in_config",
                "Host relay config stores profile names and never credentials.",
                details={"fields": sorted(forbidden)},
            )
        target = value.get("target") if isinstance(value.get("target"), Mapping) else {}
        host = value.get("host") if isinstance(value.get("host"), Mapping) else {}
        relay = value.get("relay") if isinstance(value.get("relay"), Mapping) else {}
        connection_hub = (
            value.get("connection_hub")
            if isinstance(value.get("connection_hub"), Mapping)
            else {}
        )
        journal = (
            value.get("journal_workspace")
            if isinstance(value.get("journal_workspace"), Mapping)
            else {}
        )
        receiver = (
            value.get("receiver_policy")
            if isinstance(value.get("receiver_policy"), Mapping)
            else {}
        )
        raw_workers = value.get("workers") or []
        if isinstance(raw_workers, (str, bytes, bytearray)) or not isinstance(
            raw_workers, Sequence
        ):
            raise DomainError("work_relay_config_invalid", "workers must be an array.")
        workers = tuple(
            WorkerChannelConfig.from_mapping(item)
            for item in raw_workers
            if isinstance(item, Mapping)
        )
        identities = [worker.worker_identity for worker in workers]
        profiles = [worker.profile for worker in workers]
        if len(identities) != len(set(identities)) or len(profiles) != len(set(profiles)):
            raise DomainError(
                "work_relay_config_invalid",
                "Each worker session and Connection Hub profile may appear once per host relay.",
            )
        host_kind = str(host.get("kind") or "local").strip().lower()
        if host_kind not in {"local", "hosted", "remote"}:
            raise DomainError(
                "work_relay_config_invalid", "host.kind must be local, hosted, or remote."
            )
        state_root_value = str(connection_hub.get("state_root") or "").strip()
        state_root = _absolute(state_root_value, "connection_hub.state_root") if state_root_value else None
        agent_workspace = value.get("agent_workspace") if isinstance(value.get("agent_workspace"), Mapping) else {}
        agent_root_value = str(agent_workspace.get("root") or "").strip()
        journal_root_value = str(journal.get("root") or "").strip()
        journal_root = _absolute(journal_root_value, "journal_workspace.root") if journal_root_value else None
        repositories = journal.get("source_repositories") or {}
        if not isinstance(repositories, Mapping):
            raise DomainError(
                "work_relay_config_invalid",
                "journal_workspace.source_repositories must be an object.",
            )
        try:
            entries = {
                alias: repository_entry(str(alias), raw)
                for alias, raw in repositories.items()
            }
        except DomainError as exc:
            raise DomainError(
                "work_relay_config_invalid", str(exc), details=dict(exc.details)
            ) from exc
        repository_urls = journal.get("source_repository_urls") or {}
        if not isinstance(repository_urls, Mapping):
            raise DomainError(
                "work_relay_config_invalid",
                "journal_workspace.source_repository_urls must be an object.",
            )
        roots = value.get("allowed_roots") or []
        if isinstance(roots, (str, bytes, bytearray)) or not isinstance(roots, Sequence):
            raise DomainError("work_relay_config_invalid", "allowed_roots must be an array.")
        kinds = receiver.get("allowed_control_kinds") or DEFAULT_CONTROL_KINDS
        peers = receiver.get("allowed_peer_workers")
        if peers is None:
            # A key the file never had takes the default; an explicit [] denies all.
            peers = list(DEFAULT_ALLOWED_PEER_WORKERS)
        if isinstance(kinds, (str, bytes, bytearray)) or not isinstance(kinds, Sequence):
            raise DomainError(
                "work_relay_config_invalid",
                "receiver_policy.allowed_control_kinds must be an array.",
            )
        if isinstance(peers, (str, bytes, bytearray)) or not isinstance(peers, Sequence):
            raise DomainError(
                "work_relay_config_invalid",
                "receiver_policy.allowed_peer_workers must be an array.",
            )
        result = cls(
            path=path,
            target_id=component(_required(target.get("id"), "target.id"), field="target.id"),
            endpoint=_endpoint(target.get("endpoint")),
            tenant=component(_required(target.get("tenant"), "target.tenant"), field="target.tenant"),
            platform_project=component(
                _required(target.get("project"), "target.project"), field="target.project"
            ),
            bundle_id=_bundle_id(target.get("bundle_id")),
            field_root=_absolute(value.get("field_root"), "field_root"),
            connection_hub_state_root=state_root,
            host_id=component(_required(host.get("id"), "host.id"), field="host.id"),
            host_label=_required(host.get("label"), "host.label"),
            host_kind=host_kind,
            relay_id=component(_required(relay.get("id"), "relay.id"), field="relay.id"),
            reconcile_ceiling_seconds=_reconcile_ceiling(relay),
            idle_reconcile_ceiling_seconds=_idle_reconcile_ceiling(relay),
            journal_workspace_root=journal_root,
            source_repositories=tuple(
                sorted(
                    (
                        component(str(alias), field="repository alias"),
                        str(_absolute(entry[0], f"source_repositories.{alias}")),
                    )
                    for alias, entry in entries.items()
                )
            ),
            source_read_roots=tuple(
                sorted(
                    (
                        component(str(alias), field="repository alias"),
                        str(_absolute(entry[1], f"source_repositories.{alias}.read_root")),
                        entry[2],
                    )
                    for alias, entry in entries.items()
                    if entry[1]
                )
            ),
            allowed_roots=tuple(sorted(str(_absolute(root, "allowed_root")) for root in roots)),
            create_missing_journal_home=bool(journal.get("create_missing_home", False)),
            allowed_control_kinds=tuple(
                sorted({str(item).strip() for item in kinds if str(item).strip()})
            ),
            allowed_peer_workers=tuple(
                sorted({str(item).strip().lower() for item in peers if str(item).strip()})
            ),
            max_control_bytes=max(
                1024, min(int(receiver.get("max_control_bytes") or 64 * 1024), 512 * 1024)
            ),
            allow_session_resume_view=bool(
                receiver.get("allow_session_resume_view", True)
            ),
            workers=workers,
            source_repository_urls=tuple(
                sorted(
                    (component(str(alias), field="repository alias"), _repository_url(url, alias=str(alias)))
                    for alias, url in repository_urls.items()
                )
            ),
            agent_workspace_root=_agent_root(agent_root_value, roots),
        )
        approved_roots = [Path(root) for root in result.allowed_roots]
        for alias, repository in (
            *result.source_repositories,
            *((alias, read_root) for alias, read_root, _ in result.source_read_roots),
        ):
            repository_path = Path(repository)
            if not any(
                repository_path == root or repository_path.is_relative_to(root)
                for root in approved_roots
            ):
                raise DomainError(
                    "work_repository_root_not_allowed",
                    "Every repository mapping must be inside an explicitly approved coding root.",
                    details={"alias": alias, "repository": repository},
                )
        return result

    @classmethod
    def load(cls, path: str | Path) -> "HostRelayConfig":
        config_path = Path(path).expanduser().resolve()
        return cls.from_mapping(read_json(config_path), path=config_path)

    def worker(self, identity: WorkerSessionIdentity) -> WorkerChannelConfig | None:
        return next(
            (
                worker
                for worker in self.workers
                if worker.worker_identity == identity.worker_identity
            ),
            None,
        )


def client_runtime_root() -> Path:
    configured = str(os.environ.get("KDCUBE_CLIENT_RUNTIME_HOME") or "").strip()
    return (
        Path(configured).expanduser().resolve()
        if configured
        else (Path.home() / ".kdcube" / "client-runtime").resolve()
    )


def default_pointer_path(*, state_root: Path | None = None) -> Path:
    return (state_root or client_runtime_root()) / "problem-board" / "default.json"


def target_pointer_path(
    *,
    target_id: str,
    tenant: str,
    platform_project: str,
    state_root: Path | None = None,
) -> Path:
    root = state_root or client_runtime_root()
    return (
        root
        / "problem-board"
        / "targets"
        / component(target_id, field="target_id")
        / f"{component(tenant, field='tenant')}__{component(platform_project, field='platform_project')}"
        / "default.json"
    )


def host_state_path(
    *,
    target_id: str,
    tenant: str,
    platform_project: str,
    host_id: str,
    state_root: Path | None = None,
) -> Path:
    root = state_root or client_runtime_root()
    return (
        root
        / "problem-board"
        / "targets"
        / component(target_id, field="target_id")
        / f"{component(tenant, field='tenant')}__{component(platform_project, field='platform_project')}"
        / "apps"
        / "problem-board@1-0"
        / "hosts"
        / component(host_id, field="host_id")
    )


def _repository_url(value: Any, *, alias: str) -> str:
    url = str(value or "").strip()
    if not url.startswith(("https://", "http://", "ssh://", "git@")):
        raise DomainError(
            "work_repository_url_invalid",
            "Repository URLs are remote locations (https://, ssh://, or git@), never local paths.",
            details={"alias": alias},
        )
    return url


def initialize_host_config(
    *,
    target_id: str,
    endpoint: str,
    tenant: str,
    platform_project: str,
    host_id: str = "",
    host_label: str = "Local machine",
    allowed_roots: Sequence[str],
    source_repositories: Mapping[str, str],
    source_repository_urls: Mapping[str, str] | None = None,
    config_path: str | Path | None = None,
    state_root: str | Path | None = None,
    connection_hub_state_root: str | Path | None = None,
    idle_reconcile_ceiling_seconds: int | None = None,
) -> HostRelayConfig:
    selected_root = Path(state_root).expanduser().resolve() if state_root else None
    pointer = default_pointer_path(state_root=selected_root)
    target_pointer = target_pointer_path(
        target_id=target_id,
        tenant=tenant,
        platform_project=platform_project,
        state_root=selected_root,
    )
    existing_pointer = read_json(target_pointer, required=False)
    if not existing_pointer:
        existing_pointer = read_json(pointer, required=False)
    existing_path = str(existing_pointer.get("config") or "").strip()
    if not config_path and not host_id and existing_path:
        candidate_path = Path(existing_path).expanduser().resolve()
        if candidate_path.exists():
            candidate = HostRelayConfig.load(candidate_path)
            requested_target = (
                component(target_id, field="target_id"),
                _endpoint(endpoint),
                component(tenant, field="tenant"),
                component(platform_project, field="platform_project"),
            )
            existing_target = (
                candidate.target_id,
                candidate.endpoint,
                candidate.tenant,
                candidate.platform_project,
            )
            requested_roots = tuple(
                sorted(str(Path(root).expanduser().resolve()) for root in allowed_roots)
            )
            requested_repositories = tuple(
                sorted(
                    (
                        component(str(alias), field="repository alias"),
                        str(Path(root).expanduser().resolve()),
                    )
                    for alias, root in source_repositories.items()
                )
            )
            if requested_target == existing_target:
                if (
                    requested_roots != candidate.allowed_roots
                    or requested_repositories != candidate.source_repositories
                ):
                    raise DomainError(
                        "work_relay_config_update_required",
                        "This target is already configured with different local roots; use host configure after reviewing the authority change.",
                        status=409,
                        details={"config": str(candidate_path)},
                    )
                atomic_write_json(
                    pointer,
                    {
                        "schema": HOST_CONFIG_POINTER_SCHEMA,
                        "config": str(candidate_path),
                        "updated_at": utc_now(),
                    },
                )
                atomic_write_json(
                    target_pointer,
                    {
                        "schema": HOST_CONFIG_POINTER_SCHEMA,
                        "config": str(candidate_path),
                        "updated_at": utc_now(),
                    },
                )
                return candidate
    selected_host_id = component(
        host_id or f"host-{uuid.uuid4().hex[:12]}", field="host_id"
    )
    host_root = host_state_path(
        target_id=target_id,
        tenant=tenant,
        platform_project=platform_project,
        host_id=selected_host_id,
        state_root=selected_root,
    )
    path = Path(config_path).expanduser().resolve() if config_path else host_root / "relay.json"
    if path.exists():
        raise DomainError(
            "work_relay_config_exists",
            "This host relay is already configured; inspect or update the existing record.",
            status=409,
            details={"config": str(path)},
        )
    value: dict[str, Any] = {
        "schema": HOST_CONFIG_SCHEMA,
        "target": {
            "id": target_id,
            "endpoint": endpoint,
            "tenant": tenant,
            "project": platform_project,
            "bundle_id": "problem-board@1-0",
        },
        "field_root": str(host_root / "field"),
        "allowed_roots": [str(Path(root).expanduser().resolve()) for root in allowed_roots],
        "connection_hub": {
            "state_root": str(
                Path(connection_hub_state_root).expanduser().resolve()
                if connection_hub_state_root
                else host_root / "connection-hub"
            )
        },
        "host": {"id": selected_host_id, "label": host_label, "kind": "local"},
        "relay": {
            "id": f"relay-{selected_host_id}",
            "reconcile_ceiling_seconds": 30,
            "idle_reconcile_ceiling_seconds": int(idle_reconcile_ceiling_seconds or 120),
        },
        "receiver_policy": {
            "allowed_control_kinds": list(DEFAULT_CONTROL_KINDS),
            "allowed_peer_workers": list(DEFAULT_ALLOWED_PEER_WORKERS),
            "max_control_bytes": 65536,
            "allow_session_resume_view": True,
        },
        "journal_workspace": {
            "root": str(host_root / "journals"),
            "source_repositories": {
                str(alias): str(Path(root).expanduser().resolve())
                for alias, root in source_repositories.items()
            },
            "source_repository_urls": {
                str(alias): _repository_url(url, alias=str(alias))
                for alias, url in dict(source_repository_urls or {}).items()
            },
            "create_missing_home": False,
        },
        "workers": [],
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    config = HostRelayConfig.from_mapping(value, path=path)
    atomic_write_json(path, value)
    atomic_write_json(
        pointer,
        {"schema": HOST_CONFIG_POINTER_SCHEMA, "config": str(path), "updated_at": utc_now()},
    )
    atomic_write_json(
        target_pointer,
        {"schema": HOST_CONFIG_POINTER_SCHEMA, "config": str(path), "updated_at": utc_now()},
    )
    return config


def update_host_config(
    config_path: str | Path,
    *,
    endpoint: str = "",
    connection_hub_state_root: str | Path | None = None,
    host_label: str = "",
    add_allowed_roots: Sequence[str] = (),
    source_repositories: Mapping[str, str] | None = None,
    source_repository_urls: Mapping[str, str] | None = None,
    allowed_control_kinds: Sequence[str] | None = None,
    allowed_peer_workers: Sequence[str] | None = None,
    max_control_bytes: int | None = None,
    allow_session_resume_view: bool | None = None,
    reconcile_ceiling_seconds: int | None = None,
    idle_reconcile_ceiling_seconds: int | None = None,
    create_missing_journal_home: bool | None = None,
    agent_workspace_root: str | Path | None = None,
) -> HostRelayConfig:
    """Apply one explicit, non-secret host configuration revision."""

    path = Path(config_path).expanduser().resolve()
    with exclusive_lock(path.with_suffix(f"{path.suffix}.lock")):
        value = read_json(path)
        current = HostRelayConfig.from_mapping(value, path=path)
        if endpoint:
            selected_endpoint = _endpoint(endpoint)
            if selected_endpoint != current.endpoint:
                value["target"]["endpoint"] = selected_endpoint
                profile_scope = "|".join(
                    (
                        selected_endpoint,
                        current.tenant,
                        current.platform_project,
                        current.bundle_id,
                    )
                )
                for worker in value.get("workers") or []:
                    identity = WorkerSessionIdentity.create(
                        worker.get("runtime_kind"), worker.get("runtime_session_id")
                    )
                    worker["profile"] = default_profile_name(identity, profile_scope)
                    if worker.get("state") != "disabled":
                        worker["state"] = "pending_authorization"
        if connection_hub_state_root is not None:
            value.setdefault("connection_hub", {})["state_root"] = str(
                _absolute(connection_hub_state_root, "connection_hub.state_root")
            )
        if host_label:
            value["host"]["label"] = _required(host_label, "host.label")
        roots = set(current.allowed_roots)
        roots.update(str(_absolute(root, "allowed_root")) for root in add_allowed_roots)
        value["allowed_roots"] = sorted(roots)
        repositories = dict(current.source_repositories)
        for alias, root in (source_repositories or {}).items():
            repositories[component(str(alias), field="repository alias")] = str(
                _absolute(root, f"source_repositories.{alias}")
            )
        # A read root set on an alias stays with it when its root changes.
        value["journal_workspace"]["source_repositories"] = dict(
            sorted(
                repository_entries(repositories.items(), current.source_read_roots).items()
            )
        )
        urls = dict(current.source_repository_urls)
        for alias, url in (source_repository_urls or {}).items():
            urls[component(str(alias), field="repository alias")] = _repository_url(url, alias=str(alias))
        value["journal_workspace"]["source_repository_urls"] = dict(sorted(urls.items()))
        if allowed_control_kinds is not None:
            value["receiver_policy"]["allowed_control_kinds"] = sorted(
                {str(item).strip() for item in allowed_control_kinds if str(item).strip()}
            )
        if allowed_peer_workers is not None:
            value["receiver_policy"]["allowed_peer_workers"] = sorted(
                {
                    str(item).strip().lower()
                    for item in allowed_peer_workers
                    if str(item).strip()
                }
            )
        if max_control_bytes is not None:
            value["receiver_policy"]["max_control_bytes"] = int(max_control_bytes)
        if allow_session_resume_view is not None:
            value["receiver_policy"]["allow_session_resume_view"] = bool(
                allow_session_resume_view
            )
        if reconcile_ceiling_seconds is not None:
            value["relay"]["reconcile_ceiling_seconds"] = int(reconcile_ceiling_seconds)
        if idle_reconcile_ceiling_seconds is not None:
            value["relay"]["idle_reconcile_ceiling_seconds"] = int(idle_reconcile_ceiling_seconds)
        if create_missing_journal_home is not None:
            value["journal_workspace"]["create_missing_home"] = bool(
                create_missing_journal_home
            )
        if agent_workspace_root is not None:
            if not str(agent_workspace_root).strip():
                # An empty value clears the setting: the first approved root again.
                value.pop("agent_workspace", None)
            else:
                chosen = _absolute(str(agent_workspace_root), "agent_workspace.root")
                if not any(is_inside(chosen, root) for root in value.get("allowed_roots") or []):
                    raise DomainError(
                        "work_agent_workspace_root_outside_allowed_roots",
                        "The agent workspace root must lie inside an approved work root "
                        "(`pb host configure --add-allow-root <path>` first).",
                        status=400,
                        details={"agent_workspace_root": str(chosen)},
                    )
                value["agent_workspace"] = {"root": str(chosen)}
        value["updated_at"] = utc_now()
        updated = HostRelayConfig.from_mapping(value, path=path)
        atomic_write_json(path, value)
        return updated


def resolve_host_config_path(value: str | Path | None = None) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    env_path = str(os.environ.get("PROBLEM_BOARD_CONFIG") or "").strip()
    if env_path:
        return Path(env_path).expanduser().resolve()
    pointer = read_json(default_pointer_path(), required=False)
    path = str(pointer.get("config") or "").strip()
    if path:
        return Path(path).expanduser().resolve()
    raise DomainError(
        "work_relay_config_required",
        "Problem Board has no default machine configuration. Run the setup procedure first.",
    )


# One folder name: letters, digits, `.`, `_`, `-` and `@` (an alias such as
# `claude-app@host1` is its own folder name); anything else uses the
# agent's stable name.
_SAFE_FOLDER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,63}")


def is_inside(path: str | Path, root: str | Path) -> bool:
    """Whether ``path`` resolves to ``root`` or below it (``..`` and links resolved)."""

    try:
        resolved = Path(path).expanduser().resolve()
        base = Path(root).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    return resolved == base or resolved.is_relative_to(base)


def default_working_directory(allowed_roots: Sequence[str], *, alias: str, worker_name: str) -> str:
    """An agent's own workspace when none is recorded: ``<root>/<alias>``.

    The first root given, one folder per agent: its alias when that is one safe
    folder name, else its stable name. Never the directory a session happened
    to start in: a Claude Code agent started in a shared checkout was handed
    that checkout (operator, 2026-09-26: "it had to be a dedicated
    workspace"). Empty when there is no root, or when no name stays inside it.
    """

    roots = [str(root) for root in allowed_roots if str(root).strip()]
    if not roots:
        return ""
    root = Path(roots[0])
    for name in (str(alias or "").strip(), str(worker_name or "").strip()):
        if name and _SAFE_FOLDER.fullmatch(name) and name not in {".", ".."}:
            candidate = root / name
            if is_inside(candidate, root) and candidate.resolve() != root.resolve():
                return str(candidate)
    return ""


def agent_workspace(
    config: "HostRelayConfig", *, recorded: str, alias: str, worker_name: str
) -> tuple[str, str, str]:
    """This agent's workspace, how it was chosen, and a plain note when a recorded folder is refused.

    One answer for ``listen``, ``context``, ``workspace-report`` and
    enrollment (W262): a recorded folder inside the host's agent workspace
    root is kept; any other recorded folder is not the workspace, and the
    agent's own folder under the root is named instead.
    """

    root = config.effective_agent_workspace_root
    recorded = str(recorded or "").strip()
    derived = default_working_directory([root] if root else [], alias=alias, worker_name=worker_name)
    if recorded and not root:
        return recorded, "recorded", ""
    if recorded and is_inside(recorded, root) and (Path(recorded).exists() or recorded == derived):
        # Kept only while it exists (or is the agent's own folder, not
        # created yet): a folder deleted since, or recorded under an old
        # alias, gives way to <root>/<alias> (rehearsal, 2026-09-26).
        return recorded, "recorded", ""
    if recorded and root and not is_inside(recorded, root):
        note = (
            f"The folder this session recorded ({recorded}) is outside the host's agent "
            f"workspace root ({root}); it is not your workspace."
        )
    elif recorded:
        note = f"The folder this session recorded ({recorded}) no longer exists; it is not your workspace."
    else:
        note = ""
    return derived, ("host_root" if derived else ""), note


def enroll_worker_channel(
    config_path: str | Path,
    *,
    identity: WorkerSessionIdentity,
    profile: str = "",
    worker_alias: str = "",
    capabilities: Sequence[str] = (),
    authorized: bool = False,
    working_directory: str = "",
) -> WorkerChannelConfig:
    path = Path(config_path).expanduser().resolve()
    lock = path.with_suffix(f"{path.suffix}.lock")
    with exclusive_lock(lock):
        value = read_json(path)
        config = HostRelayConfig.from_mapping(value, path=path)
        existing = config.worker(identity)
        selected_profile = component(
            profile
            or (
                existing.profile
                if existing
                else default_profile_name(identity, config.profile_scope)
            ),
            field="profile",
        )
        for candidate in config.workers:
            if (
                candidate.profile == selected_profile
                and candidate.worker_identity != identity.worker_identity
            ):
                raise DomainError(
                    "work_relay_profile_bound",
                    "Each coding-agent session requires its own Connection Hub profile.",
                    status=409,
                    details={"profile": selected_profile},
                )
        alias = normalize_worker_alias(
            worker_alias or (existing.worker_alias if existing else "")
        )
        requested_working_directory = str(working_directory or "").strip()
        requested_path = (
            str(Path(requested_working_directory).expanduser().resolve())
            if requested_working_directory
            else ""
        )
        agent_root = config.effective_agent_workspace_root
        # The folder a session enrolls from counts only inside the host's agent
        # workspace root; otherwise the recorded one is checked the same way,
        # and failing both, the agent's own folder under the root (W262).
        candidate = (
            requested_path
            if requested_path and agent_root and is_inside(requested_path, agent_root)
            else (existing.working_directory if existing else "")
        )
        selected_working_directory, _source, _note = agent_workspace(
            config, recorded=candidate, alias=alias, worker_name=identity.worker_name
        )
        channel = WorkerChannelConfig(
            runtime_kind=identity.runtime_kind,
            runtime_session_id=identity.runtime_session_id,
            worker_identity=identity.worker_identity,
            worker_name=identity.worker_name,
            profile=selected_profile,
            worker_alias=alias,
            capabilities=tuple(sorted(
                set(existing.capabilities if existing else ())
                | {str(item).strip() for item in capabilities if str(item).strip()}
            )),
            state=(
                "active"
                if authorized
                else "pending_authorization"
                if existing is None or existing.state == "disabled"
                else existing.state
            ),
            working_directory=selected_working_directory,
        )
        rows = [
            worker.to_mapping()
            for worker in config.workers
            if worker.worker_identity != identity.worker_identity
        ]
        rows.append(channel.to_mapping())
        value["workers"] = sorted(rows, key=lambda item: str(item["worker_identity"]))
        value["updated_at"] = utc_now()
        atomic_write_json(path, value)
        return channel


def set_worker_channel_state(
    config_path: str | Path,
    *,
    identity: WorkerSessionIdentity,
    state: str,
    expected_state: str | None = None,
) -> WorkerChannelConfig:
    normalized = str(state or "").strip().lower()
    if normalized not in {"active", "pending_authorization", "disabled"}:
        raise DomainError(
            "work_relay_config_invalid",
            "Worker channel state must be active, pending_authorization, or disabled.",
        )
    path = Path(config_path).expanduser().resolve()
    with exclusive_lock(path.with_suffix(f"{path.suffix}.lock")):
        value = read_json(path)
        config = HostRelayConfig.from_mapping(value, path=path)
        existing = config.worker(identity)
        if existing is None:
            raise DomainError(
                "work_relay_worker_not_enrolled",
                "This coding-agent session is not enrolled on the host relay.",
                status=404,
            )
        expected = str(expected_state or "").strip().lower()
        if expected and existing.state != expected:
            raise DomainError(
                "work_relay_worker_state_changed",
                "The worker channel state changed before this transition completed.",
                status=409,
                details={"expected_state": expected, "current_state": existing.state},
            )
        updated = WorkerChannelConfig(
            runtime_kind=existing.runtime_kind,
            runtime_session_id=existing.runtime_session_id,
            worker_identity=existing.worker_identity,
            worker_name=existing.worker_name,
            profile=existing.profile,
            worker_alias=existing.worker_alias,
            capabilities=existing.capabilities,
            state=normalized,
            working_directory=existing.working_directory,
        )
        value["workers"] = [
            updated.to_mapping()
            if worker.worker_identity == identity.worker_identity
            else worker.to_mapping()
            for worker in config.workers
        ]
        value["updated_at"] = utc_now()
        atomic_write_json(path, value)
        return updated


__all__ = [
    "DEFAULT_CONTROL_KINDS",
    "HOST_CONFIG_POINTER_SCHEMA",
    "HOST_CONFIG_SCHEMA",
    "HostRelayConfig",
    "WorkerChannelConfig",
    "client_runtime_root",
    "default_pointer_path",
    "enroll_worker_channel",
    "host_state_path",
    "initialize_host_config",
    "resolve_host_config_path",
    "set_worker_channel_state",
    "target_pointer_path",
    "update_host_config",
]

"""Which first-run step this machine and this session are at, read in one call (W249).

A new user installs `pb`, starts a coding agent and tells it to use the worker
skill. The agent then has to know where the user is: nothing configured, a
machine that is set up while this session attends nothing, or a session that is
already at work. Before this, that took four commands with four shapes
(`worker whoami`, `host inspect`, `relay-service status`, `worker inspect`),
and the skill told the agent to report an unconfigured machine and stop.

`pb status` reads the same sources those commands read, never writes, never
calls the server, and works on a machine with no configuration at all. It
names one of three states and the next step for it, and says which steps are
the user's to approve. The guided flow in the skill's first-run reference is
keyed to these names.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from .host_config import (
    HostRelayConfig,
    client_runtime_root,
    default_pointer_path,
    resolve_host_config_path,
)
from .io import read_json
from .relay_pacing import channel_reconnect_state
from .relay_source import describe_source
from .relay_service import CLIENT_SOURCE_PATHS
from ..contract.errors import DomainError

SCHEMA = "problem-board.first-run-status.v1"

MACHINE_NOT_CONFIGURED = "machine_not_configured"
SESSION_NOT_ATTENDING = "session_not_attending"
SESSION_ATTENDING = "session_attending"
# Configured and installed, but the relay process is not running. Distinct
# from machine_not_configured: the answer is a start, never a reinstall, and
# the relay may have been stopped on purpose.
RELAY_STOPPED = "relay_stopped"
# The channel is enrolled and authorized, but the relay is reconnecting it
# after a failure, so it cannot reach the board right now. Distinct from
# attending: its next step is to wait for the relay's own retry (W265).
SESSION_RECONNECTING = "session_reconnecting"
STATES = (
    MACHINE_NOT_CONFIGURED,
    RELAY_STOPPED,
    SESSION_NOT_ATTENDING,
    SESSION_RECONNECTING,
    SESSION_ATTENDING,
)

# `inspect_profile_metadata` reports this when the profile holds its authorization
# metadata (authorization.PROFILE_METADATA_PRESENT). Anything else needs `pb worker authorize`.
AUTHORIZED = "relay_observation_pending"
CHANNEL_ACTIVE = "active"

# The one home for what endpoint, tenant, project and target name mean.
IDENTIFIERS_DOC = (
    "the installed problem-board-worker reference first-run.md, section "
    "\"What The Setup Coordinates Mean\""
)


def configured_targets(root: Path | None = None) -> list[dict[str, Any]]:
    """Every relay configuration on this machine, as non-secret coordinates."""

    base = (root or client_runtime_root()) / "problem-board" / "targets"
    targets: list[dict[str, Any]] = []
    if not base.is_dir():
        return targets
    for path in sorted(base.glob("*/*/apps/*/hosts/*/relay.json")):
        try:
            config = HostRelayConfig.load(path)
        except Exception as exc:  # a damaged file is reported, never hidden
            targets.append({"config": str(path), "readable": False, "error": type(exc).__name__})
            continue
        targets.append(
            {
                "config": str(path),
                "readable": True,
                "target_id": config.target_id,
                "endpoint": config.endpoint,
                "tenant": config.tenant,
                "platform_project": config.platform_project,
                "host_id": config.host_id,
                "host_label": config.host_label,
                "workers": len(config.workers),
            }
        )
    return targets


def _default_config(explicit: str | None) -> str:
    if explicit:
        return str(Path(explicit).expanduser().resolve())
    try:
        return str(resolve_host_config_path(None))
    except DomainError:
        pointer = read_json(default_pointer_path(), required=False)
        return str(pointer.get("config") or "")


def _relay(config_path: str) -> dict[str, Any]:
    try:
        from .relay_service import RelayService

        status = RelayService.create(config_path).status()
    except Exception as exc:
        return {"installed": False, "running": False, "error": str(getattr(exc, "code", "") or type(exc).__name__)}
    return {
        "installed": bool(status.get("installed")),
        "running": bool(status.get("running")),
        "service_id": str(status.get("service_id") or ""),
        "source_mode": str(status.get("source_mode") or ""),
        "source": dict(status.get("source") or {}),
        "startup_source": dict(
            (status.get("startup_record") or {}).get("source") or {}
        ),
    }


def _client_source() -> dict[str, Any]:
    source = describe_source(Path(__file__), scope_paths=CLIENT_SOURCE_PATHS)
    return {
        "pinned": source.get("mode") in {"released", "snapshot"},
        "source": source,
    }


def _session(config: HostRelayConfig, identity: Any, read_worker: Callable[[str], Mapping[str, Any]], inspect_profile: Callable[..., Mapping[str, Any]]) -> dict[str, Any]:
    channel = config.worker(identity)
    session: dict[str, Any] = {
        "worker_name": identity.worker_name,
        "worker_identity": identity.worker_identity,
        "enrolled": channel is not None,
    }
    if channel is None:
        return session
    session["alias"] = channel.worker_alias
    session["profile"] = channel.profile
    # The relay's own record of whether this channel works: `active`,
    # `pending_authorization` (its credential was refused, it cannot reach the
    # board) or `disabled`. Attendance recorded earlier does not mean it works now.
    session["channel_state"] = channel.state
    try:
        authorization = dict(inspect_profile(config, channel.profile))
    except Exception as exc:
        authorization = {"state": str(getattr(exc, "code", "") or type(exc).__name__)}
    session["authorization"] = str(authorization.get("state") or "")
    session["card"] = str(authorization.get("access_id") or "")
    try:
        worker = dict(read_worker(identity.worker_name) or {})
    except Exception:
        worker = {}
    session["attended_project_refs"] = list(worker.get("attended_project_refs") or [])
    session["control_plane_state"] = str(worker.get("control_plane_state") or "")
    return session


def _next_step(state: str, *, config: str, relay: Mapping[str, Any], session: Mapping[str, Any] | None) -> dict[str, Any]:
    if state == MACHINE_NOT_CONFIGURED and not config:
        return {
            "step": "configure_target",
            "command": "pb setup --target-id <label> --endpoint <url> --tenant <tenant> --platform-project <project> --host-id <id> --host-label <label>",
            "approval": "user",
            "explain": IDENTIFIERS_DOC,
        }
    if state == SESSION_RECONNECTING and session is not None:
        connection = dict(session.get("connection") or {})
        return {
            "step": "wait_for_reconnect",
            "command": "",
            "approval": "none",
            "explain": (
                "The relay is reconnecting this channel after "
                f"{connection.get('reason') or 'a failure'} (attempt "
                f"{connection.get('attempts') or 0}). It retries on its own at "
                f"{connection.get('next_attempt_at') or 'its next cycle'}. Until then "
                "pb coordinate refuses at once with work_coordinate_channel_reconnecting."
            ),
        }
    if state == RELAY_STOPPED:
        return {
            "step": "start_relay",
            "command": "pb relay-service start",
            "approval": "user",
            "explain": "The relay is installed but not running. It may have been stopped on purpose (for maintenance or a coordinated restart), so ask before starting it.",
        }
    if state == MACHINE_NOT_CONFIGURED:
        return {
            "step": "install_relay",
            "command": "pb relay-service install",
            "approval": "user",
            "explain": "procedures/first-time-setup.md, section 4. Start The Machine Relay",
        }
    if state == SESSION_NOT_ATTENDING and session is None:
        return {
            "step": "identify_session",
            "command": "pb status --runtime-kind <codex|claude-code> --runtime-session-id <session>",
            "approval": "none",
            "explain": "The machine is set up. Run status again with this agent session's identity to see its own step.",
        }
    if state == SESSION_NOT_ATTENDING and not session.get("enrolled"):
        return {
            "step": "enroll_session",
            "command": "pb worker listen --runtime-kind <kind> --runtime-session-id <session> --alias <alias>",
            "approval": "none",
            "explain": "procedures/first-time-setup.md, section 5. Enroll One User-Started Agent Session",
        }
    if state == SESSION_NOT_ATTENDING and session.get("channel_state") == "disabled":
        return {
            "step": "channel_disabled",
            "command": "",
            "approval": "user",
            "explain": "The operator disabled this worker's channel on this machine. Ask them whether it should be enabled again.",
        }
    if state == SESSION_NOT_ATTENDING and (
        session.get("authorization") != AUTHORIZED
        or session.get("channel_state") == "pending_authorization"
    ):
        return {
            "step": "authorize_profile",
            "command": f"pb worker authorize {session.get('profile') or '<profile>'}",
            "approval": "user",
            "explain": "procedures/first-time-setup.md, section 6. Authorize That Worker's Profile",
        }
    if state == SESSION_NOT_ATTENDING:
        return {
            "step": "attend_project",
            "command": "",
            "approval": "user",
            "explain": "procedures/first-time-setup.md, section 8. Create And Exercise The First Project (a project links this worker in the board)",
        }
    return {"step": "none", "command": "", "approval": "none", "explain": ""}


def first_run_status(
    *,
    config: str | None = None,
    identity: Any = None,
    targets_root: Path | None = None,
    relay_reader: Callable[[str], Mapping[str, Any]] | None = None,
    worker_reader: Callable[[HostRelayConfig], Callable[[str], Mapping[str, Any]]] | None = None,
    profile_inspector: Callable[..., Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Read the machine and session state and name the first-run step. Read-only."""

    targets = configured_targets(targets_root)
    config_path = _default_config(config)
    relay: dict[str, Any] = {"installed": False, "running": False}
    loaded: HostRelayConfig | None = None
    if config_path and Path(config_path).is_file():
        try:
            loaded = HostRelayConfig.load(config_path)
        except Exception:
            loaded = None
        relay = dict((relay_reader or _relay)(config_path))
    else:
        config_path = ""

    session: dict[str, Any] | None = None
    if loaded is not None and identity is not None:
        if worker_reader is None:
            from .store import SharedFieldStore

            reader = SharedFieldStore(loaded.field_root).read_worker
        else:
            reader = worker_reader(loaded)
        if profile_inspector is None:
            from .authorization import inspect_profile_metadata

            inspector = inspect_profile_metadata
        else:
            inspector = profile_inspector
        session = _session(loaded, identity, reader, inspector)
        if session.get("channel_state") == CHANNEL_ACTIVE:
            # The config says active; the relay's pacing record says whether
            # the channel is actually open now (W265).
            reconnect = channel_reconnect_state(config_path, identity.worker_name)
            if reconnect is not None:
                session["channel_state"] = "reconnecting"
                session["connection"] = reconnect

    if loaded is None or not relay.get("installed"):
        state = MACHINE_NOT_CONFIGURED
    elif not relay.get("running"):
        state = RELAY_STOPPED
    elif session is not None and session.get("channel_state") == "reconnecting":
        state = SESSION_RECONNECTING
    elif (
        session is not None
        and session.get("channel_state") == CHANNEL_ACTIVE
        and session.get("attended_project_refs")
    ):
        state = SESSION_ATTENDING
    else:
        state = SESSION_NOT_ATTENDING

    result: dict[str, Any] = {
        "schema": SCHEMA,
        "state": state,
        "machine": {
            "default_config": config_path,
            "targets": targets,
            "relay": relay,
            "client": _client_source(),
        },
        "session": session,
        "next": _next_step(state, config=config_path, relay=relay, session=session),
    }
    if loaded is not None:
        result["machine"]["default_target"] = {
            "target_id": loaded.target_id,
            "endpoint": loaded.endpoint,
            "tenant": loaded.tenant,
            "platform_project": loaded.platform_project,
            "host_id": loaded.host_id,
            "host_label": loaded.host_label,
        }
    return result

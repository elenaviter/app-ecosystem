from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..contract.errors import DomainError
from ..contract.worker_operation_contract import (
    PROBLEM_BOARD_OPERATIONS,
    required_grants_for_operation,
)
from .credential_refusal import RECONNECT_CODES, requires_browser_reconnect as _requires_browser_reconnect
from .host_config import HostRelayConfig, WorkerChannelConfig


PROFILE_METADATA_ABSENT = "profile_metadata_absent"
PROFILE_METADATA_PRESENT = "relay_observation_pending"


def _worker_client_name(channel: WorkerChannelConfig) -> str:
    identity = channel.worker_identity
    if channel.worker_alias:
        identity = (
            f"{channel.runtime_kind}:{channel.worker_alias}:"
            f"{channel.runtime_session_id}"
        )
    return f"Connection Hub CLI · problem_board · {identity}"


def _worker_client_metadata(
    config: HostRelayConfig, channel: WorkerChannelConfig
) -> dict[str, str]:
    """Public correlation facts asserted by this enrolled worker.

    Connection Hub renders and searches these values but never treats them as
    worker, machine, or session authority.
    """

    return {
        "kdcube_app_id": config.bundle_id,
        "kdcube_credential_use": "multi_resource",
        "kdcube_target_id": config.target_id,
        "kdcube_machine_id": config.host_id,
        "kdcube_machine_label": config.host_label,
        "kdcube_relay_id": config.relay_id,
        "kdcube_agent_provider": channel.runtime_kind,
        "kdcube_agent_id": channel.worker_identity,
        "kdcube_agent_session_id": channel.runtime_session_id,
        "kdcube_worker_id": channel.worker_name,
        "kdcube_worker_alias": channel.worker_alias,
    }


def _connection_hub_error(exc: BaseException) -> DomainError:
    code = str(getattr(exc, "code", "") or "work_relay_authorization_failed")
    message = str(getattr(exc, "message", "") or str(exc) or "Worker authorization failed.")
    return DomainError(code, message, status=409)


def _device_authorization_presenter(prompt: Any) -> None:
    """Print the public RFC 8628 handoff without exposing the device secret."""

    sys.stderr.write("Open this verification URL in a browser on any device:\n")
    sys.stderr.write(f"{prompt.verification_uri}\n")
    sys.stderr.write("Enter this code:\n")
    sys.stderr.write(f"{prompt.user_code}\n")
    if prompt.verification_uri_complete:
        sys.stderr.write("Direct verification URL:\n")
        sys.stderr.write(f"{prompt.verification_uri_complete}\n")
    sys.stderr.flush()


def _profile_result(
    *,
    channel: WorkerChannelConfig,
    profile: Any,
    probe: Any,
    action: str,
    message: str,
    already_authorized: bool,
    recovered_profile_metadata: bool = False,
    previous_access_id: str = "",
) -> dict[str, Any]:
    return {
        "authorized": True,
        "already_authorized": already_authorized,
        "authorization_action": action,
        "card_preserved": action in {"already_authorized", "reconnected"},
        "previous_access_id": previous_access_id or None,
        "message": message,
        "recovered_profile_metadata": recovered_profile_metadata,
        "profile": {
            "name": profile.name,
            "endpoint": profile.endpoint,
            "access_id": profile.access_id,
            "auth_type": profile.auth_type,
        },
        "probe": probe.to_dict(),
        "worker": channel.to_mapping(),
        "activation": "The login relay will prove this profile and activate the worker channel.",
    }


def inspect_profile_metadata(
    config: HostRelayConfig, profile_name: str
) -> dict[str, Any]:
    """Inspect non-secret profile metadata without touching native credentials."""

    try:
        from connection_hub_cli.paths import StatePaths
        from connection_hub_cli.state import ProfileStore
    except ImportError:
        return {
            "state": "connection_hub_cli_not_installed",
            "profile": profile_name,
            "metadata_present": False,
        }
    paths = (
        StatePaths(config.connection_hub_state_root)
        if config.connection_hub_state_root is not None
        else StatePaths.default()
    )
    try:
        profile = ProfileStore(paths.profiles).get(profile_name)
    except Exception as exc:
        raise _connection_hub_error(exc) from exc
    if profile is None:
        return {
            "state": PROFILE_METADATA_ABSENT,
            "profile": profile_name,
            "metadata_present": False,
        }
    if profile.endpoint.rstrip("/") != config.endpoint.rstrip("/"):
        raise DomainError(
            "work_relay_profile_endpoint_mismatch",
            "The worker profile points at a different governed MCP endpoint.",
            status=409,
            details={
                "profile": profile_name,
                "profile_endpoint": profile.endpoint,
                "target_endpoint": config.endpoint,
            },
        )
    return {
        "state": PROFILE_METADATA_PRESENT,
        "profile": profile.name,
        "metadata_present": True,
        "auth_type": profile.auth_type,
        "access_id": profile.access_id,
        "client_id": (
            str(profile.oauth.client_id)
            if getattr(profile, "oauth", None) is not None
            else ""
        ),
    }


def authorization_command(
    profile_name: str, *, config_path: str | Path | None = None
) -> list[str]:
    command = ["pb", "worker", "authorize", profile_name]
    if config_path is not None:
        command.extend(["--config", str(Path(config_path).expanduser().resolve())])
    return command


def _matching_channel(
    config: HostRelayConfig, profile_name: str
) -> WorkerChannelConfig:
    matches = [channel for channel in config.workers if channel.profile == profile_name]
    if len(matches) != 1:
        raise DomainError(
            "work_relay_authorization_profile_not_enrolled",
            "Authorization is available only for a profile assigned to one worker channel on this host.",
            status=404,
            details={"profile": profile_name},
        )
    channel = matches[0]
    if channel.state == "disabled":
        raise DomainError(
            "work_relay_worker_disabled",
            "Enable this worker channel before authorizing it.",
            status=409,
            details={"worker_name": channel.worker_name},
        )
    return channel


def _manual_browser_opener(url: str) -> bool:
    sys.stderr.write("Open this authorization URL in a browser:\n")
    sys.stderr.write(f"{url}\n")
    sys.stderr.flush()
    return True


def _sibling_profile_roots(config_path: Path, configured_root: Path) -> list[Path]:
    """Find app-scoped profile stores left beside the configured host store."""

    host_root = config_path.parent.resolve()
    selected = configured_root.expanduser().resolve()
    try:
        children = sorted(host_root.iterdir(), key=lambda value: value.name)
    except OSError as exc:
        raise DomainError(
            "work_relay_profile_recovery_scan_failed",
            "The host profile-store boundary could not be inspected safely.",
            status=409,
        ) from exc
    candidates = [
        child
        for child in children
        if child.is_dir()
        and not child.is_symlink()
        and child.resolve() != selected
        and (child / "profiles.json").is_file()
    ]
    if len(candidates) > 128:
        raise DomainError(
            "work_relay_profile_recovery_scan_too_broad",
            "The host contains too many sibling profile stores for automatic recovery.",
            status=409,
        )
    return candidates


async def _recover_sibling_profile(
    *,
    config_path: Path,
    configured_paths: Any,
    config: HostRelayConfig,
    profile_name: str,
    build_services: Any,
    target_services: Any,
) -> tuple[Any, Any] | None:
    """Recover one proved profile after an app-scoped state-root mistake."""

    from connection_hub_cli.paths import StatePaths

    matches: list[tuple[Any, Any]] = []
    for root in _sibling_profile_roots(config_path, configured_paths.root):
        try:
            source_services = build_services(paths=StatePaths(root))
            profile = source_services.profiles.get(profile_name)
        except Exception as exc:
            raise DomainError(
                "work_relay_profile_recovery_scan_failed",
                "A sibling profile store could not be inspected safely.",
                status=409,
                details={"profile": profile_name},
            ) from exc
        if profile is not None:
            matches.append((profile, source_services))
    if not matches:
        return None
    if len(matches) != 1:
        raise DomainError(
            "work_relay_profile_recovery_ambiguous",
            "More than one sibling profile store contains this worker profile.",
            status=409,
            details={
                "profile": profile_name,
                "access_ids": sorted(
                    str(profile.access_id or "") for profile, _services in matches
                ),
            },
        )
    profile, source_services = matches[0]
    if profile.endpoint.rstrip("/") != config.endpoint.rstrip("/"):
        raise DomainError(
            "work_relay_profile_recovery_endpoint_mismatch",
            "The sibling worker profile belongs to a different governed endpoint.",
            status=409,
            details={
                "profile": profile_name,
                "access_id": profile.access_id,
            },
        )
    try:
        probe = await source_services.profile_service.probe_profile(profile_name)
    except Exception as exc:
        raise DomainError(
            "work_relay_profile_recovery_unproved",
            "A sibling worker profile exists, but its native credential could not be proved. Resolve its recorded Card before requesting another consent.",
            status=409,
            details={
                "profile": profile_name,
                "access_id": profile.access_id,
                "cause": str(getattr(exc, "code", "") or type(exc).__name__),
            },
        ) from exc
    try:
        target_services.profiles.add(profile)
    except Exception as exc:
        if str(getattr(exc, "code", "") or "") != "profile_exists":
            raise _connection_hub_error(exc) from exc
        profile = target_services.profiles.require(profile_name)
    return profile, probe



def worker_scope_request() -> str:
    """The claims this app's own operations need, and not one more.

    A deployment advertises the union of every app installed on it. When the
    401 challenge carries no scope, Connection Hub CLI falls back to that whole
    advertised set, so a coding-agent worker authorized by our documented
    procedure was being issued a card carrying claims for services it never
    calls: posting to LinkedIn, deleting press content, posting to Slack.

    Two things are wrong with that. The operator approving the consent cannot
    tell which claims the agent actually requires, so the screen stops being a
    decision and becomes a formality. And a card is a standing capability, so
    an over-broad one is a standing risk that nothing later narrows.

    Derived from the operation contract rather than written down, because a
    hand-kept list is exactly the kind of declaration that drifts out of step
    with what the app really does and is never noticed until it matters.
    """

    grants: set[str] = set()
    for operation in PROBLEM_BOARD_OPERATIONS:
        grants |= set(required_grants_for_operation(operation))
    return " ".join(sorted(grants))


async def authorize_worker_profile(
    config_path: str | Path,
    *,
    profile_name: str,
    wait_seconds: float = 600.0,
    no_open: bool = False,
    callback_port: int | None = None,
    device: bool = False,
    replace_card: bool = False,
) -> dict[str, Any]:
    """Authorize one enrolled channel from the user's interactive desktop."""

    path = Path(config_path).expanduser().resolve()
    if device and (no_open or callback_port is not None):
        raise DomainError(
            "oauth_device_option_conflict",
            "Device authorization does not use --callback-port or --no-open.",
            status=400,
        )
    config = HostRelayConfig.load(path)
    channel = _matching_channel(config, profile_name)
    try:
        from connection_hub_cli.cli import build_services
        from connection_hub_cli.paths import StatePaths
    except ImportError as exc:
        raise DomainError(
            "connection_hub_cli_not_installed",
            "The Problem Board host command requires Connection Hub CLI.",
            status=500,
        ) from exc

    paths = (
        StatePaths(config.connection_hub_state_root)
        if config.connection_hub_state_root is not None
        else StatePaths.default()
    )
    services = build_services(paths=paths)
    sys.stderr.write(
        "Authorizing Problem Board worker "
        f"{channel.worker_alias or channel.worker_name} "
        f"({channel.runtime_kind}:{channel.runtime_session_id}) for "
        f"{config.target_id}.\n"
    )
    sys.stderr.write(
        "Browser consent grants this worker's dedicated profile; credential "
        "values remain in native custody for the login relay.\n"
    )
    sys.stderr.flush()
    existing = services.profiles.get(profile_name)
    recovered_from_sibling_store = False
    recovered_probe = None
    if existing is None:
        recovered = await _recover_sibling_profile(
            config_path=path,
            configured_paths=paths,
            config=config,
            profile_name=profile_name,
            build_services=build_services,
            target_services=services,
        )
        if recovered is not None:
            existing, recovered_probe = recovered
            recovered_from_sibling_store = True
    if existing is not None:
        if existing.endpoint.rstrip("/") != config.endpoint.rstrip("/"):
            raise DomainError(
                "work_relay_profile_endpoint_mismatch",
                "The worker profile points at a different governed MCP endpoint.",
                status=409,
                details={"profile": profile_name},
            )
        if replace_card:
            replaced_access_id = str(existing.access_id or "")
            try:
                await services.profile_service.disconnect(profile_name)
            except Exception as disconnect_error:
                raise DomainError(
                    "work_relay_profile_disconnect_required",
                    "The old worker Card could not be revoked and retired safely. Resolve that Card in Connection Hub before authorizing a replacement.",
                    status=409,
                    details={
                        "profile": profile_name,
                        "access_id": existing.access_id,
                        "cause": str(
                            getattr(disconnect_error, "code", "")
                            or type(disconnect_error).__name__
                        ),
                    },
                ) from disconnect_error
            existing = None
        else:
            try:
                probe = (
                    recovered_probe
                    if recovered_probe is not None
                    else await services.profile_service.probe_profile(profile_name)
                )
            except Exception as exc:
                cause = str(getattr(exc, "code", "") or type(exc).__name__)
                if not _requires_browser_reconnect(exc):
                    raise DomainError(
                        "work_relay_profile_reauthorization_required",
                        "This recorded worker profile could not be proved. Repair the reported connection condition before reconnecting it.",
                        status=409,
                        details={
                            "profile": profile_name,
                            "access_id": existing.access_id,
                            "cause": cause,
                        },
                    ) from exc
                oauth = services.oauth_profile_sessions
                if oauth is None:
                    raise DomainError(
                        "oauth_profiles_unavailable",
                        "OAuth-backed caller profiles are unavailable in this process.",
                        status=500,
                    )
                options: dict[str, Any] = {}
                if device:
                    options["device"] = True
                    options["device_presenter"] = _device_authorization_presenter
                elif no_open:
                    options["browser_opener"] = _manual_browser_opener
                if callback_port is not None and not device:
                    options["callback_port"] = callback_port
                try:
                    result = await oauth.reconnect(
                        profile_name,
                        timeout_seconds=max(
                            30.0,
                            min(float(wait_seconds), 1800.0),
                        ),
                        **options,
                    )
                except Exception as reconnect_error:
                    raise _connection_hub_error(reconnect_error) from reconnect_error
                if result.profile.access_id != existing.access_id:
                    raise DomainError(
                        "work_relay_reconnect_card_mismatch",
                        "Connection Hub returned a different Card for an in-place reconnect.",
                        status=409,
                        details={
                            "profile": profile_name,
                            "expected_access_id": existing.access_id,
                            "received_access_id": result.profile.access_id,
                        },
                    )
                return _profile_result(
                    channel=channel,
                    profile=result.profile,
                    probe=result.probe,
                    action="reconnected",
                    message=(
                        f"Reconnected Card {result.profile.access_id} "
                        "(grants kept)."
                    ),
                    already_authorized=False,
                    recovered_profile_metadata=recovered_from_sibling_store,
                )
            return _profile_result(
                channel=channel,
                profile=existing,
                probe=probe,
                action="already_authorized",
                message=f"Card {existing.access_id} is already connected.",
                already_authorized=True,
                recovered_profile_metadata=recovered_from_sibling_store,
            )
    else:
        replaced_access_id = ""

    oauth = services.oauth_profile_sessions
    if oauth is None:
        raise DomainError(
            "oauth_profiles_unavailable",
            "OAuth-backed caller profiles are unavailable in this process.",
            status=500,
        )
    options: dict[str, Any] = {}
    if device:
        options["device"] = True
        options["device_presenter"] = _device_authorization_presenter
    elif no_open:
        options["browser_opener"] = _manual_browser_opener
    if callback_port is not None and not device:
        options["callback_port"] = callback_port
    try:
        result = await oauth.authorize(
            name=profile_name,
            endpoint=config.endpoint,
            # Ask for this app's own claims. Passing nothing here is what
            # made the CLI fall back to every scope the deployment
            # advertises. This is the weaker of the two scope inputs on
            # purpose: a server challenge still wins, because the server
            # naming what it needs is more specific than our declaration.
            default_scope=worker_scope_request(),
            client_name=_worker_client_name(channel),
            client_metadata=_worker_client_metadata(config, channel),
            timeout_seconds=max(30.0, min(float(wait_seconds), 1800.0)),
            **options,
        )
    except Exception as exc:
        raise _connection_hub_error(exc) from exc
    action = "replaced_card" if replaced_access_id else "authorized"
    message = (
        f"Replaced Card {replaced_access_id} with {result.profile.access_id}."
        if replaced_access_id
        else f"Authorized Card {result.profile.access_id}."
    )
    return _profile_result(
        channel=channel,
        profile=result.profile,
        probe=result.probe,
        action=action,
        message=message,
        already_authorized=False,
        previous_access_id=replaced_access_id,
    )


def authorization_observation(error: BaseException) -> dict[str, Any]:
    """Classify relay credential failures without exposing credential material."""

    code = str(getattr(error, "code", "") or "")
    try:
        status = int(getattr(error, "status", 0) or 0)
    except (TypeError, ValueError):
        status = 0
    if code in {"profile_not_found", "oauth_profile_not_found"}:
        state, action, terminal = PROFILE_METADATA_ABSENT, "authorize", True
    elif code == "work_relay_profile_endpoint_mismatch":
        state, action, terminal = "profile_endpoint_mismatch", "repair_host_configuration", True
    elif code in {
        "credential_missing",
        "oauth_profile_credential_missing",
    }:
        state, action, terminal = "credential_missing", "reconnect", True
    elif _requires_browser_reconnect(error):
        state, action, terminal = "credential_expired_or_invalid", "reconnect", True
    elif code == "oauth_profile_server_changed":
        state, action, terminal = "profile_server_changed", "replace_card", True
    elif code in {
        "delegated_card_expired",
        "delegated_card_not_active",
    }:
        state, action, terminal = "delegated_card_not_active", "authorize", True
    elif code in {
        "delegated_resource_not_granted",
        "delegated_card_scope_invalid",
        "delegated_card_scope_mismatch",
    }:
        state, action, terminal = "delegated_resource_not_granted", "grant_access", True
    elif (
        "profile_store" in code
        or "oauth_profile_store" in code
        or code
        in {
            "credential_store_read_failed",
            "unavailable_keyring_backend",
            "insecure_keyring_backend",
        }
    ):
        state, action, terminal = (
            "credential_store_inaccessible",
            "repair_relay_credential_access",
            False,
        )
    else:
        state, action, terminal = "connection_unavailable", "retry", False
    return {
        "state": state,
        "error_type": type(error).__name__,
        "error_code": code or type(error).__name__,
        "status": status,
        "action": action,
        "terminal_channel": terminal,
    }


__all__ = [
    "PROFILE_METADATA_ABSENT",
    "PROFILE_METADATA_PRESENT",
    "authorization_command",
    "authorization_observation",
    "authorize_worker_profile",
    "worker_scope_request",
    "inspect_profile_metadata",
]

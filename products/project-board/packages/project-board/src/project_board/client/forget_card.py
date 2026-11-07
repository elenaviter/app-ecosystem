"""`pb worker forget-card`: forget a revoked Card's local record (W1012 point 6).

The operator, 7 Nov 2026 ~02:17Z, verbatim: "Point 6. B": a pb host command
does the whole stale-record step that Connect a machine and add-a-worker-host
gave as a one-line Python call. After a password store reset the Card's
credential is gone, so pb cannot revoke the Card; the person revokes it in
Connection Hub and gives its access_id. The access_id is the person's
assertion of that revocation, not proof pb can check.

Without --access-id the command only reads: it names the profile, its target
and state root, the recorded access_id to revoke, and whether this machine
still holds the Card's credential (a yes or no; the value is never printed).
With --access-id it calls Connection Hub's ProfileService.retire_revoked,
which re-reads the record and requires the exact access_id and no credential
under the credential lock a refresh or reconnect takes, and removes the
metadata only. It never revokes, resets or authorizes anything.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ..contract.errors import DomainError
from .host_config import HostRelayConfig, WorkerChannelConfig
from .prerequisites import Probes

CREDENTIAL_READ_SECONDS = 10.0


def _channel(config: HostRelayConfig, *, profile: str, identity: Any) -> WorkerChannelConfig:
    """The agent's own channel from its session, or the profile a person named on a host terminal."""

    if profile:
        matches = [channel for channel in config.workers if channel.profile == profile]
    elif identity is not None:
        matches = [channel for channel in config.workers if channel.worker_name == identity.worker_name]
    else:
        matches = []
    if len(matches) != 1:
        raise DomainError(
            "work_relay_card_profile_required",
            "Name the agent's Card profile (pb status in that agent's session prints it), or run this in the "
            "agent's own session.",
            status=404,
            details={
                "profile": profile,
                "enrolled": [
                    {"alias": channel.worker_alias or channel.worker_name, "profile": channel.profile}
                    for channel in config.workers
                ],
            },
        )
    channel = matches[0]
    if channel.state == "disabled":
        raise DomainError(
            "work_relay_worker_disabled",
            "Enable this worker channel before changing its Card.",
            status=409,
            details={"worker_name": channel.worker_name},
        )
    return channel


def _store_usable(probes: Probes) -> None:
    """The bounded, non-unlocking keyring probe, before either step."""

    from .prerequisites import check_prerequisites

    checked = check_prerequisites(probes)
    keyring = next((item for item in checked.get("checked") or [] if item.get("name") == "keyring"), None)
    if keyring is not None and not keyring.get("found"):
        raise DomainError(
            "credential_store_locked",
            "This machine's password store is not usable, so nothing was changed. Make it usable first; "
            "pb status --format brief names the fix. Never reset it to get past this.",
            status=409,
            details={"fix": keyring.get("fix") or "", "detail": keyring.get("detail") or ""},
        )


async def forget_card(
    config_path: str | Path,
    *,
    profile: str = "",
    identity: Any = None,
    access_id: str = "",
    probes: Probes | None = None,
    build_services: Any = None,
) -> dict[str, Any]:
    path = Path(config_path).expanduser().resolve()
    config = HostRelayConfig.load(path)
    channel = _channel(config, profile=str(profile or "").strip(), identity=identity)
    _store_usable(probes or Probes())
    if build_services is None:
        try:
            from connection_hub.caller.paths import StatePaths
            from connection_hub.caller.services import build_caller_services
        except ImportError as exc:
            raise DomainError(
                "connection_hub_client_not_installed",
                "The Project Board host command requires the connection-hub[client] package.",
                status=500,
            ) from exc
        paths = (
            StatePaths(config.connection_hub_state_root)
            if config.connection_hub_state_root is not None
            else StatePaths.default()
        )
        services = build_caller_services(paths=paths)
    else:
        services = build_services(config)
    state_root = str(config.connection_hub_state_root or "")
    record = services.profiles.get(channel.profile)
    if record is None:
        raise DomainError(
            "profile_not_found",
            "This machine has no local record of that Card: there is nothing to forget.",
            status=404,
            details={"profile": channel.profile, "state_root": state_root},
        )
    if record.endpoint.rstrip("/") != config.endpoint.rstrip("/"):
        raise DomainError(
            "work_relay_profile_endpoint_mismatch",
            "The worker profile points at a different governed MCP endpoint.",
            status=409,
            details={"profile": channel.profile},
        )
    if record.auth_type != "oauth" or services.oauth_profile_sessions is None:
        raise DomainError(
            "work_relay_card_profile_not_oauth",
            "This profile is not an OAuth Card profile; pb does not forget it.",
            status=409,
            details={"profile": channel.profile},
        )
    # A yes or no only; the credential value is never returned or printed.
    # Off the event loop and bounded: a store that stops answering refuses.
    try:
        holds_credential = bool(
            await asyncio.wait_for(
                asyncio.to_thread(services.oauth_profile_sessions.credential_present, record),
                timeout=CREDENTIAL_READ_SECONDS,
            )
        )
    except Exception as exc:  # noqa: BLE001 - fail closed: nothing is changed
        raise DomainError(
            "credential_store_unreadable",
            "This machine's password store did not answer for that Card, so nothing was changed.",
            status=409,
            details={"profile": channel.profile, "cause": str(getattr(exc, "code", "") or type(exc).__name__)},
        ) from exc
    base = {
        "profile": channel.profile,
        "alias": channel.worker_alias or channel.worker_name,
        "target": config.target_id,
        "state_root": state_root,
        "recorded_access_id": record.access_id or "",
        "credential_on_this_machine": holds_credential,
    }
    replace_line = f"pb worker authorize {channel.profile} --device --replace-card"
    if holds_credential:
        # Absolute (design review): a Card whose credential is here is revoked
        # properly with that credential, never forgotten.
        raise DomainError(
            "work_relay_card_credential_present",
            "This machine still holds that Card's credential, so it can revoke the Card itself: run "
            f"{replace_line}. Nothing was changed.",
            status=409,
            details={**base, "next": replace_line},
        )
    given = str(access_id or "").strip()
    if not given:
        following = f"pb worker forget-card {channel.profile} --access-id {record.access_id}"
        return {
            **base,
            "state": "read_only",
            "changed": False,
            "next": (
                f"Revoke access_id {record.access_id} in Connection Hub (Access cards, that agent's card), "
                f"then run: {following}"
            ),
            "next_command": following,
        }
    try:
        removed = await services.profile_service.retire_revoked(channel.profile, access_id=given)
    except Exception as exc:  # noqa: BLE001 - every refusal is reported by its code, nothing changed
        code = str(getattr(exc, "code", "") or type(exc).__name__)
        details = {**base, "cause": code}
        if code == "oauth_profile_access_id_confirmation_required":
            raise DomainError(
                code,
                f"That is not the access_id this machine recorded for the Card ({record.access_id}). "
                "Nothing was changed.",
                status=409,
                details=details,
            ) from exc
        if code == "oauth_profile_credential_present":
            raise DomainError(
                "work_relay_card_credential_present",
                f"A credential for that Card arrived meanwhile, so it can revoke the Card itself: run {replace_line}. "
                "Nothing was changed.",
                status=409,
                details={**details, "next": replace_line},
            ) from exc
        raise DomainError(
            code,
            str(getattr(exc, "message", "") or exc) or "The local record was not forgotten.",
            status=409,
            details=details,
        ) from exc
    authorize = f"pb worker authorize {channel.profile} --device"
    return {
        **base,
        "state": "forgotten",
        "changed": True,
        "removed_access_id": removed.profile.access_id or "",
        "dangling_installations": removed.dangling_installations,
        "next": f"Give the agent a new Card: in its session, run {authorize}",
        "next_command": authorize,
    }


__all__ = ["forget_card"]

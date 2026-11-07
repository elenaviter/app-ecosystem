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

import shlex
from pathlib import Path
from typing import Any

from ..contract.errors import DomainError
from .authorization import authorization_command
from .host_config import HostRelayConfig, WorkerChannelConfig, selection_arguments
from .prerequisites import Probes


def _channel(config: HostRelayConfig, *, profile: str, identity: Any) -> WorkerChannelConfig:
    """The agent's own channel in an agent session; a named profile only from a person's host terminal.

    Design review (codex-infra): an agent acts on its own selected runtime
    channel and never on another one, even when it names a profile.
    """

    enrolled = [
        {"alias": channel.worker_alias or channel.worker_name, "profile": channel.profile}
        for channel in config.workers
    ]
    if identity is not None:
        own = [channel for channel in config.workers if channel.worker_name == identity.worker_name]
        if len(own) != 1:
            raise DomainError(
                "work_relay_card_profile_required",
                "This agent session has no channel on the selected target, so it cannot change a Card here. "
                "A person names the profile from a host terminal.",
                status=404,
                details={"worker_name": identity.worker_name, "enrolled": enrolled},
            )
        if profile and profile != own[0].profile:
            raise DomainError(
                "work_relay_card_profile_not_own",
                "An agent forgets only its own Card's record; another agent's Card is the person's to handle "
                "from a host terminal. Nothing was read or changed.",
                status=403,
                details={"profile": profile, "own_profile": own[0].profile},
            )
        matches = own
    elif profile:
        matches = [channel for channel in config.workers if channel.profile == profile]
    else:
        matches = []
    if len(matches) != 1:
        raise DomainError(
            "work_relay_card_profile_required",
            "Name the agent's Card profile (pb status in that agent's session prints it), or run this in the "
            "agent's own session.",
            status=404,
            details={"profile": profile, "enrolled": enrolled},
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
    services = (build_services or (lambda resolved: build_caller_services(paths=resolved)))(paths)
    # The store actually used, the default included (review finding 5).
    state_root = str(paths.root)
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
    # The library's bounded read: a daemon thread abandoned at its deadline, so
    # a store that never answers neither hangs the command nor keeps the
    # process alive after the refusal (review P1).
    try:
        holds_credential = bool(await services.oauth_profile_sessions.credential_present_bounded(record))
    except Exception as exc:  # noqa: BLE001 - fail closed: nothing is changed
        raise DomainError(
            "credential_store_unreadable",
            "This machine's password store did not answer for that Card, so nothing was changed.",
            status=409,
            details={"profile": channel.profile, "cause": str(getattr(exc, "code", "") or type(exc).__name__)[:80]},
        ) from exc
    base = {
        "profile": channel.profile,
        "alias": channel.worker_alias or channel.worker_name,
        "target": config.target_id,
        "state_root": state_root,
        "recorded_access_id": record.access_id or "",
        "credential_on_this_machine": holds_credential,
    }
    # Every printed line keeps the selected target, so a line pasted in another
    # terminal reaches this target's profile, never a same-named one on the
    # default target (review P1).
    selection = selection_arguments(path, target_id=config.target_id)
    replace_line = shlex.join([*authorization_command(channel.profile, selection=selection), "--replace-card"])
    authorize = shlex.join(authorization_command(channel.profile, selection=selection))
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
        following = shlex.join(
            ["pb", "worker", "forget-card", channel.profile, "--access-id", str(record.access_id or ""), *selection]
        )
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
        # Codes and catalog messages only: an exception's own text can carry
        # anything, so it is never echoed (review).
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
        known = {
            "oauth_credential_custody_timeout": "This machine's password store did not answer in time.",
            "oauth_profile_lock_timeout": "Another operation holds this Card's lock; try again in a moment.",
            "profile_changed": "The Card's record changed meanwhile; read it again with pb worker forget-card.",
            "profile_not_found": "This machine has no local record of that Card any more.",
            "profile_in_use": "The profile is installed for an MCP client; remove that client entry first.",
            "oauth_profile_credential_changed": "The Card's credential changed meanwhile; read it again.",
        }
        raise DomainError(
            "work_relay_card_forget_refused" if code not in known else code,
            f"{known.get(code, 'The local record was not forgotten.')} Nothing was changed.",
            status=409,
            details=details,
        ) from exc
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

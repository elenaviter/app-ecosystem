"""`pb worker claude-profile`: the operator's view and actions on Claude profiles (W763).

status    every Claude session enrolled on this host, its profile and whether
          that profile's login is the bound account; for an unbound session,
          the folder its stored account email proposes.
bind      the operator confirms one session's profile by its account email.
migrate   dry run (default) or --apply: copy the session's own files from the
          global folder into its profile, verify, install the worker skill
          and settings there.
rollback  remove what one migrate created, when none of it changed since.

None of these starts, stops or resumes a model, logs in, or reads a credential.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..contract.errors import DomainError
from ..contract.worker_identity import WorkerSessionIdentity
from . import claude_profile as cp
from .claude_profile_migration import apply_migration, plan_migration, rollback_migration
from .host_config import HostRelayConfig, set_worker_claude_profile
from .resume_command import build_session_resume_command
from .store import SharedFieldStore


def _stored_account(field: SharedFieldStore, worker_name: str) -> dict[str, str]:
    try:
        record = field.worker_board_record(worker_name)
    except DomainError:
        return {}
    account = record.get("runtime_account") if isinstance(record, dict) else None
    return {str(k): str(v) for k, v in (account or {}).items()} if isinstance(account, dict) else {}


def _session_row(config: HostRelayConfig, field: SharedFieldStore, channel: Any, home: Path | None) -> dict[str, Any]:
    binding = dict(channel.claude_profile or {})
    state, account = cp.profile_state(binding, home=home)
    stored = _stored_account(field, channel.worker_name)
    row: dict[str, Any] = {
        "worker_name": channel.worker_name,
        "alias": channel.worker_alias,
        "runtime_session_id": channel.runtime_session_id,
        "profile_state": state,
        "config_dir": binding.get("config_dir", ""),
        "email": binding.get("email", ""),
        "bound_by": binding.get("bound_by", ""),
    }
    if binding.get("reason"):
        row["unbound_reason"] = binding["reason"]
    if binding.get("capture") == "differs_from_binding":
        row["capture"] = f"last started with CLAUDE_CONFIG_DIR={binding.get('captured_config_dir') or '(none)'}"
    if state != cp.VERIFIED and stored.get("email"):
        try:
            row["proposed_config_dir"] = str(cp.profile_dir_for_email(stored["email"], home=home))
            row["stored_email"] = stored["email"]
        except DomainError:
            pass
    if state == cp.VERIFIED:
        row["account_email"] = account.get("email", "")
    return row


def claude_profile_command(args: Any, *, config_path: Path, home: Path | None = None) -> dict[str, Any]:
    config = HostRelayConfig.load(config_path)
    field = SharedFieldStore(config.field_root)
    action = args.claude_profile_command
    if action == "status":
        sessions = [
            _session_row(config, field, channel, home)
            for channel in config.workers
            if channel.runtime_kind == "claude-code"
        ]
        return {"profile_root": str(cp.profile_root(home)), "registry": cp.read_registry(home), "sessions": sessions}

    session_id = str(args.runtime_session_id or "").strip()
    if not session_id:
        raise DomainError("work_claude_profile_session_required", "Name the session with --runtime-session-id.")
    identity = WorkerSessionIdentity.create("claude-code", session_id)
    channel = config.worker(identity)
    if channel is None:
        raise DomainError("work_relay_worker_not_enrolled", "This session is not enrolled on this host.", status=404)

    if action == "bind":
        if not args.confirm:
            raise DomainError(
                "work_claude_profile_confirmation_required",
                "Binding names the account this session runs under; the operator confirms it with --confirm.",
            )
        email = cp.normalize_email(args.email)
        stored = _stored_account(field, channel.worker_name)
        account_id = stored.get("account_id", "") if stored.get("email", "").lower() == email else ""
        if not account_id:
            # The board's record names another account or none: only the
            # folder's own login can say which account this email is.
            try:
                account = cp.read_profile_account(cp.profile_dir_for_email(email, home=home), home=home)
            except DomainError as exc:
                raise DomainError(
                    "work_claude_profile_account_unknown",
                    f"The board does not know {email} for this session and its folder has no login yet. "
                    "Log that folder in first (start claude with CLAUDE_CONFIG_DIR set to it), then bind.",
                    status=409,
                ) from exc
            if account.get("email", "").lower() != email:
                raise DomainError(
                    "work_claude_profile_login_mismatch",
                    f"The folder for {email} is logged in to {account.get('email')}.",
                    status=409,
                )
            account_id = account["account_id"]
        binding = cp.bind_session(email=email, account_id=account_id, home=home)
        updated = set_worker_claude_profile(config_path, identity=identity, binding=binding)
        return {"bound": _session_row(config, field, updated, home), **_resume_line(updated)}

    binding = dict(channel.claude_profile or {})
    if not binding.get("config_dir"):
        raise DomainError(
            "work_claude_profile_unbound",
            "Bind this session to its profile first (pb worker claude-profile bind).",
            status=409,
        )
    target = Path(binding["config_dir"])
    if action == "migrate":
        source = Path(args.source).expanduser() if args.source else (home or Path.home()) / ".claude"
        with_memory = bool(getattr(args, "with_memory", False))
        if not args.apply:
            return {
                "dry_run": True,
                **plan_migration(source=source, target=target, session_id=session_id, with_memory=with_memory),
            }
        result = apply_migration(
            source=source,
            target=target,
            session_id=session_id,
            session_stopped=bool(getattr(args, "session_stopped", False)),
            with_memory=with_memory,
        )
        return {**result, "next": _next_after_migration(target), **_resume_line(channel)}
    if action == "rollback":
        return rollback_migration(target=target, session_id=session_id)
    raise ValueError(f"unsupported claude-profile command: {action}")


def _resume_line(channel: Any) -> dict[str, Any]:
    profile = cp.resume_profile(channel.claude_profile)
    built = build_session_resume_command(
        runtime_kind="claude-code",
        runtime_session_id=channel.runtime_session_id,
        working_directory=channel.working_directory,
        launch=channel.runtime_launch,
        profile=profile,
    )
    return {"resume_command": built["command"]}


def _next_after_migration(target: Path) -> list[str]:
    folder = str(target)
    return [
        f'pb procedure install --target claude-code --claude-config-dir "{folder}"',
        "Resume the session with resume_command; the first start in a new folder asks for that account's login once.",
        "In the resumed session run pb worker listen; pb worker claude-profile status then shows verified.",
    ]


__all__ = ["claude_profile_command"]

"""Which Claude account profile a worker session runs under (W763).

Claude Code keeps one login, one `.claude.json`, one settings file and one set
of transcripts per configuration directory (`CLAUDE_CONFIG_DIR`). Two sessions
on one host can therefore run under two different accounts, and the host's
global login (`~/.claude.json`) says nothing about either of them.

A session is bound to a profile folder named after its account email,
`~/.claude-accounts/<email>` (operator ruling: "we agreed earlier that we will
capture email per session. and that email will be profile name"). The folder is
chosen by that email; the account ID read from the folder's own `.claude.json`
is what is verified, and the `CLAUDE_CONFIG_DIR` the session actually started
with is only a cross-check. A registry (`~/.claude-accounts/registry.json`)
keeps the confirmed account-ID-to-folder mapping, so a later change of the
account's email never renames or re-points a folder.

Nothing here reads, copies or returns a credential: only the public account
fields of `.claude.json` (account UUID, email, organization).
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..contract.errors import DomainError
from .io import atomic_write_json, exclusive_lock, read_json, utc_now
from .runtime_account import _claude_account, _read_json_object


PROFILE_ROOT_NAME = ".claude-accounts"
REGISTRY_NAME = "registry.json"
CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"

# The states a session's profile evidence can be in; the board shows each one.
VERIFIED = "verified"  # the bound folder is logged in to the bound account
MISMATCH = "mismatch"  # the bound folder is logged in to another account
UNBOUND = "unbound"  # no confirmed profile for this session
UNREADABLE = "unreadable"  # the bound folder's account cannot be read

_EMAIL = re.compile(r"^[A-Za-z0-9.!#$%&'*+=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _home(home: str | Path | None) -> Path:
    return Path(os.path.abspath(Path(home).expanduser() if home is not None else Path.home()))


def profile_root(home: str | Path | None = None) -> Path:
    return _home(home) / PROFILE_ROOT_NAME


def normalize_email(value: Any) -> str:
    """The account email as a folder name: lower case, one plain address, or a refusal."""

    email = str(value or "").strip().lower()
    if not email or len(email) > 254 or not _EMAIL.match(email) or ".." in email or email.startswith("."):
        raise DomainError(
            "work_claude_profile_email_invalid",
            "A Claude profile is named by its account email, for example ana@example.com.",
            details={"email": str(value or "")[:254]},
        )
    return email


def profile_dir_for_email(email: Any, *, home: str | Path | None = None) -> Path:
    return profile_root(home) / normalize_email(email)


def _same_path(left: str | Path, right: str | Path) -> bool:
    return os.path.realpath(os.path.expanduser(str(left))) == os.path.realpath(os.path.expanduser(str(right)))


def account_file(config_dir: str | Path | None, *, home: str | Path | None = None) -> Path:
    """The `.claude.json` a Claude process reads: inside its config dir, else the home one."""

    return Path(config_dir).expanduser() / ".claude.json" if config_dir else _home(home) / ".claude.json"


def read_profile_account(config_dir: str | Path | None, *, home: str | Path | None = None) -> dict[str, str]:
    """{account_id, email, organization} of the login a config dir holds; DomainError when absent."""

    return _claude_account(_read_json_object(account_file(config_dir, home=home)))


def claude_executable(path_env: str | None = None) -> str:
    """The absolute `claude` a resume line runs: the PATH entry, never its resolved version.

    `~/.local/bin/claude` is a link to the current version; resolving it would
    pin a resume line to a version a later update removes. A shell function or
    alias of the same name is not seen here, which is the point.
    """

    found = shutil.which("claude", path=path_env)
    return os.path.abspath(found) if found else ""


# -- registry -------------------------------------------------------------


def _registry_path(home: str | Path | None) -> Path:
    return profile_root(home) / REGISTRY_NAME


def read_registry(home: str | Path | None = None) -> dict[str, dict[str, str]]:
    """{account_id: {email, config_dir, confirmed_at, confirmed_by}}; empty when none."""

    path = _registry_path(home)
    if not path.exists():
        return {}
    value = read_json(path)
    profiles = value.get("profiles") if isinstance(value, Mapping) else None
    if not isinstance(profiles, Mapping):
        raise DomainError(
            "work_claude_profile_registry_invalid",
            f"{path} has no profiles object; it was left unchanged.",
            status=409,
        )
    return {str(key): dict(row) for key, row in profiles.items() if isinstance(row, Mapping)}


def register_profile(
    *,
    account_id: str,
    email: str,
    config_dir: str | Path,
    confirmed_by: str,
    home: str | Path | None = None,
) -> dict[str, str]:
    """Record one confirmed account-ID-to-folder mapping, refusing any collision.

    An account already mapped keeps its folder (a changed email never renames
    it); a folder already mapped to another account is refused.
    """

    if not _UUID.match(str(account_id or "")):
        raise DomainError("work_claude_profile_account_invalid", "A Claude account ID is a UUID.")
    folder = os.path.abspath(os.path.expanduser(str(config_dir)))
    path = _registry_path(home)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with exclusive_lock(path.with_suffix(".json.lock")):
        profiles = read_registry(home)
        current = profiles.get(account_id)
        if current is not None:
            if not _same_path(current.get("config_dir", ""), folder):
                raise DomainError(
                    "work_claude_profile_account_mapped",
                    f"This account is already mapped to {current.get('config_dir')}; the mapping is kept.",
                    status=409,
                    details={"config_dir": current.get("config_dir", "")},
                )
            return current
        for other_id, row in profiles.items():
            if _same_path(row.get("config_dir", ""), folder):
                raise DomainError(
                    "work_claude_profile_folder_mapped",
                    f"{folder} is already the profile of another account.",
                    status=409,
                    details={"account_id": other_id},
                )
        row = {
            "email": normalize_email(email),
            "config_dir": folder,
            "confirmed_at": utc_now(),
            "confirmed_by": str(confirmed_by),
        }
        profiles[account_id] = row
        atomic_write_json(path, {"version": 1, "profiles": profiles})
        return row


# -- session binding ------------------------------------------------------


def _folder_for(account: Mapping[str, str], *, home: str | Path | None) -> Path:
    """The account's registered folder, else the folder its email names."""

    row = read_registry(home).get(str(account.get("account_id") or ""))
    if row:
        return Path(row["config_dir"])
    return profile_dir_for_email(account.get("email"), home=home)


def capture_binding(
    *,
    environ: Mapping[str, str] | None = None,
    home: str | Path | None = None,
    existing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The profile binding a Claude session reports when it enrolls (`pb worker listen`).

    Runs inside the session, so `CLAUDE_CONFIG_DIR` is the one the session was
    started with: that variable alone is read from the environment. The
    account it is logged in to decides the folder; the started folder must be
    that same folder, otherwise nothing is bound and the reason is kept. An
    existing binding is never switched by a new start: a different start is a
    mismatch for the operator to resolve (W763 [9]).
    """

    env = os.environ if environ is None else environ
    captured = str(env.get(CONFIG_DIR_ENV) or "").strip()
    observed = {
        "captured_config_dir": captured,
        "captured_at": utc_now(),
        # The program the resume line runs, also for a session on the default folder.
        "executable": claude_executable(env.get("PATH")),
    }
    if existing and existing.get("config_dir"):
        # A confirmed binding keeps its own program; the capture is a cross-check only.
        kept = {**observed, "executable": existing.get("executable") or observed["executable"]}
        if captured and _same_path(captured, existing["config_dir"]):
            return {**existing, **kept}
        return {**existing, **kept, "capture": "differs_from_binding"}
    if not captured:
        # A session on the global login has no profile of its own yet: it is
        # mapped by the operator (`pb worker claude-profile bind`), never guessed.
        return {**observed, "state": UNBOUND, "reason": "started_without_profile"}
    try:
        account = read_profile_account(captured, home=home)
    except DomainError as exc:
        return {**observed, "state": UNBOUND, "reason": exc.code}
    try:
        folder = _folder_for(account, home=home)
    except DomainError as exc:
        return {**observed, "state": UNBOUND, "reason": exc.code}
    if not _same_path(captured, folder):
        return {
            **observed,
            "state": UNBOUND,
            "reason": "started_folder_is_not_the_account_folder",
            "expected_config_dir": str(folder),
        }
    try:
        row = register_profile(
            account_id=account["account_id"],
            email=account.get("email", ""),
            config_dir=folder,
            confirmed_by="session-capture",
            home=home,
        )
    except DomainError as exc:
        # A collision is the operator's to resolve; enrolling still succeeds.
        return {**observed, "state": UNBOUND, "reason": exc.code}
    return {
        **observed,
        "config_dir": row["config_dir"],
        "email": row["email"],
        "account_id": account["account_id"],
        "executable": claude_executable(env.get("PATH")),
        "bound_by": "session-capture",
        "bound_at": utc_now(),
    }


def bind_session(
    *,
    email: str,
    account_id: str,
    executable: str = "",
    confirmed_by: str = "operator",
    home: str | Path | None = None,
) -> dict[str, Any]:
    """An operator-confirmed binding of a session to the profile its email names (W763 [10])."""

    account = {"account_id": account_id, "email": email}
    folder = _folder_for(account, home=home)
    row = register_profile(
        account_id=account_id, email=email, config_dir=folder, confirmed_by=confirmed_by, home=home
    )
    return {
        "config_dir": row["config_dir"],
        "email": row["email"],
        "account_id": account_id,
        "executable": executable or claude_executable(),
        "bound_by": confirmed_by,
        "bound_at": utc_now(),
    }


def profile_state(binding: Mapping[str, Any] | None, *, home: str | Path | None = None) -> tuple[str, dict[str, str]]:
    """(state, account) for a session's binding; the account only when it is verified."""

    if not binding or not binding.get("config_dir") or not binding.get("account_id"):
        return UNBOUND, {}
    try:
        account = read_profile_account(binding["config_dir"], home=home)
    except DomainError:
        return UNREADABLE, {}
    if account.get("account_id") != binding["account_id"]:
        return MISMATCH, {}
    return VERIFIED, account


def qualify_profile_limit_state(
    state: Mapping[str, Any] | None,
    binding: Mapping[str, Any] | None,
    *,
    environ: Mapping[str, str] | None = None,
    home: str | Path | None = None,
) -> Mapping[str, Any] | None:
    """Name the account a usage sample belongs to, and never another one (W763 [2], [3]).

    The status line runs inside the session, so `CLAUDE_CONFIG_DIR` here is
    the session's own. A sample from a bound session whose folder holds the
    bound account names it as ``session_profile``. Anything else names no
    account: a mismatch, an unbound session, and a process on the default
    folder, whose global login is never taken as this session's.
    """

    if not isinstance(state, Mapping) or not state or state.get("account_id"):
        return state
    env = os.environ if environ is None else environ
    started = str(env.get(CONFIG_DIR_ENV) or "").strip()
    if not started:
        # A process on the default folder is not a profile session: its usage
        # names no account (operator, 2026-10-12: "we re not going to work wit
        # unscoped claude agents").
        return state
    if not binding or not binding.get("config_dir") or not _same_path(started, binding["config_dir"]):
        return state
    current, account = profile_state(binding, home=home)
    if current != VERIFIED:
        return state
    return {**state, "account_id": account["account_id"], "account_source": "session_profile"}


@dataclass(frozen=True)
class SessionAccount:
    """One read of a bound session's account: the account only when verified."""

    account: dict[str, str]
    profile_state: str


async def read_session_account(binding: Mapping[str, Any], *, home: str | Path | None = None) -> SessionAccount:
    """The relay's account read for a bound Claude session, off the event loop."""

    state, account = await asyncio.to_thread(profile_state, binding, home=home)
    return SessionAccount(account=account, profile_state=state)


def resume_profile(binding: Mapping[str, Any] | None) -> dict[str, str]:
    """The {config_dir, executable} a resume line names; an empty folder is the default one."""

    value = binding or {}
    return {
        "config_dir": str(value.get("config_dir") or ""),
        "executable": str(value.get("executable") or ""),
    }


__all__ = [
    "CONFIG_DIR_ENV",
    "MISMATCH",
    "SessionAccount",
    "UNBOUND",
    "UNREADABLE",
    "VERIFIED",
    "account_file",
    "bind_session",
    "capture_binding",
    "claude_executable",
    "normalize_email",
    "profile_dir_for_email",
    "profile_root",
    "profile_state",
    "qualify_profile_limit_state",
    "read_profile_account",
    "read_registry",
    "read_session_account",
    "register_profile",
    "resume_profile",
]

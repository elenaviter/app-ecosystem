"""Move one Claude session into its account profile, copy first and verify (W763 [13]).

A session that ran on the host's global login (`~/.claude`) keeps its history
there. Once the operator has bound it to its account folder
(`~/.claude-accounts/<email>`), this copies exactly that session's own files
into the folder and verifies every copy by its hash. The originals stay where
they are: they are the backup, and nothing here deletes them.

Copied, and only these:
  projects/<project>/<session>.jsonl   the transcript `claude --resume` reads
  projects/<project>/<session>/        its subagent and tool-result files
  projects/<project>/memory/           only with with_memory: that project
                                       folder's memory, once the operator
                                       confirmed it belongs to this agent
  file-history/<session>/              its file checkpoints
  todos/<session>-*.json               its task lists
Never copied: settings, plugins, other sessions' files. The Problem Board
skill and settings are installed into the profile separately.

The login is kept only on request (``with_login``), and only on the same
machine for the same account (operator, 2026-10-12, through the coordinator:
migration must preserve each existing same-account Claude login locally,
including Spark, where nobody can log in again). The source folder's login
must be the account the profile is bound to; then its credential file is
copied as it is, mode 0600, never read into output, a log or the manifest,
and the profile's `.claude.json` is seeded with only that account's public
fields. One login is never copied into another account's folder, and nothing
leaves the machine. On macOS the login is in the Keychain, not a file: there
the person logs in once in the new folder instead: the Keychain entry of a
configuration folder is named by Claude Code in a way it does not document,
and copying a Keychain secret with the `security` tool passes it on a command
line. This gap is reported, not worked around.

Every apply writes a manifest of what it created, before and after each
file, so an interrupted copy is known. A rollback removes only those files,
only while each still has the hash it was copied with, then the directories
the apply created when they are empty; it never removes a folder tree.

Fences: neither folder may sit behind a symbolic link, and no path between a
folder and a file may be one, on the way in, on the way out and in rollback.
A temporary copy gets a fresh name and never replaces an existing file. The
operator's word that the session is stopped is required; a quiet transcript is
only the second check.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..contract.errors import DomainError
from .io import atomic_write_json, utc_now

MANIFEST_DIR = ".pb-migrations"
CREDENTIAL_FILE = ".credentials.json"
# The public fields of a login a profile's .claude.json is seeded with.
_ACCOUNT_KEYS = ("oauthAccount", "hasCompletedOnboarding")
# A transcript written this recently belongs to a session that is still running.
QUIET_SECONDS = 120
_NEVER = (".claude.json", ".credentials.json")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _refuse(code: str, message: str, **details: Any) -> DomainError:
    return DomainError(code, message, status=409, details=details)


def _real_root(path: Path, *, what: str) -> Path:
    """An absolute folder with no symbolic link anywhere on its path."""

    absolute = Path(os.path.abspath(path))
    if os.path.realpath(absolute) != str(absolute):
        raise _refuse(
            "work_claude_profile_path_link",
            f"The {what} folder {absolute} is reached through a symbolic link; nothing was copied.",
            path=str(absolute),
        )
    return absolute


def _inside(root: Path, relative: str | Path) -> Path:
    """``root/relative`` for a plain relative path with no link between them."""

    rel = Path(relative)
    if rel.is_absolute() or not rel.parts or any(part in ("", ".", "..") for part in rel.parts):
        raise _refuse("work_claude_profile_path_invalid", f"{relative} is not a plain relative path.")
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise _refuse(
                "work_claude_profile_path_link",
                f"{current} is a symbolic link; nothing was copied or removed.",
                path=str(current),
            )
    return current


def _session_project(source: Path, session_id: str) -> Path:
    found = sorted((source / "projects").glob(f"*/{session_id}.jsonl"))
    if not found:
        raise _refuse(
            "work_claude_profile_session_not_found",
            f"No transcript of session {session_id} under {source / 'projects'}.",
            source=str(source),
        )
    if len(found) > 1:
        raise _refuse(
            "work_claude_profile_session_ambiguous",
            "The session's transcript is in more than one project folder; the operator chooses.",
            paths=[str(item) for item in found],
        )
    return found[0].parent


def _files_under(root: Path) -> list[Path]:
    if not root.is_dir() or root.is_symlink():
        return []
    links = [path for path in root.rglob("*") if path.is_symlink()]
    if links:
        raise _refuse(
            "work_claude_profile_path_link",
            f"{links[0]} is a symbolic link inside the session's files; nothing was copied.",
            path=str(links[0]),
        )
    return sorted(path for path in root.rglob("*") if path.is_file() and path.name not in _NEVER)


def session_files(source: Path, session_id: str, *, with_memory: bool = False) -> list[Path]:
    """The source files that belong to one session, relative to the source folder.

    A project folder's memory is shared by every session that ran in that
    folder, so it comes along only when the operator says it is this agent's.
    """

    project = _session_project(source, session_id)
    selected = [project / f"{session_id}.jsonl"]
    selected += _files_under(project / session_id)
    if with_memory:
        selected += _files_under(project / "memory")
    selected += _files_under(source / "file-history" / session_id)
    selected += sorted(
        path for path in (source / "todos").glob(f"{session_id}-*.json")
        if path.is_file() and not path.is_symlink()
    )
    return [path.relative_to(source) for path in selected]


def plan_migration(
    *, source: str | Path, target: str | Path, session_id: str, now: float | None = None, with_memory: bool = False
) -> dict[str, Any]:
    """The dry run: each file with its action (copy, present, conflict) and why a run would stop."""

    source_dir = _real_root(Path(source).expanduser(), what="source")
    target_dir = _real_root(Path(target).expanduser(), what="profile")
    if source_dir == target_dir:
        raise _refuse("work_claude_profile_same_folder", "The session already lives in this profile folder.")
    rows: list[dict[str, str]] = []
    for relative in session_files(source_dir, session_id, with_memory=with_memory):
        source_hash = _sha256(_inside(source_dir, relative))
        destination = _inside(target_dir, relative)
        if destination.exists():
            action = "present" if destination.is_file() and _sha256(destination) == source_hash else "conflict"
        else:
            action = "copy"
        rows.append({"path": str(relative), "sha256": source_hash, "action": action})
    transcript = source_dir / rows[0]["path"]
    age = (time.time() if now is None else now) - transcript.stat().st_mtime
    blockers = []
    if age < QUIET_SECONDS:
        blockers.append("session_running")
    if any(row["action"] == "conflict" for row in rows):
        blockers.append("conflict")
    return {
        "session_id": session_id,
        "with_memory": with_memory,
        "source": str(source_dir),
        "target": str(target_dir),
        "files": rows,
        "copy": sum(row["action"] == "copy" for row in rows),
        "present": sum(row["action"] == "present" for row in rows),
        "conflicts": [row["path"] for row in rows if row["action"] == "conflict"],
        "transcript_quiet_seconds": int(age),
        "blockers": blockers,
    }


def _login_record(source: Path) -> Path:
    """The `.claude.json` that names the source folder's login."""

    # The default folder keeps it beside itself, ~/.claude.json; any other
    # configuration folder keeps it inside.
    return source.parent / ".claude.json" if source.name == ".claude" else source / ".claude.json"


def _publish(partial: Path, destination: Path) -> None:
    """Put ``partial`` at ``destination`` only if nothing is there: one atomic step.

    A hard link fails when the name exists, so a file that appears at the
    destination at any moment is never replaced; ``partial`` is then removed.
    """

    try:
        os.link(partial, destination)
    except FileExistsError as exc:
        partial.unlink()
        raise _refuse(
            "work_claude_profile_conflict", f"{destination.name} appeared in the profile during the copy; it was kept."
        ) from exc
    partial.unlink()


def _same_bytes(left: Path, right: Path) -> bool:
    return left.is_file() and right.is_file() and _sha256(left) == _sha256(right)


def _write_new(path: Path, write: Any) -> None:
    """Create ``path`` (mode 0600) through a fresh temporary name; never replace a file."""

    handle, partial_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.pb-partial-")
    partial = Path(partial_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            write(stream)
        os.chmod(partial, 0o600)
        _publish(partial, path)
    finally:
        if partial.exists():
            partial.unlink()


def preserve_login(*, source: str | Path, target: str | Path, account_id: str) -> dict[str, Any]:
    """Keep this machine's login of one account in that account's profile.

    Refused unless the source folder's login is ``account_id`` (the account
    the profile is bound to), the credential is a file, and the profile has no
    login of its own yet. The credential bytes are copied, never parsed or
    returned.
    """

    source_dir = _real_root(Path(source).expanduser(), what="source")
    target_dir = _real_root(Path(target).expanduser(), what="profile")
    if not account_id:
        raise _refuse("work_claude_profile_unbound", "The profile is not bound to an account.")
    record_path = _login_record(source_dir)
    if record_path.is_symlink() or os.path.realpath(record_path) != str(record_path):
        raise _refuse(
            "work_claude_profile_path_link",
            f"{record_path} is reached through a symbolic link; its login is not copied.",
            path=str(record_path),
        )
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _refuse("work_claude_profile_login_unknown", f"{record_path} cannot be read.") from exc
    account = record.get("oauthAccount") if isinstance(record, Mapping) else None
    if not isinstance(account, Mapping) or account.get("accountUuid") != account_id:
        raise _refuse(
            "work_claude_profile_login_other_account",
            "The source folder is logged in to another account; its login is not copied. "
            "Log the profile in with its own account instead.",
        )
    credential = _inside(source_dir, CREDENTIAL_FILE)
    if not credential.is_file():
        raise _refuse(
            "work_claude_profile_login_not_a_file",
            "This machine keeps the login outside the folder (macOS: in the Keychain). "
            "Log in once in the new folder instead.",
        )
    target_credential = _inside(target_dir, CREDENTIAL_FILE)
    target_record = _inside(target_dir, ".claude.json")
    # Each step can be interrupted and run again: the credential first, then
    # the public metadata, and each one accepted on a retry when it already
    # holds this account.
    if target_record.exists() or target_record.is_symlink():
        if _record_account(target_record) != account_id:
            raise _refuse(
                "work_claude_profile_login_exists",
                "The profile's .claude.json names another account; it is kept and nothing was copied.",
            )
    target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    copied = False
    if target_credential.exists() or target_credential.is_symlink():
        if not _same_bytes(credential, target_credential):
            raise _refuse(
                "work_claude_profile_login_exists",
                "The profile already has its own login; it is kept and nothing was copied.",
            )
    else:
        with credential.open("rb") as original:
            _write_new(target_credential, lambda stream: shutil.copyfileobj(original, stream))
        if not _same_bytes(credential, target_credential):
            raise _refuse("work_claude_profile_copy_mismatch", "The login changed while it was copied.")
        copied = True
    if not target_record.exists():
        seed = {key: record[key] for key in _ACCOUNT_KEYS if key in record}
        _write_new(target_record, lambda stream: stream.write(json.dumps(seed, indent=2).encode("utf-8")))
        copied = True
    return {"login": "copied", "account_id": account_id} if copied else {"login": "present"}


def _record_account(path: Path) -> str:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    account = record.get("oauthAccount") if isinstance(record, Mapping) else None
    return str(account.get("accountUuid") or "") if isinstance(account, Mapping) else ""


def _manifest_path(target: Path, session_id: str) -> Path:
    return target / MANIFEST_DIR / f"{session_id}.json"


def apply_migration(
    *,
    source: str | Path,
    target: str | Path,
    session_id: str,
    session_stopped: bool,
    now: float | None = None,
    with_memory: bool = False,
) -> dict[str, Any]:
    """Copy the session's files and verify each; refuses on a running session or any conflict.

    ``session_stopped`` is the operator's word that the session has exited;
    without it nothing is copied. A retry after an interrupted run copies what
    is missing and accepts what is already there with the same hash.
    """

    if not session_stopped:
        raise _refuse(
            "work_claude_profile_session_not_confirmed_stopped",
            "The operator confirms the session has exited before its files are copied (--session-stopped).",
        )
    plan = plan_migration(source=source, target=target, session_id=session_id, now=now, with_memory=with_memory)
    if plan["blockers"]:
        raise _refuse(
            "work_claude_profile_migration_blocked",
            "Stop the session before migrating it, and resolve every conflicting file."
            if "session_running" in plan["blockers"]
            else "Files already in the profile differ from the session's; nothing was copied.",
            blockers=plan["blockers"],
            conflicts=plan["conflicts"],
        )
    source_dir = Path(plan["source"])
    target_dir = Path(plan["target"])
    manifest_path = _manifest_path(target_dir, session_id)
    _inside(target_dir, Path(MANIFEST_DIR) / f"{session_id}.json")
    previous = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    created = {row["path"]: row["sha256"] for row in previous.get("created", [])}
    created_dirs = set(previous.get("created_dirs", []))
    pending = [row for row in previous.get("pending", []) if isinstance(row, dict)]
    if pending:
        # An interrupted run: claim what it published, drop what it did not.
        created.update(_settle_pending(target_dir, pending))
        pending = []
        atomic_write_json(manifest_path, _manifest(plan, created, created_dirs, pending))
    for row in plan["files"]:
        if row["action"] != "copy":
            continue
        destination = _inside(target_dir, row["path"])
        missing = []
        parent = destination.parent
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        created_dirs.update(str(path.relative_to(target_dir)) for path in missing if target_dir in path.parents)
        manifest_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # The directories are recorded before they exist, so a rollback finds them.
        atomic_write_json(manifest_path, _manifest(plan, created, created_dirs, pending))
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # A fresh name: an earlier partial copy, or anything else, is never replaced.
        handle, partial_name = tempfile.mkstemp(dir=destination.parent, prefix=f".{destination.name}.pb-partial-")
        identity = os.fstat(handle)
        os.close(handle)
        partial = Path(partial_name)
        # Ownership is durable before the file exists: the path, its hash, the
        # temporary name and the temporary file's identity on disk. Publication
        # links that same file into place, so after a crash the destination is
        # this migration's only when it is that file. Equal bytes are not
        # ownership: a file someone else wrote with the same content has
        # another identity and is never claimed.
        pending.append({
            "path": row["path"],
            "partial": str(partial.relative_to(target_dir)),
            "sha256": row["sha256"],
            "file_id": [identity.st_dev, identity.st_ino],
        })
        atomic_write_json(manifest_path, _manifest(plan, created, created_dirs, pending))
        shutil.copy2(_inside(source_dir, row["path"]), partial)
        staged = partial.stat()
        if [staged.st_dev, staged.st_ino] != [identity.st_dev, identity.st_ino]:
            partial.unlink()
            raise _refuse("work_claude_profile_copy_mismatch", f"{row['path']} was not copied into its staged file.")
        if _sha256(partial) != row["sha256"]:
            partial.unlink()
            raise _refuse("work_claude_profile_copy_mismatch", f"{row['path']} changed while it was copied.")
        _publish(partial, destination)
        pending = [item for item in pending if item.get("partial") != str(partial.relative_to(target_dir))]
        created[row["path"]] = row["sha256"]
        # Recorded after every file, so an interrupted run can still be rolled back.
        atomic_write_json(manifest_path, _manifest(plan, created, created_dirs, pending))
    verified = verify_migration(target=target_dir, session_id=session_id, source=source_dir, with_memory=with_memory)
    atomic_write_json(manifest_path, {**_manifest(plan, created, created_dirs, pending), "verified_at": utc_now()})
    return {**plan, "applied": len(created), "verified": verified}


def _settle_pending(
    target_dir: Path, pending: list[dict[str, Any]], kept: list[str] | None = None
) -> dict[str, str]:
    """{path: hash} of the files an interrupted run published; its temporary files removed.

    A pending destination is that run's only when it is the very file the
    run staged (same device and inode, recorded before the copy): publication
    links the staged file into a name that did not exist. Any other
    destination, equal bytes included, is not this migration's and is left
    alone. A claimed file keeps its recorded hash, so one changed since is
    caught by the rollback's check.
    """

    published: dict[str, str] = {}
    for row in pending:
        if row.get("partial"):
            partial = _inside(target_dir, row["partial"])
            if partial.is_file():
                partial.unlink()
        if not row.get("path") or not row.get("sha256"):
            continue
        destination = _inside(target_dir, row["path"])
        file_id = row.get("file_id")
        if destination.is_file() and isinstance(file_id, list) and len(file_id) == 2:
            current = destination.stat()
            if [current.st_dev, current.st_ino] == file_id:
                published[row["path"]] = row["sha256"]
                continue
        if destination.exists() and kept is not None:
            # Changed since, or never this migration's: it stays, and is said.
            kept.append(row["path"])
    return published


def _manifest(
    plan: Mapping[str, Any], created: Mapping[str, str], created_dirs: set[str], pending: list[dict[str, str]]
) -> dict[str, Any]:
    return {
        "version": 1,
        "session_id": plan["session_id"],
        "source": plan["source"],
        "target": plan["target"],
        "created": [{"path": path, "sha256": digest} for path, digest in sorted(created.items())],
        "created_dirs": sorted(created_dirs),
        "pending": list(pending),
        "updated_at": utc_now(),
    }


def verify_migration(
    *, target: str | Path, session_id: str, source: str | Path, with_memory: bool = False
) -> dict[str, Any]:
    """Every source file of the session is in the profile with the same hash."""

    source_dir = _real_root(Path(source).expanduser(), what="source")
    target_dir = _real_root(Path(target).expanduser(), what="profile")
    differing = []
    files = session_files(source_dir, session_id, with_memory=with_memory)
    for relative in files:
        destination = _inside(target_dir, relative)
        if not destination.is_file() or _sha256(destination) != _sha256(_inside(source_dir, relative)):
            differing.append(str(relative))
    if differing:
        raise _refuse(
            "work_claude_profile_migration_unverified",
            "Some of the session's files are missing or different in the profile.",
            paths=differing,
        )
    return {"files": len(files), "state": "verified"}


def rollback_migration(*, target: str | Path, session_id: str) -> dict[str, Any]:
    """Remove what an apply created, and nothing else; refuses when any of it changed since.

    Every path comes from the manifest and is checked again here: a plain
    relative path inside the profile, no symbolic link on the way, and each
    file with the hash it was copied with. A temporary copy an interrupted
    apply left is removed too.
    """

    target_dir = _real_root(Path(target).expanduser(), what="profile")
    manifest_path = _inside(target_dir, Path(MANIFEST_DIR) / f"{session_id}.json")
    if not manifest_path.exists():
        raise _refuse("work_claude_profile_migration_unknown", "No migration of this session into this profile.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    created = {row["path"]: row["sha256"] for row in manifest.get("created", [])}
    # What an interrupted run published counts as created; its temporary files go.
    kept_files: list[str] = []
    created.update(
        _settle_pending(target_dir, [row for row in manifest.get("pending", []) if isinstance(row, dict)], kept_files)
    )
    files = [(_inside(target_dir, path), digest) for path, digest in created.items()]
    folders = [_inside(target_dir, rel) for rel in manifest.get("created_dirs", [])]
    changed = [
        str(path.relative_to(target_dir)) for path, digest in files
        if path.exists() and (not path.is_file() or _sha256(path) != digest)
    ]
    if changed:
        raise _refuse(
            "work_claude_profile_rollback_conflict",
            "Files the migration created have changed since (the session ran in the profile); nothing was removed.",
            paths=changed,
        )
    removed = 0
    for path, _digest in files:
        if path.exists():
            path.unlink()
            removed += 1
    kept_dirs = []
    for folder in sorted(folders, key=lambda item: len(item.parts), reverse=True):
        try:
            folder.rmdir()  # only an empty directory; never a tree
        except OSError:
            if folder.exists():
                kept_dirs.append(str(folder.relative_to(target_dir)))
    manifest_path.unlink()
    try:
        manifest_path.parent.rmdir()
    except OSError:
        pass
    return {"session_id": session_id, "removed": removed, "kept_files": kept_files, "kept_dirs": kept_dirs}


__all__ = [
    "apply_migration",
    "preserve_login",
    "plan_migration",
    "rollback_migration",
    "session_files",
    "verify_migration",
]

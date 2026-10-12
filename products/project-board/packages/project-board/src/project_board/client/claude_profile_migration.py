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
Never copied: `.claude.json`, credential files, settings, plugins, other
sessions' files. The profile gets its own login and the Problem Board skill
and settings are installed into it separately.

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
        os.close(handle)
        partial = Path(partial_name)
        pending.append({"path": row["path"], "partial": str(partial.relative_to(target_dir))})
        atomic_write_json(manifest_path, _manifest(plan, created, created_dirs, pending))
        shutil.copy2(_inside(source_dir, row["path"]), partial)
        if _sha256(partial) != row["sha256"]:
            partial.unlink()
            raise _refuse("work_claude_profile_copy_mismatch", f"{row['path']} changed while it was copied.")
        if destination.exists() or destination.is_symlink():
            partial.unlink()
            raise _refuse("work_claude_profile_conflict", f"{row['path']} appeared in the profile during the copy.")
        os.replace(partial, destination)
        pending = [item for item in pending if item.get("partial") != str(partial.relative_to(target_dir))]
        created[row["path"]] = row["sha256"]
        # Recorded after every file, so an interrupted run can still be rolled back.
        atomic_write_json(manifest_path, _manifest(plan, created, created_dirs, pending))
    verified = verify_migration(target=target_dir, session_id=session_id, source=source_dir, with_memory=with_memory)
    atomic_write_json(manifest_path, {**_manifest(plan, created, created_dirs, pending), "verified_at": utc_now()})
    return {**plan, "applied": len(created), "verified": verified}


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
    files = [(_inside(target_dir, row["path"]), row["sha256"]) for row in manifest.get("created", [])]
    partials = [_inside(target_dir, row["partial"]) for row in manifest.get("pending", []) if row.get("partial")]
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
    for path in partials:
        if path.is_file():
            path.unlink()
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
    return {"session_id": session_id, "removed": removed, "kept_dirs": kept_dirs}


__all__ = [
    "apply_migration",
    "plan_migration",
    "rollback_migration",
    "session_files",
    "verify_migration",
]

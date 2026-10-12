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

Every apply writes a manifest of what it created. A rollback removes only
those files, only while each still has the hash it was copied with, then the
directories the apply created when they are empty; it never removes a folder
tree.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
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
    return sorted(
        path for path in root.rglob("*")
        if path.is_file() and not path.is_symlink() and path.name not in _NEVER
    )


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

    source_dir = Path(source).expanduser()
    target_dir = Path(target).expanduser()
    if os.path.realpath(source_dir) == os.path.realpath(target_dir):
        raise _refuse("work_claude_profile_same_folder", "The session already lives in this profile folder.")
    rows: list[dict[str, str]] = []
    for relative in session_files(source_dir, session_id, with_memory=with_memory):
        source_hash = _sha256(source_dir / relative)
        destination = target_dir / relative
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
    *, source: str | Path, target: str | Path, session_id: str, now: float | None = None, with_memory: bool = False
) -> dict[str, Any]:
    """Copy the session's files and verify each; refuses on a running session or any conflict.

    A retry after an interrupted run copies what is missing and accepts what
    is already there with the same hash.
    """

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
    previous = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    created = {row["path"]: row["sha256"] for row in previous.get("created", [])}
    created_dirs = set(previous.get("created_dirs", []))
    for row in plan["files"]:
        if row["action"] != "copy":
            continue
        destination = target_dir / row["path"]
        missing = []
        parent = destination.parent
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        destination.parent.mkdir(parents=True, exist_ok=True)
        created_dirs.update(str(path.relative_to(target_dir)) for path in missing if target_dir in path.parents)
        partial = destination.with_name(destination.name + ".pb-partial")
        shutil.copy2(source_dir / row["path"], partial)
        if _sha256(partial) != row["sha256"]:
            partial.unlink()
            raise _refuse("work_claude_profile_copy_mismatch", f"{row['path']} changed while it was copied.")
        os.replace(partial, destination)
        created[row["path"]] = row["sha256"]
        # Recorded after every file, so an interrupted run can still be rolled back.
        atomic_write_json(manifest_path, _manifest(plan, created, created_dirs))
    verified = verify_migration(target=target_dir, session_id=session_id, source=source_dir, with_memory=with_memory)
    atomic_write_json(manifest_path, {**_manifest(plan, created, created_dirs), "verified_at": utc_now()})
    return {**plan, "applied": len(created), "verified": verified}


def _manifest(plan: Mapping[str, Any], created: Mapping[str, str], created_dirs: set[str]) -> dict[str, Any]:
    return {
        "version": 1,
        "session_id": plan["session_id"],
        "source": plan["source"],
        "target": plan["target"],
        "created": [{"path": path, "sha256": digest} for path, digest in sorted(created.items())],
        "created_dirs": sorted(created_dirs),
        "updated_at": utc_now(),
    }


def verify_migration(
    *, target: str | Path, session_id: str, source: str | Path, with_memory: bool = False
) -> dict[str, Any]:
    """Every source file of the session is in the profile with the same hash."""

    source_dir = Path(source).expanduser()
    target_dir = Path(target).expanduser()
    differing = []
    files = session_files(source_dir, session_id, with_memory=with_memory)
    for relative in files:
        destination = target_dir / relative
        if not destination.is_file() or _sha256(destination) != _sha256(source_dir / relative):
            differing.append(str(relative))
    if differing:
        raise _refuse(
            "work_claude_profile_migration_unverified",
            "Some of the session's files are missing or different in the profile.",
            paths=differing,
        )
    return {"files": len(files), "state": "verified"}


def rollback_migration(*, target: str | Path, session_id: str) -> dict[str, Any]:
    """Remove what an apply created, and nothing else; refuses when any of it changed since."""

    target_dir = Path(target).expanduser()
    manifest_path = _manifest_path(target_dir, session_id)
    if not manifest_path.exists():
        raise _refuse("work_claude_profile_migration_unknown", "No migration of this session into this profile.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    changed = [
        row["path"] for row in manifest.get("created", [])
        if (target_dir / row["path"]).exists() and _sha256(target_dir / row["path"]) != row["sha256"]
    ]
    if changed:
        raise _refuse(
            "work_claude_profile_rollback_conflict",
            "Files the migration created have changed since (the session ran in the profile); nothing was removed.",
            paths=changed,
        )
    removed = 0
    for row in manifest.get("created", []):
        path = target_dir / row["path"]
        if path.exists():
            path.unlink()
            removed += 1
    kept_dirs = []
    for relative in sorted(manifest.get("created_dirs", []), key=lambda item: item.count("/"), reverse=True):
        folder = target_dir / relative
        try:
            folder.rmdir()  # only an empty directory; never a tree
        except OSError:
            if folder.exists():
                kept_dirs.append(relative)
    manifest_path.unlink()
    return {"session_id": session_id, "removed": removed, "kept_dirs": kept_dirs}


__all__ = [
    "apply_migration",
    "plan_migration",
    "rollback_migration",
    "session_files",
    "verify_migration",
]

# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Runtime-window database backups in one managed place per host and project (W423).

A coordinator backs up the board tables before each runtime window. Those
dumps went to whichever agent's scratch folder ran the window and nothing
removed them: on 2026-09-30 one host held 24 dumps in one agent's scratch and
9 in another's, and its disk filled. This module gives them one place and one
lifetime:

- The directory is ``<backup root>/<project id>``. The operator chooses the
  root per host (``pb host configure --backup-root``); unset, nothing is
  created and every command says so. The root may not lie inside a Git
  working tree (a backup must never be committed), and the project directory
  is private to the host user (0700, files 0600).
- A manifest (``manifest.json``) in that directory lists every backup the
  coordinator recorded: file, format, size, SHA-256, label, who recorded it
  and its verification. Files the manifest does not list are never touched.
- Verification is specific to the format, and never called restore proof:

  - ``plain-sql-gzip`` (``pg_dump ... | gzip``): the whole gzip stream is
    read, so its CRC and length are checked, and the dump must end with
    pg_dump's completion marker and create at least one table. ``pg_restore
    --list`` cannot read a plain SQL dump and is never used for one.
  - ``pg-custom`` (``pg_dump -Fc``): the archive header is checked and
    ``pg_restore --list`` must read its table of contents.

  Either way the result is labelled integrity, not restore proof: only
  restoring into a scratch database proves a backup restores.
- After the window's ALL CLEAR is verified, pruning keeps the newest backup and
  deletes the other manifest entries. It refuses when the newest one did not
  pass verification or no longer matches its recorded hash, so the kept copy
  is always a verified one. Without ``apply`` it only reports.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..contract.errors import DomainError
from .io import atomic_write_json, exclusive_lock, read_json

MANIFEST_NAME = "manifest.json"
MANIFEST_SCHEMA = "problem-board-backup-manifest/1"
FORMATS: dict[str, str] = {"plain-sql-gzip": ".sql.gz", "pg-custom": ".dump"}
PLAIN_DUMP_HEADER = b"-- PostgreSQL database dump"
PLAIN_DUMP_COMPLETE = b"-- PostgreSQL database dump complete"
CUSTOM_ARCHIVE_MAGIC = b"PGDMP"
PG_RESTORE_TIMEOUT_SECONDS = 300
READ_CHUNK_BYTES = 1 << 20
RESTORE_PROOF_NOTE = (
    "Integrity check, not restore proof: only restoring the backup into a scratch "
    "database proves it restores."
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _inside_git_tree(path: Path) -> Path | None:
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def backup_directory(root: str | Path, project_id: str, *, create: bool = False) -> Path:
    """The project's backup directory under the operator-chosen root."""

    if not str(root or "").strip():
        raise DomainError(
            "backup_root_unset",
            "This host has no backup root. The operator chooses it: "
            "pb host configure --backup-root <absolute path outside any Git tree>.",
            status=409,
        )
    clean_root = Path(str(root)).expanduser()
    if not clean_root.is_absolute():
        raise DomainError("backup_root_invalid", "The backup root must be an absolute path.", status=400)
    tree = _inside_git_tree(clean_root.resolve() if clean_root.exists() else clean_root)
    if tree is not None:
        raise DomainError(
            "backup_root_in_git_tree",
            f"The backup root {clean_root} lies inside the Git working tree {tree}; a backup must never be committed.",
            status=409,
            details={"root": str(clean_root), "git_tree": str(tree)},
        )
    clean_id = str(project_id or "").strip()
    if not clean_id or "/" in clean_id or clean_id in {".", ".."}:
        raise DomainError("backup_project_invalid", "A backup directory needs a project id.", status=400)
    directory = clean_root / clean_id
    if create and not directory.is_dir():
        directory.mkdir(parents=True, mode=0o700)
    if directory.is_dir():
        os.chmod(directory, 0o700)
    return directory


def new_backup_path(directory: Path, backup_format: str, *, now: datetime | None = None) -> Path:
    """Where the next backup of this format is written: ``pb-backup-<UTC time><suffix>``."""

    suffix = _suffix(backup_format)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return directory / f"pb-backup-{stamp}{suffix}"


def _suffix(backup_format: str) -> str:
    try:
        return FORMATS[backup_format]
    except KeyError:
        raise DomainError(
            "backup_format_unknown",
            f"Backup format {backup_format!r} is not one of {', '.join(sorted(FORMATS))}.",
            status=400,
        ) from None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(READ_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_plain_sql_gzip(path: Path) -> dict[str, Any]:
    tables = 0
    head = b""
    tail = b""
    carry = b""
    try:
        with gzip.open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(READ_CHUNK_BYTES), b""):
                if len(head) < 4096:
                    head += chunk[: 4096 - len(head)]
                data = carry + chunk
                lines = data.split(b"\n")
                carry = lines.pop()
                tables += sum(1 for line in lines if line.startswith(b"CREATE TABLE "))
                tail = (tail + chunk)[-4096:]
        if carry.startswith(b"CREATE TABLE "):
            tables += 1
    except (OSError, EOFError, gzip.BadGzipFile) as exc:
        return {"ok": False, "detail": f"gzip stream unreadable: {type(exc).__name__}: {exc}"[:300], "tables": tables}
    problems = []
    if PLAIN_DUMP_HEADER not in head:
        problems.append("no pg_dump header")
    if PLAIN_DUMP_COMPLETE not in tail:
        problems.append("no pg_dump completion marker (truncated dump)")
    if tables == 0:
        problems.append("no CREATE TABLE statement")
    return {
        "ok": not problems,
        "detail": "; ".join(problems) or f"gzip CRC and length valid; pg_dump completion marker present; {tables} tables",
        "tables": tables,
    }


def _verify_pg_custom(path: Path, pg_restore: Sequence[str]) -> dict[str, Any]:
    with path.open("rb") as stream:
        if stream.read(len(CUSTOM_ARCHIVE_MAGIC)) != CUSTOM_ARCHIVE_MAGIC:
            return {"ok": False, "detail": "not a pg_dump custom-format archive (no PGDMP header)", "tables": 0}
    command = [*pg_restore, "--list"]
    try:
        with path.open("rb") as stream:
            completed = subprocess.run(
                command, stdin=stream, capture_output=True, timeout=PG_RESTORE_TIMEOUT_SECONDS, check=False
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "detail": f"{shlex.join(command)} did not run: {exc}"[:300], "tables": 0}
    listing = completed.stdout.decode("utf-8", "replace")
    tables = sum(1 for line in listing.splitlines() if " TABLE " in line and " TABLE DATA " not in line)
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", "replace").strip()[:300]
        return {"ok": False, "detail": f"{shlex.join(command)} failed: {detail}", "tables": tables}
    return {
        "ok": tables > 0,
        "detail": f"{shlex.join(command)} read the table of contents; {tables} tables"
        if tables
        else "the table of contents lists no table",
        "tables": tables,
    }


def verify_backup(path: Path, backup_format: str, *, pg_restore: Sequence[str] = ("pg_restore",)) -> dict[str, Any]:
    """The format's own integrity check. Never restore proof (``restore_proof`` is always false)."""

    _suffix(backup_format)
    if backup_format == "plain-sql-gzip":
        result = _verify_plain_sql_gzip(path)
        method = "gzip stream read in full (CRC, length) + pg_dump header and completion marker + CREATE TABLE count"
    else:
        result = _verify_pg_custom(path, pg_restore)
        method = "PGDMP header + pg_restore --list"
    return {**result, "method": method, "restore_proof": False, "note": RESTORE_PROOF_NOTE, "at": _now()}


def _manifest_path(directory: Path) -> Path:
    return directory / MANIFEST_NAME


def read_manifest(directory: Path) -> dict[str, Any]:
    value = read_json(_manifest_path(directory), required=False) or {}
    return {
        "schema": MANIFEST_SCHEMA,
        "backups": [dict(item) for item in value.get("backups") or [] if isinstance(item, Mapping)],
        "prunes": [dict(item) for item in value.get("prunes") or [] if isinstance(item, Mapping)],
        **{key: value[key] for key in ("project_id",) if key in value},
    }


def _lock(directory: Path):
    return exclusive_lock(directory / ".manifest.lock")


def record_backup(
    directory: Path,
    file: str | Path,
    backup_format: str,
    *,
    label: str,
    recorded_by: str,
    pg_restore: Sequence[str] = ("pg_restore",),
) -> dict[str, Any]:
    """Verify a backup written into the directory and add it to the manifest."""

    suffix = _suffix(backup_format)
    path = Path(str(file)).expanduser()
    if not path.is_absolute():
        path = directory / path
    if path.is_symlink() or not path.is_file():
        raise DomainError("backup_file_missing", f"{path} is not a regular file.", status=404)
    if path.resolve().parent != directory.resolve():
        raise DomainError(
            "backup_outside_directory",
            f"Record only a backup written into {directory}; {path} lies elsewhere.",
            status=400,
            details={"directory": str(directory), "file": str(path)},
        )
    if not path.name.endswith(suffix):
        raise DomainError(
            "backup_format_mismatch",
            f"A {backup_format} backup ends with {suffix}; {path.name} does not.",
            status=400,
        )
    os.chmod(path, 0o600)
    verification = verify_backup(path, backup_format, pg_restore=pg_restore)
    stat = path.stat()
    entry = {
        "file": path.name,
        "format": backup_format,
        "bytes": stat.st_size,
        "sha256": _sha256(path),
        "created_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "recorded_at": _now(),
        "recorded_by": recorded_by,
        "label": str(label or "")[:200],
        "verification": verification,
    }
    with _lock(directory):
        manifest = read_manifest(directory)
        manifest["project_id"] = directory.name
        manifest["backups"] = [item for item in manifest["backups"] if item.get("file") != path.name] + [entry]
        atomic_write_json(_manifest_path(directory), manifest)
    os.chmod(_manifest_path(directory), 0o600)
    return entry


def _newest_first(backups: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return sorted((dict(item) for item in backups), key=lambda item: (str(item.get("created_at") or ""), str(item.get("file") or "")), reverse=True)


def backup_report(directory: Path) -> dict[str, Any]:
    """The manifest's backups newest first, and files here the manifest does not list."""

    manifest = read_manifest(directory)
    listed = {str(item.get("file") or "") for item in manifest["backups"]}
    unmanaged = sorted(
        child.name
        for child in (directory.iterdir() if directory.is_dir() else ())
        if child.name not in listed and child.name not in {MANIFEST_NAME, ".manifest.lock"}
    )
    backups = []
    for item in _newest_first(manifest["backups"]):
        path = directory / str(item.get("file") or "")
        backups.append({**item, "present": path.is_file()})
    return {
        "directory": str(directory),
        "backups": backups,
        "unmanaged": unmanaged,
        "total_bytes": sum(int(item.get("bytes") or 0) for item in backups if item["present"]),
    }


def prune_backups(
    directory: Path,
    *,
    all_clear: str,
    pruned_by: str,
    apply: bool = False,
) -> dict[str, Any]:
    """After a verified ALL CLEAR, keep the newest backup and delete the other manifest entries."""

    evidence = str(all_clear or "").strip()
    if not evidence:
        raise DomainError(
            "backup_prune_needs_all_clear",
            "Prune only after the window's ALL CLEAR is verified: pass --all-clear with its evidence "
            "(the receipt or message that verified it).",
            status=409,
        )
    with _lock(directory):
        manifest = read_manifest(directory)
        ordered = _newest_first(manifest["backups"])
        if not ordered:
            return {"directory": str(directory), "state": "nothing_recorded", "kept": None, "deleted": [], "would_delete": []}
        newest = ordered[0]
        newest_path = directory / str(newest.get("file") or "")
        refusal = ""
        if not (newest.get("verification") or {}).get("ok"):
            refusal = f"the newest backup {newest.get('file')} did not pass verification; nothing is pruned"
        elif not newest_path.is_file():
            refusal = f"the newest backup {newest.get('file')} is missing; nothing is pruned"
        elif _sha256(newest_path) != newest.get("sha256"):
            refusal = f"the newest backup {newest.get('file')} no longer matches its recorded SHA-256; nothing is pruned"
        if refusal:
            raise DomainError(
                "backup_prune_newest_unverified",
                refusal + ". Take and record a new backup first.",
                status=409,
                details={"newest": newest.get("file")},
            )
        others = ordered[1:]
        if not apply:
            return {
                "directory": str(directory),
                "state": "report_only",
                "kept": newest.get("file"),
                "would_delete": [item.get("file") for item in others],
                "freed_bytes": sum(int(item.get("bytes") or 0) for item in others),
            }
        deleted: list[str] = []
        failed: list[dict[str, str]] = []
        for item in others:
            path = directory / str(item.get("file") or "")
            try:
                if path.is_symlink() or (path.exists() and not path.is_file()):
                    raise OSError("not a regular file")
                path.unlink(missing_ok=True)
                deleted.append(str(item.get("file")))
            except OSError as exc:
                failed.append({"file": str(item.get("file")), "reason": str(exc)[:200]})
        failed_files = {row["file"] for row in failed}
        manifest["backups"] = [newest] + [item for item in others if str(item.get("file")) in failed_files]
        manifest["prunes"] = (manifest["prunes"] + [{
            "at": _now(),
            "by": pruned_by,
            "all_clear": evidence[:500],
            "kept": newest.get("file"),
            "deleted": deleted,
        }])[-50:]
        manifest["project_id"] = directory.name
        atomic_write_json(_manifest_path(directory), manifest)
    return {
        "directory": str(directory),
        "state": "pruned",
        "kept": newest.get("file"),
        "deleted": deleted,
        "failed": failed,
        "freed_bytes": sum(int(item.get("bytes") or 0) for item in others if str(item.get("file")) in deleted),
    }


def pg_restore_command(value: str) -> list[str]:
    """``--pg-restore "docker exec -i <container> pg_restore"``: the archive is read from stdin."""

    parts = shlex.split(str(value or "").strip())
    return parts or ["pg_restore"]


__all__ = [
    "FORMATS",
    "MANIFEST_NAME",
    "RESTORE_PROOF_NOTE",
    "backup_directory",
    "backup_report",
    "new_backup_path",
    "pg_restore_command",
    "prune_backups",
    "read_manifest",
    "record_backup",
    "verify_backup",
]

# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Managed scratch: work files that are not git trees, kept until their content is safe (W423).

Agents write reports, probes, payloads and logs while they work. Left at the
workspace root they piled up with no owner, no purpose and no way to tell a
unique finding from a regenerable log (2026-10-01: 225 loose files in one
workspace, several of them the only copy of a review result).

Every such file lives in one run folder, ``<workspace>/scratch/<item>/<run>/``,
made by ``pb worker scratch new``. The folder's ``.run.json`` says who owns it,
which item it serves and why it exists, and, per file, its hash and how it can
be let go: published (where its content now lives) or generated (the command
that makes it again). The owner closes the run when the job is over, naming
where the run's findings are published.

A run is removed only when every one of these holds:

- this agent owns it and closed it, and its item is Done or Cancelled on the
  board (an open item, or a state that cannot be read, keeps it);
- the findings it names are verified published (a tracked file at a commit in
  the clone's default branch); a reference that cannot be verified here keeps
  the run;
- no consumer it lists is still open;
- every file on disk is in the manifest, is not a link, still has its recorded
  hash, and is published or generated;
- the last dry run (sweep_plan) listed it with the same fingerprint, and a
  receipt is written before anything is deleted.

Anything unknown, unreadable, changed or foreign keeps the data, and the report
names why. Age is shown as a signal for review and never removes anything.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..contract.errors import DomainError
from .io import atomic_write_json, exclusive_lock, read_json

SCRATCH_FOLDER = "scratch"
MANIFEST_NAME = ".run.json"
RECEIPT_FOLDER = Path(".problem-board") / "scratch-receipts"
SCHEMA = "problem-board.scratch-run.v1"
GIT_TIMEOUT_SECONDS = 20

_ITEM = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_REPOSITORY_REF = re.compile(r"^repo:(?P<alias>[A-Za-z0-9._-]+)/(?P<path>[^@]+)@(?P<commit>[0-9a-fA-F]{7,64})$")

Verifier = Callable[[str], "bool | None"]
Consumers = Callable[[str], "Sequence[str] | None"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def scratch_root(workspace: Path | str) -> Path:
    return Path(workspace).expanduser() / SCRATCH_FOLDER


def _run_dir(workspace: Path | str, run: Path | str) -> Path:
    """The run folder, which must be a real folder two levels under scratch/."""

    root = scratch_root(workspace)
    path = Path(run).expanduser()
    if not path.is_absolute():
        path = root / path
    if path.is_symlink() or not path.is_dir() or not _inside(path, root):
        raise DomainError("scratch_run_unknown", f"{run} is not a run folder under {root}.")
    if path.resolve().parent.parent != root.resolve():
        raise DomainError("scratch_run_unknown", f"{run} is not scratch/<item>/<run> under {root}.")
    return path


def _read_manifest(run: Path) -> dict[str, Any]:
    manifest = read_json(run / MANIFEST_NAME, required=True)
    if manifest.get("schema") != SCHEMA:
        raise DomainError("scratch_manifest_invalid", f"{run / MANIFEST_NAME} is not a scratch run manifest.")
    return manifest


def new_run(
    workspace: Path | str,
    *,
    item: str,
    purpose: str,
    worker_name: str,
    session: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Make one run folder for one item, with its manifest written atomically."""

    item = str(item or "").strip()
    purpose = str(purpose or "").strip()
    if not _ITEM.fullmatch(item):
        raise DomainError("scratch_item_invalid", "--item is an item key such as W423.")
    if not purpose:
        raise DomainError("scratch_purpose_required", "--purpose says why this run exists.")
    workspace = Path(workspace).expanduser()
    if not workspace.is_dir():
        raise DomainError("scratch_workspace_missing", f"The workspace {workspace} does not exist.")
    moment = now or _now()
    run_id = f"{moment.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"
    run = scratch_root(workspace) / item / run_id
    run.mkdir(parents=True, exist_ok=False)
    manifest = {
        "schema": SCHEMA,
        "run_id": run_id,
        "item": item,
        "purpose": purpose,
        "owner": worker_name,
        "session": session,
        "created_at": _iso(moment),
        "files": {},
        "consumers": {},
        "closed": None,
    }
    atomic_write_json(run / MANIFEST_NAME, manifest)
    return {"run": str(run), **manifest}


def record(
    workspace: Path | str,
    run: Path | str,
    *,
    worker_name: str,
    file: str = "",
    published: str = "",
    generated_by: str = "",
    consumer: str = "",
    consumer_done: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record one file's hash and disposition, or a consumer that pins the run."""

    path = _run_dir(workspace, run)
    with exclusive_lock(path / f"{MANIFEST_NAME}.lock"):
        manifest = _read_manifest(path)
        if manifest.get("owner") != worker_name:
            raise DomainError("scratch_run_not_owned", f"{path} belongs to {manifest.get('owner')}.")
        moment = _iso(now or _now())
        if file:
            if bool(published) == bool(generated_by):
                raise DomainError(
                    "scratch_disposition_required",
                    "Give exactly one of --published <ref> or --generated-by <command that makes it again>.",
                )
            target = path / file
            if target.is_symlink() or not target.is_file() or not _inside(target, path):
                raise DomainError("scratch_file_unknown", f"{file} is not a regular file in {path}.")
            name = target.relative_to(path).as_posix()
            if name == MANIFEST_NAME:
                raise DomainError("scratch_file_unknown", "The manifest is not a recorded file.")
            manifest["files"][name] = {
                "sha256": _sha256(target),
                "size": target.stat().st_size,
                "published": published,
                "generated_by": generated_by,
                "recorded_at": moment,
            }
        if consumer:
            manifest["consumers"][consumer] = {"open": True, "recorded_at": moment}
        if consumer_done:
            if consumer_done not in manifest["consumers"]:
                raise DomainError("scratch_consumer_unknown", f"{consumer_done} is not a consumer of this run.")
            manifest["consumers"][consumer_done] = {**manifest["consumers"][consumer_done], "open": False, "done_at": moment}
        if not (file or consumer or consumer_done):
            raise DomainError("scratch_record_empty", "Name --file, --consumer or --consumer-done.")
        atomic_write_json(path / MANIFEST_NAME, manifest)
    return {"run": str(path), **manifest}


def close(
    workspace: Path | str,
    run: Path | str,
    *,
    worker_name: str,
    reason: str,
    findings: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The owner's terminal disposition: the job is over and its findings live at ``findings``."""

    if not str(reason or "").strip():
        raise DomainError("scratch_reason_required", "--reason says why the job is over.")
    if not str(findings or "").strip():
        raise DomainError(
            "scratch_findings_required",
            "--findings names where this run's findings are published (repo:<alias>/<path>@<commit>).",
        )
    path = _run_dir(workspace, run)
    with exclusive_lock(path / f"{MANIFEST_NAME}.lock"):
        manifest = _read_manifest(path)
        if manifest.get("owner") != worker_name:
            raise DomainError("scratch_run_not_owned", f"{path} belongs to {manifest.get('owner')}.")
        manifest["closed"] = {"reason": reason, "findings": findings, "closed_at": _iso(now or _now())}
        atomic_write_json(path / MANIFEST_NAME, manifest)
    return {"run": str(path), **manifest}


def repository_verifier(workspace: Path | str) -> Verifier:
    """Verify ``repo:<alias>/<path>@<commit>``: the file at that commit, in the clone's default branch.

    Returns True when proved, False when disproved, None when this host cannot
    tell (no clone, no default branch, git unreadable). Board references are
    not verified here and return None, so they keep the run.
    """

    base = Path(workspace).expanduser()

    def git(clone: Path, *args: str) -> int:
        try:
            return subprocess.run(
                ["git", "--no-optional-locks", "-C", str(clone), *args],
                capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS, check=False,
                stdin=subprocess.DEVNULL,
            ).returncode
        except (OSError, subprocess.TimeoutExpired):
            return -1

    def verify(ref: str) -> bool | None:
        match = _REPOSITORY_REF.fullmatch(str(ref or "").strip())
        if not match:
            return None
        clone = base / match["alias"]
        if not (clone / ".git").is_dir():
            return None
        default = ""
        for candidate in ("origin/HEAD", "origin/main", "origin/master"):
            if git(clone, "rev-parse", "--verify", "--quiet", candidate) == 0:
                default = candidate
                break
        if not default:
            return None
        commit = match["commit"]
        if git(clone, "cat-file", "-e", f"{commit}^{{commit}}") != 0:
            return False
        if git(clone, "merge-base", "--is-ancestor", commit, default) != 0:
            return False
        return git(clone, "cat-file", "-e", f"{commit}:{match['path']}") == 0

    return verify


@dataclass
class Run:
    path: Path
    item: str = ""
    owner: str = ""
    purpose: str = ""
    created_at: str = ""
    age_hours: float | None = None
    files: list[str] = field(default_factory=list)
    fingerprint: str = ""
    keep: list[str] = field(default_factory=list)
    size_bytes: int = 0

    @property
    def removable(self) -> bool:
        return not self.keep

    def to_mapping(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "kind": "scratch",
            "item": self.item,
            "owner": self.owner,
            "purpose": self.purpose,
            "created_at": self.created_at,
            "age_hours": self.age_hours,
            "files": list(self.files),
            "keep": list(self.keep),
            "action": "remove" if self.removable else "keep",
            "size_bytes": self.size_bytes,
        }


def _judge(
    path: Path,
    root: Path,
    *,
    worker_name: str,
    verify: Verifier,
    consumers: Consumers | None,
    protected: Sequence[Path],
    now: datetime,
) -> Run:
    run = Run(path=path)
    if path.is_symlink() or not _inside(path, root):
        run.keep.append("a link or a folder outside scratch/: owner not proved")
        return run
    for current in path.rglob("*"):
        try:
            if current.is_file() and not current.is_symlink():
                run.size_bytes += current.stat().st_size
        except OSError:
            pass
    if any(_inside(path, guard) or _inside(guard, path) for guard in protected):
        run.keep.append("protected path")
    try:
        manifest = _read_manifest(path)
    except DomainError as exc:
        run.keep.append(f"no readable manifest ({exc.code}): owner and purpose unknown")
        return run
    run.item = str(manifest.get("item") or "")
    run.owner = str(manifest.get("owner") or "")
    run.purpose = str(manifest.get("purpose") or "")
    run.created_at = str(manifest.get("created_at") or "")
    try:
        created = datetime.fromisoformat(run.created_at.replace("Z", "+00:00"))
        run.age_hours = round((now - created).total_seconds() / 3600, 1)
    except ValueError:
        run.age_hours = None
    if run.owner != worker_name:
        run.keep.append(f"owned by {run.owner or 'nobody named'}")
    closed = manifest.get("closed") or {}
    semantic: list[str] = []
    if consumers is not None:
        found = consumers(run.item)
        semantic.append(f"item:{run.item}:{'unknown' if found is None else ','.join(found)}")
        if found is None:
            run.keep.append(f"consumer state unknown (item {run.item}; offline or unreadable)")
        elif found:
            run.keep.append(f"still needed: {', '.join(found)}")
    if not closed:
        run.keep.append("not closed by its owner: the job is not over")
    else:
        findings = str(closed.get("findings") or "")
        proved = verify(findings)
        semantic.append(f"findings:{findings}:{proved}")
        if proved is not True:
            run.keep.append(f"findings publication {'disproved' if proved is False else 'not verifiable here'}: {findings}")
    open_consumers = sorted(name for name, state in (manifest.get("consumers") or {}).items() if (state or {}).get("open"))
    if open_consumers:
        run.keep.append(f"still used by {', '.join(open_consumers)}")
    recorded: Mapping[str, Mapping[str, Any]] = manifest.get("files") or {}
    fingerprint_parts: list[str] = [json.dumps(closed, sort_keys=True), *semantic,
                                    json.dumps(manifest.get("consumers") or {}, sort_keys=True)]
    for current in sorted(path.rglob("*")):
        relative = current.relative_to(path).as_posix()
        if relative == MANIFEST_NAME or relative.startswith(f"{MANIFEST_NAME}.lock"):
            continue
        if current.is_symlink():
            run.keep.append(f"link inside the run: {relative}")
            continue
        if current.is_dir():
            continue
        run.files.append(relative)
        entry = recorded.get(relative)
        if entry is None:
            run.keep.append(f"not in the manifest: {relative}")
            continue
        try:
            digest = _sha256(current)
        except OSError:
            run.keep.append(f"unreadable: {relative}")
            continue
        fingerprint_parts.append(f"{relative}:{digest}")
        if digest != entry.get("sha256"):
            run.keep.append(f"changed since it was recorded: {relative}")
            continue
        published = str(entry.get("published") or "")
        if published:
            proved = verify(published)
            fingerprint_parts.append(f"{relative}:published:{published}:{proved}")
            if proved is not True:
                run.keep.append(f"publication {'disproved' if proved is False else 'not verifiable here'}: {relative} -> {published}")
        elif not str(entry.get("generated_by") or ""):
            run.keep.append(f"unpublished: {relative}")
    run.fingerprint = hashlib.sha256("\n".join(fingerprint_parts).encode("utf-8")).hexdigest()
    return run


def inspect_runs(
    workspace: Path | str,
    *,
    worker_name: str,
    verify: Verifier | None = None,
    consumers: Consumers | None = None,
    protected: Sequence[Path | str] = (),
    now: datetime | None = None,
) -> list[Run]:
    """Every run under scratch/ with its state and the decision. Reads only.

    ``consumers`` answers, for an item key, what still needs it: an empty list
    when the item is Done or Cancelled, None when its state cannot be read.
    """

    root = scratch_root(workspace)
    if not root.is_dir():
        return []
    check = verify or repository_verifier(workspace)
    guards = [Path(str(path)).expanduser() for path in protected if str(path)]
    moment = now or _now()
    runs: list[Run] = []
    for item in sorted(root.iterdir()):
        if item.is_symlink() or not item.is_dir():
            runs.append(Run(path=item, keep=["not an item folder (a link or a file directly in scratch/)"]))
            continue
        for path in sorted(item.iterdir()):
            if path.is_symlink() or not path.is_dir():
                runs.append(Run(path=path, item=item.name, keep=["not a run folder (a link or a loose file)"]))
                continue
            runs.append(_judge(path, root, worker_name=worker_name, verify=check, consumers=consumers,
                               protected=guards, now=moment))
    return runs


def loose_entries(workspace: Path | str, *, known: Sequence[str] = (), now: datetime | None = None) -> list[dict[str, Any]]:
    """Top-level entries that are not clones, trees or scratch: named, never removed."""

    base = Path(workspace).expanduser()
    if not base.is_dir():
        return []
    skip = {SCRATCH_FOLDER, "wt", "rv", ".problem-board", *known}
    moment = (now or _now()).timestamp()
    rows: list[dict[str, Any]] = []
    for entry in sorted(base.iterdir()):
        if entry.name in skip or (entry.is_dir() and (entry / ".git").is_dir()):
            continue
        try:
            stat = entry.lstat()
        except OSError:
            continue
        rows.append({
            "path": str(entry),
            "kind": "link" if entry.is_symlink() else ("folder" if entry.is_dir() else "file"),
            "age_hours": round((moment - stat.st_mtime) / 3600, 1),
            "keep": "loose at the workspace root: move it into a run with pb worker scratch new",
        })
    return rows


def apply_runs(
    workspace: Path | str,
    *,
    worker_name: str,
    planned: Mapping[str, str],
    verify: Verifier | None = None,
    consumers: Consumers | None = None,
    protected: Sequence[Path | str] = (),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Remove the runs the last dry run found removable, after judging each again.

    Each run is judged again from disk, its fingerprint must match the dry
    run's, and its receipt is written before the folder is deleted. A run that
    changed, or that the dry run did not list, is kept.
    """

    moment = now or _now()
    runs = inspect_runs(workspace, worker_name=worker_name, verify=verify, consumers=consumers,
                        protected=protected, now=moment)
    removed: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    receipts = Path(workspace).expanduser() / RECEIPT_FOLDER
    for run in runs:
        if run.removable and planned.get(str(run.path)) != run.fingerprint:
            run.keep.append("not listed with this content by the last dry run: run --sweep first")
        if not run.removable:
            kept.append(run.to_mapping())
            continue
        receipt = receipts / f"{moment.strftime('%Y%m%dT%H%M%SZ')}-{run.item}-{run.path.name}.json"
        try:
            receipt.parent.mkdir(parents=True, exist_ok=True)
            manifest = _read_manifest(run.path)
            atomic_write_json(receipt, {
                "removed_at": _iso(moment),
                "run": str(run.path),
                "fingerprint": run.fingerprint,
                "manifest": manifest,
                "size_bytes": run.size_bytes,
            })
            shutil.rmtree(run.path)
            if run.path.parent.is_dir() and not any(run.path.parent.iterdir()):
                run.path.parent.rmdir()
        except (OSError, DomainError) as exc:
            failed.append({"path": str(run.path), "reason": str(exc)[:500]})
            continue
        removed.append({**run.to_mapping(), "receipt": str(receipt)})
    return {
        "removed": removed,
        "kept": kept,
        "failed": failed,
        "freed_bytes": sum(int(row.get("size_bytes") or 0) for row in removed),
    }


__all__ = [
    "Run", "apply_runs", "close", "inspect_runs", "loose_entries", "new_run", "record",
    "repository_verifier", "scratch_root",
]

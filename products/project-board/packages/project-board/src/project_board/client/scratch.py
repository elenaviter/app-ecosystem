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
- the findings it names are verified published: a tracked file at a commit in
  the clone's default branch, or an applied note on the run's own item read
  from the board now; a reference that cannot be verified keeps the run;
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
from typing import Any, Callable, Collection, Mapping, Sequence

from ..contract.errors import DomainError
from .io import atomic_write_json, exclusive_lock, read_json

SCRATCH_FOLDER = "scratch"
MANIFEST_NAME = ".run.json"
RECEIPT_FOLDER = Path(".problem-board") / "scratch-receipts"
SCHEMA = "problem-board.scratch-run.v1"
GIT_TIMEOUT_SECONDS = 20

_ITEM = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_REPOSITORY_REF = re.compile(r"^repo:(?P<alias>[A-Za-z0-9._-]+)/(?P<path>[^@]+)@(?P<commit>[0-9a-fA-F]{7,64})$")
_NOTE_REF = re.compile(r"^work:note:\S+$")

Verifier = Callable[..., "bool | None"]
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

    With ``sha256``, the published blob must also hash to it: a reference to
    another version of the file, or to another file, does not cover the
    content. Returns True when proved, False when disproved, None when this
    host cannot tell (no clone, no default branch, git unreadable). Board
    references are not verified here and return None, so they keep the run;
    ``publication_verifier`` adds the board.
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

    def blob_sha256(clone: Path, spec: str) -> str | None:
        try:
            completed = subprocess.run(
                ["git", "--no-optional-locks", "-C", str(clone), "cat-file", "blob", spec],
                capture_output=True, timeout=GIT_TIMEOUT_SECONDS, check=False, stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return hashlib.sha256(completed.stdout).hexdigest() if completed.returncode == 0 else None

    def verify(ref: str, sha256: str = "", item: str = "") -> bool | None:
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
        if git(clone, "cat-file", "-e", f"{commit}:{match['path']}") != 0:
            return False
        if not sha256:
            return True
        published = blob_sha256(clone, f"{commit}:{match['path']}")
        return None if published is None else published == sha256

    return verify


NoteReader = Callable[[str], "Sequence[Mapping[str, Any]] | None"]


def board_note_verifier(read_notes: NoteReader) -> Verifier:
    """Verify ``work:note:<...>``: an applied note on the run's own item, read from the board now.

    ``read_notes(item)`` returns that item's notes, every page, or None when the
    board cannot be read. The note must be on the run's item: a note on another
    item, or a run with no item, is not proved. With ``sha256`` (a recorded
    file), the note's text must hash to it: the note holds the same content, not
    a summary. Without it (the run's findings), the note's presence is enough.
    Returns True when proved, False when disproved, None when this host cannot
    tell, which keeps the run.
    """

    def verify(ref: str, sha256: str = "", item: str = "") -> bool | None:
        ref = str(ref or "").strip()
        if not _NOTE_REF.fullmatch(ref) or not str(item or "").strip():
            return None
        try:
            notes = read_notes(str(item).strip())
        except Exception:  # noqa: BLE001 - an unreadable board proves nothing
            return None
        if notes is None:
            return None
        note = next((entry for entry in notes if str((entry or {}).get("note_ref") or "") == ref), None)
        if note is None:
            return False
        if not sha256:
            return True
        text = (note or {}).get("text")
        if not isinstance(text, str):
            return None
        return hashlib.sha256(text.encode("utf-8")).hexdigest() == sha256

    return verify


def publication_verifier(workspace: Path | str, read_notes: NoteReader | None = None) -> Verifier:
    """The verifier a sweep uses: repository references through git, item notes through the board."""

    repository = repository_verifier(workspace)
    board = board_note_verifier(read_notes) if read_notes is not None else None

    def verify(ref: str, sha256: str = "", item: str = "") -> bool | None:
        if board is not None and _NOTE_REF.fullmatch(str(ref or "").strip()):
            return board(ref, sha256=sha256, item=item)
        return repository(ref, sha256=sha256, item=item)

    return verify


# How much of one run a report shows. A run can hold hundreds of thousands of
# files (2026-10-03: a sweep report of 350 MB), so the report names a sample
# and counts the rest; the decision itself still reads every file it needs.
FILE_SAMPLE = 20
FILE_REASON_DETAIL = 5


@dataclass
class Run:
    path: Path
    item: str = ""
    owner: str = ""
    purpose: str = ""
    created_at: str = ""
    age_hours: float | None = None
    # The first FILE_SAMPLE files, and how many there are. None: not counted,
    # because a run-level reason already keeps the run.
    files: list[str] = field(default_factory=list)
    files_count: int | None = None
    fingerprint: str = ""
    keep: list[str] = field(default_factory=list)
    # None: not measured (measurement is opt-in), never a claim of zero.
    size_bytes: int | None = None

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
            "files_count": "not counted" if self.files_count is None else self.files_count,
            "keep": list(self.keep),
            "action": "remove" if self.removable else "keep",
            "size_bytes": "not measured" if self.size_bytes is None else self.size_bytes,
        }


def _size_of(path: Path) -> int:
    total = 0
    for current in path.rglob("*"):
        try:
            if current.is_file() and not current.is_symlink():
                total += current.stat().st_size
        except OSError:
            pass
    return total


class _FileReasons:
    """File-level keep reasons: the first few in full, the rest counted by class."""

    def __init__(self, run: Run) -> None:
        self._run = run
        self._detailed = 0
        self._more: dict[str, int] = {}

    def add(self, kind: str, detail: str) -> None:
        if self._detailed < FILE_REASON_DETAIL:
            self._run.keep.append(f"{kind}: {detail}")
            self._detailed += 1
        else:
            self._more[kind] = self._more.get(kind, 0) + 1

    def close(self, unchecked: int) -> None:
        for kind, count in sorted(self._more.items()):
            self._run.keep.append(f"and {count} more files: {kind}")
        if unchecked:
            self._run.keep.append(
                f"{unchecked} files after the first kept file were not hashed or verified"
            )


def _judge(
    path: Path,
    root: Path,
    *,
    worker_name: str,
    verify: Verifier,
    consumers: Consumers | None,
    protected: Sequence[Path],
    now: datetime,
    measure: bool = False,
) -> Run:
    """Decide one run. The run-level reasons are read first and cheaply; a run
    one of them keeps is not walked or hashed, because nothing in its files can
    make it removable. A run that passes them is read file by file exactly as
    the plan and --apply need; hashing stops at the first file that keeps it,
    and the remaining files are only counted."""

    run = Run(path=path)
    if path.is_symlink() or not _inside(path, root):
        run.keep.append("a link or a folder outside scratch/: owner not proved")
        return run
    if measure:
        run.size_bytes = _size_of(path)
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
    # Run-level reasons, cheapest first. Any of them keeps the run, and nothing
    # in its files can change that, so the run is returned before the next,
    # costlier check: the manifest alone, then the board, then git. A kept run
    # is never listed by the plan, so it needs no fingerprint.
    if run.owner != worker_name:
        run.keep.append(f"owned by {run.owner or 'nobody named'}")
    closed = manifest.get("closed") or {}
    if not closed:
        run.keep.append("not closed by its owner: the job is not over")
    open_consumers = sorted(name for name, state in (manifest.get("consumers") or {}).items() if (state or {}).get("open"))
    if open_consumers:
        run.keep.append(f"still used by {', '.join(open_consumers)}")
    if run.keep:
        return run
    semantic: list[str] = []
    if consumers is not None:
        found = consumers(run.item)
        semantic.append(f"item:{run.item}:{'unknown' if found is None else ','.join(found)}")
        if found is None:
            run.keep.append(f"consumer state unknown (item {run.item}; offline or unreadable)")
        elif found:
            run.keep.append(f"still needed: {', '.join(found)}")
    if run.keep:
        return run
    findings = str(closed.get("findings") or "")
    proved = verify(findings, item=run.item)
    semantic.append(f"findings:{findings}:{proved}")
    if proved is not True:
        run.keep.append(f"findings publication {'disproved' if proved is False else 'not verifiable here'}: {findings}")
        return run
    recorded: Mapping[str, Mapping[str, Any]] = manifest.get("files") or {}
    fingerprint_parts: list[str] = [json.dumps(closed, sort_keys=True), *semantic,
                                    json.dumps(manifest.get("consumers") or {}, sort_keys=True)]
    reasons = _FileReasons(run)
    files_count = 0
    unchecked = 0
    for current in sorted(path.rglob("*")):
        relative = current.relative_to(path).as_posix()
        if relative == MANIFEST_NAME or relative.startswith(f"{MANIFEST_NAME}.lock"):
            continue
        if current.is_symlink():
            reasons.add("link inside the run", relative)
            continue
        if current.is_dir():
            continue
        files_count += 1
        if len(run.files) < FILE_SAMPLE:
            run.files.append(relative)
        entry = recorded.get(relative)
        if entry is None:
            reasons.add("not in the manifest", relative)
            continue
        if run.keep:
            # A file already keeps the run: the rest are counted, not hashed.
            unchecked += 1
            continue
        try:
            digest = _sha256(current)
        except OSError:
            reasons.add("unreadable", relative)
            continue
        fingerprint_parts.append(f"{relative}:{digest}")
        if digest != entry.get("sha256"):
            reasons.add("changed since it was recorded", relative)
            continue
        published = str(entry.get("published") or "")
        if published:
            # A copy must be the same content, not only a file at that path.
            proved = verify(published, sha256=digest, item=run.item)
            fingerprint_parts.append(f"{relative}:published:{published}:{proved}")
            if proved is not True:
                reasons.add(
                    f"publication {'disproved' if proved is False else 'not verifiable here'}",
                    f"{relative} -> {published}",
                )
        elif not str(entry.get("generated_by") or ""):
            reasons.add("unpublished", relative)
    reasons.close(unchecked)
    run.files_count = files_count
    if not run.keep:
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
    measure: bool = False,
    measure_paths: Collection[str] = (),
) -> list[Run]:
    """Every run under scratch/ with its state and the decision. Reads only.

    ``consumers`` answers, for an item key, what still needs it: an empty list
    when the item is Done or Cancelled, None when its state cannot be read.
    ``measure`` adds each run's size, which walks every file of every run;
    ``measure_paths`` adds it for those runs only. A run is measured before
    it is judged, so a change during the measurement fails the judgment.
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
                               protected=guards, now=moment,
                               measure=measure or str(path) in measure_paths))
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
    # A run the plan lists is measured for its receipt while it is judged
    # again, before its files are hashed: a measurement after the last check
    # would leave a window in which a changed file is deleted unchecked.
    runs = inspect_runs(workspace, worker_name=worker_name, verify=verify, consumers=consumers,
                        protected=protected, now=moment, measure_paths=set(planned))
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

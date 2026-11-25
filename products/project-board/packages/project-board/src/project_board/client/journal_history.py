"""The published content history of a journal entry, read from Git (W1031 slice 1).

The coordinator's ruling on W1031 Q1 (25 Nov 2026): no board event; the update
history must be rebuildable from Git. This module reads it and claims nothing Git
does not hold:

- **Published content versions** are the first-parent commits of the journal home's
  integration ref (``origin/main`` by default) whose change against their first
  parent touches the entry's file. A merge or a squash onto main is one version;
  intermediate branch commits never were on main and are not versions. A revert is
  a new version. A rename is followed only when both sides carry the same
  ``entry_ref``.
- **Intent and actor** come only from durable commit trailers on a commit that the
  first-parent step introduced and that touches the path, with both keys in the
  message's final paragraph (Git reads trailers there only):
  ``Journal-Update: <entry_ref>`` and ``Journal-Worker: <stable worker name>``.
  Without them a later version's intent and worker are ``unknown``; nothing is
  synthesized from the author name or the commit time.
- **Completeness** is reported, never assumed: a shallow clone or an unreadable
  object makes ``complete`` false with the reason.

Local indexing operations (when this host indexed which version) are not part of
this history; the receipt records them separately and labels them local.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

from ..contract.errors import DomainError
from .journal_running import AppendVerdict, JournalRecord, check_append_only, parse_records

INTEGRATION_REF = "origin/main"
UPDATE_TRAILER = "Journal-Update"
WORKER_TRAILER = "Journal-Worker"
MAX_RENAME_STEPS = 16
_UNIT = "\x1f"
_RECORD = "\x1e"
# Trailer values get their own separator, distinct from the row separator
# (pb-knowledge, slice 1 finding 3: two Journal-Update values lost both).
_VALUE = "\x1d"

GitRunner = Callable[..., "subprocess.CompletedProcess[str]"]


def _run_git(clone: Path, *args: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(["git", "-C", str(clone), *args], capture_output=True, text=True, timeout=120)


@dataclass(frozen=True)
class ContentVersion:
    commit: str
    committed_at: str
    path: str
    content_hash: str
    entry_ref: str
    intent: str          # "first" for the oldest version, else "update" or "unknown"
    worker: str          # stable worker name from the trailer, or "unknown"
    deleted: bool = False
    text: str = field(default="", repr=False, compare=False)

    def summary(self) -> dict[str, Any]:
        return {
            "commit": self.commit, "committed_at": self.committed_at, "path": self.path,
            "content_hash": self.content_hash, "entry_ref": self.entry_ref,
            "intent": self.intent, "worker": self.worker, "deleted": self.deleted,
        }


@dataclass(frozen=True)
class ContentChain:
    entry_ref: str
    versions: tuple[ContentVersion, ...]
    complete: bool
    reason: str = ""
    issues: tuple[dict[str, Any], ...] = ()

    def page(self, *, after: str = "", limit: int = 100) -> dict[str, Any]:
        """Oldest first, keyset by commit, so nothing disappears behind a display limit."""

        start = 0
        if after:
            commits = [version.commit for version in self.versions]
            if after not in commits:
                raise DomainError("journal_history_cursor_unknown",
                                  "The history cursor names no version of this entry.",
                                  status=409, details={"after": after})
            start = commits.index(after) + 1
        size = max(1, min(int(limit), 500))
        chunk = self.versions[start:start + size]
        more = start + size < len(self.versions)
        return {
            "entry_ref": self.entry_ref,
            "versions": [version.summary() for version in chunk],
            "next_cursor": chunk[-1].commit if (more and chunk) else "",
            "complete": self.complete,
            "reason": self.reason,
            "issues": list(self.issues),
        }


def _entry_ref(text: str) -> str:
    if not text.startswith("---\n"):
        return ""
    marker = text.find("\n---\n", 4)
    if marker < 0:
        return ""
    try:
        loaded = yaml.safe_load(text[4:marker]) or {}
    except yaml.YAMLError:
        return ""
    return str(loaded.get("entry_ref") or "") if isinstance(loaded, Mapping) else ""


def _show(git: GitRunner, clone: Path, commit: str, path: str) -> str | None:
    result = git(clone, "show", f"{commit}:{path}")
    return result.stdout if result.returncode == 0 else None


def _first_parent_commits(git: GitRunner, clone: Path, ref: str, path: str) -> list[tuple[str, str]]:
    result = git(clone, "log", "--first-parent", "--reverse", f"--format=%H{_UNIT}%cI", ref, "--", path)
    if result.returncode != 0:
        raise DomainError("journal_history_unavailable", "Git could not list the entry's history.",
                          status=503, details={"path": path, "stderr": result.stderr.strip()[:400]})
    rows = []
    for line in result.stdout.splitlines():
        commit, _, committed_at = line.partition(_UNIT)
        if commit:
            rows.append((commit, committed_at))
    return rows


def _renamed_from(git: GitRunner, clone: Path, commit: str, path: str) -> str:
    result = git(clone, "diff", "-M", "--name-status", "-z", f"{commit}^1", commit)
    if result.returncode != 0:
        return ""
    fields = result.stdout.split("\0")
    index = 0
    while index < len(fields):
        status = fields[index]
        if status.startswith("R") and index + 2 < len(fields):
            old, new = fields[index + 1], fields[index + 2]
            if new == path:
                return old
            index += 3
        elif status:
            index += 2
        else:
            index += 1
    return ""


def _trailers(git: GitRunner, clone: Path, commit: str, path: str, entry_ref: str) -> tuple[str, str]:
    """(intent, worker) from the commits this first-parent step introduced that touch ``path``."""

    parent = git(clone, "rev-parse", "--verify", "--quiet", f"{commit}^1")
    span = [f"{parent.stdout.strip()}..{commit}"] if parent.returncode == 0 else [commit]
    fmt = (f"--format=%H{_UNIT}%(trailers:key={UPDATE_TRAILER},valueonly,separator=%x1d)"
           f"{_UNIT}%(trailers:key={WORKER_TRAILER},valueonly,separator=%x1d){_RECORD}")
    result = git(clone, "log", fmt, *span, "--", path)
    if result.returncode != 0:
        return "unknown", "unknown"
    for row in result.stdout.split(_RECORD):
        parts = row.strip("\n").split(_UNIT)
        if len(parts) != 3:
            continue
        updates = [value.strip() for value in parts[1].split(_VALUE) if value.strip()]
        workers = [value.strip() for value in parts[2].split(_VALUE) if value.strip()]
        if entry_ref and entry_ref in updates and len(workers) == 1:
            return "update", workers[0]
    return "unknown", "unknown"


def published_chain(clone: Path | str, path: str, *, ref: str = INTEGRATION_REF,
                    git: GitRunner = _run_git) -> ContentChain:
    """The entry's published content versions, oldest first, with completeness stated."""

    clone = Path(clone)
    issues: list[dict[str, Any]] = []
    shallow = git(clone, "rev-parse", "--is-shallow-repository")
    if shallow.returncode != 0:
        raise DomainError("journal_history_unavailable", "The journal clone is not a readable Git repository.",
                          status=503, details={"clone": str(clone)})
    complete, reason = True, ""
    if shallow.stdout.strip() == "true":
        complete, reason = False, "shallow_clone"

    segments: list[tuple[str, list[tuple[str, str]]]] = []
    current_path = path
    rows = _first_parent_commits(git, clone, ref, current_path)
    if not rows:
        raise DomainError("journal_entry_not_published", "The entry has no version on the integration ref.",
                          status=404, details={"path": path, "ref": ref})
    newest_text = _show(git, clone, rows[-1][0], current_path) or ""
    entry_ref = _entry_ref(newest_text)
    segments.append((current_path, rows))
    for _ in range(MAX_RENAME_STEPS):
        oldest_commit = segments[0][1][0][0]
        if _show(git, clone, f"{oldest_commit}^1", segments[0][0]) is not None:
            break
        old_path = _renamed_from(git, clone, oldest_commit, segments[0][0])
        if not old_path:
            break
        old_text = _show(git, clone, f"{oldest_commit}^1", old_path) or ""
        if not entry_ref or _entry_ref(old_text) != entry_ref:
            issues.append({"code": "journal_history_rename_not_followed", "from": old_path,
                           "to": segments[0][0], "commit": oldest_commit})
            break
        older = [row for row in _first_parent_commits(git, clone, f"{oldest_commit}^1", old_path)]
        if not older:
            break
        segments.insert(0, (old_path, older))
    else:
        complete, reason = False, reason or "rename_chain_too_long"

    versions: list[ContentVersion] = []
    for segment_path, segment_rows in segments:
        for commit, committed_at in segment_rows:
            text = _show(git, clone, commit, segment_path)
            deleted = text is None
            if deleted:
                # Deleted only when the commit's tree has no entry for the path. An entry
                # whose blob cannot be read is a missing object, and the history is not
                # complete (pb-knowledge, slice 1 finding 2).
                listed = git(clone, "ls-tree", commit, "--", segment_path)
                if listed.returncode != 0 or listed.stdout.strip():
                    deleted = False  # present in the tree, unreadable: unknown, not deleted
                    complete, reason = False, reason or "object_missing"
                    issues.append({"code": "journal_history_object_missing", "commit": commit,
                                   "path": segment_path})
            version_ref = _entry_ref(text or "")
            if not deleted and version_ref != entry_ref:
                issues.append({"code": "journal_history_entry_ref_mismatch", "commit": commit,
                               "entry_ref": version_ref})
            if not versions:
                intent, worker = "first", "unknown"
            else:
                intent, worker = _trailers(git, clone, commit, segment_path, entry_ref)
            versions.append(ContentVersion(
                commit=commit, committed_at=committed_at, path=segment_path,
                content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest() if text is not None else "",
                entry_ref=version_ref, intent=intent, worker=worker, deleted=deleted, text=text or "",
            ))
    return ContentChain(entry_ref=entry_ref, versions=tuple(versions), complete=complete, reason=reason,
                        issues=tuple(issues))


@dataclass(frozen=True)
class RunningGenerations:
    """R2: admission against the last accepted generation of a running entry."""

    served: ContentVersion | None
    observed: ContentVersion | None
    invalid: tuple[dict[str, Any], ...]
    records: tuple[JournalRecord, ...]

    def summary(self) -> dict[str, Any]:
        return {
            "served_commit": self.served.commit if self.served else "",
            "observed_commit": self.observed.commit if self.observed else "",
            "invalid_versions": list(self.invalid),
        }


def running_generations(versions: Sequence[ContentVersion]) -> RunningGenerations:
    """Walk the published versions oldest first, keeping the last accepted one.

    A version is accepted when its records extend the accepted version's records
    (``check_append_only``). A version that does not is reported invalid and the
    accepted one stays served; a later version that extends the accepted one is
    accepted again, so a reviewed forward commit recovers without rewriting history.
    """

    accepted: ContentVersion | None = None
    accepted_records: tuple[JournalRecord, ...] = ()
    invalid: list[dict[str, Any]] = []
    observed: ContentVersion | None = None
    for version in versions:
        observed = version
        if version.deleted:
            invalid.append({"commit": version.commit, "reason": "journal_running_entry_deleted"})
            continue
        if not version.content_hash:
            invalid.append({"commit": version.commit, "reason": "journal_running_history_unavailable"})
            continue
        try:
            records = tuple(parse_records(version.text))
        except DomainError as exc:
            invalid.append({"commit": version.commit, "reason": exc.code})
            continue
        if accepted is None:
            accepted, accepted_records = version, records
            continue
        verdict: AppendVerdict = check_append_only(accepted_records, records)
        if verdict.accepted:
            accepted, accepted_records = version, records
        else:
            invalid.append({"commit": version.commit, "reason": verdict.reason, "record_id": verdict.record_id})
    return RunningGenerations(served=accepted, observed=observed, invalid=tuple(invalid),
                              records=accepted_records)


__all__ = [
    "ContentChain",
    "ContentVersion",
    "INTEGRATION_REF",
    "RunningGenerations",
    "UPDATE_TRAILER",
    "WORKER_TRAILER",
    "published_chain",
    "running_generations",
]

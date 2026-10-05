# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Worktrees never pile up: a safe sweep of one agent's workspace (W423).

Agents create a worktree per item, per repository and per review, as a side
effect of work, and nothing removed them: on 2026-09-30 one agent held 55
finished trees (about 11 GB) and the host disk filled. This module finds every
tree in an agent's workspace, says what state each is in, and removes only a
tree whose job ended and that is safe to lose.

A tree is removed only when all of these hold:

- its job ended: its registration recorded an end (a review decision, the
  agent's own ``--end``). Being merged is reported, and never ends a job: a
  merged tree can still serve a pending review or a release;
- nothing is lost: no uncommitted change, no untracked file (nested ones
  included), no ignored file its owner did not declare regenerable for this
  tree (``--generated <path> --generated-by <command>``; a folder name such as
  build or dist proves nothing), no commit that no remote has;
- nothing depends on it: its item is Done or Cancelled on the board (an item
  still open, or a state that cannot be read, keeps it), no consumer pinned it
  (``--pin``), no other tree's symlink points into it, and it is not a
  protected (runtime-mounted) path;
- the last dry run listed it with the same head, status and ignored files.

Everything else is kept and named with its reason. Removal is ``git worktree
remove`` without force, then ``git branch -d`` for a merged local branch, then
``git worktree prune``. A clone at the workspace root is never removed.

Ancestry never ends a job: a fresh tree cut from main is an ancestor of main,
and a merged tree may still be what a review or a release reads. A tree ends
only when its end is recorded: the review decision for a review tree, or
``pb worker workspace --end --path`` by its owner. Gitignored files (test
results, captures) can be the only copy of a finding, so they keep the tree
unless its owner declared that path regenerable with the command that makes it.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

GIT_TIMEOUT_SECONDS = 20
MAX_SYMLINK_SCAN_DEPTH = 8
TREE_FOLDERS = ("wt", "rv")


def _git(path: Path, *args: str) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(path), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return completed.returncode, (completed.stdout if completed.returncode == 0 else completed.stderr)


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _size_bytes(path: Path) -> int:
    total = 0
    for current, dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += (Path(current) / name).lstat().st_size
            except OSError:
                pass
    return total


# An end reason that suspends a checkout without ending its job: the item
# stays open and the tree is recreated from its pushed branch on resume.
SUSPENDED_PREFIX = "suspended:"

@dataclass
class Tree:
    path: Path
    clone: Path | None
    kind: str  # clone, implementation, review, unregistered, orphan
    branch: str = ""
    head: str = ""
    registration: dict[str, Any] | None = None
    dirty: list[str] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    pinned_by: list[str] = field(default_factory=list)
    consumers: list[str] = field(default_factory=list)
    unpushed: int = 0
    merged: bool = False
    ended: str = ""
    keep: list[str] = field(default_factory=list)
    size_bytes: int = 0

    @property
    def removable(self) -> bool:
        return self.kind not in {"clone", "orphan"} and bool(self.ended) and not self.keep

    @property
    def fingerprint(self) -> str:
        """What the dry run judged: --apply removes the tree only if this is unchanged."""

        parts = [self.head, self.ended, str(self.unpushed), *sorted(self.dirty), *sorted(self.untracked),
                 *sorted(self.ignored), *sorted(self.pinned_by), *sorted(self.consumers)]
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()

    def to_mapping(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "clone": str(self.clone) if self.clone else "",
            "kind": self.kind,
            "branch": self.branch,
            "head": self.head,
            "item": str((self.registration or {}).get("item") or ""),
            "ended": self.ended,
            "dirty": list(self.dirty),
            "untracked": list(self.untracked),
            "ignored": list(self.ignored),
            "pinned_by": list(self.pinned_by),
            "consumers": list(self.consumers),
            "merged": self.merged,
            "unpushed_commits": self.unpushed,
            "keep": list(self.keep),
            "action": "remove" if self.removable else "keep",
            "size_bytes": self.size_bytes,
        }


def _clones(workspace: Path) -> list[Path]:
    """Clones at the workspace root: a ``.git`` directory, not a worktree's ``.git`` file."""

    if not workspace.is_dir():
        return []
    return sorted(child for child in workspace.iterdir() if (child / ".git").is_dir())


def _worktrees(clone: Path) -> list[dict[str, str]]:
    code, out = _git(clone, "worktree", "list", "--porcelain")
    if code:
        return []
    trees: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in out.splitlines() + [""]:
        if not line:
            if current:
                trees.append(current)
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value or "true"
    return trees


def _default_branch(clone: Path) -> str:
    code, out = _git(clone, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if not code and out.strip():
        return out.strip()
    for candidate in ("origin/main", "origin/master"):
        if not _git(clone, "rev-parse", "--verify", "--quiet", candidate)[0]:
            return candidate
    return ""


def _symlink_targets(roots: Iterable[Path]) -> list[tuple[Path, Path]]:
    """Every symlink under the trees (bounded depth) and where it points."""

    links: list[tuple[Path, Path]] = []
    for root in roots:
        base_depth = len(root.parts)
        for current, dirs, files in os.walk(root, followlinks=False):
            depth = len(Path(current).parts) - base_depth
            if depth >= MAX_SYMLINK_SCAN_DEPTH:
                dirs[:] = []
            dirs[:] = [name for name in dirs if name != ".git"]
            for name in [*dirs, *files]:
                candidate = Path(current) / name
                if candidate.is_symlink():
                    try:
                        links.append((candidate, candidate.resolve()))
                    except OSError:
                        pass
            dirs[:] = [name for name in dirs if not (Path(current) / name).is_symlink()]
    return links


def inspect_workspace(
    workspace: Path | str,
    registrations: Sequence[Mapping[str, Any]] = (),
    *,
    protected: Sequence[Path | str] = (),
    measure: bool = True,
    pins: Mapping[str, Sequence[str]] | None = None,
    generated: Mapping[str, Mapping[str, str]] | None = None,
    consumers: Callable[[str], Sequence[str] | None] | None = None,
    only_ended: bool = False,
) -> list[Tree]:
    """Every tree in one agent's workspace, with its state and the decision.

    ``pins`` maps a tree's resolved path to the consumers that still need it;
    the key ``*`` pins every tree (an unreadable pin record). ``generated``
    maps a tree's resolved path to the ignored paths its owner declared
    regenerable. ``consumers`` answers, for an item key, what still needs that
    item's trees: an empty list when the item is Done or Cancelled, None when
    its state cannot be read (the tree is then kept).

    ``only_ended`` is for the automatic sweeps, which act only on trees whose
    job ended: a tree with no recorded end is listed without its git status
    and ignored-file scan. Those scans grow with every file in every tree, and
    the review-decision sweep runs before its receipt is printed (W563: one
    tree holding 100,000 ignored files cost 2.3 s).
    """

    root = Path(workspace).expanduser()
    registered = {
        str(Path(str(row.get("path") or "")).expanduser().resolve()): dict(row)
        for row in registrations
        if str(row.get("path") or "")
    }
    protected_roots = [Path(str(path)).expanduser() for path in protected if str(path)]
    trees: list[Tree] = []
    seen: set[str] = set()
    for clone in _clones(root):
        default = _default_branch(clone)
        trees.append(Tree(path=clone, clone=clone, kind="clone"))
        seen.add(str(clone.resolve()))
        for entry in _worktrees(clone):
            path = Path(entry.get("worktree", ""))
            if not entry.get("worktree") or path.resolve() == clone.resolve():
                continue
            key = str(path.resolve())
            seen.add(key)
            registration = registered.get(key)
            kind = str((registration or {}).get("kind") or "") or (
                "review" if entry.get("detached") else "unregistered"
            )
            if registration is not None and kind not in {"implementation", "review"}:
                kind = "implementation"
            branch = entry.get("branch", "").removeprefix("refs/heads/")
            full_head = entry.get("HEAD", "")
            tree = Tree(path=path, clone=clone, kind=kind, branch=branch, head=full_head[:12],
                        registration=registration)
            if not path.is_dir():
                tree.keep.append("missing on disk; git worktree prune clears it")
                trees.append(tree)
                continue
            if only_ended and not (registration and registration.get("ended_at")):
                tree.keep.append("job not ended (not inspected by an automatic sweep)")
                trees.append(tree)
                continue
            code, out = _git(path, "status", "--porcelain", "--untracked-files=no")
            tree.dirty = [line[3:] for line in out.splitlines() if line.strip()] if not code else ["<status unreadable>"]
            code, out = _git(path, "status", "--porcelain", "--untracked-files=all")
            tree.untracked = [line[3:] for line in out.splitlines() if line.startswith("?? ")] if not code else ["<status unreadable>"]
            code, out = _git(path, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory")
            if code:
                tree.ignored = ["<ignored files unreadable>"]
            else:
                declared = [rel.strip("/") for rel in ((generated or {}).get(key) or {})]
                tree.ignored = [
                    line for line in (entry.strip() for entry in out.splitlines())
                    if line and not any(line.rstrip("/") == rel or line.startswith(rel + "/") for rel in declared)
                ]
            tree.pinned_by = sorted({*((pins or {}).get(key) or ()), *((pins or {}).get("*") or ())})
            code, out = _git(path, "rev-list", "--count", "HEAD", "--not", "--remotes")
            tree.unpushed = int(out.strip()) if not code and out.strip().isdigit() else -1
            if default:
                tree.merged = _git(path, "merge-base", "--is-ancestor", "HEAD", default)[0] == 0
            # Only a recorded end finishes a job. Being merged is a fact the
            # report shows; a merged tree may still serve a review or a release.
            if registration and registration.get("ended_at"):
                tree.ended = str(registration.get("end_reason") or "ended")
            if tree.dirty:
                tree.keep.append(f"uncommitted changes: {', '.join(tree.dirty[:5])}")
            if tree.untracked:
                tree.keep.append(f"untracked files (evidence?): {', '.join(tree.untracked[:5])}")
            if tree.ignored:
                tree.keep.append(f"ignored files outside regenerable folders (evidence?): {', '.join(tree.ignored[:5])}")
            if tree.pinned_by:
                tree.keep.append(f"pinned by {', '.join(tree.pinned_by)}")
            if consumers is not None:
                item = str((registration or {}).get("item") or "")
                suspended = str((registration or {}).get("end_reason") or "").startswith(SUSPENDED_PREFIX)
                if item and not (registration and registration.get("ended_at") and suspended):
                    found = consumers(item)
                elif registration and registration.get("ended_at"):
                    # An end its owner recorded by path names no item, or names a
                    # suspended checkout of a still-open item (operator, 2026-10-01:
                    # an open PR is no reason to keep a pushed, clean tree). Either
                    # way there is no item to wait for; every other check above
                    # still applies, and the item and its assignment stay open.
                    found = []
                else:
                    found = None
                if found is None:
                    tree.consumers = [f"item {item or '(none recorded)'}: state unknown"]
                    tree.keep.append(
                        f"consumer state unknown (item {item or 'not recorded'}; offline or unreadable)"
                    )
                elif found:
                    tree.consumers = list(found)
                    tree.keep.append(f"still needed: {', '.join(found)}")
            if tree.unpushed:
                tree.keep.append("commits no remote has" if tree.unpushed > 0 else "push state unreadable")
            if any(_is_inside(path, guard) or _is_inside(guard, path) for guard in protected_roots):
                tree.keep.append("protected path (runtime or tool in use)")
            if not tree.ended and registration is None:
                tree.keep.append("unregistered: job end unknown; record it with pb worker workspace --end --path <tree>")
            elif not tree.ended:
                tree.keep.append(
                    "job not ended (no recorded end" + ("; merged, which alone never ends a job)" if tree.merged else ")")
                )
            trees.append(tree)
    # Directories under wt/ and rv/ that no clone knows are leftovers to name.
    for folder in TREE_FOLDERS:
        base = root / folder
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if child.is_dir() and str(child.resolve()) not in seen:
                trees.append(Tree(path=child, clone=None, kind="orphan",
                                  keep=["not a worktree of any clone here; inspect by hand"]))
    for key, registration in registered.items():
        if key not in seen:
            trees.append(Tree(path=Path(key), clone=None, kind="orphan", registration=registration,
                              keep=["registered but no clone lists it"]))
    # A tree another tree links into (a shared node_modules) stays.
    worktree_paths = [tree.path for tree in trees if tree.kind not in {"clone", "orphan"} and tree.path.is_dir()]
    links = _symlink_targets(worktree_paths)
    for tree in trees:
        if tree.kind in {"clone", "orphan"}:
            continue
        consumers = sorted({str(link) for link, target in links
                            # The link's own place is its parent: resolving the
                            # link itself would follow it into this tree.
                            if _is_inside(target, tree.path) and not _is_inside(link.parent, tree.path)})
        if consumers:
            tree.keep.append(f"linked from {consumers[0]}" + (f" and {len(consumers) - 1} more" if len(consumers) > 1 else ""))
        if measure and tree.path.is_dir():
            tree.size_bytes = _size_bytes(tree.path)
    return trees


def apply_sweep(
    trees: Sequence[Tree],
    *,
    forget: Callable[[Path], None] | None = None,
    planned: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Remove the removable trees, never with force; name what was kept and why.

    With ``planned`` (path -> fingerprint from the last dry run), a tree is
    removed only when the dry run listed it with the same fingerprint.
    """

    removed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    clones: set[Path] = set()
    for tree in trees:
        if tree.removable and planned is not None and planned.get(str(tree.path)) != tree.fingerprint:
            tree.keep.append("not listed with this state by the last dry run: run --sweep first")
        if not tree.removable or tree.clone is None:
            continue
        code, out = _git(tree.clone, "worktree", "remove", str(tree.path))
        if code:
            failed.append({"path": str(tree.path), "reason": out.strip()[:500]})
            continue
        entry = tree.to_mapping()
        if tree.branch and tree.merged:
            code, out = _git(tree.clone, "branch", "-d", tree.branch)
            entry["branch_deleted"] = code == 0
        clones.add(tree.clone)
        if forget is not None:
            forget(tree.path)
        removed.append(entry)
    for clone in sorted(clones):
        _git(clone, "worktree", "prune")
    return {
        "removed": removed,
        "failed": failed,
        "kept": [tree.to_mapping() for tree in trees if not tree.removable and tree.kind != "clone"],
        "freed_bytes": sum(int(entry.get("size_bytes") or 0) for entry in removed),
    }


def sweep_report(trees: Sequence[Tree]) -> dict[str, Any]:
    """The list view: every tree with its state and what --apply would do."""

    rows = [tree.to_mapping() for tree in trees]
    return {
        "trees": rows,
        "would_remove": [row["path"] for row in rows if row["action"] == "remove"],
        "total_bytes": sum(int(row.get("size_bytes") or 0) for row in rows),
    }


__all__ = ["Tree", "apply_sweep", "inspect_workspace", "sweep_report"]

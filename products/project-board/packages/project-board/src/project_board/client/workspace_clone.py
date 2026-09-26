"""How current a worker's own clone of a repository is (W343).

A worker reads its project's setup, facts, environment, instructions and
journal from its own clone, ``<workspace>/<alias>``. When that clone is
missing or behind its remote, ``pb worker context`` says so, with the folder
and the step of project-workspace.md that fixes it, and reads no other
checkout in its place.

Local refs only: nothing here fetches, so "behind" means behind what the clone
last fetched. Every git call takes list arguments, no shell, a timeout, and
never raises.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .read_roots import git, head_commit, ref_commit

# The remote's default branch, recorded by `git clone` and by the
# `git remote set-head origin --auto` of project-workspace.md step 2.
REMOTE_DEFAULT_REF = "origin/HEAD"
FALLBACK_REF = "origin/main"

CLONE_STEP = (
    "clone it into your workspace as project-workspace.md step 2 says, "
    "then run `pb worker workspace-report`"
)
FETCH_STEP = (
    "fetch and fast-forward it as project-workspace.md step 2 says "
    "(`git -C {clone} fetch --prune origin` then "
    "`git -C {clone} merge --ff-only {ref}`)"
)


def _compare_ref(clone: Path, branch: str) -> tuple[str, str]:
    """(ref, commit) the clone is compared with: the declared branch, else the remote default."""

    candidates = [f"origin/{branch}"] if branch else []
    candidates += [REMOTE_DEFAULT_REF, FALLBACK_REF]
    for ref in candidates:
        commit = ref_commit(clone, ref)
        if commit:
            return ref, commit
    return "", ""


def clone_state(alias: str, clone: Path | None, *, branch: str = "") -> dict[str, Any]:
    """The state of one workspace clone, with the action that fixes it.

    ``state`` is ``current``, ``ahead``, ``behind``, ``diverged``,
    ``no_upstream`` or ``missing``. ``action`` is empty when there is nothing
    to do.
    """

    path = str(clone) if clone is not None else ""
    base: dict[str, Any] = {"alias": alias, "path": path}
    if clone is None or not (clone / ".git").exists():
        return {
            **base,
            "state": "missing",
            "commit": "",
            "compared_with": "",
            "compared_commit": "",
            "behind": 0,
            "action": f"{alias} is not cloned at {path}: {CLONE_STEP}.",
        }
    head = head_commit(clone)
    ref, target = _compare_ref(clone, branch)
    result = {**base, "commit": head, "compared_with": ref, "compared_commit": target}
    if not target:
        return {
            **result,
            "state": "no_upstream",
            "behind": 0,
            "action": (
                f"{alias} at {path} has no remote branch to compare with: "
                + FETCH_STEP.format(clone=path, ref=f"origin/{branch or 'main'}")
                + "."
            ),
        }
    if head == target:
        return {**result, "state": "current", "behind": 0, "action": ""}
    code, count = git(clone, "rev-list", "--count", f"{head}..{target}")
    behind = int(count) if code == 0 and count.isdigit() else 0
    if behind == 0:
        return {**result, "state": "ahead", "behind": 0, "action": ""}
    code, _ = git(clone, "merge-base", "--is-ancestor", head, target)
    state = "behind" if code == 0 else "diverged"
    return {
        **result,
        "state": state,
        "behind": behind,
        "action": (
            f"{alias} at {path} is {behind} commits behind {ref} "
            f"(commit {target}) as last fetched: "
            + FETCH_STEP.format(clone=path, ref=ref)
            + ("." if state == "behind" else "; it has diverged, so never force it: tell the coordinator.")
        ),
    }


__all__ = ["clone_state"]

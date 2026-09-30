"""W423: finished worktrees go, and nothing that could be lost ever does.

2026-09-30: the host disk filled because agents create a worktree per item,
repository and review and nothing removed them (one agent held 55 finished
trees). The sweep removes a tree only when its job ended and it is clean,
fully pushed, unlinked and unprotected; every other tree is kept and named
with its reason. These are real git repositories: a bare origin, a clone at
the workspace root, and trees in every state.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from project_board.client import cli
from project_board.client.workspace_sweep import apply_sweep, inspect_workspace, sweep_report


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
                          env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test",
                               "HOME": str(cwd), "PATH": "/usr/bin:/bin"}).stdout.strip()


def commit(cwd: Path, name: str) -> str:
    (cwd / name).write_text(name, encoding="utf-8")
    git(cwd, "add", name)
    git(cwd, "commit", "-q", "-m", name)
    return git(cwd, "rev-parse", "HEAD")


@pytest.fixture
def workspace(tmp_path: Path) -> dict[str, Path]:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    ws = tmp_path / "workspace"
    ws.mkdir()
    clone = ws / "app"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
    git(clone, "checkout", "-q", "-b", "main")
    commit(clone, "base")
    git(clone, "push", "-q", "origin", "main")
    git(clone, "remote", "set-head", "origin", "main")

    def tree(name: str, branch: str) -> Path:
        path = ws / "wt" / name
        git(clone, "worktree", "add", "-q", "-b", branch, str(path), "origin/main")
        return path

    # A merged implementation branch: pushed, then merged into main on origin.
    merged = tree("w2-app", "work/w2")
    commit(merged, "w2")
    git(merged, "push", "-q", "origin", "work/w2")
    git(clone, "merge", "-q", "--ff-only", "work/w2")
    git(clone, "push", "-q", "origin", "main")
    git(clone, "fetch", "-q", "origin")
    # An open implementation branch, pushed, dirty.
    dirty = tree("w3-app", "work/w3")
    commit(dirty, "w3")
    git(dirty, "push", "-q", "origin", "work/w3")
    (dirty / "w3").write_text("changed", encoding="utf-8")
    # An ended implementation branch with a commit no remote has.
    unpushed = tree("w4-app", "work/w4")
    commit(unpushed, "w4")
    # A finished review tree: detached at a pushed, unmerged head.
    review_head = commit(tree("w6-src", "work/w6"), "w6")
    git(ws / "wt" / "w6-src", "push", "-q", "origin", "work/w6")
    review = ws / "rv" / "w6-app-abc"
    git(clone, "worktree", "add", "-q", "--detach", str(review), review_head)
    # A merged tree another tree links into (a shared node_modules).
    shared = tree("w7-app", "work/w7")
    (shared / "node_modules").mkdir()  # work/w7 is at origin/main: merged
    consumer = ws / "wt" / "w6-src"
    (consumer / "node_modules").symlink_to(shared / "node_modules")
    # An ended tree holding an untracked evidence file.
    evidence = tree("w8-app", "work/w8")
    (evidence / "review-notes.md").write_text("evidence", encoding="utf-8")
    # A leftover directory no clone knows.
    (ws / "wt" / "stray").mkdir()
    return {"ws": ws, "clone": clone, "merged": merged, "dirty": dirty, "unpushed": unpushed,
            "review": review, "shared": shared, "evidence": evidence}


def by_path(trees):
    return {str(tree.path): tree for tree in trees}


def test_only_ended_clean_pushed_unlinked_trees_are_removed(workspace):
    registrations = [
        {"path": str(workspace["review"]), "kind": "review", "item": "W6",
         "ended_at": "2026-09-30T19:00:00Z", "end_reason": "review decision recorded (review.accept)"},
        {"path": str(workspace["unpushed"]), "kind": "implementation", "item": "W4",
         "ended_at": "2026-09-30T19:00:00Z", "end_reason": "released"},
        {"path": str(workspace["dirty"]), "kind": "implementation", "item": "W3",
         "ended_at": "2026-09-30T19:00:00Z", "end_reason": "released"},
        {"path": str(workspace["evidence"]), "kind": "implementation", "item": "W8",
         "ended_at": "2026-09-30T19:00:00Z", "end_reason": "released"},
    ]
    trees = inspect_workspace(workspace["ws"], registrations)
    rows = by_path(trees)
    report = sweep_report(trees)

    # Would remove: the finished review tree and the merged branch tree.
    assert sorted(report["would_remove"]) == sorted([str(workspace["review"]), str(workspace["merged"])])
    # Kept, each with its reason.
    assert any("uncommitted changes" in reason for reason in rows[str(workspace["dirty"])].keep)
    assert any("commits no remote has" in reason for reason in rows[str(workspace["unpushed"])].keep)
    assert any("untracked files" in reason and "review-notes.md" in reason for reason in rows[str(workspace["evidence"])].keep)
    assert any(reason.startswith("linked from") for reason in rows[str(workspace["shared"])].keep)
    stray = rows[str(workspace["ws"] / "wt" / "stray")]
    assert stray.kind == "orphan" and stray.keep
    assert rows[str(workspace["clone"])].kind == "clone" and not rows[str(workspace["clone"])].removable

    forgotten = []
    result = apply_sweep(trees, forget=forgotten.append)
    assert sorted(entry["path"] for entry in result["removed"]) == sorted([str(workspace["review"]), str(workspace["merged"])])
    assert not workspace["review"].exists() and not workspace["merged"].exists()
    assert sorted(str(path) for path in forgotten) == sorted([str(workspace["review"]), str(workspace["merged"])])
    # The merged local branch is deleted with -d; nothing else moved.
    assert "work/w2" not in git(workspace["clone"], "branch", "--list", "work/w2")
    for kept in ("dirty", "unpushed", "shared", "evidence"):
        assert workspace[kept].exists(), kept
    assert (workspace["unpushed"] / "w4").exists()
    assert result["failed"] == []


def test_an_unregistered_leftover_is_found_and_a_job_that_did_not_end_is_kept(workspace):
    trees = inspect_workspace(workspace["ws"], [])
    rows = by_path(trees)
    # Found without a registration: the merged tree is removable, the dirty one is not.
    assert rows[str(workspace["merged"])].kind == "unregistered" and rows[str(workspace["merged"])].removable
    assert rows[str(workspace["dirty"])].kind == "unregistered"
    assert any("job not ended" in reason for reason in rows[str(workspace["dirty"])].keep)
    # An unregistered, unmerged review tree is kept: nothing says its job ended.
    assert rows[str(workspace["review"])].kind == "review"
    assert not rows[str(workspace["review"])].removable


def test_a_protected_path_is_never_removed(workspace):
    trees = inspect_workspace(workspace["ws"], [], protected=[workspace["merged"]])
    rows = by_path(trees)
    assert any("protected path" in reason for reason in rows[str(workspace["merged"])].keep)
    assert apply_sweep(trees)["removed"] == []


def test_a_fresh_registered_tree_is_not_finished_work(workspace):
    """Review return on #394: a tree just cut from origin/main is an ancestor of
    main; registered with its base head and no commit since, it is kept."""

    clone = workspace["clone"]
    fresh = workspace["ws"] / "wt" / "w900-app"
    git(clone, "worktree", "add", "-q", "-b", "work/w900", str(fresh), "origin/main")
    base = git(fresh, "rev-parse", "HEAD")
    registration = {"path": str(fresh), "kind": "implementation", "item": "W900", "base_head": base}
    rows = by_path(inspect_workspace(workspace["ws"], [registration]))
    assert not rows[str(fresh)].removable
    assert any("job not ended" in reason for reason in rows[str(fresh)].keep)
    # Without a base head, a registered tree ends only by a recorded end.
    rows = by_path(inspect_workspace(workspace["ws"], [{"path": str(fresh), "kind": "implementation"}]))
    assert not rows[str(fresh)].removable
    # Once it moved past its base and that head is merged, it is finished.
    commit(fresh, "w900")
    git(fresh, "push", "-q", "origin", "work/w900")
    git(clone, "merge", "-q", "--ff-only", "origin/work/w900")
    git(clone, "push", "-q", "origin", "main")
    git(clone, "fetch", "-q", "origin")
    rows = by_path(inspect_workspace(workspace["ws"], [registration]))
    assert rows[str(fresh)].removable and rows[str(fresh)].ended.startswith("merged into")


def test_automatic_apply_is_off_until_the_host_turns_it_on(monkeypatch):
    from types import SimpleNamespace

    calls = []

    def sweep(field, identity, args, *, apply, only_ended=False):
        calls.append(apply)
        return {"would_remove": ["/ws/wt/w2-app"], "removed": [], "kept": [], "failed": []}

    monkeypatch.setattr(cli, "_workspace_sweep", sweep)
    monkeypatch.setattr(cli, "_sweep_host", lambda _args: (Path("/ws"), SimpleNamespace(workspace_sweep_auto_apply=False)))
    report = cli._automatic_sweep(object(), object(), object(), "session_start")
    assert calls == [False]
    assert report["state"] == "report_only" and report["would_remove"] == ["/ws/wt/w2-app"]
    assert "pb host configure --workspace-sweep-auto-apply" in report["enable"]
    monkeypatch.setattr(cli, "_sweep_host", lambda _args: (Path("/ws"), SimpleNamespace(workspace_sweep_auto_apply=True)))
    cli._automatic_sweep(object(), object(), object(), "idle")
    assert calls == [False, True]


def test_the_host_config_carries_the_sweep_opt_in_and_protected_paths(tmp_path):
    from project_board.client.host_config import HostRelayConfig

    source = tmp_path / "config.json"
    parser = cli.build_parser()
    args = parser.parse_args(["host", "configure", "--workspace-sweep-auto-apply", "--workspace-sweep-protect", "/srv/runtime"])
    assert args.workspace_sweep_auto_apply is True and args.workspace_sweep_protect == ["/srv/runtime"]
    assert HostRelayConfig.__dataclass_fields__["workspace_sweep_auto_apply"].default is False
    del source


def test_the_automatic_sweep_never_fails_the_command_it_follows(monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("disk unreadable")

    monkeypatch.setattr(cli, "_workspace_sweep", broken)
    result = cli._automatic_sweep(object(), object(), object(), "idle")
    assert result["state"] == "failed" and "disk unreadable" in result["reason"]


def test_the_three_triggers_run_the_sweep_without_agent_memory():
    source = Path(cli.__file__).read_text(encoding="utf-8")
    assert '_automatic_sweep(field, identity, args, "session_start")' in source
    assert '_automatic_sweep(field, identity, args, "idle")' in source
    assert '_automatic_sweep(field, identity, args, "review_decision")' in source
    assert 'REVIEW_DECISION_ACTIONS = frozenset({"review.accept", "review.return", "review.cancel"})' in source


def test_the_procedure_owns_registration_the_sweep_and_its_triggers():
    """W423 acceptance 5: one owning rule, one skill pointer, the commands described."""

    procedures = Path(cli.__file__).resolve().parents[1] / "procedures" / "problem-board-worker"
    workspace = " ".join((procedures / "references" / "project-workspace.md").read_text(encoding="utf-8").split())
    assert "**Register every tree, and let the sweep remove what is finished (W423).**" in workspace
    assert "pb worker workspace --kind review --assignment-ref <reviewed item work_ref>" in workspace
    assert "`--apply` removes a tree only when its job ended **and** nothing could be lost" in workspace
    assert "at session start (`pb worker listen`), on `pb worker idle`, and after a review decision recorded through `pb coordinate`" in workspace
    assert "Until the operator turns it on for the host, these automatic runs only report what they would remove (`pb host configure --workspace-sweep-auto-apply`" in workspace
    assert "Gitignored files (build output, `node_modules`, ignored test results or screenshots) go with a removed tree" in workspace
    assert "The first real sweep on a host with an existing pile is the operator's decision" in workspace
    assert "Never `rm -rf` a worktree folder" in workspace
    skill = (procedures / "SKILL.md").read_text(encoding="utf-8")
    assert skill.count("a sweep removes finished, clean, fully pushed trees at session start, on idle and after a review decision") == 1

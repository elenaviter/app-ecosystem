"""Observed files in flight come from git, tracked paths only, bounded (W278 part B, P4a)."""

from __future__ import annotations

import subprocess

from project_board.client.worktree_files import (
    observation_signature,
    observe_worktree,
    worktree_root,
)


def _git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("a = 1\n")
    (repo / "README.md").write_text("read me\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    return repo, base


def test_committed_since_base_and_modified_now_are_reported_and_untracked_files_are_not(tmp_path):
    repo, base = _repo(tmp_path)
    (repo / "src" / "b.py").write_text("b = 2\n")
    _git(repo, "add", "src/b.py")
    _git(repo, "commit", "-q", "-m", "add b")
    (repo / "src" / "a.py").write_text("a = 2\n")            # modified, tracked
    (repo / "notes.txt").write_text("private\n")             # untracked, never published
    (repo / "src" / "c.py").write_text("c = 3\n")
    _git(repo, "add", "src/c.py")                            # staged, tracked
    seen = observe_worktree(repo, base_commit=base)
    assert seen["paths"] == ["src/a.py", "src/b.py", "src/c.py"]
    assert seen["path_count"] == 3 and seen["truncated"] == 0
    assert seen["head_commit"] and seen["error"] == ""
    assert "notes.txt" not in seen["paths"]
    assert worktree_root(repo / "src") == repo.resolve() or worktree_root(repo / "src") == repo


def test_the_set_is_bounded_and_a_bad_base_or_a_non_worktree_is_said_not_raised(tmp_path):
    repo, base = _repo(tmp_path)
    for index in range(12):
        (repo / f"f{index:02d}.txt").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "many")
    seen = observe_worktree(repo, base_commit=base, limit=5)
    assert len(seen["paths"]) == 5 and seen["path_count"] == 12 and seen["truncated"] == 7
    assert seen["paths"] == ["f00.txt", "f01.txt", "f02.txt", "f03.txt", "f04.txt"]
    bad_base = observe_worktree(repo, base_commit="deadbeef1")
    assert bad_base["error"] == "base_commit_unreachable"
    assert bad_base["paths"] == []                           # nothing modified now
    nowhere = observe_worktree(tmp_path / "missing")
    assert nowhere["error"] == "not_a_worktree" and nowhere["paths"] == []
    plain = tmp_path / "plain"
    plain.mkdir()
    assert worktree_root(plain) is None


def test_the_signature_ignores_the_clock_and_changes_with_the_set(tmp_path):
    repo, base = _repo(tmp_path)
    first = observe_worktree(repo, base_commit=base)
    again = {**first, "observed_at": "2030-01-01T00:00:00Z"}
    assert observation_signature(first) == observation_signature(again)
    (repo / "README.md").write_text("changed\n")
    changed = observe_worktree(repo, base_commit=base)
    assert changed["paths"] == ["README.md"]
    assert observation_signature(changed) != observation_signature(first)


def test_observe_assignments_reads_only_the_declared_worktrees_of_assignments_this_worker_holds(tmp_path):
    from project_board.client.worktree_files import observations_signature, observe_assignments

    repo, base = _repo(tmp_path)
    (repo / "src" / "a.py").write_text("a = 2\n")
    workspaces = [
        {"assignment_ref": "work:assignment:one", "repository_ref": "repo:ae/products", "path": str(repo)},
        {"assignment_ref": "work:assignment:gone", "repository_ref": "repo:ae/products", "path": str(repo)},
        {"assignment_ref": "work:assignment:theirs", "repository_ref": "repo:ae/products", "path": str(repo)},
    ]
    assignments = [
        {"assignment_ref": "work:assignment:one", "worker_name": "Codex-One", "state": "working",
         "sources": [{"repository_ref": "repo:ae/products", "base_commit": base, "branch": "work/x"}]},
        {"assignment_ref": "work:assignment:gone", "worker_name": "codex-one", "state": "completed", "sources": []},
        {"assignment_ref": "work:assignment:theirs", "worker_name": "someone-else", "state": "working", "sources": []},
    ]
    seen = observe_assignments(workspaces, assignments, worker_name="codex-one")
    assert [o["assignment_ref"] for o in seen] == ["work:assignment:one"]
    assert seen[0]["paths"] == ["src/a.py"] and seen[0]["repository_ref"] == "repo:ae/products"
    assert seen[0]["error"] == "" and seen[0]["head_commit"]
    first = observations_signature(seen)
    assert first == observations_signature([{**seen[0], "observed_at": "2030-01-01T00:00:00Z"}])
    # No declaration, no observation, and a different signature than any set.
    assert observe_assignments([], assignments, worker_name="codex-one") == []
    assert observations_signature([]) != first


def test_every_relay_git_call_refuses_optional_locks_and_unusual_names_survive_z_output(tmp_path, monkeypatch):
    import subprocess as sp

    from project_board.client import worktree_files

    repo, base = _repo(tmp_path)
    (repo / "with space.txt").write_text("s\n")
    (repo / 'quote"d.txt').write_text("q\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "odd names")
    _git(repo, "mv", "src/a.py", "src/renamed.py")
    (repo / "with space.txt").write_text("changed\n")
    calls = []
    real_run = sp.run

    def recording_run(argv, **kwargs):
        calls.append(list(argv))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(worktree_files.subprocess, "run", recording_run)
    seen = worktree_files.observe_worktree(repo, base_commit=base)
    assert calls and all(argv[:2] == ["git", "--no-optional-locks"] for argv in calls)
    assert any("-z" in argv for argv in calls if "status" in argv)
    assert any("-z" in argv for argv in calls if "diff" in argv)
    # The rename shows the path that exists now, not the original; odd names are intact.
    assert "src/renamed.py" in seen["paths"] and "src/a.py" not in seen["paths"]  # the original name is not in flight
    assert "with space.txt" in seen["paths"] and 'quote"d.txt' in seen["paths"]
    assert not any(path.startswith('"') for path in seen["paths"])


def test_the_observer_cache_runs_git_once_per_interval_per_worktree_unless_asked_fresh(tmp_path):
    from project_board.client.worktree_files import WorktreeObserverCache, observe_assignments

    repo, base = _repo(tmp_path)
    clock = {"now": 100.0}
    cache = WorktreeObserverCache(interval_seconds=30.0, clock=lambda: clock["now"])
    workspaces = [{"assignment_ref": "work:assignment:one", "repository_ref": "repo:ae/products", "path": str(repo)}]
    assignments = [{"assignment_ref": "work:assignment:one", "worker_name": "w", "state": "working", "sources": [{"repository_ref": "repo:ae/products", "base_commit": base}]}]
    first = observe_assignments(workspaces, assignments, worker_name="w", observe=cache)
    (repo / "README.md").write_text("changed\n")
    # Within the interval the cached observation is returned, git does not run.
    clock["now"] = 110.0
    second = observe_assignments(workspaces, assignments, worker_name="w", observe=cache)
    assert second[0]["paths"] == first[0]["paths"] == []
    # A forced read sees the change at once, and past the interval so does a plain read.
    forced = cache(str(repo), base_commit=base, fresh=True)
    assert forced["paths"] == ["README.md"]
    (repo / "src" / "a.py").write_text("a = 3\n")
    clock["now"] = 150.0
    later = observe_assignments(workspaces, assignments, worker_name="w", observe=cache)
    assert later[0]["paths"] == ["README.md", "src/a.py"]
    cache.forget(str(repo))
    assert cache(str(repo), base_commit=base)["paths"] == ["README.md", "src/a.py"]

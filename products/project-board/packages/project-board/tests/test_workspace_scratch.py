# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W423 ownership 5: managed scratch runs and publication-gated removal.

Nothing unique is lost: a run or a tree is removed only when its owner ended
the job, its content is verified published or regenerable, no consumer still
needs it, and the last dry run listed it unchanged.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from project_board.client import scratch, sweep_plan
from project_board.client.workspace_sweep import apply_sweep, inspect_workspace
from project_board.contract.errors import DomainError

OWNER = "claude-code-owner"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(cwd: Path, name: str, text: str = "x") -> str:
    (cwd / name).parent.mkdir(parents=True, exist_ok=True)
    (cwd / name).write_text(text, encoding="utf-8")
    git(cwd, "add", name)
    git(cwd, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", name)
    return git(cwd, "rev-parse", "HEAD")


@pytest.fixture()
def ws(tmp_path: Path) -> dict[str, Path]:
    """A workspace with one clone (alias ``app``) whose main holds a published finding."""

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    clone = workspace / "app"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
    git(clone, "checkout", "-q", "-b", "main")
    (clone / ".gitignore").write_text("test-results/\nnode_modules/\n", encoding="utf-8")
    git(clone, "add", ".gitignore")
    published = commit(clone, "docs/journal/w423-finding.md", "the finding")
    git(clone, "push", "-q", "origin", "main")
    git(clone, "remote", "set-head", "origin", "main")
    return {"ws": workspace, "clone": clone, "published": published}


def tree(ws: dict[str, Path], name: str) -> Path:
    path = ws["ws"] / "wt" / name
    git(ws["clone"], "worktree", "add", "-q", "-b", f"work/{name}", str(path), "origin/main")
    return path


def ended(path: Path, item: str = "W1") -> dict:
    return {"path": str(path), "kind": "implementation", "item": item,
            "ended_at": "2026-10-01T09:00:00Z", "end_reason": "change request merged"}


def rows(ws, registrations, **kwargs):
    return {str(t.path): t for t in inspect_workspace(ws["ws"], registrations, **kwargs)}


# Trees ---------------------------------------------------------------------


def test_ignored_content_keeps_the_tree_unless_its_owner_declared_it_regenerable(ws):
    capture = tree(ws, "w1-capture")
    (capture / "test-results").mkdir()
    (capture / "test-results" / "dialog.png").write_bytes(b"png")
    modules = tree(ws, "w2-modules")
    (modules / "node_modules" / "pkg").mkdir(parents=True)
    (modules / "node_modules" / "pkg" / "index.js").write_text("x", encoding="utf-8")
    (modules / ".gitignore").write_text("node_modules/\nbuild/\n", encoding="utf-8")
    git(modules, "add", ".gitignore")
    git(modules, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "ignore build")
    git(modules, "push", "-q", "origin", "work/w2-modules")
    (modules / "build").mkdir()
    (modules / "build" / "unique-capture.json").write_text("only copy", encoding="utf-8")

    # A folder name proves nothing: undeclared node_modules and build keep their trees.
    found = rows(ws, [ended(capture), ended(modules, "W2")])
    assert any("ignored files" in r and "test-results" in r for r in found[str(capture)].keep)
    assert any("ignored files" in r and "node_modules" in r for r in found[str(modules)].keep)

    # node_modules declared regenerable; the unique capture under build/ still keeps the tree.
    sweep_plan.declare_generated(ws["ws"], modules, "node_modules", "npm ci")
    generated = sweep_plan.read_generated(ws["ws"])
    found = rows(ws, [ended(modules, "W2")], generated=generated)
    keep = found[str(modules)].keep
    assert not any("node_modules" in r for r in keep) and any("build" in r for r in keep)

    # Once build/ is declared too, nothing unique is left: removable.
    sweep_plan.declare_generated(ws["ws"], modules, "build", "npm run build")
    found = rows(ws, [ended(modules, "W2")], generated=sweep_plan.read_generated(ws["ws"]))
    assert found[str(modules)].removable, found[str(modules)].keep
    with pytest.raises(DomainError):
        sweep_plan.declare_generated(ws["ws"], modules, "build", "")


def test_a_tree_whose_item_is_open_or_unknown_on_the_board_is_kept(ws):
    done = tree(ws, "w8-done")
    registration = ended(done, "W8")
    answers = {"open": ["item W8 is review"], "unknown": None, "terminal": []}
    for state, answer in answers.items():
        found = rows(ws, [registration], consumers=lambda _item, answer=answer: answer)[str(done)]
        if state == "terminal":
            assert found.removable, found.keep
        else:
            assert not found.removable, state
    found = rows(ws, [registration], consumers=lambda _item: None)[str(done)]
    assert any("consumer state unknown" in r for r in found.keep)
    # The item's state is part of what the dry run saw.
    open_print = rows(ws, [registration], consumers=lambda _item: ["item W8 is review"])[str(done)].fingerprint
    done_print = rows(ws, [registration], consumers=lambda _item: [])[str(done)].fingerprint
    assert open_print != done_print


def test_a_nested_untracked_file_keeps_the_tree_and_is_named(ws):
    nested = tree(ws, "w3-nested")
    (nested / "probe" / "deep").mkdir(parents=True)
    (nested / "probe" / "deep" / "result.txt").write_text("only copy", encoding="utf-8")
    found = rows(ws, [ended(nested, "W3")])
    assert not found[str(nested)].removable
    assert "probe/deep/result.txt" in found[str(nested)].untracked


def test_a_pinned_review_or_release_keeps_an_ended_tree(ws):
    review = tree(ws, "w4-review")
    pins = sweep_plan.set_pin(ws["ws"], review, "review:W4 reopened", pinned=True)
    sweep_plan.set_pin(ws["ws"], review, "release:2026.10.01", pinned=True)
    found = rows(ws, [ended(review, "W4")], pins=sweep_plan.read_pins(ws["ws"]))
    assert not found[str(review)].removable
    assert any("pinned by" in r and "release:2026.10.01" in r for r in found[str(review)].keep)
    sweep_plan.set_pin(ws["ws"], review, "review:W4 reopened", pinned=False)
    sweep_plan.set_pin(ws["ws"], review, "release:2026.10.01", pinned=False)
    assert rows(ws, [ended(review, "W4")], pins=sweep_plan.read_pins(ws["ws"]))[str(review)].removable
    assert pins  # the record existed while pinned


def test_an_unreadable_pin_record_keeps_every_tree(ws):
    done = tree(ws, "w5-done")
    (ws["ws"] / ".problem-board").mkdir(exist_ok=True)
    (ws["ws"] / ".problem-board" / "tree-pins.json").write_text("{not json", encoding="utf-8")
    found = rows(ws, [ended(done, "W5")], pins=sweep_plan.read_pins(ws["ws"]))
    assert not found[str(done)].removable


def test_merged_without_a_recorded_end_is_kept(ws):
    merged = tree(ws, "w6-merged")
    commit(merged, "w6.txt")
    git(merged, "push", "-q", "origin", "work/w6-merged")
    git(ws["clone"], "merge", "-q", "--ff-only", "origin/work/w6-merged")
    git(ws["clone"], "push", "-q", "origin", "main")
    git(ws["clone"], "fetch", "-q", "origin")
    found = rows(ws, [{"path": str(merged), "kind": "review", "item": "W6"}])
    assert found[str(merged)].merged and not found[str(merged)].removable


def test_apply_removes_a_tree_only_as_the_dry_run_saw_it(ws):
    done = tree(ws, "w7-done")
    first = rows(ws, [ended(done, "W7")])[str(done)]
    assert first.removable
    planned = {str(done): first.fingerprint}
    # Between the dry run and the apply, the owner commits and pushes again.
    commit(done, "late.txt")
    git(done, "push", "-q", "origin", "work/w7-done")
    trees = inspect_workspace(ws["ws"], [ended(done, "W7")])
    result = apply_sweep(trees, planned=planned)
    assert result["removed"] == [] and done.exists()
    assert any("not listed with this state by the last dry run" in r for row in result["kept"] for r in row["keep"])
    # A tree the dry run never listed is kept too.
    assert apply_sweep(inspect_workspace(ws["ws"], [ended(done, "W7")]), planned={})["removed"] == []
    # Listed unchanged: removed.
    again = inspect_workspace(ws["ws"], [ended(done, "W7")])
    planned = {str(t.path): t.fingerprint for t in again if t.removable}
    assert [row["path"] for row in apply_sweep(again, planned=planned)["removed"]] == [str(done)]


# Scratch runs ----------------------------------------------------------------


def make_run(ws, item="W423", purpose="probe the sweep") -> Path:
    return Path(scratch.new_run(ws["ws"], item=item, purpose=purpose, worker_name=OWNER)["run"])


def published_ref(ws) -> str:
    return f"repo:app/docs/journal/w423-finding.md@{ws['published']}"


def judge(ws, **kwargs):
    return {str(r.path): r for r in scratch.inspect_runs(ws["ws"], worker_name=OWNER, **kwargs)}


def test_a_run_has_an_atomic_manifest_inside_the_workspace(ws):
    run = make_run(ws)
    manifest = json.loads((run / ".run.json").read_text(encoding="utf-8"))
    assert run.parent.parent == ws["ws"] / "scratch" and run.parent.name == "W423"
    assert manifest["owner"] == OWNER and manifest["purpose"] == "probe the sweep" and manifest["closed"] is None
    assert run.name[:8].isdigit() and "T" in run.name  # timestamped run id
    with pytest.raises(DomainError):
        scratch.new_run(ws["ws"], item="../escape", purpose="x", worker_name=OWNER)
    with pytest.raises(DomainError):
        scratch.new_run(ws["ws"], item="W1", purpose="", worker_name=OWNER)
    with pytest.raises(DomainError):
        scratch.record(ws["ws"], ws["ws"] / "elsewhere", worker_name=OWNER, consumer="x")


def test_a_closed_published_run_is_removable_and_every_gap_keeps_it(ws):
    run = make_run(ws)
    (run / "report.md").write_text("the finding", encoding="utf-8")
    (run / "pytest.log").write_text("log", encoding="utf-8")
    scratch.record(ws["ws"], run, worker_name=OWNER, file="report.md", published=published_ref(ws))
    scratch.record(ws["ws"], run, worker_name=OWNER, file="pytest.log", generated_by="pytest -q tests/test_x.py")

    # Not closed: the job is not over, whatever its files say.
    assert any("not closed" in r for r in judge(ws)[str(run)].keep)
    # Closed with findings that cannot be verified on this host: kept.
    scratch.close(ws["ws"], run, worker_name=OWNER, reason="item done", findings="work:note:20261001T000000Z:note_x:y")
    assert any("not verifiable here" in r for r in judge(ws)[str(run)].keep)
    # Closed with verified findings: removable.
    scratch.close(ws["ws"], run, worker_name=OWNER, reason="item done", findings=published_ref(ws))
    assert judge(ws)[str(run)].removable, judge(ws)[str(run)].keep

    # Each gap keeps it, named.
    (run / "extra.txt").write_text("not recorded", encoding="utf-8")
    assert any("not in the manifest: extra.txt" in r for r in judge(ws)[str(run)].keep)
    (run / "extra.txt").unlink()
    (run / "report.md").write_text("edited after recording", encoding="utf-8")
    assert any("changed since it was recorded: report.md" in r for r in judge(ws)[str(run)].keep)
    (run / "report.md").write_text("the finding", encoding="utf-8")
    (run / "link").symlink_to(ws["clone"])
    assert any("link inside the run" in r for r in judge(ws)[str(run)].keep)
    (run / "link").unlink()
    scratch.record(ws["ws"], run, worker_name=OWNER, consumer="review:W423 pending")
    assert any("still used by review:W423 pending" in r for r in judge(ws)[str(run)].keep)
    scratch.record(ws["ws"], run, worker_name=OWNER, consumer_done="review:W423 pending")
    assert judge(ws)[str(run)].removable


def test_a_generated_flag_alone_never_makes_a_run_disposable(ws):
    run = make_run(ws)
    (run / "capture.log").write_text("diagnostic", encoding="utf-8")
    scratch.record(ws["ws"], run, worker_name=OWNER, file="capture.log", generated_by="rerun the probe")
    # Every file is generated, but the run's findings are not published: kept.
    assert not judge(ws)[str(run)].removable
    with pytest.raises(DomainError):
        scratch.close(ws["ws"], run, worker_name=OWNER, reason="done", findings="")


def test_unpublished_disproved_and_foreign_runs_are_kept(ws):
    run = make_run(ws)
    (run / "unique.md").write_text("only copy", encoding="utf-8")
    with pytest.raises(DomainError):
        scratch.record(ws["ws"], run, worker_name=OWNER, file="unique.md")  # no disposition
    scratch.record(ws["ws"], run, worker_name=OWNER, file="unique.md",
                   published=f"repo:app/docs/journal/missing.md@{ws['published']}")
    scratch.close(ws["ws"], run, worker_name=OWNER, reason="done", findings=published_ref(ws))
    assert any("publication disproved: unique.md" in r for r in judge(ws)[str(run)].keep)
    # Another agent's run, and a run without a manifest.
    other = Path(scratch.new_run(ws["ws"], item="W9", purpose="theirs", worker_name="someone-else")["run"])
    bare = ws["ws"] / "scratch" / "W9" / "no-manifest"
    bare.mkdir()
    found = judge(ws)
    assert any("owned by someone-else" in r for r in found[str(other)].keep)
    assert any("no readable manifest" in r for r in found[str(bare)].keep)


def test_apply_writes_the_receipt_first_and_keeps_a_run_changed_since_the_dry_run(ws):
    run = make_run(ws)
    (run / "report.md").write_text("the finding", encoding="utf-8")
    scratch.record(ws["ws"], run, worker_name=OWNER, file="report.md", published=published_ref(ws))
    scratch.close(ws["ws"], run, worker_name=OWNER, reason="item done", findings=published_ref(ws))

    # No dry run: nothing is removed.
    result = scratch.apply_runs(ws["ws"], worker_name=OWNER, planned={})
    assert result["removed"] == [] and run.exists()

    planned = {str(r.path): r.fingerprint for r in scratch.inspect_runs(ws["ws"], worker_name=OWNER) if r.removable}
    # The run's record changes between dry run and apply (a consumer came and
    # went): the dry run did not see this state, so it is kept.
    scratch.record(ws["ws"], run, worker_name=OWNER, consumer="review:W423")
    scratch.record(ws["ws"], run, worker_name=OWNER, consumer_done="review:W423")
    result = scratch.apply_runs(ws["ws"], worker_name=OWNER, planned=planned)
    assert result["removed"] == [] and run.exists()

    planned = {str(r.path): r.fingerprint for r in scratch.inspect_runs(ws["ws"], worker_name=OWNER) if r.removable}
    result = scratch.apply_runs(ws["ws"], worker_name=OWNER, planned=planned,
                                now=datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc))
    assert [row["path"] for row in result["removed"]] == [str(run)] and not run.exists()
    receipt = Path(result["removed"][0]["receipt"])
    stored = json.loads(receipt.read_text(encoding="utf-8"))
    assert stored["manifest"]["files"]["report.md"]["published"] == published_ref(ws)
    assert stored["run"] == str(run)


def test_loose_root_entries_are_named_and_never_removed(ws):
    (ws["ws"] / "w407-notes.md").write_text("loose", encoding="utf-8")
    (ws["ws"] / "logs").mkdir()
    loose = {Path(row["path"]).name: row for row in scratch.loose_entries(ws["ws"])}
    assert set(loose) == {"w407-notes.md", "logs"}
    assert all("pb worker scratch new" in row["keep"] for row in loose.values())
    assert "app" not in loose  # a clone is not loose


def test_a_run_whose_item_is_open_or_unknown_is_kept(ws):
    run = make_run(ws)
    (run / "report.md").write_text("the finding", encoding="utf-8")
    scratch.record(ws["ws"], run, worker_name=OWNER, file="report.md", published=published_ref(ws))
    scratch.close(ws["ws"], run, worker_name=OWNER, reason="item done", findings=published_ref(ws))
    assert judge(ws, consumers=lambda _item: [])[str(run)].removable
    assert any("still needed: item W423 is working" in r
               for r in judge(ws, consumers=lambda _item: ["item W423 is working"])[str(run)].keep)
    assert any("consumer state unknown" in r for r in judge(ws, consumers=lambda _item: None)[str(run)].keep)


def test_the_cli_reads_item_state_from_the_board_and_unknown_keeps(monkeypatch):
    from types import SimpleNamespace

    from project_board.client import cli

    answers = {"W1": {"object": {"status": "done"}}, "W2": {"object": {"status": "review"}},
               "W3": {"object": {}}}
    calls = []

    def board(request):
        key = json.loads(request.payload_json)["item_key"]
        calls.append(key)
        if key == "W4":
            raise RuntimeError("relay offline")
        return answers[key]

    monkeypatch.setattr(cli, "_attended_project_ref", lambda _field, _name: "work:project:p")
    monkeypatch.setattr(cli, "_coordinate_command", board)
    consumers = cli._sweep_item_consumers(object(), SimpleNamespace(worker_name="w"), SimpleNamespace())  # noqa: SLF001
    assert consumers("W1") == []
    assert consumers("W2") == ["item W2 is review"]
    assert consumers("W3") is None
    assert consumers("W4") is None
    assert consumers("") is None
    consumers("W1")
    assert calls.count("W1") == 1  # read once per sweep

    monkeypatch.setattr(cli, "_attended_project_ref", lambda _field, _name: (_ for _ in ()).throw(RuntimeError("none")))
    assert cli._sweep_item_consumers(object(), SimpleNamespace(worker_name="w"), SimpleNamespace())("W1") is None  # noqa: SLF001


def test_the_procedures_own_scratch_runs_and_where_knowledge_goes():
    from project_board.client import cli

    references = Path(cli.__file__).resolve().parents[1] / "procedures" / "problem-board-worker" / "references"
    workspace = " ".join((references / "project-workspace.md").read_text(encoding="utf-8").split())
    journaling = " ".join((references / "journaling.md").read_text(encoding="utf-8").split())
    assert "| `<workspace>/scratch/<item>/<run>` |" in workspace
    assert "**Work files live in a scratch run, and go only after their content is safe (W423).**" in workspace
    assert 'pb worker scratch --close --run <run> --reason "<why the job is over>" --findings' in workspace
    assert "Anything unknown, unreadable, changed or offline keeps the run, and age only flags it for review." in workspace
    assert "Files loose at the workspace root are listed by the sweep and never removed" in workspace
    assert "| How an app feature works now: its contract, nuances, rejected approaches and open gaps | The feature's owning doc" in journaling
    assert "| A unique finding from a scratch run" in journaling
    # One owning rule: journaling points at the workspace section, it does not restate it.
    assert journaling.count("scratch run") == 1


def test_the_cli_sweep_lists_then_applies_only_what_it_listed(ws, monkeypatch):
    from types import SimpleNamespace

    from project_board.client import cli

    finished = tree(ws, "w10-finished")
    still_open = tree(ws, "w11-open")
    run = make_run(ws, item="W10")
    (run / "report.md").write_text("the finding", encoding="utf-8")
    scratch.record(ws["ws"], run, worker_name=OWNER, file="report.md", published=published_ref(ws))
    scratch.close(ws["ws"], run, worker_name=OWNER, reason="item done", findings=published_ref(ws))

    class Field:
        def __init__(self):
            self.rows = [ended(finished, "W10"), ended(still_open, "W11")]

        def workspaces(self, _worker):
            return [row for row in self.rows if Path(row["path"]).exists()]

        def forget_workspace_path(self, _worker, path):
            self.rows = [row for row in self.rows if row["path"] != path]

    states = {"W10": [], "W11": ["item W11 is review"]}
    monkeypatch.setattr(cli, "_sweep_host", lambda _args: (ws["ws"], SimpleNamespace(workspace_sweep_protected=())))
    monkeypatch.setattr(cli, "_sweep_item_consumers", lambda *_a: (lambda item: states.get(item)))
    field = Field()
    identity = SimpleNamespace(worker_name=OWNER)
    args = SimpleNamespace(config=None)

    # Apply before any dry run: nothing goes.
    result = cli._workspace_sweep(field, identity, args, apply=True)  # noqa: SLF001
    assert result["removed"] == [] and result["scratch"]["removed"] == []
    assert finished.exists() and run.exists()
    # That apply recorded a plan, so the next apply removes what it listed; a dry run does the same.
    report = cli._workspace_sweep(field, identity, args, apply=False)  # noqa: SLF001
    assert str(finished) in report["would_remove"] and str(still_open) not in report["would_remove"]
    assert any(row["path"] == str(run) and row["action"] == "remove" for row in report["scratch_runs"])
    result = cli._workspace_sweep(field, identity, args, apply=True)  # noqa: SLF001
    assert [row["path"] for row in result["removed"]] == [str(finished)]
    assert [row["path"] for row in result["scratch"]["removed"]] == [str(run)]
    assert still_open.exists() and not finished.exists() and not run.exists()


def test_a_published_reference_must_hold_the_same_content(ws):
    """Ops review of #409: a reference proves a path, not the content. A file
    recorded as published at a commit whose blob differs is kept."""

    run = make_run(ws)
    (run / "report.md").write_text("a different, unique report", encoding="utf-8")
    scratch.record(ws["ws"], run, worker_name=OWNER, file="report.md", published=published_ref(ws))
    scratch.close(ws["ws"], run, worker_name=OWNER, reason="item done", findings=published_ref(ws))
    found = judge(ws)[str(run)]
    assert any("publication disproved: report.md" in r for r in found.keep)
    # The findings reference stays an existence check: it points at a summary, not a copy.
    assert not any("findings publication" in r for r in found.keep)


def test_an_owner_ended_tree_without_an_item_is_judged_on_everything_else(ws, monkeypatch):
    """Ops review of #409: `--end --path` records no item, and the board lookup
    then kept every such tree as unknown, so the existing pile could never go."""

    from types import SimpleNamespace

    from project_board.client import cli

    old = tree(ws, "w12-old")
    unended = tree(ws, "w13-unended")

    class Field:
        def __init__(self):
            self.rows = [{"path": str(old), "ended_at": "2026-10-01T10:00:00Z", "end_reason": "made before registration"},
                         {"path": str(unended), "kind": "implementation"}]

        def workspaces(self, _worker):
            return [row for row in self.rows if Path(row["path"]).exists()]

        def forget_workspace_path(self, _worker, path):
            self.rows = [row for row in self.rows if row["path"] != path]

    monkeypatch.setattr(cli, "_sweep_host", lambda _args: (ws["ws"], SimpleNamespace(workspace_sweep_protected=())))
    monkeypatch.setattr(cli, "_sweep_item_consumers", lambda *_a: (lambda item: None))  # board unreachable
    identity = SimpleNamespace(worker_name=OWNER)
    report = cli._workspace_sweep(Field(), identity, SimpleNamespace(config=None), apply=False)  # noqa: SLF001
    rows = {row["path"]: row for row in report["trees"]}
    assert rows[str(old)]["action"] == "remove", rows[str(old)]["keep"]
    assert rows[str(unended)]["action"] == "keep"
    # Every other check still applies to the item-less ended tree.
    (old / "probe.txt").write_text("only copy", encoding="utf-8")
    report = cli._workspace_sweep(Field(), identity, SimpleNamespace(config=None), apply=False)  # noqa: SLF001
    rows = {row["path"]: row for row in report["trees"]}
    assert rows[str(old)]["action"] == "keep" and any("untracked" in r for r in rows[str(old)]["keep"])


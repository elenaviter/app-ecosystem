# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W992: workspace and scratch brief output stays bounded at real worker cardinality.

2026-10-25: `pb worker workspace --list --format brief` printed 388
historical declarations, 55,030 tokens; one `workspace --end` printed them
all again although it ended one; `pb worker scratch --list --format brief`
printed 545 runs and 196 loose entries, 85,048 tokens. The brief form now
reports what a mutation changed and a bounded, newest-first summary of a
list, with narrow reads for the rest. The JSON keeps every record.

These run through the real command dispatch and the real field store at
400 declarations and 550 scratch runs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from project_board.client import cli, scratch
from project_board.client.render import render_envelope
from project_board.contract.errors import DomainError

DECLARATIONS = 400
ENDED = 150
RUNS = 550
LOOSE = 12
# A bounded brief stays well under what one tool result can carry; the
# reported cases were 55,030 and 85,048 tokens.
BRIEF_BYTES = 12_000


def brief(result) -> str:
    return render_envelope({"ok": True, "result": result})


@pytest.fixture()
def host(tmp_path: Path, monkeypatch):
    from project_board.client.store import SharedFieldStore
    from relay_helpers import make_host

    config, identity, _channel = make_host(tmp_path)
    field = SharedFieldStore(config.field_root)
    field.initialize(field_id="w992")
    field.register_worker(
        worker_name=identity.worker_name, worker_identity=identity.worker_identity,
        runtime_kind=identity.runtime_kind, runtime_session_id=identity.runtime_session_id,
        capabilities=[], authority_label="connection-hub:test-profile", control_plane_state="published",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(cli, "_identity", lambda _args: identity)
    monkeypatch.setattr(cli, "_sweep_host", lambda _args: (workspace, SimpleNamespace(workspace_sweep_protected=())))
    parser = cli.build_parser()

    def run(*argv: str):
        return cli._worker_command(parser.parse_args(["worker", *argv, "--config", str(config.path)]))  # noqa: SLF001

    return SimpleNamespace(field=field, identity=identity, workspace=workspace, run=run, tmp=tmp_path)


@pytest.fixture()
def declared(host):
    """400 declarations over 80 items, the oldest 150 ended."""

    trees = []
    for index in range(DECLARATIONS):
        tree = host.tmp / "trees" / f"t{index:03d}"
        tree.mkdir(parents=True)
        host.field.declare_workspace(
            host.identity.worker_name, assignment_ref=f"work:assignment:a{index:03d}",
            repository_ref="app-ecosystem", path=str(tree), item=f"W{index % 80 + 1}",
        )
        trees.append(tree.resolve())
    for tree in trees[:ENDED]:
        host.field.end_workspace(host.identity.worker_name, reason="change request merged", path=str(tree))
    return trees


def test_the_list_at_400_declarations_is_bounded_newest_first_and_the_json_keeps_all(host, declared):
    result = host.run("workspace", "--list")
    assert set(result) == {"worker", "workspaces"} and len(result["workspaces"]) == DECLARATIONS
    text = brief(result)
    assert len(text.encode()) < BRIEF_BYTES
    assert f"workspaces: {DECLARATIONS} declared · active {DECLARATIONS - ENDED} · ended {ENDED}" in text
    # The newest active trees, each path whole; the oldest active one is counted, not shown.
    assert f" · {declared[-1]}\n" in text and f" · {declared[ENDED]}\n" not in text
    assert f"active declarations: 20 of {DECLARATIONS - ENDED} shown in brief" in text
    assert f"newest ended: 5 of {ENDED}" in text
    assert "--item <Wn> or --path <tree> narrows the list" in text


def test_end_prints_what_it_ended_not_the_history(host, declared):
    target = declared[200]
    result = host.run("workspace", "--end", "--path", str(target), "--reason", "suspended: PR pushed")
    assert result["ended"] == 1 and len(result["workspaces"]) == DECLARATIONS  # JSON unchanged
    text = brief(result)
    assert len(text.encode()) < 2_000
    assert "ended: 1 declaration(s)" in text
    assert f" · {target}\n" in text and "suspended: PR pushed" in text
    assert sum(1 for line in text.splitlines() if line.startswith("--- ")) == 1
    assert f"workspaces: {DECLARATIONS} declared · active {DECLARATIONS - ENDED - 1} · ended {ENDED + 1}" in text


def test_declare_and_clear_print_the_changed_record_and_counts(host, declared):
    tree = host.tmp / "trees" / "fresh"
    tree.mkdir()
    result = host.run("workspace", "--assignment-ref", "work:assignment:fresh", "--repository", "applications",
                      "--path", str(tree), "--item", "W992")
    text = brief(result)
    assert len(result["workspaces"]) == DECLARATIONS + 1 and len(text.encode()) < 2_000
    assert "declared:\n  assignment_ref = work:assignment:fresh\n" in text
    assert f"  path = {tree.resolve()}\n" in text and "  item = W992\n" in text
    assert not any(line.startswith("--- ") for line in text.splitlines())
    cleared = brief(host.run("workspace", "--clear", "--assignment-ref", "work:assignment:fresh"))
    assert "cleared: 1 declaration(s) forgotten" in cleared and len(cleared.encode()) < 2_000
    assert f"workspaces: {DECLARATIONS} declared" in cleared


def test_item_and_path_narrow_the_list_and_detail_is_bounded(host, declared):
    one_item = host.run("workspace", "--list", "--item", "w7")
    assert one_item["filter"] == {"item": "w7"} and len(one_item["workspaces"]) == DECLARATIONS // 80
    assert all(row["item"] == "W7" for row in one_item["workspaces"])
    assert "filter item w7" in brief(one_item)
    one_tree = host.run("workspace", "--list", "--path", str(declared[3]))
    assert [row["path"] for row in one_tree["workspaces"]] == [str(declared[3])]
    detail = host.run("workspace", "--list", "--detail")
    text = brief(detail)
    assert sum(1 for line in text.splitlines() if line.startswith("--- ")) == 200
    assert f"declarations: 200 of {DECLARATIONS} shown in brief" in text


@pytest.fixture()
def runs(host):
    """550 runs over 50 items, one minute apart, and loose root entries."""

    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    paths = []
    for index in range(RUNS):
        made = scratch.new_run(host.workspace, item=f"W{index % 50 + 1}", purpose=f"probe {index}",
                               worker_name=host.identity.worker_name, now=start + timedelta(minutes=index))
        paths.append(Path(made["run"]))
    for index in range(LOOSE):
        (host.workspace / f"loose-{index}.log").write_text("x", encoding="utf-8")
    return paths


def test_the_scratch_list_at_550_runs_is_bounded_and_the_json_keeps_all(host, runs):
    result = host.run("scratch", "--list")
    assert set(result) == {"workspace", "runs", "loose"}
    assert len(result["runs"]) == RUNS and len(result["loose"]) == LOOSE
    text = brief(result)
    assert len(text.encode()) < BRIEF_BYTES
    assert f"runs {RUNS} · keep {RUNS} · remove 0 · loose {LOOSE}" in text
    assert f" · {runs[-1]} · keep:" in text and f" · {runs[0]} · " not in text
    assert f"scratch runs: 20 of {RUNS} shown in brief" in text
    detail = brief(host.run("scratch", "--list", "--detail"))
    assert sum(1 for line in detail.splitlines() if line.startswith("--- run")) == 200
    assert len(detail.encode()) < 100_000


def test_item_and_run_narrow_the_scratch_list(host, runs):
    one_item = host.run("scratch", "--list", "--item", "W3")
    assert len(one_item["runs"]) == RUNS // 50 and one_item["loose"] == []
    assert all(row["item"] == "W3" for row in one_item["runs"])
    target = runs[123]
    one_run = host.run("scratch", "--list", "--run", f"{target.parent.name}/{target.name}")
    assert [row["path"] for row in one_run["runs"]] == [str(target)]
    text = brief(one_run)
    assert f"run: {target}" in text and "  purpose = probe 123" in text
    with pytest.raises(DomainError) as refused:
        host.run("scratch", "--list", "--run", "W3/no-such-run")
    assert refused.value.code == "scratch_run_unknown"


def test_a_scratch_mutation_prints_its_run_only(host, runs):
    target = runs[42]
    (target / "report.md").write_text("finding", encoding="utf-8")
    result = host.run("scratch", "--record", "--run", str(target), "--file", "report.md",
                      "--generated-by", "make report")
    text = brief(result)
    assert f"run = {target}" in text and len(text.encode()) < 3_000
    assert str(runs[41]) not in text

# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W423: the scratch sweep reads only what its decision needs, and reports a bounded view.

2026-10-03: ``pb worker idle`` ran the automatic sweep for 79 s at 50% CPU and
623 MB RSS, and an explicit sweep's report was 350 MB. Every run was walked
twice and every recorded file hashed, even when the run's owner, closing or
consumers already kept it, and every file was listed in the report. The
automatic trigger then threw the detail away.

These tests count operations, not seconds: hashes, walks and report entries.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from project_board.client import cli, scratch

from test_workspace_scratch import OWNER, make_run, published_ref, ws  # noqa: F401 - fixture

MANY = 5000


def _hash_spy(monkeypatch) -> list[Path]:
    hashed: list[Path] = []
    original = scratch._sha256

    def spy(path: Path) -> str:
        hashed.append(path)
        return original(path)

    monkeypatch.setattr(scratch, "_sha256", spy)
    return hashed


def _walk_spy(monkeypatch) -> list[Path]:
    walked: list[Path] = []
    original = Path.rglob

    def spy(self: Path, pattern: str, *args: Any, **kwargs: Any):
        walked.append(self)
        return original(self, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "rglob", spy)
    return walked


def _many_files(folder: Path, count: int = MANY, prefix: str = "extra") -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (folder / f"{prefix}-{index:05d}.log").write_text("x", encoding="utf-8")


def _closed_published_run(ws, files: dict[str, str]) -> Path:
    run = make_run(ws)
    for name, text in files.items():
        (run / name).parent.mkdir(parents=True, exist_ok=True)
        (run / name).write_text(text, encoding="utf-8")
        if name.endswith(".md"):
            scratch.record(ws["ws"], run, worker_name=OWNER, file=name, published=published_ref(ws))
        else:
            scratch.record(ws["ws"], run, worker_name=OWNER, file=name, generated_by="rerun the probe")
    scratch.close(ws["ws"], run, worker_name=OWNER, reason="item done", findings=published_ref(ws))
    return run


def _judge_one(ws, run: Path, **kwargs) -> scratch.Run:
    found = {str(r.path): r for r in scratch.inspect_runs(ws["ws"], worker_name=OWNER, **kwargs)}
    return found[str(run)]


# A run that a run-level reason keeps is never read file by file ---------------


def test_a_run_kept_for_a_run_level_reason_is_never_walked_or_hashed(ws, monkeypatch):
    run = make_run(ws)  # open: not closed by its owner
    _many_files(run / "node_modules")
    (run / "report.md").write_text("the finding", encoding="utf-8")
    scratch.record(ws["ws"], run, worker_name=OWNER, file="report.md", published=published_ref(ws))
    hashed = _hash_spy(monkeypatch)
    walked = _walk_spy(monkeypatch)

    judged = _judge_one(ws, run)

    assert any("not closed" in reason for reason in judged.keep)
    assert hashed == []
    assert run not in walked, "the run's files were walked although its owner had not closed it"
    assert judged.files == [] and judged.files_count is None and judged.fingerprint == ""
    mapping = judged.to_mapping()
    assert mapping["files_count"] == "not counted" and mapping["size_bytes"] == "not measured"


def test_another_agents_run_is_not_read_either(ws, monkeypatch):
    other = Path(scratch.new_run(ws["ws"], item="W9", purpose="theirs", worker_name="someone-else")["run"])
    _many_files(other)
    hashed = _hash_spy(monkeypatch)
    walked = _walk_spy(monkeypatch)

    judged = _judge_one(ws, other)

    assert any("owned by someone-else" in reason for reason in judged.keep)
    assert hashed == [] and other not in walked


# A run that passes them stops hashing at the first file that keeps it ---------


def test_hashing_stops_at_the_first_file_that_keeps_the_run_and_the_count_stays_true(ws, monkeypatch):
    run = _closed_published_run(ws, {"a-report.md": "the finding", "b.log": "b", "c.log": "c"})
    (run / "a-report.md").write_text("edited after recording", encoding="utf-8")
    _many_files(run / "x")
    hashed = _hash_spy(monkeypatch)

    judged = _judge_one(ws, run)

    assert [path.name for path in hashed] == ["a-report.md"], "files after the first kept one were hashed"
    assert judged.files_count == 3 + MANY
    assert len(judged.files) == scratch.FILE_SAMPLE and judged.files[0] == "a-report.md"
    assert judged.keep[0] == "changed since it was recorded: a-report.md"
    detailed = [reason for reason in judged.keep if reason.startswith("not in the manifest: ")]
    assert len(detailed) == scratch.FILE_REASON_DETAIL - 1
    assert f"and {MANY - len(detailed)} more files: not in the manifest" in judged.keep
    assert "2 files after the first kept file were not hashed or verified" in judged.keep
    assert len(judged.keep) <= scratch.FILE_REASON_DETAIL + 2
    assert not judged.removable and judged.fingerprint == ""


def test_an_unrecorded_file_first_keeps_the_run_without_hashing_anything(ws, monkeypatch):
    run = _closed_published_run(ws, {"report.md": "the finding"})
    _many_files(run / "a-first")
    hashed = _hash_spy(monkeypatch)

    judged = _judge_one(ws, run)

    assert hashed == []
    assert judged.files_count == 1 + MANY
    assert "1 files after the first kept file were not hashed or verified" in judged.keep


# A removable run is read exactly as before ------------------------------------


def _prior_fingerprint(run: Path, verify, consumers=None) -> str:
    """The fingerprint as the sweep computed it before this change (AE 4a4432db)."""

    manifest = json.loads((run / scratch.MANIFEST_NAME).read_text(encoding="utf-8"))
    closed = manifest.get("closed") or {}
    semantic: list[str] = []
    if consumers is not None:
        found = consumers(str(manifest.get("item") or ""))
        semantic.append(f"item:{manifest.get('item')}:{'unknown' if found is None else ','.join(found)}")
    findings = str(closed.get("findings") or "")
    semantic.append(f"findings:{findings}:{verify(findings)}")
    recorded = manifest.get("files") or {}
    parts = [json.dumps(closed, sort_keys=True), *semantic,
             json.dumps(manifest.get("consumers") or {}, sort_keys=True)]
    for current in sorted(run.rglob("*")):
        relative = current.relative_to(run).as_posix()
        if relative == scratch.MANIFEST_NAME or relative.startswith(f"{scratch.MANIFEST_NAME}.lock"):
            continue
        if current.is_symlink() or current.is_dir() or relative not in recorded:
            continue
        digest = hashlib.sha256(current.read_bytes()).hexdigest()
        parts.append(f"{relative}:{digest}")
        published = str(recorded[relative].get("published") or "")
        if published:
            parts.append(f"{relative}:published:{published}:{verify(published, sha256=digest)}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def test_a_removable_run_keeps_the_fingerprint_the_plan_and_apply_rely_on(ws, monkeypatch):
    run = _closed_published_run(ws, {
        "report.md": "the finding",
        "logs/pytest.log": "log",
        "logs/nested/trace.log": "trace",
    })
    hashed = _hash_spy(monkeypatch)
    verify = scratch.repository_verifier(ws["ws"])

    judged = _judge_one(ws, run, verify=verify)

    assert judged.removable, judged.keep
    assert sorted(path.name for path in hashed) == ["pytest.log", "report.md", "trace.log"]
    assert judged.fingerprint == _prior_fingerprint(run, verify)
    assert judged.files_count == 3

    planned = {str(run): judged.fingerprint}
    on_disk = sum(path.stat().st_size for path in run.rglob("*") if path.is_file())
    result = scratch.apply_runs(ws["ws"], worker_name=OWNER, planned=planned, verify=verify)
    assert [row["path"] for row in result["removed"]] == [str(run)] and not run.exists()
    # A run about to go is measured for its receipt, manifest included.
    assert result["freed_bytes"] == on_disk


# Size is measured only when asked ----------------------------------------------


def test_size_is_measured_only_when_asked(ws):
    run = _closed_published_run(ws, {"report.md": "the finding"})

    assert _judge_one(ws, run).to_mapping()["size_bytes"] == "not measured"
    measured = _judge_one(ws, run, measure=True)
    assert measured.size_bytes == (run / "report.md").stat().st_size + (run / scratch.MANIFEST_NAME).stat().st_size


# The sweep: the automatic trigger carries no run detail, an explicit one a bounded view


def _sweep_world(ws, monkeypatch, *, measure: bool = False) -> SimpleNamespace:
    config = SimpleNamespace(
        workspace_sweep_auto_apply=False,
        workspace_sweep_protected=(),
        effective_agent_workspace_root="",
    )
    monkeypatch.setattr(cli, "_sweep_host", lambda _args: (ws["ws"], config))
    monkeypatch.setattr(cli, "_sweep_item_consumers", lambda _field, _identity, _args: (lambda _item: []))
    field = SimpleNamespace(workspaces=lambda _name: [], forget_workspace_path=lambda *_args: None)
    identity = SimpleNamespace(worker_name=OWNER)
    args = SimpleNamespace(measure=measure)
    return SimpleNamespace(field=field, identity=identity, args=args)


def test_the_automatic_sweep_reads_no_kept_run_and_carries_no_run_detail(ws, monkeypatch):
    kept = make_run(ws)  # open
    _many_files(kept)
    world = _sweep_world(ws, monkeypatch, measure=True)
    hashed = _hash_spy(monkeypatch)
    walked = _walk_spy(monkeypatch)

    result = cli._workspace_sweep(world.field, world.identity, world.args, apply=False, only_ended=True)
    report = cli._automatic_sweep(world.field, world.identity, world.args, "idle")

    assert "scratch_runs" not in result and "loose" not in result
    assert hashed == [] and kept not in walked
    assert report["state"] in {"report_only", "apply_refused"}
    assert len(json.dumps(report)) < 4096


def test_an_explicit_sweep_reports_a_bounded_view_of_a_large_run(ws, monkeypatch):
    run = _closed_published_run(ws, {"report.md": "the finding"})
    _many_files(run / "z")
    world = _sweep_world(ws, monkeypatch)

    result = cli._workspace_sweep(world.field, world.identity, world.args, apply=False)

    (row,) = [row for row in result["scratch_runs"] if row["path"] == str(run)]
    assert row["files_count"] == 1 + MANY
    assert len(row["files"]) == scratch.FILE_SAMPLE
    assert len(row["keep"]) <= scratch.FILE_REASON_DETAIL + 2
    assert row["size_bytes"] == "not measured"
    assert len(json.dumps(result)) < 32 * 1024, "the report grew with the number of files"


def test_an_explicit_sweep_measures_scratch_with_measure(ws, monkeypatch):
    run = _closed_published_run(ws, {"report.md": "the finding"})
    world = _sweep_world(ws, monkeypatch, measure=True)

    result = cli._workspace_sweep(world.field, world.identity, world.args, apply=False)

    (row,) = [row for row in result["scratch_runs"] if row["path"] == str(run)]
    assert isinstance(row["size_bytes"], int) and row["size_bytes"] > 0


def test_measure_is_an_explicit_sweep_option():
    args = cli.build_parser().parse_args(["worker", "workspace", "--sweep", "--measure"])
    assert args.sweep is True and args.measure is True

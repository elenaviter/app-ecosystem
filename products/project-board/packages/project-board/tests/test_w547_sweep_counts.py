"""W547: each sweep publishes its counts, and the coordinator sees them.

On 2026-10-04, 32 of 71 folders on one host were unregistered and nobody saw
it until they were cleaned by hand. Each sweep now writes counts only, never a
path or a name. The heartbeat carries them, and the coordinator's team view
shows them with the time they were observed. The routing view stays as it was.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from project_board.client import cli, relay, sweep_plan
from project_board.client.render import render_envelope
from project_board.client.workspace_size import WorkspaceSizes

from test_relay_disk_usage import _beat_adapter, _counting_walk
from test_workspace_sweep import workspace  # noqa: F401 - the real-git fixture


PROCEDURE = (
    Path(__file__).resolve().parents[1] / "src" / "project_board" / "procedures" / "problem-board-worker"
    / "references" / "coordinator" / "reconcile-the-work-not-the-inbox.md"
)


def _sweep(workspace, monkeypatch, *, only_ended):  # noqa: F811 - the fixture's value
    class Field:
        def workspaces(self, _worker):
            return [{"path": str(workspace["review"]), "kind": "review", "item": "W6",
                     "ended_at": "2026-09-30T19:00:00Z", "end_reason": "review decision recorded"}]

        def forget_workspace_path(self, _worker, _path):
            pass

    own = workspace["ws"]
    config = SimpleNamespace(effective_agent_workspace_root=str(own.parent), workspace_sweep_protected=())
    monkeypatch.setattr(cli, "_sweep_host", lambda _args: (own, config))
    monkeypatch.setattr(cli, "_sweep_own_folder", lambda _config, _args: str(own))
    monkeypatch.setattr(cli, "_sweep_item_consumers", lambda *_a: (lambda _item: []))
    identity = SimpleNamespace(worker_name="claude-code-w547")
    cli._workspace_sweep(Field(), identity, SimpleNamespace(config=None),  # noqa: SLF001
                         apply=False, only_ended=only_ended)
    return sweep_plan.summary_path(own)


def test_a_sweep_writes_counts_only_and_an_automatic_one_still_counts_unregistered(workspace, monkeypatch):  # noqa: F811
    for only_ended in (False, True):
        path = _sweep(workspace, monkeypatch, only_ended=only_ended)
        text = path.read_text(encoding="utf-8")
        summary = json.loads(text)
        assert summary["schema"] == sweep_plan.SUMMARY_SCHEMA and summary["observed_at"]
        assert set(summary) == {"schema", "observed_at", *sweep_plan.SUMMARY_COUNTS}
        # The fixture registers only the review tree: the other worktrees are
        # unregistered, and wt/stray is an orphan. An automatic sweep narrows
        # what it acts on to ended jobs, never what it counts.
        assert summary["unregistered"] == 6, only_ended
        assert summary["orphan"] == 1, only_ended
        assert summary["would_remove"] == 1, only_ended
        # Never a path, a branch or a file name: every value but the time is a count.
        assert all(type(summary[name]) is int for name in sweep_plan.SUMMARY_COUNTS)
        for leaked in (str(workspace["ws"]), "work/w", "stray", "review-notes"):
            assert leaked not in text, leaked


def test_no_summary_or_a_bad_one_reads_as_unknown_never_zero(tmp_path):
    assert sweep_plan.read_summary(tmp_path) is None
    path = sweep_plan.summary_path(tmp_path)
    path.parent.mkdir(parents=True)
    for bad in ("not json", json.dumps({"schema": "other"}),
                json.dumps({"schema": sweep_plan.SUMMARY_SCHEMA, "observed_at": "2026-10-06T00:00:00Z",
                            **{name: -1 for name in sweep_plan.SUMMARY_COUNTS}})):
        path.write_text(bad, encoding="utf-8")
        assert sweep_plan.read_summary(tmp_path) is None, bad
    sweep_plan.write_summary(tmp_path, {"unregistered": 3, "trees": 5})
    read = sweep_plan.read_summary(tmp_path)
    assert read["unregistered"] == 3 and read["trees"] == 5 and read["orphan"] == 0


def test_the_heartbeat_carries_the_counts_and_leaves_them_out_without_a_sweep(tmp_path):
    def beat():
        payload: dict = {}
        adapter = _beat_adapter(str(tmp_path), WorkspaceSizes(walk=_counting_walk([])))
        asyncio.run(relay.ProblemBoardHostRelayAdapter._add_disk_usage(adapter, payload))
        return payload["disk_usage"]

    assert "sweep" not in beat()
    sweep_plan.write_summary(tmp_path, {"unregistered": 2, "ended_but_kept": 1})
    carried = beat()["sweep"]
    assert carried["unregistered"] == 2 and carried["ended_but_kept"] == 1 and carried["observed_at"]


def _context(*, routing: bool, sweep: dict | None) -> str:
    usage = {"host_free_bytes": 40_000_000_000, "host_total_bytes": 400_000_000_000,
             "workspace_bytes": 12_500_000_000, "reported_at": "2026-10-06T00:30:00Z"}
    if sweep is not None:
        usage["sweep"] = sweep
    member = {"worker_alias": "agent-one@host-two", "worker_name": "codex-1", "role": "worker",
              "runtime_kind": "codex", "host_label": "host-two", "presence": "online", "disk_usage": usage}
    result = {"worker_name": "me", "project_ref": "work:project:p", "workspace": "/w/me",
              "repositories": [], "team": [member]}
    if routing:
        result["context_view"] = "routing"
    return render_envelope({"ok": True, "operation": "worker.context", "result": result})


def test_the_coordinator_sees_each_agents_disk_and_sweep_counts_with_their_times():
    text = _context(routing=False, sweep={"observed_at": "2026-10-06T00:10:00Z", "trees": 9, "unregistered": 3,
                                          "orphan": 1, "ended_but_kept": 2, "would_remove": 0, "scratch_kept": 4})
    [line] = [line for line in text.splitlines() if line.startswith("disk ")]
    assert line.startswith("disk agent-one@host-two: host free 10%")
    assert "workspace 12.5 GB" in line and "reported 10-06 00:30Z" in line
    assert "unregistered 3 · orphan 1 · ended but kept 2 · swept 10-06 00:10Z" in line
    # No sweep yet is unknown, never zero.
    unknown = _context(routing=False, sweep=None)
    assert "sweep not reported" in unknown and "unregistered 0" not in unknown


def test_the_routing_view_leaves_the_disk_line_out():
    text = _context(routing=True, sweep={"observed_at": "2026-10-06T00:10:00Z", "trees": 9, "unregistered": 3,
                                         "orphan": 1, "ended_but_kept": 2, "would_remove": 0, "scratch_kept": 4})
    assert not [line for line in text.splitlines() if line.startswith("disk ")]


def test_the_coordinator_procedure_asks_the_agent_and_never_sweeps_for_it():
    text = PROCEDURE.read_text(encoding="utf-8")
    section = text[text.index("**The team's disk (W547).**"):text.index("**What to do.**")]
    for needle in ("pb worker workspace --sweep", "pb worker workspace --end --path <tree>",
                   "disk_alert_free_percent", "unknown, never zero",
                   "Never sweep, end or delete another agent's trees yourself",
                   "--workspace-sweep-auto-apply"):
        assert needle in section, needle


def test_a_count_above_the_boards_bound_is_capped_not_sent_to_be_refused(tmp_path):
    """Review of 9e8115d6: the board drops a whole summary with any count above 10,000."""

    sweep_plan.write_summary(tmp_path, {"trees": 25_000, "unregistered": 10_001})
    read = sweep_plan.read_summary(tmp_path)
    assert read["trees"] == read["unregistered"] == sweep_plan.SUMMARY_COUNT_MAX == 10_000

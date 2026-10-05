"""Every teammate on the default read, and the current owner on every assignment row (W393).

2026-09-30: the default ``pb worker context`` showed the first eight of
thirteen teammates, so the routing facts of the other five (an info line
saying a worker had stopped, its usage) were one flag away. An
``assignment.list`` row printed only the implementation's worker, so after a
review was routed the finished implementer read as the current owner (W404).
"""

from __future__ import annotations

import json
from pathlib import Path

from project_board.client import cli
from project_board.client.render import render_envelope
from project_board.client.store import SharedFieldStore

PROJECT = "work:project:compact-team-" + "p" * 12
LONG_INFO = "Stopped for the weekly limit, mail and leases only; " * 20 + "INFO_TAIL"


def _member(index: int, **extra) -> dict:
    row = {
        "worker_alias": f"agent-{index}",
        "worker_name": f"codex-{index:02d}-" + "w" * 40,
        "role": "worker",
        "runtime_kind": "codex",
        "host_label": f"host-{index % 3}",
        "pool_status": "active",
        "presence": "online",
        "info_text": "",
        "runtime_model": {"model": "gpt-5.6", "effort": "high", "source": "codex-rollout", "observed_at": "2026-09-30T04:00:00Z"},
        "runtime_account": {"email": f"owner-{index % 2}@example.test", "source": "host-report", "observed_at": "2026-09-30T04:00:00Z", "state": "reported"},
        "limit_state": {
            "kind": "ok",
            "source": "codex-rollout",
            "observed_at": "2026-09-30T04:00:00Z",
            "windows": [
                {"name": "five_hour", "used_percent": 10 + index, "resets_at": "2099-01-01T05:00:00Z"},
                {"name": "seven_day", "used_percent": 40 + index, "resets_at": "2099-01-07T00:00:00Z"},
            ],
        },
    }
    row.update(extra)
    return row


def _team() -> list[dict]:
    team = [_member(index) for index in range(13)]
    team[1] = _member(1, info_text=LONG_INFO, info_set_at="2026-09-28T14:52:00Z")
    team[2] = _member(2, presence="stale", wake_hold={"since": "2026-09-30T03:00:00Z", "until": "2026-10-01T07:00:00Z", "pending": 4})
    team[3] = _member(3, wake_recovery={"wake_id": "wake_" + "3" * 32, "state": "submitted", "requested_at": "2026-09-30T02:20:54Z"})
    team[4] = _member(4, limit_state={
        "kind": "ok", "source": "codex-rollout", "observed_at": "2026-09-29T10:00:00Z",
        "windows": [{"name": "five_hour", "used_percent": 97, "resets_at": "2026-09-29T12:40:00Z"}],
    })
    return team


def _context(team: list[dict], **extra) -> dict:
    return {
        "ok": True,
        "result": {"project_ref": PROJECT, "workspace": "/workspace/agent", "repositories": [], "team": team, **extra},
    }


def _lines(envelope: dict) -> list[str]:
    return render_envelope(envelope).splitlines()


def _member_view(team: list[dict], member: str) -> list[str]:
    return _lines({"ok": True, "result": cli.context_for_member(_context(team)["result"], member)})


def test_every_teammate_has_a_scheduling_row_and_a_usage_line():
    # W563 (Q8 compact team row): one default row per teammate carries its usage
    # figures; runtime provenance, the provider account and the `team usage:`
    # block moved to the `--member` view.
    team = _team()
    text = render_envelope(_context(team))
    lines = text.splitlines()
    assert "team: 13 · shown 13" in lines
    assert "team members:" not in text, "no silent first-eight cut"
    assert "team usage:" not in lines, "the default view no longer repeats usage in a second block"
    rows = [line for line in lines if line.startswith("--- agent-")]
    assert len(rows) == 13
    for index, member in enumerate(team):
        [row] = [line for line in rows if line.startswith(f"--- agent-{index} ({member['worker_name']}) · ")]
        assert " · model gpt-5.6/high" in row, index
        if index != 4:
            assert f"usage week {40 + index}% resets 01-07 00:00Z · 5 h {10 + index}% resets 01-01 05:00Z" in row, index
        # Q8: a shared-account figure is never presented as one agent's usage.
        assert f"account shared by {7 if index % 2 == 0 else 6}" in row, index
    assert not any(line.startswith("  runtime:") or line.startswith("  provider account:") for line in lines)
    assert (
        f"team detail (provider account, provenance): pb worker context --project-ref {PROJECT} --member <name>"
        in lines
    )
    member = _member_view(team, "agent-1")
    assert any("runtime: model gpt-5.6 · reasoning effort high" in line for line in member)
    assert any("email owner-1@example.test" in line and "state reported" in line for line in member)
    usage = member[member.index("team usage:") + 1:]
    assert sum(team[1]["worker_name"] in line for line in usage) == 1
    # Budget unchanged; the one-row format measures 23 lines / ~3.9 KB here.
    assert len(lines) <= 40 + 8 * 13 and len(text.encode()) <= 4_000 + 1_300 * 13, (len(lines), len(text.encode()))


def test_a_long_info_line_is_cut_visibly_and_member_shows_it_whole():
    # W563 (Q8 compact team row): the default row cuts info at 160 B and names
    # `--member <name>`; the complete command is on the team detail line.
    team = _team()
    name = team[1]["worker_name"]
    lines = _lines(_context(team))
    [row] = [line for line in lines if line.startswith("--- agent-1 (")]
    info = row.split(" · info: ", 1)[1]
    assert info.startswith("Stopped for the weekly limit")
    size = len(LONG_INFO.encode())
    assert "INFO_TAIL" not in info
    assert info.endswith(f" (cut from {size} B: --member {name})")
    # PR372 review: the printed command is complete. The real parser accepts
    # it with this project and this member, never a usage error.
    import shlex

    [detail] = [line for line in lines if line.startswith("team detail (provider account, provenance): ")]
    command = shlex.split(detail.split(": ", 1)[1].replace("<name>", info.rsplit("--member ", 1)[1].rstrip(")")))
    assert command[:3] == ["pb", "worker", "context"]
    parsed = cli.build_parser().parse_args(command[1:])
    assert parsed.project_ref == PROJECT and parsed.member == name

    narrowed = cli.context_for_member(_context(team)["result"], "AGENT-1")
    member_lines = _lines({"ok": True, "result": narrowed})
    assert "team: 1 of 13 match --member agent-1" in member_lines
    assert any("INFO_TAIL" in line for line in member_lines), "the member read shows the info line whole"
    assert any(line.startswith("  info: Stopped") and line.endswith("· set 2026-09-28T14:52:00Z") for line in member_lines)
    assert not any("agent-2 (" in line for line in member_lines)


def test_a_member_filter_that_matches_nobody_says_so():
    narrowed = cli.context_for_member(_context(_team())["result"], "nobody")
    assert narrowed["team"] == []
    assert "team: 0 of 13 match --member nobody" in _lines({"ok": True, "result": narrowed})


def test_stale_presence_a_held_wake_and_a_recovery_are_on_the_default_row():
    # W563 (Q8 compact team row): presence is a bare field on the one-line row.
    lines = _lines(_context(_team()))
    text = "\n".join(lines)
    assert any(line.startswith("--- agent-2 (") and " · codex · host-2 · stale · " in line for line in lines)
    assert "  wake held since 2026-09-30T03:00:00Z until 2026-10-01T07:00:00Z · pending 4" in lines
    assert f"  wake recovery submitted for wake_{'3' * 32} · since 2026-09-30T02:20:54Z" in lines
    assert text.count("  wake ") == 2, "only members whose board reports a wake"
    # Each wake line sits directly under its member's row.
    assert lines[lines.index("  wake held since 2026-09-30T03:00:00Z until 2026-10-01T07:00:00Z · pending 4") - 1].startswith("--- agent-2 (")
    assert lines[lines.index(f"  wake recovery submitted for wake_{'3' * 32} · since 2026-09-30T02:20:54Z") - 1].startswith("--- agent-3 (")


def test_a_passed_reset_is_not_read_as_capacity_now():
    # W563 (Q8 compact team row): the passed-reset marker is on the default row;
    # the long-form note stays in the member view's `team usage:` block.
    rows = [line for line in _lines(_context(_team())) if line.startswith("--- agent-")]
    [passed] = [line for line in rows if line.startswith("--- agent-4 ")]
    assert "usage 5 h 97% resets 09-29 12:40Z" in passed
    assert "5 h reset passed, current use unknown" in passed
    assert sum("reset passed" in line for line in rows) == 1, "future resets carry no note"
    member = _member_view(_team(), "agent-4")
    usage = [line for line in member[member.index("team usage:") + 1:] if line.startswith("  agent-")]
    [detail] = [line for line in usage if line.startswith("  agent-4 ")]
    assert detail.endswith("reset passed for 5 h: its figure is from before the reset, current use not reported")


def _assignment_page(**row) -> dict:
    base = {
        "item_key": "W94",
        "state": "completed",
        "item_status": "review",
        "ownership_version": 9,
        "assignment_ref": "work:assignment:20260929T181443Z:assignment_" + "a" * 32 + ":w94-contract",
        "identity_ref": "work:plan:node:20260929T181058Z:w94:" + "contract-" * 6,
        "worker_name": "claude-code-00000000-0000-4000-8000-000000000001",
        "assigned_at": "2026-09-29T18:14:43Z",
        "updated_at": "2026-09-30T04:39:11Z",
    }
    base.update(row)
    return {"ok": True, "result": {"operation": "assignment.list", "object": {"items": [base], "matched_count": 1, "item_count": 1}}}


def test_an_assignment_row_names_the_current_owner_apart_from_the_implementer():
    reviewer = "codex-00000000-0000-4000-8000-000000000002"
    lines = _lines(_assignment_page(item_assignee=reviewer, item_reviewer=reviewer))
    assert f"current owner: {reviewer} · reviewer {reviewer}" in lines
    assert "implementation: claude-code-00000000-0000-4000-8000-000000000001 · assigned 2026-09-29T18:14:43Z · updated 2026-09-30T04:39:11Z" in lines
    assert (
        "note: claude-code-00000000-0000-4000-8000-000000000001 held the implementation. "
        f"The item's current owner is {reviewer}."
    ) in lines
    page = _assignment_page()["result"]["object"]["items"][0]
    assert f"assignment_ref: {page['assignment_ref']}" in lines
    assert f"identity_ref: {page['identity_ref']}" in lines


def test_an_empty_owner_and_an_older_board_are_told_apart():
    empty = _lines(_assignment_page(item_assignee="", item_reviewer=""))
    assert "current owner: none · reviewer none" in empty
    older = _lines(_assignment_page())
    assert "current owner: not reported by this board" in older
    assert not any(line.startswith("current owner: claude-code") for line in older), "never filled in with the implementer"


def test_the_team_snapshot_keeps_reported_wake_fields_bounded(tmp_path: Path):
    field = SharedFieldStore(tmp_path / "field")
    field.initialize(field_id="team-snapshot")
    field.create_project(project_id="compact-team", title="Compact team", goal="Route from complete rows.", owner="operator")
    team = _team()
    team[2]["wake_hold"]["pending"] = "not-a-number"
    team[3]["wake_recovery"]["unexpected"] = "dropped"
    field.sync_project_team("compact-team", team)
    rows = {row["worker_alias"]: row for row in field.read_project_team("compact-team")}
    assert rows["agent-2"]["wake_hold"] == {"since": "2026-09-30T03:00:00Z", "until": "2026-10-01T07:00:00Z"}
    assert rows["agent-3"]["wake_recovery"] == {
        "wake_id": "wake_" + "3" * 32, "state": "submitted", "requested_at": "2026-09-30T02:20:54Z",
    }
    assert rows["agent-1"]["info_set_at"] == "2026-09-28T14:52:00Z"
    assert rows["agent-0"]["wake_hold"] == {} and rows["agent-0"]["wake_recovery"] == {}


def test_pb_render_prints_the_same_team_and_owner_reads(tmp_path: Path, capsys):
    for envelope in (_context(_team()), _assignment_page(item_assignee="", item_reviewer="")):
        saved = tmp_path / "saved.json"
        saved.write_text(json.dumps(envelope), encoding="utf-8")
        assert cli.main(["render", "--file", str(saved)]) == 0
        assert capsys.readouterr().out == render_envelope(envelope)

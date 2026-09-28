"""The team roster keeps each teammate's usage limit (W26 line 7).

The board sends every linked agent's `limit_state` on the heartbeat's team
roster, so an agent reads a teammate's limit without asking it: an agent out
of tokens cannot answer mail. The host keeps it in team.json, which
`pb worker context` prints.
"""

from __future__ import annotations

from project_board.client.store import SharedFieldStore

PROJECT = "demo-project"


def test_team_json_keeps_a_teammates_limit_and_leaves_an_unreported_one_empty(tmp_path):
    store = SharedFieldStore(tmp_path / "field")
    store.initialize(field_id="team-limits")
    store.create_project(project_id=PROJECT, title="Quickstart works", goal="Onboard agents.", owner="operator")
    store.sync_project_team(
        PROJECT,
        [
            {
                "worker_name": "Claude-Code-Main", "role": "coordinator",
                "limit_state": {
                    "kind": "out_of_tokens", "reached": "primary", "resets_at": "2026-09-25T06:00:00Z",
                    "observed_at": "2026-09-25T01:00:00Z", "source": "claude-code-statusline",
                    "windows": [
                        {"name": "primary", "used_percent": 100.0, "window_minutes": 300, "resets_at": "2026-09-25T06:00:00Z", "extra": "x"},
                        {"name": "secondary", "used_percent": "65", "window_minutes": 10080},
                        {"name": "no-figure"},
                        {"name": "nonsense", "used_percent": "a lot"},
                    ],
                },
            },
            {"worker_name": "codex-main", "role": "worker"},
            {"worker_name": "codex-ui", "role": "worker", "limit_state": {"kind": ""}},
        ],
    )
    members = {row["worker_name"]: row for row in store.read_project_team(PROJECT)}
    assert members["claude-code-main"]["limit_state"] == {
        "kind": "out_of_tokens",
        "reached": "primary",
        "resets_at": "2026-09-25T06:00:00Z",
        "observed_at": "2026-09-25T01:00:00Z",
        "source": "claude-code-statusline",
        # W351: the windows are kept, bounded; a window without a figure is dropped.
        "windows": [
            {"name": "primary", "used_percent": 100.0, "window_minutes": 300, "resets_at": "2026-09-25T06:00:00Z"},
            {"name": "secondary", "used_percent": 65.0, "window_minutes": 10080},
        ],
    }
    assert members["codex-main"]["limit_state"] == {}
    assert members["codex-ui"]["limit_state"] == {}


def test_at_most_eight_windows_are_kept(tmp_path):
    store = SharedFieldStore(tmp_path / "field")
    store.initialize(field_id="team-limits")
    store.create_project(project_id=PROJECT, title="Quickstart works", goal="Onboard agents.", owner="operator")
    many = [{"name": f"w{i}", "used_percent": i} for i in range(20)]
    store.sync_project_team(PROJECT, [{"worker_name": "a", "role": "worker", "limit_state": {"kind": "ok", "windows": many}}])
    assert len(store.read_project_team(PROJECT)[0]["limit_state"]["windows"]) == 8


def test_context_shows_each_teammates_usage_windows_end_to_end(tmp_path, monkeypatch):
    """W351 line 1: what the board sends reaches `pb worker context --format brief` as figures."""

    from project_board.client import cli
    from project_board.client.render import render_envelope
    from test_workspace_report import _cli, _host_with_workspace
    from test_attendance_materializes_project import PROJECT_REF

    host, identity, field, config, workspace = _host_with_workspace(tmp_path, monkeypatch)
    project_id = PROJECT_REF.split(":")[-1]
    field.create_project(project_id=project_id, title="Quickstart works", goal="Onboard agents.", owner="operator")
    field.sync_project_team(
        project_id,
        [
            {
                "worker_name": "claude-code-teammate", "worker_alias": "claude-app@host", "role": "worker",
                "limit_state": {
                    "kind": "ok", "observed_at": "2026-09-28T00:00:00Z",
                    "windows": [
                        {"name": "primary", "used_percent": 23.4, "window_minutes": 300, "resets_at": "2026-09-28T03:00:00Z"},
                        {"name": "secondary", "used_percent": 65, "window_minutes": 10080, "resets_at": "2026-09-29T10:00:00Z"},
                    ],
                },
            },
        ],
    )
    context = _cli(identity, "context", "--project-ref", PROJECT_REF)
    brief = render_envelope({"ok": True, "result": context})
    line = next(line for line in brief.splitlines() if line.startswith("  claude-app@host"))
    assert "65%" in line and "resets 09-29 10:00Z" in line
    assert "23%" in line and "resets 09-28 03:00Z" in line
    assert "no windows" not in line

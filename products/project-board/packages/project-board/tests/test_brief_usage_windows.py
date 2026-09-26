"""Usage windows reach the coordinator across hosts, with their resets (W351).

2026-09-26 17:10Z: the operator routes by quota (one pool's agents up to 70%
of the weekly window, then others). The board records every agent's windows,
but `pb worker context` team cards dropped them, and that is the only
cross-host view a worker's CLI has; `pb worker list --format brief` showed
percentages only while usage was ok, and never when a window resets. The
acting coordinator told the operator usage was not visible.
"""

from __future__ import annotations

from project_board.client.render import render_envelope

WINDOWS = [
    {"name": "five_hour", "used_percent": 23.0, "window_minutes": 300, "resets_at": "2026-09-26T23:00:00Z"},
    {"name": "seven_day", "used_percent": 65.4, "window_minutes": 10080, "resets_at": "2026-09-29T10:00:00Z"},
]


def _list_lines(state):
    worker = {
        "worker_alias": "agent-one",
        "worker_name": "claude-code-1111",
        "runtime_kind": "claude-code",
        "pool_status": "active",
        "reachability": {"session_state": "working", "reachable": True},
        "runtime_limit_state": state,
    }
    text = render_envelope({"ok": True, "result": {"workers": [worker]}})
    return [line for line in text.splitlines() if line.startswith("usage:")]


def test_the_worker_list_prints_each_window_and_its_reset():
    state = {"kind": "ok", "source": "claude-code-statusline", "windows": WINDOWS}
    assert _list_lines(state) == [
        "usage: week 65% resets 09-29 10:00Z · 5 h 23% resets 09-26 23:00Z"
    ]


def test_a_limited_worker_still_shows_its_windows():
    state = {
        "kind": "rate_limited",
        "reached": "five_hour",
        "resets_at": "2026-09-26T23:00:00Z",
        "windows": [dict(WINDOWS[0], used_percent=100.0), WINDOWS[1]],
    }
    assert _list_lines(state) == [
        "usage: 5 h 100% resets 09-26 23:00Z · week 65% resets 09-29 10:00Z"
    ]


def test_a_worker_without_windows_says_so():
    assert _list_lines({"kind": "unknown", "windows": []}) == ["usage: no windows reported"]


def _context_team_lines(team):
    result = {
        "project_ref": "work:project:example",
        "workspace": "/workspace/agent-one",
        "team": team,
    }
    lines = render_envelope({"ok": True, "result": result}).splitlines()
    start = lines.index("team usage:")
    return lines[start + 1 :]


def test_context_prints_one_usage_line_per_teammate_across_hosts():
    team = [
        {
            "worker_name": "claude-code-1111",
            "worker_alias": "agent-one",
            "host_label": "host-one",
            "limit_state": {"kind": "ok", "windows": WINDOWS},
        },
        {
            "worker_name": "claude-code-2222",
            "worker_alias": "agent-two",
            "host_label": "host-two",
            "limit_state": {
                "kind": "rate_limited",
                "reached": "seven_day",
                "resets_at": "2026-09-29T10:00:00Z",
                "windows": [dict(WINDOWS[1], used_percent=100.0)],
            },
        },
        {"worker_name": "codex-3333", "worker_alias": "", "host_label": "host-two", "limit_state": {}},
    ]
    assert _context_team_lines(team) == [
        "  agent-one (claude-code-1111) on host-one: usage ok · week 65% resets 09-29 10:00Z · 5 h 23% resets 09-26 23:00Z",
        "  agent-two (claude-code-2222) on host-two: rate limited (week), resets 10:00Z · week 100% resets 09-29 10:00Z",
        "  codex-3333 on host-two: not reported",
    ]

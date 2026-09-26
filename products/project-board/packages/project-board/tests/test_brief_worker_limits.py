"""The brief worker list shows each worker's usage limits (W26).

2026-09-26: the shared account stops at 70% of its limits, and agents were told
to check before each large step. ``pb worker list`` carried
``runtime_limit_state`` in its JSON, but the brief rendering dropped it, so an
agent reading brief output could not see the numbers at all.
"""

from __future__ import annotations

from project_board.client.render import render_envelope


def _listed(state):
    worker = {
        "worker_alias": "claude-app@spark1",
        "worker_name": "claude-code-0107f9f8",
        "runtime_kind": "claude-code",
        "pool_status": "active",
        "reachability": {"session_state": "working", "reachable": True},
    }
    if state is not None:
        worker["runtime_limit_state"] = state
    text = render_envelope({"ok": True, "result": {"workers": [worker]}})
    return [line for line in text.splitlines() if line.startswith("limits:")]


def test_an_ok_state_shows_every_window_and_when_it_was_observed():
    assert _listed(
        {
            "kind": "ok",
            "source": "claude-code-statusline",
            "observed_at": "2026-09-26T10:35:57Z",
            "reached": "",
            "resets_at": "",
            "windows": [
                {"name": "five_hour", "used_percent": 7.0, "window_minutes": 300, "resets_at": "2026-09-26T14:10:00Z"},
                {"name": "seven_day", "used_percent": 45.0, "window_minutes": 10080, "resets_at": "2026-10-01T07:00:00Z"},
            ],
        }
    ) == ["limits: usage ok (week 45% · 5 h 7%) · observed 2026-09-26 10:35Z"]


def test_a_reached_limit_names_the_window_and_its_reset():
    assert _listed(
        {
            "kind": "rate_limited",
            "source": "claude-code-statusline",
            "observed_at": "2026-09-26T12:01:00Z",
            "reached": "five_hour",
            "resets_at": "2026-09-26T14:10:00Z",
            "windows": [
                {"name": "five_hour", "used_percent": 100.0, "window_minutes": 300, "resets_at": "2026-09-26T14:10:00Z"},
            ],
        }
    ) == ["limits: rate limited (5 h), resets 14:10Z · observed 2026-09-26 12:01Z"]


def test_a_state_without_windows_is_unknown_not_fine():
    assert _listed(
        {"kind": "unknown", "source": "claude-code-statusline", "windows": [], "observed_at": ""}
    ) == ["limits: limit unknown"]


def test_a_worker_that_reported_nothing_says_so():
    assert _listed(None) == ["limits: not reported"]

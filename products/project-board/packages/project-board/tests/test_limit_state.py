"""The limit state comes from the runtime's own record, never from silence (W26).

The Codex shape is the one codex-cli 0.154 writes into its rollout files
(checked on dev-main, 2026-09-23). The Claude Code shape is the status line
JSON from code.claude.com/docs/en/statusline.
"""

from __future__ import annotations

import json

from project_board.client.limit_state import (
    codex_limit_state,
    codex_rollout_path,
    limit_state_at,
    limit_state_from_claude_statusline,
    limit_state_from_claude_stop_failure,
    limit_state_from_codex,
    read_codex_rate_limits,
)

SESSION = "01a08da1-ee31-70a2-bf8d-8a08d55bdcf7"


def _rollout(tmp_path, *, limits, session=SESSION, filler=0):
    day = tmp_path / "2026" / "09" / "23"
    day.mkdir(parents=True)
    path = day / f"rollout-2026-09-23T19-00-00-{session}.jsonl"
    lines = [
        json.dumps({"timestamp": "2026-09-23T19:00:00.000Z", "type": "session_meta", "payload": {"id": session, "cli_version": "0.154.0"}}),
    ]
    # An older snapshot first, so the reader has to take the newest one.
    lines.append(json.dumps({"timestamp": "2026-09-23T19:05:00.000Z", "type": "event_msg", "payload": {"type": "token_count", "info": {}, "rate_limits": {"primary": {"used_percent": 10.0, "window_minutes": 300, "resets_at": 1790000000}, "secondary": None, "rate_limit_reached_type": None}}}))
    lines.extend(json.dumps({"timestamp": "2026-09-23T19:06:00.000Z", "type": "response_item", "payload": {"type": "message", "content": "x" * 200}}) for _ in range(filler))
    lines.append(json.dumps({"timestamp": "2026-09-23T19:40:00.000Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {}}, "rate_limits": limits}}))
    lines.append(json.dumps({"timestamp": "2026-09-23T19:41:00.000Z", "type": "event_msg", "payload": {"type": "agent_message", "message": "done"}}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


LIMITED = {
    "limit_id": "codex",
    "primary": {"used_percent": 100.0, "window_minutes": 300, "resets_at": 1790010000},
    "secondary": {"used_percent": 41.0, "window_minutes": 10080, "resets_at": 1790400000},
    "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
    "plan_type": "pro",
    "rate_limit_reached_type": "primary",
}


def test_the_newest_token_count_in_the_rollout_tail_is_the_state(tmp_path):
    path = _rollout(tmp_path, limits=LIMITED, filler=3000)
    assert codex_rollout_path(SESSION, sessions_root=tmp_path) == path
    assert codex_rollout_path("not-a-session", sessions_root=tmp_path) is None
    found = read_codex_rate_limits(path, tail_bytes=64 * 1024)
    assert found is not None
    timestamp, limits = found
    assert timestamp == "2026-09-23T19:40:00.000Z"
    assert limits["rate_limit_reached_type"] == "primary"
    state = codex_limit_state(SESSION, sessions_root=tmp_path)
    assert state["kind"] == "rate_limited"
    assert state["reached"] == "primary"
    assert state["resets_at"] == "2026-09-21T17:00:00Z"
    assert state["observed_at"] == "2026-09-23T19:40:00Z"
    assert state["source"] == "codex-rollout"
    assert state["plan_type"] == "pro"
    assert [w["name"] for w in state["windows"]] == ["primary", "secondary"]


def test_a_window_under_its_limit_reads_ok_and_a_missing_rollout_reads_none(tmp_path):
    fine = {"primary": {"used_percent": 33.0, "window_minutes": 10080, "resets_at": 1789435373}, "secondary": None, "rate_limit_reached_type": None, "plan_type": "pro"}
    state = limit_state_from_codex(fine, observed_at="2026-09-10T23:55:25.266Z")
    assert state["kind"] == "ok"
    assert state["reached"] == ""
    assert state["resets_at"] == ""
    assert state["windows"][0]["resets_at"] == "2026-09-15T01:22:53Z"
    assert state["observed_at"] == "2026-09-10T23:55:25Z"
    assert codex_limit_state(SESSION, sessions_root=tmp_path) is None
    assert limit_state_from_codex(None)["kind"] == "unknown"


def test_a_window_at_one_hundred_percent_is_rate_limited_even_without_the_named_type():
    state = limit_state_from_codex({"primary": {"used_percent": 100.0, "window_minutes": 300, "resets_at": 1790010000}, "secondary": {"used_percent": 5.0, "window_minutes": 10080, "resets_at": 1790400000}, "rate_limit_reached_type": None})
    assert state["kind"] == "rate_limited"
    assert state["reached"] == "primary"
    assert state["resets_at"] == "2026-09-21T17:00:00Z"
    spent = limit_state_from_codex({**LIMITED, "spend_control_reached": True, "rate_limit_reached_type": None})
    assert spent["kind"] == "out_of_tokens"
    assert spent["reached"] == "credits"


def test_claude_code_status_line_and_stop_failure_read_the_same_way():
    fine = limit_state_from_claude_statusline({"session_id": "s", "rate_limits": {"five_hour": {"used_percentage": 42, "resets_at": 1790010000}, "seven_day": {"used_percentage": 12, "resets_at": 1790400000}}}, observed_at="2026-09-23T20:00:00Z")
    assert fine["kind"] == "ok"
    assert [w["name"] for w in fine["windows"]] == ["five_hour", "seven_day"]
    assert fine["windows"][0]["window_minutes"] == 300
    hit = limit_state_from_claude_statusline({"rate_limits": {"five_hour": {"used_percentage": 100, "resets_at": 1790010000}}})
    assert hit["kind"] == "rate_limited"
    assert hit["reached"] == "five_hour"
    assert hit["resets_at"] == "2026-09-21T17:00:00Z"
    # Before the first API response there is no rate_limits object: unknown, not ok.
    assert limit_state_from_claude_statusline({"session_id": "s"})["kind"] == "unknown"
    stop = limit_state_from_claude_stop_failure({"error": "rate_limit"}, observed_at="2026-09-23T20:01:00Z")
    assert stop["kind"] == "rate_limited" and stop["resets_at"] == ""
    assert limit_state_from_claude_stop_failure({"error": "overloaded"})["kind"] == "unknown"


def test_a_limit_clears_when_its_reset_time_has_passed():
    state = {"kind": "rate_limited", "source": "codex-rollout", "windows": [], "reached": "primary", "resets_at": "2026-09-23T21:00:00Z", "observed_at": "2026-09-23T19:40:00Z"}
    assert limit_state_at(state, now="2026-09-23T20:30:00Z")["kind"] == "rate_limited"
    cleared = limit_state_at(state, now="2026-09-23T21:00:00Z")
    assert cleared["kind"] == "ok"
    assert cleared["cleared_at"] == "2026-09-23T21:00:00Z"
    assert limit_state_at(None, now="2026-09-23T21:00:00Z") is None


def test_the_listener_row_carries_the_state_for_codex_and_the_recorded_one_for_claude_code(tmp_path):
    from project_board.client.limit_state import session_with_limit_state

    _rollout(tmp_path, limits=LIMITED)
    listener = {"session_id": SESSION, "state": "listening", "presence": "listening"}
    codex = session_with_limit_state(listener, runtime_kind="codex", runtime_session_id=SESSION, now="2026-09-21T16:00:00Z", sessions_root=tmp_path)
    assert codex["limit_state"]["kind"] == "rate_limited"
    assert codex["session_id"] == SESSION
    # Past the reset, the same rollout reads ok, so the board clears it.
    later = session_with_limit_state(listener, runtime_kind="codex", runtime_session_id=SESSION, now="2026-09-21T17:00:00Z", sessions_root=tmp_path)
    assert later["limit_state"]["kind"] == "ok"
    # No rollout on this host: the row has no field, which is "not reported".
    absent = session_with_limit_state(listener, runtime_kind="codex", runtime_session_id="other", now="2026-09-21T16:00:00Z", sessions_root=tmp_path)
    assert "limit_state" not in absent
    # Claude Code has no file of its own: the recorded status-line state is used.
    recorded = limit_state_from_claude_statusline({"rate_limits": {"five_hour": {"used_percentage": 100, "resets_at": 1790010000}}}, observed_at="2026-09-21T16:30:00Z")
    claude = session_with_limit_state(listener, runtime_kind="claude-code", runtime_session_id="c", now="2026-09-21T16:40:00Z", recorded=recorded)
    assert claude["limit_state"]["kind"] == "rate_limited" and claude["limit_state"]["reached"] == "five_hour"
    assert "limit_state" not in session_with_limit_state(listener, runtime_kind="claude-code", runtime_session_id="c", now="2026-09-21T16:40:00Z")


def test_pb_worker_limit_state_records_what_claude_code_said_and_prints_one_line(tmp_path, capsys):
    import argparse
    import io

    from project_board.client import cli
    from project_board.client.limit_state import limit_state_line, session_with_limit_state
    from project_board.client.store import SharedFieldStore
    from relay_helpers import make_host

    host, identity, _channel = make_host(tmp_path)
    field = SharedFieldStore(host.field_root)
    field.initialize(field_id="limit-state")
    field.register_worker(
        worker_name=identity.worker_name,
        worker_identity=identity.worker_identity,
        runtime_kind=identity.runtime_kind,
        runtime_session_id=identity.runtime_session_id,
        capabilities=[],
        authority_label="connection-hub:test-profile",
        control_plane_state="published",
    )
    args = argparse.Namespace(
        config=str(host.path),
        runtime_kind=identity.runtime_kind,
        runtime_session_id=identity.runtime_session_id,
        source="statusline",
        payload_file="-",
    )
    payload = {"session_id": "s", "model": {"id": "m"}, "rate_limits": {"five_hour": {"used_percentage": 100, "resets_at": 1790010000}, "seven_day": {"used_percentage": 30, "resets_at": 1790400000}}}
    assert cli._limit_state_command(args, stdin=io.StringIO(json.dumps(payload))) == 0
    out = capsys.readouterr()
    # One plain line for the status bar, no envelope.
    assert out.out.strip() == "rate limited (five_hour), resets 17:00Z"
    recorded = field.runtime_limit_state(identity.worker_name)
    assert recorded["kind"] == "rate_limited" and recorded["source"] == "claude-code-statusline"
    assert recorded["recorded_at"]
    # The relay puts the recorded state on the listener row for a Claude Code worker.
    row = session_with_limit_state({"session_id": "s"}, runtime_kind="claude-code", runtime_session_id="s", now="2026-09-21T16:00:00Z", recorded=recorded)
    assert row["limit_state"]["reached"] == "five_hour"
    # An unreadable payload never breaks the status line.
    assert cli._limit_state_command(args, stdin=io.StringIO("not json")) == 0
    out = capsys.readouterr()
    assert out.out.strip() == "limit unknown" and "payload unreadable" in out.err
    # The stop-failure hook is the hit itself, without a reset time.
    stop_args = argparse.Namespace(**{**vars(args), "source": "stop-failure"})
    assert cli._limit_state_command(stop_args, stdin=io.StringIO(json.dumps({"error": "rate_limit"}))) == 0
    assert capsys.readouterr().out.strip() == "rate limited (rate_limit)"
    assert field.runtime_limit_state(identity.worker_name)["source"] == "claude-code-stop-failure"
    assert limit_state_line({"kind": "ok", "windows": [{"name": "five_hour", "used_percent": 42.0}]}) == "usage ok (five_hour 42%)"
    assert field.runtime_limit_state("nobody") == {}


def test_a_wake_waits_for_the_reset_the_runtime_named_and_for_nothing_else():
    from project_board.client.limit_state import wake_deferred_until

    limited = {"kind": "rate_limited", "resets_at": "2026-09-23T21:40:00Z", "windows": [], "reached": "primary"}
    assert wake_deferred_until(limited, now="2026-09-23T20:30:00Z") == "2026-09-23T21:40:00Z"
    # Past the reset the wake goes, and a limit without a reset time never withholds mail.
    assert wake_deferred_until(limited, now="2026-09-23T21:40:00Z") == ""
    assert wake_deferred_until({"kind": "rate_limited", "resets_at": ""}, now="2026-09-23T20:30:00Z") == ""
    assert wake_deferred_until({"kind": "ok", "resets_at": "2026-09-23T21:40:00Z"}, now="2026-09-23T20:30:00Z") == ""
    assert wake_deferred_until(None, now="2026-09-23T20:30:00Z") == ""
    spent = {"kind": "out_of_tokens", "resets_at": "2026-09-24T00:00:00Z"}
    assert wake_deferred_until(spent, now="2026-09-23T20:30:00Z") == "2026-09-24T00:00:00Z"

"""The model and reasoning effort come from the runtime's own record (W327).

Claude Code's status line shape is the one 2.1.282 builds (read from the
shipped binary, 2026-09-25): ``model.id``, ``model.display_name``,
``effort.level`` only for a model that supports effort, ``thinking.enabled``.
The Codex shape is the rollout's ``turn_context`` record.
"""

from __future__ import annotations

import argparse
import io
import json

from project_board.client.limit_state import limit_state_line
from project_board.client.relay import _heartbeat_session_projection
from project_board.client.runtime_model import (
    codex_runtime_model,
    read_codex_turn_context,
    runtime_model_from_claude_statusline,
    runtime_model_from_codex,
    runtime_model_line,
    session_with_runtime_model,
)

SESSION = "01a08da1-ee31-70a2-bf8d-8a08d55bdcf7"


# The newest real turn_context line of a codex-cli 0.154.0 session on dev-main
# (2026-09-25, paths redacted by codex-main).
REAL_TURN_CONTEXT = {"type": "turn_context", "payload": {"cwd": "...", "workspace_roots": ["..."], "model": "gpt-5.6-sol", "collaboration_mode": {"mode": "default", "settings": {"model": "gpt-5.6-sol", "reasoning_effort": "ultra", "developer_instructions": None}}, "effort": "ultra", "summary": "auto"}}


def _turn_context(model: str, effort: str | None, *, at: str) -> str:
    payload = json.loads(json.dumps(REAL_TURN_CONTEXT["payload"]))
    payload["model"] = model
    payload["collaboration_mode"]["settings"]["model"] = model
    if effort is None:
        payload.pop("effort")
        payload["collaboration_mode"]["settings"]["reasoning_effort"] = None
    else:
        payload["effort"] = effort
        payload["collaboration_mode"]["settings"]["reasoning_effort"] = effort
    return json.dumps({"timestamp": at, "type": "turn_context", "payload": payload})


def _rollout(tmp_path, lines: list[str], *, filler: int = 0):
    day = tmp_path / "2026" / "09" / "25"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-2026-09-25T15-00-00-{SESSION}.jsonl"
    body = [json.dumps({"timestamp": "2026-09-25T15:00:00.000Z", "type": "session_meta", "payload": {"id": SESSION, "cli_version": "0.154.0"}})]
    body.extend(lines[:1])
    body.extend(json.dumps({"timestamp": "2026-09-25T15:01:00.000Z", "type": "response_item", "payload": {"type": "message", "content": "x" * 200}}) for _ in range(filler))
    body.extend(lines[1:])
    body.append(json.dumps({"timestamp": "2026-09-25T15:41:00.000Z", "type": "event_msg", "payload": {"type": "agent_message", "message": "done"}}))
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return path


def test_the_real_codex_turn_context_reads_model_and_effort():
    record = runtime_model_from_codex(REAL_TURN_CONTEXT["payload"], observed_at="2026-09-25T15:40:00Z")
    assert record["model"] == "gpt-5.6-sol" and record["effort"] == "ultra"
    # Only the collaboration mode names them: still read.
    settings_only = {"collaboration_mode": {"settings": {"model": "gpt-5.6-sol", "reasoning_effort": "high"}}}
    assert runtime_model_from_codex(settings_only)["effort"] == "high"
    assert runtime_model_from_codex(settings_only)["model"] == "gpt-5.6-sol"


def test_codex_reads_the_newest_turn_context_in_the_rollout_tail(tmp_path):
    path = _rollout(
        tmp_path,
        [
            _turn_context("gpt-5.1-codex", "medium", at="2026-09-25T15:00:05.000Z"),
            # The agent changed its effort: the newest turn is what it runs with now.
            _turn_context("gpt-5.1-codex", "high", at="2026-09-25T15:40:00.000Z"),
        ],
        filler=3000,
    )
    found = read_codex_turn_context(path, tail_bytes=64 * 1024)
    assert found is not None and found[0] == "2026-09-25T15:40:00.000Z"
    record = codex_runtime_model(SESSION, sessions_root=tmp_path)
    assert record == {
        "model": "gpt-5.1-codex",
        "model_display": "",
        "effort": "high",
        "thinking": None,
        "source": "codex-rollout",
        "observed_at": "2026-09-25T15:40:00Z",
    }
    assert runtime_model_line(record) == "gpt-5.1-codex · effort high"


def test_codex_without_an_effort_reports_the_model_and_effort_not_reported(tmp_path):
    _rollout(tmp_path, [_turn_context("gpt-5.1-codex", None, at="2026-09-25T15:00:05.000Z")])
    record = codex_runtime_model(SESSION, sessions_root=tmp_path)
    assert record["model"] == "gpt-5.1-codex" and record["effort"] == ""
    assert runtime_model_line(record) == "gpt-5.1-codex · effort not reported"
    # The config key spelling reads too, so a rename in either direction reports.
    assert runtime_model_from_codex({"model": "m", "reasoning_effort": "XHigh"})["effort"] == "xhigh"
    # No turn context, or no rollout on this host: no record, which is "not reported".
    assert runtime_model_from_codex({"cwd": "..."}) is None
    assert codex_runtime_model("not-a-session", sessions_root=tmp_path) is None
    assert runtime_model_line(None) == "model not reported"


def test_claude_code_status_line_reads_model_effort_and_thinking():
    payload = {
        "session_id": "s",
        "model": {"id": "claude-opus-5-5[1m]", "display_name": "Opus 5.5 (1M context)"},
        "effort": {"level": "high"},
        "thinking": {"enabled": True},
        "fast_mode": False,
    }
    record = runtime_model_from_claude_statusline(payload, observed_at="2026-09-25T15:40:00Z")
    assert record == {
        "model": "claude-opus-5-5[1m]",
        "model_display": "Opus 5.5 (1M context)",
        "effort": "high",
        "thinking": True,
        "source": "claude-code-statusline",
        "observed_at": "2026-09-25T15:40:00Z",
    }
    assert runtime_model_line(record) == "Opus 5.5 (1M context) · effort high"
    # A model without effort support: Claude Code leaves ``effort`` out.
    haiku = runtime_model_from_claude_statusline({"model": {"id": "claude-haiku-4-5", "display_name": "Haiku 4.5"}, "thinking": {"enabled": False}})
    assert haiku["effort"] == "" and haiku["thinking"] is False
    assert runtime_model_line(haiku) == "Haiku 4.5 · effort not reported"
    # Nothing about the model at all is no record.
    assert runtime_model_from_claude_statusline({"session_id": "s"}) is None
    assert runtime_model_from_claude_statusline(None) is None


def test_the_listener_row_and_the_heartbeat_carry_the_model(tmp_path):
    _rollout(tmp_path, [_turn_context("gpt-5.1-codex", "low", at="2026-09-25T15:00:05.000Z")])
    listener = {"session_id": SESSION, "state": "listening", "presence": "listening", "attached_at": "x", "heartbeat_at": "y"}
    codex = session_with_runtime_model(listener, runtime_kind="codex", runtime_session_id=SESSION, sessions_root=tmp_path)
    assert codex["runtime_model"]["effort"] == "low"
    assert _heartbeat_session_projection(codex)["runtime_model"]["model"] == "gpt-5.1-codex"
    recorded = {**runtime_model_from_claude_statusline({"model": {"id": "m", "display_name": "M"}, "effort": {"level": "max"}}), "recorded_at": "z"}
    claude = session_with_runtime_model(listener, runtime_kind="claude-code", runtime_session_id="c", recorded=recorded)
    assert claude["runtime_model"]["effort"] == "max" and "recorded_at" not in claude["runtime_model"]
    # Nothing reported: the row carries no field, and neither does the heartbeat.
    bare = session_with_runtime_model({**listener, "runtime_model": {"model": "stale"}}, runtime_kind="claude-code", runtime_session_id="c")
    assert "runtime_model" not in bare
    assert "runtime_model" not in _heartbeat_session_projection(bare)


def test_pb_worker_limit_state_records_the_model_and_rewrites_it_only_on_a_change(tmp_path, capsys):
    from project_board.client import cli
    from project_board.client.io import read_json
    from project_board.client.store import SharedFieldStore
    from relay_helpers import make_host

    host, identity, _channel = make_host(tmp_path)
    field = SharedFieldStore(host.field_root)
    field.initialize(field_id="runtime-model")
    field.register_worker(
        worker_name=identity.worker_name, worker_identity=identity.worker_identity,
        runtime_kind=identity.runtime_kind, runtime_session_id=identity.runtime_session_id,
        capabilities=[], authority_label="connection-hub:test-profile", control_plane_state="published",
    )
    before = read_json(field._worker_path(identity.worker_name))
    args = argparse.Namespace(
        config=str(host.path), runtime_kind=identity.runtime_kind,
        runtime_session_id=identity.runtime_session_id, source="statusline", payload_file="-",
    )
    payload = {"session_id": "s", "model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"}, "effort": {"level": "high"}, "thinking": {"enabled": True}, "rate_limits": {"five_hour": {"used_percentage": 20, "resets_at": 1790010000}}}
    assert cli._limit_state_command(args, stdin=io.StringIO(json.dumps(payload))) == 0
    # The status line itself is unchanged: one limit line.
    assert capsys.readouterr().out.strip() == limit_state_line({"kind": "ok", "windows": [{"name": "five_hour", "used_percent": 20.0}]})
    first = field.runtime_model(identity.worker_name)
    assert first["model"] == "claude-opus-5-5" and first["effort"] == "high" and first["recorded_at"]

    # The same model and effort again is not rewritten, and never counts as activity.
    assert cli._limit_state_command(args, stdin=io.StringIO(json.dumps(payload))) == 0
    assert field.runtime_model(identity.worker_name)["recorded_at"] == first["recorded_at"]
    assert read_json(field._worker_path(identity.worker_name)).get("updated_at") == before.get("updated_at")

    # The agent changes its effort: the record follows at once.
    changed = {**payload, "effort": {"level": "low"}}
    assert cli._limit_state_command(args, stdin=io.StringIO(json.dumps(changed))) == 0
    assert field.runtime_model(identity.worker_name)["effort"] == "low"

    # A StopFailure hook carries no model and leaves the record alone.
    stop = argparse.Namespace(**{**vars(args), "source": "stop-failure"})
    assert cli._limit_state_command(stop, stdin=io.StringIO(json.dumps({"error": "rate_limit"}))) == 0
    capsys.readouterr()
    assert field.runtime_model(identity.worker_name)["effort"] == "low"
    assert field.runtime_model("nobody") == {}

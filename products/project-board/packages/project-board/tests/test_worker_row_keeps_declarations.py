"""Re-registering a worker keeps what the worker declared (W26, W278 part B).

The relay registers the worker on every cycle. On 2026-09-23 that rebuilt the
row from a chosen set of fields, so a worktree declared at 22:09:20Z was gone
by the next cycle and the board never saw a file in flight. The same would
have erased a Claude Code session's recorded limit state.
"""

from __future__ import annotations

from project_board.client.store import SharedFieldStore
from relay_helpers import make_host


def _register(field, identity, **overrides):
    values = dict(
        worker_name=identity.worker_name, worker_identity=identity.worker_identity,
        runtime_kind=identity.runtime_kind, runtime_session_id=identity.runtime_session_id,
        capabilities=[], authority_label="connection-hub:test-profile", control_plane_state="published",
    )
    values.update(overrides)
    return field.register_worker(**values)


def test_workspaces_and_the_recorded_limit_state_survive_re_registration(tmp_path):
    host, identity, _channel = make_host(tmp_path)
    field = SharedFieldStore(host.field_root)
    field.initialize(field_id="keeps")
    _register(field, identity)
    (tmp_path / "wt").mkdir()
    field.declare_workspace(identity.worker_name, assignment_ref="work:assignment:one", repository_ref="repo:ae/products", path=str(tmp_path / "wt"))
    field.record_runtime_limit_state(identity.worker_name, {"kind": "rate_limited", "source": "claude-code-statusline", "windows": [], "reached": "five_hour", "resets_at": "2026-09-23T23:00:00Z", "observed_at": "2026-09-23T22:00:00Z"})
    # The relay's next cycle re-registers the same worker.
    for _ in range(3):
        row = _register(field, identity)
    assert [w["repository_ref"] for w in field.workspaces(identity.worker_name)] == ["repo:ae/products"]
    assert field.runtime_limit_state(identity.worker_name)["reached"] == "five_hour"
    assert row["workspaces"][0]["path"] == str((tmp_path / "wt").resolve())
    assert row["runtime_limit_state"]["kind"] == "rate_limited"
    # Registration overwrites only what it owns: an unrelated field written by
    # a future command survives without anyone naming it here.
    from project_board.client.io import atomic_write_json, read_json

    path = field._worker_path(identity.worker_name)
    row_now = read_json(path)
    row_now["future_declaration"] = {"kind": "anything", "since": "2026-09-23T22:00:00Z"}
    atomic_write_json(path, row_now)
    again = _register(field, identity)
    assert again["future_declaration"] == {"kind": "anything", "since": "2026-09-23T22:00:00Z"}
    assert again["worker_name"] == identity.worker_name and again["revision"] > row["revision"]
    # A worker that declared nothing has no such keys and reads as empty.
    fresh = _register(field, identity.__class__.create("codex", "22222222-2222-4222-8222-222222222222"))
    assert field.workspaces(fresh["worker_name"]) == [] and field.runtime_limit_state(fresh["worker_name"]) == {}

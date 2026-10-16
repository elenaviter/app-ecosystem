"""A worker with files in flight heartbeats on its interval, not on every relay cycle (W875).

The supervisor rebuilds a child adapter per attended project every cycle. The
child shared the channel's session-report signatures and heartbeat clock, but
not its tracked-files signatures, so each cycle's child found no signature,
saw the unchanged non-empty set as a change, and sent a heartbeat. On a busy
host relay, which cycles every few seconds, workers with files in flight
heartbeated 4-5 times a minute instead of about twice. The 30 s interval stays.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

from project_board.client import relay
from project_board.client.relay import ProblemBoardHostRelayAdapter

PROJECT = "work:project:project-one"
FILES = [{"assignment_ref": "work:assignment:one", "repository": "repo", "paths": ["a.py"]}]


def _channel(monkeypatch, clock: list[float]):
    """The persistent channel adapter, built with the real constructor."""

    monkeypatch.setattr(relay, "RelayConfig",
                        lambda **values: SimpleNamespace(**{**values, "journal_workspace": lambda: None}))
    config = SimpleNamespace(project_id="attendance", worker_name="worker-a", worker_identity="synthetic-viewer",
                             connection_hub_profile="synthetic-profile", reconcile_ceiling_seconds=30,
                             journal_workspace=lambda: None)
    field = SimpleNamespace(workspaces=lambda name: [{"path": "/synthetic"}], list_assignments=lambda project_id: [])
    client = SimpleNamespace(connected=True, connection_generation=1, partition_ref="synthetic-partition")
    channel = ProblemBoardHostRelayAdapter(config=config, field=field, client=client, monotonic=lambda: clock[0],
                                           worktree_observer=lambda *args, **kwargs: None)
    channel._attendance_cache.update({"initialized": True, "items": []})
    return channel


def _cycle(channel, monkeypatch, observations) -> bool:
    """One relay cycle: a fresh child from the supervisor's own builder, the real decision."""

    child = channel._attended_project_adapter("project-one")
    monkeypatch.setattr(relay, "observe_assignments", lambda *args, **kwargs: list(observations))
    for name, result in {"_store_reads_delta": (None, "r"), "_worker_info": ({}, False),
                         "_alias_request": ({}, False), "_workspace_report": ({}, False)}.items():
        monkeypatch.setattr(child, name, lambda *args, _result=result, **kwargs: _result)
    for name in ("_add_worker_info", "_add_alias_request", "_add_project_record", "_add_workspace_report"):
        monkeypatch.setattr(child, name, lambda *args, **kwargs: None)
    sessions = [{"session_id": "s1", "check_interval_seconds": 30}]
    # Unforced, as poll_attendances_once calls it.
    prepared = child._prepare_project_heartbeat(project_ref=PROJECT, agent_sessions=sessions, force_heartbeat=False)
    if prepared["heartbeat_sent"]:
        # What _record_project_heartbeat_result writes for these three after an answer.
        child._record_project_heartbeat(PROJECT)
        child._record_session_report(project_ref=PROJECT, signature=prepared["session_signature"])
        child._assignment_files_signatures[PROJECT] = prepared["files_signature"]
    return bool(prepared["heartbeat_sent"])


def test_unchanged_files_in_flight_heartbeat_once_per_interval_on_a_busy_rebuilding_relay(monkeypatch):
    clock = [0.0]
    channel = _channel(monkeypatch, clock)
    sent = []
    # A busy host: a cycle every 5 s for 60 s, a fresh child each cycle.
    for step in range(13):
        clock[0] = step * 5.0
        if _cycle(channel, monkeypatch, FILES):
            sent.append(clock[0])
    # The first heartbeat publishes the set; after that only the 30 s interval.
    assert sent == [0.0, 30.0, 60.0]


def test_a_changed_file_set_still_heartbeats_at_once(monkeypatch):
    clock = [0.0]
    channel = _channel(monkeypatch, clock)
    assert _cycle(channel, monkeypatch, FILES)
    clock[0] = 5.0
    assert not _cycle(channel, monkeypatch, FILES)
    clock[0] = 10.0
    changed = [{**FILES[0], "paths": ["a.py", "b.py"]}]
    assert _cycle(channel, monkeypatch, changed), "a new file in flight is reported at once"


def test_each_child_holds_the_channels_maps_not_copies(monkeypatch):
    channel = _channel(monkeypatch, [0.0])
    child = channel._attended_project_adapter("project-one")
    assert child._assignment_files_signatures is channel._assignment_files_signatures
    assert child._store_reads_signatures is channel._store_reads_signatures
    assert child._session_report_signatures is channel._session_report_signatures
    assert child._heartbeat_sent_at is channel._heartbeat_sent_at


def test_the_supervisor_builds_its_children_with_the_shared_builder():
    source = inspect.getsource(ProblemBoardHostRelayAdapter.poll_attendances_once)
    assert "self._attended_project_adapter(parsed.object_id)" in source
    assert "ProblemBoardHostRelayAdapter(" not in source

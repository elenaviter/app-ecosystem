"""Display snapshot integrity, legacy recovery and actual relay materializer."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from project_board.client.heartbeat_team import TeamSnapshots
from project_board.client.mcp_client import ProblemBoardDataBusClient
from project_board.client.relay import ProblemBoardHostRelayAdapter
from project_board.contract.errors import DomainError
from project_board.contract.heartbeat_team import TEAM_PROTOCOL, content_digest, revision_parts


PROJECT = "work:project:project-one"
CONTEXT = ("channel-a", 1)
TEAM = [{"worker_name": "worker-a", "runtime_model": {"model": "synthetic-a"},
         "runtime_account": {"account_id": "synthetic-account"}, "limit_state": {"used_percent": 10}}]


def full(team=TEAM, scope="a" * 64):
    return {"team": deepcopy(team), "team_view": {"protocol": TEAM_PROTOCOL, "state": "changed",
            "scope": scope, "revision": "team-v1:" + scope + ":" + content_digest(team)}}


def unchanged(token):
    return {"team_view": {"protocol": TEAM_PROTOCOL, "state": "unchanged", "revision": token, "scope": revision_parts(token)[0]}}


def seed(cache, context=CONTEXT):
    request = cache.begin(PROJECT, context)
    cache.apply(request, full(), context=context, materialize=lambda rows: None)


def test_only_coherent_materialized_pair_can_send_a_known_token():
    cache = TeamSnapshots()
    first = cache.begin(PROJECT, CONTEXT)
    assert first.payload() == {"team_protocol": TEAM_PROTOCOL}
    assert not cache.begin(PROJECT, CONTEXT).known_revision
    seed(cache)
    assert cache.begin(PROJECT, CONTEXT).known_revision == full()["team_view"]["revision"]


def test_unchanged_rematerializes_exact_snapshot_defensively():
    cache = TeamSnapshots()
    seed(cache)
    request = cache.begin(PROJECT, CONTEXT)
    saved = []
    def materialize(rows):
        saved.append(deepcopy(rows))
        rows.clear()
    assert cache.apply(request, unchanged(request.known_revision), context=CONTEXT, materialize=materialize) == "unchanged"
    assert saved == [TEAM]
    assert cache.begin(PROJECT, CONTEXT).known_revision == request.known_revision


def test_materialization_failure_drops_token_and_next_request_is_full():
    cache = TeamSnapshots()
    seed(cache)
    request = cache.begin(PROJECT, CONTEXT)
    def fail(rows):
        raise OSError("synthetic store failure")
    with pytest.raises(OSError):
        cache.apply(request, full([]), context=CONTEXT, materialize=fail)
    assert cache.begin(PROJECT, CONTEXT).known_revision == ""


@pytest.mark.parametrize("context", [("channel-a", 2), ("channel-b", 1), None])
def test_reconnect_account_or_channel_replacement_forgets_previous_pair(context):
    cache = TeamSnapshots()
    seed(cache)
    request = cache.begin(PROJECT, context)
    assert request.known_revision == ""
    if context is None:
        assert request.payload() == {}


def test_old_reply_cannot_overwrite_later_request():
    cache = TeamSnapshots()
    first = cache.begin(PROJECT, CONTEXT)
    newer = cache.begin(PROJECT, CONTEXT)
    seen = []
    assert cache.apply(newer, full([]), context=CONTEXT, materialize=seen.append) == "full"
    assert cache.apply(first, full(), context=CONTEXT, materialize=seen.append) == "obsolete"
    assert seen == [[]]


def test_reconnect_during_request_fences_the_reply():
    cache = TeamSnapshots()
    seed(cache)
    request = cache.begin(PROJECT, CONTEXT)
    assert cache.apply(request, full([]), context=("channel-a", 2), materialize=lambda rows: pytest.fail("obsolete reply")) == "obsolete"
    assert not cache.begin(PROJECT, ("channel-a", 2)).known_revision


@pytest.mark.parametrize("legacy", [{"team": []}, {"team": TEAM}, {"team": TEAM, "team_view": {"protocol": "future"}}])
def test_old_server_rollback_replaces_whole_team_and_clears_conditional_knowledge(legacy):
    cache = TeamSnapshots()
    seed(cache)
    request = cache.begin(PROJECT, CONTEXT)
    rows = []
    assert cache.apply(request, legacy, context=CONTEXT, materialize=rows.append) == "full"
    assert rows == [legacy["team"]]
    assert cache.begin(PROJECT, CONTEXT).known_revision == ""


def test_real_changed_empty_clears_and_keeps_its_matching_token():
    cache = TeamSnapshots()
    seed(cache)
    request = cache.begin(PROJECT, CONTEXT)
    rows = []
    cache.apply(request, full([]), context=CONTEXT, materialize=rows.append)
    assert rows == [[]]
    assert cache.begin(PROJECT, CONTEXT).known_revision == full([])["team_view"]["revision"]


def test_missing_pair_unchanged_never_pretends_an_empty_snapshot():
    cache = TeamSnapshots()
    request = cache.begin(PROJECT, CONTEXT)
    result = cache.apply(request, unchanged(full()["team_view"]["revision"]), context=CONTEXT,
                         materialize=lambda rows: pytest.fail("no snapshot exists"))
    assert result == "full_required"
    assert not cache.begin(PROJECT, CONTEXT).known_revision


def test_corrupt_pair_and_changed_body_digest_require_a_full_next_request():
    cache = TeamSnapshots()
    seed(cache)
    cache._snapshots[PROJECT].team.clear()
    assert not cache.begin(PROJECT, CONTEXT).known_revision
    request = cache.begin(PROJECT, CONTEXT)
    bad = full()
    bad["team"] = []
    assert cache.apply(request, bad, context=CONTEXT, materialize=lambda rows: pytest.fail("mismatched full body")) == "full_required"
    assert not cache.begin(PROJECT, CONTEXT).known_revision


def test_unchanged_tag_with_team_is_not_an_implicit_clear():
    cache = TeamSnapshots()
    seed(cache)
    request = cache.begin(PROJECT, CONTEXT)
    response = {**unchanged(request.known_revision), "team": []}
    assert cache.apply(request, response, context=CONTEXT, materialize=lambda rows: pytest.fail("contradictory marker")) == "full_required"
    assert not cache.begin(PROJECT, CONTEXT).known_revision


def test_unlink_and_restart_require_full_snapshots():
    cache = TeamSnapshots()
    seed(cache)
    cache.retain_projects(set())
    assert not cache.begin(PROJECT, CONTEXT).known_revision
    assert not TeamSnapshots().begin(PROJECT, CONTEXT).known_revision


def adapter_for_team(monkeypatch):
    adapter = ProblemBoardHostRelayAdapter.__new__(ProblemBoardHostRelayAdapter)
    adapter.config = SimpleNamespace(project_id="project-one", worker_name="worker-a", worker_identity="synthetic-viewer",
                                     connection_hub_profile="synthetic-profile")
    adapter.client = SimpleNamespace(connected=True, connection_generation=1, partition_ref="synthetic-partition")
    adapter._assignment_files_signatures = {}
    adapter._store_reads_signatures = {}
    adapter._attendance_cache = {"initialized": True, "items": []}
    adapter._session_report_signatures = {}
    adapter._heartbeat_sent_at = {}
    adapter._monotonic = lambda: 0
    seen = []
    adapter.field = SimpleNamespace(sync_project_team=lambda project_id, team: seen.append(deepcopy(team)),
                                    sync_project_coordinator=lambda *args: None, sync_project_roles=lambda *args: None)
    for name in ("_record_project_heartbeat", "_record_session_report", "_acknowledge_worker_info", "_acknowledge_alias_request",
                 "_acknowledge_workspace_report", "_record_attendance_observation", "_materialize_attended_project"):
        monkeypatch.setattr(adapter, name, lambda *args, **kwargs: None)
    return adapter, seen


def prepared(adapter):
    return {"session_signature": "s", "files_signature": "f", "store_reads_delta": None, "store_reads_signature": "r",
            "worker_info": {}, "alias_request": {}, "workspace_report": {},
            "team_request": adapter._heartbeat_teams().begin(PROJECT, adapter._team_context())}


def test_actual_relay_materializer_retains_unchanged_and_clears_removed_fields(monkeypatch):
    adapter, seen = adapter_for_team(monkeypatch)
    first = prepared(adapter)
    adapter._record_project_heartbeat_result(project_ref=PROJECT, prepared=first, heartbeat_result=full())
    next_request = prepared(adapter)
    adapter._record_project_heartbeat_result(project_ref=PROJECT, prepared=next_request,
        heartbeat_result=unchanged(next_request["team_request"].known_revision))
    assert seen == [TEAM, TEAM]
    bare = [{"worker_name": "worker-a"}]
    adapter._record_project_heartbeat_result(project_ref=PROJECT, prepared=prepared(adapter), heartbeat_result=full(bare))
    assert seen[-1] == bare, "omitted model/account/limit values cannot survive a replacement"


def test_real_client_public_socket_generation_is_the_relay_cache_fence(monkeypatch):
    transport = SimpleNamespace(connected=True, connection_generation=1)
    client = ProblemBoardDataBusClient(transport, partition_ref="synthetic-partition")
    adapter, seen = adapter_for_team(monkeypatch)
    adapter.client = client
    adapter._record_project_heartbeat_result(project_ref=PROJECT, prepared=prepared(adapter), heartbeat_result=full())
    assert prepared(adapter)["team_request"].known_revision
    transport.connection_generation = 2
    assert not prepared(adapter)["team_request"].known_revision
    del transport.connection_generation
    assert adapter._team_context() is None and client.connection_generation == 0


def test_actual_relay_materialization_refusal_drops_conditional_knowledge(monkeypatch):
    adapter, seen = adapter_for_team(monkeypatch)
    def refuse(*args):
        raise DomainError("project_not_found", "synthetic missing project")
    adapter.field.sync_project_team = refuse
    adapter._record_project_heartbeat_result(project_ref=PROJECT, prepared=prepared(adapter), heartbeat_result=full())
    assert not prepared(adapter)["team_request"].known_revision


def test_actual_prepare_path_sends_only_the_coherent_token_and_resets_on_reconnect(monkeypatch):
    adapter, seen = adapter_for_team(monkeypatch)
    for name, result in {"_session_report_delta": (None, "s"), "_assignment_files_delta": (None, "f"),
                         "_store_reads_delta": (None, "r"), "_worker_info": ({}, False),
                         "_alias_request": ({}, False), "_workspace_report": ({}, False)}.items():
        monkeypatch.setattr(adapter, name, lambda *args, _result=result, **kwargs: _result)
    for name in ("_add_worker_info", "_add_alias_request", "_add_project_record", "_add_workspace_report"):
        monkeypatch.setattr(adapter, name, lambda *args, **kwargs: None)
    def prepare():
        return adapter._prepare_project_heartbeat(project_ref=PROJECT, agent_sessions=[], force_heartbeat=True)
    first = prepare()
    assert first["heartbeat_payload"] == {"availability": "available", "project_ref": PROJECT, "team_protocol": TEAM_PROTOCOL}
    adapter._record_project_heartbeat_result(project_ref=PROJECT, prepared=first, heartbeat_result=full())
    assert prepare()["heartbeat_payload"]["known_team_revision"] == full()["team_view"]["revision"]
    adapter.client.connection_generation = 2
    assert "known_team_revision" not in prepare()["heartbeat_payload"]
    adapter.client.connected = False
    assert "team_protocol" not in prepare()["heartbeat_payload"]
    adapter.client.connected = True
    assert "known_team_revision" not in prepare()["heartbeat_payload"]
    adapter._invalidate_cached_project(PROJECT)
    assert "known_team_revision" not in prepare()["heartbeat_payload"]


def test_rebuilt_attendance_adapter_uses_the_parent_pair_owner(monkeypatch):
    adapter, seen = adapter_for_team(monkeypatch)
    adapter.config.journal_workspace = lambda: None
    adapter._record_project_heartbeat_result(project_ref=PROJECT, prepared=prepared(adapter), heartbeat_result=full())
    child = ProblemBoardHostRelayAdapter(config=adapter.config, field=adapter.field, client=adapter.client,
                                        team_snapshots=adapter._heartbeat_teams())
    assert child._heartbeat_teams() is adapter._heartbeat_teams()
    assert child._heartbeat_teams().begin(PROJECT, child._team_context()).known_revision

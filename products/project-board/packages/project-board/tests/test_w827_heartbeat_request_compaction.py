"""Smaller heartbeat requests: a known-reply digest and compact session/store-read forms (W827 S2b).

The relay's ``known_reply`` echo was about half of every heartbeat request on
the Data Bus message stream. A Board that keeps the identities it sent under
their digest lets the relay echo only that digest; ``agent_sessions`` and
``store_reads`` shrink to what changed. Every compact form is used only after
the Board's previous reply for that scope advertised it, so an old Board, an
old relay and a rollback between two heartbeats all keep the full forms.
When a heartbeat is sent is unchanged (W875).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
from types import SimpleNamespace

import pytest

from project_board.client import relay
from project_board.client.heartbeat_reply import ReplySnapshots
from project_board.client.relay import ProblemBoardHostRelayAdapter
from project_board.contract.heartbeat_reply import (
    KNOWN_DIGEST_FEATURE, REPLY_PROTOCOL, REQUEST_FEATURES, SESSION_TIMES_FEATURE,
    STORE_READS_CHANGED_FEATURE, canonical_bytes, digest, encode_reply, knowledge,
    known_from_record, known_record, reply_features, reply_scope_id,
)

SCOPE = {"tenant": "test", "platform_project": "test", "bundle": "test",
         "viewer": "test-worker", "authority": "test-card", "view": "heartbeat.reply",
         "project_ref": "work:project:test"}
SCOPE_ID = reply_scope_id(SCOPE)
STAMP = "2026-10-16T23:00:00.000000Z"
NEXT = "2026-10-16T23:01:00.000000Z"
LATER = "2026-10-16T23:02:00.000000Z"
PROJECT = "work:project:project-one"
CONTEXT = ("synthetic-client", 1, "synthetic-worker", "profile", "partition")


def value(stamp=STAMP):
    return {"team": [{"worker_name": f"synthetic-{i}", "heartbeat_at": stamp,
                      "info_text": "synthetic display " * 20} for i in range(6)],
            "self": {"worker_name": "test-worker"}, "heartbeat_at": stamp, "updated_at": stamp}


class NewBoard:
    """The Board side of S2b, in memory: keeps knowledge by digest, advertises features."""

    def __init__(self):
        self.kept: dict[str, bytes] = {}
        self.payloads: list[dict] = []

    def reply(self, snapshot, payload):
        self.payloads.append(deepcopy(payload))
        known = payload.get("known_reply")
        if known is None and payload.get("known_reply_digest"):
            raw = self.kept.get(payload["known_reply_digest"])
            known = known_from_record(raw, payload["known_reply_digest"], SCOPE_ID) if raw else None
        wire = encode_reply(deepcopy(snapshot), protocol=payload.get("reply_protocol"), scope=SCOPE, known=known)
        marker = wire["heartbeat_reply"]
        marker["features"] = sorted(REQUEST_FEATURES)
        record = known_record(snapshot, marker["scope"])
        if record is not None:
            self.kept[record[0]] = record[1]
            marker["known_digest"] = record[0]
        return wire


class OldBoard:
    """A W877 Board: the reply protocol, no digest, no features."""

    def __init__(self):
        self.payloads: list[dict] = []

    def reply(self, snapshot, payload):
        self.payloads.append(deepcopy(payload))
        return encode_reply(deepcopy(snapshot), protocol=payload.get("reply_protocol"), scope=SCOPE,
                            known=payload.get("known_reply"))


# -- contract --------------------------------------------------------------------------------

def test_known_record_is_the_digest_of_exactly_the_knowledge_a_relay_echoes():
    snapshot = value()
    record = known_record(snapshot, SCOPE_ID)
    known = knowledge(snapshot, SCOPE_ID)
    assert record == (digest(known), canonical_bytes(known))
    assert known_from_record(record[1], record[0], SCOPE_ID) == known
    assert known_from_record(record[1].decode("ascii"), record[0], SCOPE_ID) == known


@pytest.mark.parametrize("case", ["tampered", "wrong-digest", "foreign-scope", "not-hex", "not-bytes", "not-json"])
def test_a_kept_record_that_does_not_verify_is_a_miss(case):
    snapshot = value()
    found, raw = known_record(snapshot, SCOPE_ID)
    scope_id = SCOPE_ID
    if case == "tampered":
        raw = raw.replace(b'"revision":"', b'"revision":"0', 1)
    elif case == "wrong-digest":
        found = "0" * 64
    elif case == "foreign-scope":
        scope_id = reply_scope_id({**SCOPE, "viewer": "another-worker"})
    elif case == "not-hex":
        found = found.upper()
    elif case == "not-bytes":
        raw = {"scope": SCOPE_ID}
    elif case == "not-json":
        raw = b"{"
        found = hashlib.sha256(raw).hexdigest()
    assert known_from_record(raw, found, scope_id) is None


def test_oversized_knowledge_is_never_kept():
    snapshot = {f"section-{i}": {"v": i} for i in range(1200)}
    assert len(canonical_bytes(knowledge(snapshot, SCOPE_ID))) > 32_768
    assert known_record(snapshot, SCOPE_ID) is None


def test_features_name_only_known_request_features():
    marker = {"protocol": REPLY_PROTOCOL, "features": [KNOWN_DIGEST_FEATURE, "future-feature", 7]}
    assert reply_features({"heartbeat_reply": marker}) == {KNOWN_DIGEST_FEATURE}
    assert reply_features({"heartbeat_reply": {"protocol": "other", "features": [KNOWN_DIGEST_FEATURE]}}) == set()
    assert reply_features({"team": []}) == set()
    assert reply_features(None) == set()


def test_an_old_relay_reading_a_new_board_reply_ignores_the_extra_marker_fields():
    # decode_reply is what a W877 relay runs; the new marker keys are not in its way.
    from project_board.contract.heartbeat_reply import decode_reply
    board = NewBoard()
    first = board.reply(value(), {"reply_protocol": REPLY_PROTOCOL})
    snapshot, scope = decode_reply(first)
    assert snapshot == value() and scope == SCOPE_ID
    second = board.reply(value(NEXT), {"reply_protocol": REPLY_PROTOCOL, "known_reply": knowledge(snapshot, scope)})
    assert second["heartbeat_reply"]["state"] == "delta"
    assert decode_reply(second, baseline=snapshot, scope=scope)[0] == value(NEXT)


# -- the relay's snapshot owner --------------------------------------------------------------

def _beat(owner, board, snapshot, project="p", context=CONTEXT):
    request = owner.begin(project, context)
    payload = request.payload()
    wire = board.reply(snapshot, payload)
    seen = []
    result = owner.apply(request, wire, context=context, materialize=seen.append)
    assert result == snapshot and seen == [snapshot]
    return payload


def test_new_relay_new_board_echoes_only_the_digest_after_the_board_kept_it():
    owner, board = ReplySnapshots(), NewBoard()
    first = _beat(owner, board, value())
    assert "known_reply" not in first and "known_reply_digest" not in first
    second = _beat(owner, board, value(NEXT))
    assert set(second) == {"reply_protocol", "known_reply_digest"}
    assert len(canonical_bytes(second)) < 160  # was the whole identity map
    # The Board resolved it: the reply was a delta, not a full snapshot.
    assert board.kept and board.payloads[-1]["known_reply_digest"] in board.kept


def test_a_board_miss_is_a_full_reply_and_the_next_digest_is_kept_again():
    owner, board = ReplySnapshots(), NewBoard()
    _beat(owner, board, value())
    board.kept.clear()  # TTL expiry, another process's cache, a flush
    request = owner.begin("p", CONTEXT)
    wire = board.reply(value(NEXT), request.payload())
    assert wire["heartbeat_reply"]["state"] == "full"
    assert owner.apply(request, wire, context=CONTEXT, materialize=lambda v: None) == value(NEXT)
    request = owner.begin("p", CONTEXT)
    wire = board.reply(value(LATER), request.payload())
    assert wire["heartbeat_reply"]["state"] == "delta"


def test_new_relay_old_board_keeps_sending_the_full_identities():
    owner, board = ReplySnapshots(), OldBoard()
    _beat(owner, board, value())
    second = _beat(owner, board, value(NEXT))
    assert "known_reply" in second and "known_reply_digest" not in second


def test_a_digest_the_board_offers_for_other_knowledge_is_not_echoed():
    owner, board = ReplySnapshots(), NewBoard()
    request = owner.begin("p", CONTEXT)
    wire = board.reply(value(), request.payload())
    wire["heartbeat_reply"]["known_digest"] = "0" * 64
    owner.apply(request, wire, context=CONTEXT, materialize=lambda v: None)
    payload = owner.begin("p", CONTEXT).payload()
    assert "known_reply" in payload and "known_reply_digest" not in payload


def test_rollback_to_an_old_board_between_two_beats_stays_lossless():
    owner, new, old = ReplySnapshots(), NewBoard(), OldBoard()
    _beat(owner, new, value())
    # The digest reaches a Board that ignores it: no known_reply, so a full reply.
    request = owner.begin("p", CONTEXT)
    payload = request.payload()
    assert "known_reply_digest" in payload
    wire = old.reply(value(NEXT), payload)
    assert wire["heartbeat_reply"]["state"] == "full"
    assert owner.apply(request, wire, context=CONTEXT, materialize=lambda v: None) == value(NEXT)
    # That reply named no feature: the next echo is the full identities again.
    third = owner.begin("p", CONTEXT).payload()
    assert "known_reply" in third and "known_reply_digest" not in third


def test_forget_and_changed_context_drop_the_digest_with_the_snapshot():
    owner, board = ReplySnapshots(), NewBoard()
    _beat(owner, board, value())
    owner.forget("p")
    assert set(owner.begin("p", CONTEXT).payload()) == {"reply_protocol"}
    _beat(owner, board, value())
    assert set(owner.begin("p", ("replacement-client", 1)).payload()) == {"reply_protocol"}


# -- the relay: discovery path with the real request/reconstruction methods -------------------

@pytest.fixture
def discovery():
    executor = ThreadPoolExecutor(max_workers=1)
    client = SimpleNamespace(connected=True, connection_generation=1, partition_ref="test-partition")
    config = SimpleNamespace(project_id="attendance", worker_name="test-worker", worker_identity="test-viewer",
                             connection_hub_profile="test-profile", journal_workspace=lambda: None)
    adapter = ProblemBoardHostRelayAdapter(config=config, field=SimpleNamespace(), client=client,
                                           store_executor=executor)
    state = SimpleNamespace(board=NewBoard(), value={"attendances": [], "self": {"worker_name": "test-worker"},
                                                     "heartbeat_at": STAMP})

    async def action(**kwargs):
        return {"object": state.board.reply(state.value, kwargs["payload"])}

    async def account_action(payload, call):
        return await call()

    async def no_account(payload):
        pass

    client.action = action
    adapter._account_action = account_action
    adapter._add_runtime_account = no_account
    yield adapter, state
    executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_discovery_beats_use_the_digest_and_reconstruct_exactly(discovery):
    adapter, state = discovery
    seen = []
    for stamp in (STAMP, NEXT, LATER):
        state.value["heartbeat_at"] = stamp
        result, _ = await adapter._heartbeat_with_republish({"availability": "available"}, materialize=seen.append)
        assert result == state.value
    payloads = state.board.payloads
    assert "known_reply_digest" not in payloads[0]
    assert all("known_reply_digest" in p and "known_reply" not in p for p in payloads[1:])
    assert seen == [dict(state.value, heartbeat_at=s) for s in (STAMP, NEXT, LATER)]


@pytest.mark.asyncio
async def test_discovery_against_an_old_board_keeps_full_identities(discovery):
    adapter, state = discovery
    state.board = OldBoard()
    for stamp in (STAMP, NEXT):
        state.value["heartbeat_at"] = stamp
        await adapter._heartbeat_with_republish({"availability": "available"}, materialize=lambda v: None)
    assert "known_reply" in state.board.payloads[1] and "known_reply_digest" not in state.board.payloads[1]


# -- the relay: project heartbeat forms ------------------------------------------------------

SESSION = {"session_id": "s1", "state": "waiting", "presence": "listening", "check_interval_seconds": 30,
           "heartbeat_at": STAMP, "last_inbox_check_at": STAMP, "limit_state": {"kind": "none"}}
READS = {"mailbox": {"store": "mailbox", "op": "pending", "range": "h", "at": STAMP, "partitions": 1,
                     "records": 3, "ms": 2, "project": "p1"},
         "journal": {"store": "journal", "op": "list", "range": "h", "at": STAMP, "partitions": 2,
                     "records": 9, "ms": 4}}


def _channel(monkeypatch, clock):
    monkeypatch.setattr(relay, "RelayConfig",
                        lambda **values: SimpleNamespace(**{**values, "journal_workspace": lambda: None}))
    config = SimpleNamespace(project_id="attendance", worker_name="worker-a", worker_identity="synthetic-viewer",
                             connection_hub_profile="synthetic-profile", reconcile_ceiling_seconds=30,
                             journal_workspace=lambda: None)
    field = SimpleNamespace(workspaces=lambda name: [], list_assignments=lambda project_id: [])
    client = SimpleNamespace(connected=True, connection_generation=1, partition_ref="synthetic-partition")
    channel = ProblemBoardHostRelayAdapter(config=config, field=field, client=client, monotonic=lambda: clock[0],
                                           worktree_observer=lambda *args, **kwargs: None)
    channel._attendance_cache.update({"initialized": True, "items": []})
    return channel


class ProjectLoop:
    """One relay cycle per call: a fresh child, the real decision, a Board answer, the real commit."""

    def __init__(self, channel, monkeypatch, board_features):
        self.channel, self.monkeypatch = channel, monkeypatch
        self.board_features = board_features
        self.reads = deepcopy(READS)
        self.payloads: list[dict] = []
        self.resend: list[str] = []

    def cycle(self, sessions, *, force=False, lose_reply=False):
        child = self.channel._attended_project_adapter("project-one")
        self.monkeypatch.setattr(relay, "last_read_summaries", lambda worker: deepcopy(self.reads))
        self.monkeypatch.setattr(relay, "observe_assignments", lambda *args, **kwargs: [])
        for name, result in {"_worker_info": ({}, False), "_alias_request": ({}, False),
                             "_workspace_report": ({}, False)}.items():
            self.monkeypatch.setattr(child, name, lambda *args, _result=result, **kwargs: _result)
        for name in ("_add_worker_info", "_add_alias_request", "_add_project_record", "_add_workspace_report"):
            self.monkeypatch.setattr(child, name, lambda *args, **kwargs: None)
        prepared = child._prepare_project_heartbeat(project_ref=PROJECT, agent_sessions=deepcopy(sessions),
                                                    force_heartbeat=force)
        if not prepared["heartbeat_sent"]:
            return None
        payload = prepared["heartbeat_payload"]
        self.payloads.append(deepcopy(payload))
        if lose_reply:
            # The Board applied it, the answer never became a result here.
            return payload
        marker = {"protocol": REPLY_PROTOCOL, "scope": SCOPE_ID, "revision": "0" * 64, "state": "full",
                  "snapshot": {}}
        if self.board_features:
            marker["features"] = sorted(self.board_features)
        if self.resend:
            marker["resend"] = list(self.resend)
        response = {"heartbeat_reply": marker}
        prepared["reply_features"] = reply_features(response)
        prepared["reply_resend"] = relay._reply_resend(response)
        child._record_project_heartbeat(PROJECT)
        child._record_session_report(project_ref=PROJECT, signature=prepared["session_signature"])
        child._assignment_files_signatures[PROJECT] = prepared["files_signature"]
        if prepared["store_reads_delta"] is not None:
            child._store_reads_signatures[PROJECT] = prepared["store_reads_signature"]
        child._record_compact_baselines(project_ref=PROJECT, prepared=prepared)
        # What the reply owner retains after this answer: the features for the next request.
        replies = child._heartbeat_replies()
        request = replies.begin(PROJECT, child._team_context())
        snapshot = {"heartbeat_at": STAMP}
        wire = encode_reply(snapshot, protocol=REPLY_PROTOCOL, scope=SCOPE)
        wire["heartbeat_reply"].update({k: v for k, v in marker.items() if k in {"features"}})
        replies.apply(request, wire, context=child._team_context(), materialize=lambda v: None)
        return payload


def _session(stamp, **changes):
    return [{**SESSION, "heartbeat_at": stamp, "last_inbox_check_at": stamp, **changes}]


def test_times_only_session_change_is_sent_as_times(monkeypatch):
    loop = ProjectLoop(_channel(monkeypatch, [0.0]), monkeypatch, REQUEST_FEATURES)
    first = loop.cycle(_session(STAMP))
    assert first["agent_sessions"][0]["heartbeat_at"] == STAMP and "agent_session_times" not in first
    second = loop.cycle(_session(NEXT))
    assert "agent_sessions" not in second
    assert second["agent_session_times"] == [{"session_id": "s1", "heartbeat_at": NEXT, "last_inbox_check_at": NEXT}]


@pytest.mark.parametrize("change", [
    {"presence": "stale"}, {"limit_state": {"kind": "limited"}}, {"state": "detached"},
    {"last_control_refs": ["work:control:one"]},
])
def test_any_other_session_change_is_the_full_projection(monkeypatch, change):
    loop = ProjectLoop(_channel(monkeypatch, [0.0]), monkeypatch, REQUEST_FEATURES)
    loop.cycle(_session(STAMP))
    second = loop.cycle(_session(NEXT, **change))
    assert "agent_session_times" not in second and second["agent_sessions"][0]["session_id"] == "s1"


def test_an_added_session_or_a_vanished_time_field_is_the_full_projection(monkeypatch):
    loop = ProjectLoop(_channel(monkeypatch, [0.0]), monkeypatch, REQUEST_FEATURES)
    loop.cycle(_session(STAMP))
    two = _session(NEXT) + [{**SESSION, "session_id": "s2"}]
    assert "agent_sessions" in loop.cycle(two)
    without = [{k: v for k, v in row.items() if k != "last_inbox_check_at"} for row in _session(LATER)]
    without.append({**SESSION, "session_id": "s2", "heartbeat_at": LATER})
    assert "agent_sessions" in loop.cycle(without)


def test_store_reads_send_only_the_stores_that_changed(monkeypatch):
    loop = ProjectLoop(_channel(monkeypatch, [0.0]), monkeypatch, REQUEST_FEATURES)
    first = loop.cycle(_session(STAMP), force=True)
    assert first["store_reads"] == READS
    loop.reads["mailbox"].update({"project": "p2", "at": NEXT, "ms": 7})
    second = loop.cycle(_session(NEXT))
    assert "store_reads" not in second
    assert second["store_reads_changed"] == {"mailbox": loop.reads["mailbox"]}
    # Telemetry alone (ms/at) is not a change, exactly as before S2b.
    loop.reads["journal"].update({"at": LATER, "ms": 99})
    third = loop.cycle(_session(LATER))
    assert "store_reads" not in third and "store_reads_changed" not in third


def test_a_store_the_board_never_acknowledged_or_a_vanished_one_sends_the_full_map(monkeypatch):
    loop = ProjectLoop(_channel(monkeypatch, [0.0]), monkeypatch, REQUEST_FEATURES)
    loop.cycle(_session(STAMP), force=True)
    loop.reads["outbox"] = {**READS["journal"], "store": "outbox"}
    second = loop.cycle(_session(NEXT))
    assert "store_reads_changed" in second and set(second["store_reads_changed"]) == {"outbox"}
    del loop.reads["journal"]
    loop.reads["mailbox"]["records"] = 4
    third = loop.cycle(_session(LATER))
    assert "store_reads" in third and "store_reads_changed" not in third


def test_old_board_gets_the_full_forms_every_time(monkeypatch):
    loop = ProjectLoop(_channel(monkeypatch, [0.0]), monkeypatch, frozenset())
    loop.cycle(_session(STAMP), force=True)
    loop.reads["mailbox"]["op"] = "lookup"
    second = loop.cycle(_session(NEXT))
    assert "agent_sessions" in second and "store_reads" in second
    assert "agent_session_times" not in second and "store_reads_changed" not in second


def test_a_compact_form_an_answering_board_did_not_accept_is_followed_by_the_full_form(monkeypatch):
    loop = ProjectLoop(_channel(monkeypatch, [0.0]), monkeypatch, REQUEST_FEATURES)
    loop.cycle(_session(STAMP), force=True)
    # Rolled back between two heartbeats: the compact forms reach an old Board.
    loop.board_features = frozenset()
    loop.reads["mailbox"]["op"] = "lookup"
    rolled = loop.cycle(_session(NEXT))
    assert "agent_session_times" in rolled and "store_reads_changed" in rolled
    channel = loop.channel
    assert ("sessions", PROJECT) not in channel._compact_baselines
    assert ("store_reads", PROJECT) not in channel._compact_baselines
    # The next heartbeat is full, with the store map the old Board missed.
    after = loop.cycle(_session(NEXT))
    assert after["agent_sessions"] and after["store_reads"] == loop.reads


def test_a_session_the_board_asks_again_is_sent_full_next(monkeypatch):
    loop = ProjectLoop(_channel(monkeypatch, [0.0]), monkeypatch, REQUEST_FEATURES)
    loop.cycle(_session(STAMP), force=True)
    loop.resend = ["agent_sessions"]
    assert "agent_session_times" in loop.cycle(_session(NEXT))
    loop.resend = []
    assert "agent_sessions" in loop.cycle(_session(NEXT))


def test_a_full_report_whose_reply_was_lost_is_never_followed_by_times_only(monkeypatch):
    """The Board may hold the unacknowledged projection; times against the older one would keep it."""

    loop = ProjectLoop(_channel(monkeypatch, [0.0]), monkeypatch, REQUEST_FEATURES)
    loop.cycle(_session(STAMP), force=True)
    loop.reads["mailbox"]["records"] = 11
    lost = loop.cycle(_session(NEXT, presence="working"), lose_reply=True)
    assert "agent_sessions" in lost and "store_reads_changed" in lost
    # Back to the acknowledged presence, only the times differ from it.
    after = loop.cycle(_session(LATER), force=True)
    assert "agent_session_times" not in after and after["agent_sessions"][0]["presence"] == "listening"


def test_a_lost_full_store_map_is_followed_by_the_full_map(monkeypatch):
    loop = ProjectLoop(_channel(monkeypatch, [0.0]), monkeypatch, REQUEST_FEATURES)
    loop.cycle(_session(STAMP), force=True)
    loop.reads["outbox"] = {**READS["journal"], "store": "outbox"}
    del loop.reads["journal"]
    assert "store_reads" in loop.cycle(_session(NEXT), lose_reply=True)
    loop.reads["mailbox"]["records"] = 12
    assert "store_reads" in loop.cycle(_session(LATER))


def test_a_lost_compact_heartbeat_is_covered_by_the_next_one(monkeypatch):
    """A compact request whose answer never arrived (the Board may or may not have applied it).

    Nothing is committed, so the same changed stores go again (the Board's merge is idempotent) and the next
    times-only report carries the newer times (the Board never moves a session back).
    """

    loop = ProjectLoop(_channel(monkeypatch, [0.0]), monkeypatch, REQUEST_FEATURES)
    loop.cycle(_session(STAMP), force=True)
    loop.reads["mailbox"]["op"] = "lookup"
    lost = loop.cycle(_session(NEXT), lose_reply=True)
    assert lost["agent_session_times"][0]["heartbeat_at"] == NEXT
    assert set(lost["store_reads_changed"]) == {"mailbox"}
    after = loop.cycle(_session(LATER))
    assert after["agent_session_times"] == [{"session_id": "s1", "heartbeat_at": LATER, "last_inbox_check_at": LATER}]
    assert after["store_reads_changed"] == {"mailbox": loop.reads["mailbox"]}
    # Once acknowledged, an unchanged store is not sent again.
    assert "store_reads_changed" not in loop.cycle(_session(STAMP.replace("23:00", "23:03")))


@pytest.mark.asyncio
async def test_a_compact_heartbeat_lost_in_the_real_poll_is_followed_by_full_forms(monkeypatch):
    """The real _poll_project_once path: a transport timeout reaches it as a DomainError.

    The relay forgets the project's reply knowledge on that error, so the next heartbeat carries no compact
    form at all: the full session projection, the full store map and the full identities.
    """

    from project_board.contract.errors import DomainError

    loop = ProjectLoop(_channel(monkeypatch, [0.0]), monkeypatch, REQUEST_FEATURES)
    loop.cycle(_session(STAMP), force=True)
    loop.reads["mailbox"]["op"] = "lookup"
    child = loop.channel._attended_project_adapter("project-one")
    monkeypatch.setattr(relay, "last_read_summaries", lambda worker: deepcopy(loop.reads))
    for name, result in {"_worker_info": ({}, False), "_alias_request": ({}, False),
                         "_workspace_report": ({}, False)}.items():
        monkeypatch.setattr(child, name, lambda *args, _result=result, **kwargs: _result)
    for name in ("_add_worker_info", "_add_alias_request", "_add_project_record", "_add_workspace_report"):
        monkeypatch.setattr(child, name, lambda *args, **kwargs: None)

    async def nothing(payload):
        return None

    async def passthrough(payload, call):
        return await call()

    sent = []

    async def timeout(**kwargs):
        sent.append(deepcopy(kwargs["payload"]))
        raise DomainError("work_data_bus_timeout", "synthetic: no answer within the deadline")

    monkeypatch.setattr(child, "_add_disk_usage", nothing)
    monkeypatch.setattr(child, "_add_runtime_account", nothing)
    monkeypatch.setattr(child, "_account_action", passthrough)
    child.client.action = timeout
    with pytest.raises(DomainError):
        await child._poll_project_once(agent_sessions=_session(NEXT), force_heartbeat=True)
    [lost] = sent
    assert "agent_session_times" in lost and "store_reads_changed" in lost
    after = loop.cycle(_session(LATER), force=True)
    assert "agent_session_times" not in after and "store_reads_changed" not in after
    assert after["agent_sessions"][0]["heartbeat_at"] == LATER and after["store_reads"] == loop.reads
    assert "known_reply_digest" not in after


def test_a_store_merge_the_board_refused_is_followed_by_the_full_map(monkeypatch):
    loop = ProjectLoop(_channel(monkeypatch, [0.0]), monkeypatch, REQUEST_FEATURES)
    loop.cycle(_session(STAMP), force=True)
    loop.reads["mailbox"]["records"] = 21
    loop.resend = ["store_reads"]
    assert "store_reads_changed" in loop.cycle(_session(NEXT))
    loop.resend = []
    loop.reads["mailbox"]["records"] = 22
    after = loop.cycle(_session(LATER))
    assert after["store_reads"] == loop.reads and "store_reads_changed" not in after


def test_invalidated_project_forgets_the_compact_baselines(monkeypatch):
    loop = ProjectLoop(_channel(monkeypatch, [0.0]), monkeypatch, REQUEST_FEATURES)
    loop.cycle(_session(STAMP), force=True)
    assert ("sessions", PROJECT) in loop.channel._compact_baselines
    loop.channel._invalidate_cached_project(PROJECT)
    assert ("sessions", PROJECT) not in loop.channel._compact_baselines
    assert ("store_reads", PROJECT) not in loop.channel._compact_baselines


def test_children_share_the_channels_compact_baselines(monkeypatch):
    channel = _channel(monkeypatch, [0.0])
    assert channel._attended_project_adapter("project-one")._compact_baselines is channel._compact_baselines


def test_w875_pacing_is_the_same_with_and_without_compact_forms(monkeypatch):
    """When a heartbeat goes is decided before the form is chosen: same beats either way."""

    def run(features):
        clock = [0.0]
        loop = ProjectLoop(_channel(monkeypatch, clock), monkeypatch, features)
        sent, last = [], STAMP
        for step in range(25):
            clock[0] = step * 5.0
            # A session that touches its times every 15 s, a store read every 20 s.
            if step % 3 == 0:
                last = f"2026-10-16T23:{step // 12:02d}:{(step * 5) % 60:02d}.000000Z"
            sessions = _session(last)
            if step % 4 == 0:
                loop.reads["mailbox"]["records"] = step
            if loop.cycle(sessions) is not None:
                sent.append(clock[0])
        return sent, loop.payloads

    full_beats, full_payloads = run(frozenset())
    compact_beats, compact_payloads = run(REQUEST_FEATURES)
    assert full_beats == compact_beats
    assert len(full_beats) > 3
    assert any("agent_session_times" in p for p in compact_payloads)
    assert sum(len(canonical_bytes(p)) for p in compact_payloads) < sum(len(canonical_bytes(p)) for p in full_payloads)

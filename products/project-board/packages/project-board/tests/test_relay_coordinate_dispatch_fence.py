"""A coordinate request is dispatched only by the channel's current session and Card (W461 review).

PR 440 moved ``mark_attempt`` off the loop, which put an await between the
drain-entry fence and the governed dispatch. Infra's probes (2026-10-02)
held ``mark_attempt`` and, meanwhile, replaced the Card, closed the session
or replaced it: the old client still carried the request. The drain now
checks the session, its closing flag and the Card on the loop right before
the dispatch, and returns a fenced request to pending under its transport id.
The refusal path's ``queue.fail`` runs in the store thread too.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from loop_guard import LoopGuard
from project_board.client import coordinate_queue
from project_board.contract.errors import DomainError
from relay_helpers import StableClient, make_host, make_supervisor, submit_request
from test_relay_coordinate_beside_cycle import _bound_session, _write_profile


def test_a_refused_operation_is_answered_without_store_work_on_the_loop(tmp_path, monkeypatch):
    host, _, channel = make_host(tmp_path)
    queue = coordinate_queue.CoordinateQueue(host.field_root)

    class RefusingClient(StableClient):
        async def action_with_transport_identity(self, **kwargs):
            raise DomainError("synthetic_refused", "Synthetic test refusal", status=403)

    supervisor = make_supervisor(host)
    supervisor._sessions[channel.worker_name] = _bound_session(host, channel, supervisor, RefusingClient())
    request = submit_request(queue, channel)
    guard = LoopGuard(monkeypatch).install()

    async def scenario():
        await supervisor.serve_coordinate_pass()
        await asyncio.gather(*supervisor._coordinate_draining.values())

    try:
        asyncio.run(scenario())
        response = queue.take_response(worker_name=channel.worker_name, request_id=request["request_id"])
        assert response is not None and not response.get("ok")
        assert guard.report() == [], guard.report()
    finally:
        supervisor._store_executors.shutdown()


@pytest.mark.parametrize("change", ["card", "closing", "replacement"])
def test_a_change_while_the_attempt_is_marked_keeps_the_old_session_from_dispatching(
    tmp_path, monkeypatch, change
):
    host, _, channel = make_host(tmp_path)
    queue = coordinate_queue.CoordinateQueue(host.field_root)
    supervisor = make_supervisor(host)
    old_client = StableClient()
    session = _bound_session(host, channel, supervisor, old_client)
    supervisor._sessions[channel.worker_name] = session
    request = submit_request(queue, channel)
    entered = threading.Event()
    release = threading.Event()
    original = coordinate_queue.CoordinateQueue.mark_attempt

    def paused_mark(self, row):
        result = original(self, row)
        entered.set()
        assert release.wait(3), "test gate was not released"
        return result

    monkeypatch.setattr(coordinate_queue.CoordinateQueue, "mark_attempt", paused_mark)
    new_client = StableClient()

    async def scenario():
        await supervisor.serve_coordinate_pass()
        drains = list(supervisor._coordinate_draining.values())
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), 2.5)
        if change == "card":
            _write_profile(host, channel, access_id="synthetic-replaced-card", updated_at="later")
            assert supervisor._card_fingerprint(host, channel) != session.card_fingerprint
        elif change == "closing":
            session.closing = True
        else:
            supervisor._sessions[channel.worker_name] = _bound_session(host, channel, supervisor, new_client)
        release.set()
        await asyncio.gather(*drains)

    try:
        asyncio.run(scenario())
        assert old_client.calls == [], f"the old session dispatched after {change}"
        assert queue.take_response(worker_name=channel.worker_name, request_id=request["request_id"]) is None
        assert queue.holds(worker_name=channel.worker_name, request_id=request["request_id"]), (
            "the fenced request is kept for the current session"
        )
    finally:
        release.set()
        supervisor._store_executors.shutdown()


def test_a_fenced_request_is_carried_by_the_replacement_session(tmp_path, monkeypatch):
    host, _, channel = make_host(tmp_path)
    queue = coordinate_queue.CoordinateQueue(host.field_root)
    supervisor = make_supervisor(host)
    old_client = StableClient()
    session = _bound_session(host, channel, supervisor, old_client)
    supervisor._sessions[channel.worker_name] = session
    request = submit_request(queue, channel)
    new_client = StableClient()
    original = coordinate_queue.CoordinateQueue.mark_attempt
    replaced = []

    def mark_then_replace(self, row):
        result = original(self, row)
        if not replaced:
            replaced.append(True)
            supervisor._sessions[channel.worker_name] = _bound_session(host, channel, supervisor, new_client)
        return result

    monkeypatch.setattr(coordinate_queue.CoordinateQueue, "mark_attempt", mark_then_replace)

    async def scenario():
        await supervisor.serve_coordinate_pass()
        await asyncio.gather(*supervisor._coordinate_draining.values())
        await asyncio.sleep(0.4)  # past the deferred request's not_before
        await supervisor.serve_coordinate_pass()
        await asyncio.gather(*supervisor._coordinate_draining.values())

    try:
        asyncio.run(scenario())
        assert old_client.calls == []
        assert len(new_client.calls) == 1, "the current session carried the request"
        response = queue.take_response(worker_name=channel.worker_name, request_id=request["request_id"])
        assert response is not None and response.get("ok")
    finally:
        supervisor._store_executors.shutdown()

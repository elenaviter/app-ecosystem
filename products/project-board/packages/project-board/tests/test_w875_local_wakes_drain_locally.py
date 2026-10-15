"""W875 (A4): local work the side servers will serve no longer wakes every channel's full turn.

Measured on spark1 (2026-10-15 19:45:15-19:45:24Z): three ``pb coordinate`` requests from one agent were each
followed by full turns of every channel, seven codex-app turns within 150 ms, and each turn sends two
``control.pull`` to the board. The outbox and coordinate servers (W316, W459) already serve those rows beside the
cycle, but only for a worker with an open, non-closing session. So the exclusion is per worker: a served worker's
outbox row or coordinate request does not end the wait; any other worker's still does (the cycle opens its session
and drains it), and so do operator responses, listener state, relay faults, and a change in who is served.

Operator, 2026-10-15 (W875), verbatim: "this pulling is wrong. we have data bus. why its pull if this must be push."
"""

from __future__ import annotations

import asyncio
import time

from project_board.client import coordinate_queue, relay
from project_board.client.outbox_store import OutboxStore

from relay_helpers import make_host, submit_request
from test_relay_outbox_beside_cycle import _row


def _supervisor(tmp_path):
    host, _identity, channel = make_host(tmp_path)
    supervisor = relay.ProblemBoardRelaySupervisor(
        config_path=host.path,
        connector=lambda *_args, **_kwargs: None,
    )
    return host, channel, supervisor


def _wait(supervisor, host, channel, timeout, served):
    return supervisor._wait_for_local_work(
        host.field_root, timeout, worker_names=[channel.worker_name], served_workers=served,
    )


def test_a_served_workers_outbox_row_does_not_wake_the_cycle(tmp_path):
    host, channel, supervisor = _supervisor(tmp_path)
    outbox = OutboxStore(host.field_root / ".problem-board")
    served = lambda: frozenset({channel.worker_name})  # noqa: E731

    async def scenario():
        waiting = asyncio.create_task(_wait(supervisor, host, channel, 0.8, served))
        await asyncio.sleep(0.3)
        outbox.write_pending(_row(channel.worker_name, suffix="served-beside"))
        return await waiting

    started = time.monotonic()
    assert asyncio.run(scenario()) is False
    assert time.monotonic() - started >= 0.7  # ran to its timeout: the row did not end the wait


def test_a_served_workers_coordinate_request_does_not_wake_the_cycle(tmp_path):
    host, channel, supervisor = _supervisor(tmp_path)
    queue = coordinate_queue.CoordinateQueue(host.field_root)
    served = lambda: frozenset({channel.worker_name})  # noqa: E731

    async def scenario():
        waiting = asyncio.create_task(_wait(supervisor, host, channel, 0.8, served))
        await asyncio.sleep(0.3)
        submit_request(queue, channel)
        return await waiting

    assert asyncio.run(scenario()) is False


def test_an_unserved_workers_outbox_row_still_wakes_the_cycle(tmp_path):
    """op-assist, PR #820: the servers skip a worker with no open session; its row must still wake the cycle."""

    host, channel, supervisor = _supervisor(tmp_path)
    outbox = OutboxStore(host.field_root / ".problem-board")
    served = lambda: frozenset({"another-worker-with-a-session"})  # noqa: E731

    async def scenario():
        waiting = asyncio.create_task(_wait(supervisor, host, channel, 5, served))
        await asyncio.sleep(0.3)
        outbox.write_pending(_row(channel.worker_name, suffix="no-session"))
        return await waiting

    started = time.monotonic()
    assert asyncio.run(scenario()) is True
    assert time.monotonic() - started < 2


def test_an_unserved_workers_coordinate_request_still_wakes_the_cycle(tmp_path):
    host, channel, supervisor = _supervisor(tmp_path)
    queue = coordinate_queue.CoordinateQueue(host.field_root)
    served = lambda: frozenset({"another-worker-with-a-session"})  # noqa: E731

    async def scenario():
        waiting = asyncio.create_task(_wait(supervisor, host, channel, 5, served))
        await asyncio.sleep(0.3)
        submit_request(queue, channel)
        return await waiting

    assert asyncio.run(scenario()) is True


def test_an_operator_response_still_wakes_the_cycle(tmp_path):
    host, channel, supervisor = _supervisor(tmp_path)
    responses = host.field_root / ".problem-board" / "operator-responses"
    responses.mkdir(parents=True, exist_ok=True)
    served = lambda: frozenset({channel.worker_name})  # noqa: E731

    async def scenario():
        waiting = asyncio.create_task(_wait(supervisor, host, channel, 5, served))
        await asyncio.sleep(0.3)
        (responses / "reply.json").write_text("{}", encoding="utf-8")
        return await waiting

    assert asyncio.run(scenario()) is True


def test_a_change_in_who_is_served_wakes_the_cycle(tmp_path):
    """op-assist, PR #820: a side server that stops mid-wait (or a session that closes) is noticed in the loop."""

    host, channel, supervisor = _supervisor(tmp_path)
    state = {"served": frozenset({channel.worker_name})}

    async def scenario():
        waiting = asyncio.create_task(_wait(supervisor, host, channel, 5, lambda: state["served"]))
        await asyncio.sleep(0.3)
        state["served"] = frozenset()  # e.g. the outbox server stopped
        return await waiting

    started = time.monotonic()
    assert asyncio.run(scenario()) is True
    assert time.monotonic() - started < 2


def test_with_no_served_worker_the_old_wake_is_unchanged(tmp_path):
    host, channel, supervisor = _supervisor(tmp_path)
    outbox = OutboxStore(host.field_root / ".problem-board")

    async def scenario():
        waiting = asyncio.create_task(_wait(supervisor, host, channel, 5, lambda: frozenset()))
        await asyncio.sleep(0.3)
        outbox.write_pending(_row(channel.worker_name, suffix="no-server"))
        return await waiting

    assert asyncio.run(scenario()) is True


def test_the_signature_leaves_out_only_served_workers_outbox_and_coordinate(tmp_path):
    host, channel, supervisor = _supervisor(tmp_path)
    full = supervisor._local_work_signature(host.field_root, worker_names=[channel.worker_name])
    served = supervisor._local_work_signature(
        host.field_root, worker_names=[channel.worker_name], served_workers=frozenset({channel.worker_name})
    )
    kinds_full = {entry[0] for entry in full}
    kinds_served = {entry[0] for entry in served}
    assert {"outbox-ready", "coordinate"} <= kinds_full
    assert kinds_served == kinds_full - {"outbox-ready", "coordinate"}
    other = supervisor._local_work_signature(
        host.field_root, worker_names=[channel.worker_name], served_workers=frozenset({"someone-else"})
    )
    assert {entry[0] for entry in other} == kinds_full


def test_served_workers_need_both_servers_and_an_open_session(tmp_path):
    _host, channel, supervisor = _supervisor(tmp_path)

    class Session:
        closing = False

    async def scenario():
        supervisor._sessions[channel.worker_name] = Session()
        assert supervisor._side_served_workers() == frozenset()  # no server running
        supervisor._ensure_coordinate_server()
        supervisor._ensure_outbox_server()
        try:
            assert supervisor._side_served_workers() == frozenset({channel.worker_name})
            supervisor._sessions[channel.worker_name].closing = True
            assert supervisor._side_served_workers() == frozenset()
        finally:
            await supervisor.stop_coordinate_server()
            await supervisor.stop_outbox_server()
            supervisor._sessions.clear()

    asyncio.run(scenario())


def test_one_server_down_serves_no_worker(tmp_path):
    """claude-ops, PR #820 R2: with the outbox server up and the coordinate server down nobody is side-served."""

    _host, channel, supervisor = _supervisor(tmp_path)

    class Session:
        closing = False

    async def scenario():
        supervisor._sessions[channel.worker_name] = Session()
        supervisor._ensure_outbox_server()
        try:
            assert supervisor._side_served_workers() == frozenset()
        finally:
            await supervisor.stop_outbox_server()
            supervisor._sessions.clear()
        supervisor._ensure_coordinate_server()
        try:
            assert supervisor._side_served_workers() == frozenset()
        finally:
            await supervisor.stop_coordinate_server()

    asyncio.run(scenario())

"""W875 (A4): local work served beside the cycle no longer wakes every channel's full turn.

Measured on spark1 (2026-10-15 19:45:15-19:45:24Z): three ``pb coordinate`` requests from one agent were each
followed by full turns of every channel, seven codex-app turns within 150 ms, and each turn sends two
``control.pull`` to the board. The outbox and coordinate servers (W316, W459) already serve those rows beside the
cycle as they arrive, so while both run, a new outbox row or coordinate request is not a reason to wake the cycle.
Operator responses, listener state and relay faults still wake it, and with the servers down the old wake stays.

Operator, 2026-10-15 (W875), verbatim: "this pulling is wrong. we have data bus. why its pull if this must be push."
"""

from __future__ import annotations

import asyncio
import time

from project_board.client import relay
from project_board.client.outbox_store import OutboxStore

from relay_helpers import make_host
from test_relay_outbox_beside_cycle import _row


def _supervisor(tmp_path):
    host, _identity, channel = make_host(tmp_path)
    supervisor = relay.ProblemBoardRelaySupervisor(
        config_path=host.path,
        connector=lambda *_args, **_kwargs: None,
    )
    return host, channel, supervisor


def test_while_the_side_servers_run_a_new_outbox_row_does_not_wake_the_cycle(tmp_path):
    host, channel, supervisor = _supervisor(tmp_path)
    outbox = OutboxStore(host.field_root / ".problem-board")

    async def scenario():
        waiting = asyncio.create_task(supervisor._wait_for_local_work(
            host.field_root, 0.8, worker_names=[channel.worker_name], side_served=True,
        ))
        await asyncio.sleep(0.3)
        outbox.write_pending(_row(channel.worker_name, suffix="served-beside"))
        return await waiting

    started = time.monotonic()
    assert asyncio.run(scenario()) is False
    assert time.monotonic() - started >= 0.7  # ran to its timeout: the row did not end the wait


def test_while_the_side_servers_run_an_operator_response_still_wakes_the_cycle(tmp_path):
    host, channel, supervisor = _supervisor(tmp_path)
    responses = host.field_root / ".problem-board" / "operator-responses"
    responses.mkdir(parents=True, exist_ok=True)

    async def scenario():
        waiting = asyncio.create_task(supervisor._wait_for_local_work(
            host.field_root, 5, worker_names=[channel.worker_name], side_served=True,
        ))
        await asyncio.sleep(0.3)
        (responses / "reply.json").write_text("{}", encoding="utf-8")
        return await waiting

    started = time.monotonic()
    assert asyncio.run(scenario()) is True
    assert time.monotonic() - started < 2


def test_with_the_side_servers_down_an_outbox_row_still_wakes_the_cycle(tmp_path):
    host, channel, supervisor = _supervisor(tmp_path)
    outbox = OutboxStore(host.field_root / ".problem-board")

    async def scenario():
        waiting = asyncio.create_task(supervisor._wait_for_local_work(
            host.field_root, 5, worker_names=[channel.worker_name], side_served=False,
        ))
        await asyncio.sleep(0.3)
        outbox.write_pending(_row(channel.worker_name, suffix="no-server"))
        return await waiting

    assert asyncio.run(scenario()) is True


def test_the_side_served_signature_leaves_out_outbox_and_coordinate_only(tmp_path):
    host, channel, supervisor = _supervisor(tmp_path)
    full = supervisor._local_work_signature(host.field_root, worker_names=[channel.worker_name])
    served = supervisor._local_work_signature(
        host.field_root, worker_names=[channel.worker_name], side_served=True
    )
    kinds_full = {entry[0] for entry in full}
    kinds_served = {entry[0] for entry in served}
    assert {"outbox-ready", "coordinate"} <= kinds_full
    assert kinds_served == kinds_full - {"outbox-ready", "coordinate"}
    assert {"operator-responses", "relay-faults"} <= kinds_served


def test_side_servers_running_needs_both_servers(tmp_path):
    _host, _channel, supervisor = _supervisor(tmp_path)

    async def scenario():
        assert supervisor._side_servers_running() is False
        supervisor._ensure_coordinate_server()
        assert supervisor._side_servers_running() is False  # outbox server not yet up
        supervisor._ensure_outbox_server()
        try:
            assert supervisor._side_servers_running() is True
        finally:
            await supervisor.stop_coordinate_server()
            await supervisor.stop_outbox_server()
        assert supervisor._side_servers_running() is False

    asyncio.run(scenario())

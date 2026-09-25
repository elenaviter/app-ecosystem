"""A durable outbox write wakes a bounded drain beside the relay cycle (W316)."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from project_board.client import local_wake, relay
from project_board.client.io import new_id, utc_now
from project_board.client.outbox_store import OutboxStore
from project_board.client.store import SharedFieldStore

from relay_helpers import make_host


PROJECT_REF = "work:project:one"


class _OutboxClient:
    connected = True

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.called = asyncio.Event()

    async def action(self, **kwargs):
        self.calls.append(dict(kwargs))
        self.called.set()
        return {"object": {"ref": f"work:message:{len(self.calls)}"}}


def _row(worker_name: str, *, suffix: str, next_attempt_at: str = "") -> dict:
    return {
        "outbox_id": new_id(f"outbox_{suffix}"),
        "kind": "mail.route",
        "worker_name": worker_name,
        "project_ref": PROJECT_REF,
        "object_ref": PROJECT_REF,
        "payload": {
            "recipient": "operator",
            "kind": "update",
            "subject": suffix,
            "body": "Sent beside the cycle.",
        },
        "state": "pending",
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "next_attempt_at": next_attempt_at,
    }


def _bind_session(host, channel, supervisor, client):
    adapter = relay.ProblemBoardHostRelayAdapter(
        config=relay.RelayConfig.from_host_channel(
            host,
            channel,
            project_id="attendance",
        ),
        field=SharedFieldStore(host.field_root),
        client=client,
        trace=supervisor._trace,
        outbox_drain_lock=supervisor._outbox_drain_lock(channel.worker_name),
    )
    session = SimpleNamespace(
        adapter=adapter,
        closing=False,
        close_failure=None,
        profile=channel.profile,
        worker_name=channel.worker_name,
        channel_identity=channel.worker_identity,
        replacement_epoch=1,
        card_fingerprint="test-card",
    )
    supervisor._sessions[channel.worker_name] = session
    supervisor._session_matches = lambda *_args, **_kwargs: True
    return session


async def _wait_for_socket(path, *, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not path.exists() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.005)
    assert path.exists(), f"relay did not bind {path}"


async def _wait_for_calls(client, count: int, *, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while len(client.calls) < count and asyncio.get_running_loop().time() < deadline:
        client.called.clear()
        try:
            await asyncio.wait_for(
                client.called.wait(),
                timeout=max(0.001, deadline - asyncio.get_running_loop().time()),
            )
        except asyncio.TimeoutError:
            break
    assert len(client.calls) >= count


def test_partitioned_ready_row_is_visible_before_the_wait_baseline(tmp_path):
    host, _identity, channel = make_host(tmp_path)
    supervisor = relay.ProblemBoardRelaySupervisor(
        config_path=host.path,
        connector=lambda *_args, **_kwargs: None,
    )
    outbox = OutboxStore(host.field_root / ".problem-board")
    before = supervisor._local_work_signature(
        host.field_root,
        worker_names=[channel.worker_name],
    )

    outbox.write_pending(_row(channel.worker_name, suffix="already-ready"))

    after = supervisor._local_work_signature(
        host.field_root,
        worker_names=[channel.worker_name],
    )
    assert after != before

    async def scenario():
        started = time.monotonic()
        ready = await supervisor._wait_for_local_work(
            host.field_root,
            30,
            worker_names=[channel.worker_name],
        )
        assert ready is True
        assert time.monotonic() - started < 0.1
        # Pacing or a reconnect may leave the same row pending. It wakes one
        # cycle, then becomes the baseline instead of creating a busy loop.
        assert not await supervisor._wait_for_local_work(
            host.field_root,
            0.02,
            worker_names=[channel.worker_name],
        )

    asyncio.run(scenario())


def test_sender_before_relay_and_relay_before_sender_each_send_once(tmp_path):
    host, _identity, channel = make_host(tmp_path)
    supervisor = relay.ProblemBoardRelaySupervisor(
        config_path=host.path,
        connector=lambda *_args, **_kwargs: None,
    )
    client = _OutboxClient()
    _bind_session(host, channel, supervisor, client)
    outbox = OutboxStore(host.field_root / ".problem-board")
    outbox.write_pending(_row(channel.worker_name, suffix="before-relay"))

    async def scenario():
        # The socket did not exist for the first write. Binding precedes the
        # startup probe, so the durable row still leaves immediately.
        supervisor._ensure_outbox_server()
        await _wait_for_calls(client, 1)
        await supervisor.stop_outbox_server()

        # Restarting the relay does not replay the settled first row.
        supervisor._ensure_outbox_server()
        endpoint = local_wake.relay_outbox_wake_socket_path(host.field_root)
        await _wait_for_socket(endpoint)
        await asyncio.sleep(0.05)
        assert len(client.calls) == 1

        started = time.monotonic()
        outbox.write_pending(_row(channel.worker_name, suffix="after-relay"))
        await _wait_for_calls(client, 2)
        assert time.monotonic() - started < 1.0
        await supervisor.stop_outbox_server()

    asyncio.run(scenario())
    assert len(client.calls) == 2


def test_event_loop_cancellation_releases_the_listener_thread(tmp_path, monkeypatch):
    host, _identity, _channel = make_host(tmp_path)
    supervisor = relay.ProblemBoardRelaySupervisor(
        config_path=host.path,
        connector=lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(supervisor._outbox_server, "SAFETY_PROBE_SECONDS", 2.0)

    async def scenario():
        supervisor._ensure_outbox_server()
        await _wait_for_socket(
            local_wake.relay_outbox_wake_socket_path(host.field_root)
        )
        server = supervisor._outbox_server.task
        assert server is not None
        server.cancel()
        await asyncio.gather(server, return_exceptions=True)

    started = time.monotonic()
    asyncio.run(scenario())
    assert time.monotonic() - started < 1.0


def test_outbox_leaves_while_an_attendance_step_is_blocked(tmp_path):
    host, _identity, channel = make_host(tmp_path)
    supervisor = relay.ProblemBoardRelaySupervisor(
        config_path=host.path,
        connector=lambda *_args, **_kwargs: None,
    )
    client = _OutboxClient()
    _bind_session(host, channel, supervisor, client)
    outbox = OutboxStore(host.field_root / ".problem-board")

    async def scenario():
        blocked = asyncio.Event()
        attendance = asyncio.create_task(blocked.wait())
        supervisor._ensure_outbox_server()
        await _wait_for_socket(
            local_wake.relay_outbox_wake_socket_path(host.field_root)
        )

        started = time.monotonic()
        outbox.write_pending(_row(channel.worker_name, suffix="during-attendance"))
        await _wait_for_calls(client, 1)
        assert time.monotonic() - started < 1.0
        assert not attendance.done()

        blocked.set()
        await attendance
        await supervisor.stop_outbox_server()

    asyncio.run(scenario())


def test_write_during_an_active_side_drain_leaves_without_the_safety_probe(
    tmp_path,
):
    host, _identity, channel = make_host(tmp_path)
    supervisor = relay.ProblemBoardRelaySupervisor(
        config_path=host.path,
        connector=lambda *_args, **_kwargs: None,
    )
    outbox = OutboxStore(host.field_root / ".problem-board")

    class _BlockingFirstClient(_OutboxClient):
        def __init__(self) -> None:
            super().__init__()
            self.first_started = asyncio.Event()
            self.release_first = asyncio.Event()

        async def action(self, **kwargs):
            if not self.calls:
                self.first_started.set()
                await self.release_first.wait()
            return await super().action(**kwargs)

    client = _BlockingFirstClient()
    _bind_session(host, channel, supervisor, client)

    async def scenario():
        supervisor._ensure_outbox_server()
        await _wait_for_socket(
            local_wake.relay_outbox_wake_socket_path(host.field_root)
        )
        try:
            outbox.write_pending(_row(channel.worker_name, suffix="first"))
            await client.first_started.wait()

            outbox.write_pending(_row(channel.worker_name, suffix="second"))
            # Let the server consume this wake while the first drain still owns
            # the worker. Drain completion must arrange the follow-up pass.
            await asyncio.sleep(0.05)
            client.release_first.set()

            await _wait_for_calls(client, 2, timeout=0.5)
        finally:
            client.release_first.set()
            await supervisor.stop_outbox_server()

    asyncio.run(scenario())
    assert len(client.calls) == 2


def test_twenty_row_batch_wakes_the_server_for_its_remainder(tmp_path):
    host, _identity, channel = make_host(tmp_path)
    supervisor = relay.ProblemBoardRelaySupervisor(
        config_path=host.path,
        connector=lambda *_args, **_kwargs: None,
    )
    client = _OutboxClient()
    _bind_session(host, channel, supervisor, client)
    outbox = OutboxStore(host.field_root / ".problem-board")

    # No listener exists yet, so startup has only the durable rows to inspect.
    # One flush claims 20; completion must wake a second bounded flush for row 21.
    for index in range(21):
        outbox.write_pending(
            _row(channel.worker_name, suffix=f"bounded-{index:02d}")
        )

    async def scenario():
        supervisor._ensure_outbox_server()
        try:
            await _wait_for_calls(client, 21, timeout=0.5)
        finally:
            await supervisor.stop_outbox_server()

    asyncio.run(scenario())
    assert len(client.calls) == 21


def test_cycle_and_side_drain_serialize_one_worker_card(tmp_path):
    host, _identity, channel = make_host(tmp_path)
    supervisor = relay.ProblemBoardRelaySupervisor(
        config_path=host.path,
        connector=lambda *_args, **_kwargs: None,
    )
    outbox = OutboxStore(host.field_root / ".problem-board")
    active = {"now": 0, "max": 0}
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class _SlowFirstClient(_OutboxClient):
        async def action(self, **kwargs):
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
            try:
                if not self.calls:
                    first_started.set()
                    await release_first.wait()
                return await super().action(**kwargs)
            finally:
                active["now"] -= 1

    client = _SlowFirstClient()
    session = _bind_session(host, channel, supervisor, client)
    outbox.write_pending(_row(channel.worker_name, suffix="side"))

    async def scenario():
        assert supervisor.serve_outbox_once() == [channel.worker_name]
        side = list(supervisor._outbox_draining.values())
        await first_started.wait()

        outbox.write_pending(_row(channel.worker_name, suffix="cycle"))
        cycle = asyncio.create_task(
            session.adapter._flush_outbox(project_ref=PROJECT_REF)
        )
        for _ in range(5):
            await asyncio.sleep(0)
        assert not cycle.done()
        assert active == {"now": 1, "max": 1}

        release_first.set()
        await asyncio.gather(*side, cycle)

    asyncio.run(scenario())
    assert len(client.calls) == 2
    assert active["max"] == 1


def test_retry_backoff_row_is_not_ready_for_the_side_drain(tmp_path):
    host, _identity, channel = make_host(tmp_path)
    outbox = OutboxStore(host.field_root / ".problem-board")
    future = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()

    outbox.write_pending(
        _row(channel.worker_name, suffix="backoff", next_attempt_at=future)
    )

    assert not outbox.has_ready_work(worker_names=[channel.worker_name])
    assert outbox.ready_project_refs(worker_name=channel.worker_name) == []


def test_project_level_row_waits_for_an_attended_project_card(tmp_path):
    host, _identity, channel = make_host(tmp_path)
    outbox = OutboxStore(host.field_root / ".problem-board")

    outbox.write_pending(_row("", suffix="project-level"))

    # It wakes the ordinary attendance cycle, but the side server cannot pick
    # an arbitrary active worker Card for a row with no worker owner.
    assert outbox.has_ready_work(worker_names=[channel.worker_name])
    assert outbox.ready_project_refs(worker_name=channel.worker_name) == []

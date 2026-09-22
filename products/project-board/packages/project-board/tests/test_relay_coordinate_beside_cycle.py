"""A pb coordinate request is served beside the relay's channel cycle (W267).

2026-09-22: codex-main's project.plan.item waited 38.5 s in the local queue
for a governed action that took about 1 s, because the relay drained the queue
only inside the channel cycle, and that cycle was waiting on other work after
a reload.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from contextlib import asynccontextmanager

from project_board.client import coordinate_queue, relay

from relay_helpers import (
    StableClient as _StableClient,
    make_host as _host,
    make_supervisor as _supervisor,
    submit_request as _submit,
)


def _session(channel, client, **overrides):
    closed: list[str] = []

    async def aclose():
        closed.append("closed")

    values = {
        "adapter": SimpleNamespace(client=client),
        "closing": False,
        "close_failure": None,
        "profile": channel.profile,
        "worker_name": channel.worker_name,
        "channel_identity": channel.worker_identity,
        "replacement_epoch": 1,
        "card_fingerprint": "",
        "closed": closed,
        "aclose": aclose,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _bound_session(host, channel, supervisor, client, **overrides):
    """A session opened for the Card the channel's profile holds now."""

    _write_profile(host, channel, access_id="oauth-card-current", updated_at="t0")
    return _session(
        channel,
        client,
        card_fingerprint=supervisor._card_fingerprint(host, channel),
        **overrides,
    )


def test_a_request_completes_while_the_channel_cycle_is_blocked(tmp_path):
    host, _identity, channel = _host(tmp_path)
    queue = coordinate_queue.CoordinateQueue(host.field_root)
    client = _StableClient()
    supervisor = _supervisor(host)
    supervisor._sessions[channel.worker_name] = _bound_session(
        host, channel, supervisor, client
    )

    async def scenario():
        blocked_cycle = asyncio.Event()

        async def slow_cycle_step():
            # Another channel waiting out a Data Bus timeout, or a reload.
            await blocked_cycle.wait()

        cycle = asyncio.create_task(slow_cycle_step())
        request = _submit(queue, channel)
        started = supervisor.serve_coordinate_once()
        assert started == [channel.worker_name]
        await asyncio.gather(*supervisor._coordinate_draining.values())

        response = queue.take_response(
            worker_name=channel.worker_name, request_id=request["request_id"]
        )
        assert response is not None and response["ok"] is True
        assert not cycle.done(), "the request finished while the cycle was still blocked"
        blocked_cycle.set()
        await cycle

    asyncio.run(scenario())


def test_a_worker_without_an_open_channel_is_left_to_the_cycle(tmp_path):
    host, _identity, channel = _host(tmp_path)
    queue = coordinate_queue.CoordinateQueue(host.field_root)
    supervisor = _supervisor(host)

    async def scenario():
        _submit(queue, channel)
        assert supervisor.serve_coordinate_once() == []

    asyncio.run(scenario())


def test_one_worker_is_drained_once_at_a_time(tmp_path):
    host, _identity, channel = _host(tmp_path)
    queue = coordinate_queue.CoordinateQueue(host.field_root)
    release = asyncio.Event()

    class _SlowClient(_StableClient):
        async def action_with_transport_identity(self, **kwargs):
            await release.wait()
            return await super().action_with_transport_identity(**kwargs)

    supervisor = _supervisor(host)
    supervisor._sessions[channel.worker_name] = _bound_session(
        host, channel, supervisor, _SlowClient()
    )

    async def scenario():
        _submit(queue, channel)
        assert supervisor.serve_coordinate_once() == [channel.worker_name]
        await asyncio.sleep(0)
        _submit(queue, channel)
        assert supervisor.serve_coordinate_once() == [], "a drain is already running"
        release.set()
        await asyncio.gather(*supervisor._coordinate_draining.values())
        await supervisor.stop_coordinate_server()

    asyncio.run(scenario())


def test_a_session_from_a_replaced_profile_or_channel_does_not_drain(tmp_path):
    host, _identity, channel = _host(tmp_path)
    queue = coordinate_queue.CoordinateQueue(host.field_root)
    client = _StableClient()
    supervisor = _supervisor(host)

    async def scenario():
        request = _submit(queue, channel)
        for stale in (
            {"profile": "problem-board-old-profile"},
            {"channel_identity": "claude-code:replaced-session"},
        ):
            supervisor._sessions[channel.worker_name] = _session(channel, client, **stale)
            assert supervisor.serve_coordinate_once() == []
        assert client.calls == []
        assert queue.take_response(
            worker_name=channel.worker_name, request_id=request["request_id"]
        ) is None

    asyncio.run(scenario())


def _write_profile(host, channel, *, access_id, updated_at):
    root = host.connection_hub_state_root
    root.mkdir(parents=True, exist_ok=True)
    (root / "profiles.json").write_text(
        json.dumps(
            {
                "profiles": [
                    {
                        "name": channel.profile,
                        "endpoint": "https://runtime.example/mcp",
                        "credential_ref": "ref",
                        "access_id": access_id,
                        "auth_type": "oauth",
                        "record_version": 3,
                        "updated_at": updated_at,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def test_a_session_opened_for_another_card_under_the_same_profile_does_not_drain(tmp_path):
    """Same profile name and worker identity; only the bound Card changes."""

    host, _identity, channel = _host(tmp_path)
    queue = coordinate_queue.CoordinateQueue(host.field_root)
    client = _StableClient()
    supervisor = _supervisor(host)
    _write_profile(host, channel, access_id="oauth-card-old", updated_at="t1")
    opened_for = supervisor._card_fingerprint(host, channel)
    supervisor._sessions[channel.worker_name] = _session(
        channel, client, card_fingerprint=opened_for
    )

    async def scenario():
        request = _submit(queue, channel)
        _write_profile(host, channel, access_id="oauth-card-new", updated_at="t2")
        assert supervisor.serve_coordinate_once() == []
        assert client.calls == []
        assert queue.take_response(
            worker_name=channel.worker_name, request_id=request["request_id"]
        ) is None

        # A token refresh rewrites the record for the same Card: still served.
        _write_profile(host, channel, access_id="oauth-card-old", updated_at="t3")
        assert supervisor.serve_coordinate_once() == [channel.worker_name]
        await asyncio.gather(*supervisor._coordinate_draining.values())
        response = queue.take_response(
            worker_name=channel.worker_name, request_id=request["request_id"]
        )
        assert response is not None and response["ok"] is True

    asyncio.run(scenario())


def test_closing_the_relay_stops_the_coordinate_server_before_sessions_close(tmp_path):
    host, _identity, channel = _host(tmp_path)
    supervisor = _supervisor(host)
    order: list[str] = []

    async def scenario():
        supervisor._ensure_coordinate_server()
        server = supervisor._coordinate_task

        async def close_session(_name):
            order.append("session_closed" if server.done() else "session_closed_while_serving")

        supervisor._sessions[channel.worker_name] = _session(channel, _StableClient())
        supervisor._drop_session = close_session
        await supervisor.aclose()
        assert server.done()

    asyncio.run(scenario())
    assert order == ["session_closed"]


def test_an_unknown_card_never_drains_beside_the_cycle(tmp_path):
    """Fail closed: a missing or unreadable profile record is not a match."""

    host, _identity, channel = _host(tmp_path)
    queue = coordinate_queue.CoordinateQueue(host.field_root)
    client = _StableClient()
    supervisor = _supervisor(host)

    async def scenario():
        _submit(queue, channel)
        # Neither read found a Card.
        supervisor._sessions[channel.worker_name] = _session(channel, client)
        assert supervisor.serve_coordinate_once() == []
        # Bound at open, then the record became unreadable.
        supervisor._sessions[channel.worker_name] = _bound_session(
            host, channel, supervisor, client
        )
        (host.connection_hub_state_root / "profiles.json").write_text(
            "{not json", encoding="utf-8"
        )
        assert supervisor.serve_coordinate_once() == []
        assert client.calls == []

    asyncio.run(scenario())


def test_the_cycle_reopens_a_session_whose_card_was_replaced_before_draining(tmp_path):
    """The ordinary cycle applies the same exact-session check (W267 review 3)."""

    host, _identity, channel = _host(tmp_path)
    supervisor = _supervisor(host)
    stale_client = _StableClient()
    stale = _bound_session(host, channel, supervisor, stale_client)
    supervisor._sessions[channel.worker_name] = stale
    _write_profile(host, channel, access_id="oauth-card-replacement", updated_at="t1")
    fresh_client = _StableClient()
    drained_with: list[object] = []

    async def open_session(_host_config, _channel):
        async def poll_attendances_once():
            return {"ok": True}

        return _session(
            channel,
            fresh_client,
            card_fingerprint=supervisor._card_fingerprint(host, channel),
            adapter=SimpleNamespace(
                client=fresh_client, poll_attendances_once=poll_attendances_once
            ),
        )

    async def drain(_host_config, _channel, session):
        drained_with.append(session.adapter.client)
        return {"claimed": 0}

    supervisor._open_session = open_session
    supervisor._drain_coordinate_requests = drain

    async def scenario():
        await supervisor._poll_channel(host, channel)

    asyncio.run(scenario())
    assert stale.closed == ["closed"], "the stale session was closed"
    assert drained_with == [fresh_client], "the cycle drained only through the new Card"
    assert supervisor._sessions[channel.worker_name].adapter.client is fresh_client


def test_dropping_a_session_waits_for_its_drain_beside_the_cycle(tmp_path):
    """A routine replacement never closes a client a side drain is using."""

    host, _identity, channel = _host(tmp_path)
    queue = coordinate_queue.CoordinateQueue(host.field_root)
    release = asyncio.Event()
    order: list[str] = []

    class _SlowClient(_StableClient):
        async def action_with_transport_identity(self, **kwargs):
            await release.wait()
            result = await super().action_with_transport_identity(**kwargs)
            order.append("request_answered")
            return result

    supervisor = _supervisor(host)
    session = _bound_session(host, channel, supervisor, _SlowClient())

    async def aclose():
        order.append("session_closed")

    session.aclose = aclose
    supervisor._sessions[channel.worker_name] = session

    async def scenario():
        request = _submit(queue, channel)
        assert supervisor.serve_coordinate_once() == [channel.worker_name]
        await asyncio.sleep(0)
        dropping = asyncio.create_task(supervisor._drop_session(channel.worker_name))
        await asyncio.sleep(0)
        assert session.closing is True
        assert supervisor.serve_coordinate_once() == [], "no new drain on a closing session"
        release.set()
        await dropping
        response = queue.take_response(
            worker_name=channel.worker_name, request_id=request["request_id"]
        )
        assert response is not None and response["ok"] is True

    asyncio.run(scenario())
    assert order == ["request_answered", "session_closed"]


def test_a_card_replaced_while_the_channel_opens_never_drains_through_it(
    tmp_path, monkeypatch
):
    """The connector is blocked, the profile's Card is replaced during the
    await, and the session bound to the old Card is dropped before any drain
    (W267 review 4)."""

    host, _identity, channel = _host(tmp_path)
    _write_profile(host, channel, access_id="oauth-card-old", updated_at="t0")
    opening = asyncio.Event()
    release = asyncio.Event()
    clients: list[_StableClient] = []
    drained_with: list[object] = []

    @asynccontextmanager
    async def connector(_host_config, _channel, *, replacement_epoch):
        client = _StableClient()
        clients.append(client)
        if len(clients) == 1:
            opening.set()
            await release.wait()
        yield client

    supervisor = relay.ProblemBoardRelaySupervisor(
        config_path=host.path, connector=connector
    )

    async def drain(_host_config, _channel, session):
        drained_with.append(session.adapter.client)
        return {"claimed": 0}

    async def poll_attendances_once(self):
        return {"ok": True}

    supervisor._drain_coordinate_requests = drain
    monkeypatch.setattr(
        relay.ProblemBoardHostRelayAdapter, "poll_attendances_once", poll_attendances_once
    )

    async def scenario():
        cycle = asyncio.create_task(supervisor._poll_channel(host, channel))
        await opening.wait()
        # The Card is replaced while the first connection is still opening.
        _write_profile(host, channel, access_id="oauth-card-new", updated_at="t1")
        release.set()
        await cycle

    asyncio.run(scenario())
    assert len(clients) == 2, "the stale session was dropped and the channel reopened"
    assert drained_with == [clients[1]], "nothing drained through the old Card's session"
    assert supervisor._sessions[channel.worker_name].adapter.client is clients[1]
    assert supervisor._sessions[channel.worker_name].card_fingerprint == (
        supervisor._card_fingerprint(host, channel)
    )


def test_the_cycle_waits_for_a_side_drain_of_the_same_worker(tmp_path):
    """The cycle and side server serialize one worker's requests."""

    host, _identity, channel = _host(tmp_path)
    queue = coordinate_queue.CoordinateQueue(host.field_root)
    release = asyncio.Event()
    active = {"now": 0, "max": 0}
    order: list[str] = []

    class _SlowFirstClient(_StableClient):
        started = 0

        async def action_with_transport_identity(self, **kwargs):
            _SlowFirstClient.started += 1
            first = _SlowFirstClient.started == 1
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
            try:
                if first:
                    await release.wait()
                result = await super().action_with_transport_identity(**kwargs)
                order.append(kwargs["object_ref"])
                return result
            finally:
                active["now"] -= 1

    supervisor = _supervisor(host)
    client = _SlowFirstClient()

    async def poll_attendances_once():
        return {"ok": True}

    supervisor._sessions[channel.worker_name] = _bound_session(
        host,
        channel,
        supervisor,
        client,
        adapter=SimpleNamespace(
            client=client,
            poll_attendances_once=poll_attendances_once,
        ),
    )

    async def scenario():
        earlier = _submit(queue, channel, object_ref="work:project:earlier")
        assert supervisor.serve_coordinate_once() == [channel.worker_name]
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert active["now"] == 1
        later = _submit(queue, channel, object_ref="work:project:later")
        cycle = asyncio.create_task(supervisor._poll_channel(host, channel))
        for _ in range(5):
            await asyncio.sleep(0)
        assert not cycle.done()
        assert active["max"] == 1
        assert order == []
        assert supervisor.serve_coordinate_once() == []
        release.set()
        await cycle
        await asyncio.gather(*supervisor._coordinate_draining.values())
        await supervisor.stop_coordinate_server()
        for request in (earlier, later):
            response = queue.take_response(
                worker_name=channel.worker_name,
                request_id=request["request_id"],
            )
            assert response is not None and response["ok"] is True

    asyncio.run(scenario())
    assert active["max"] == 1
    assert order == ["work:project:earlier", "work:project:later"]

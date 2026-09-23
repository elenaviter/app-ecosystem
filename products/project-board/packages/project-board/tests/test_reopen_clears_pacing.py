"""A reopened channel has no failure on record (W274).

2026-09-22: after a relay restart during a stack rebuild, the first open of a
channel failed with oauth_challenge_not_advertised and the relay recorded it
in relay-pacing.json. The relay reopened the channel at 23:57:25Z, but the
record stayed until the first completed cycle poll at 00:00:32Z, and for
those three minutes seven seconds pb status and the pb coordinate admission
read the record as "not open now" and refused an open channel. The cycle was
the only caller of record_success. Now the open itself clears the record.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from project_board.client import relay, relay_pacing

from relay_helpers import StableClient, make_host


def test_opening_a_channel_clears_its_failure_record_before_any_cycle(tmp_path):
    host, _identity, channel = make_host(tmp_path)
    opens: list[str] = []

    @asynccontextmanager
    async def connector(_host_config, opened_channel, *, replacement_epoch):
        opens.append(opened_channel.worker_name)
        yield StableClient()

    supervisor = relay.ProblemBoardRelaySupervisor(
        config_path=host.path, connector=connector
    )
    supervisor._pacing.record_failure(
        channel.worker_name, "oauth_challenge_not_advertised"
    )
    before = relay_pacing.channel_reconnect_state(host.path, channel.worker_name)
    assert before is not None
    assert before["reason"] == "oauth_challenge_not_advertised"
    assert before["attempts"] == 1

    async def scenario():
        session = await supervisor._open_session(host, channel)
        try:
            # No cycle has completed: the open alone must leave no record.
            return relay_pacing.channel_reconnect_state(
                host.path, channel.worker_name
            )
        finally:
            await session.stack.aclose()

    after = asyncio.run(scenario())
    assert opens == [channel.worker_name]
    assert after is None, "an open channel is refused while its failure record stands"


def test_a_failed_open_keeps_its_record(tmp_path):
    host, _identity, channel = make_host(tmp_path)

    @asynccontextmanager
    async def connector(_host_config, _channel, *, replacement_epoch):
        raise ConnectionError("endpoint mid-rebuild")
        yield  # pragma: no cover - never reached

    supervisor = relay.ProblemBoardRelaySupervisor(
        config_path=host.path, connector=connector
    )
    supervisor._pacing.record_failure(
        channel.worker_name, "oauth_challenge_not_advertised"
    )

    async def scenario():
        try:
            await supervisor._open_session(host, channel)
        except ConnectionError:
            return relay_pacing.channel_reconnect_state(
                host.path, channel.worker_name
            )
        raise AssertionError("the open should have failed")

    still = asyncio.run(scenario())
    assert still is not None
    assert still["reason"] == "oauth_challenge_not_advertised"

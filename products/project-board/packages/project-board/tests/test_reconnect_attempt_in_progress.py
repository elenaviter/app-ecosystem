"""A reconnect attempt that is running is reported as running (W461).

2026-10-02 02:58-03:00 UTC: a channel's reopen took 50 to 96 seconds during a
host stall. Its pacing record still showed the scheduled time of that attempt,
already past, so an agent was told to "retry after" a time that had gone. The
relay now records the attempt in progress when an open starts and clears it
when the open succeeds, fails or is cancelled.
"""

from __future__ import annotations

import asyncio
from argparse import Namespace
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from project_board.client import cli, first_run, relay_pacing as pacing
from project_board.contract.errors import DomainError

from relay_helpers import make_host as _host

PROJECT_REF = "work:project:demo-project"


class _Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _state(host, clock=None) -> pacing.RelayPacing:
    return pacing.RelayPacing(
        Path(host.path).parent / pacing.PACING_FILENAME, rng=lambda: 1.0, clock=clock or _Clock()
    )


def _failed(host, identity, clock=None) -> pacing.RelayPacing:
    state = _state(host, clock)
    state.record_failure(identity.worker_name, "oauth_metadata_request_failed")
    return state


def _record(host, identity) -> dict:
    return _state(host)._state["channels"].get(identity.worker_name) or {}


def _coordinate_args(host, identity) -> Namespace:
    return Namespace(
        command="coordinate",
        action="project.plan.item",
        contract=False,
        object_ref=PROJECT_REF,
        payload_json='{"item_key":"W1"}',
        payload_file="",
        runtime_kind=identity.runtime_kind,
        runtime_session_id=identity.runtime_session_id,
        config=str(host.path),
        route="relay",
        timeout_seconds=30.0,
    )


# -- the record ------------------------------------------------------------------


def test_an_attempt_on_a_failed_channel_is_recorded_and_shown_running(tmp_path):
    host, identity, _channel = _host(tmp_path)
    clock = _Clock()
    state = _failed(host, identity, clock)
    clock.now += 120.0  # the scheduled time has passed

    state.record_attempt_started(identity.worker_name)

    view = pacing.channel_reconnect_state(host.path, identity.worker_name, clock=clock)
    assert view["attempt_in_progress"] is True
    assert view["attempt_started_at"] == pacing._iso(clock.now)


def test_a_failure_ends_the_attempt_and_schedules_the_next(tmp_path):
    host, identity, _channel = _host(tmp_path)
    state = _failed(host, identity)
    state.record_attempt_started(identity.worker_name)

    state.record_failure(identity.worker_name, "oauth_metadata_request_failed")

    assert "attempt" not in _record(host, identity)
    view = pacing.channel_reconnect_state(host.path, identity.worker_name)
    assert view["attempt_in_progress"] is False and view["next_attempt_at"]


def test_a_cancelled_attempt_is_cleared(tmp_path):
    host, identity, _channel = _host(tmp_path)
    state = _failed(host, identity)
    state.record_attempt_started(identity.worker_name)

    state.record_attempt_ended(identity.worker_name)

    assert "attempt" not in _record(host, identity)
    assert _record(host, identity)["reason"] == "oauth_metadata_request_failed", "the failure stays"


def test_success_clears_the_attempt_with_the_failure(tmp_path):
    host, identity, _channel = _host(tmp_path)
    state = _failed(host, identity)
    state.record_attempt_started(identity.worker_name)

    state.record_success(identity.worker_name)

    assert pacing.channel_reconnect_state(host.path, identity.worker_name) is None


def test_a_healthy_channel_records_nothing_when_it_opens(tmp_path):
    host, identity, _channel = _host(tmp_path)

    _state(host).record_attempt_started(identity.worker_name)

    assert not (Path(host.path).parent / pacing.PACING_FILENAME).exists()


def test_a_mark_left_by_a_stopped_relay_is_not_reported_running(tmp_path):
    host, identity, _channel = _host(tmp_path)
    clock = _Clock()
    _failed(host, identity, clock).record_attempt_started(identity.worker_name)
    clock.now += pacing.ATTEMPT_STALE_SECONDS + 1

    view = pacing.channel_reconnect_state(host.path, identity.worker_name, clock=clock)

    assert view["attempt_in_progress"] is False


def test_a_relay_start_forgets_the_previous_process_attempt(tmp_path):
    host, identity, _channel = _host(tmp_path)
    _failed(host, identity).record_attempt_started(identity.worker_name)

    pacing.RelayPacing(
        Path(host.path).parent / pacing.PACING_FILENAME, forget_permanent=True
    )

    assert "attempt" not in _record(host, identity)


# -- what the agent reads ----------------------------------------------------------


def test_coordinate_says_the_attempt_is_running_not_a_past_time(tmp_path):
    host, identity, _channel = _host(tmp_path)
    clock = _Clock(now=__import__("time").time())
    state = _failed(host, identity, clock)
    state.record_attempt_started(identity.worker_name)

    with pytest.raises(DomainError) as refused:
        cli._coordinate_command(_coordinate_args(host, identity))

    error = refused.value
    assert error.code == "work_coordinate_channel_reconnecting"
    assert error.details["attempt_in_progress"] is True
    assert error.details["attempt_started_at"]
    assert "has been running since" in str(error)
    assert "Retry after that time" not in str(error)
    assert error.details["reauthorization_indicated"] is False


def test_coordinate_without_an_attempt_still_names_the_next_time(tmp_path):
    host, identity, _channel = _host(tmp_path)
    _failed(host, identity, _Clock(now=__import__("time").time()))

    with pytest.raises(DomainError) as refused:
        cli._coordinate_command(_coordinate_args(host, identity))

    assert refused.value.details["attempt_in_progress"] is False
    assert "Retry after that time" in str(refused.value)


def test_status_explains_a_running_attempt():
    step = first_run._next_step(
        first_run.SESSION_RECONNECTING,
        config="/tmp/relay.json",
        relay={"installed": True, "running": True},
        session={
            "connection": {
                "reason": "oauth_metadata_request_failed",
                "attempts": 2,
                "next_attempt_at": "2026-10-02T02:58:25Z",
                "attempt_in_progress": True,
                "attempt_started_at": "2026-10-02T02:58:25Z",
            }
        },
    )

    assert "has been running since 2026-10-02T02:58:25Z" in step["explain"]
    assert "retries on its own at" not in step["explain"]


# -- the relay sets and clears it around its own open -------------------------------


def _supervisor(host, connector):
    from project_board.client import relay

    return relay.ProblemBoardRelaySupervisor(config_path=host.path, connector=connector)


def test_the_relay_marks_its_open_and_clears_it_when_the_open_fails(tmp_path):
    host, identity, channel = _host(tmp_path)
    _failed(host, identity, _Clock(now=__import__("time").time()))
    seen: list[bool] = []

    @asynccontextmanager
    async def connector(_host, _channel, *, replacement_epoch):
        view = pacing.channel_reconnect_state(host.path, identity.worker_name)
        seen.append(view["attempt_in_progress"])
        raise DomainError("work_relay_transport_unavailable", "dropped", status=503)
        yield  # pragma: no cover

    supervisor = _supervisor(host, connector)
    with pytest.raises(DomainError):
        asyncio.run(supervisor._open_session(_reload(host), channel))

    assert seen == [True], "the open ran with the attempt recorded"
    assert "attempt" not in _record(host, identity)


def test_a_cancelled_open_clears_its_mark(tmp_path):
    host, identity, channel = _host(tmp_path)
    _failed(host, identity, _Clock(now=__import__("time").time()))
    entered = asyncio.Event()

    @asynccontextmanager
    async def connector(_host, _channel, *, replacement_epoch):
        entered.set()
        await asyncio.sleep(3600)
        yield  # pragma: no cover

    supervisor = _supervisor(host, connector)

    async def run() -> None:
        task = asyncio.create_task(supervisor._open_session(_reload(host), channel))
        await entered.wait()
        assert pacing.channel_reconnect_state(host.path, identity.worker_name)["attempt_in_progress"] is True
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())

    assert "attempt" not in _record(host, identity)


def _reload(host):
    from project_board.client.host_config import HostRelayConfig

    return HostRelayConfig.load(host.path)

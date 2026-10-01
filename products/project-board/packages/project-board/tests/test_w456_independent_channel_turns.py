"""Each relay channel runs its own turn; one slow channel delays only itself (W456).

On 2026-10-01 one or two channels' attendance polls and Data Bus connects
took 30-86 s, and every other channel on the host waited for them: the cycle
gathered all due channels and ended with the slowest. A channel whose socket
dropped could reopen only in the next cycle, so its pb coordinate calls
failed with 90 s deadlines while the server answered in under a second.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

from project_board.client import coordinate_queue, host_config
from project_board.contract.worker_identity import WorkerSessionIdentity

from relay_helpers import (
    StableClient,
    make_host,
    make_supervisor,
    submit_request,
)


def _two_channel_host(tmp_path):
    host, _identity, fast = make_host(tmp_path)
    slow = host_config.enroll_worker_channel(
        host.path,
        identity=WorkerSessionIdentity.create(
            "claude-code", "22222222-2222-4222-8222-222222222222"
        ),
        profile="problem-board-claude-two",
        authorized=True,
    )
    host = host_config.HostRelayConfig.load(host.path)
    root = host.connection_hub_state_root
    root.mkdir(parents=True, exist_ok=True)
    (root / "profiles.json").write_text(
        json.dumps(
            {
                "profiles": [
                    {
                        "name": channel.profile,
                        "endpoint": "https://runtime.example/mcp",
                        "credential_ref": f"ref-{channel.profile}",
                        "access_id": f"oauth-card-{channel.profile}",
                        "auth_type": "oauth",
                        "record_version": 3,
                        "updated_at": "t0",
                    }
                    for channel in (fast, slow)
                ]
            }
        ),
        encoding="utf-8",
    )
    return host, fast, slow


class _Attendance:
    """Each channel's attendance poll: the slow one waits for its gate."""

    def __init__(self, slow_name: str) -> None:
        self.slow_name = slow_name
        self.gate = asyncio.Event()
        self.polls: dict[str, int] = {}
        self.polled = asyncio.Event()

    async def poll(self, worker_name: str) -> dict:
        self.polls[worker_name] = self.polls.get(worker_name, 0) + 1
        self.polled.set()
        if worker_name == self.slow_name:
            await self.gate.wait()
        return {"worker_name": worker_name, "state": "attending"}


def _supervisor_with_fake_channels(host, attendance: _Attendance, *, grace: float):
    supervisor = make_supervisor(host)
    supervisor.CHANNEL_TURN_CYCLE_GRACE_SECONDS = grace
    opened: list[str] = []

    async def open_session(host_, channel):
        opened.append(channel.worker_name)

        async def aclose():
            return None

        client = StableClient()
        return SimpleNamespace(
            adapter=SimpleNamespace(
                client=client,
                poll_attendances_once=lambda: attendance.poll(channel.worker_name),
            ),
            client=client,
            closing=False,
            close_failure=None,
            profile=channel.profile,
            worker_name=channel.worker_name,
            channel_identity=channel.worker_identity,
            replacement_epoch=len(opened),
            card_fingerprint=supervisor._card_fingerprint(host_, channel),
            aclose=aclose,
        )

    async def nothing(*_args, **_kwargs):
        return None

    async def no_retirements(_host):
        return []

    supervisor._open_session = open_session
    supervisor._notify_available_input = nothing
    supervisor._reconcile_queues_before_channels = nothing
    supervisor._record_relay_channel_recovered = nothing
    supervisor._disable_locally_terminal_channels = no_retirements
    # Side servers and samplers have their own tests; here only turns run.
    supervisor._ensure_coordinate_server = lambda: None
    supervisor._ensure_outbox_server = lambda: None
    supervisor._ensure_local_state_maintenance = lambda _root: None
    supervisor._ensure_loop_lag_sampler = lambda: None
    return supervisor, opened


def _rows(result: dict) -> dict[str, dict]:
    return {row["worker_name"]: row for row in result["workers"]}


def test_a_hung_channel_does_not_hold_another_channels_turns(tmp_path):
    host, fast, slow = _two_channel_host(tmp_path)

    async def scenario():
        attendance = _Attendance(slow.worker_name)
        supervisor, _opened = _supervisor_with_fake_channels(
            host, attendance, grace=0.05
        )
        try:
            first = await asyncio.wait_for(supervisor.poll_once(), timeout=2)
            second = await asyncio.wait_for(supervisor.poll_once(), timeout=2)
        finally:
            attendance.gate.set()
            await supervisor.aclose()
        assert attendance.polls[fast.worker_name] == 2
        assert attendance.polls[slow.worker_name] == 1, "a running turn is not started twice"
        for result in (first, second):
            rows = _rows(result)
            assert rows[fast.worker_name]["state"] == "attending"
            assert rows[slow.worker_name]["state"] == "running"
            assert rows[slow.worker_name]["reason"] == "turn_in_progress"

    asyncio.run(scenario())


def test_a_turn_past_its_deadline_fails_alone_with_a_named_outcome(tmp_path):
    host, fast, slow = _two_channel_host(tmp_path)

    async def scenario():
        attendance = _Attendance(slow.worker_name)
        supervisor, _opened = _supervisor_with_fake_channels(
            host, attendance, grace=2.0
        )
        supervisor.CHANNEL_TURN_DEADLINE_SECONDS = 0.2
        try:
            result = await asyncio.wait_for(supervisor.poll_once(), timeout=3)
        finally:
            attendance.gate.set()
            await supervisor.aclose()
        rows = _rows(result)
        assert rows[fast.worker_name]["state"] == "attending"
        failed = rows[slow.worker_name]
        assert failed["state"] == "error"
        assert failed["error_code"] == "work_relay_channel_turn_deadline_exceeded"
        assert failed["retryable"] is True
        assert slow.worker_name not in supervisor._sessions, "the hung session is dropped"
        assert supervisor._pacing.channel_due(fast.worker_name)
        assert not supervisor._pacing.channel_due(slow.worker_name), "only the hung channel backs off"

    asyncio.run(scenario())


def test_a_turn_that_ends_after_its_cycle_wakes_the_next_cycle(tmp_path):
    host, _fast, slow = _two_channel_host(tmp_path)

    async def scenario():
        attendance = _Attendance(slow.worker_name)
        supervisor, _opened = _supervisor_with_fake_channels(
            host, attendance, grace=0.05
        )
        try:
            first = await asyncio.wait_for(supervisor.poll_once(), timeout=2)
            assert _rows(first)[slow.worker_name]["state"] == "running"
            attendance.gate.set()
            started = time.monotonic()
            stopping = await asyncio.wait_for(
                supervisor.wait_for_wakeup(30.0), timeout=5
            )
            woke_after = time.monotonic() - started
            second = await asyncio.wait_for(supervisor.poll_once(), timeout=2)
        finally:
            await supervisor.aclose()
        assert stopping is False
        assert woke_after < 2.0, f"the late turn woke the loop after {woke_after:.2f}s"
        assert _rows(second)[slow.worker_name]["state"] == "attending"

    asyncio.run(scenario())


def test_a_dropped_channel_reopens_and_serves_its_calls_while_another_hangs(tmp_path):
    host, fast, slow = _two_channel_host(tmp_path)
    queue = coordinate_queue.CoordinateQueue(host.field_root)

    async def scenario():
        attendance = _Attendance(slow.worker_name)
        supervisor, opened = _supervisor_with_fake_channels(
            host, attendance, grace=0.05
        )

        async def run_relay():
            # What the host relay runtime does: a cycle, a short wait, again.
            while True:
                await supervisor.poll_once()
                await asyncio.sleep(0.05)

        relay_loop = asyncio.create_task(run_relay())
        try:
            await asyncio.wait_for(attendance.polled.wait(), timeout=2)
            while attendance.polls.get(fast.worker_name, 0) < 1:
                await asyncio.sleep(0.01)
            # The fast channel's socket drops while the slow channel hangs.
            await supervisor._drop_session(fast.worker_name)
            request = submit_request(queue, fast)
            started = time.monotonic()
            response = None
            while response is None and time.monotonic() - started < 2.0:
                await asyncio.sleep(0.02)
                response = queue.take_response(
                    worker_name=fast.worker_name, request_id=request["request_id"]
                )
            served_after = time.monotonic() - started
        finally:
            relay_loop.cancel()
            await asyncio.gather(relay_loop, return_exceptions=True)
            attendance.gate.set()
            await supervisor.aclose()
        assert response is not None and response["ok"] is True, (
            "the dropped channel's call waited on the hung channel"
        )
        assert served_after < 2.0
        assert opened.count(fast.worker_name) >= 2, "the dropped channel reopened"
        assert attendance.polls[slow.worker_name] == 1

    asyncio.run(scenario())

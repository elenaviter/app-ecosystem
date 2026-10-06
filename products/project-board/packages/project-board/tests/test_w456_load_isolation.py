"""W456 under host load: no channel waits on another's file read, and the loop never reads files (2026-10-06).

On dev-main (16 CPUs, APFS 94 % full, swap 8.2 of 9.2 GB) small file reads took
seconds. Coordinate calls then waited 10-13 s and the event loop stalled 4.7 s:
- the cycle and every wakeup wait read the host config and the fault file on
  the shared event loop;
- one coordinate-server thread checked every channel's request queue and Card
  in turn, so one slow read held every channel's coordinate calls;
- each channel's poll ran its file reads in the loop's shared default pool.

Each case runs the production supervisor with fake sessions and models the slow
host by blocking the real reads. No read is cached: every Card and session
fence still reads the profile file at the moment it decides (operator, 2026-10-06:
"we are distributed system, and no we do not use any module level caches!").
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid

from project_board.client import coordinate_queue, host_config, relay
from project_board.client.host_config import WorkerSessionIdentity

from relay_helpers import Attendance, make_host, submit_request, supervisor_with_fake_channels


def _host_with_channels(tmp_path, count):
    """A host with ``count`` authorized channels and their local profiles, in enrolment order."""

    host, _identity, first = make_host(tmp_path)
    channels = [first]
    for index in range(1, count):
        channels.append(host_config.enroll_worker_channel(
            host.path,
            identity=WorkerSessionIdentity.create("claude-code", str(uuid.UUID(int=index + 1000))),
            profile=f"problem-board-load-{index}", authorized=True))
    host = host_config.HostRelayConfig.load(host.path)
    root = host.connection_hub_state_root
    root.mkdir(parents=True, exist_ok=True)
    _write_profiles(root, channels)
    return host, channels


def _write_profiles(root, channels, *, access_ids=None):
    access_ids = access_ids or {}
    (root / "profiles.json").write_text(json.dumps({"profiles": [
        {"name": channel.profile, "endpoint": "https://runtime.example/mcp",
         "credential_ref": f"ref-{channel.profile}",
         "access_id": access_ids.get(channel.worker_name, f"oauth-card-{channel.profile}"),
         "auth_type": "oauth", "record_version": 3, "updated_at": "t0"}
        for channel in channels]}), encoding="utf-8")


class _Gaps:
    """The largest gap between event-loop wake-ups while it runs."""

    def __init__(self) -> None:
        self.largest = 0.0
        self._stop = False

    async def run(self) -> None:
        last = time.monotonic()
        while not self._stop:
            await asyncio.sleep(0.005)
            now = time.monotonic()
            self.largest = max(self.largest, now - last)
            last = now

    def stop(self) -> None:
        self._stop = True


def test_a_slow_host_config_read_never_stalls_the_event_loop(tmp_path, monkeypatch):
    host, _channels = _host_with_channels(tmp_path, 4)
    real_load = host_config.HostRelayConfig.load.__func__
    reads = []

    def slow_load(cls, path, *args, **kwargs):
        reads.append(threading.current_thread() is threading.main_thread())
        time.sleep(0.4)  # dev-main's 4.7 s, scaled
        return real_load(cls, path, *args, **kwargs)

    async def scenario():
        attendance = Attendance(slow_name="-none-")
        attendance.gate.set()
        supervisor, _opened = supervisor_with_fake_channels(host, attendance)
        monkeypatch.setattr(host_config.HostRelayConfig, "load", classmethod(slow_load))
        gaps = _Gaps()
        ticker = asyncio.create_task(gaps.run())
        try:
            await asyncio.wait_for(supervisor.poll_once(), timeout=10)
            await asyncio.wait_for(supervisor.wait_for_wakeup(0.05), timeout=10)
        finally:
            gaps.stop()
            await ticker
            await supervisor.aclose()
        assert reads, "the config was read"
        assert not any(reads), "a host config read ran on the event loop's thread"
        assert gaps.largest < 0.2, f"the event loop stalled {gaps.largest:.2f}s on a config read"

    asyncio.run(scenario())


def test_one_channels_hung_card_read_never_holds_another_channels_coordinate_calls(tmp_path, monkeypatch):
    host, channels = _host_with_channels(tmp_path, 16)
    slow, fast = channels[0], channels[-1]  # the hung channel is checked first
    queue = coordinate_queue.CoordinateQueue(host.field_root)
    hung = threading.Event()
    release = threading.Event()
    real_entry = relay.ProblemBoardRelaySupervisor._profile_entry

    def entry(host_, channel):
        if channel.worker_name == slow.worker_name and not release.is_set():
            hung.set()
            release.wait(10)
        return real_entry(host_, channel)

    async def scenario():
        attendance = Attendance(slow_name="-none-")
        attendance.gate.set()
        supervisor, _opened = supervisor_with_fake_channels(host, attendance)
        try:
            await asyncio.wait_for(supervisor.poll_once(), timeout=10)
            monkeypatch.setattr(relay.ProblemBoardRelaySupervisor, "_profile_entry", staticmethod(entry))
            slow_request = submit_request(queue, slow)
            fast_request = submit_request(queue, fast)
            # The production server loop (the helper stubs its starter out).
            server = asyncio.create_task(relay.ProblemBoardRelaySupervisor._serve_coordinate_requests(supervisor))
            started = time.monotonic()
            await asyncio.to_thread(hung.wait, 5)
            response = None
            while response is None and time.monotonic() - started < 3.0:
                await asyncio.sleep(0.02)
                response = queue.take_response(worker_name=fast.worker_name,
                                               request_id=fast_request["request_id"])
            served_after = time.monotonic() - started
            slow_response = queue.take_response(worker_name=slow.worker_name,
                                                request_id=slow_request["request_id"])
        finally:
            release.set()
            server.cancel()
            await asyncio.gather(server, return_exceptions=True)
            await supervisor.stop_coordinate_server()
            await supervisor.aclose()
        assert hung.is_set(), "the slow channel's Card read was reached"
        assert response is not None and response["ok"] is True, (
            "another channel's coordinate call waited on the hung Card read")
        assert served_after < 1.0, f"served after {served_after:.2f}s"
        assert slow_response is None, "the hung channel's own call waits for its own read"

    asyncio.run(scenario())


def test_a_full_shared_thread_pool_never_holds_a_channels_poll(tmp_path):
    host, channels = _host_with_channels(tmp_path, 2)

    async def scenario():
        attendance = Attendance(slow_name="-none-")
        attendance.gate.set()
        supervisor, _opened = supervisor_with_fake_channels(host, attendance)
        loop = asyncio.get_running_loop()
        await asyncio.wait_for(supervisor.poll_once(), timeout=10)
        polled = dict(attendance.polls)
        # Something else on the host occupies every thread of the loop's shared pool.
        blocker = threading.Event()
        workers = loop._default_executor._max_workers if loop._default_executor else 64
        held = [loop.run_in_executor(None, blocker.wait, 10) for _ in range(max(workers, 64))]
        try:
            await asyncio.sleep(0.05)
            started = time.monotonic()
            await asyncio.wait_for(supervisor.poll_once(), timeout=3)
            took = time.monotonic() - started
        finally:
            blocker.set()
            await asyncio.gather(*held)
            await supervisor.aclose()
        assert took < 1.0, f"a channel's poll waited {took:.2f}s for the shared pool"
        assert all(attendance.polls[c.worker_name] > polled.get(c.worker_name, 0) for c in channels)

    asyncio.run(scenario())


def test_a_card_replaced_between_two_checks_of_one_pass_is_never_dispatched(tmp_path, monkeypatch):
    """No cache: the second, fenced Card read sees the replacement made after the first."""

    host, channels = _host_with_channels(tmp_path, 3)
    target = channels[1]
    queue = coordinate_queue.CoordinateQueue(host.field_root)
    root = host.connection_hub_state_root
    real_ready = relay.ProblemBoardRelaySupervisor._coordinate_ready

    def ready_then_replace(self, host_, candidates):
        ready = real_ready(self, host_, candidates)
        if any(name == target.worker_name for name, _session in ready):
            _write_profiles(root, channels, access_ids={target.worker_name: "oauth-card-replaced"})
        return ready

    async def scenario():
        attendance = Attendance(slow_name="-none-")
        attendance.gate.set()
        supervisor, _opened = supervisor_with_fake_channels(host, attendance)
        try:
            await asyncio.wait_for(supervisor.poll_once(), timeout=10)
            session = supervisor._sessions[target.worker_name]
            monkeypatch.setattr(relay.ProblemBoardRelaySupervisor, "_coordinate_ready", ready_then_replace)
            request = submit_request(queue, target)
            started = await asyncio.wait_for(supervisor.serve_coordinate_pass(), timeout=5)
            await asyncio.sleep(0.2)
        finally:
            await supervisor.stop_coordinate_server()
            await supervisor.aclose()
        assert target.worker_name not in started, "the replaced Card's session was given a drain"
        assert session.client.calls == [], "a request went through the session of the replaced Card"
        assert queue.take_response(worker_name=target.worker_name, request_id=request["request_id"]) is None

    asyncio.run(scenario())

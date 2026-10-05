from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from project_board.client import host_config, relay
from project_board.contract.worker_identity import WorkerSessionIdentity


def make_host(tmp_path: Path, *, authorized: bool = True):
    host = host_config.initialize_host_config(
        target_id="target",
        endpoint="https://runtime.example/mcp",
        tenant="tenant",
        platform_project="project",
        host_id="host-one",
        allowed_roots=[str(tmp_path)],
        source_repositories={},
        config_path=tmp_path / "relay.json",
        state_root=tmp_path / "state",
    )
    identity = WorkerSessionIdentity.create(
        "codex", "11111111-1111-4111-8111-111111111111"
    )
    channel = host_config.enroll_worker_channel(
        host.path,
        identity=identity,
        profile="problem-board-codex-one",
        authorized=authorized,
    )
    return host_config.HostRelayConfig.load(host.path), identity, channel


def submit_request(queue, channel, **overrides: Any) -> dict[str, Any]:
    values = {
        "worker_name": channel.worker_name,
        "worker_identity": channel.worker_identity,
        "runtime_kind": channel.runtime_kind,
        "runtime_session_id": channel.runtime_session_id,
        "action": "project.plan.item",
        "object_ref": "work:project:one",
        "payload": {"item_key": "W1"},
        "timeout_seconds": 30,
    }
    values.update(overrides)
    return queue.submit(**values)


class StableClient:
    connected = True

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def action_with_transport_identity(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(dict(kwargs))
        return {
            "ok": True,
            "object": {"ref": kwargs["object_ref"], "worker": "card-caller"},
        }


def make_supervisor(host):
    async def connector(
        _host, _channel, *, replacement_epoch
    ):  # pragma: no cover - focused tests supply an existing session
        raise AssertionError("the focused test supplies an existing session")

    return relay.ProblemBoardRelaySupervisor(
        config_path=host.path,
        connector=connector,
    )


class LoopHeartbeat:
    """The largest gap between event-loop wake-ups while it runs.

    The gap since the last wake is counted at stop too: a blocked loop may let
    the test resume and stop the heartbeat before the heartbeat runs again.
    """

    def __init__(self) -> None:
        self.max_gap = 0.0
        self._last = time.monotonic()
        self._task: asyncio.Task | None = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(0.02)
            now = time.monotonic()
            self.max_gap = max(self.max_gap, now - self._last)
            self._last = now

    def start(self) -> None:
        self._last = time.monotonic()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        assert self._task is not None
        self.max_gap = max(self.max_gap, time.monotonic() - self._last)
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)


def two_channel_host(tmp_path):
    """A codex channel (``fast``) and a claude-code channel (``slow``) with local profiles."""

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


class Attendance:
    """Each channel's attendance poll: the slow one waits for its gate."""

    def __init__(self, slow_name: str, *, slow_fails: bool = False) -> None:
        self.slow_name = slow_name
        self.slow_fails = slow_fails
        self.gate = asyncio.Event()
        self.polls: dict[str, int] = {}
        self.polled = asyncio.Event()

    async def poll(self, worker_name: str) -> dict:
        self.polls[worker_name] = self.polls.get(worker_name, 0) + 1
        self.polled.set()
        if worker_name == self.slow_name:
            await self.gate.wait()
            if self.slow_fails:
                raise ConnectionError("the slow channel's socket dropped")
        return {"worker_name": worker_name, "state": "attending"}


def supervisor_with_fake_channels(host, attendance: Attendance, *, real_notify: bool = False):
    """The production supervisor with fake sessions; its turn timings stay default."""

    supervisor = make_supervisor(host)
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
            # Read off the loop, as the production open does (W456).
            card_fingerprint=await asyncio.to_thread(supervisor._card_fingerprint, host_, channel),
            aclose=aclose,
        )

    async def nothing(*_args, **_kwargs):
        return None

    async def no_retirements(_host):
        return []

    supervisor._open_session = open_session
    if not real_notify:
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

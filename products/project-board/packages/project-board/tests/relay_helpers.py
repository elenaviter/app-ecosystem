from __future__ import annotations

from pathlib import Path
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

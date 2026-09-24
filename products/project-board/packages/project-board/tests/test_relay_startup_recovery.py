"""Relay-process recovery from authorization startup failures (W52)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import pytest
from service_foundation.host_relay import HostRelayPolicy, HostRelayRuntime

from project_board.client import host_config, relay, relay_pacing
from project_board.client.store import SharedFieldStore

from relay_helpers import make_host


class _StructuredConnectionHubError(RuntimeError):
    """A Connection Hub-shaped failure without relying on its Python class."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = dict(details or {})


class _RelayClient:
    connected = True

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def action(
        self,
        *,
        object_ref: str,
        action: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = dict(payload or {})
        self.calls.append(
            {"object_ref": object_ref, "action": action, "payload": body}
        )
        if action == "worker.publish":
            worker: dict[str, Any] = {
                "ref": "work:worker:remote",
                "pool_status": "active",
            }
            if body.get("relay_degraded_intervals"):
                worker["relay_degraded_intervals"] = list(
                    body["relay_degraded_intervals"]
                )
            return {"ok": True, "object": worker}
        if action == "worker.heartbeat":
            return {
                "ok": True,
                "object": {
                    "ref": "work:worker:remote",
                    "attendances": [],
                    "host_retirements": [],
                },
            }
        if action == "control.pull":
            return {"ok": True, "object": {"lease_id": "lease", "items": []}}
        return {"ok": True, "object": {"ref": object_ref}}


def _registered_host(tmp_path):
    host, identity, channel = make_host(tmp_path)
    field = SharedFieldStore(host.field_root)
    field.register_worker(
        worker_name=identity.worker_name,
        worker_alias="worker",
        worker_identity=identity.worker_identity,
        runtime_kind=identity.runtime_kind,
        runtime_session_id=identity.runtime_session_id,
        capabilities=[],
        authority_label="connection-hub:test",
        host_id=host.host_id,
        host_label=host.host_label,
        host_kind=host.host_kind,
        relay_id=host.relay_id,
    )
    return host, identity, channel, field


@pytest.mark.asyncio
async def test_metadata_rejection_at_startup_is_retried_without_ending_relay(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(relay_pacing, "CHANNEL_BACKOFF_BASE_SECONDS", 0.0)
    host, identity, _channel, field = _registered_host(tmp_path)
    client = _RelayClient()
    attempts = 0

    @asynccontextmanager
    async def connector(_host, _channel, *, replacement_epoch):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise _StructuredConnectionHubError(
                "oauth_metadata_request_failed",
                "The OAuth metadata endpoint returned HTTP 503 while the catalog loaded.",
                status=503,
                details={
                    "method": "GET",
                    "url": (
                        "https://runtime.example/.well-known/"
                        "oauth-protected-resource"
                    ),
                    "status": 503,
                    "server_reason": "Application catalog is loading",
                },
            )
        yield client

    supervisor = relay.ProblemBoardRelaySupervisor(
        config_path=host.path,
        connector=connector,
    )
    retry_wait_started = asyncio.Event()
    allow_retry = asyncio.Event()
    degraded = asyncio.Event()
    recovered = asyncio.Event()
    stop = asyncio.Event()
    waits: list[float] = []
    events = []

    async def wait_for_wakeup(
        timeout_seconds: float, stop_event: asyncio.Event | None = None
    ) -> bool:
        waits.append(timeout_seconds)
        if len(waits) == 1:
            retry_wait_started.set()
            await allow_retry.wait()
        return bool(stop_event is not None and stop_event.is_set())

    async def observe(event) -> None:
        events.append(event)
        if event.kind == "cycle.retryable_failure":
            degraded.set()
        elif event.kind == "cycle.succeeded":
            recovered.set()
            stop.set()

    supervisor.wait_for_wakeup = wait_for_wakeup  # type: ignore[method-assign]
    runtime = HostRelayRuntime(
        adapter=supervisor,
        policy=HostRelayPolicy(
            poll_interval_seconds=60.0,
            retry_initial_seconds=0.01,
            retry_max_seconds=0.02,
        ),
        observer=observe,
    )
    task = asyncio.create_task(runtime.run(stop_event=stop))
    try:
        await asyncio.wait_for(degraded.wait(), timeout=1)
        await asyncio.wait_for(retry_wait_started.wait(), timeout=1)

        assert not task.done(), "a transient startup rejection ended the relay"
        assert runtime.health.state == "degraded"
        assert runtime.health.consecutive_failures == 1
        assert waits == [pytest.approx(0.01)]
        assert host_config.HostRelayConfig.load(host.path).worker(identity).state == (
            "active"
        )
        diagnostic = field.read_worker(identity.worker_name)["relay_diagnostic"]
        assert diagnostic["state"] == "degraded"
        assert diagnostic["code"] == "oauth_metadata_request_failed"
        assert diagnostic["started_at"]
        assert diagnostic["request"]["status"] == 503
        assert diagnostic["request"]["server_reason"] == (
            "Application catalog is loading"
        )

        allow_retry.set()
        await asyncio.wait_for(recovered.wait(), timeout=1)
        await asyncio.wait_for(task, timeout=1)
    finally:
        stop.set()
        allow_retry.set()
        if not task.done():
            await asyncio.wait_for(task, timeout=1)
        await supervisor.aclose()

    assert attempts == 2
    assert runtime.health.cycles_succeeded == 1
    assert [event.kind for event in events[:4]] == [
        "runtime.started",
        "cycle.started",
        "cycle.retryable_failure",
        "cycle.started",
    ]
    recovered_diagnostic = field.read_worker(identity.worker_name)[
        "relay_diagnostic"
    ]
    assert recovered_diagnostic["state"] == "ready"
    assert recovered_diagnostic["code"] == ""
    assert len(recovered_diagnostic["recent"]) == 1
    interval = recovered_diagnostic["recent"][0]
    assert interval["code"] == "oauth_metadata_request_failed"
    assert interval["started_at"] and interval["ended_at"]
    assert interval["published_at"]


@pytest.mark.asyncio
async def test_permanent_card_rejection_stops_and_names_the_reason(
    tmp_path, caplog
) -> None:
    host, identity, _channel, field = _registered_host(tmp_path)
    attempts = 0

    @asynccontextmanager
    async def connector(_host, _channel, *, replacement_epoch):
        nonlocal attempts
        attempts += 1
        raise _StructuredConnectionHubError(
            "delegated_card_revoked",
            "The delegated Card was revoked.",
            status=401,
        )
        yield  # pragma: no cover

    supervisor = relay.ProblemBoardRelaySupervisor(
        config_path=host.path,
        connector=connector,
    )
    runtime = HostRelayRuntime(adapter=supervisor)

    with pytest.raises(_StructuredConnectionHubError) as raised:
        await runtime.run_once()
    await supervisor.aclose()

    assert raised.value.code == "delegated_card_revoked"
    assert attempts == 1
    assert runtime.health.state == "failed"
    assert runtime.health.consecutive_failures == 1
    restarted = relay_pacing.RelayPacing(
        host.path.parent / relay_pacing.PACING_FILENAME,
        forget_permanent=True,
    )
    assert restarted.channel_due(identity.worker_name) is False
    assert restarted.restart_decisions[identity.worker_name] == {
        "decision": "kept_backoff",
        "reason": "delegated_card_revoked",
    }
    assert field.read_worker(identity.worker_name)["relay_diagnostic"]["state"] == (
        "ready"
    )
    assert "error_code=delegated_card_revoked" in caplog.text

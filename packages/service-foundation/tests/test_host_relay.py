from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest

from service_foundation.host_relay import (
    HostRelayEvent,
    HostRelayPolicy,
    HostRelayRetryableError,
    HostRelayRuntime,
)


class _Adapter:
    adapter_id = "synthetic"

    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    async def poll_once(self) -> Mapping[str, Any]:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.mark.asyncio
async def test_run_once_reports_result_and_health_without_domain_assumptions() -> None:
    events: list[HostRelayEvent] = []
    adapter = _Adapter([{"materialized": 2}])
    runtime = HostRelayRuntime(adapter=adapter, observer=events.append)

    result = await runtime.run_once()

    assert result == {"materialized": 2}
    assert runtime.health.state == "ready"
    assert runtime.health.cycles_started == 1
    assert runtime.health.cycles_succeeded == 1
    assert [event.kind for event in events] == ["cycle.started", "cycle.succeeded"]
    assert events[-1].details == {"result_keys": ["materialized"]}


@pytest.mark.asyncio
async def test_runtime_retries_only_classified_transient_failure() -> None:
    stop = asyncio.Event()

    class _StoppingAdapter(_Adapter):
        async def poll_once(self) -> Mapping[str, Any]:
            result = await super().poll_once()
            stop.set()
            return result

    adapter = _StoppingAdapter(
        [HostRelayRetryableError("transport_unavailable", "Retry later."), {"ok": True}]
    )
    runtime = HostRelayRuntime(
        adapter=adapter,
        policy=HostRelayPolicy(
            poll_interval_seconds=0.01,
            retry_initial_seconds=0.01,
            retry_max_seconds=0.02,
        ),
    )

    await runtime.run(stop_event=stop)

    assert adapter.calls == 2
    assert runtime.health.state == "stopped"
    assert runtime.health.cycles_succeeded == 1
    assert runtime.health.consecutive_failures == 0


@pytest.mark.asyncio
async def test_unclassified_adapter_failure_stops_runtime() -> None:
    runtime = HostRelayRuntime(adapter=_Adapter([RuntimeError("domain failure")]))

    with pytest.raises(RuntimeError, match="domain failure"):
        await runtime.run()

    assert runtime.health.state == "stopped"
    assert runtime.health.cycles_started == 1
    assert runtime.health.last_error_code == "host_relay_adapter_failed"


def test_policy_rejects_invalid_retry_bounds() -> None:
    with pytest.raises(ValueError, match="retry_max_seconds"):
        HostRelayPolicy(retry_initial_seconds=10, retry_max_seconds=5)

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


@pytest.mark.asyncio
async def test_runtime_honors_adapter_next_poll_hint_and_falls_back_to_policy() -> None:
    stop = asyncio.Event()
    waits: list[float] = []

    class _HintingAdapter(_Adapter):
        async def poll_once(self) -> Mapping[str, Any]:
            result = await super().poll_once()
            if not self.outcomes:
                stop.set()
            return result

    adapter = _HintingAdapter([{"next_poll_seconds": 0.02}, {"next_poll_seconds": 0}, {"ok": True}])
    runtime = HostRelayRuntime(adapter=adapter, policy=HostRelayPolicy(poll_interval_seconds=0.01))

    async def _recording_wait(delay: float, stop_event: asyncio.Event | None) -> bool:
        waits.append(delay)
        return stop_event is not None and stop_event.is_set()

    runtime._wait = _recording_wait  # type: ignore[method-assign]
    await runtime.run(stop_event=stop)

    # A positive hint wins; zero, negative, or missing hints fall back to the policy interval.
    assert waits == [0.02, 0.01, 0.01]
    assert adapter.calls == 3


@pytest.mark.asyncio
async def test_runtime_lets_adapter_wake_next_cycle_before_poll_interval() -> None:
    stop = asyncio.Event()

    class _PushAdapter(_Adapter):
        def __init__(self) -> None:
            super().__init__([{"cycle": 1}, {"cycle": 2}])
            self.waits: list[float] = []

        async def poll_once(self) -> Mapping[str, Any]:
            result = await super().poll_once()
            if self.calls == 2:
                stop.set()
            return result

        async def wait_for_wakeup(
            self, timeout_seconds: float, stop_event: asyncio.Event | None
        ) -> bool:
            self.waits.append(timeout_seconds)
            assert stop_event is stop
            return False

    adapter = _PushAdapter()
    runtime = HostRelayRuntime(
        adapter=adapter,
        policy=HostRelayPolicy(poll_interval_seconds=60),
    )

    await runtime.run(stop_event=stop)

    assert adapter.calls == 2
    assert adapter.waits == [60, 60]

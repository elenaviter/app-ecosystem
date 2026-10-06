"""W461 C4 (c): the Data Bus client's own reconnect loop, test-only.

Ops asked (6 October 2026, 05:31 UTC) whether the client's `_reconnect` loop
is bounded, or whether only the relay replacing the session stops it. The
loop retries a dropped socket while the client is open, doubling its delay up
to `_RECONNECT_DELAY_MAX_SECONDS`; it has no attempt limit and ends only when
the client is closed, which is what the relay does when it replaces a session.

Synthetic only: no server, no credential; the connect is a stub that always
fails, and the delay is recorded instead of slept.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from app_foundation.data_bus import DataBusClaim, FederatedDataBusClient
from app_foundation.data_bus import client as client_module


def _claim() -> DataBusClaim:
    return DataBusClaim(
        tenant="tenant-c4",
        project="project-c4",
        bundle_id="problem-board@1-0",
        session_id="session-c4",
        expires_at=2_000_000_000,
        federated_token="synthetic-not-a-credential",
        partition_ref="work:worker-stream:c4",
    )


class _AsyncioWithRecordedSleep:
    """The asyncio module as the client sees it, with sleep recorded, not slept."""

    def __init__(self, delays: list[float]) -> None:
        self._delays = delays
        self._real_sleep = asyncio.sleep

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)

    async def sleep(self, delay: float, *args: Any, **kwargs: Any) -> None:
        self._delays.append(float(delay))
        await self._real_sleep(0)


def _drive(monkeypatch, *, close_after: int, ceiling: int) -> tuple[list[float], int, bool]:
    """Run the loop against a connect that always fails; close after ``close_after`` attempts."""

    delays: list[float] = []
    monkeypatch.setattr(client_module, "asyncio", _AsyncioWithRecordedSleep(delays))
    client = FederatedDataBusClient(platform_url="http://127.0.0.1:9", claim=_claim())
    attempts = 0

    async def failing_connect() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == close_after:
            asyncio.get_running_loop().create_task(client.close())
        if attempts >= ceiling:
            raise asyncio.CancelledError
        raise ConnectionError("synthetic: the bus is unreachable")

    client._connect_namespace = failing_connect  # type: ignore[method-assign]

    async def scenario() -> None:
        task = asyncio.get_running_loop().create_task(client._reconnect())
        client._reconnect_task = task
        try:
            await asyncio.wait_for(task, timeout=5)
        except asyncio.CancelledError:
            pass  # close() cancels the loop, or the test's safety ceiling did

    asyncio.run(scenario())
    # The loop stopped before the safety ceiling only if close() (or the loop
    # itself) ended it.
    return delays, attempts, attempts < ceiling


def test_c_reconnect_delays_double_to_a_cap(monkeypatch, record_property):
    started = time.monotonic()
    delays, attempts, _ = _drive(monkeypatch, close_after=12, ceiling=1_000)
    record_property("c_data_bus_delays_seconds", ",".join(f"{delay:g}" for delay in delays))
    record_property("c_data_bus_drive_ms", round((time.monotonic() - started) * 1000, 3))

    assert delays[0] == client_module._RECONNECT_DELAY_SECONDS
    assert all(later >= earlier for earlier, later in zip(delays, delays[1:]))
    assert max(delays) == client_module._RECONNECT_DELAY_MAX_SECONDS


def test_c_closing_the_client_ends_the_reconnect_loop(monkeypatch, record_property):
    # The relay closes the old client when it replaces the session; that is
    # what ends the loop today.
    delays, attempts, ended = _drive(monkeypatch, close_after=3, ceiling=1_000)
    record_property("c_data_bus_attempts_until_close", attempts)

    assert ended is True
    assert attempts == 3, "no attempt after the client was closed"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "W461 C4 finding: the Data Bus client's _reconnect loop has no attempt limit; "
        "it retries every _RECONNECT_DELAY_MAX_SECONDS until the client is closed"
    ),
)
def test_c_the_reconnect_loop_ends_by_itself_after_a_bounded_number_of_attempts(monkeypatch):
    _delays, attempts, ended = _drive(monkeypatch, close_after=0, ceiling=200)

    assert ended is True and attempts < 200, "the loop gave up on its own before 200 attempts"

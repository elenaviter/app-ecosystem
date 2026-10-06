"""W461 C4 (c): the Data Bus client's own reconnect loop, test-only.

Ops asked (6 October 2026, 05:31 UTC) whether the client's `_reconnect` loop
is bounded, or whether only the relay replacing the session stops it. The
loop retries a dropped socket while the client is open, doubling its delay up
to `_RECONNECT_DELAY_MAX_SECONDS`; it has no attempt limit and ends only when
the client is closed, which is what the relay does when it replaces a session.

W573 adds an owner-supplied refusal classifier: a permanent refusal ends the
loop in the named state refused_permanent; without a classifier the loop
retries until close(), which stays the pinned default.

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


class _Refused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(f"synthetic refusal {code}")
        self.code = code


def _drive(
    monkeypatch, *, close_after: int, ceiling: int, classifier=None, error=None, holder=None, on_terminal=None,
) -> tuple[list[float], int, bool]:
    """Run the loop against a connect that always fails; close after ``close_after`` attempts."""

    delays: list[float] = []
    monkeypatch.setattr(client_module, "asyncio", _AsyncioWithRecordedSleep(delays))
    client = FederatedDataBusClient(
        platform_url="http://127.0.0.1:9", claim=_claim(), refusal_classifier=classifier,
        on_terminal_refusal=on_terminal,
    )
    if holder is not None:
        holder.append(client)
    attempts = 0

    async def failing_connect() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == close_after:
            asyncio.get_running_loop().create_task(client.close())
        if attempts >= ceiling:
            raise asyncio.CancelledError
        raise error if error is not None else ConnectionError("synthetic: the bus is unreachable")

    client._connect_namespace = failing_connect  # type: ignore[method-assign]

    async def scenario() -> None:
        task = asyncio.get_running_loop().create_task(client._reconnect())
        client._reconnect_task = task
        try:
            await asyncio.wait_for(task, timeout=5)
        except asyncio.CancelledError:
            pass  # close() cancels the loop, or the test's safety ceiling did
        finally:
            # Close and drain every client this helper creates, so no
            # reconnect task outlives the event loop.
            await client.close()

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


def test_c_without_a_classifier_the_loop_retries_until_close(monkeypatch):
    # W573 [1]: the default consumer behaviour is pinned. A client whose owner
    # supplies no classifier keeps retrying on the capped delay until close().
    _delays, attempts, ended = _drive(monkeypatch, close_after=0, ceiling=200)

    # The test's safety ceiling cancels the task; the loop's own handoff then
    # starts a fresh one, which is the resident behaviour being pinned.
    assert ended is False and attempts >= 200, "no classifier: the loop does not end by itself"


def test_c_a_permanent_refusal_ends_the_loop_in_a_named_state(monkeypatch, record_property):
    # W573: Mint's finding 3. The owner's classifier calls the refusal
    # permanent; the loop stops after that one attempt, by itself.
    holder: list = []
    started = time.monotonic()
    _delays, attempts, ended = _drive(
        monkeypatch, close_after=0, ceiling=200, holder=holder,
        classifier=lambda error: getattr(error, "code", "") == "synthetic_permanent",
        error=_Refused("synthetic_permanent"),
    )
    record_property("c_data_bus_attempts_until_terminal", attempts)
    record_property("c_data_bus_terminal_ms", round((time.monotonic() - started) * 1000, 3))

    assert ended is True and attempts == 1
    client = holder[0]
    assert client.terminal_refusal == {
        "state": "refused_permanent", "code": "synthetic_permanent", "error_type": "_Refused",
    }
    assert client.transport_recovering is False, "the owner replaces the session rather than waiting"


def test_c_a_transient_failure_keeps_retrying_with_a_classifier(monkeypatch):
    holder: list = []
    _delays, attempts, ended = _drive(
        monkeypatch, close_after=5, ceiling=200, holder=holder,
        classifier=lambda error: getattr(error, "code", "") == "synthetic_permanent",
    )

    assert ended is True and attempts == 5, "only close() ended it"
    assert holder[0].terminal_refusal is None


def test_c_a_failing_classifier_is_treated_as_transient(monkeypatch):
    def broken(error):
        raise RuntimeError("synthetic classifier fault")

    holder: list = []
    _delays, attempts, _ended = _drive(
        monkeypatch, close_after=4, ceiling=200, holder=holder, classifier=broken, error=_Refused("anything"),
    )

    assert attempts == 4 and holder[0].terminal_refusal is None


def test_c_the_owner_is_told_exactly_once_and_close_stays_idempotent(monkeypatch):
    # Ops' refinements via Root (06:10 UTC): exactly-once terminal
    # notification; close() idempotent, resources cleaned, no reconnect.
    told: list = []
    holder: list = []
    _delays, attempts, ended = _drive(
        monkeypatch, close_after=0, ceiling=200, holder=holder, on_terminal=told.append,
        classifier=lambda error: True, error=_Refused("synthetic_permanent"),
    )
    client = holder[0]

    async def close_twice():
        await client.close()
        await client.close()
        return client._reconnect_task

    remaining_task = asyncio.run(close_twice())

    assert ended is True and attempts == 1
    assert told == [{"state": "refused_permanent", "code": "synthetic_permanent", "error_type": "_Refused"}]
    assert remaining_task is None, "no reconnect task left after close"
    assert client.transport_recovering is False


def test_c_a_failing_terminal_callback_never_restarts_the_loop(monkeypatch):
    def broken(_terminal):
        raise RuntimeError("synthetic owner fault")

    holder: list = []
    _delays, attempts, ended = _drive(
        monkeypatch, close_after=0, ceiling=200, holder=holder, on_terminal=broken,
        classifier=lambda error: True, error=_Refused("synthetic_permanent"),
    )

    assert ended is True and attempts == 1
    assert holder[0].terminal_refusal["state"] == "refused_permanent"


def _terminal_client(attempts: list[int]) -> FederatedDataBusClient:
    client = FederatedDataBusClient(
        platform_url="http://127.0.0.1:9", claim=_claim(), refusal_classifier=lambda error: True,
    )
    client._reconnect_delay_seconds = 0

    async def refused() -> None:
        attempts.append(1)
        raise _Refused("synthetic_permanent")

    client._connect_namespace = refused  # type: ignore[method-assign]
    return client


def test_c_a_terminal_client_refuses_a_public_connect_without_an_attempt():
    # Infra's witness on cdbc3045 (W573 review, 6 October 2026, 06:26 UTC):
    # the public connect() opened a terminal client again.
    attempts: list[int] = []

    async def scenario() -> None:
        client = _terminal_client(attempts)
        try:
            client._reconnect_task = asyncio.get_running_loop().create_task(client._reconnect())
            await asyncio.wait_for(client._reconnect_task, 1)
            assert len(attempts) == 1 and client.terminal_refusal["state"] == "refused_permanent"
            with pytest.raises(client_module.DataBusClientError) as refused:
                await client.connect()
            assert refused.value.code == "synthetic_permanent"
            assert refused.value.details["terminal_refusal"]["state"] == "refused_permanent"
            assert len(attempts) == 1, "a terminal client made a second open attempt through connect()"
        finally:
            await client.close()

    asyncio.run(scenario())


def test_c_a_terminal_client_is_fenced_on_disconnect_and_reconnect_entry():
    attempts: list[int] = []

    async def scenario() -> None:
        client = _terminal_client(attempts)
        try:
            client._reconnect_task = asyncio.get_running_loop().create_task(client._reconnect())
            await asyncio.wait_for(client._reconnect_task, 1)
            assert len(attempts) == 1 and client._reconnect_task is None

            # A late disconnect of an active socket does not start a loop.
            client._connection_generation = max(1, client._connection_generation)
            client._socket_is_active = lambda socket, token: True  # type: ignore[method-assign]
            client._deactivate_socket = lambda socket, token: None  # type: ignore[method-assign]
            await client._on_disconnect(object(), object())
            assert client._reconnect_task is None

            # A loop entered directly returns at once, with no attempt.
            await asyncio.wait_for(client._reconnect(), 1)
            assert len(attempts) == 1
            assert client.terminal_refusal["state"] == "refused_permanent"
        finally:
            await client.close()

    asyncio.run(scenario())

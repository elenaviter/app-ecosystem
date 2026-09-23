"""A runtime that is not there is retried soon, and forgotten when it is back (W265).

2026-09-23, 16:10 to 16:21 UTC: chat-proc was down after a rebuild, the MCP
endpoint answered 404, each channel's open failed five times with
oauth_challenge_not_advertised, and the doubling backoff put the next attempt
at 16:31. The platform was healthy at 16:21 and every channel stayed down ten
more minutes. A relay restart kept the schedule, since it is on disk.
"""

from __future__ import annotations

import json

from project_board.client import relay, relay_pacing
from project_board.client.relay_admission import is_runtime_unavailable
from project_board.contract.errors import DomainError

from relay_helpers import make_host, make_supervisor


class Clock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _not_advertised() -> DomainError:
    return DomainError("oauth_challenge_not_advertised", "The endpoint did not advertise a challenge.", status=404)


def test_the_runtime_being_down_is_told_apart_from_a_refusal():
    assert is_runtime_unavailable(_not_advertised()) is True
    unreachable = DomainError("oauth_mcp_endpoint_unreachable", "The MCP endpoint could not be reached for OAuth discovery.")
    assert is_runtime_unavailable(unreachable) is True
    assert is_runtime_unavailable(ConnectionRefusedError("connection refused")) is True
    # 18:04 the same day: four channels opened together against a runtime
    # still down. The first held the profile store lock through its hung
    # refresh, the other three timed out on the lock and then waited on the
    # doubling schedule for 3.5 minutes after the runtime was back.
    locked = DomainError("oauth_profile_lock_timeout", "Timed out waiting for the OAuth profile lock.")
    assert is_runtime_unavailable(locked) is True
    wrapped = RuntimeError("open failed")
    wrapped.__cause__ = _not_advertised()
    assert is_runtime_unavailable(wrapped) is True

    refused = DomainError("delegated_card_refresh_refused", "The card refused to refresh.", status=400)
    assert is_runtime_unavailable(refused) is False
    # Load and mid-flight failures keep the doubling: a timeout, an unknown
    # outcome, a reset connection.
    assert is_runtime_unavailable(TimeoutError("slow")) is False
    unknown = DomainError("data_bus_outcome_unknown", "no receipt", status=504)
    unknown.details = {"status": 504}
    assert is_runtime_unavailable(unknown) is False
    assert is_runtime_unavailable(ConnectionResetError("reset")) is False
    # A 503 that names Retry-After is the gateway asking this host to wait:
    # the host quiet rule reads it, not the runtime schedule.
    quiet = DomainError("work_relay_transport_unavailable", "busy", status=503)
    quiet.details = {"status": 503, "retry_after_seconds": 30}
    assert is_runtime_unavailable(quiet) is False
    assert relay_pacing.rate_limit_wait(quiet) == 30.0


def test_five_failures_during_an_outage_leave_the_channel_due_within_ten_seconds_of_recovery():
    clock = Clock()
    pacing = relay_pacing.RelayPacing(None, clock=clock, rng=lambda: 1.0)

    delays = []
    for _ in range(5):
        delays.append(pacing.record_failure("w", "oauth_challenge_not_advertised", runtime_unavailable=True))
        assert pacing.channel_due("w") is False
        clock.now += delays[-1]
        assert pacing.channel_due("w") is True
    assert delays == [5.0, 10.0, 10.0, 10.0, 10.0], "no doubling, no jitter, ten seconds at most"
    assert pacing.snapshot()["channels"]["w"]["schedule"] == relay_pacing.RUNTIME_SCHEDULE
    assert pacing.soonest_runtime_retry_seconds() == 0.0

    # The runtime is back: the open succeeds and the record is gone.
    pacing.record_success("w")
    assert pacing.snapshot()["channels"] == {}
    assert pacing.soonest_runtime_retry_seconds() is None


def test_a_long_outage_falls_to_once_a_minute_and_never_feeds_the_doubling():
    clock = Clock()
    pacing = relay_pacing.RelayPacing(None, clock=clock, rng=lambda: 1.0)

    for _ in range(20):
        clock.now += pacing.record_failure("w", "oauth_challenge_not_advertised", runtime_unavailable=True)
    assert clock.now - 1_000_000.0 < relay_pacing.RUNTIME_RETRY_WINDOW_SECONDS
    clock.now = 1_000_000.0 + relay_pacing.RUNTIME_RETRY_WINDOW_SECONDS + 1
    assert pacing.record_failure("w", "oauth_challenge_not_advertised", runtime_unavailable=True) == 60.0

    # A refusal after all that starts the normal backoff at one minute, as if
    # the runtime attempts had never happened.
    assert pacing.record_failure("w", "delegated_card_refresh_refused") == relay_pacing.CHANNEL_BACKOFF_BASE_SECONDS
    assert pacing.snapshot()["channels"]["w"]["schedule"] == "backoff"
    assert pacing.record_failure("w", "delegated_card_refresh_refused") == 2 * relay_pacing.CHANNEL_BACKOFF_BASE_SECONDS


def test_a_relay_start_forgets_the_channels_that_waited_on_the_runtime(tmp_path):
    clock = Clock()
    path = tmp_path / relay_pacing.PACING_FILENAME
    pacing = relay_pacing.RelayPacing(path, clock=clock, rng=lambda: 1.0)
    for _ in range(5):
        pacing.record_failure("down", "oauth_challenge_not_advertised", runtime_unavailable=True)
    pacing.record_failure("refused", "delegated_card_refresh_refused")
    assert pacing.channel_due("down") is False and pacing.channel_due("refused") is False

    # Same process, no restart: the schedule stands, as it must across cycles.
    again = relay_pacing.RelayPacing(path, clock=clock, rng=lambda: 1.0)
    assert again.channel_due("down") is False

    restarted = relay_pacing.RelayPacing(path, clock=clock, rng=lambda: 1.0, forget_permanent=True)
    assert restarted.channel_due("down") is True, "the relay is restarted because the runtime is back"
    assert restarted.channel_due("refused") is False, "a refusal keeps its backoff across a restart"
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert set(stored["channels"]) == {"refused"}


def test_one_channel_back_clears_every_channel_that_waited_on_the_runtime():
    clock = Clock()
    pacing = relay_pacing.RelayPacing(None, clock=clock, rng=lambda: 1.0)
    for name in ("a", "b", "c"):
        pacing.record_failure(name, "oauth_challenge_not_advertised", runtime_unavailable=True)
    pacing.record_failure("refused", "delegated_card_refresh_refused")

    pacing.record_success("a")

    assert pacing.channel_due("b") is True and pacing.channel_due("c") is True
    assert pacing.channel_due("refused") is False, "a refusal is that channel's own answer"
    assert set(pacing.snapshot()["channels"]) == {"refused"}


def test_the_relay_classifies_the_open_failure_and_pulls_the_cycle_to_the_retry(tmp_path):
    host, _identity, channel = make_host(tmp_path)
    supervisor = make_supervisor(host)
    clock = Clock()
    pacing = relay_pacing.RelayPacing(None, clock=clock, rng=lambda: 1.0)

    supervisor._record_channel_failure(pacing, channel.worker_name, _not_advertised())
    view = pacing.snapshot()["channels"][channel.worker_name]
    assert view["schedule"] == relay_pacing.RUNTIME_SCHEDULE
    assert view["reason"] == "oauth_challenge_not_advertised"

    # A failed cycle would otherwise keep the base interval (30 s and more):
    # the channel waiting on the runtime pulls the next cycle to its retry.
    assert supervisor._cycle_next_poll([30], [_not_advertised()], pacing) == 5
    clock.now += 2
    assert supervisor._cycle_next_poll([], [_not_advertised()], pacing) == 3
    pacing.record_success(channel.worker_name)
    assert supervisor._cycle_next_poll([30], [_not_advertised()], pacing) is None
    assert supervisor._cycle_next_poll([30, 12], [], pacing) == 12

    refused = DomainError("delegated_card_refresh_refused", "The card refused to refresh.", status=400)
    supervisor._record_channel_failure(pacing, channel.worker_name, refused)
    assert pacing.snapshot()["channels"][channel.worker_name]["schedule"] == "backoff"

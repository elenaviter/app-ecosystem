"""What a relay start does with each channel's backoff (W292).

On 2026-09-24 Card admission was broken for about an hour. The channels it
refused doubled their backoff, and when the fix went live and the relay was
restarted, two of them still waited fifteen minutes. A restart now tries every
channel refused for a transient reason at once, and parks a credential the
server refused until it is authorized again.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from project_board.client.credential_refusal import credential_refused
from project_board.contract.errors import DomainError
from project_board.client.relay_pacing import (
    CHANNEL_BACKOFF_BASE_SECONDS,
    PACING_FILENAME,
    RelayPacing,
)


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _pacing(path: Path, clock: Clock, *, start: bool = False) -> RelayPacing:
    # rng 1.0 removes the jitter, so delays compare exactly.
    return RelayPacing(path, clock=clock, rng=lambda: 1.0, forget_permanent=start)


def test_a_long_data_bus_refusal_backoff_connects_at_once_after_a_restart(tmp_path, caplog):
    path = tmp_path / PACING_FILENAME
    clock = Clock()
    pacing = _pacing(path, clock)
    for _ in range(6):
        delay = pacing.record_failure("claude-main", "data_bus_connect_refused")
    assert delay == 1800.0, "the doubling reached the cap while admission was broken"
    clock.now += 60
    assert pacing.channel_due("claude-main") is False

    with caplog.at_level(logging.INFO, logger="project_board.client.relay_pacing"):
        restarted = _pacing(path, clock, start=True)

    assert restarted.channel_due("claude-main") is True, "one attempt at once, whatever the backoff"
    assert restarted.restart_decisions["claude-main"] == {
        "decision": "attempted",
        "reason": "data_bus_connect_refused",
    }
    assert "relay pacing restart worker=claude-main decision=attempted reason=data_bus_connect_refused" in caplog.text
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["channels"]["claude-main"]["attempts"] == 6, "the count survives, only the time moved"


def test_a_failed_restart_attempt_returns_to_its_schedule_never_faster(tmp_path):
    path = tmp_path / PACING_FILENAME
    clock = Clock()
    pacing = _pacing(path, clock)
    delays = [pacing.record_failure("codex-main", "data_bus_connect_refused") for _ in range(3)]
    assert delays == [CHANNEL_BACKOFF_BASE_SECONDS, 120.0, 240.0]

    restarted = _pacing(path, clock, start=True)
    assert restarted.channel_due("codex-main") is True
    after = restarted.record_failure("codex-main", "data_bus_connect_refused")

    assert after == 480.0, "the next step of the same schedule"
    assert after >= delays[-1]
    assert restarted.channel_due("codex-main") is False


def test_a_revoked_credential_stays_parked_across_restarts_until_it_is_authorized_again(tmp_path, caplog):
    path = tmp_path / PACING_FILENAME
    clock = Clock()
    pacing = _pacing(path, clock)
    pacing.record_pending_refusal(
        "codex-ui",
        fingerprint="card-a|session-1",
        permanent=True,
        reason="oauth_token_request_failed",
        credential=True,
    )
    assert pacing.pending_due("codex-ui", "card-a|session-1") is False

    with caplog.at_level(logging.INFO, logger="project_board.client.relay_pacing"):
        restarted = _pacing(path, clock, start=True)
        again = _pacing(path, clock, start=True)

    for relay in (restarted, again):
        assert relay.pending_due("codex-ui", "card-a|session-1") is False, "a restart never retries a dead credential"
        assert relay.restart_decisions["codex-ui"]["decision"] == "parked_permanent"
    assert "decision=parked_permanent reason=oauth_token_request_failed" in caplog.text
    assert "server refused this credential" in again.snapshot()["pending"]["codex-ui"]["retry"]
    # pb worker authorize rewrites the profile: the new fingerprint is tried at once.
    assert again.pending_due("codex-ui", "card-a|session-2") is True


def test_a_permanent_refusal_a_server_fix_can_clear_is_tried_once_on_restart(tmp_path):
    path = tmp_path / PACING_FILENAME
    clock = Clock()
    pacing = _pacing(path, clock)
    pacing.record_pending_refusal(
        "worker-a", fingerprint="card-b", permanent=True, reason="delegated_capability_not_granted"
    )
    assert pacing.pending_due("worker-a", "card-b") is False

    restarted = _pacing(path, clock, start=True)

    assert restarted.pending_due("worker-a", "card-b") is True
    assert restarted.restart_decisions["worker-a"]["decision"] == "attempted"


def test_a_credential_refused_channel_and_a_rate_limited_host_keep_their_backoff(tmp_path):
    path = tmp_path / PACING_FILENAME
    clock = Clock()
    pacing = _pacing(path, clock)
    pacing.record_failure("refused", "delegated_card_refresh_refused", credential=True)
    restarted = _pacing(path, clock, start=True)
    assert restarted.channel_due("refused") is False
    assert restarted.restart_decisions["refused"] == {
        "decision": "kept_backoff",
        "reason": "delegated_card_refresh_refused",
    }

    quiet_path = tmp_path / "quiet" / PACING_FILENAME
    quiet = _pacing(quiet_path, clock)
    quiet.record_failure("claude-main", "data_bus_connect_refused")
    quiet._state["host_quiet_until"] = clock.now + 900  # a 429 named 15 minutes
    quiet._save()

    in_window = _pacing(quiet_path, clock, start=True)

    assert in_window.channel_due("claude-main") is False, "a restart does not lift the gateway's wait"
    assert in_window.restart_decisions["claude-main"] == {
        "decision": "kept_backoff",
        "reason": "host_rate_limited",
    }


def test_a_token_endpoint_that_was_down_is_attempted_at_restart_under_the_same_code(tmp_path):
    """claude-main's review of #65: at 01:32Z a 503 ("token withheld: delegated
    card conflict") and the later 400 invalid_grant shared one code. Only the
    second is the credential's answer."""

    path = tmp_path / PACING_FILENAME
    clock = Clock()
    pacing = _pacing(path, clock)
    down = DomainError(
        "oauth_token_request_failed",
        "token withheld: delegated card conflict",
        status=503,
        details={"status": 503},
    )
    dead = DomainError(
        "oauth_token_request_failed",
        "The refresh token was revoked.",
        status=400,
        details={"status": 400, "oauth_error": "invalid_grant"},
    )
    assert credential_refused(down) is False
    assert credential_refused(dead) is True
    for name, error in (("worker-down", down), ("worker-dead", dead)):
        pacing.record_pending_refusal(
            name,
            fingerprint=f"card-{name}",
            permanent=True,
            reason="oauth_token_request_failed",
            credential=credential_refused(error),
        )

    restarted = _pacing(path, clock, start=True)

    assert restarted.restart_decisions["worker-down"]["decision"] == "attempted"
    assert restarted.pending_due("worker-down", "card-worker-down") is True
    assert restarted.restart_decisions["worker-dead"]["decision"] == "parked_permanent"
    assert restarted.pending_due("worker-dead", "card-worker-dead") is False


def test_the_credential_predicate_reads_wrapped_errors_and_revoked_cards():
    dead = DomainError(
        "oauth_token_request_failed", "revoked", details={"oauth_error": "invalid_grant"}
    )
    try:
        try:
            raise dead
        except DomainError as inner:
            raise RuntimeError("channel failed") from inner
    except RuntimeError as outer:
        wrapped = outer
    assert credential_refused(wrapped) is True
    assert credential_refused(DomainError("delegated_card_revoked", "revoked")) is True
    assert credential_refused(DomainError("delegated_card_refresh_refused", "refused")) is True
    assert credential_refused(DomainError("data_bus_connect_refused", "refused")) is False


def test_a_record_written_before_the_flag_existed_is_attempted_once(tmp_path):
    path = tmp_path / PACING_FILENAME
    clock = Clock()
    path.write_text(
        json.dumps(
            {
                "host_quiet_until": 0,
                "channels": {"codex-ui": {"attempts": 0, "next_at": clock.now + 1800, "reason": "oauth_token_request_failed"}},
                "pending": {
                    "codex-ui": {
                        "fingerprint": "card-a",
                        "permanent": True,
                        "reason": "oauth_token_request_failed",
                        "refused_at": clock.now,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    restarted = _pacing(path, clock, start=True)

    assert restarted.restart_decisions["codex-ui"]["decision"] == "attempted"
    assert restarted.pending_due("codex-ui", "card-a") is True

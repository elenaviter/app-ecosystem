"""A wake is pushed or the log says why not, and a rebuild leaves no channel closed (W265).

2026-09-23, after the 17:18 platform rebuild: Codex workers with mail on disk
got no wake for minutes across three relay starts and two relay versions, and
the relay log had no line about any wake decision. Two things the surviving
state proved: one Codex channel never opened at the first start (a doubling
backoff record with a runtime reason, written by the previous relay during the
outage, kept it deferred), and every branch that declines a push was silent.
"""

from __future__ import annotations

from project_board.client import relay, relay_pacing


def test_a_failed_queue_listing_withholds_a_wake_only_while_an_earlier_one_is_in_doubt():
    failed = {"reconciled": False, "reason": "codex_app_server_not_started"}

    # Nothing outstanding: a duplicate is impossible, the wake goes out.
    assert relay.wake_withheld_by_reconciliation(failed, {}) == ""
    assert relay.wake_withheld_by_reconciliation(failed, {"wake_delivery_state": "", "last_acknowledged_wake_id": "wake_1"}) == ""
    assert relay.wake_withheld_by_reconciliation(failed, {"wake_delivery_state": "failed"}) == ""

    # An earlier wake whose outcome is unknown: wait for the listing.
    for state in ("attempting", "queued", "consumed"):
        assert relay.wake_withheld_by_reconciliation(failed, {"wake_delivery_state": state}).startswith("queue_reconciliation_failed")
    assert relay.wake_withheld_by_reconciliation(failed, {"outstanding_wake_id": "wake_2"}) == "queue_reconciliation_failed_with_outstanding_wake"

    # A listing that worked, or none (a Claude Code channel), never withholds.
    assert relay.wake_withheld_by_reconciliation({"reconciled": True}, {"wake_delivery_state": "queued"}) == ""
    assert relay.wake_withheld_by_reconciliation(None, {"wake_delivery_state": "queued"}) == ""


def test_a_relay_start_forgets_an_outage_record_written_before_the_runtime_schedule(tmp_path):
    path = tmp_path / relay_pacing.PACING_FILENAME
    clock = lambda: 1_000_000.0  # noqa: E731
    pacing = relay_pacing.RelayPacing(path, clock=clock, rng=lambda: 1.0)
    # The old relay recorded the outage on the doubling schedule, five times.
    for _ in range(5):
        pacing.record_failure("codex-ui", "oauth_challenge_not_advertised")
    pacing.record_failure("refused", "delegated_card_refresh_refused")
    assert pacing.channel_due("codex-ui") is False
    assert pacing.snapshot()["channels"]["codex-ui"]["schedule"] == "backoff"

    restarted = relay_pacing.RelayPacing(path, clock=clock, rng=lambda: 1.0, forget_permanent=True)

    assert restarted.channel_due("codex-ui") is True, "the rebuilt relay opens the channel at once"
    assert restarted.channel_due("refused") is False, "a refusal keeps its backoff"

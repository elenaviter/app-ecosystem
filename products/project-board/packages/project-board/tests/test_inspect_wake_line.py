"""A Claude Code session's watch has no native wake to fail (rehearsal, 2026-09-26).

`pb worker inspect` showed "wake delivery failed" for a session-owned watch
while its inbox check was current: the relay attempted a native wake for a
runtime that has none. The relay no longer attempts one for that adapter, and
inspect says the field does not apply.
"""

from __future__ import annotations

from pathlib import Path

from project_board.client import relay, render


def _inspect(adapter: str, state: str) -> str:
    result = {
        "session": {"state": "listening", "inbox_check_state": "current",
                    "subscription": {"adapter": adapter, "state": "session_owned", "wake_delivery_state": state}},
        "channel": {"worker_name": "claude-code-x", "state": "active"},
        "worker": {}, "authorization": {"state": "active"},
    }
    return "\n".join(render._render_inspect(result))  # noqa: SLF001


def test_inspect_says_wake_delivery_does_not_apply_to_a_session_owned_watch():
    line = _inspect("session-owned-watch", "failed")
    assert "wake delivery n/a (session-owned watch)" in line
    assert "wake delivery failed" not in line


def test_a_codex_queue_still_shows_its_wake_state():
    assert "wake delivery queued" in _inspect("codex-queue", "queued")


def test_the_relay_attempts_no_native_wake_for_a_session_owned_watch():
    source = Path(relay.__file__).read_text(encoding="utf-8")
    guard = 'if str(subscription.get("adapter") or "") == "session-owned-watch":'
    assert guard in source
    # The guard sits before the wake is prepared.
    assert source.index(guard) < source.index("withheld = wake_withheld_by_reconciliation(queue_reconciliation, subscription)")

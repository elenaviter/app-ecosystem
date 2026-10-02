"""The W461 triage script labels each OAuth failure by the evidence present."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[5] / "scripts" / "oauth_failure_triage.py"

RELAY = """\
2026-10-02T10:00:01.000Z INFO connection_hub.oauth.spans: Connection Hub OAuth span kind=transaction operation=commit_refreshed profile=abc123def456 outcome=ok wait_ms=3 hold_ms=9800 pid=1 task=Task-7
2026-10-02T10:00:05.000Z WARNING project_board.client.relay: Problem Board worker channel failed worker=codex-aaaa profile=p error_type=E error_code=oauth_profile_lock_timeout retryable=True message=Timed out waiting for the OAuth profile lock.
2026-10-02T10:01:00.000Z WARNING project_board.client.relay: Problem Board relay loop blocked blocked_seconds=3.1 threshold_seconds=3.000
2026-10-02T10:01:02.000Z WARNING project_board.client.relay: Problem Board worker channel failed worker=claude-bbbb profile=p error_type=E error_code=oauth_token_request_failed retryable=True failure_kind="ConnectError" message=x (request_id 0123456789abcdef).
2026-10-02T10:02:00.000Z WARNING project_board.client.relay: Problem Board worker channel failed worker=claude-bbbb profile=p error_type=E error_code=oauth_metadata_request_failed retryable=True failure_kind="ReadError" message=y (request_id fedcba9876543210).
"""


def _run(tmp_path: Path) -> list[str]:
    log = tmp_path / "relay.stderr.log"
    log.write_text(RELAY)
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "--relay-log", str(log),
         "--since", "2026-10-02T10:00:00Z", "--until", "2026-10-02T10:05:00Z"],
        capture_output=True, text=True, check=True,
    ).stdout
    return out.splitlines()


def test_each_failure_gets_its_evidence_label(tmp_path):
    lines = _run(tmp_path)
    lock, token, metadata = lines[:3]
    assert "oauth_profile_lock_timeout" in lock
    assert "lock-holder-seen:transaction/commit_refreshed/9800ms" in lock
    assert "0123456789abcdef no-proxy-record loop-blocked:1" in token
    assert "fedcba9876543210 no-proxy-record" in metadata and "loop-blocked" not in metadata
    assert lines[3].startswith("--- 3 OAuth failures, 1 spans (1 held 250 ms or more), 1 loop-block lines")


def test_a_lock_timeout_without_span_lines_is_not_called_holderless(tmp_path):
    log = tmp_path / "relay.stderr.log"
    log.write_text(RELAY.splitlines(keepends=True)[1])
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "--relay-log", str(log),
         "--since", "2026-10-02T10:00:00Z", "--until", "2026-10-02T10:05:00Z"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "no-span-evidence" in out and "no-holder-seen" not in out

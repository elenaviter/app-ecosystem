"""The W461 triage script labels each OAuth failure by the evidence present, and only that."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[5] / "scripts" / "oauth_failure_triage.py"
PROFILE = "problem-board-codex-0123456789ab"
TAG = hashlib.sha256(PROFILE.encode()).hexdigest()[:12]
OTHER_TAG = hashlib.sha256(b"another-profile").hexdigest()[:12]
WINDOW = ["--since", "2026-10-02T10:00:00Z", "--until", "2026-10-02T10:05:00Z"]


def _span(at, kind, op, tag, hold_ms, outcome="ok"):
    return (f"{at} INFO connection_hub.oauth.spans: Connection Hub OAuth span kind={kind} operation={op} "
            f"profile={tag} outcome={outcome} wait_ms=3 hold_ms={hold_ms} pid=1 task=t00abcd\n")


def _failed(at, code, extra=""):
    return (f"{at} WARNING project_board.client.relay: Problem Board worker channel failed worker=codex-aaaa "
            f"profile={PROFILE} error_type=E error_code={code} retryable=True {extra}message=x.\n")


def _run(tmp_path: Path, relay: str, *extra: str, env=None) -> str:
    log = tmp_path / "relay.stderr.log"
    log.write_text(relay)
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--relay-log", str(log), *WINDOW, *extra],
        capture_output=True, text=True, check=True, env=env,
    ).stdout


def test_a_lock_holder_counts_only_when_it_overlaps_the_failed_wait(tmp_path):
    relay = (
        _span("2026-10-02T10:00:01.000Z", "transaction", "commit_refreshed", OTHER_TAG, 9800)
        + _failed("2026-10-02T10:00:05.000Z", "oauth_profile_lock_timeout")
    )
    out = _run(tmp_path, relay)
    assert f"holder-overlap:transaction/commit_refreshed/{OTHER_TAG}/ok/9800ms" in out


def test_an_unrelated_span_before_the_wait_is_not_a_holder(tmp_path):
    relay = (
        # A refresh slot of the same profile, finished 14 s before the failure: before the 10 s wait began.
        _span("2026-10-02T10:00:01.000Z", "refresh_slot", "refresh", TAG, 400)
        # Another profile's refresh slot overlapping the wait cannot block this profile's slot.
        + _span("2026-10-02T10:00:14.000Z", "refresh_slot", "refresh", OTHER_TAG, 5000)
        + _failed("2026-10-02T10:00:15.000Z", "oauth_profile_lock_timeout")
    )
    out = _run(tmp_path, relay)
    assert "no-overlapping-holder" in out and "holder-overlap" not in out


def test_a_lock_timeout_without_span_lines_is_not_called_holderless(tmp_path):
    out = _run(tmp_path, _failed("2026-10-02T10:00:05.000Z", "oauth_profile_lock_timeout"))
    assert "no-span-evidence" in out and "no-overlapping-holder" not in out


def test_tunnel_messages_are_counted_never_copied(tmp_path):
    tunnel = tmp_path / "ngrok.log"
    tunnel.write_text(
        't=2026-10-02T12:01:03+0200 lvl=warn msg="failed https://private.example.internal/x?token=CANARY-SECRET-123"\n'
        't=2026-10-02T12:01:04+0200 lvl=info msg="join connections"\n'
    )
    relay = _failed("2026-10-02T10:01:02.000Z", "oauth_token_request_failed",
                    'failure_kind="ConnectError" (request_id 0123456789abcdef) ')
    out = _run(tmp_path, relay, "--tunnel-log", str(tunnel))
    assert "tunnel-warn:1" in out
    assert "CANARY" not in out and "private.example" not in out


def test_an_unreadable_proxy_log_is_not_evidence_of_absence(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text("#!/bin/sh\necho 'Cannot connect to the Docker daemon' >&2\nexit 1\n")
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    relay = _failed("2026-10-02T10:01:02.000Z", "oauth_token_request_failed",
                    'failure_kind="ReadError" (request_id 0123456789abcdef) ')
    out = _run(tmp_path, relay, "--proxy-container", "web-proxy", env=env)
    assert "proxy-log-unavailable" in out and "no-proxy-record" not in out
    assert "Docker daemon" not in out


def test_a_read_proxy_log_without_the_id_is_no_proxy_record(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\necho '1.2.3.4 - - [x] \"GET /a HTTP/1.1\" 200 5 \"-\" \"ua\" \"-\" rid=\"fedcba9876543210\" rt=0.010'\n"
    )
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    relay = (
        _failed("2026-10-02T10:01:02.000Z", "oauth_token_request_failed",
                'failure_kind="ReadError" (request_id 0123456789abcdef) ')
        + _failed("2026-10-02T10:02:02.000Z", "oauth_metadata_request_failed",
                  'failure_kind="ReadError" (request_id fedcba9876543210) ')
    )
    out = _run(tmp_path, relay, "--proxy-container", "web-proxy", env=env)
    first, second = out.splitlines()[:2]
    assert "no-proxy-record" in first and "reached-proxy:200" in second

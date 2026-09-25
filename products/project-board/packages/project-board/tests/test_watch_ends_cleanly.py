"""A replaced watch ends as completed, not failed (W182, operator 2026-09-25).

The Claude Code guard starts a new `pb worker watch` and stops the older one
with SIGTERM every cycle, and the harness reported each stopped watch as
"script failed (exit 144)". SIGTERM is a watch's normal end: it exits 0 and
prints nothing, because every stdout line is an event.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from project_board.client.store import SharedFieldStore
from relay_helpers import make_host


def test_sigterm_ends_the_watch_with_status_0_and_no_output(tmp_path):
    host, identity, _channel = make_host(tmp_path)
    field = SharedFieldStore(host.field_root)
    field.register_worker(
        worker_name=identity.worker_name,
        runtime_kind=identity.runtime_kind,
        runtime_session_id=identity.runtime_session_id,
        capabilities=[],
        authority_label="connection-hub:test",
    )
    field.listen_worker(identity.worker_name)
    env = {**os.environ, "PROBLEM_BOARD_CONFIG": str(host.path), "PYTHONPATH": os.pathsep.join(sys.path)}
    watch = subprocess.Popen(
        [
            sys.executable, "-c", "import sys; from project_board.client import cli; sys.exit(cli.main())",
            "worker", "watch", "--runtime-kind", identity.runtime_kind,
            "--runtime-session-id", identity.runtime_session_id, "--check-interval", "5",
        ],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 20
        while field.read_watch_attachment(identity.worker_name) is None:
            assert watch.poll() is None, watch.communicate()
            assert time.monotonic() < deadline, "the watch never recorded its attachment"
            time.sleep(0.05)
        watch.send_signal(signal.SIGTERM)
        stdout, stderr = watch.communicate(timeout=10)
    finally:
        if watch.poll() is None:
            watch.kill()
    assert watch.returncode == 0, stderr
    assert stdout == ""

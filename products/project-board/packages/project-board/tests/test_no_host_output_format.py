"""The host's output format never reaches a test (W349).

Agents run the suites from shells with `PB_FORMAT=brief` exported, as the
worker procedure tells them to. A board test that ran `pb` in-process and read
its JSON failed in exactly those runs, on 2026-09-27, three times, and passed
whenever the variable was not exported.
"""

from __future__ import annotations

import os
import subprocess
import sys


def test_pb_format_is_not_in_a_test_environment() -> None:
    assert "PB_FORMAT" not in os.environ


def test_a_child_process_does_not_inherit_it() -> None:
    child = subprocess.run(
        [sys.executable, "-c", "import os; print(os.environ.get('PB_FORMAT', 'unset'))"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert child.stdout.strip() == "unset"
